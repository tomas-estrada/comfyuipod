"""Tests para el helper Mac `tools/mac/comfy` (VAL-SERVER-005 y VAL-CROSS-004).

Verifica:
1. `comfy --help` documenta `rent studio`, `comfy studio [<id>]` y `comfy studio report`.
2. `comfy rent studio`:
   - Lectura oculta (`read -s`) de `STUDIO_LLM_KEY` sin eco, sin disco ni historial.
   - `STUDIO_LLM_KEY` llega en `--env` de `vastai create instance` junto con
     `MODEL_PROFILE="studio"` y `--disk 200` (`--disk 250` con `STUDIO_INCLUDE_ANIMATE=1`).
   - Filtros (`gpu_name=RTX_5090`, `disk_space>=200`, `inet_down>=5000`,
     `reliability>=0.98`) probados contra fixtures con ofertas mixtas.
3. `comfy studio [<id>]`:
   - Resuelve la URL del portal, verifica `/studio/api/health` con Basic Auth
     por stdin (`curl -K -`) y abre el navegador en `/studio/`.
4. `comfy studio report [<id>] [--stdout]`:
   - Diagnóstico redactado con envs enmascaradas, modelos, salud de nodes y disco.
   - Jamás expone `STUDIO_LLM_KEY`, `COMFY_PASS`, `HF_TOKEN` ni tokens `sk-*`/`hf_*`.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMFY_SCRIPT = REPO_ROOT / "tools" / "mac" / "comfy"


def _find_bash() -> str:
    if os.name == "nt":
        candidates = [
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("C:/Program Files/Git/usr/bin/bash.exe"),
        ]
        for c in candidates:
            if c.is_file():
                return str(c)
    found = shutil.which("bash")
    if not found:
        pytest.skip("bash no disponible en este entorno")
    return found


def _to_bash_path(path: Path) -> str:
    posix = path.resolve().as_posix()
    if os.name == "nt" and len(posix) >= 2 and posix[1] == ":":
        drive = posix[0].lower()
        return f"/{drive}{posix[2:]}"
    return posix


def _setup_mock_bin(tmp_path: Path) -> tuple[Path, Path]:
    """Crea un directorio bin temporal con shims para `python3`, `vastai` y `COMFY_BROWSER`."""
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "mock_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    py_exe = _to_bash_path(Path(sys.executable))

    # Shim python3 -> ejecuta el intérprete Python activo del .venv / CI
    python3_shim = bin_dir / "python3"
    python3_shim.write_text(
        f'#!/usr/bin/env bash\nexec "{py_exe}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    python3_shim.chmod(0o755)

    # Shim vastai -> delega a un script Python que inspecciona subcomandos y guarda trazas
    vastai_py = bin_dir / "vastai_impl.py"
    vastai_py.write_text(
        """from __future__ import annotations
import json, os, pathlib, sys

state_dir = pathlib.Path(os.environ["MOCK_VASTAI_STATE_DIR"])
args = sys.argv[1:]

calls_file = state_dir / "vastai_calls.jsonl"
with calls_file.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "args": args,
        "has_llm_key_in_proc_env": "STUDIO_LLM_KEY" in os.environ,
        "has_fallback_key_in_proc_env": "STUDIO_LLM_FALLBACK_KEY" in os.environ,
    }) + "\\n")

if len(args) >= 2 and args[0] == "search" and args[1] == "offers":
    offers_path = state_dir / "offers_fixture.json"
    if offers_path.is_file():
        sys.stdout.write(offers_path.read_text(encoding="utf-8"))
    else:
        sys.stdout.write("[]")
    sys.exit(0)

if len(args) >= 2 and args[0] == "create" and args[1] == "instance":
    created_path = state_dir / "created_instance.json"
    created_path.write_text(json.dumps({"args": args}), encoding="utf-8")
    sys.stdout.write("Started. {'success': True, 'new_contract': 987654}\\n")
    sys.exit(0)

if len(args) >= 2 and args[0] == "show" and args[1] == "instances":
    inst_path = state_dir / "instances_fixture.json"
    if inst_path.is_file():
        sys.stdout.write(inst_path.read_text(encoding="utf-8"))
    else:
        sys.stdout.write('{"instances": []}')
    sys.exit(0)

if len(args) >= 1 and args[0] == "logs":
    logs_path = state_dir / "logs_fixture.txt"
    if logs_path.is_file():
        sys.stdout.write(logs_path.read_text(encoding="utf-8"))
    else:
        sys.stdout.write("")
    sys.exit(0)

if len(args) >= 1 and args[0] == "ssh":
    ssh_fixture = state_dir / "ssh_outputs_fixture.b64"
    if ssh_fixture.is_file():
        sys.stdout.write(ssh_fixture.read_text(encoding="utf-8"))
        sys.exit(0)
    sys.stderr.write("mock vastai ssh: sin fixture ssh_outputs_fixture.b64\\n")
    sys.exit(1)

sys.stderr.write(f"Unsupported mock vastai args: {args}\\n")
sys.exit(1)
""",
        encoding="utf-8",
        newline="\n",
    )

    vastai_shim = bin_dir / "vastai"
    vastai_shim.write_text(
        f'#!/usr/bin/env bash\nexec "{py_exe}" "{_to_bash_path(vastai_py)}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    vastai_shim.chmod(0o755)

    browser_shim = bin_dir / "mock-browser"
    browser_shim.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$1" >> "${MOCK_VASTAI_STATE_DIR}/browser_opened.txt"\n',
        encoding="utf-8",
        newline="\n",
    )
    browser_shim.chmod(0o755)

    return bin_dir, state_dir


def _run_comfy(
    args: list[str],
    *,
    bin_dir: Path,
    state_dir: Path,
    extra_env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bash_bin = _find_bash()
    env = os.environ.copy()
    # Limpiar variables sensibles del entorno heredado para pruebas deterministas
    for k in (
        "COMFY_IMAGE",
        "COMFY_PASS",
        "COMFY_USER",
        "HF_TOKEN",
        "STUDIO_LLM_KEY",
        "STUDIO_LLM_FALLBACK_KEY",
        "STUDIO_LLM_BASE_URL",
        "STUDIO_LLM_MODEL",
        "STUDIO_LLM_FALLBACK_BASE_URL",
        "STUDIO_LLM_FALLBACK_MODEL",
        "STUDIO_INCLUDE_ANIMATE",
        "STUDIO_STUB_LLM",
        "COMFY_DISK_GB",
    ):
        env.pop(k, None)

    orig_path = env.get("PATH", "")
    # En Git Bash sobre Windows, /usr/bin:/bin provee utilidades POSIX (sed, tr, wc, curl, etc.)
    env["PATH"] = f"{_to_bash_path(bin_dir)}:/usr/bin:/bin:{orig_path}"
    env["MOCK_VASTAI_STATE_DIR"] = str(state_dir)
    env["COMFY_BROWSER"] = _to_bash_path(bin_dir / "mock-browser")
    env["COMFY_PYTHON"] = _to_bash_path(Path(sys.executable))
    env["HISTFILE"] = str(state_dir / "bash_history")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [bash_bin, _to_bash_path(COMFY_SCRIPT), *args],
        input=stdin_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )


def _sample_offers_fixture() -> list[dict[str, Any]]:
    return [
        {
            "id": 101,
            "gpu_name": "RTX_5090",
            "gpu_ram": 32768,
            "cpu_ram": 65536,
            "cuda_vers": 12.8,
            "disk_space": 150,  # Insuficiente (< 200 GB)
            "inet_down": 6000,
            "reliability": 0.99,
            "verified": True,
            "rentable": True,
            "dph_total": 0.35,
            "storage_cost": 0.15,
        },
        {
            "id": 102,
            "gpu_name": "RTX_5090",
            "gpu_ram": 32768,
            "cpu_ram": 65536,
            "cuda_vers": 12.8,
            "disk_space": 250,
            "inet_down": 2400,  # Insuficiente (< 5000 Mbps)
            "reliability": 0.99,
            "verified": True,
            "rentable": True,
            "dph_total": 0.38,
            "storage_cost": 0.15,
        },
        {
            "id": 103,
            "gpu_name": "RTX_5090",
            "gpu_ram": 32768,
            "cpu_ram": 65536,
            "cuda_vers": 12.8,
            "disk_space": 250,
            "inet_down": 6500,
            "reliability": 0.94,  # Insuficiente (< 0.98)
            "verified": True,
            "rentable": True,
            "dph_total": 0.40,
            "storage_cost": 0.15,
        },
        {
            "id": 104,
            "gpu_name": "RTX_4090",  # No es RTX_5090 (24 GB VRAM insuficiente para studio)
            "gpu_ram": 24576,
            "cpu_ram": 65536,
            "cuda_vers": 12.8,
            "disk_space": 300,
            "inet_down": 7000,
            "reliability": 0.995,
            "verified": True,
            "rentable": True,
            "dph_total": 0.42,
            "storage_cost": 0.15,
        },
        {
            "id": 105,
            "gpu_name": "RTX_5090",
            "gpu_ram": 32768,
            "cpu_ram": 65536,
            "cuda_vers": 12.8,
            # Válida para studio estándar (>= 200 GB), insuficiente para Animate (>= 250 GB)
            "disk_space": 220,
            "inet_down": 5400,
            "reliability": 0.991,
            "verified": True,
            "rentable": True,
            "dph_total": 0.62,
            "storage_cost": 0.15,
        },
        {
            "id": 106,
            "gpu_name": "RTX_5090",
            "gpu_ram": 32768,
            "cpu_ram": 65536,
            "cuda_vers": 12.9,
            "disk_space": 300,  # Válida también para STUDIO_INCLUDE_ANIMATE=1 (>= 250 GB)
            "inet_down": 8200,
            "reliability": 0.996,
            "verified": True,
            "rentable": True,
            "dph_total": 0.74,
            "storage_cost": 0.15,
        },
    ]


def test_comfy_help_lists_studio_commands(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    res = _run_comfy(["--help"], bin_dir=bin_dir, state_dir=state_dir)
    assert res.returncode == 0, res.stderr
    assert "studio" in res.stdout
    assert "comfy studio" in res.stdout
    assert "comfy studio report" in res.stdout
    assert "fetch-outputs" in res.stdout


def test_rent_studio_hidden_prompt_no_echo_no_disk_and_env_plumbing(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    (state_dir / "offers_fixture.json").write_text(
        json.dumps(_sample_offers_fixture()),
        encoding="utf-8",
    )

    synthetic_llm_key = "sk" + "-studio-unit-secret-key-9988776655"
    synthetic_fallback_key = "sk" + "-studio-fallback-secret-key-1122334455"
    synthetic_pod_pass = "pod-secret-pass-44332211"

    # 1. Dry-run leyendo la llave desde stdin (read -r -s) y con STUDIO_LLM_FALLBACK_KEY definida
    dry = _run_comfy(
        ["rent", "studio", "--dry-run"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "COMFY_PASS": synthetic_pod_pass,
            "STUDIO_LLM_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "STUDIO_LLM_MODEL": "qwen3-vl-plus",
            "STUDIO_LLM_FALLBACK_KEY": synthetic_fallback_key,
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert dry.returncode == 0, f"stderr={dry.stderr}\nstdout={dry.stdout}"
    combined_dry = dry.stdout + "\n" + dry.stderr
    assert synthetic_llm_key not in combined_dry, "La llave STUDIO_LLM_KEY se filtró en dry-run"
    assert (
        synthetic_fallback_key not in combined_dry
    ), "La llave STUDIO_LLM_FALLBACK_KEY se filtró en dry-run"
    assert synthetic_pod_pass not in combined_dry, "COMFY_PASS se filtró en dry-run"
    assert "[DRY-RUN]" in dry.stdout
    assert "105" in dry.stdout  # Oferta 105 seleccionada tras filtrar 101..104
    assert "200" in dry.stdout
    assert "STUDIO_LLM_KEY=<definida>" in dry.stdout
    assert "STUDIO_LLM_FALLBACK_KEY=<definida>" in dry.stdout

    # Verificar que no se escribió al archivo HISTFILE
    hist_file = state_dir / "bash_history"
    if hist_file.exists():
        hist_txt = hist_file.read_text(encoding="utf-8")
        assert synthetic_llm_key not in hist_txt
        assert synthetic_fallback_key not in hist_txt

    # 2. Simulación completa con `--yes` (verifica env_flags entregados a vastai create instance)
    sim = _run_comfy(
        ["rent", "studio", "--yes"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "COMFY_PASS": synthetic_pod_pass,
            "STUDIO_LLM_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "STUDIO_LLM_MODEL": "qwen3-vl-plus",
            "STUDIO_LLM_FALLBACK_BASE_URL": "https://api.deepseek.com",
            "STUDIO_LLM_FALLBACK_KEY": synthetic_fallback_key,
            "STUDIO_LLM_FALLBACK_MODEL": "deepseek-chat",
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert sim.returncode == 0, f"stderr={sim.stderr}\nstdout={sim.stdout}"
    combined_sim = sim.stdout + "\n" + sim.stderr
    assert synthetic_llm_key not in combined_sim, "STUDIO_LLM_KEY se imprimió en terminal"
    assert (
        synthetic_fallback_key not in combined_sim
    ), "STUDIO_LLM_FALLBACK_KEY se imprimió en terminal"
    assert synthetic_pod_pass not in combined_sim, "COMFY_PASS se imprimió en terminal"

    calls = [
        json.loads(line)
        for line in (state_dir / "vastai_calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Ningún subproceso vastai hereda STUDIO_LLM_KEY ni STUDIO_LLM_FALLBACK_KEY en su entorno
    for call in calls:
        assert call["has_llm_key_in_proc_env"] is False
        assert call["has_fallback_key_in_proc_env"] is False

    search_calls = [c for c in calls if c["args"][:2] == ["search", "offers"]]
    assert search_calls, "No se invocó vastai search offers"
    search_query = search_calls[-1]["args"][2]
    assert "gpu_name=RTX_5090" in search_query
    assert "disk_space>=200" in search_query
    assert "inet_down>=5000" in search_query
    assert "reliability>=0.98" in search_query

    created = json.loads((state_dir / "created_instance.json").read_text(encoding="utf-8"))
    c_args = created["args"]
    assert c_args[:3] == ["create", "instance", "105"]
    assert "--disk" in c_args
    assert c_args[c_args.index("--disk") + 1] == "200"
    assert "--label" in c_args
    assert c_args[c_args.index("--label") + 1] == "comfy-studio"
    assert "--args" in c_args
    env_arg = c_args[c_args.index("--env") + 1]
    assert '-e MODEL_PROFILE="studio"' in env_arg
    assert f'-e STUDIO_LLM_KEY="{synthetic_llm_key}"' in env_arg
    assert f'-e STUDIO_LLM_FALLBACK_KEY="{synthetic_fallback_key}"' in env_arg
    assert f'-e COMFY_PASS="{synthetic_pod_pass}"' in env_arg
    assert '-e STUDIO_LLM_MODEL="qwen3-vl-plus"' in env_arg
    assert '-e STUDIO_LLM_FALLBACK_MODEL="deepseek-chat"' in env_arg


def test_rent_studio_animate_opt_in_requires_250gb_disk(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    (state_dir / "offers_fixture.json").write_text(
        json.dumps(_sample_offers_fixture()),
        encoding="utf-8",
    )
    synthetic_llm_key = "sk" + "-animate-optin-secret-12345678"

    sim = _run_comfy(
        ["rent", "studio", "--yes"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "STUDIO_INCLUDE_ANIMATE": "1",
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert sim.returncode == 0, f"stderr={sim.stderr}\nstdout={sim.stdout}"
    assert synthetic_llm_key not in (sim.stdout + sim.stderr)

    created = json.loads((state_dir / "created_instance.json").read_text(encoding="utf-8"))
    c_args = created["args"]
    # Oferta 105 tiene 220 GB (< 250 GB), por lo que debe elegirse la oferta 106 (300 GB)
    assert c_args[:3] == ["create", "instance", "106"]
    assert c_args[c_args.index("--disk") + 1] == "250"
    env_arg = c_args[c_args.index("--env") + 1]
    assert '-e STUDIO_INCLUDE_ANIMATE="1"' in env_arg


def test_rent_studio_rejects_insufficient_disk_override_and_accepts_valid_override(
    tmp_path: Path,
) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    (state_dir / "offers_fixture.json").write_text(
        json.dumps(_sample_offers_fixture()),
        encoding="utf-8",
    )
    synthetic_llm_key = "sk" + "-disk-override-secret-12345678"

    # 1. Override COMFY_DISK_GB=120 (< 200 GB mínimo de studio) debe rechazarse antes de
    #    filtrar/seleccionar ofertas de 150 GB o llamar a vastai create instance.
    low_base = _run_comfy(
        ["rent", "studio", "--yes"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "COMFY_DISK_GB": "120",
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert low_base.returncode != 0, (
        f"Se esperaba fallo con COMFY_DISK_GB=120; "
        f"stdout={low_base.stdout}\nstderr={low_base.stderr}"
    )
    assert "COMFY_DISK_GB" in low_base.stderr and "200" in low_base.stderr
    assert not (state_dir / "created_instance.json").exists()
    assert not (state_dir / "vastai_calls.jsonl").exists()

    # 2. Con STUDIO_INCLUDE_ANIMATE=1, el mínimo sube a 250 GB; COMFY_DISK_GB=200 debe rechazarse
    #    y nunca seleccionar la oferta 105 (220 GB) ni llamar a create instance.
    low_animate = _run_comfy(
        ["rent", "studio", "--yes"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "STUDIO_INCLUDE_ANIMATE": "1",
            "COMFY_DISK_GB": "200",
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert low_animate.returncode != 0, (
        f"Se esperaba fallo con STUDIO_INCLUDE_ANIMATE=1 y COMFY_DISK_GB=200; "
        f"stdout={low_animate.stdout}\nstderr={low_animate.stderr}"
    )
    assert "COMFY_DISK_GB" in low_animate.stderr and "250" in low_animate.stderr
    assert not (state_dir / "created_instance.json").exists()
    assert not (state_dir / "vastai_calls.jsonl").exists()

    # 3. Valor no numérico en COMFY_DISK_GB también se rechaza con mensaje claro.
    invalid_disk = _run_comfy(
        ["rent", "studio", "--dry-run"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "COMFY_DISK_GB": "abc",
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert invalid_disk.returncode != 0
    assert "COMFY_DISK_GB" in invalid_disk.stderr
    assert not (state_dir / "created_instance.json").exists()

    # 4. Override válido por encima del mínimo (COMFY_DISK_GB=260) se respeta: filtra
    #    la oferta 105 (220 GB) y selecciona la 106 (300 GB) con --disk 260.
    valid_override = _run_comfy(
        ["rent", "studio", "--yes"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={
            "COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest",
            "COMFY_DISK_GB": "260",
        },
        stdin_text=f"{synthetic_llm_key}\n",
    )
    assert valid_override.returncode == 0, (
        f"stderr={valid_override.stderr}\nstdout={valid_override.stdout}"
    )
    created = json.loads((state_dir / "created_instance.json").read_text(encoding="utf-8"))
    c_args = created["args"]
    assert c_args[:3] == ["create", "instance", "106"]
    assert c_args[c_args.index("--disk") + 1] == "260"


class _MockPodServer:
    """Servidor HTTP local que simula el edge Nginx + comfyctl + studio-server."""

    def __init__(
        self,
        *,
        expected_user: str = "admin",
        expected_pass: str = "changeme-pod-pass-8899",
        leaked_llm_key: str = "YOUR_STUDIO_API_KEY_HERE",
        leaked_hf_token: str = "YOUR_HF_TOKEN_HERE",
        studio_healthy: bool = True,
        llm_key_unset: bool = False,
    ) -> None:
        self.expected_user = expected_user
        self.expected_pass = expected_pass
        self.leaked_llm_key = leaked_llm_key
        self.leaked_hf_token = leaked_hf_token
        self.studio_healthy = studio_healthy
        self.llm_key_unset = llm_key_unset
        self.requests: list[str] = []

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _check_auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    return False
                raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                return raw == f"{parent.expected_user}:{parent.expected_pass}"

            def _send(self, code: int, body: str, content_type: str = "application/json") -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def do_HEAD(self) -> None:
                parent.requests.append(f"HEAD {self.path}")
                if self.path == "/healthz":
                    self._send(200, "")
                else:
                    self._send(404, "")

            def do_GET(self) -> None:
                parent.requests.append(f"GET {self.path}")
                if self.path == "/healthz":
                    self._send(200, "ok", "text/plain")
                    return
                if not self._check_auth():
                    self._send(401, '{"detail":"Unauthorized"}')
                    return
                if self.path == "/studio/api/health":
                    if not parent.studio_healthy:
                        self._send(502, '{"status":"error"}')
                        return
                    key_st = "<no definida>" if parent.llm_key_unset else "<definida>"
                    self._send(
                        200,
                        json.dumps(
                            {
                                "status": "ok",
                                "server": "ok",
                                "comfyui": "ok",
                                "comfyui_reachable": True,
                                "model_profile": "studio",
                                "llm_model": "qwen3-vl-plus",
                                "llm_key_status": key_st,
                                "schema_version": 1,
                            }
                        ),
                    )
                    return
                if self.path == "/studio/api/status":
                    key_st = "<no definida>" if parent.llm_key_unset else "<definida>"
                    self._send(
                        200,
                        json.dumps(
                            {
                                "status": "ok",
                                "comfyui": {"status": "ok", "reachable": True},
                                "db": {"schema_version": 1, "projects_count": 2, "jobs_count": 3},
                                "llm": {
                                    "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                                    "model": "qwen3-vl-plus",
                                    "fallback_base_url": "https://api.deepseek.com",
                                    "fallback_model": "deepseek-chat",
                                    "key_status": key_st,
                                    "stub": parent.llm_key_unset,
                                },
                                "env_masked": {
                                    "MODEL_PROFILE": "studio",
                                    "STUDIO_LLM_KEY": key_st,
                                    "STUDIO_LLM_FALLBACK_KEY": key_st,
                                    "STUDIO_LLM_MODEL": "qwen3-vl-plus",
                                    "COMFY_USER": parent.expected_user,
                                    "COMFY_PASS": "<definida>",
                                },
                            }
                        ),
                    )
                    return
                if self.path == "/status/api":
                    self._send(
                        200,
                        json.dumps(
                            {
                                "generated_at": "2026-10-27T12:00:00Z",
                                "phase_banner": "Listo (MODEL_PROFILE=studio)",
                                "instance": {
                                    "hostname": "pod-studio-5090",
                                    "uptime_s": 420,
                                    "vast_instance_id": "4242",
                                    "external_port_8188": "18188",
                                },
                                "gpu": {
                                    "available": True,
                                    "name": "NVIDIA GeForce RTX 5090",
                                    "driver": "570.124.06",
                                    "vram_used_mb": 2048,
                                    "vram_total_mb": 32768,
                                    "util_pct": 5,
                                    "temp_c": 38,
                                },
                                "memory": {
                                    "total_mb": 65536,
                                    "used_mb": 8192,
                                    "available_mb": 57344,
                                    "swap_used_mb": 0,
                                },
                                "disk": {
                                    "path": "/workspace/comfyui-data",
                                    "total_gb": 200.0,
                                    "used_gb": 163.2,
                                    "free_gb": 36.8,
                                },
                                "services": {
                                    "nginx": {"running": True, "workers_user": "www-data"},
                                    "status_server": {"running": True},
                                    "terminal": {"enabled": True, "running": True},
                                    "studio_server": {
                                        "enabled": True,
                                        "running": True,
                                        "port": 18190,
                                    },
                                },
                                "strict": {
                                    "result": "ok",
                                    "strict_start": True,
                                    "checked_at": "2026-10-27T11:55:00Z",
                                    "checks": [
                                        {
                                            "id": "S5",
                                            "name": "disk_space",
                                            "status": "ok",
                                            "detail": (
                                                "195.0 GB libres >= 180 GB requeridos (studio)"
                                            ),
                                            "remediation": None,
                                        },
                                        {
                                            "id": "S11",
                                            "name": "studio_health",
                                            "status": "ok",
                                            "detail": (
                                                "studio-server respondiendo en 127.0.0.1:18190"
                                            ),
                                            "remediation": None,
                                        },
                                    ],
                                },
                                "env_masked": {
                                    "MODEL_PROFILE": "studio",
                                    "COMFY_USER": parent.expected_user,
                                    "COMFY_PASS": (
                                        "<definida por el usuario>"
                                        if parent.llm_key_unset
                                        else "<definida>"
                                    ),
                                    "HF_TOKEN": (
                                        "<no definido>"
                                        if parent.llm_key_unset
                                        else "<definido>"
                                    ),
                                    "STUDIO_LLM_KEY": (
                                        "<no definida>"
                                        if parent.llm_key_unset
                                        else "<definida>"
                                    ),
                                    "STUDIO_LLM_FALLBACK_KEY": (
                                        "<no definida>"
                                        if parent.llm_key_unset
                                        else "<definida>"
                                    ),
                                    "STUDIO_LLM_MODEL": "qwen3-vl-plus",
                                },
                            }
                        ),
                    )
                    return
                if self.path == "/report.md":
                    # Incluye datos de modelos, nodes, disco y una traza accidental con secretos
                    # para comprobar que `comfy studio report` redacta en defensa en profundidad.
                    llm_rep_val = "<no definida>" if parent.llm_key_unset else parent.leaked_llm_key
                    fb_rep_val = (
                        "<no definida>"
                        if parent.llm_key_unset
                        else "YOUR_FALLBACK_REPORT_KEY_HERE"
                    )
                    hf_rep_val = "<no definido>" if parent.llm_key_unset else parent.leaked_hf_token
                    pass_rep_val = (
                        "<definida por el usuario>"
                        if parent.llm_key_unset
                        else parent.expected_pass
                    )
                    extra_trace = (
                        "STUDIO_LLM_KEY=<no definida> STUDIO_LLM_FALLBACK_KEY=<no definida>\n\n"
                        if parent.llm_key_unset
                        else f"- Traza fugada: Authorization: Bearer {parent.leaked_llm_key}\n\n"
                    )
                    md = (
                        "# Informe de Estado y Diagnóstico — ComfyUIPod\n"
                        "## 4. GPU, memoria y disco\n"
                        "- **GPU**: `NVIDIA GeForce RTX 5090` · Driver `570.124.06`\n"
                        "- **Disco (`/workspace/comfyui-data`)**: `163.2` GB usados / "
                        "`200.0` GB totales (`36.8` GB libres)\n\n"
                        "## 8. Drift del entorno\n"
                        "### Estado de `custom_nodes/` vs `/etc/comfyui/nodes.lock`\n"
                        "- Los 12 custom nodes coinciden exactamente con los SHAs de "
                        "`/etc/comfyui/nodes.lock` (incl. ComfyUI-LatentSyncWrapper y "
                        "ComfyUI-LivePortraitKJ).\n"
                        "### Variables de entorno relevantes (enmascaradas)\n"
                        "- `MODEL_PROFILE` = `studio`\n"
                        f"- `STUDIO_LLM_KEY` = `{llm_rep_val}`\n"
                        f"- `STUDIO_LLM_FALLBACK_KEY` = `{fb_rep_val}`\n"
                        f"- `COMFY_PASS` = `{pass_rep_val}`\n"
                        f"- `HF_TOKEN` = `{hf_rep_val}`\n"
                        f"{extra_trace}"
                        "## 9. Modelos\n"
                        "- `diffusion_models/`: 8 archivo(s) (115.20 GiB)\n"
                        "- `liveportrait/`: 6 archivo(s) (0.59 GiB)\n"
                        "- `whisper/`: 7 archivo(s) (5.23 GiB)\n"
                        "### Modelos esperados para el perfil `studio` aún ausentes\n"
                        "- Todos los modelos del perfil están presentes en disco.\n\n"
                        "## 10. Arranque estricto\n"
                        "- **Resultado**: `ok` · `[OK]` **S5** (disk_space) · "
                        "`[OK]` **S11** (studio_health)\n"
                    )
                    self._send(200, md, "text/markdown; charset=utf-8")
                    return
                self._send(404, '{"detail":"not found"}')

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_port
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _MockPodServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def test_comfy_studio_resolves_portal_verifies_health_and_opens_browser(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    pod_pass = "pod-edge-pass-8899"

    with _MockPodServer(expected_pass=pod_pass, studio_healthy=True) as srv:
        (state_dir / "instances_fixture.json").write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "id": 4242,
                            "actual_status": "running",
                            "public_ipaddr": "127.0.0.1",
                            "gpu_name": "RTX 5090",
                            "dph_total": 0.65,
                            "label": "comfy-studio",
                            "ports": {"8188/tcp": [{"HostPort": str(srv.port)}]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        res = _run_comfy(
            ["studio", "4242"],
            bin_dir=bin_dir,
            state_dir=state_dir,
            extra_env={"COMFY_PASS": pod_pass, "COMFY_WAIT_TIMEOUT": "10"},
        )
        assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
        assert pod_pass not in (res.stdout + res.stderr)
        assert f"http://127.0.0.1:{srv.port}/studio/" in res.stdout
        assert "GET /studio/api/health" in srv.requests

        opened = (state_dir / "browser_opened.txt").read_text(encoding="utf-8").splitlines()
        assert opened == [f"http://127.0.0.1:{srv.port}/studio/"]


def test_comfy_studio_fails_when_health_endpoint_unhealthy(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    pod_pass = "pod-edge-pass-8899"

    with _MockPodServer(expected_pass=pod_pass, studio_healthy=False) as srv:
        (state_dir / "instances_fixture.json").write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "id": 4242,
                            "actual_status": "running",
                            "public_ipaddr": "127.0.0.1",
                            "gpu_name": "RTX 5090",
                            "dph_total": 0.65,
                            "label": "comfy-studio",
                            "ports": {"8188/tcp": [{"HostPort": str(srv.port)}]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        res = _run_comfy(
            ["studio", "4242"],
            bin_dir=bin_dir,
            state_dir=state_dir,
            extra_env={"COMFY_PASS": pod_pass, "COMFY_WAIT_TIMEOUT": "2"},
        )
        assert res.returncode != 0
        assert not (state_dir / "browser_opened.txt").exists()


def test_comfy_studio_report_includes_diagnostics_and_masks_all_secrets(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    pod_pass = "changeme-pod-pass-8899"
    leaked_llm_key = "sk" + "-YOUR_API_KEY_HERE_77665544"
    leaked_hf_token = "hf" + "_YOUR_HF_TOKEN_HERE_12345"

    with _MockPodServer(
        expected_pass=pod_pass,
        leaked_llm_key=leaked_llm_key,
        leaked_hf_token=leaked_hf_token,
        studio_healthy=True,
    ) as srv:
        (state_dir / "instances_fixture.json").write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "id": 4242,
                            "actual_status": "running",
                            "public_ipaddr": "127.0.0.1",
                            "gpu_name": "RTX 5090",
                            "dph_total": 0.65,
                            "label": "comfy-studio",
                            "ports": {"8188/tcp": [{"HostPort": str(srv.port)}]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        res = _run_comfy(
            ["studio", "report", "4242", "--stdout"],
            bin_dir=bin_dir,
            state_dir=state_dir,
            extra_env={
                "COMFY_PASS": pod_pass,
                "STUDIO_LLM_KEY": leaked_llm_key,
                "STUDIO_LLM_FALLBACK_KEY": "YOUR_FALLBACK_REPORT_KEY_HERE",
                "HF_TOKEN": leaked_hf_token,
            },
        )
        assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
        out = res.stdout
        combined = res.stdout + "\n" + res.stderr

        # Cero fugas de secretos (VAL-SERVER-005 / VAL-CROSS-004)
        assert leaked_llm_key not in combined, "Fuga de STUDIO_LLM_KEY en comfy studio report"
        assert (
            "YOUR_FALLBACK_REPORT_KEY_HERE" not in combined
        ), "Fuga de STUDIO_LLM_FALLBACK_KEY en comfy studio report"
        assert pod_pass not in combined, "Fuga de COMFY_PASS en comfy studio report"
        assert leaked_hf_token not in combined, "Fuga de HF_TOKEN en comfy studio report"

        # Presencia de las 4 secciones diagnósticas requeridas:
        # 1) envs enmascaradas, 2) presencia de modelos, 3) salud de nodes, 4) disco
        assert "STUDIO_LLM_KEY" in out
        assert "<definida>" in out or "[REDACTED]" in out
        assert "diffusion_models/" in out
        assert "Todos los modelos del perfil están presentes" in out
        assert "nodes.lock" in out
        assert "S11" in out and "studio_health" in out
        assert "/workspace/comfyui-data" in out
        assert "163.2" in out and "200.0" in out

        # También verificar que `comfy report 4242 --stdout` redacta en defensa en profundidad
        res_rep = _run_comfy(
            ["report", "4242", "--stdout"],
            bin_dir=bin_dir,
            state_dir=state_dir,
            extra_env={
                "COMFY_PASS": pod_pass,
                "STUDIO_LLM_KEY": leaked_llm_key,
                "HF_TOKEN": leaked_hf_token,
            },
        )
        assert res_rep.returncode == 0, f"stderr={res_rep.stderr}\nstdout={res_rep.stdout}"
        assert leaked_llm_key not in (res_rep.stdout + res_rep.stderr)
        assert pod_pass not in (res_rep.stdout + res_rep.stderr)
        assert leaked_hf_token not in (res_rep.stdout + res_rep.stderr)


def test_comfy_studio_report_preserves_multiword_unset_markers(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    pod_pass = "changeme-pod-pass-8899"

    with _MockPodServer(
        expected_pass=pod_pass,
        studio_healthy=True,
        llm_key_unset=True,
    ) as srv:
        (state_dir / "instances_fixture.json").write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "id": 4242,
                            "actual_status": "running",
                            "public_ipaddr": "127.0.0.1",
                            "gpu_name": "RTX 5090",
                            "dph_total": 0.65,
                            "label": "comfy-studio",
                            "ports": {"8188/tcp": [{"HostPort": str(srv.port)}]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        res = _run_comfy(
            ["studio", "report", "4242", "--stdout"],
            bin_dir=bin_dir,
            state_dir=state_dir,
            extra_env={"COMFY_PASS": pod_pass},
        )
        assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
        out = res.stdout

        assert "<definida> definida>" not in out
        assert "<definida> definido>" not in out
        assert "`STUDIO_LLM_KEY`: `<no definida>`" in out
        assert "- `STUDIO_LLM_KEY` = `<no definida>`" in out
        assert "- `STUDIO_LLM_FALLBACK_KEY` = `<no definida>`" in out
        assert "- `HF_TOKEN` = `<no definido>`" in out
        assert "- `COMFY_PASS` = `<definida por el usuario>`" in out
        assert "STUDIO_LLM_KEY=<no definida>" in out
        assert "`STUDIO_LLM_KEY` = `<definida>`" not in out
        assert "`STUDIO_LLM_FALLBACK_KEY` = `<definida>`" not in out

        res_plain = _run_comfy(
            ["report", "4242", "--stdout"],
            bin_dir=bin_dir,
            state_dir=state_dir,
            extra_env={"COMFY_PASS": pod_pass},
        )
        assert res_plain.returncode == 0, f"stderr={res_plain.stderr}\nstdout={res_plain.stdout}"
        out_plain = res_plain.stdout
        assert "<definida> definida>" not in out_plain
        assert "- `STUDIO_LLM_KEY` = `<no definida>`" in out_plain
        assert "- `STUDIO_LLM_FALLBACK_KEY` = `<no definida>`" in out_plain
        assert "- `HF_TOKEN` = `<no definido>`" in out_plain
        assert "- `COMFY_PASS` = `<definida por el usuario>`" in out_plain


def test_rent_non_studio_profile_preserves_legacy_filters(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    (state_dir / "offers_fixture.json").write_text(
        json.dumps(_sample_offers_fixture()),
        encoding="utf-8",
    )
    res = _run_comfy(
        ["rent", "all", "--dry-run"],
        bin_dir=bin_dir,
        state_dir=state_dir,
        extra_env={"COMFY_IMAGE": "ghcr.io/test-owner/comfyuipod:latest"},
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    # Para perfil 'all' (disco>=120, inet_down>=500), la oferta 101 es válida y la más barata
    assert "Se alquilaría la oferta ID 101" in res.stdout
    assert "disco=120 GB" in res.stdout


def _write_running_instance_fixture(state_dir: Path, inst_id: int = 4242) -> None:
    (state_dir / "instances_fixture.json").write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "id": inst_id,
                        "actual_status": "running",
                        "public_ipaddr": "127.0.0.1",
                        "gpu_name": "RTX 5090",
                        "dph_total": 0.65,
                        "label": "comfy-studio",
                        "ports": {"8188/tcp": [{"HostPort": "38188"}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _build_outputs_tar_b64(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_comfy_fetch_outputs_streams_tar_and_skips_existing(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    _write_running_instance_fixture(state_dir)
    (state_dir / "ssh_outputs_fixture.b64").write_text(
        _build_outputs_tar_b64(
            {
                "output/hello.txt": b"hello-outputs",
                "output/nested/render.mp4": b"\x00\x01\x02binary-payload",
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "dest"

    res = _run_comfy(
        ["fetch-outputs", "4242", dest.as_posix()],
        bin_dir=bin_dir,
        state_dir=state_dir,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    assert (dest / "output" / "hello.txt").read_bytes() == b"hello-outputs"
    assert (dest / "output" / "nested" / "render.mp4").read_bytes() == b"\x00\x01\x02binary-payload"
    assert "2 archivo(s) descargados" in res.stdout

    # Segunda ejecución: todo existe ya → 0 descargados, 2 omitidos (resumible).
    res2 = _run_comfy(
        ["fetch-outputs", "4242", dest.as_posix()],
        bin_dir=bin_dir,
        state_dir=state_dir,
    )
    assert res2.returncode == 0, f"stderr={res2.stderr}\nstdout={res2.stdout}"
    assert "0 archivo(s) descargados" in res2.stdout
    assert "2 omitidos" in res2.stdout

    # El comando remoto usa tar+base64 sobre el data dir del pod.
    calls = [
        json.loads(line)
        for line in (state_dir / "vastai_calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ssh_calls = [c for c in calls if c["args"] and c["args"][0] == "ssh"]
    assert ssh_calls, "se esperaba una llamada 'vastai ssh'"
    remote_cmd = ssh_calls[-1]["args"][-1]
    assert "tar czf - -C '/workspace/comfyui-data' output" in remote_cmd
    assert "| base64" in remote_cmd


def test_comfy_fetch_outputs_rejects_path_traversal(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    _write_running_instance_fixture(state_dir)
    (state_dir / "ssh_outputs_fixture.b64").write_text(
        _build_outputs_tar_b64(
            {
                "output/safe.txt": b"ok",
                "output/../../escape.txt": b"malicioso",
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "dest-traversal"

    res = _run_comfy(
        ["fetch-outputs", "4242", dest.as_posix()],
        bin_dir=bin_dir,
        state_dir=state_dir,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    assert (dest / "output" / "safe.txt").read_bytes() == b"ok"
    assert not (tmp_path / "escape.txt").exists()
    assert "Ruta fuera del destino omitida" in res.stderr


def test_comfy_fetch_outputs_warns_on_empty_remote_archive(tmp_path: Path) -> None:
    bin_dir, state_dir = _setup_mock_bin(tmp_path)
    _write_running_instance_fixture(state_dir)
    (state_dir / "ssh_outputs_fixture.b64").write_text("", encoding="utf-8")
    dest = tmp_path / "dest-empty"

    res = _run_comfy(
        ["fetch-outputs", "4242", dest.as_posix()],
        bin_dir=bin_dir,
        state_dir=state_dir,
    )
    assert res.returncode != 0
    assert "vacío" in res.stderr or "válido" in res.stderr

