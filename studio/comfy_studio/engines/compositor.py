"""Motor de composición / Natron (M5-SCR, fase 10).

Compositing basado en nodos (Natron, el "Nuke" open-source): combina en un solo
frame/secuencia las salidas de varios motores — captura NeRF, render Blender,
vídeo IA — con capas, keying, grading. Es la pieza de "ensamblaje final" que
ffmpeg no puede hacer. tier WARM. Stub-first (`STUDIO_STUB_COMPOSITOR=1`).
Backend real vía `NatronRenderer` CLI con un proyecto .ntp; sin binario falla
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

_STUB_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_BIN_ENV = "STUDIO_COMPOSITOR_BIN"
_DEFAULT_TIMEOUT_S = 1800.0


@dataclass(slots=True)
class CompositeSpec:
    project_path: Path  # proyecto Natron .ntp con el grafo de nodos
    output_path: Path
    frame_start: int = 1
    frame_end: int = 1
    writer_node: str = "Write1"
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not str(self.project_path):
            raise ValueError("compositor requiere 'project_path' (.ntp)")
        if self.frame_start < 1 or self.frame_end < self.frame_start:
            raise ValueError("rango de frames inválido (1 <= start <= end)")
        if self.frame_end - self.frame_start > 100000:
            raise ValueError("demasiados frames (máx 100000)")


class CompositorEngine:
    """Motor de compositing (Natron). Contrato de 4 verbos."""

    name = "compositor"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _bin(self) -> str:
        return (os.environ.get(_BIN_ENV) or shutil.which("NatronRenderer") or "").strip()

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={"available": bool(self._bin()), "stub": self.is_stub()},
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="Natron se hornea pinned en la imagen; no se instala en runtime",
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = bool(self._bin()) or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"available": bool(self._bin()), "stub": self.is_stub()},
            error=None if ready else f"NatronRenderer ausente ({_BIN_ENV} o PATH)",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            comp = self._parse(spec.job)
            comp.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            comp.output_path.parent.mkdir(parents=True, exist_ok=True)
            comp.output_path.write_bytes(_STUB_PNG)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(comp.output_path),
                    "frames": comp.frame_end - comp.frame_start + 1,
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_COMPOSITOR: composite simulado",
            )

        bin_path = self._bin()
        if not bin_path:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"NatronRenderer no disponible ({_BIN_ENV} o PATH); "
                    "hornear Natron o usar STUDIO_STUB_COMPOSITOR=1"
                ),
            )
        return self._run_natron(comp, bin_path, start)

    @staticmethod
    def _parse(job: dict) -> CompositeSpec:
        return CompositeSpec(
            project_path=Path(job["project_path"]),
            output_path=Path(job["output_path"]),
            frame_start=int(job.get("frame_start", 1)),
            frame_end=int(job.get("frame_end", 1)),
            writer_node=str(job.get("writer_node", "Write1")),
        )

    def _run_natron(self, comp: CompositeSpec, bin_path: str, start: float) -> EngineResult:
        redactor = SecretRedactor()
        # NatronRenderer -w <Writer> <start>-<end> <project.ntp>
        cmd = [
            bin_path,
            "-w",
            comp.writer_node,
            f"{comp.frame_start}-{comp.frame_end}",
            str(comp.project_path),
        ]
        comp.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(  # noqa: S603 - args fijos, sin shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=comp.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"compositor excedió el timeout ({comp.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar Natron: {exc}"),
            )
        if res.returncode != 0:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(
                    f"Natron falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "output": str(comp.output_path),
                "frames": comp.frame_end - comp.frame_start + 1,
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
