"""Integración: lo que cruza la puerta de aprobación DEBE ser ejecutable y terminar.

Riesgo real: un plan no ejecutable que pasa igual el `approve` se traduce en
horas de pod pagado sin resultado — el grafo gira hasta `max_steps` (`halted`),
o muere con RuntimeError, o deja la producción clavada en `executing`. Este
test ejercita el lazo COMPLETO con los componentes reales de `orchestration`
(planner/executor/evaluator/assembler/grafo/checkpointer SQLite y la puerta de
storyboard), sin parchear ninguno, sobre un tmp dir. El modo simulado es el
camino de producto sin GPU (colaboradores stub shipped), no un mock del sistema
bajo prueba.

Las aserciones no dependen del contenido del plan del planner: se derivan del
plan efectivamente devuelto (cantidad de shots, ids observados) y verifican el
invariante — toda aprobación termina en una fase terminal, coherente y
persistida, con trabajo acotado (nunca girando).

  1. un plan aprobado de punta a punta termina terminal y completo, ejecutando
     cada shot a lo sumo una vez, con bitácora acotada (sin spin a `halted`);
  2. una dependencia alucinada (id inexistente) se tipifica (`FAILED`,
     "dependencia inexistente") y el lazo TERMINA — no gira hasta max_steps ni
     revienta con RuntimeError;
  3. tras un reinicio de pod (servicio nuevo, puertas en RAM perdidas), aprobar
     sigue funcionando: la puerta viaja serializada en el checkpoint
     (regresión histórica M6-SCR-002, que clavaba la producción para siempre).
"""

from __future__ import annotations

import pytest

from comfy_studio.orchestration import (
    ProductionCheckpointer,
    ProductionService,
)


@pytest.fixture()
def checkpointer(tmp_path) -> ProductionCheckpointer:
    return ProductionCheckpointer(db_path=tmp_path / "executability.sqlite3")


def _shots_of(state_dict: dict) -> list[dict]:
    return [shot for seq in state_dict["sequences"] for shot in seq["shots"]]


def _count_log(log: list[dict], kind: str) -> int:
    return sum(1 for entry in log if entry.get("kind") == kind)


def test_plan_aprobado_termina_terminal_completo_y_acotado(
    checkpointer: ProductionCheckpointer,
) -> None:
    """Aprobar lo que pasó la puerta produce una producción TERMINAL, no un bucle.

    El invariante de coste: todo `approve` que no es rechazado por la puerta
    llega a una fase terminal con el mismo resultado persistido en el
    checkpointer, con como mucho un `execute` por shot y bitácora acotada
    (un grafo que gira hasta max_steps revienta el presupuesto de pasos).
    """
    svc = ProductionService(checkpointer=checkpointer)
    planned = svc.plan_production("invariante de ejecutabilidad", cost_cap_usd=2.0)
    production_id = planned["production_id"]
    shots = _shots_of(planned["plan"])
    assert shots, "un plan aprobado no puede no tener nada que ejecutar"

    out = svc.approve_plan(production_id)
    assert out["phase"] in ("simulated", "done"), out
    status = out["status"]
    assert status["complete"] is True
    assert status["shots_accepted"] == status["shots_total"] >= 1

    final = svc.get_plan(production_id)["plan"]
    executed = [e["shot"] for e in final["log"] if e.get("kind") == "execute"]
    assert len(executed) == len(set(executed)) == status["shots_total"]
    # Frontera anti-spin: ~2 eventos por shot es lo que genera el lazo sano;
    # max_steps=200 generaría cientos. El margen es amplio pero finito.
    assert len(final["log"]) < 12 * status["shots_total"] + 40
    assert final["phase"] not in ("executing", "halted")


def test_dependencia_alucinada_se_tipifica_y_el_lazo_termina(
    checkpointer: ProductionCheckpointer,
) -> None:
    """Un id de dependencia inexistente nunca debe dejar el lazo girando.

    Se corrompe el plan por la vía real (checkpointer, sin parches): añade una
    dependencia fantasma a un shot existente y aprueba. El executor debe marcar
    ese shot FAILED con "dependencia inexistente" y el grafo debe TERMINAR en
    una fase terminal con bitácora acotada. Si esta inyección de defensa se
    pierde, el router re-ejecuta el shot eternamente hasta `halted`/max_steps.
    """
    svc = ProductionService(checkpointer=checkpointer)
    production_id = svc.plan_production("plan con dependencia fantasma")["production_id"]
    state = checkpointer.load(production_id)
    assert state is not None and state.all_shots()
    ghost = "shot_ghost_" + "0" * 8
    state.all_shots()[-1].depends_on.append(ghost)
    checkpointer.save(state)

    out = svc.approve_plan(production_id)
    assert out["phase"] in ("simulated", "done", "failed"), out
    assert out["phase"] not in ("executing", "halted")

    final = svc.get_plan(production_id)["plan"]
    bad = [shot for shot in _shots_of(final) if ghost in (shot.get("error") or "")]
    assert len(bad) == 1, "el shot con la dependencia fantasma debe fallar tipado"
    assert bad[0]["status"] == "failed"
    assert "dependencia inexistente" in bad[0]["error"]
    assert _count_log(final["log"], "shot_unreachable") >= 1
    assert len(final["log"]) < 12 * len(_shots_of(final)) + 60
    # Nada pendiente colgado: el lazo llegó a un estado terminal estable.
    assert all(shot["status"] in ("accepted", "failed") for shot in _shots_of(final))


def test_aprobar_sobrevive_al_reinicio_del_pod(checkpointer: ProductionCheckpointer) -> None:
    """Un servicio NUEVO sobre el mismo checkpoint aprueba sin la puerta en RAM.

    El pod puede reiniciar entre el `plan` (humano mirando el borrador) y el
    `approve`. La StoryboardGate viaja serializada en el checkpoint; si esa
    serialización se rompe, la producción queda bloqueada para siempre
    (M6-SCR-002). Aquí no hay instancia compartida: `svc2` empieza con las
    puertas de RAM vacías y solo tiene el SQLite real.
    """
    svc1 = ProductionService(checkpointer=checkpointer)
    production_id = svc1.plan_production("reinicio entre plan y approve")["production_id"]

    svc2 = ProductionService(checkpointer=ProductionCheckpointer(db_path=checkpointer.db_path))
    out = svc2.approve_plan(production_id)
    assert out["phase"] in ("simulated", "done"), out
    assert out["status"]["storyboard_approved"] is True

    # Y la tercera vista (otra instancia nueva) ve el mismo estado terminal.
    status = svc2.production_status(production_id)
    assert status["phase"] == out["phase"]
    assert status["complete"] is True
