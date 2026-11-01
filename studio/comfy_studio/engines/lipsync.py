"""Motor de lipsync (M5-SCR, fase 5).

Sincroniza los labios de un vídeo con una pista de audio (Wav2Lip-style). tier
WARM. Stub-first (`STUDIO_STUB_LIPSYNC=1`). Backend real en `_run_model` vía
binario horneado; modelo pinned en la fase Dockerfile. Sin modelo, falla
TIPADO. Subproceso aislado, timeout, stderr redactado.
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

_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
_MODEL_DIR_ENV = "STUDIO_LIPSYNC_MODEL_DIR"
_BIN_ENV = "STUDIO_LIPSYNC_BIN"
_DEFAULT_TIMEOUT_S = 900.0


@dataclass(slots=True)
class LipsyncSpec:
    input_video: Path
    input_audio: Path
    output_path: Path
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not str(self.input_video):
            raise ValueError("lipsync requiere 'input_video'")
        if not str(self.input_audio):
            raise ValueError("lipsync requiere 'input_audio'")


class LipsyncEngine:
    """Motor de sincronía labial. Contrato de 4 verbos."""

    name = "lipsync"
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
            payload={"model_available": self._model_available(), "stub": self.is_stub()},
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="el modelo de lipsync se hornea pinned; no se instala en runtime",
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = self._model_available() or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"model_available": self._model_available(), "stub": self.is_stub()},
            error=None if ready else f"modelo de lipsync ausente ({_MODEL_DIR_ENV})",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            ls = self._parse(spec.job)
            ls.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            ls.output_path.parent.mkdir(parents=True, exist_ok=True)
            ls.output_path.write_bytes(_STUB_MP4)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(ls.output_path),
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_LIPSYNC: lipsync simulado",
            )

        if not self._model_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"modelo de lipsync no disponible ({_MODEL_DIR_ENV}); "
                    "hornear el modelo pinned o usar STUDIO_STUB_LIPSYNC=1"
                ),
            )
        return self._run_model(ls, start)

    @staticmethod
    def _parse(job: dict) -> LipsyncSpec:
        return LipsyncSpec(
            input_video=Path(job["input_video"]),
            input_audio=Path(job["input_audio"]),
            output_path=Path(job["output_path"]),
        )

    def _run_model(self, ls: LipsyncSpec, start: float) -> EngineResult:
        redactor = SecretRedactor()
        bin_path = (os.environ.get(_BIN_ENV) or shutil.which("wav2lip") or "").strip()
        if not bin_path:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"binario de lipsync no encontrado ({_BIN_ENV} o PATH)",
            )
        cmd = [
            bin_path,
            "--model-dir",
            os.environ.get(_MODEL_DIR_ENV) or "",
            "--video",
            str(ls.input_video),
            "--audio",
            str(ls.input_audio),
            "--output",
            str(ls.output_path),
        ]
        ls.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=ls.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"lipsync excedió el timeout ({ls.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar lipsync: {exc}"),
            )
        if res.returncode != 0 or not ls.output_path.exists():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(
                    f"lipsync falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "output": str(ls.output_path),
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
