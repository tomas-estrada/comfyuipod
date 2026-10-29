"""Detección de capacidades del entorno ML para el backend `auto` (M3.1).

Comprueba:
  - que el intérprete Python del worker exista y sea ejecutable,
  - que `torch`, `open_clip`/`transformers`, `faster_whisper`, `funasr`,
    `insightface` se puedan importar,
  - que los pesos estén presentes bajo `COMFY_DATA_DIR`,
  - que CUDA esté disponible.

Se invoca como subcomando `probe` de `worker.py` y también desde el arranque
de `app.py` para cachear `app.state.ml_capabilities` con TTL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comfy_studio.config import SecretRedactor
from comfy_studio.ml import ML_ENV_ALLOWLIST, WORKER_ENTRY

_COMFY_DATA_DIR_ENV = "COMFY_DATA_DIR"

# Pesos esperados por backend (rutas relativas a COMFY_DATA_DIR).
ML_WEIGHTS: dict[str, list[str]] = {
    "clip": ["models/clip/clip-vit-large-patch14"],
    "insightface": ["models/insightface/models/buffalo_l/det_10g.onnx"],
    "asr": ["models/faster_whisper/large-v3"],
    "diarize": ["models/funasr/campplus/campplus_cn_common.bin"],
}

# Módulos Python que debe poder importar el intérprete del worker por backend.
ML_IMPORTS: dict[str, list[str]] = {
    "clip": ["torch", "open_clip"],
    "insightface": ["insightface"],
    "asr": ["faster_whisper", "ctranslate2"],
    "diarize": ["funasr"],
}


@dataclass
class MlCapabilities:
    """Resultado de la detección de capacidades del entorno ML."""

    interpreter_ok: bool = False
    interpreter_path: str = ""
    cuda_available: bool = False
    backends: dict[str, bool] = field(default_factory=dict)
    weights_ok: dict[str, bool] = field(default_factory=dict)
    imports_ok: dict[str, bool] = field(default_factory=dict)
    probe_ok: bool = False
    reason: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def available_backends(self) -> list[str]:
        return sorted(b for b, ok in self.backends.items() if ok)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "interpreter_ok": self.interpreter_ok,
            "interpreter_path": self.interpreter_path,
            "cuda_available": self.cuda_available,
            "backends": self.backends,
            "weights_ok": self.weights_ok,
            "imports_ok": self.imports_ok,
            "probe_ok": self.probe_ok,
            "reason": self.reason,
            "available_backends": self.available_backends,
            "checked_at": self.checked_at,
        }


def _redact_stderr(redactor: SecretRedactor, text: str) -> str:
    if not text:
        return ""
    return redactor.redact(text.strip())


def _build_probe_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in sorted(ML_ENV_ALLOWLIST):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["COMFY_DATA_DIR"] = os.environ.get("COMFY_DATA_DIR", "")
    return env


def _check_imports(interpreter: str, modules: list[str]) -> tuple[bool, str]:
    """Ejecuta un script inline que intenta importar cada módulo."""
    code = "import sys\n" + "\n".join(f"import {m}\n" for m in modules)
    try:
        res = subprocess.run(
            [interpreter, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env=_build_probe_env(),
        )
        if res.returncode != 0:
            return False, res.stderr.strip() or "import falló sin stderr"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _check_weights(data_dir: Path, rel_paths: list[str]) -> tuple[bool, str]:
    missing = [p for p in rel_paths if not (data_dir / p).exists()]
    if missing:
        return False, "pesos ausentes: " + ", ".join(missing)
    return True, ""


def _check_cuda(interpreter: str) -> tuple[bool, str]:
    code = (
        "import torch\n"
        "print('1' if torch.cuda.is_available() else '0')\n"
    )
    try:
        res = subprocess.run(
            [interpreter, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env=_build_probe_env(),
        )
        if res.returncode != 0:
            return False, res.stderr.strip() or "cuda check falló"
        return res.stdout.strip() == "1", ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def probe_capabilities(
    interpreter: str | None = None,
    data_dir: Path | str | None = None,
    redactor: SecretRedactor | None = None,
) -> MlCapabilities:
    """Ejecuta la detección completa de capacidades ML.

    Nunca expone secretos: el entorno del hijo es allowlistado y todo stderr
    pasa por `redactor` antes de salir.
    """
    redactor = redactor or SecretRedactor()
    interp = interpreter or os.environ.get("STUDIO_ML_PYTHON_BIN") or "python3"
    dd = Path(data_dir) if data_dir else Path(os.environ.get(_COMFY_DATA_DIR_ENV, ""))

    caps = MlCapabilities(interpreter_path=interp)

    # 1. Intérprete ejecutable
    interp_bin = shutil.which(interp) if not os.path.isabs(interp) else interp
    if interp_bin and Path(interp_bin).is_file():
        caps.interpreter_ok = True
    else:
        caps.reason = f"intérprete no encontrado: {interp}"
        return caps

    # 2. CUDA
    caps.cuda_available, cuda_err = _check_cuda(interp_bin)

    # 3. Por backend: importes + pesos
    for backend in ["clip", "insightface", "asr", "diarize"]:
        mod_list = ML_IMPORTS.get(backend, [])
        w_list = ML_WEIGHTS.get(backend, [])

        im_ok, im_err = _check_imports(interp_bin, mod_list)
        w_ok, w_err = (
            _check_weights(dd, w_list)
            if dd and dd.is_dir()
            else (False, "COMFY_DATA_DIR no definido")
        )

        caps.imports_ok[backend] = im_ok
        caps.weights_ok[backend] = w_ok
        caps.backends[backend] = im_ok and w_ok

        if not im_ok:
            im_msg = f"{backend} imports: {_redact_stderr(redactor, im_err)}"
            caps.reason = (caps.reason + "; " if caps.reason else "") + im_msg
        if not w_ok:
            w_msg = f"{backend} weights: {w_err}"
            caps.reason = (caps.reason + "; " if caps.reason else "") + w_msg

    caps.probe_ok = all(caps.backends.values())
    return caps


def run_worker_probe(
    interpreter: str | None = None,
    timeout_s: float = 30.0,
    redactor: SecretRedactor | None = None,
) -> dict[str, Any]:
    """Ejecuta `worker.py probe` como subprocesso y devuelve su salida parseada.

    Útil para validar el worker aislado (CI smoke, `--dry-run`).
    """
    redactor = redactor or SecretRedactor()
    interp = interpreter or os.environ.get("STUDIO_ML_PYTHON_BIN") or "python3"
    worker_path = str(Path(__file__).resolve().parent / WORKER_ENTRY)
    env = _build_probe_env()

    try:
        res = subprocess.run(
            [interp, worker_path, "probe"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        tail = _redact_stderr(redactor, exc.stderr.decode() if exc.stderr else "")
        return {
            "ok": False,
            "subcommand": "probe",
            "error": "worker probe timeout",
            "exit_code": None,
            "stderr_tail": tail,
        }
    except Exception as exc:  # noqa: BLE001
        tail = _redact_stderr(redactor, str(exc))
        return {
            "ok": False,
            "subcommand": "probe",
            "error": f"{type(exc).__name__}: {exc}",
            "exit_code": None,
            "stderr_tail": tail,
        }

    if res.returncode != 0:
        return {
            "ok": False,
            "subcommand": "probe",
            "error": f"worker probe salió con rc={res.returncode}",
            "exit_code": res.returncode,
            "stderr_tail": _redact_stderr(redactor, res.stderr),
        }

    try:
        import json

        parsed = json.loads(res.stdout)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "subcommand": "probe",
            "error": f"salida del worker no es JSON válido: {exc}",
            "exit_code": res.returncode,
            "stderr_tail": _redact_stderr(redactor, res.stderr),
        }

    parsed.setdefault("ok", True)
    parsed.setdefault("subcommand", "probe")
    parsed.setdefault("exit_code", res.returncode)
    parsed["stderr_tail"] = _redact_stderr(redactor, res.stderr)
    return parsed
