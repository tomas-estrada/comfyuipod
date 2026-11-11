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


def _extract_loudnorm_metrics(output: str) -> tuple[float, float] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(output[index:])
            integrated = float(payload["output_i"])
            true_peak = float(payload["output_tp"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if math.isfinite(integrated) and math.isfinite(true_peak):
            return integrated, true_peak
    return None


@dataclass(slots=True)
class Clip:
    """Un clip de entrada en la línea de tiempo."""

    path: Path
    duration_s: float = 0.0  # 0 = usar la duración nativa del archivo
    start_s: float = 0.0
    transition_in: str = "cut"
    transition_duration_s: float = 0.3

    def validate(self) -> None:
        if self.transition_in not in _TRANSITIONS:
            raise ValueError(f"transición desconocida: {self.transition_in!r}")
        if not math.isfinite(self.duration_s) or self.duration_s < 0 or self.duration_s > 600:
            raise ValueError("duration_s fuera de rango (0..600)")
        if not math.isfinite(self.start_s) or self.start_s < 0 or self.start_s > 3600:
            raise ValueError("start_s fuera de rango (0..3600)")
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
    audio_paths: list[Path] = field(default_factory=list)
    audio_gains_db: list[float] = field(default_factory=list)
    safe_area_percent: float = 8.0
    color_space: str = "bt709"
    color_range: str = "limited"
    audio_loudness_lufs: float = -16.0
    true_peak_dbfs: float = -1.0
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
        if not math.isfinite(self.safe_area_percent) or not 0 <= self.safe_area_percent < 25:
            raise ValueError("safe_area_percent debe estar entre 0 y 25")
        if self.color_space != "bt709" or self.color_range not in {"limited", "full"}:
            raise ValueError("el renderer Remotion admite solo BT.709 y rango limited/full")
        if self.audio_gains_db and len(self.audio_gains_db) != len(self.audio_paths):
            raise ValueError("cada pista de audio debe tener una ganancia")
        if any(not math.isfinite(gain) or not -60 <= gain <= 12 for gain in self.audio_gains_db):
            raise ValueError("ganancia de audio fuera de rango (-60..12 dB)")
        if not math.isfinite(self.audio_loudness_lufs) or not -70 <= self.audio_loudness_lufs <= 0:
            raise ValueError("audio_loudness_lufs debe estar entre -70 y 0")
        if not math.isfinite(self.true_peak_dbfs) or not -20 <= self.true_peak_dbfs <= 0:
            raise ValueError("true_peak_dbfs debe estar entre -20 y 0")
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
    audio_loudness_lufs: float | None = None
    true_peak_dbfs: float | None = None

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
                "audio_loudness_lufs": res.audio_loudness_lufs,
                "true_peak_dbfs": res.true_peak_dbfs,
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
                start_s=float(c.get("start_s", 0.0)),
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
            audio_paths=[Path(path) for path in job.get("audio_paths", [])],
            audio_gains_db=[float(value) for value in job.get("audio_gains_db", [])],
            safe_area_percent=float(job.get("safe_area_percent", 8.0)),
            color_space=str(job.get("color_space", "bt709")),
            color_range=str(job.get("color_range", "limited")),
            audio_loudness_lufs=float(job.get("audio_loudness_lufs", -16.0)),
            true_peak_dbfs=float(job.get("true_peak_dbfs", -1.0)),
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
                            "start_s": c.start_s,
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
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return EditorResult(
                exit_code=1,
                output=None,
                log_tail="ffmpeg es requerido para fijar el rango y etiquetado BT.709",
                duration_s=time.monotonic() - start,
            )
        tagged = out.with_name(f".{out.stem}.color-policy{out.suffix}")
        output_range = "pc" if timeline.color_range == "full" else "tv"
        range_filter = f"scale=in_range=tv:out_range={output_range}"
        try:
            color_result = run_cancellable(
                [
                    ffmpeg,
                    "-y",
                    "-v",
                    "info",
                    "-i",
                    str(out),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-vf",
                    range_filter,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-x264-params",
                    "colorprim=bt709:transfer=bt709:colormatrix=bt709",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-colorspace",
                    "bt709",
                    "-color_range",
                    output_range,
                    "-af",
                    (
                        f"loudnorm=I={timeline.audio_loudness_lufs}:"
                        f"TP={timeline.true_peak_dbfs}:LRA=11:print_format=json"
                    ),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(tagged),
                ],
                cancel_event=spec.cancel_event,
                cwd=project,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, timeout - (time.monotonic() - start)),
            )
        except (ProcessCancelled, subprocess.TimeoutExpired):
            tagged.unlink(missing_ok=True)
            return EditorResult(
                exit_code=130 if spec.cancel_event and spec.cancel_event.is_set() else 124,
                output=None,
                log_tail="aplicación de política de color cancelada o vencida",
                duration_s=time.monotonic() - start,
            )
        if color_result.returncode != 0 or not tagged.is_file():
            tagged.unlink(missing_ok=True)
            return EditorResult(
                exit_code=color_result.returncode or 1,
                output=None,
                log_tail=redactor.redact(
                    (color_result.stderr or color_result.stdout or "")[-4096:]
                ) or "falló la aplicación de política de color",
                duration_s=time.monotonic() - start,
            )
        audio_loudness = None
        true_peak = None
        if timeline.audio_path is not None or timeline.audio_paths:
            metrics = _extract_loudnorm_metrics(color_result.stderr or "")
            if metrics is None:
                tagged.unlink(missing_ok=True)
                return EditorResult(
                    exit_code=1,
                    output=None,
                    log_tail=redactor.redact(
                        "FFmpeg no informó mediciones de loudness para las pistas de audio: "
                        + (color_result.stderr or color_result.stdout or "")[-2048:]
                    ),
                    duration_s=time.monotonic() - start,
                )
            audio_loudness, true_peak = metrics
            if (
                abs(audio_loudness - timeline.audio_loudness_lufs) > 1.0
                or true_peak > timeline.true_peak_dbfs + 0.5
            ):
                tagged.unlink(missing_ok=True)
                return EditorResult(
                    exit_code=1,
                    output=None,
                    log_tail=(
                        "el audio normalizado quedó fuera de la política: "
                        f"{audio_loudness:.2f} LUFS / {true_peak:.2f} dBFS"
                    ),
                    duration_s=time.monotonic() - start,
                )
        tagged.replace(out)
        return EditorResult(
            exit_code=0,
            output=out,
            log_tail=tail,
            duration_s=time.monotonic() - start,
            outputs=[out],
            audio_loudness_lufs=audio_loudness,
            true_peak_dbfs=true_peak,
        )

    @staticmethod
    def _stage_remotion_assets(timeline: TimelineSpec, public_dir: Path) -> dict[Path, str]:
        """Copia medios verificados al public-dir que Remotion sirve al navegador."""
        paths = [clip.path for clip in timeline.clips]
        paths.extend(timeline.audio_paths)
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
            "audioTracks": [
                {
                    "src": resolve(path),
                    "gainDb": timeline.audio_gains_db[index]
                    if timeline.audio_gains_db
                    else 0.0,
                }
                for index, path in enumerate(timeline.audio_paths)
            ],
            "safeAreaPercent": timeline.safe_area_percent,
            "colorSpace": timeline.color_space,
            "colorRange": timeline.color_range,
            "clips": [
                {
                    "src": resolve(c.path),
                    "durationInSeconds": c.duration_s,
                    "startFrom": round(c.start_s * timeline.fps),
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
