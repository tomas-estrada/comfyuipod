#!/usr/bin/env python3
"""
scripts/status-server.py — Servidor ligero de estado, triage e informe Markdown (§4 y §6)
Escucha exclusivamente en 127.0.0.1:18189 (expuesto vía Nginx con HTTP Basic Auth).
Usa únicamente la biblioteca estándar de Python 3.12.
"""

from __future__ import annotations

import collections
import datetime
import html
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from typing import Any

HOST = "127.0.0.1"
PORT = int(os.environ.get("STATUS_SERVER_PORT", "18189"))

LOG_WHITELIST: dict[str, str] = {
    "comfyui": "/var/log/comfyui.log",
    "nginx-error": "/var/log/nginx/error.log",
    "nginx-access": "/var/log/nginx/access.log",
    "download": "/var/log/download-models.log",
    "download-models": "/var/log/download-models.log",
    "supervisor": "/var/log/supervisor.log",
    "status-server": "/var/log/status-server.log",
    "diag": "/var/log/diag.txt",
    "container": "/var/log/start.log",
    "start": "/var/log/start.log",
    "ttyd": "/var/log/ttyd.log",
}

EXPECTED_MODELS: dict[str, list[tuple[str, str, list[str]]]] = {
    "phase1": [
        ("insightface/models/buffalo_l", "det_10g.onnx", ["all", "general", "realistic", "anime", "minimal"]),
        ("insightface", "inswapper_128.onnx", ["all", "general", "realistic", "anime", "minimal"]),
        ("facerestore_models", "codeformer-v0.1.0.pth", ["all", "general", "realistic", "anime", "minimal"]),
        ("facerestore_models", "GFPGANv1.4.pth", ["all", "general", "realistic", "anime", "minimal"]),
        ("facedetection", "detection_Resnet50_Final.pth", ["all", "general", "realistic", "anime", "minimal"]),
        ("facedetection", "parsing_parsenet.pth", ["all", "general", "realistic", "anime", "minimal"]),
        ("ultralytics/bbox", "face_yolov8m.pt", ["all", "general", "realistic", "anime", "minimal"]),
        ("sams", "sam_vit_b_01ec64.pth", ["all", "general", "realistic", "anime", "minimal"]),
        ("vae", "sdxl.vae.safetensors", ["all", "general", "realistic", "anime", "minimal"]),
        ("vae", "vae-ft-mse-840000-ema-pruned.safetensors", ["all", "general", "realistic", "anime", "minimal"]),
        ("upscale_models", "4x-UltraSharp.pth", ["all", "general", "realistic", "anime", "minimal"]),
        ("upscale_models", "4x-AnimeSharp.pth", ["all", "general", "realistic", "anime", "minimal"]),
    ],
    "phase2": [
        ("checkpoints", "Juggernaut-XI-byRunDiffusion.safetensors", ["all", "general", "realistic"]),
        ("checkpoints", "RealVisXL_V5.0_fp16.safetensors", ["all", "realistic"]),
        ("checkpoints", "Realistic_Vision_V6.0_NV_B1_fp16.safetensors", ["all", "general", "realistic"]),
        ("checkpoints", "waiIllustriousSDXL_v160.safetensors", ["all", "general", "anime"]),
        ("checkpoints", "Illustrious-XL-v2.0.safetensors", ["all", "anime"]),
        ("checkpoints", "NoobAI-XL-v1.1.safetensors", ["all", "anime"]),
        ("controlnet", "controlnet-union-sdxl-promax.safetensors", ["all", "general", "realistic", "anime"]),
        ("ipadapter", "ip-adapter-plus_sdxl_vit-h.safetensors", ["all", "general", "realistic", "anime"]),
        ("ipadapter", "ip-adapter-plus-face_sdxl_vit-h.safetensors", ["all", "general", "realistic", "anime"]),
        ("ipadapter", "ip-adapter-plus_sd15.safetensors", ["all", "general", "realistic", "anime"]),
        ("ipadapter", "ip-adapter-plus-face_sd15.safetensors", ["all", "general", "realistic", "anime"]),
        ("clip_vision", "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors", ["all", "general", "realistic", "anime"]),
        ("clip_vision", "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors", ["all", "general", "realistic", "anime"]),
        ("checkpoints", "flux1-dev-fp8.safetensors", ["all"]),
    ],
}

_CACHE_LOCK = threading.Lock()
_API_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_HEAVY_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_START_MONOTONIC = time.monotonic()


def _load_triage_patterns() -> list[dict[str, Any]]:
    for candidate in ("/etc/comfyui/triage_patterns.json", "scripts/triage_patterns.json"):
        p = pathlib.Path(candidate)
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return []


TRIAGE_PATTERNS = _load_triage_patterns()


def redact_secrets(text: str) -> str:
    """Garantiza que ningún secreto (COMFY_PASS, HF_TOKEN) salga jamás en logs, JSON o informes (§8.3)."""
    if not text:
        return text
    for env_key in ("COMFY_PASS", "HF_TOKEN"):
        val = os.environ.get(env_key, "")
        if val and len(val) >= 4:
            text = text.replace(val, "[REDACTED]")
    return text


def run_cmd(args: list[str], timeout_s: int = 5) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, redact_secrets(out.strip())
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT ({timeout_s}s)"
    except Exception as exc:
        return 1, f"ERROR: {exc}"


def tail_lines(filepath: str, n: int = 200) -> list[str]:
    """Lee las últimas n líneas buscando por bloques de 8 KiB desde el final del fichero (O-04)."""
    p = pathlib.Path(filepath)
    if not p.is_file() or n <= 0:
        return []
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size == 0:
                return []
            block_size = 8192
            pos = file_size
            chunks: list[bytes] = []
            newlines_found = 0
            while pos > 0 and newlines_found <= n:
                read_len = min(block_size, pos)
                pos -= read_len
                fh.seek(pos, os.SEEK_SET)
                chunk = fh.read(read_len)
                chunks.append(chunk)
                newlines_found += chunk.count(b"\n")
            raw_bytes = b"".join(reversed(chunks))
        text = raw_bytes.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return [redact_secrets(line.rstrip("\r\n")) for line in lines[-n:]]
    except Exception:
        return []


def check_tcp_port(host: str, port: int, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def get_proc_rss_and_uptime(pid: int | None, started_at: int | None = None) -> tuple[int, int]:
    rss_mb = 0
    uptime_s = 0
    if started_at is not None and started_at > 0:
        uptime_s = max(0, int(time.time()) - int(started_at))
    if not pid or pid <= 0:
        return rss_mb, uptime_s
    status_path = pathlib.Path(f"/proc/{pid}/status")
    if status_path.is_file():
        try:
            for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        rss_mb = int(parts[1]) // 1024
                    break
            if started_at is None:
                stat_mtime = status_path.stat().st_mtime
                uptime_s = max(0, int(time.time() - stat_mtime))
        except Exception:
            pass
    return rss_mb, uptime_s


def get_container_uptime_s() -> int:
    started_file = pathlib.Path("/run/comfyui-started-at")
    if started_file.is_file():
        try:
            ts = int(started_file.read_text(encoding="utf-8").strip())
            return max(0, int(time.time()) - ts)
        except Exception:
            pass
    try:
        p1 = pathlib.Path("/proc/1")
        if p1.exists():
            return max(0, int(time.time() - p1.stat().st_mtime))
    except Exception:
        pass
    return int(time.monotonic() - _START_MONOTONIC)


def load_image_meta() -> dict[str, str]:
    default_meta = {"tag": "dev", "built_at": "unknown", "comfyui_sha": "unknown"}
    p = pathlib.Path("/etc/comfyui/image.json")
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {
                "tag": str(data.get("tag", "dev")),
                "built_at": str(data.get("built_at", "unknown")),
                "comfyui_sha": str(data.get("comfyui_sha", "unknown")),
            }
        except Exception:
            pass
    return default_meta


def get_gpu_status() -> dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {
            "available": False,
            "reason": "nvidia-smi no disponible (modo CPU)",
            "name": "CPU Mode",
            "driver": "N/A",
            "vram_total_mb": 0,
            "vram_used_mb": 0,
            "util_pct": 0,
            "temp_c": 0,
            "processes": [],
        }
    rc, out = run_cmd(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout_s=5,
    )
    if rc != 0 or not out:
        return {
            "available": False,
            "reason": out or "nvidia-smi sin respuesta",
            "name": "No detectada",
            "driver": "N/A",
            "vram_total_mb": 0,
            "vram_used_mb": 0,
            "util_pct": 0,
            "temp_c": 0,
            "processes": [],
        }
    first_line = out.splitlines()[0]
    parts = [p.strip() for p in first_line.split(",")]
    if len(parts) < 6:
        return {
            "available": False,
            "reason": f"Formato inesperado: {first_line}",
            "name": "Desconocida",
            "driver": "N/A",
            "vram_total_mb": 0,
            "vram_used_mb": 0,
            "util_pct": 0,
            "temp_c": 0,
            "processes": [],
        }

    def _to_int(val: str) -> int:
        try:
            return int(float(val))
        except Exception:
            return 0

    procs: list[dict[str, Any]] = []
    rc_p, out_p = run_cmd(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        timeout_s=5,
    )
    if rc_p == 0 and out_p:
        for line in out_p.splitlines():
            pp = [x.strip() for x in line.split(",")]
            if len(pp) >= 3 and pp[0].isdigit():
                procs.append(
                    {
                        "pid": int(pp[0]),
                        "name": os.path.basename(pp[1]),
                        "used_mb": _to_int(pp[2]),
                    }
                )

    return {
        "available": True,
        "name": parts[0],
        "driver": parts[1],
        "vram_total_mb": _to_int(parts[2]),
        "vram_used_mb": _to_int(parts[3]),
        "util_pct": _to_int(parts[4]),
        "temp_c": _to_int(parts[5]),
        "processes": procs,
    }


def get_memory_and_disk() -> tuple[dict[str, int], dict[str, Any]]:
    mem_total = 0
    mem_avail = 0
    swap_total = 0
    swap_free = 0
    meminfo = pathlib.Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) // 1024
                elif line.startswith("SwapTotal:"):
                    swap_total = int(line.split()[1]) // 1024
                elif line.startswith("SwapFree:"):
                    swap_free = int(line.split()[1]) // 1024
        except Exception:
            pass
    memory = {
        "total_mb": mem_total,
        "available_mb": mem_avail,
        "swap_used_mb": max(0, swap_total - swap_free),
    }

    disk_path = "/workspace" if pathlib.Path("/workspace").exists() else "/"
    try:
        usage = shutil.disk_usage(disk_path)
        disk = {
            "path": disk_path,
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
        }
    except Exception:
        disk = {"path": disk_path, "total_gb": 0, "used_gb": 0, "free_gb": 0}
    return memory, disk


def get_comfyui_status() -> dict[str, Any]:
    sup_path = pathlib.Path("/run/comfyui-supervisor.json")
    max_restarts = int(os.environ.get("COMFY_MAX_RESTARTS", "5"))
    sup_data: dict[str, Any] = {}
    if sup_path.is_file():
        try:
            sup_data = json.loads(sup_path.read_text(encoding="utf-8"))
        except Exception:
            sup_data = {}

    sup_state = str(sup_data.get("state", "starting"))
    pid = sup_data.get("pid")
    pid_int = int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None
    started_raw = sup_data.get("started_at")
    started_int = (
        int(started_raw)
        if isinstance(started_raw, int) or (isinstance(started_raw, str) and started_raw.isdigit())
        else None
    )
    restarts = int(sup_data.get("restarts", 0))
    healthy_resets = int(sup_data.get("healthy_resets", 0))
    last_exit = sup_data.get("last_exit")
    max_r = int(sup_data.get("max", max_restarts))

    port_open = check_tcp_port("127.0.0.1", 18188, timeout_s=0.3)
    rss_mb, uptime_s = get_proc_rss_and_uptime(pid_int, started_int)

    if port_open:
        state = "running"
    elif sup_state in ("down", "exhausted"):
        state = "down"
    else:
        state = "starting"

    return {
        "state": state,
        "supervisor_state": sup_state,
        "pid": pid_int,
        "rss_mb": rss_mb,
        "uptime_s": uptime_s,
        "port_open": port_open,
        "restarts": restarts,
        "healthy_resets": healthy_resets,
        "last_exit": last_exit,
        "max_restarts": max_r,
    }


def _find_hf_process() -> tuple[int | None, int]:
    rc, out = run_cmd(["ps", "-eo", "pid,rss,args"], timeout_s=3)
    if rc != 0 or not out:
        return None, 0
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            cmd = parts[2]
            if ("/hf download" in cmd or cmd.startswith("hf download")) and "grep" not in cmd:
                return int(parts[0]), int(parts[1]) // 1024
    return None, 0


def get_phases_status() -> dict[str, Any]:
    profile = os.environ.get("MODEL_PROFILE", "all")
    if profile not in ("all", "general", "realistic", "anime", "minimal"):
        profile = "all"

    expected_p2 = len([m for m in EXPECTED_MODELS["phase2"] if profile in m[2]])

    if os.environ.get("SKIP_MODEL_DOWNLOAD", "0") == "1":
        return {
            "phase1": {"state": "skipped", "done": 0, "total": 12, "failed": []},
            "phase2": {
                "state": "skipped",
                "done": 0,
                "total": expected_p2,
                "failed": [],
                "current_file": None,
                "hf_pid": None,
                "hf_rss_mb": 0,
            },
        }

    lines = tail_lines("/var/log/download-models.log", 2000)
    p1_state = "pending"
    p2_state = "pending"
    p1_done: set[str] = set()
    p2_done: set[str] = set()
    p1_failed: list[str] = []
    p2_failed: list[str] = []
    p2_total = expected_p2
    current_phase = 0
    current_file: str | None = None

    p1_names = {m[1] for m in EXPECTED_MODELS["phase1"]}

    for line in lines:
        if "INICIANDO FASE 1" in line:
            current_phase = 1
            if p1_state == "pending":
                p1_state = "running"
        elif "INICIANDO FASE 2" in line:
            current_phase = 2
            p2_state = "running"
        elif "FASE 1 COMPLETADA" in line:
            p1_state = "done"
            current_file = None
        elif "FASE 2 COMPLETADA" in line:
            p2_state = "done"
            current_file = None
        elif "Fase 1 terminó con advertencias o timeout" in line:
            if p1_state != "done":
                p1_state = "timeout"

        m_plan = re.search(r"PLAN FASE 2:\s*(\d+)\s*archivos", line)
        if m_plan:
            p2_total = int(m_plan.group(1))

        m_start = re.search(r"INICIO:\s*([^\s(]+)", line)
        if m_start:
            current_file = m_start.group(1)

        m_ok = re.search(
            r"\[MODELS\]\s+(?:OK:\s+([^\s]+)|SKIP \(ya existe\):\s+([^\s]+))",
            line,
        )
        if m_ok:
            raw_name = os.path.basename(m_ok.group(1) or m_ok.group(2) or "")
            if raw_name in ("InsightFace", "buffalo_l.zip", "buffalo_l"):
                fname = "det_10g.onnx"
            else:
                fname = raw_name
            if fname in p1_names or current_phase == 1:
                p1_done.add(fname)
            else:
                p2_done.add(fname)
            if current_file in (fname, raw_name):
                current_file = None

        m_err = re.search(r"ERROR:\s*Falló la descarga de\s*([^\s]+)", line)
        if m_err:
            fname = m_err.group(1)
            if fname in p1_names or current_phase == 1:
                if fname not in p1_failed:
                    p1_failed.append(fname)
            else:
                if fname not in p2_failed:
                    p2_failed.append(fname)

    hf_pid, hf_rss_mb = _find_hf_process()
    if p2_state == "done":
        current_file = None

    return {
        "phase1": {
            "state": p1_state,
            "done": len(p1_done),
            "total": 12,
            "failed": p1_failed,
        },
        "phase2": {
            "state": p2_state,
            "done": len(p2_done),
            "total": p2_total,
            "failed": p2_failed,
            "current_file": current_file,
            "hf_pid": hf_pid,
            "hf_rss_mb": hf_rss_mb,
        },
    }


def get_services_status() -> dict[str, Any]:
    rc_ng, out_ng = run_cmd(["ps", "-o", "user=", "-C", "nginx"], timeout_s=3)
    users = [u.strip() for u in out_ng.splitlines() if u.strip()] if rc_ng == 0 else []
    nginx_running = len(users) > 0
    workers_user = "www-data" if "www-data" in users else (users[0] if users else "none")

    term_enabled = os.environ.get("ENABLE_WEB_TERMINAL", "0") == "1"
    term_running = check_tcp_port("127.0.0.1", 7681, timeout_s=0.2) if term_enabled else False

    return {
        "nginx": {"running": nginx_running, "workers_user": workers_user},
        "status_server": {"running": True},
        "terminal": {"enabled": term_enabled, "running": term_running},
    }


def get_masked_env() -> dict[str, str]:
    if os.environ.get("COMFY_PASS_AUTOGENERATED", "0") == "1":
        pass_mask = "<autogenerada — ver logs del contenedor>"
    elif os.environ.get("COMFY_PASS"):
        pass_mask = "<definida por el usuario>"
    else:
        pass_mask = "<no definida>"

    hf_mask = "<definido>" if os.environ.get("HF_TOKEN") else "<no definido>"

    return {
        "MODEL_PROFILE": os.environ.get("MODEL_PROFILE", "all"),
        "COMFY_USER": os.environ.get("COMFY_USER", "admin"),
        "COMFY_PASS": pass_mask,
        "HF_TOKEN": hf_mask,
        "COMFYUI_EXTRA_ARGS": os.environ.get("COMFYUI_EXTRA_ARGS", ""),
        "HF_HUB_DISABLE_XET": os.environ.get("HF_HUB_DISABLE_XET", "<no definido>"),
        "ENABLE_WEB_TERMINAL": os.environ.get("ENABLE_WEB_TERMINAL", "0"),
    }


def evaluate_triage_and_errors(
    disk: dict[str, Any],
    phases: dict[str, Any],
    comfyui: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    log_Buffers: dict[str, list[str]] = {
        "comfyui": tail_lines(LOG_WHITELIST["comfyui"], 400),
        "nginx-error": tail_lines(LOG_WHITELIST["nginx-error"], 200),
        "nginx-access": tail_lines(LOG_WHITELIST["nginx-access"], 400),
        "download": tail_lines(LOG_WHITELIST["download"], 300),
        "supervisor": tail_lines(LOG_WHITELIST["supervisor"], 200),
        "diag": tail_lines(LOG_WHITELIST["diag"], 100),
        "container": tail_lines(LOG_WHITELIST["container"], 200),
    }

    # Check if hf process has been running with >30m stale download log
    proc_lines: list[str] = []
    if phases["phase2"].get("hf_pid"):
        dl_path = pathlib.Path(LOG_WHITELIST["download"])
        if dl_path.is_file():
            age_s = time.time() - dl_path.stat().st_mtime
            if age_s > 1800:
                proc_lines.append(f"hf_stuck_over_30m: pid={phases['phase2']['hf_pid']} sin avance hace {int(age_s)}s")
    log_Buffers["processes"] = proc_lines

    api_lines: list[str] = []
    if disk.get("total_gb", 0) > 0 and disk.get("free_gb", 999) < 10:
        api_lines.append(f"disk.free_gb < 10 (libre actual: {disk.get('free_gb')} GB en {disk.get('path')})")
    log_Buffers["api"] = api_lines

    triage_hits: list[dict[str, Any]] = []
    errors_recent: list[dict[str, str]] = []
    seen_errors: set[str] = set()

    sev_rank = {"alta": 0, "media": 1, "baja": 2}
    comfy_running = bool(comfyui and comfyui.get("state") == "running")

    for item in TRIAGE_PATTERNS:
        pat_id = item.get("id", "unknown")
        # O-08a: Suprimir nginx-502 cuando ComfyUI ya está running (evita falso positivo por los 503 de _starting.html)
        if pat_id == "nginx-502" and comfy_running:
            continue
        regex_str = item.get("pattern", "")
        if not regex_str:
            continue
        try:
            rx = re.compile(regex_str, re.IGNORECASE)
        except re.error:
            continue

        matched_line = None
        matched_src = None
        for src in item.get("sources", []):
            for line in reversed(log_Buffers.get(src, [])):
                if rx.search(line):
                    matched_line = line.strip()
                    matched_src = src
                    break
            if matched_line:
                break

        if matched_line and matched_src:
            triage_hits.append(
                {
                    "severity": item.get("severity", "baja"),
                    "pattern_id": pat_id,
                    "source": matched_src,
                    "evidence": matched_line[:220],
                    "summary": item.get("cause", ""),
                    "first_command": item.get("first_command", ""),
                }
            )

    triage_hits.sort(key=lambda x: sev_rank.get(x["severity"], 3))
    triage_hits = triage_hits[:10]

    # O-08c: Sensible a mayúsculas con límites de palabra para evitar falsos positivos con "error" en minúsculas
    err_rx = re.compile(
        r"(\bERROR\b|\bCRITICAL\b|\bFATAL\b|OutOfMemoryError|Traceback|IMPORT FAILED|Exception:|rc=13[479]|\[DIAG\]\[FAIL\])"
    )
    for src in ("comfyui", "download", "nginx-error", "supervisor"):
        for line in reversed(log_Buffers.get(src, [])):
            if err_rx.search(line):
                clean = line.strip()
                key = f"{src}:{clean[:120]}"
                if key not in seen_errors:
                    seen_errors.add(key)
                    ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})", clean)
                    errors_recent.append(
                        {
                            "ts": ts_match.group(1) if ts_match else "reciente",
                            "source": src,
                            "line": clean[:300],
                        }
                    )
                if len(errors_recent) >= 20:
                    break
        if len(errors_recent) >= 20:
            break

    return triage_hits, errors_recent


def format_phase_banner(comfy: dict[str, Any], phases: dict[str, Any]) -> str:
    p1 = phases["phase1"]
    p2 = phases["phase2"]
    c_state = comfy["state"]
    restarts = comfy["restarts"]
    max_r = comfy["max_restarts"]

    if c_state == "down":
        if comfy.get("supervisor_state") == "exhausted":
            return f"ComfyUI caído (límite de {max_r} reinicios alcanzado)"
        return f"ComfyUI caído (reinicio {restarts}/{max_r})"

    if p1["state"] == "running":
        return f"Fase 1: descargando ({p1['done']}/{p1['total']})"

    if c_state == "starting":
        return "Arrancando ComfyUI..."

    if p2["state"] == "skipped":
        return "ComfyUI listo · Descargas omitidas (SKIP_MODEL_DOWNLOAD=1)"
    if p2["state"] == "running":
        cur = f" ({p2['current_file']})" if p2.get("current_file") else ""
        return f"ComfyUI listo · Fase 2: {p2['done']}/{p2['total']} modelos{cur}"
    if p2["state"] == "done":
        return "ComfyUI listo · Fase 2 completada"
    return "ComfyUI listo"


def build_status_api() -> dict[str, Any]:
    now = time.monotonic()
    with _CACHE_LOCK:
        if _API_CACHE["data"] is not None and (now - _API_CACHE["ts"]) < 3.0:
            return _API_CACHE["data"]

    image_meta = load_image_meta()
    gpu = get_gpu_status()
    memory, disk = get_memory_and_disk()
    comfyui = get_comfyui_status()
    phases = get_phases_status()
    services = get_services_status()
    triage, errors_recent = evaluate_triage_and_errors(disk, phases, comfyui)
    env_masked = get_masked_env()

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "image": image_meta,
        "instance": {
            "hostname": socket.gethostname(),
            "uptime_s": get_container_uptime_s(),
            "vast_instance_id": os.environ.get("VAST_CONTAINERLABEL", "local"),
            "external_port_8188": os.environ.get("VAST_TCP_PORT_8188", "8188"),
        },
        "gpu": gpu,
        "memory": memory,
        "disk": disk,
        "comfyui": comfyui,
        "phases": phases,
        "phase_banner": format_phase_banner(comfyui, phases),
        "services": services,
        "errors_recent": errors_recent,
        "triage": triage,
        "env_masked": env_masked,
    }

    with _CACHE_LOCK:
        _API_CACHE["ts"] = time.monotonic()
        _API_CACHE["data"] = payload
    return payload


def _compute_heavy_sections_now() -> dict[str, Any]:
    """Calcula deriva pip/git e inventario de modelos (ejecutado por el hilo de fondo cada 60s, O-03)."""
    lock_path = pathlib.Path("/etc/comfyui/requirements.lock")
    lock_lines = (
        set(lock_path.read_text(encoding="utf-8", errors="replace").splitlines())
        if lock_path.is_file()
        else set()
    )
    rc_pip, out_pip = run_cmd(["pip3", "freeze"], timeout_s=20)
    curr_lines = set(out_pip.splitlines()) if rc_pip == 0 else set()

    added = sorted(curr_lines - lock_lines) if lock_lines else []
    removed = sorted(lock_lines - curr_lines) if lock_lines else []

    nodes_lock: dict[str, str] = {}
    nlock_path = pathlib.Path("/etc/comfyui/nodes.lock")
    if nlock_path.is_file():
        for line in nlock_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "node":
                nodes_lock[parts[1]] = parts[3]

    custom_nodes_dir = pathlib.Path(os.environ.get("COMFY_APP_DIR", "/opt/ComfyUI")) / "custom_nodes"
    node_drift: list[str] = []
    if custom_nodes_dir.is_dir():
        for child in sorted(custom_nodes_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                continue
            rc_sha, sha_out = run_cmd(["git", "-C", str(child), "rev-parse", "HEAD"], timeout_s=3)
            curr_sha = sha_out.strip() if rc_sha == 0 else "no-git"
            rc_st, st_out = run_cmd(["git", "-C", str(child), "status", "--porcelain"], timeout_s=3)
            dirty = " [DIRTY]" if (rc_st == 0 and st_out.strip()) else ""
            expected_sha = nodes_lock.get(child.name)
            if expected_sha is None:
                node_drift.append(f"- `{child.name}`: `{curr_sha[:12]}` (EXTRA — instalado fuera de nodes.lock){dirty}")
            elif curr_sha != expected_sha:
                node_drift.append(
                    f"- `{child.name}`: `{curr_sha[:12]}` (CAMBIADO — esperado `{expected_sha[:12]}`){dirty}"
                )
            elif dirty:
                node_drift.append(f"- `{child.name}`: `{curr_sha[:12]}`{dirty}")

    models_root = pathlib.Path(os.environ.get("COMFY_DATA_DIR", "/workspace/comfyui-data")) / "models"
    folder_stats: list[str] = []
    if models_root.is_dir():
        for sub in sorted(models_root.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            files = [f for f in sub.rglob("*") if f.is_file() and not f.name.startswith(".")]
            total_b = sum(f.stat().st_size for f in files)
            folder_stats.append(f"- `{sub.name}/`: {len(files)} archivo(s) ({total_b / (1024**3):.2f} GiB)")

    profile = os.environ.get("MODEL_PROFILE", "all")
    if profile not in ("all", "general", "realistic", "anime", "minimal"):
        profile = "all"
    missing_models: list[str] = []
    for phase_key in ("phase1", "phase2"):
        for subdir, fname, profiles in EXPECTED_MODELS[phase_key]:
            if profile in profiles:
                fpath = models_root / subdir / fname
                if not fpath.is_file() or fpath.stat().st_size == 0:
                    missing_models.append(f"- `{subdir}/{fname}` ({phase_key})")

    result = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pip_added": added,
        "pip_removed": removed,
        "node_drift": node_drift,
        "folder_stats": folder_stats,
        "missing_models": missing_models,
    }
    with _CACHE_LOCK:
        _HEAVY_CACHE["ts"] = time.monotonic()
        _HEAVY_CACHE["data"] = result
    return result


def _compute_heavy_sections() -> dict[str, Any]:
    with _CACHE_LOCK:
        cached = _HEAVY_CACHE["data"]
    if cached is not None:
        return cached
    return _compute_heavy_sections_now()


def _heavy_worker_loop() -> None:
    while True:
        try:
            _compute_heavy_sections_now()
        except Exception:
            pass
        time.sleep(60.0)


def _extract_tracebacks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    seen: set[str] = set()
    current: list[str] = []
    in_tb = False
    for line in lines:
        if "Traceback (most recent call last):" in line:
            if in_tb and current:
                tb_text = "\n".join(current)
                sig = current[-1] if current else tb_text
                if sig not in seen:
                    seen.add(sig)
                    blocks.append(tb_text)
            current = [line]
            in_tb = True
        elif in_tb:
            current.append(line)
            if re.match(r"^[A-Za-z0-9_.]*(Error|Exception|Interrupt):", line.strip()) or len(current) > 45:
                tb_text = "\n".join(current)
                sig = current[-1]
                if sig not in seen:
                    seen.add(sig)
                    blocks.append(tb_text)
                in_tb = False
                current = []
    if in_tb and current:
        tb_text = "\n".join(current)
        sig = current[-1]
        if sig not in seen:
            blocks.append(tb_text)
    return blocks[-5:]


def _format_iso_utc(epoch_val: Any) -> str:
    try:
        ts = int(epoch_val)
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(epoch_val)


def build_report_markdown() -> str:
    api = build_status_api()
    heavy = _compute_heavy_sections()

    up_s = api["instance"]["uptime_s"]
    up_hms = f"{up_s // 3600:02d}:{(up_s % 3600) // 60:02d}:{up_s % 60:02d}"
    gpu = api["gpu"]
    mem = api["memory"]
    disk = api["disk"]
    comfy = api["comfyui"]
    phases = api["phases"]
    env_m = api["env_masked"]

    diag_lines = tail_lines(LOG_WHITELIST["diag"], 100)
    torch_ver = "2.9.1+cu128"
    for dl in diag_lines:
        m_tv = re.search(r"torch=([^\s]+)", dl)
        if m_tv:
            torch_ver = m_tv.group(1)

    out: list[str] = []
    out.append("# ComfyUI Everything-Ready · Informe de diagnóstico")
    out.append(
        f"Generado: `{api['generated_at']}` · Imagen: `{api['image']['tag']}` · "
        f"ComfyUI: `{api['image']['comfyui_sha'][:12]}` · Uptime: `{up_hms}`"
    )
    out.append(
        f"GPU: `{gpu['name']}` (`{gpu['vram_total_mb']} MB`) · Driver `{gpu['driver']}` · "
        f"torch `{torch_ver}` · `cuda.is_available={gpu['available']}`"
    )
    out.append(
        f"RAM: `{mem['total_mb']} MB` total / `{mem['available_mb']} MB` libre · "
        f"Disco `{disk['path']}`: `{disk['total_gb']} GB` total / `{disk['free_gb']} GB` libre · "
        f"Perfil: `{env_m['MODEL_PROFILE']}`"
    )
    out.append("")

    # 0. Triage automático
    out.append("## 0. Triage automático")
    if api["triage"]:
        out.append("| Sev. | Patrón | Evidencia (línea y origen) | Causa probable | Primer comando sugerido |")
        out.append("|---|---|---|---|---|")
        for t in api["triage"]:
            ev = t["evidence"].replace("|", "\\|")
            cs = t["summary"].replace("|", "\\|")
            fc = t["first_command"].replace("|", "\\|")
            out.append(f"| **{t['severity']}** | `{t['pattern_id']}` | `[{t['source']}]` {ev} | {cs} | `{fc}` |")
    else:
        out.append("Sin coincidencias de patrones de fallo en los logs actuales.")
    out.append("")

    # 1. Contexto fijo para el agente
    out.append("## 1. Contexto fijo para el agente")
    out.append(
        "- **Rutas**: app `/opt/ComfyUI` · datos `/workspace/comfyui-data` · configs `/etc/comfyui` · "
        "logs `/var/log/{comfyui,start,supervisor,download-models,status-server,diag}.log` y `/var/log/nginx/{access,error}.log`"
    )
    out.append(
        f"- **Procesos**: `tini(1)` → `/start.sh` → `{{nginx, status-server, ttyd?, download-models.sh --phase2, "
        f"python3 main.py (supervisado, máx {comfy['max_restarts']} reinicios)}}`"
    )
    out.append(
        "- **Puertos**: `8188` público (Nginx Basic Auth) → `18188` ComfyUI · `18189` status-server · `7681` ttyd "
        "(todos en `127.0.0.1` salvo `8188`)"
    )
    out.append(
        "- **Cómo ejecutar comandos**: terminal web en `/terminal/` (si `ENABLE_WEB_TERMINAL=1`) · "
        "SSH si la instancia se creó con SSH · `vastai execute <id> 'ls|du|rm …'`"
    )
    out.append(
        "- **Cómo reiniciar ComfyUI sin reiniciar la instancia**: `pkill -f 'main.py'` "
        "(envía `SIGTERM` / `rc=143`; el supervisor de `/start.sh` lo relanza en 5 s **sin consumir** el presupuesto `COMFY_MAX_RESTARTS`, "
        "y el contador `restarts` se resetea automáticamente a `0` tras `COMFY_HEALTHY_UPTIME=600s` de ejecución estable)"
    )
    out.append(
        "- **Cómo reinstalar la terna PyTorch CUDA 12.8**: "
        "`pip3 install --force-reinstall torch==2.9.1+cu128 torchvision==0.24.1+cu128 torchaudio==2.9.1+cu128 "
        "--index-url https://download.pytorch.org/whl/cu128`"
    )
    out.append(
        "- **Restricciones**: `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` activo · "
        "no usar 'Update ComfyUI' desde Manager · ningún secreto se incluye en este informe"
    )
    out.append("")

    # 2. Diagnóstico de arranque ([DIAG])
    out.append("## 2. Diagnóstico de arranque ([DIAG])")
    if diag_lines:
        out.append("```text")
        out.extend(diag_lines)
        out.append("```")
    else:
        out.append("sin datos (diagnóstico aún no ejecutado)")
    out.append("")

    # 3. ComfyUI y supervisor
    out.append("## 3. ComfyUI y supervisor")
    out.append(
        f"- **Estado**: `{comfy['state']}` (supervisor: `{comfy['supervisor_state']}`) · "
        f"**PID**: `{comfy['pid']}` · **RSS**: `{comfy['rss_mb']} MB` · "
        f"**Uptime proceso**: `{comfy['uptime_s']}s` · **Reinicios**: `{comfy['restarts']}/{comfy['max_restarts']}` · "
        f"**Resets por uptime sano**: `{comfy.get('healthy_resets', 0)}`"
    )
    if comfy.get("last_exit"):
        le = comfy["last_exit"]
        at_iso = _format_iso_utc(le.get("at"))
        out.append(
            f"- **Última caída registrada**: `rc={le.get('rc')}` a las `{at_iso}` — {le.get('reason')}"
        )
    sup_lines = tail_lines(LOG_WHITELIST["supervisor"], 140)
    if sup_lines:
        out.append("```text")
        out.extend(sup_lines)
        out.append("```")
    else:
        out.append("Sin caídas registradas en `/var/log/supervisor.log`.")
    out.append("")

    # 4. GPU, memoria y disco
    out.append("## 4. GPU, memoria y disco")
    out.append(
        f"- **GPU**: `{gpu['name']}` · VRAM usada `{gpu['vram_used_mb']} / {gpu['vram_total_mb']} MB` · "
        f"Utilización `{gpu['util_pct']}%` · Temp `{gpu['temp_c']}°C`"
    )
    if gpu.get("processes"):
        for pr in gpu["processes"]:
            out.append(f"  - Proceso GPU PID `{pr['pid']}` (`{pr['name']}`): `{pr['used_mb']} MB`")
    out.append(
        f"- **RAM**: total `{mem['total_mb']} MB` · disponible `{mem['available_mb']} MB` · swap `{mem['swap_used_mb']} MB`"
    )
    out.append(
        f"- **Disco (`{disk['path']}`)**: total `{disk['total_gb']} GB` · usado `{disk['used_gb']} GB` · libre `{disk['free_gb']} GB`"
    )
    out.append("")

    # 5. Errores de ComfyUI
    out.append("## 5. Errores de ComfyUI")
    comfy_500 = tail_lines(LOG_WHITELIST["comfyui"], 500)
    tbs = _extract_tracebacks(comfy_500)
    if tbs:
        out.append("### Tracebacks deduplicados (últimas 500 líneas)")
        for idx, tb in enumerate(tbs, 1):
            out.append(f"#### Traceback #{idx}")
            out.append("```text")
            out.append(tb)
            out.append("```")
    else:
        out.append("Sin bloques `Traceback` en las últimas 500 líneas de ComfyUI.")

    comfy_150 = comfy_500[-150:]
    if comfy_150:
        out.append("### Últimas líneas de `/var/log/comfyui.log`")
        out.append("```text")
        out.extend(comfy_150)
        out.append("```")
    else:
        out.append("sin datos en `/var/log/comfyui.log`")
    out.append("")

    # 6. Descargas
    out.append("## 6. Descargas")
    out.append(
        f"- **Fase 1**: `{phases['phase1']['state']}` (`{phases['phase1']['done']}/{phases['phase1']['total']}`) · "
        f"Fallidos: `{phases['phase1']['failed']}`"
    )
    out.append(
        f"- **Fase 2**: `{phases['phase2']['state']}` (`{phases['phase2']['done']}/{phases['phase2']['total']}`) · "
        f"Archivo actual: `{phases['phase2']['current_file']}` · "
        f"PID `hf`: `{phases['phase2']['hf_pid']}` (`{phases['phase2']['hf_rss_mb']} MB` RSS) · "
        f"Fallidos: `{phases['phase2']['failed']}`"
    )
    dl_30 = tail_lines(LOG_WHITELIST["download"], 30)
    if dl_30:
        out.append("```text")
        out.extend(dl_30)
        out.append("```")
    else:
        out.append("sin datos en `/var/log/download-models.log`")
    out.append("")

    # 7. Nginx
    out.append("## 7. Nginx")
    acc_500 = tail_lines(LOG_WHITELIST["nginx-access"], 500)
    counts = {"401": 0, "413": 0, "429": 0, "502": 0, "503": 0}
    for line in acc_500:
        for code in counts:
            if f' {code} ' in line:
                counts[code] += 1
    out.append(
        f"- **Recuento de códigos en últimas 500 peticiones**: "
        f"`401={counts['401']}` · `413={counts['413']}` · `429={counts['429']}` · "
        f"`502={counts['502']}` · `503={counts['503']}`"
    )
    ng_err = tail_lines(LOG_WHITELIST["nginx-error"], 50)
    if ng_err:
        out.append("```text")
        out.extend(ng_err)
        out.append("```")
    else:
        out.append("Sin errores en `/var/log/nginx/error.log`.")
    out.append("")

    # 8. Drift del entorno
    out.append("## 8. Drift del entorno")
    out.append("### Diferencias `pip freeze` vs `/etc/comfyui/requirements.lock`")
    if not heavy["pip_added"] and not heavy["pip_removed"]:
        out.append("- Sin cambios respecto a `/etc/comfyui/requirements.lock`.")
    else:
        for p in heavy["pip_added"][:40]:
            out.append(f"- **+** `{p}`")
        for p in heavy["pip_removed"][:40]:
            out.append(f"- **-** `{p}`")

    out.append("### Estado de `custom_nodes/` vs `/etc/comfyui/nodes.lock`")
    if not heavy["node_drift"]:
        out.append("- Los 10 custom nodes coinciden exactamente con los SHAs de `/etc/comfyui/nodes.lock`.")
    else:
        out.extend(heavy["node_drift"])

    out.append("### Variables de entorno relevantes (enmascaradas)")
    for k, v in env_m.items():
        out.append(f"- `{k}` = `{v}`")
    out.append("")

    # 9. Modelos
    out.append("## 9. Modelos")
    if heavy["folder_stats"]:
        out.extend(heavy["folder_stats"])
    else:
        out.append("- Sin datos de carpetas de modelos.")

    out.append(f"### Modelos esperados para el perfil `{env_m['MODEL_PROFILE']}` aún ausentes")
    if not heavy["missing_models"]:
        out.append("- Todos los modelos del perfil están presentes en disco.")
    else:
        out.extend(heavy["missing_models"])

    report_str = redact_secrets("\n".join(out) + "\n")
    if len(report_str.encode("utf-8")) > 400_000:
        report_str = report_str[:395_000] + "\n\n...[Informe truncado al límite de 400 KB]...\n"
    return report_str


STATUS_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Estado del Sistema · ComfyUI Vast.ai</title>
  <style>
    /* Pares de contraste verificados WCAG AA (>= 4.5:1):
       Oscuro: #f1f5f9 sobre #0f172a (15.4:1), #cbd5e1 sobre #1e293b (9.2:1)
       Claro : #0f172a sobre #f8fafc (16.1:1), #334155 sobre #ffffff (10.3:1) */
    :root {
      --bg: #f8fafc;
      --surface: #ffffff;
      --border: #cbd5e1;
      --text: #0f172a;
      --muted: #334155;
      --accent: #1d4ed8;
      --ok-bg: #dcfce7; --ok-fg: #14532d;
      --warn-bg: #fef3c7; --warn-fg: #78350f;
      --err-bg: #fee2e2; --err-fg: #7f1d1d;
      --code-bg: #f1f5f9;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f172a;
        --surface: #1e293b;
        --border: #334155;
        --text: #f1f5f9;
        --muted: #cbd5e1;
        --accent: #60a5fa;
        --ok-bg: #064e3b; --ok-fg: #dcfce7;
        --warn-bg: #78350f; --warn-fg: #fef3c7;
        --err-bg: #7f1d1d; --err-fg: #fee2e2;
        --code-bg: #090d16;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 20px;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      font-size: 16px; line-height: 1.5;
      background: var(--bg); color: var(--text);
    }
    .container { max-width: 1240px; margin: 0 auto; }
    header {
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 12px; padding: 16px 20px; background: var(--surface);
      border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px;
    }
    h1 { font-size: 20px; margin: 0; }
    h2 { font-size: 18px; margin: 0 0 12px 0; }
    .btn {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 8px 14px; border-radius: 6px; border: 1px solid var(--border);
      background: var(--surface); color: var(--text); font-size: 14px; font-weight: 600;
      cursor: pointer; text-decoration: none;
    }
    .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .grid {
      display: grid; grid-template-columns: 1fr; gap: 16px; margin-bottom: 20px;
    }
    @media (min-width: 760px) { .grid-2 { grid-template-columns: repeat(2, 1fr); } }
    @media (min-width: 1080px) { .grid-3 { grid-template-columns: repeat(3, 1fr); } }
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 10px; padding: 16px;
    }
    .badge {
      display: inline-block; padding: 2px 10px; border-radius: 999px;
      font-size: 13px; font-weight: 700;
    }
    .b-ok { background: var(--ok-bg); color: var(--ok-fg); }
    .b-warn { background: var(--warn-bg); color: var(--warn-fg); }
    .b-err { background: var(--err-bg); color: var(--err-fg); }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
    pre {
      background: var(--code-bg); padding: 12px; border-radius: 8px;
      border: 1px solid var(--border); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px; white-space: pre-wrap; word-break: break-word;
      max-height: 440px; overflow-y: auto; margin: 8px 0 0 0;
    }
    .err-line { background: var(--err-bg); color: var(--err-fg); display: block; padding: 1px 4px; border-radius: 3px; }
    .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
    .tab-active { border-color: var(--accent); color: var(--accent); }
    .progress-bar { width: 100%; height: 12px; background: var(--code-bg); border-radius: 6px; overflow: hidden; margin-top: 8px; border: 1px solid var(--border); }
    .progress-fill { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s ease; }
    textarea.fallback { width: 100%; height: 160px; margin-top: 10px; font-family: monospace; font-size: 13px; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Estado del Sistema · ComfyUI Vast.ai</h1>
        <div id="hdr-sub" style="font-size:14px;color:var(--muted);">Cargando estado en vivo...</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <a class="btn" href="/home">← Portal /home</a>
        <a class="btn" href="/report.md">Informe (/report.md)</a>
        <button class="btn" id="pause-btn" type="button">Pausar actualización</button>
      </div>
    </header>

    <div class="card" id="triage-card" style="margin-bottom:16px;">
      <h2>Triage automático</h2>
      <div id="triage-body">Sin alertas detectadas.</div>
    </div>

    <div class="grid grid-3">
      <div class="card">
        <h2>ComfyUI y Supervisor</h2>
        <div id="comfy-body">Cargando...</div>
      </div>
      <div class="card">
        <h2>GPU</h2>
        <div id="gpu-body">Cargando...</div>
      </div>
      <div class="card">
        <h2>Memoria y Disco</h2>
        <div id="mem-body">Cargando...</div>
      </div>
    </div>

    <div class="grid grid-2">
      <div class="card">
        <h2>Descargas de Modelos</h2>
        <div id="dl-body">Cargando...</div>
        <div class="progress-bar"><div class="progress-fill" id="dl-bar"></div></div>
      </div>
      <div class="card">
        <h2>Servicios y Entorno (Enmascarado)</h2>
        <div id="svc-body">Cargando...</div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <h2>Errores Recientes</h2>
      <div id="err-body">Sin errores recientes.</div>
    </div>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
        <h2>Visor de Logs (Solo lectura)</h2>
        <div style="display:flex;gap:8px;">
          <button class="btn" id="more-lines-btn" type="button">Más líneas (hasta 2000)</button>
          <button class="btn" id="copy-log-btn" type="button">Copiar log</button>
        </div>
      </div>
      <div class="tabs" id="log-tabs">
        <button class="btn tab-active" data-log="comfyui" type="button">ComfyUI</button>
        <button class="btn" data-log="nginx-error" type="button">Nginx Error</button>
        <button class="btn" data-log="download" type="button">Descargas</button>
        <button class="btn" data-log="supervisor" type="button">Supervisor</button>
        <button class="btn" data-log="diag" type="button">Diagnóstico</button>
        <button class="btn" data-log="container" type="button">Contenedor</button>
      </div>
      <pre id="log-view">Cargando log...</pre>
      <div id="copy-fallback" style="display:none;">
        <textarea class="fallback" id="copy-textarea" readonly></textarea>
      </div>
    </div>
  </div>

  <script>
    let paused = false;
    let currentLog = "comfyui";
    let currentLines = 200;
    let lastRawLog = "";

    function esc(s) {
      return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    async function refreshStatus() {
      if (paused) return;
      try {
        const res = await fetch("/status/api", { cache: "no-store" });
        if (!res.ok) return;
        const d = await res.json();
        document.getElementById("hdr-sub").textContent =
          `${d.phase_banner} · Imagen: ${d.image.tag} · Uptime: ${Math.floor(d.instance.uptime_s / 60)} min`;

        if (d.triage && d.triage.length > 0) {
          let rows = d.triage.map(t =>
            `<tr><td><span class="badge ${t.severity === 'alta' ? 'b-err' : 'b-warn'}">${esc(t.severity)}</span></td>` +
            `<td><code>${esc(t.pattern_id)}</code></td><td>${esc(t.summary)}</td>` +
            `<td><code>${esc(t.first_command)}</code></td></tr>`
          ).join("");
          document.getElementById("triage-body").innerHTML =
            `<table><thead><tr><th>Sev.</th><th>Patrón</th><th>Causa probable</th><th>Acción sugerida</th></tr></thead><tbody>${rows}</tbody></table>`;
        } else {
          document.getElementById("triage-body").textContent = "Sin alertas de triage detectadas.";
        }

        const c = d.comfyui;
        const cBadge = c.state === "running" ? "b-ok" : (c.state === "starting" ? "b-warn" : "b-err");
        const cLabel = c.state === "running" ? "Activo" : (c.state === "starting" ? "Arrancando" : "Caído");
        const lastEx = c.last_exit ? `rc=${esc(c.last_exit.rc)} (${esc(c.last_exit.reason)})` : "ninguna";
        document.getElementById("comfy-body").innerHTML =
          `<p><span class="badge ${cBadge}">${cLabel}</span> · PID: <code>${esc(c.pid ?? "-")}</code> · RSS: <code>${c.rss_mb} MB</code></p>` +
          `<p>Reinicios: <code>${c.restarts}/${c.max_restarts}</code> · Última caída: ${lastEx}</p>`;

        const g = d.gpu;
        document.getElementById("gpu-body").innerHTML = g.available
          ? `<p><span class="badge b-ok">Activa</span> <strong>${esc(g.name)}</strong> (Driver ${esc(g.driver)})</p>` +
            `<p>VRAM: <code>${g.vram_used_mb} / ${g.vram_total_mb} MB</code> · Uso: <code>${g.util_pct}%</code> · Temp: <code>${g.temp_c}°C</code></p>`
          : `<p><span class="badge b-warn">Modo CPU</span> ${esc(g.reason || "Sin GPU")}</p>`;

        document.getElementById("mem-body").innerHTML =
          `<p>RAM: <code>${d.memory.available_mb} MB libres / ${d.memory.total_mb} MB total</code> (Swap: <code>${d.memory.swap_used_mb} MB</code>)</p>` +
          `<p>Disco (${esc(d.disk.path)}): <code>${d.disk.free_gb} GB libres / ${d.disk.total_gb} GB total</code></p>`;

        const p1 = d.phases.phase1, p2 = d.phases.phase2;
        const pct = p2.total > 0 ? Math.min(100, Math.round((p2.done / p2.total) * 100)) : 100;
        document.getElementById("dl-bar").style.width = `${pct}%`;
        document.getElementById("dl-body").innerHTML =
          `<p>Fase 1: <code>${esc(p1.state)}</code> (${p1.done}/${p1.total}) · Fase 2: <code>${esc(p2.state)}</code> (${p2.done}/${p2.total})</p>` +
          `<p>Archivo en curso: <code>${esc(p2.current_file || "ninguno")}</code> · PID hf: <code>${esc(p2.hf_pid || "-")}</code> (${p2.hf_rss_mb} MB)</p>`;

        const envRows = Object.entries(d.env_masked || {}).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" · ");
        document.getElementById("svc-body").innerHTML =
          `<p>Nginx: <span class="badge b-ok">Activo (${esc(d.services.nginx.workers_user)})</span> · ` +
          `Terminal Web: <span class="badge ${d.services.terminal.running ? 'b-ok' : 'b-warn'}">${d.services.terminal.running ? 'Activa' : 'Desactivada'}</span></p>` +
          `<p style="font-size:13px;color:var(--muted);">${envRows}</p>`;

        if (d.errors_recent && d.errors_recent.length > 0) {
          document.getElementById("err-body").innerHTML = d.errors_recent.map(e =>
            `<div style="font-family:monospace;font-size:13px;margin-bottom:4px;"><strong>[${esc(e.source)}]</strong> ${esc(e.line)}</div>`
          ).join("");
        } else {
          document.getElementById("err-body").textContent = "Sin errores recientes.";
        }
      } catch (_) {}
    }

    async function loadLog() {
      if (paused) return;
      try {
        const res = await fetch(`/logs/${currentLog}?n=${currentLines}`, { cache: "no-store" });
        const txt = await res.text();
        lastRawLog = txt;
        const errRx = /(ERROR|CRITICAL|FATAL|OutOfMemoryError|Traceback|IMPORT FAILED|\[DIAG\]\[FAIL\])/i;
        const htmlLines = txt.split("\n").map(line =>
          errRx.test(line) ? `<span class="err-line">${esc(line)}</span>` : esc(line)
        ).join("\n");
        document.getElementById("log-view").innerHTML = htmlLines || "(Log vacío)";
      } catch (_) {}
    }

    document.getElementById("pause-btn").addEventListener("click", (ev) => {
      paused = !paused;
      ev.target.textContent = paused ? "Reanudar actualización" : "Pausar actualización";
    });

    document.getElementById("more-lines-btn").addEventListener("click", () => {
      currentLines = 2000;
      loadLog();
    });

    document.getElementById("copy-log-btn").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(lastRawLog);
        alert("Log copiado al portapapeles.");
      } catch (_) {
        const fb = document.getElementById("copy-fallback");
        const ta = document.getElementById("copy-textarea");
        fb.style.display = "block";
        ta.value = lastRawLog;
        ta.focus();
        ta.select();
      }
    });

    document.querySelectorAll("#log-tabs button").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#log-tabs button").forEach(b => b.classList.remove("tab-active"));
        btn.classList.add("tab-active");
        currentLog = btn.getAttribute("data-log");
        currentLines = 200;
        loadLog();
      });
    });

    refreshStatus();
    loadLog();
    setInterval(refreshStatus, 10000);
    setInterval(loadLog, 10000);
  </script>
</body>
</html>
"""


class StatusHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ComfyUIStatus/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silenciar logs rutinarios salvo errores
        return

    def _send(self, code: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self._handle_request()

    def do_GET(self) -> None:
        self._handle_request()

    def do_POST(self) -> None:
        self._send(405, b"Method Not Allowed\n", "text/plain; charset=utf-8")

    def do_PUT(self) -> None:
        self._send(405, b"Method Not Allowed\n", "text/plain; charset=utf-8")

    def do_DELETE(self) -> None:
        self._send(405, b"Method Not Allowed\n", "text/plain; charset=utf-8")

    def _handle_request(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/status":
            self._send(200, STATUS_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/status/api":
            data = build_status_api()
            raw = redact_secrets(json.dumps(data, ensure_ascii=False, indent=2)).encode("utf-8")
            self._send(200, raw, "application/json; charset=utf-8")
            return

        if path == "/report.md":
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
            report_md = build_report_markdown().encode("utf-8")
            self._send(
                200,
                report_md,
                "text/markdown; charset=utf-8",
                {"Content-Disposition": f'inline; filename="comfyui-report-{ts}.md"'},
            )
            return

        if path == "/diag":
            diag_path = pathlib.Path(LOG_WHITELIST["diag"])
            if not diag_path.is_file() or diag_path.stat().st_size == 0:
                self._send(
                    200,
                    "[DIAG] pendiente: diagnóstico de arranque en curso\n".encode("utf-8"),
                    "text/plain; charset=utf-8",
                )
                return
            content = redact_secrets(diag_path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
            self._send(200, content, "text/plain; charset=utf-8")
            return

        if path.startswith("/logs/"):
            log_name = path[len("/logs/"):].strip("/")
            if log_name not in LOG_WHITELIST:
                self._send(404, b"Log no encontrado en la lista blanca\n", "text/plain; charset=utf-8")
                return
            qs = urllib.parse.parse_qs(parsed.query)
            n_str = qs.get("n", ["200"])[0]
            if not n_str.isdigit():
                self._send(400, b"Parametro n debe ser numerico\n", "text/plain; charset=utf-8")
                return
            n_val = max(1, min(2000, int(n_str)))
            lines = tail_lines(LOG_WHITELIST[log_name], n_val)
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self._send(200, body, "text/plain; charset=utf-8")
            return

        self._send(404, b"Not Found\n", "text/plain; charset=utf-8")


def main() -> None:
    threading.Thread(target=_heavy_worker_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer((HOST, PORT), StatusHandler)
    print(f"[status-server] Escuchando en http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

