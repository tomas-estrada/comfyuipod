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

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
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
from comfy_studio.engines.process import ProcessCancelled, run_cancellable

# MP4 mínimo-ish para el modo stub (caja ftyp válida, suficiente para tests).
_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16

_TRANSITIONS = ("cut", "crossfade", "fade", "slide")

# Backend Remotion (elegido: hardware Blackwell + muchos cores → capaz, no ligero).
# El proyecto Remotion vive en la imagen; el agente solo pasa props y renderiza.
_REMOTION_PROJECT_ENV = "STUDIO_REMOTION_PROJECT"  # dir del proyecto Remotion horneado
_REMOTION_BROWSER_ENV = "STUDIO_REMOTION_BROWSER"  # ejecutable Chromium opcional
_REMOTION_COMPOSITION = "Reel"  # id de composición en el proyecto
_REMOTION_DEFAULT_TIMEOUT_S = 1800.0


@dataclass(slots=True)
class Clip:
    """Un clip de entrada en la línea de tiempo."""

    path: Path
    duration_s: float = 0.0  # 0 = usar la duración nativa del archivo
    transition_in: str = "cut"
    transition_duration_s: float = 0.3

    def validate(self) -> None:
        if self.transition_in not in _TRANSITIONS:
            raise ValueError(f"transición desconocida: {self.transition_in!r}")
        if not math.isfinite(self.duration_s) or self.duration_s < 0 or self.duration_s > 600:
            raise ValueError("duration_s fuera de rango (0..600)")
        if (
            not math.isfinite(self.transition_duration_s)
            or self.transition_duration_s < 0
            or self.transition_duration_s > 5
        ):
            raise ValueError("transition_duration_s fuera de rango (0..5)")


@dataclass(slots=True)
class TextOverlay:
    """Título o caption temporizado, renderizado localmente por Remotion."""

    text: str
    start_s: float
    end_s: float
    position: str = "bottom"

    def validate(self) -> None:
        if not self.text.strip() or len(self.text) > 1000:
            raise ValueError("texto de overlay vacío o demasiado largo")
        if (
            not math.isfinite(self.start_s)
            or not math.isfinite(self.end_s)
            or self.start_s < 0
            or self.end_s <= self.start_s
        ):
            raise ValueError("rango de overlay inválido")
        if self.position not in {"top", "center", "bottom"}:
            raise ValueError("posición de overlay desconocida")


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
    titles: list[TextOverlay] = field(default_factory=list)
    captions: list[TextOverlay] = field(default_factory=list)

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
        duration = self.total_duration_s()
        for overlay in [*self.titles, *self.captions]:
            overlay.validate()
            if overlay.end_s > duration + 0.001:
                raise ValueError("overlay excede la duración de la línea de tiempo")

    def total_duration_s(self) -> float:
        duration = sum(c.duration_s for c in self.clips)
        for index, clip in enumerate(self.clips[1:], start=1):
            if clip.transition_in in {"crossfade", "slide"}:
                duration -= min(
                    clip.transition_duration_s,
                    clip.duration_s,
                    self.clips[index - 1].duration_s,
                )
        return float(duration)


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
        if self.is_stub():
            res = self._run_stub(timeline)
        elif self.backend is None:
            # Sin backend vinculado no se finge un ensamblado: el stub existe
            # para simular a propósito (STUDIO_STUB_EDITOR=1). Un MP4 mínimo
            # con ok=True aquí sería un éxito simulado silencioso.
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    "editor sin backend vinculado (unbound); vincula "
                    "backend='remotion' o usa STUDIO_STUB_EDITOR=1 para simular"
                ),
            )
        else:
            res = self._render_backend(timeline, spec)
        res.duration_s = time.monotonic() - start

        return EngineResult(
            ok=res.ok,
            engine=self.name,
            verb="run",
            payload={
                "output": str(res.output) if res.output else None,
                "cancelled": res.exit_code == 130,
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
                transition_duration_s=float(c.get("transition_duration_s", 0.3)),
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
            titles=[TextOverlay(**o) for o in job.get("titles", [])],
            captions=[TextOverlay(**o) for o in job.get("captions", [])],
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
                        {
                            "path": str(c.path),
                            "duration_s": c.duration_s,
                            "transition_in": c.transition_in,
                            "transition_duration_s": c.transition_duration_s,
                        }
                        for c in timeline.clips
                    ],
                    "titles": [
                        {
                            "text": overlay.text,
                            "start_s": overlay.start_s,
                            "end_s": overlay.end_s,
                            "position": overlay.position,
                        }
                        for overlay in timeline.titles
                    ],
                    "captions": [
                        {
                            "text": overlay.text,
                            "start_s": overlay.start_s,
                            "end_s": overlay.end_s,
                            "position": overlay.position,
                        }
                        for overlay in timeline.captions
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
        timeout = spec.timeout_s or timeline.timeout_s or _REMOTION_DEFAULT_TIMEOUT_S
        start = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="studio-remotion-") as public_dir:
                staged = self._stage_remotion_assets(timeline, Path(public_dir))
                props_path = Path(public_dir) / "props.json"
                props_path.write_text(
                    json.dumps(self._remotion_props(timeline, staged), ensure_ascii=False),
                    encoding="utf-8",
                )
                cmd = [
                    npx,
                    "remotion",
                    "render",
                    _REMOTION_COMPOSITION,
                    str(out),
                    f"--props={props_path}",
                    f"--public-dir={public_dir}",
                    f"--width={timeline.width}",
                    f"--height={timeline.height}",
                    f"--fps={timeline.fps}",
                    "--timeout=120000",
                    "--log=error",
                ]
                # Fixed caps keep the renderer from defaulting the decoded
                # video cache to half of all host memory. Preset values are
                # provided by the project route; generic callers get the
                # same conservative defaults.
                concurrency = int(spec.job.get("render_concurrency", 2))
                cache_bytes = int(spec.job.get("offthread_cache_bytes", 268_435_456))
                if not 1 <= concurrency <= 4 or not 16_777_216 <= cache_bytes <= 536_870_912:
                    return EditorResult(
                        exit_code=1,
                        output=None,
                        log_tail="límites de recursos Remotion fuera del rango permitido",
                    )
                cmd.extend(
                    [
                        f"--concurrency={concurrency}",
                        f"--offthreadvideo-cache-size-in-bytes={cache_bytes}",
                    ]
                )
                if spec.job.get("memory_efficient", True):
                    cmd.append("--disallow-parallel-encoding")
                browser = (os.environ.get(_REMOTION_BROWSER_ENV) or "").strip()
                if browser:
                    cmd.extend(["--browser-executable", browser])
                res = run_cancellable(
                    cmd,
                    cancel_event=spec.cancel_event,
                    cwd=project,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
        except ProcessCancelled:
            return EditorResult(
                exit_code=130,
                output=None,
                log_tail="remotion render cancelado",
                duration_s=time.monotonic() - start,
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
        except ValueError as exc:
            return EditorResult(
                exit_code=1,
                output=None,
                log_tail=redactor.redact(f"medios de Remotion inválidos: {exc}"),
                duration_s=time.monotonic() - start,
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
    def _stage_remotion_assets(timeline: TimelineSpec, public_dir: Path) -> dict[Path, str]:
        """Copia medios verificados al public-dir que Remotion sirve al navegador."""
        paths = [clip.path for clip in timeline.clips]
        if timeline.audio_path is not None:
            paths.append(timeline.audio_path)
        staged: dict[Path, str] = {}
        for source in paths:
            resolved = source.resolve(strict=True)
            if not resolved.is_file():
                raise ValueError(f"medio de Remotion no es archivo regular: {source}")
            digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
            name = f"asset-{digest}{resolved.suffix.lower()}"
            target = public_dir / name
            if not target.exists():
                shutil.copy2(resolved, target)
            staged[source] = name
        return staged

    @staticmethod
    def _remotion_props(timeline: TimelineSpec, staged: dict[Path, str] | None = None) -> dict:
        """Input-props que consume la composición Remotion 'Reel'."""
        resolve = (lambda path: staged[path]) if staged is not None else str
        return {
            "fps": timeline.fps,
            "width": timeline.width,
            "height": timeline.height,
            "audio": resolve(timeline.audio_path) if timeline.audio_path else None,
            "clips": [
                {
                    "src": resolve(c.path),
                    "durationInSeconds": c.duration_s,
                    "transitionIn": c.transition_in,
                    "transitionDurationSeconds": c.transition_duration_s,
                }
                for c in timeline.clips
            ],
            "titles": [
                {
                    "text": overlay.text,
                    "startInSeconds": overlay.start_s,
                    "endInSeconds": overlay.end_s,
                    "position": overlay.position,
                }
                for overlay in timeline.titles
            ],
            "captions": [
                {
                    "text": overlay.text,
                    "startInSeconds": overlay.start_s,
                    "endInSeconds": overlay.end_s,
                    "position": overlay.position,
                }
                for overlay in timeline.captions
            ],
        }
