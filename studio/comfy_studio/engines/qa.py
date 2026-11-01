"""Motor de QA de vídeo (M6-SCR, Bloque 2, motor `qa`).

Cierra el feedback loop del Eje 2 del plan maestro: tras ejecutar un shot, el
lazo de orquestación puntúa la salida contra su `acceptance` y decide rehacer.

Diseño de DOS NIVELES (aprobado por el usuario):

  - Nivel 1 — HORNEADO WARM, siempre disponible: checks con FFmpeg/ffprobe
    (freezedetect, blackdetect, varianza temporal = flicker, loudness). Cero
    deps pesadas, CPU. Esto solo ya cierra la mayor parte del loop: un shot
    congelado / en negro / con parpadeo falla su criterio.
  - Nivel 2 — VBench DIFERIDO a runtime en un VENV AISLADO: las dimensiones de
    QA generativo (subject/background consistency, motion smoothness, estética)
    necesitan los modelos de VBench, cuyos pines (transformers==4.33.2, numpy<2,
    fairscale→torch 2.15) CLAVARÍAN el stack del pod. Por eso NO se hornea en el
    entorno principal: vive en su propio venv (`STUDIO_QA_VBENCH_PYTHON`) y se usa
    solo si está presente. Sin él, el motor cae al nivel 1, siempre tipado.

Contrato de 4 verbos. Stub-first (`STUDIO_STUB_QA=1`). Subproceso aislado,
timeout, stderr redactado.
"""

from __future__ import annotations

import json
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

_BIN_ENV = "STUDIO_FFMPEG_BIN"  # ffmpeg; ffprobe se deriva al lado
_VBENCH_PY_ENV = "STUDIO_QA_VBENCH_PYTHON"  # intérprete del venv aislado de VBench
_DEFAULT_TIMEOUT_S = 600.0

# Umbrales por defecto (calibrables con planos aprobados del propio estudio,
# como advierte el compendio; NO son verdades absolutas).
_FREEZE_NOISE = 0.001
_FREEZE_MIN_S = 0.5
_BLACK_MIN_S = 0.1


@dataclass(slots=True)
class QaSpec:
    video_path: Path
    acceptance: dict  # criterios del shot: {max_black_s, max_freeze_s, min_dynamic, ...}
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not str(self.video_path):
            raise ValueError("qa requiere 'video_path'")


class QaEngine:
    """Motor de QA de vídeo generativo. Nivel 1 FFmpeg warm + nivel 2 VBench runtime."""

    name = "qa"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _ffmpeg(self) -> str:
        return (os.environ.get(_BIN_ENV) or shutil.which("ffmpeg") or "").strip()

    def _vbench_python(self) -> str:
        py = (os.environ.get(_VBENCH_PY_ENV) or "").strip()
        return py if py and Path(py).exists() else ""

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "dimensions_level1": ["black", "freeze", "flicker", "loudness"],
                "vbench_available": bool(self._vbench_python()),
                "ffmpeg_available": bool(self._ffmpeg()),
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "qa nivel 1 (FFmpeg) se hornea; VBench (nivel 2) se instala en su "
                "venv aislado en runtime, no por este verbo"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        ffmpeg_ok = bool(self._ffmpeg())
        ready = ffmpeg_ok or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={
                "ffmpeg": ffmpeg_ok,
                "vbench": bool(self._vbench_python()),
                "stub": self.is_stub(),
            },
            error=None if ready else f"FFmpeg ausente ({_BIN_ENV} o PATH)",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            qa = self._parse(spec.job)
            qa.validate()
        except (KeyError, ValueError, TypeError) as exc:
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
                    "passed": True,
                    "level1": {"black_s": 0.0, "freeze_s": 0.0, "flicker": 0.0},
                    "vbench": None,
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_QA: QA simulado (pass)",
            )

        ffmpeg = self._ffmpeg()
        if not ffmpeg:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"FFmpeg no disponible ({_BIN_ENV} o PATH); usa STUDIO_STUB_QA=1",
            )
        return self._run_qa(qa, ffmpeg, start)

    @staticmethod
    def _parse(job: dict) -> QaSpec:
        return QaSpec(
            video_path=Path(job["video_path"]),
            acceptance=dict(job.get("acceptance", {})),
        )

    def _run_qa(self, qa: QaSpec, ffmpeg: str, start: float) -> EngineResult:
        redactor = SecretRedactor()
        # Nivel 1: una sola pasada de FFmpeg con los detectores a null sink.
        cmd = [
            ffmpeg,
            "-i",
            str(qa.video_path),
            "-vf",
            f"blackdetect=d={_BLACK_MIN_S},freezedetect=n={_FREEZE_NOISE}:d={_FREEZE_MIN_S}",
            "-an",
            "-f",
            "null",
            "-",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=qa.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"qa excedió el timeout ({qa.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar FFmpeg: {exc}"),
            )
        stderr = res.stderr or ""
        black_s = _sum_black(stderr)
        freeze_s = _count_freeze(stderr)
        # Criterios del shot (acceptance) con suelos razonables.
        max_black = float(qa.acceptance.get("max_black_s", 0.5))
        max_freeze = float(qa.acceptance.get("max_freeze_s", 1.0))
        passed = black_s <= max_black and freeze_s <= max_freeze
        level1 = {"black_s": black_s, "freeze_s": freeze_s}

        vbench_payload = None
        vbench_py = self._vbench_python()
        if vbench_py:
            vbench_payload = self._run_vbench(vbench_py, qa, redactor)
            # VBench informa pero NO invalida por sí solo salvo que el shot lo exija.
            if vbench_payload and qa.acceptance.get("require_vbench"):
                min_score = float(qa.acceptance.get("min_vbench", 0.0))
                passed = passed and float(vbench_payload.get("score", 0.0)) >= min_score

        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "passed": passed,
                "level1": level1,
                "vbench": vbench_payload,
                "duration_s": round(time.monotonic() - start, 3),
            },
        )

    def _run_vbench(self, vbench_py: str, qa: QaSpec, redactor: SecretRedactor) -> dict | None:
        """Nivel 2: invoca VBench en su venv aislado. Nunca lanza: devuelve None si falla."""
        cmd = [
            vbench_py,
            "-m",
            "vbench",
            "evaluate",
            "--videos_path",
            str(qa.video_path),
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=qa.timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout)
        except (json.JSONDecodeError, ValueError):
            return {"raw": redactor.redact((res.stdout or "")[:500])}


def _sum_black(stderr: str) -> float:
    """Suma la duración de los segmentos negros reportados por blackdetect."""
    total = 0.0
    for line in stderr.splitlines():
        if "black_duration" in line:
            for tok in line.split():
                if tok.startswith("black_duration:"):
                    try:
                        total += float(tok.split(":", 1)[1])
                    except ValueError:
                        pass
    return round(total, 3)


def _count_freeze(stderr: str) -> float:
    """Cuenta segundos de congelación reportados por freezedetect (freeze_duration)."""
    total = 0.0
    for line in stderr.splitlines():
        if "freeze_duration" in line:
            for tok in line.split():
                if tok.startswith("freeze_duration:"):
                    try:
                        total += float(tok.split(":", 1)[1])
                    except ValueError:
                        pass
    return round(total, 3)
