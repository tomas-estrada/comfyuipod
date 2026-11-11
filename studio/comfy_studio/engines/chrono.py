"""Motor de física/articulaciones proxy (plan Ultra, W12 / A14 / C18).

Envuelve ``comfy_studio.physics`` con el contrato de 4 verbos.

  - ``list``    → maniobras disponibles.
  - ``verify``  → valida la ``SimSpec`` del ``submotor`` (sin simular).
  - ``run``     → opera ``simulate``: ejecuta el solver cinemático CPU y devuelve
                  el ``SimResult`` como dict.

Honestidad (A14): este motor es SIEMPRE un proxy determinista CPU, NO el solver
Chrono/Blender real. El payload incluye ``is_proxy=True`` y ``proxy_note``.
``simulated`` solo es ``False`` cuando ``simulate()`` ejecutó realmente.
Nunca se reclama validación GPU/neural.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
)
from comfy_studio.engines.base import Engine
from comfy_studio.physics import (
    Joint,
    PhysicsError,
    SimSpec,
    simulate,
)

_OPS = ("simulate",)

_PROXY_NOTE = "proxy cinemático CPU; la fidelidad tras render neural NO se deduce de esto (A14)"


class ChronoEngine(Engine):
    """Física/articulaciones proxy (warm)."""

    name = "chrono"
    tier = EngineTier.WARM

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "maneuvers": ["free_fall", "joint_sweep", "joint_cycle"],
                "is_proxy": True,
                "proxy_note": _PROXY_NOTE,
                "stub": self.is_stub(),
            },
        )

    def verify(self, submotor: str) -> EngineResult:
        """Valida la SimSpec (parseada desde el submotor JSON)."""
        try:
            spec = self._parse_spec(submotor)
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"SimSpec inválida: {exc}",
            )
        try:
            # validate() implícito en simulate()
            simulate(spec)
        except PhysicsError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"SimSpec no válida: {exc}",
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="verify",
            payload={
                "spec_hash": spec.state_hash(),
                "frame_count": spec.frame_count(),
                "is_proxy": True,
                "proxy_note": _PROXY_NOTE,
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="chrono no requiere instalación de submotores",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        job = spec.job if isinstance(spec.job, dict) else {}
        op = str(job.get("op", ""))
        if op not in _OPS:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"op desconocida para {self.name}: {op!r}",
            )
        try:
            chrono_spec = self._parse_spec(job.get("spec", {}))
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"spec inválida: {exc}",
            )
        if self.is_stub():
            return self._stub_run(chrono_spec)

        try:
            result = simulate(chrono_spec)
        except PhysicsError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"simulate falló: {exc}",
                simulated=False,
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                **result.to_dict(),
                "simulated": False,
            },
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _parse_spec(raw: Any) -> SimSpec:
        if not isinstance(raw, dict):
            raise ValueError(f"spec debe ser dict, no {type(raw).__name__}")
        joints_raw = raw.get("joints") or []
        joints = []
        for j in joints_raw:
            joints.append(
                Joint(
                    name=str(j["name"]),
                    axis=str(j["axis"]),
                    min_deg=float(j["min_deg"]),
                    max_deg=float(j["max_deg"]),
                )
            )
        return SimSpec(
            maneuver=str(raw.get("maneuver", "free_fall")),
            seed=int(raw.get("seed", 0)),
            fps=int(raw.get("fps", 24)),
            duration_s=float(raw.get("duration_s", 2.0)),
            initial_height_m=float(raw.get("initial_height_m", 10.0)),
            joints=joints,
        )

    def _stub_run(self, chrono_spec: SimSpec) -> EngineResult:
        try:
            result = simulate(chrono_spec)
        except PhysicsError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"spec inválida (stub): {exc}",
                simulated=True,
            )
        except Exception as exc:
            # El contrato prohíbe lanzar también en modo stub.
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"simulate lanzó (stub): {type(exc).__name__}: {exc}",
                simulated=True,
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                **result.to_dict(),
                "simulated": True,
            },
            simulated=True,
            stderr_tail=f"STUDIO_STUB_{self.name.upper().replace('-', '_')}: simulate simulado",
        )
