"""Salud de la capa de orquestación (M6 Bloque 5, fase 18).

Produce un reporte serializable que la sonda strict-check del supervisor Rust
(`comfyctl/src/strict.rs`, item S15 `orchestration_ready`) lee para confirmar que
el lazo de producción está montado y es utilizable — no solo que el studio vive.

Mismo espíritu que `engines/health.py` y `agent_brain/registry.py`: tres estados,
nunca lanza, serializable. Un pod cuyo grafo de producción no importa debe fallar
RUIDOSAMENTE al arrancar, no a mitad de un reel.

Estados:
  - `ready`    : el grafo y el servicio importan y construyen; el lazo corre. El
                 runner stdlib es el camino OFICIAL de producción (LangGraph no se
                 hornea por diseño), así que su ausencia NO degrada: se reporta
                 `ready` con `engine` en los metadatos.
  - `degraded` : reservado para un subsistema realmente roto pero no caído.
  - `down`     : el grafo no se puede construir (fallo de import) — fail-closed.
"""

from __future__ import annotations

from typing import Any


def orchestration_health_report() -> dict[str, Any]:
    """Reporte de salud de la orquestación para strict-check S15."""
    try:
        from comfy_studio.orchestration import (
            ProductionService,
            build_production_graph,
            langgraph_available,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed, nunca propaga
        return {
            "state": "down",
            "graph_buildable": False,
            "service_available": False,
            "langgraph": False,
            "engine": None,
            "note": f"import de orquestación falló: {type(exc).__name__}",
        }

    graph_ok = True
    note = ""
    try:
        build_production_graph()
    except Exception as exc:  # noqa: BLE001
        graph_ok = False
        note = f"grafo no construible: {type(exc).__name__}"

    lg = bool(langgraph_available())
    service_ok = ProductionService is not None
    engine = "langgraph" if lg else "stdlib"

    if not graph_ok or not service_ok:
        state = "down"
    else:
        # el runner stdlib es un camino de producción válido (equivalente), no un
        # estado degradado: el pod corre en su modo estándar. Solo 'down' avisa.
        state = "ready"
        if not lg:
            note = note or "runner stdlib (LangGraph no horneado, por diseño)"

    return {
        "state": state,
        "graph_buildable": graph_ok,
        "service_available": service_ok,
        "langgraph": lg,
        "engine": engine,
        "note": note,
    }
