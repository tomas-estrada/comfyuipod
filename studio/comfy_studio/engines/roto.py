"""Motor de rotoscopia por texto / SAM 3 (M6-SCR, Bloque 2, motor `roto`).

La forma más agent-friendly de rotoscopia: el LLM da una FRASE NOMINAL ("el robot",
"los escombros") y SAM 3 detecta, segmenta y sigue TODAS las instancias en el vídeo,
sin que el agente invente cajas ni coordenadas (eso era SAM 2). Produce máscaras por
frame para composición (combinar con el motor `compositor`).

tier WARM. Stub-first (`STUDIO_STUB_ROTO=1`). Backend real `sam3` horneado en la
imagen bajo constraint torch-safe; los PESOS (`facebook/sam3`, HF-gated) se bajan en
runtime a `STUDIO_ROTO_MODEL_DIR` (aceptar licencia + token HF). Sin paquete o sin
pesos, falla TIPADO. Subproceso/inferencia aislada, timeout, stderr redactado.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

_MODEL_DIR_ENV = "STUDIO_ROTO_MODEL_DIR"  # dir con los pesos SAM 3 (runtime, HF-gated)
_DEFAULT_TIMEOUT_S = 1800.0


@dataclass(slots=True)
class RotoSpec:
    video_path: Path
    prompts: list[str]  # frases nominales: ["el robot", "los escombros"]
    output_dir: Path  # carpeta donde se escriben las máscaras por frame
    timeout_s: float = _DEFAULT_TIMEOUT_S
    _extra: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not str(self.video_path):
            raise ValueError("roto requiere 'video_path'")
        if not self.prompts or not all(str(p).strip() for p in self.prompts):
            raise ValueError("roto requiere 'prompts' (frases nominales no vacías)")


class RotoEngine:
    """Motor de rotoscopia por texto (SAM 3). Contrato de 4 verbos."""

    name = "roto"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _package_available(self) -> bool:
        import importlib.util

        return importlib.util.find_spec("sam3") is not None

    def _weights_available(self) -> bool:
        md = (os.environ.get(_MODEL_DIR_ENV) or "").strip()
        return bool(md) and Path(md).is_dir()

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "mode": "text-prompt",
                "package_available": self._package_available(),
                "weights_available": self._weights_available(),
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "SAM 3 se hornea pinned; los pesos (facebook/sam3, HF-gated) se bajan "
                "en runtime a STUDIO_ROTO_MODEL_DIR, no por este verbo"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = (self._package_available() and self._weights_available()) or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={
                "package": self._package_available(),
                "weights": self._weights_available(),
                "stub": self.is_stub(),
            },
            error=(
                None
                if ready
                else "SAM 3 no listo: falta el paquete o los pesos (STUDIO_ROTO_MODEL_DIR)"
            ),
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            r = self._parse(spec.job)
            r.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            r.output_dir.mkdir(parents=True, exist_ok=True)
            # Stub honesto: una máscara vacía por prompt, sin inventar segmentación.
            for i, _p in enumerate(r.prompts):
                (r.output_dir / f"mask_{i:02d}_00000.png").write_bytes(
                    b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
                )
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output_dir": str(r.output_dir),
                    "prompts": list(r.prompts),
                    "instances": 0,
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_ROTO: rotoscopia simulada (máscaras vacías)",
            )

        if not self._package_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    "paquete SAM 3 no disponible; hornear `sam3` en la imagen o usar "
                    "STUDIO_STUB_ROTO=1"
                ),
            )
        if not self._weights_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"pesos de SAM 3 ausentes ({_MODEL_DIR_ENV}); son HF-gated "
                    "(facebook/sam3): acepta la licencia y bájalos en runtime con tu token HF"
                ),
            )
        # El cableado de inferencia real (cargar el predictor SAM 3 y propagar las
        # frases nominales por el vídeo) se completa cuando los pesos estén en la
        # Blackwell; aquí el contrato ya es correcto y falla tipado sin ellos.
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="run",
            error="inferencia SAM 3 pendiente de validar con pesos reales en la GPU",
        )

    @staticmethod
    def _parse(job: dict) -> RotoSpec:
        prompts = job.get("prompts")
        if isinstance(prompts, str):
            prompts = [prompts]
        return RotoSpec(
            video_path=Path(job["video_path"]),
            prompts=[str(p) for p in (prompts or [])],
            output_dir=Path(job["output_dir"]),
        )
