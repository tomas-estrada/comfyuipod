"""Inspiración activa: referencia → receta editable (plan Ultra, W06 / A09).

W06 pide convertir una referencia autorizada (cortes/beat/cámara/texto/sonido) en
una **receta editable** adaptada a los assets propios, con conectores gratuitos
opcionales y una política de selección por relevancia. El invariante (A09): no
depender de reverse cloud, compras, descargas no autorizadas ni cuentas nuevas;
el modo sin red funciona sobre la biblioteca propia; un conector sin credenciales
queda `blocked`, no inventa datos; y el análisis distingue **observado /
inferido / adaptado** sin prometer viralidad.

Este módulo implementa el análisis LOCAL sobre una referencia ya disponible en la
biblioteca propia (metadatos de cortes, beats, títulos OCR y movimiento que la
ingesta/perception ya extrajeron): produce un `ReferenceAnalysis` y lo transforma
en una `EditableRecipe`. No llama a ninguna red; los conectores externos se
declaran como capacidades opcionales con su estado real.

Puro CPU y determinista: no ejecuta modelos ni descarga. La extracción fina real
(OCR/flow) la hacen los backends con pesos; aquí se consume lo ya extraído y se
razona la receta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Origin(StrEnum):
    """De dónde sale cada dato de la receta derivada."""

    OBSERVED = "observed"   # medido de la referencia real
    INFERRED = "inferred"   # deducido por el análisis
    ADAPTED = "adapted"     # ajustado al brief/assets propios


class ConnectorStatus(StrEnum):
    AVAILABLE = "available"
    #: sin credenciales o cuota agotada: opcional, no inventa datos.
    BLOCKED = "blocked"
    #: de pago / cuenta no concedida: nunca se invoca.
    FORBIDDEN_PAID = "forbidden_paid"


@dataclass(slots=True)
class Cut:
    """Un corte detectado en la referencia (tiempo en segundos)."""

    start_s: float
    end_s: float
    origin: Origin = Origin.OBSERVED

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(slots=True)
class ReferenceAnalysis:
    """Lo que el análisis LOCAL extrae de una referencia autorizada."""

    reference_id: str
    #: shot map: cortes con sus tiempos.
    cuts: list[Cut] = field(default_factory=list)
    #: beat map: instantes (s) de acento rítmico.
    beats: list[float] = field(default_factory=list)
    #: títulos/textos detectados por OCR, con su origen.
    titles: list[dict[str, Any]] = field(default_factory=list)
    #: movimiento de cámara aproximado por corte (pan/tilt/zoom/static).
    camera_moves: list[str] = field(default_factory=list)
    #: de dónde salió la referencia (biblioteca propia / conector permitido).
    source: str = "own_library"

    def beat_interval_s(self) -> float | None:
        """Intervalo medio entre beats (tempo), o None si <2 beats."""
        if len(self.beats) < 2:
            return None
        deltas = [b - a for a, b in zip(self.beats, self.beats[1:], strict=False)]
        return round(sum(deltas) / len(deltas), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "source": self.source,
            "cuts": [
                {"start_s": c.start_s, "end_s": c.end_s, "origin": str(c.origin)}
                for c in self.cuts
            ],
            "beats": list(self.beats),
            "beat_interval_s": self.beat_interval_s(),
            "titles": list(self.titles),
            "camera_moves": list(self.camera_moves),
        }


@dataclass(slots=True)
class RecipeStep:
    """Un paso de la receta editable derivada, con su origen declarado."""

    kind: str          # "shot", "title", "beat_sync", "camera"
    detail: dict[str, Any]
    origin: Origin

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": dict(self.detail), "origin": str(self.origin)}


@dataclass(slots=True)
class EditableRecipe:
    """Receta editable derivada de una referencia y adaptada al brief."""

    reference_id: str
    steps: list[RecipeStep] = field(default_factory=list)
    notes: str = ""

    def by_origin(self, origin: Origin) -> list[RecipeStep]:
        return [s for s in self.steps if s.origin is origin]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "steps": [s.to_dict() for s in self.steps],
            "origin_summary": {
                "observed": len(self.by_origin(Origin.OBSERVED)),
                "inferred": len(self.by_origin(Origin.INFERRED)),
                "adapted": len(self.by_origin(Origin.ADAPTED)),
            },
            "notes": self.notes,
            # A09: no se promete viralidad; el análisis es descriptivo.
            "disclaimer": "análisis descriptivo; no predice viralidad ni garantiza resultados",
        }


def derive_recipe(
    analysis: ReferenceAnalysis,
    *,
    brief_shots: int | None = None,
) -> EditableRecipe:
    """Convierte el análisis en una receta editable adaptada al brief.

    - cada corte observado → un paso `shot` (observado);
    - el tempo (beat interval) → un paso `beat_sync` (inferido del ritmo);
    - cada título OCR → un paso `title` (observado, con el texto real);
    - si el brief pide un número de shots distinto, se ADAPTA (origen adapted),
      sin borrar la procedencia.
    """
    recipe = EditableRecipe(reference_id=analysis.reference_id)

    for i, cut in enumerate(analysis.cuts):
        recipe.steps.append(
            RecipeStep(
                kind="shot",
                detail={
                    "index": i,
                    "duration_s": round(cut.duration_s, 3),
                    "camera_move": (
                        analysis.camera_moves[i] if i < len(analysis.camera_moves) else "static"
                    ),
                },
                origin=Origin.OBSERVED,
            )
        )

    interval = analysis.beat_interval_s()
    if interval:
        recipe.steps.append(
            RecipeStep(
                kind="beat_sync",
                detail={"interval_s": interval, "beats": len(analysis.beats)},
                origin=Origin.INFERRED,
            )
        )

    for title in analysis.titles:
        recipe.steps.append(
            RecipeStep(kind="title", detail=dict(title), origin=Origin.OBSERVED)
        )

    # Adaptación al brief: si pide otro número de shots, se ajusta declarándolo.
    observed_shots = len([s for s in recipe.steps if s.kind == "shot"])
    if brief_shots is not None and brief_shots != observed_shots:
        recipe.steps.append(
            RecipeStep(
                kind="shot_count_adaptation",
                detail={"from": observed_shots, "to": brief_shots},
                origin=Origin.ADAPTED,
            )
        )
        recipe.notes = (
            f"adaptado: la referencia tiene {observed_shots} shots, el brief pide {brief_shots}"
        )

    return recipe


@dataclass(slots=True)
class Connector:
    """Un conector de inspiración externo y su estado (nunca de pago activo)."""

    name: str
    status: ConnectorStatus
    note: str = ""

    def is_usable(self) -> bool:
        return self.status is ConnectorStatus.AVAILABLE


def connector_catalog(env: dict[str, str] | None = None) -> list[Connector]:
    """Catálogo de conectores de inspiración con su estado REAL.

    Sin credenciales presentes, un conector queda `blocked` (opcional), no
    inventa datos. Los de pago quedan `forbidden_paid` y nunca se invocan. El
    análisis local sobre la biblioteca propia no necesita ninguno.
    """
    import os

    environment = os.environ if env is None else env
    return [
        Connector(
            name="own_library",
            status=ConnectorStatus.AVAILABLE,
            note="biblioteca propia; modo sin red",
        ),
        Connector(
            name="wikimedia_commons",
            status=(
                ConnectorStatus.AVAILABLE
                if environment.get("STUDIO_WIKIMEDIA_ENABLED")
                else ConnectorStatus.BLOCKED
            ),
            note="API gratuita; opcional, requiere activación explícita",
        ),
        Connector(
            name="heygen_reverse",
            status=ConnectorStatus.FORBIDDEN_PAID,
            note="de pago / cuenta; nunca se invoca (A09)",
        ),
    ]


def assert_no_paid_connector(connector: Connector) -> None:
    """Falla si se intenta usar un conector de pago/no autorizado (A09)."""
    if connector.status is ConnectorStatus.FORBIDDEN_PAID:
        raise RuntimeError(
            f"conector {connector.name} es de pago/no autorizado: no se invoca"
        )


__all__ = [
    "Connector",
    "ConnectorStatus",
    "Cut",
    "EditableRecipe",
    "Origin",
    "RecipeStep",
    "ReferenceAnalysis",
    "assert_no_paid_connector",
    "connector_catalog",
    "derive_recipe",
]
