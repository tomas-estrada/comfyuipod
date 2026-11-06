"""Motor de análisis local de referencias (plan Ultra, W06 / A09).

Envuelve ``comfy_studio.inspiration`` con el contrato de 4 verbos.

  - ``list``    → catálogo de conectores con su estado real.
  - ``verify``  → comprueba que no hay conector de pago activo y que ffprobe
                  está disponible.
  - ``run``     → opera ``analyze_reference``: construye un ``ReferenceAnalysis``
                  a partir de los cortes/beats/títulos/movimientos del ``job``,
                  deriva una ``EditableRecipe`` y la devuelve como dict.

Honestidad (A09): ``simulated`` solo es ``False`` cuando ``derive_recipe``
ejecutó realmente. Si falta ffmpeg/ffprobe, ``ok`` queda tipificado a
``False`` y ``simulated`` permanece ``False`` (no se finge éxito).
"""

from __future__ import annotations

from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
)
from comfy_studio.engines.base import Engine
from comfy_studio.inspiration import (
    Cut,
    ReferenceAnalysis,
    assert_no_paid_connector,
    connector_catalog,
    derive_recipe,
)
from comfy_studio.media_bins import find_ffprobe

_OPS = ("analyze_reference",)


class ReferenceLocalEngine(Engine):
    """Análisis local de referencias (resident)."""

    name = "reference-local"
    tier = EngineTier.RESIDENT

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        connectors = connector_catalog()
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "connectors": [
                    {
                        "name": c.name,
                        "status": str(c.status),
                        "note": c.note,
                    }
                    for c in connectors
                ],
                "ops": list(_OPS),
                "stub": self.is_stub(),
            },
        )

    def verify(self, submotor: str) -> EngineResult:
        """Verifica conectores y disponibilidad de ffprobe."""
        errors: list[str] = []
        # 1) ningún conector de pago activo.
        for connector in connector_catalog():
            try:
                assert_no_paid_connector(connector)
            except RuntimeError as exc:
                errors.append(str(exc))
        # 2) ffprobe disponible.
        if not find_ffprobe():
            errors.append("ffprobe no disponible en PATH")
        ok = not errors
        return EngineResult(
            ok=ok,
            engine=self.name,
            verb="verify",
            payload={"ffprobe_available": bool(find_ffprobe())},
            error="; ".join(errors) if errors else None,
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "reference-local no requiere instalación de submotores; "
                "usa ffmpeg/ffprobe y módulos Python ya presentes"
            ),
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        import time

        job = spec.job if isinstance(spec.job, dict) else {}
        op = str(job.get("op", ""))
        if op != "analyze_reference":
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"op desconocida para {self.name}: {op!r}",
            )
        if self.is_stub():
            return self._stub_run(job)

        # parsear la referencia
        try:
            analysis = self._build_analysis(job)
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"spec inválido: {exc}",
            )

        # ffprobe disponible?
        if not find_ffprobe():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error="ffprobe no disponible; no se puede analizar la referencia",
            )

        start = time.monotonic()
        try:
            recipe = derive_recipe(
                analysis,
                brief_shots=job.get("brief_shots"),
            )
        except Exception as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"derive_recipe falló: {type(exc).__name__}: {exc}",
            )

        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "recipe": recipe.to_dict(),
                "duration_s": round(time.monotonic() - start, 3),
                "simulated": False,
            },
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _build_analysis(job: dict) -> ReferenceAnalysis:
        cuts_raw = job.get("cuts") or []
        beats_raw = job.get("beats") or []
        titles_raw = job.get("titles") or []
        camera_moves_raw = job.get("camera_moves") or []
        cuts = [
            Cut(
                start_s=float(c.get("start_s", 0.0)),
                end_s=float(c.get("end_s", 0.0)),
            )
            for c in cuts_raw
        ]
        beats = [float(b) for b in beats_raw]
        titles = [dict(t) for t in titles_raw]
        camera_moves = [str(m) for m in camera_moves_raw]
        return ReferenceAnalysis(
            reference_id=str(job.get("reference_id", "unknown")),
            cuts=cuts,
            beats=beats,
            titles=titles,
            camera_moves=camera_moves,
        )

    def _stub_run(self, job: dict) -> EngineResult:
        import time

        try:
            analysis = self._build_analysis(job)
            recipe = derive_recipe(analysis, brief_shots=job.get("brief_shots"))
        except Exception as exc:
            # El contrato prohíbe lanzar: un job inválido en modo stub se tipifica.
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"spec inválida (stub): {type(exc).__name__}: {exc}",
                simulated=True,
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "recipe": recipe.to_dict(),
                "duration_s": round(time.monotonic() - 0.0, 3),
            },
            simulated=True,
            stderr_tail=(
                f"STUDIO_STUB_{self.name.upper().replace('-', '_')}:"
                " analyze_reference simulado"
            ),
        )
