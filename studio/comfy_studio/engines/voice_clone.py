"""Motor de clonación de voz / RVC (M5-SCR, fase 3).

Da al estudio su voz: clonar el timbre de una muestra y aplicarlo a una voz de
entrada (habla o canto). Es el backend de voz del que carecía el pod (STT ya
existe vía el worker ASR; la síntesis/clonación no estaba).

Dos operaciones (`spec.job['op']`):
  clone   → muestra de referencia + audio fuente → audio con el timbre clonado
            (RVC: retrieval-based voice conversion).
  tts     → texto + voz de referencia → habla sintetizada con ese timbre
            (síntesis guiada por la voz de referencia).

tier WARM. Stub-first (`STUDIO_STUB_VOICE_CLONE=1`) para CI. El backend real
(inferencia RVC) se cablea en `_run_rvc`; el modelo se hornea pinned en la fase
Dockerfile. Sin modelo disponible, falla TIPADO en vez de fingir.

Seguridad: sin secretos; audio de entrada/salida en rutas del proyecto; stderr
redactado; subproceso aislado con timeout (igual que ml/runner y blender).
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

_OPS = ("clone", "tts")
_STUB_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16

#: ruta del modelo RVC horneado (se fija en el Dockerfile); env override.
_MODEL_DIR_ENV = "STUDIO_VOICE_CLONE_MODEL_DIR"
#: binario/entrypoint de inferencia RVC (horneado); env override.
_RVC_BIN_ENV = "STUDIO_VOICE_CLONE_BIN"
_DEFAULT_TIMEOUT_S = 600.0


@dataclass(slots=True)
class VoiceSpec:
    op: str
    output_path: Path
    reference_audio: Path  # muestra del timbre a clonar
    source_audio: Path | None = None  # para op=clone: voz fuente a convertir
    text: str = ""  # para op=tts
    pitch_shift: float = 0.0
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"op debe ser uno de {_OPS}, no {self.op!r}")
        if self.op == "clone" and self.source_audio is None:
            raise ValueError("op=clone requiere 'source_audio'")
        if self.op == "tts" and not self.text.strip():
            raise ValueError("op=tts requiere 'text'")
        if not (-24.0 <= self.pitch_shift <= 24.0):
            raise ValueError("pitch_shift fuera de rango (-24..24 semitonos)")


class VoiceCloneEngine:
    """Motor de clonación de voz (RVC). Contrato de 4 verbos."""

    name = "voice_clone"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _model_available(self) -> bool:
        model_dir = (os.environ.get(_MODEL_DIR_ENV) or "").strip()
        return bool(model_dir) and Path(model_dir).is_dir()

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "model_available": self._model_available(),
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="el modelo RVC se hornea pinned en la imagen; no se instala en runtime",
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = self._model_available() or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"model_available": self._model_available(), "stub": self.is_stub()},
            error=None if ready else f"modelo RVC ausente ({_MODEL_DIR_ENV})",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            v = self._parse(spec.job)
            v.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            v.output_path.parent.mkdir(parents=True, exist_ok=True)
            v.output_path.write_bytes(_STUB_WAV)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "op": v.op,
                    "output": str(v.output_path),
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_VOICE_CLONE: voz simulada",
            )

        if not self._model_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"modelo RVC no disponible ({_MODEL_DIR_ENV}); "
                    "hornear el modelo pinned o usar STUDIO_STUB_VOICE_CLONE=1"
                ),
            )
        return self._run_rvc(v, start)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _parse(job: dict) -> VoiceSpec:
        return VoiceSpec(
            op=str(job.get("op", "")),
            output_path=Path(job["output_path"]),
            reference_audio=Path(job["reference_audio"]),
            source_audio=Path(job["source_audio"]) if job.get("source_audio") else None,
            text=str(job.get("text", "")),
            pitch_shift=float(job.get("pitch_shift", 0.0)),
        )

    def _run_rvc(self, v: VoiceSpec, start: float) -> EngineResult:
        """Inferencia RVC real vía el binario horneado, subproceso aislado."""
        redactor = SecretRedactor()
        model_dir = os.environ.get(_MODEL_DIR_ENV) or ""
        rvc_bin = (os.environ.get(_RVC_BIN_ENV) or shutil.which("rvc") or "").strip()
        if not rvc_bin:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"binario RVC no encontrado ({_RVC_BIN_ENV} o PATH)",
            )
        cmd = [
            rvc_bin,
            "--op",
            v.op,
            "--model-dir",
            model_dir,
            "--reference",
            str(v.reference_audio),
            "--output",
            str(v.output_path),
            "--pitch",
            str(v.pitch_shift),
        ]
        if v.op == "clone":
            cmd += ["--source", str(v.source_audio)]
        else:
            cmd += ["--text", v.text]
        v.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(  # noqa: S603 - args fijos, sin shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=v.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"RVC excedió el timeout ({v.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar RVC: {exc}"),
            )
        if res.returncode != 0 or not v.output_path.exists():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(
                    f"RVC falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "op": v.op,
                "output": str(v.output_path),
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
