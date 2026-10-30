"""Motor de generación de música (M5-SCR, fase 4).

Pista musical original para el reel a partir de un prompt (MusicGen / Stable
Audio). tier WARM. Stub-first (`STUDIO_STUB_MUSIC_GEN=1`). Backend real en
`_run_model` vía binario horneado; modelo pinned en la fase Dockerfile. Sin
modelo, falla TIPADO. Subproceso aislado, timeout, stderr redactado.
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

_STUB_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
_MODEL_DIR_ENV = "STUDIO_MUSIC_GEN_MODEL_DIR"
_BIN_ENV = "STUDIO_MUSIC_GEN_BIN"
_DEFAULT_TIMEOUT_S = 600.0


@dataclass(slots=True)
class MusicSpec:
    prompt: str
    output_path: Path
    duration_s: float = 15.0
    bpm: int = 0  # 0 = libre
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ValueError("music_gen requiere 'prompt'")
        if not (1.0 <= self.duration_s <= 300.0):
            raise ValueError("duration_s fuera de rango (1..300)")
        if self.bpm < 0 or self.bpm > 400:
            raise ValueError("bpm fuera de rango (0..400)")


class MusicGenEngine:
    """Motor de música generativa. Contrato de 4 verbos."""

    name = "music_gen"
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
            error="el modelo de música se hornea pinned; no se instala en runtime",
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = self._model_available() or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"model_available": self._model_available(), "stub": self.is_stub()},
            error=None if ready else f"modelo de música ausente ({_MODEL_DIR_ENV})",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            m = self._parse(spec.job)
            m.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            m.output_path.parent.mkdir(parents=True, exist_ok=True)
            m.output_path.write_bytes(_STUB_WAV)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(m.output_path),
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_MUSIC_GEN: música simulada",
            )

        if not self._model_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"modelo de música no disponible ({_MODEL_DIR_ENV}); "
                    "hornear el modelo pinned o usar STUDIO_STUB_MUSIC_GEN=1"
                ),
            )
        return self._run_model(m, start)

    @staticmethod
    def _parse(job: dict) -> MusicSpec:
        return MusicSpec(
            prompt=str(job.get("prompt", "")),
            output_path=Path(job["output_path"]),
            duration_s=float(job.get("duration_s", 15.0)),
            bpm=int(job.get("bpm", 0)),
        )

    def _run_model(self, m: MusicSpec, start: float) -> EngineResult:
        redactor = SecretRedactor()
        bin_path = (os.environ.get(_BIN_ENV) or shutil.which("musicgen") or "").strip()
        if not bin_path:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"binario de música no encontrado ({_BIN_ENV} o PATH)",
            )
        cmd = [
            bin_path,
            "--model-dir",
            os.environ.get(_MODEL_DIR_ENV) or "",
            "--prompt",
            m.prompt,
            "--duration",
            str(m.duration_s),
            "--output",
            str(m.output_path),
        ]
        if m.bpm:
            cmd += ["--bpm", str(m.bpm)]
        m.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(  # noqa: S603 - args fijos, sin shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=m.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"music_gen excedió el timeout ({m.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar music_gen: {exc}"),
            )
        if res.returncode != 0 or not m.output_path.exists():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(
                    f"music_gen falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "output": str(m.output_path),
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
