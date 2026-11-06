"""Motor de optimización de escena (plan Ultra, W11 / A13 / C20).

Envuelve ``comfy_studio.cinematography`` con el contrato de 4 verbos.

  - ``list``    → comprobaciones disponibles.
  - ``verify``  → valida el ``CameraPlan`` del ``submotor`` (sin ejecutar
                  ``check_composition``; solo ``CameraPlan.validate()``).
  - ``run``     → opera ``check_plan``: ejecuta ``check_composition`` y devuelve
                  el ``CompositionReport`` como dict.

Honestidad (A13, fail-closed): ``simulated`` solo es ``False`` cuando
``check_composition`` ejecutó realmente. Un plan inválido o con colisiones
produce ``ok=False`` tipificado; nunca lanza, nunca finge éxito.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.cinematography import (
    CameraError,
    CameraKeyframe,
    CameraPlan,
    Vec3,
    check_composition,
)
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
)
from comfy_studio.engines.base import Engine

_OPS = ("check_plan",)


class SceneOptimizerEngine(Engine):
    """Optimización de escena (warm)."""

    name = "scene-optimizer"
    tier = EngineTier.WARM

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "checks": [
                    "colisión con sujeto",
                    "colisión con obstáculos",
                    "visibilidad (FOV)",
                ],
                "stub": self.is_stub(),
            },
        )

    def verify(self, submotor: str) -> EngineResult:
        """Valida el CameraPlan (sin comprobar composición aún)."""
        try:
            plan = self._parse_plan(submotor)
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"plan inválido: {exc}",
            )
        try:
            plan.validate()
        except CameraError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"CameraPlan.validate() falló: {exc}",
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="verify",
            payload={"plan": plan.to_dict()},
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="scene-optimizer no requiere instalación de submotores",
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
        # parsear plan
        try:
            plan = self._parse_plan(job.get("plan", {}))
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"plan inválido: {exc}",
            )
        # parsear sujeto
        try:
            subject_pos = self._parse_vec3(
                job.get("subject_position", (0.0, 0.0, 0.0))
            )
            subject_radius = float(job.get("subject_radius", 0.5))
        except (TypeError, ValueError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"sujeto inválido: {exc}",
            )
        obstacles_raw = job.get("obstacles") or []
        obstacles: list[tuple[Vec3, float]] = []
        for obs in obstacles_raw:
            try:
                obstacles.append(
                    (self._parse_vec3(obs["position"]), float(obs["radius"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                return EngineResult(
                    ok=False,
                    engine=self.name,
                    verb="run",
                    error=f"obstáculo inválido: {exc}",
                )
        if self.is_stub():
            return self._stub_run(
                plan, subject_pos, subject_radius, obstacles,
                float(job.get("min_distance", 0.5)),
            )

        try:
            report = check_composition(
                plan,
                subject_position=subject_pos,
                subject_radius=subject_radius,
                min_distance=float(job.get("min_distance", 0.5)),
                obstacles=obstacles or None,
            )
        except CameraError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"check_composition falló: {exc}",
                simulated=False,
            )
        except Exception as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"check_composition lanzó: {type(exc).__name__}: {exc}",
                simulated=False,
            )
        return EngineResult(
            ok=report.ok,
            engine=self.name,
            verb="run",
            payload={"report": report.to_dict()},
            error=None if report.ok else "colisiones u oclusiones detectadas",
            simulated=False,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _parse_vec3(value: Any) -> Vec3:
        if (
            isinstance(value, (list, tuple))
            and len(value) == 3
            and all(isinstance(v, (int, float)) for v in value)
        ):
            return (float(value[0]), float(value[1]), float(value[2]))
        raise ValueError(f"Vec3 inválido: {value!r}")

    @staticmethod
    def _parse_plan(raw: Any) -> CameraPlan:
        if not isinstance(raw, dict):
            raise ValueError(f"plan debe ser dict, no {type(raw).__name__}")
        raw_kfs = raw.get("keyframes", [])
        keyframes = [
            CameraKeyframe(
                t=float(kf["t"]),
                position=SceneOptimizerEngine._parse_vec3(kf["position"]),
                target=SceneOptimizerEngine._parse_vec3(kf["target"]),
            )
            for kf in raw_kfs
        ]
        return CameraPlan(
            shot_id=str(raw.get("shot_id", "shot")),
            keyframes=keyframes,
            focal_mm=float(raw.get("focal_mm", 35.0)),
            sensor_mm=float(raw.get("sensor_mm", 36.0)),
            duration_s=float(raw.get("duration_s", 5.0)),
            factual=bool(raw.get("factual", False)),
        )

    def _stub_run(
        self,
        plan: CameraPlan,
        subject_pos: Vec3,
        subject_radius: float,
        obstacles: list[tuple[Vec3, float]],
        min_distance: float,
    ) -> EngineResult:
        try:
            plan.validate()
            report = check_composition(
                plan,
                subject_position=subject_pos,
                subject_radius=subject_radius,
                min_distance=min_distance,
                obstacles=obstacles or None,
            )
        except CameraError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"plan inválido (stub): {exc}",
                simulated=True,
            )
        except Exception as exc:
            # El contrato prohíbe lanzar también en modo stub.
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"check_composition lanzó (stub): {type(exc).__name__}: {exc}",
                simulated=True,
            )
        return EngineResult(
            ok=report.ok,
            engine=self.name,
            verb="run",
            payload={"report": report.to_dict()},
            error=None if report.ok else "colisiones u oclusiones detectadas (stub)",
            simulated=True,
            stderr_tail=f"STUDIO_STUB_{self.name.upper().replace('-', '_')}: check_plan simulado",
        )
