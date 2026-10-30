"""Motor de super-resolución / upscaler (M5-SCR, fase 6).

Sube la resolución del render final (Real-ESRGAN para imagen, frame-a-frame +
ffmpeg para vídeo). tier WARM. Stub-first (`STUDIO_STUB_UPSCALER=1`). Backend
real en `_run_model` vía binario horneado; modelo pinned en la fase Dockerfile.
Sin modelo, falla TIPADO. Subproceso aislado, timeout, stderr redactado.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from comfy_studio.config import SecretRedactor
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

_STUB_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
_MODEL_DIR_ENV = "STUDIO_UPSCALER_MODEL_DIR"
_BIN_ENV = "STUDIO_UPSCALER_BIN"
# Nodo ComfyUI del upscaler de vídeo SeedVR2 (submotor on-the-run, no horneado).
_SEEDVR2_NODE = "ComfyUI-SeedVR2_VideoUpscaler"
_SEEDVR2_NODE_ENV = "STUDIO_UPSCALER_VIDEO_NODE"  # tipo de nodo que verifica object_info
_MODES = ("image", "video")
_SCALES = (2, 3, 4)
_DEFAULT_TIMEOUT_S = 900.0


@dataclass(slots=True)
class UpscaleSpec:
    input_path: Path
    output_path: Path
    scale: int = 4
    mode: str = "image"  # image = Real-ESRGAN (horneado); video = SeedVR2 (ComfyUI)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if self.scale not in _SCALES:
            raise ValueError(f"scale debe ser uno de {_SCALES}, no {self.scale!r}")
        if self.mode not in _MODES:
            raise ValueError(f"mode debe ser uno de {_MODES}, no {self.mode!r}")


class UpscalerEngine:
    """Motor de upscaling. Contrato de 4 verbos."""

    name = "upscaler"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _model_available(self) -> bool:
        md = (os.environ.get(_MODEL_DIR_ENV) or "").strip()
        return bool(md) and Path(md).is_dir()

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "scales": list(_SCALES),
                "modes": list(_MODES),
                "video_submotor": os.environ.get(_SEEDVR2_NODE_ENV) or _SEEDVR2_NODE,
                "model_available": self._model_available(),
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="el modelo de upscaler se hornea pinned; no se instala en runtime",
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = self._model_available() or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"model_available": self._model_available(), "stub": self.is_stub()},
            error=None if ready else f"modelo de upscaler ausente ({_MODEL_DIR_ENV})",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            u = self._parse(spec.job)
            u.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            u.output_path.parent.mkdir(parents=True, exist_ok=True)
            u.output_path.write_bytes(_STUB_MP4 if u.mode == "video" else _STUB_PNG)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(u.output_path),
                    "scale": u.scale,
                    "mode": u.mode,
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail=f"STUDIO_STUB_UPSCALER: upscale {u.mode} simulado",
            )

        # Modo vídeo = SeedVR2, upscale temporalmente consistente (sin flicker).
        # Es un SUBMOTOR de ComfyUI (nodo ComfyUI-SeedVR2_VideoUpscaler, Apache) +
        # pesos: NO se hornea (traería torch deps que clavarían el pod). Se instala
        # on-the-run vía ComfyUI-Manager. Aquí falla TIPADO con remediación si el
        # nodo no está; el cableado real al grafo ComfyUI llega en una fase posterior.
        if u.mode == "video":
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    "upscale de vídeo (SeedVR2) es un submotor de ComfyUI aún no "
                    f"instalado; instala el nodo {_SEEDVR2_NODE} vía ComfyUI-Manager "
                    "(requiere STUDIO_DYNAMIC=1) o usa STUDIO_STUB_UPSCALER=1. "
                    "Para imagen frame-a-frame usa mode='image' (Real-ESRGAN horneado)."
                ),
            )

        if not self._model_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"modelo de upscaler no disponible ({_MODEL_DIR_ENV}); "
                    "hornear el modelo pinned o usar STUDIO_STUB_UPSCALER=1"
                ),
            )
        return self._run_model(u, start)

    @staticmethod
    def _parse(job: dict) -> UpscaleSpec:
        return UpscaleSpec(
            input_path=Path(job["input_path"]),
            output_path=Path(job["output_path"]),
            scale=int(job.get("scale", 4)),
            mode=str(job.get("mode", "image")),
        )

    def _run_model(self, u: UpscaleSpec, start: float) -> EngineResult:
        redactor = SecretRedactor()
        bin_path = (
            os.environ.get(_BIN_ENV) or shutil.which("realesrgan-ncnn-vulkan") or ""
        ).strip()
        if not bin_path:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"binario de upscaler no encontrado ({_BIN_ENV} o PATH)",
            )
        cmd = [
            bin_path,
            "-i",
            str(u.input_path),
            "-o",
            str(u.output_path),
            "-s",
            str(u.scale),
            "-m",
            os.environ.get(_MODEL_DIR_ENV) or "",
        ]
        u.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(  # noqa: S603 - args fijos, sin shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=u.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"upscaler excedió el timeout ({u.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar upscaler: {exc}"),
            )
        if res.returncode != 0 or not u.output_path.exists():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(
                    f"upscaler falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "output": str(u.output_path),
                "scale": u.scale,
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
