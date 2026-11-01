"""Motor editor (M4-SCR-002, fase F1): ensambla clips en un reel.

Es el motor que FALTA y aparece dos veces en el pipeline:
  - F1/PRE: merge del storyboard borrador (varios clips cortos → prototipo).
  - POST: corte final (escenas de ~3/5/7s → reel de ~30s/1m) con transiciones.

Diseño stub-first y agnóstico del backend. El backend real (Remotion vs
OTIO+ffmpeg) se decide al cablear `_render_backend`; TODO lo demás —contrato,
validación, modo stub, tests— es idéntico en ambos casos. Así F1 avanza sin
bloquear en esa decisión humana.

Igual que `BlenderRunner`/`ml.runner`: subproceso aislado en el backend real,
respuesta tipada, `STUDIO_STUB_EDITOR=1` para tests sin binario, timeout y
redacción. Los verbos `list`/`install`/`verify` son triviales aquí (un editor
no tiene submotores dinámicos propios de momento) pero cumplen el contrato.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from comfy_studio.config import SecretRedactor
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
)
from comfy_studio.engines.base import Engine

# MP4 mínimo-ish para el modo stub (caja ftyp válida, suficiente para tests).
_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16

_TRANSITIONS = ("cut", "crossfade", "fade", "slide")

# Backend Remotion (elegido: hardware Blackwell + muchos cores → capaz, no ligero).
# El proyecto Remotion vive en la imagen; el agente solo pasa props y renderiza.
_REMOTION_PROJECT_ENV = "STUDIO_REMOTION_PROJECT"  # dir del proyecto Remotion horneado
_REMOTION_COMPOSITION = "Reel"  # id de composición en el proyecto
_REMOTION_DEFAULT_TIMEOUT_S = 1800.0


@dataclass(slots=True)
class Clip:
    """Un clip de entrada en la línea de tiempo."""

    path: Path
    duration_s: float = 0.0  # 0 = usar la duración nativa del archivo
    transition_in: str = "cut"

    def validate(self) -> None:
        if self.transition_in not in _TRANSITIONS:
            raise ValueError(f"transición desconocida: {self.transition_in!r}")
        if self.duration_s < 0 or self.duration_s > 600:
            raise ValueError("duration_s fuera de rango (0..600)")


@dataclass(slots=True)
class TimelineSpec:
    """Una línea de tiempo a ensamblar en un solo archivo de salida."""

    clips: list[Clip]
    output_path: Path
    width: int = 1080
    height: int = 1920  # vertical 9:16 por defecto (reel)
    fps: int = 30
    audio_path: Path | None = None
    timeout_s: float = 1800.0

    def validate(self) -> None:
        if not self.clips:
            raise ValueError("la línea de tiempo no puede estar vacía")
        if len(self.clips) > 512:
            raise ValueError("demasiados clips (máx 512)")
        if self.fps < 1 or self.fps > 120:
            raise ValueError("fps fuera de rango (1..120)")
        for dim in (self.width, self.height):
            if dim < 16 or dim > 8192:
                raise ValueError("width/height fuera de rango (16..8192)")
        for clip in self.clips:
            clip.validate()

    def total_duration_s(self) -> float:
        return float(sum(c.duration_s for c in self.clips))


@dataclass(slots=True)
class EditorResult:
    """Resultado de un ensamblado."""

    exit_code: int
    output: Path | None = None
    log_tail: str = ""
    duration_s: float = 0.0
    stubbed: bool = False
    outputs: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class EditorEngine(Engine):
    """Motor editor del estudio. Backend real se inyecta en `_render_backend`."""

    name = "editor"
    tier = EngineTier.WARM

    def __init__(self, backend: str | None = None) -> None:
        # backend: "remotion" | "otio_ffmpeg" | None (sin decidir → stub/declarado)
        self.backend = (backend or "").strip().lower() or None

    # -- contrato: 4 verbos --------------------------------------------------

    def list(self) -> EngineResult:
        """Capacidades del editor: transiciones y backend activo."""
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "transitions": list(_TRANSITIONS),
                "backend": self.backend or "unbound",
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        """El editor no instala submotores dinámicos aún; contrato cumplido."""
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="el motor editor no expone submotores instalables en F1",
        )

    def verify(self, submotor: str) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="verify",
            payload={"backend": self.backend or "unbound", "stub": self.is_stub()},
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        """Ensambla una línea de tiempo. `spec.job` describe la timeline."""
        try:
            timeline = self._parse_timeline(spec.job)
            timeline.validate()
        except (ValueError, KeyError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"timeline inválida: {exc}"
            )

        start = time.monotonic()
        if self.is_stub() or self.backend is None:
            res = self._run_stub(timeline)
        else:
            res = self._render_backend(timeline, spec)
        res.duration_s = time.monotonic() - start

        return EngineResult(
            ok=res.ok,
            engine=self.name,
            verb="run",
            payload={
                "output": str(res.output) if res.output else None,
                "duration_s": round(res.duration_s, 3),
                "clips": len(timeline.clips),
                "total_timeline_s": timeline.total_duration_s(),
            },
            exit_code=res.exit_code,
            stderr_tail=res.log_tail or None,
            simulated=res.stubbed,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _parse_timeline(job: dict) -> TimelineSpec:
        clips_raw = job["clips"]
        clips = [
            Clip(
                path=Path(c["path"]),
                duration_s=float(c.get("duration_s", 0.0)),
                transition_in=str(c.get("transition_in", "cut")),
            )
            for c in clips_raw
        ]
        return TimelineSpec(
            clips=clips,
            output_path=Path(job["output_path"]),
            width=int(job.get("width", 1080)),
            height=int(job.get("height", 1920)),
            fps=int(job.get("fps", 30)),
            audio_path=Path(job["audio_path"]) if job.get("audio_path") else None,
        )

    def _run_stub(self, timeline: TimelineSpec) -> EditorResult:
        """Escribe un MP4 mínimo + un sidecar JSON con la timeline resuelta."""
        out = timeline.output_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_STUB_MP4)
        sidecar = out.with_suffix(out.suffix + ".timeline.json")
        sidecar.write_text(
            json.dumps(
                {
                    "backend": self.backend or "unbound",
                    "width": timeline.width,
                    "height": timeline.height,
                    "fps": timeline.fps,
                    "clips": [
                        {"path": str(c.path), "duration_s": c.duration_s, "t": c.transition_in}
                        for c in timeline.clips
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return EditorResult(
            exit_code=0,
            output=out,
            log_tail="STUDIO_STUB_EDITOR: ensamblado simulado",
            stubbed=True,
            outputs=[out, sidecar],
        )

    def _render_backend(self, timeline: TimelineSpec, spec: EngineRunSpec) -> EditorResult:
        """Backend real Remotion (Node + Chromium headless).

        Construye las input-props de la timeline, invoca `npx remotion render`
        como subproceso AISLADO (igual que BlenderRunner: sin linkar, un crash
        no tumba el servidor), y recoge el MP4. `STUDIO_STUB_EDITOR=1` evita
        esta ruta en CI/tests. El proyecto Remotion está horneado en la imagen;
        su ruta se da en `STUDIO_REMOTION_PROJECT`.

        Solo Remotion está cableado: otro backend declarado falla tipado.
        """
        redactor = SecretRedactor()
        if self.backend != "remotion":
            return EditorResult(
                exit_code=1,
                output=None,
                log_tail=f"backend de editor '{self.backend}' no soportado; use 'remotion'",
            )

        project = (os.environ.get(_REMOTION_PROJECT_ENV) or "").strip()
        if not project or not Path(project).is_dir():
            return EditorResult(
                exit_code=1,
                output=None,
                log_tail=(
                    f"proyecto Remotion no encontrado ({_REMOTION_PROJECT_ENV}={project!r}); "
                    "debe estar horneado en la imagen"
                ),
            )

        npx = shutil.which("npx")
        if npx is None:
            return EditorResult(
                exit_code=1, output=None, log_tail="npx (Node) no está en PATH"
            )

        out = timeline.output_path
        out.parent.mkdir(parents=True, exist_ok=True)
        props_path = out.with_suffix(out.suffix + ".props.json")
        props_path.write_text(
            json.dumps(self._remotion_props(timeline), ensure_ascii=False),
            encoding="utf-8",
        )

        timeout = spec.timeout_s or timeline.timeout_s or _REMOTION_DEFAULT_TIMEOUT_S
        cmd = [
            npx,
            "remotion",
            "render",
            _REMOTION_COMPOSITION,
            str(out),
            f"--props={props_path}",
            f"--width={timeline.width}",
            f"--height={timeline.height}",
            f"--fps={timeline.fps}",
            "--log=error",
        ]
        start = time.monotonic()
        try:
            res = subprocess.run(
                cmd,
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return EditorResult(
                exit_code=124,
                output=None,
                log_tail=f"remotion render excedió el timeout ({timeout:.0f}s)",
                duration_s=time.monotonic() - start,
            )
        except OSError as exc:
            return EditorResult(
                exit_code=1,
                output=None,
                log_tail=redactor.redact(f"no se pudo lanzar remotion: {exc}"),
            )

        tail = redactor.redact((res.stderr or res.stdout or "")[-4096:])
        if res.returncode != 0 or not out.exists():
            return EditorResult(
                exit_code=res.returncode or 1,
                output=None,
                log_tail=tail or "remotion render falló sin salida",
                duration_s=time.monotonic() - start,
            )
        return EditorResult(
            exit_code=0,
            output=out,
            log_tail=tail,
            duration_s=time.monotonic() - start,
            outputs=[out],
        )

    @staticmethod
    def _remotion_props(timeline: TimelineSpec) -> dict:
        """Input-props que consume la composición Remotion 'Reel'."""
        return {
            "fps": timeline.fps,
            "width": timeline.width,
            "height": timeline.height,
            "audio": str(timeline.audio_path) if timeline.audio_path else None,
            "clips": [
                {
                    "src": str(c.path),
                    "durationInSeconds": c.duration_s,
                    "transitionIn": c.transition_in,
                }
                for c in timeline.clips
            ],
        }
