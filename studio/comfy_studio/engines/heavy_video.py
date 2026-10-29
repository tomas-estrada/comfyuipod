"""Motor de vídeo pesado (M4-SCR-002, F5): sube de nivel un render de Blender.

La etapa 5 del pipeline: el render limpio de Blender (geometría, cámara, física)
se pasa a un modelo de vídeo pesado (Minimax u otro que el agente determine) que
añade realismo/estilo que la rasterización no da. Es tier INSTALLABLE: no reside
en GPU — es un cliente de API externo, se usa por escena bajo tope de coste.

Stub-first y agnóstico del proveedor: el backend real se cablea en `_call_api`
según el proveedor elegido; TODO lo demás —contrato, validación, tope de coste,
modo stub, redacción— es idéntico. Así F5 avanza sin fijar proveedor.

Seguridad (AGENTS.md): la API key se lee de entorno en el momento de la llamada,
NUNCA se persiste ni se registra; todo log pasa por el redactor del pod.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

# Proveedores conocidos; el agente puede elegir "best" y resolver dinámicamente.
_PROVIDERS = ("minimax", "best", "custom")

# MP4 mínimo-ish para el stub.
_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16


@dataclass(slots=True)
class HeavyVideoSpec:
    """Un trabajo de level-up: vídeo de entrada → vídeo mejorado."""

    input_video: Path
    output_path: Path
    prompt: str = ""
    provider: str = "best"
    strength: float = 0.6  # cuánto reinterpreta el modelo (0..1)
    max_seconds: float = 7.0  # tope de duración por escena (reel-safe)
    cost_cap_usd: float = 1.0

    def validate(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(f"provider debe ser uno de {_PROVIDERS}, no {self.provider!r}")
        if not (0.0 <= self.strength <= 1.0):
            raise ValueError("strength fuera de rango (0..1)")
        if not (0.1 <= self.max_seconds <= 60.0):
            raise ValueError("max_seconds fuera de rango (0.1..60)")
        if self.cost_cap_usd <= 0:
            raise ValueError("cost_cap_usd debe ser > 0")


class HeavyVideoEngine:
    """Motor de vídeo pesado vía API. Contrato de 4 verbos, async en `run`."""

    name = "heavy_video"
    tier = EngineTier.INSTALLABLE

    #: env var con la key del proveedor (se lee al llamar, nunca se persiste).
    KEY_ENV = "STUDIO_HEAVY_VIDEO_KEY"

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={"providers": list(_PROVIDERS), "stub": self.is_stub()},
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        """Un cliente de API no instala submotores; contrato cumplido."""
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="heavy_video es un cliente de API; no expone submotores instalables",
        )

    def verify(self, submotor: str) -> EngineResult:
        """Confirma que hay credencial disponible (sin revelarla)."""
        has_key = bool((os.environ.get(self.KEY_ENV) or "").strip()) or self.is_stub()
        return EngineResult(
            ok=has_key,
            engine=self.name,
            verb="verify",
            payload={"has_credential": has_key, "stub": self.is_stub()},
            error=None if has_key else f"falta credencial {self.KEY_ENV}",
        )

    async def run(self, spec: EngineRunSpec) -> EngineResult:
        """Sube de nivel un vídeo. `spec.job` describe HeavyVideoSpec."""
        try:
            hv = self._parse(spec.job)
            hv.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            out = hv.output_path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_STUB_MP4)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(out),
                    "provider": hv.provider,
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_HEAVY_VIDEO: level-up simulado",
            )

        key = (os.environ.get(self.KEY_ENV) or "").strip()
        if not key:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"falta credencial {self.KEY_ENV}; no se llama al proveedor",
            )
        return await self._call_api(hv, key)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _parse(job: dict) -> HeavyVideoSpec:
        return HeavyVideoSpec(
            input_video=Path(job["input_video"]),
            output_path=Path(job["output_path"]),
            prompt=str(job.get("prompt", "")),
            provider=str(job.get("provider", "best")),
            strength=float(job.get("strength", 0.6)),
            max_seconds=float(job.get("max_seconds", 7.0)),
            cost_cap_usd=float(job.get("cost_cap_usd", 1.0)),
        )

    async def _call_api(self, hv: HeavyVideoSpec, key: str) -> EngineResult:
        """Backend real del proveedor. Cableado en F5+ tras elegir proveedor.

        `key` NO se registra ni se devuelve: solo se usa en la cabecera de la
        petición saliente a loopback-permitido/externo-permitido del proveedor.
        Hasta cablear, falla tipado en vez de fingir.
        """
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="run",
            error=(
                f"backend de vídeo pesado '{hv.provider}' aún no cableado; "
                "usar STUDIO_STUB_HEAVY_VIDEO=1 para tests"
            ),
        )
