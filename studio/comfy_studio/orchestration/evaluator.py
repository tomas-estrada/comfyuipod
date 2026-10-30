"""Feedback loop + replanificación escalonada (M6 Bloque 4, fase 15).

Cierra el lazo del Eje 2 y dispara la replanificación del Eje 1 en el orden EXACTO
que pidió el usuario:

  1. **local**    — mismo motor/plantilla, otra seed/params;
  2. **plantilla** — el `TemplateSelector` elige otra plantilla del índice;
  3. **global**   — el planner LLM reconsidera el shot entero.

Tras ejecutar un shot, el **evaluador** (VBench + checks FFmpeg: flicker/freeze/
negro) lo puntúa contra su `acceptance`. Si pasa → ACCEPTED. Si no → sube un
escalón de replan y vuelve a PLANNED/REPLANNING para que el executor lo rehaga;
si ya agotó el global → FAILED.

Criterio (consistente con el resto del bloque): el evaluador real (motor `qa` =
VBench/FFmpeg) se inyecta; por defecto un **stub determinista**. La adaptación de
plantilla en el escalón 2 también se inyecta (el `TemplateSelector` del Bloque 3),
de modo que esta capa no depende de él en CI.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from comfy_studio.orchestration.state import (
    ProductionState,
    ReplanTier,
    Shot,
    ShotStatus,
)


class ShotEvaluator(Protocol):
    """Contrato del evaluador de un shot (síncrono).

    La implementación real llama al motor `qa` (VBench si está, si no FFmpeg) y
    devuelve un dict de métricas, p.ej. {'vbench': 0.72}. El stub lo cumple.
    """

    def evaluate(self, shot: Shot) -> dict[str, float]:
        """Puntúa la salida del shot; devuelve métricas numéricas."""
        ...


class StubShotEvaluator:
    """Evaluador determinista para CI/tests.

    Puntúa según el escalón de replan: un shot recién ejecutado (sin replan) puede
    'fallar' si su acceptance es exigente, y mejora al replanificar — así los
    tests pueden ejercitar el lazo de replan de forma reproducible. Por defecto
    devuelve un score alto para que un plan normal pase a la primera.
    """

    def __init__(self, score: float = 0.9) -> None:
        self._score = score

    def evaluate(self, shot: Shot) -> dict[str, float]:
        # cada replan sube un poco el score (modela "mejora al reintentar").
        bonus = 0.1 * (shot.replan_attempts)
        return {"vbench": min(1.0, self._score + bonus)}


#: función que adapta un shot a otra plantilla (escalón 2). Inyectable: el
#: TemplateSelector del Bloque 3 la provee; por defecto un no-op que solo marca.
TemplateReselector = Callable[[Shot], bool]


def _passes_acceptance(score: dict[str, float], acceptance: dict[str, float]) -> bool:
    """True si el score cumple todos los umbrales `*_min` del acceptance."""
    for key, threshold in acceptance.items():
        metric = key[:-4] if key.endswith("_min") else key
        value = score.get(metric)
        if value is None:
            continue
        if value < threshold:
            return False
    return True


def _default_reselect(shot: Shot) -> bool:
    """Reselección de plantilla por defecto (sin TemplateSelector): no-op.

    Devuelve False = no cambió de plantilla; el escalón sigue siendo válido
    (el executor reintentará con los mismos params). El selector real devuelve
    True cuando encontró y aplicó otra plantilla viable.
    """
    return False


def evaluate_and_replan(
    state: ProductionState,
    evaluator: ShotEvaluator | None = None,
    *,
    reselect_template: TemplateReselector | None = None,
) -> ProductionState:
    """Evalúa el shot en EVALUATING y decide aceptar o replanificar (nodo del grafo).

    Implementa el escalón del Eje 1: local → plantilla → global. En el escalón
    'plantilla' invoca `reselect_template`; en 'global' marca el shot para que el
    planner lo reconsidere (fase 13 vía re-plan global, aquí se registra la señal).
    """
    ev = evaluator or StubShotEvaluator()
    reselect = reselect_template or _default_reselect

    for shot in state.all_shots():
        if shot.status != ShotStatus.EVALUATING:
            continue

        score = ev.evaluate(shot)
        shot.last_score = score

        if _passes_acceptance(score, shot.acceptance):
            shot.status = ShotStatus.ACCEPTED
            state.record("evaluate", shot=shot.id, score=score, accepted=True)
            return state

        # no pasa → escalar replanificación.
        tier = shot.escalate_replan()
        if tier is None:
            # agotó local+plantilla+global → el shot falla definitivamente.
            state.record("replan_exhausted", shot=shot.id, score=score)
            return state

        if tier is ReplanTier.TEMPLATE:
            changed = reselect(shot)
            state.record(
                "replan", shot=shot.id, tier=str(tier), template_changed=changed
            )
        elif tier is ReplanTier.GLOBAL:
            # señal para el planner global: reconsiderar el shot entero.
            state.record("replan", shot=shot.id, tier=str(tier), global_replan=True)
        else:  # LOCAL
            state.record("replan", shot=shot.id, tier=str(tier))
        return state

    return state
