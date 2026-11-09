"""Motor FFmpeg (transcodificación/inspección local).

Cierra el hueco del roster: `ffmpeg` estaba declarado como WARM+cableado pero
sin clase de motor (15 clases para 16 entradas). Contrato de 4 verbos,
stub-first (`STUDIO_STUB_FFMPEG=1`), subproceso aislado sin `shell=True`,
timeout y stderr redactado. Sin GPU, sin red: todo es binario local.

- `list` informa disponibilidad (binario, versión) sin exigir nada.
- `install` se rechaza siempre de forma tipada: el binario se hornea, no hay
  submotores instalables por este verbo.
- `verify` exige binario ejecutable (`ffmpeg -version` con rc 0).
- `run` ejecuta una transcodificación declarada `{inputs, output, args?}`.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from comfy_studio.config import SecretRedactor
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)
from comfy_studio.engines.base import Engine

_DEFAULT_TIMEOUT_S = 600.0


def _find_binary() -> str:
    from comfy_studio.media_bins import find_ffmpeg

    return find_ffmpeg() or ""


def parse_version_line(first_line: str) -> str | None:
    """Extrae `6.1.2` de `ffmpeg version 6.1.2 Copyright ...`; None si no casa."""
    text = (first_line or "").strip()
    prefix = "ffmpeg version "
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix):].split()
    return rest[0] if rest else None


def ffmpeg_version(binary: str, *, timeout_s: float = 15.0) -> str | None:
    """Versión del binario (`None` si no ejecuta). Solo proceso local."""
    try:
        res = subprocess.run(
            [binary, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    first = (res.stdout or "").splitlines()
    return parse_version_line(first[0]) if first else None


@dataclass(slots=True)
class TranscodeSpec:
    """Transcodificación declarada: entradas existentes + salida + args extra."""

    inputs: list[Path] = field(default_factory=list)
    output: Path = Path("")
    args: list[str] = field(default_factory=list)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not self.inputs:
            raise ValueError("ffmpeg run requiere 'inputs' no vacío")
        for path in self.inputs:
            if not path.is_file():
                raise ValueError(f"entrada inexistente: {path}")
        if not str(self.output):
            raise ValueError("ffmpeg run requiere 'output'")
        if not all(isinstance(a, str) for a in self.args):
            raise ValueError("'args' debe ser lista de cadenas (sin shell)")


class FfmpegEngine(Engine):
    """Motor FFmpeg local. Contrato de 4 verbos."""

    name = "ffmpeg"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def list(self) -> EngineResult:
        binary = _find_binary()
        version = ffmpeg_version(binary) if binary else None
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "available": bool(binary),
                "binary": binary,
                "version": version,
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "ffmpeg se hornea en la imagen y no tiene submotores instalables; "
                f"nada que instalar para {submotor!r}"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        if submotor:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"ffmpeg no tiene submotores; no verificable: {submotor!r}",
            )
        if self.is_stub():
            return EngineResult(
                ok=True, engine=self.name, verb="verify",
                payload={"available": True, "stub": True},
            )
        binary = _find_binary()
        version = ffmpeg_version(binary) if binary else None
        ready = version is not None
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"available": bool(binary), "binary": binary, "version": version},
            error=None if ready else "FFmpeg ausente o no ejecutable (STUDIO_FFMPEG_BIN o PATH)",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            job = spec.job if isinstance(spec.job, dict) else {}
            parsed = TranscodeSpec(
                inputs=[Path(p) for p in job.get("inputs", [])],
                output=Path(job.get("output", "")),
                args=list(job.get("args", [])),
                timeout_s=float(job.get("timeout_s", _DEFAULT_TIMEOUT_S)),
            )
            parsed.validate()
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )
        start = time.monotonic()
        if self.is_stub():
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(parsed.output),
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_FFMPEG: transcodificación simulada",
            )
        binary = _find_binary()
        if not binary:
            return EngineResult(
                ok=False, engine=self.name, verb="run",
                error="FFmpeg no disponible (STUDIO_FFMPEG_BIN o PATH); usa STUDIO_STUB_FFMPEG=1",
            )
        return self._run_transcode(binary, parsed, start)

    def _run_transcode(
        self, binary: str, parsed: TranscodeSpec, start: float
    ) -> EngineResult:
        # Igual que en upscaler/heavy_video: con secretos literales del entorno,
        # no un redactor pelado sin ninguno.
        redactor = SecretRedactor.from_env()
        cmd = [binary, "-y"]
        for path in parsed.inputs:
            cmd += ["-i", str(path)]
        cmd += parsed.args
        cmd.append(str(parsed.output))
        try:
            parsed.output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run",
                error=f"no se pudo crear el directorio de salida: {exc}",
            )
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=parsed.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False, engine=self.name, verb="run",
                error=f"ffmpeg excedió el timeout ({parsed.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run",
                error=redactor.redact(f"no se pudo lanzar FFmpeg: {exc}"),
            )
        if res.returncode != 0:
            return EngineResult(
                ok=False, engine=self.name, verb="run",
                exit_code=res.returncode,
                stderr_tail=redactor.redact((res.stderr or "")[-2000:]),
                error=f"ffmpeg falló con código {res.returncode}",
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "output": str(parsed.output),
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
