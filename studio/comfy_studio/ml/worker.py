"""Worker de ML ejecutable como subprocesso (M3.1).

Uso:
    python3 worker.py <subcomando> < payload.json > respuesta.json

Un JSON request en stdin, un JSON response en stdout. Todo el ruido de
diagnóstico va a stderr. El entorno del proceso hijo se construye con
allowlist (nunca `os.environ.copy()`): ningún secreto llega al worker.

Subcommandos:
  probe               -> detección de capacidades (intérprete, torch, etc.)
  clip_embed_text     -> embedding CLIP de texto (768-d)
  clip_embed_image    -> embedding CLIP de imagen (768-d)
  faces_detect        -> detección + embeddings faciales (512-d)
  asr_transcribe      -> transcripción ASR (faster-whisper)
  diarize             -> diarización cam++ + fusión con transcripción

Cada respuesta incluye `ok`, `subcommand`, y o bien `payload` (éxito) o
`error` + `stderr_tail` (fallo). Todo el contenido sensible se redacta.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# El worker intercambia JSON por stdin/stdout con `ensure_ascii=False`, así que
# los streams DEBEN ser UTF-8 en cualquier SO. En Windows el code page por
# defecto (cp1252) rompe el intercambio con caracteres acentuados (p.ej. 0xf3);
# el pod Linux ya es UTF-8. Reconfigurar aquí lo hace correcto en ambos.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    _reconfig = getattr(_stream, "reconfigure", None)
    if callable(_reconfig):
        try:
            _reconfig(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # stream ya cerrado o no reconfigurable
            pass

# Asegurar que el paquete comfy_studio sea importable cuando el worker se
# lanza con el intérprete del sistema (comfyui) que puede no tener el cwd
# en sys.path.
_HERE = os.path.dirname(os.path.abspath(__file__))           # .../comfy_studio/ml
_STUDIO_DIR = os.path.dirname(os.path.dirname(_HERE))         # .../studio
for _p in (_HERE, _STUDIO_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from comfy_studio import SIMULATED_ML_NOTICE  # noqa: E402
from comfy_studio.config import SecretRedactor  # noqa: E402
from comfy_studio.ml import (  # noqa: E402
    ML_ENV_ALLOWLIST,
    backends,  # noqa: E402
    probe_capabilities,
)

REDACTOR = SecretRedactor()


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _write_stdout(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _allowlist_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in sorted(ML_ENV_ALLOWLIST):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def _redact(value: Any) -> Any:
    return REDACTOR.redact_value(value)


def cmd_probe(_payload: dict[str, Any]) -> dict[str, Any]:
    caps = probe_capabilities()
    return {
        "ok": caps.probe_ok,
        "subcommand": "probe",
        "payload": caps.to_public_dict(),
        "error": caps.reason or None,
        "stderr_tail": None,
    }


def cmd_clip_embed_text(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", ""))
    real = backends.clip_embed_text(text)
    if real is None:
        _stderr("CLIP real no disponible; usando simulación determinista")
        return {
            "ok": True,
            "subcommand": "clip_embed_text",
            "payload": {
                "embedding": _redact([0.0] * 768),
                "dim": 768,
                "model": "openai/clip-vit-large-patch14",
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
            },
            "error": None,
            "stderr_tail": None,
        }
    return {
        "ok": True,
        "subcommand": "clip_embed_text",
        "payload": _redact(real),
        "error": None,
        "stderr_tail": None,
    }


def cmd_clip_embed_image(payload: dict[str, Any]) -> dict[str, Any]:
    image_path = str(payload.get("image_path", ""))
    real = backends.clip_embed_image(image_path) if image_path else None
    if real is None:
        _stderr("CLIP real no disponible; usando simulación determinista")
        return {
            "ok": True,
            "subcommand": "clip_embed_image",
            "payload": {
                "embedding": _redact([0.0] * 768),
                "dim": 768,
                "model": "openai/clip-vit-large-patch14",
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
            },
            "error": None,
            "stderr_tail": None,
        }
    return {
        "ok": True,
        "subcommand": "clip_embed_image",
        "payload": _redact(real),
        "error": None,
        "stderr_tail": None,
    }


def cmd_faces_detect(payload: dict[str, Any]) -> dict[str, Any]:
    image_path = payload.get("image_path") or None
    real = backends.faces_detect(image_path) if image_path else None
    if real is None:
        _stderr("InsightFace real no disponible; usando simulación determinista")
        return {
            "ok": True,
            "subcommand": "faces_detect",
            "payload": {
                "faces": [],
                "model_pack": "buffalo_l",
                "dim": 512,
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
            },
            "error": None,
            "stderr_tail": None,
        }
    return {
        "ok": True,
        "subcommand": "faces_detect",
        "payload": _redact(real),
        "error": None,
        "stderr_tail": None,
    }


def cmd_asr_transcribe(payload: dict[str, Any]) -> dict[str, Any]:
    audio_path = (
        payload.get("audio_path") or payload.get("asset_path") or payload.get("media_path")
    )
    language = payload.get("language") or None
    real = backends.asr_transcribe(audio_path, language=language) if audio_path else None
    if real is None:
        _stderr("faster-whisper real no disponible; usando simulación determinista")
        return {
            "ok": True,
            "subcommand": "asr_transcribe",
            "payload": {
                "text": "",
                "segments": [],
                "words": [],
                "language": "es",
                "backend": "faster-whisper",
                "model": "large-v3",
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
            },
            "error": None,
            "stderr_tail": None,
        }
    return {
        "ok": True,
        "subcommand": "asr_transcribe",
        "payload": _redact(real),
        "error": None,
        "stderr_tail": None,
    }


def cmd_diarize(payload: dict[str, Any]) -> dict[str, Any]:
    audio_path = payload.get("audio_path") or payload.get("media_path")
    real = backends.diarize(audio_path) if audio_path else None
    if real is None:
        _stderr("FunASR cam++ real no disponible; usando simulación determinista")
        return {
            "ok": True,
            "subcommand": "diarize",
            "payload": {
                "speakers": [],
                "turns": [],
                "by_speaker": {},
                "backend": "funasr_campplus",
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
            },
            "error": None,
            "stderr_tail": None,
        }
    return {
        "ok": True,
        "subcommand": "diarize",
        "payload": _redact(real),
        "error": None,
        "stderr_tail": None,
    }


_CMD_MAP: dict[str, callable] = {
    "probe": cmd_probe,
    "clip_embed_text": cmd_clip_embed_text,
    "clip_embed_image": cmd_clip_embed_image,
    "faces_detect": cmd_faces_detect,
    "asr_transcribe": cmd_asr_transcribe,
    "diarize": cmd_diarize,
}


def main() -> int:
    if len(sys.argv) != 2:
        _stderr("uso: worker.py <subcomando>")
        _write_stdout(
            {
                "ok": False,
                "subcommand": None,
                "error": "subcomando requerido",
                "stderr_tail": None,
            }
        )
        return 2

    subcommand = sys.argv[1]
    if subcommand not in _CMD_MAP:
        _stderr(f"subcomando desconocido: {subcommand}")
        _write_stdout(
            {
                "ok": False,
                "subcommand": subcommand,
                "error": f"subcomando desconocido: {subcommand}",
                "stderr_tail": None,
            }
        )
        return 2

    try:
        payload = _read_stdin()
    except Exception as exc:  # noqa: BLE001
        _stderr(f"stdin no es JSON válido: {exc}")
        _write_stdout(
            {
                "ok": False,
                "subcommand": subcommand,
                "error": f"stdin no es JSON válido: {exc}",
                "stderr_tail": None,
            }
        )
        return 2

    try:
        result = _CMD_MAP[subcommand](payload)
    except Exception as exc:  # noqa: BLE001
        _stderr(f"error en worker {subcommand}: {type(exc).__name__}: {exc}")
        _write_stdout(
            {
                "ok": False,
                "subcommand": subcommand,
                "error": f"{type(exc).__name__}: {exc}",
                "stderr_tail": _redact(str(exc)),
            }
        )
        return 1

    _write_stdout(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
