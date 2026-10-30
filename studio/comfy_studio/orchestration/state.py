"""Estado tipado de una producción (M6 Bloque 4, fase 12).

`ProductionState` es el estado que viaja por el grafo de orquestación LangGraph
(Eje 4). Es la jerarquía del Eje 1 (idea → secuencias → shots → capas) más el
registro del feedback loop del Eje 2 (score de aceptación + escalones de
replanificación local→plantilla→global).

Criterio de diseño (igual que el cerebro: NO Letta, SÍ stdlib):
  - **dataclasses + enums de la stdlib**, no Pydantic duro. El repo no obliga una
    versión de Pydantic en esta capa; un estado serializable con `asdict`/JSON es
    suficiente y no arrastra deps. (El checkpointer SQLite guarda ese JSON.)
  - toda falla se tipifica; nada en esta capa lanza por sí mismo.
  - serializable de ida y vuelta (`to_dict`/`from_dict`) para que el checkpointer
    reanude tras un corte — encaja con el patrón reanudable del repo.

No introduce ningún proceso nuevo ni toca los motores: es el contrato de datos que
los nodos del grafo (fases 13–16) leen y escriben.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ShotStatus(StrEnum):
    """Dónde está un shot en el lazo adaptativo."""

    PLANNED = "planned"
    RUNNING = "running"
    EVALUATING = "evaluating"
    ACCEPTED = "accepted"
    REPLANNING = "replanning"
    FAILED = "failed"


class ReplanTier(StrEnum):
    """Escalón de replanificación (Eje 1, orden exacto pedido por el usuario)."""

    #: 1. mismo motor/plantilla, otra seed/params.
    LOCAL = "local"
    #: 2. el TemplateSelector elige otra plantilla del índice.
    TEMPLATE = "template"
    #: 3. el planner LLM reconsidera el shot/secuencia entero.
    GLOBAL = "global"


#: progresión de escalones; el grafo salta al siguiente cuando el actual falla.
REPLAN_ESCALATION: tuple[ReplanTier, ...] = (
    ReplanTier.LOCAL,
    ReplanTier.TEMPLATE,
    ReplanTier.GLOBAL,
)


def _next_replan_tier(current: ReplanTier | None) -> ReplanTier | None:
    """Siguiente escalón, o None si ya se agotó el global."""
    if current is None:
        return REPLAN_ESCALATION[0]
    idx = REPLAN_ESCALATION.index(current)
    if idx + 1 < len(REPLAN_ESCALATION):
        return REPLAN_ESCALATION[idx + 1]
    return None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass(slots=True)
class Layer:
    """Una capa dentro de un shot (gen, depth, relight, comp...)."""

    kind: str
    engine: str
    template_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    output_path: str | None = None


@dataclass(slots=True)
class Shot:
    """Un plano de 3–7 s: la unidad que se ejecuta, evalúa y replanifica."""

    description: str
    id: str = field(default_factory=lambda: _new_id("shot"))
    duration_s: float = 5.0
    layers: list[Layer] = field(default_factory=list)
    #: umbral de aceptación (VBench/FFmpeg) que el evaluador debe cumplir.
    acceptance: dict[str, float] = field(default_factory=dict)
    status: ShotStatus = ShotStatus.PLANNED
    #: shots de los que depende (orden topológico del Eje 2).
    depends_on: list[str] = field(default_factory=list)
    #: último score del evaluador (None hasta la primera evaluación).
    last_score: dict[str, float] | None = None
    #: escalón de replanificación actual (None = aún no replanificado).
    replan_tier: ReplanTier | None = None
    replan_attempts: int = 0
    cost_cap_usd: float = 0.0
    output_path: str | None = None
    error: str | None = None

    def escalate_replan(self) -> ReplanTier | None:
        """Avanza al siguiente escalón; devuelve el nuevo (o None si agotado)."""
        nxt = _next_replan_tier(self.replan_tier)
        if nxt is None:
            self.status = ShotStatus.FAILED
            return None
        self.replan_tier = nxt
        self.replan_attempts += 1
        self.status = ShotStatus.REPLANNING
        return nxt


@dataclass(slots=True)
class Sequence:
    """Una secuencia: grupo de shots que cuentan una parte de la idea."""

    title: str
    id: str = field(default_factory=lambda: _new_id("seq"))
    shots: list[Shot] = field(default_factory=list)


@dataclass(slots=True)
class ProductionState:
    """Estado completo de una producción que viaja por el grafo LangGraph.

    Es el `state` del Eje 4: cada nodo lo recibe, lo muta y lo devuelve. El
    checkpointer lo serializa con `to_dict` tras cada paso para reanudar.
    """

    idea: str
    id: str = field(default_factory=lambda: _new_id("prod"))
    sequences: list[Sequence] = field(default_factory=list)
    #: fase actual del grafo (planning, executing, evaluating, assembling, done).
    phase: str = "planning"
    #: cost cap total de la producción (se reparte por shot).
    cost_cap_usd: float = 0.0
    #: ¿el borrador barato pasó la StoryboardGate humana?
    storyboard_approved: bool = False
    #: ruta del reel final ensamblado.
    final_output_path: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: bitácora de decisiones del grafo (replan, bifurcación, aceptación).
    log: list[dict[str, Any]] = field(default_factory=list)
    #: metadatos persistidos junto al estado (puerta de storyboard, contadores
    #: de reintento, etc.). Se serializa con el checkpoint para reanudar.
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- helpers de recorrido ------------------------------------------------

    def all_shots(self) -> list[Shot]:
        """Todos los shots de todas las secuencias, en orden."""
        return [shot for seq in self.sequences for shot in seq.shots]

    def shot_by_id(self, shot_id: str) -> Shot | None:
        for shot in self.all_shots():
            if shot.id == shot_id:
                return shot
        return None

    def pending_shots(self) -> list[Shot]:
        """Shots que aún no están aceptados ni fallados definitivamente."""
        done = {ShotStatus.ACCEPTED, ShotStatus.FAILED}
        return [s for s in self.all_shots() if s.status not in done]

    def is_complete(self) -> bool:
        """True si ningún shot queda pendiente."""
        return not self.pending_shots()

    def record(self, kind: str, **fields: Any) -> None:
        """Anota una decisión del grafo en la bitácora (sin secretos)."""
        entry = {"ts": time.time(), "kind": kind, **fields}
        self.log.append(entry)
        self.updated_at = time.time()

    # -- serialización (checkpointer / API) ----------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Estado como dict JSON-serializable (sin perder enums)."""
        return {
            "id": self.id,
            "idea": self.idea,
            "phase": self.phase,
            "cost_cap_usd": self.cost_cap_usd,
            "storyboard_approved": self.storyboard_approved,
            "final_output_path": self.final_output_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "log": list(self.log),
            "metadata": dict(self.metadata),
            "sequences": [
                {
                    "id": seq.id,
                    "title": seq.title,
                    "shots": [
                        {
                            "id": sh.id,
                            "description": sh.description,
                            "duration_s": sh.duration_s,
                            "acceptance": dict(sh.acceptance),
                            "status": str(sh.status),
                            "depends_on": list(sh.depends_on),
                            "last_score": sh.last_score,
                            "replan_tier": (
                                str(sh.replan_tier) if sh.replan_tier else None
                            ),
                            "replan_attempts": sh.replan_attempts,
                            "cost_cap_usd": sh.cost_cap_usd,
                            "output_path": sh.output_path,
                            "error": sh.error,
                            "layers": [
                                {
                                    "kind": ly.kind,
                                    "engine": ly.engine,
                                    "template_id": ly.template_id,
                                    "params": dict(ly.params),
                                    "output_path": ly.output_path,
                                }
                                for ly in sh.layers
                            ],
                        }
                        for sh in seq.shots
                    ],
                }
                for seq in self.sequences
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProductionState:
        """Reconstruye el estado desde un dict del checkpointer."""
        state = cls(
            idea=data["idea"],
            id=data.get("id") or _new_id("prod"),
            phase=data.get("phase", "planning"),
            cost_cap_usd=float(data.get("cost_cap_usd", 0.0)),
            storyboard_approved=bool(data.get("storyboard_approved", False)),
            final_output_path=data.get("final_output_path"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            log=list(data.get("log", [])),
            metadata=dict(data.get("metadata", {})),
        )
        for seq_d in data.get("sequences", []):
            seq = Sequence(title=seq_d["title"], id=seq_d["id"])
            for sh_d in seq_d.get("shots", []):
                shot = Shot(
                    description=sh_d["description"],
                    id=sh_d["id"],
                    duration_s=float(sh_d.get("duration_s", 5.0)),
                    acceptance=dict(sh_d.get("acceptance", {})),
                    status=ShotStatus(sh_d.get("status", "planned")),
                    depends_on=list(sh_d.get("depends_on", [])),
                    last_score=sh_d.get("last_score"),
                    replan_tier=(
                        ReplanTier(sh_d["replan_tier"])
                        if sh_d.get("replan_tier")
                        else None
                    ),
                    replan_attempts=int(sh_d.get("replan_attempts", 0)),
                    cost_cap_usd=float(sh_d.get("cost_cap_usd", 0.0)),
                    output_path=sh_d.get("output_path"),
                    error=sh_d.get("error"),
                )
                shot.layers = [
                    Layer(
                        kind=ly["kind"],
                        engine=ly["engine"],
                        template_id=ly.get("template_id"),
                        params=dict(ly.get("params", {})),
                        output_path=ly.get("output_path"),
                    )
                    for ly in sh_d.get("layers", [])
                ]
                seq.shots.append(shot)
            state.sequences.append(seq)
        return state
