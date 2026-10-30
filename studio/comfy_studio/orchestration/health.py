"""Salud de la capa de orquestación (M6 Bloque 5, fase 18).

Produce un reporte serializable que la sonda strict-check del supervisor Rust
(`comfyctl/src/strict.rs`, item S15 `orchestration_ready`) lee para confirmar que
el lazo de producción está montado y es utilizable — no solo que el studio vive.

Mismo espíritu que `engines/health.py` y `agent_brain/registry.py`: tres estados,
nunca lanza, serializable. Un pod cuyo grafo de producción no importa debe fallar
RUIDOSAMENTE al arrancar, no a mitad de un reel.

Estados:
  - `ready`    : el grafo y el servicio importan y construyen; el lazo corre.
  - `degraded` : importan pero falta un colaborador real clave (p.ej. LangGraph no
                 horneado → se usa el runner stdlib; es utilizable pero se marca).
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

    if not graph_ok or not service_ok:
        state = "down"
    elif not lg:
        # utilizable con el runner stdlib; LangGraph no horneado -> degradado.
        state = "degraded"
        note = note or "LangGraph no instalado; se usa el runner stdlib equivalente"
    else:
        state = "ready"

    return {
        "state": state,
        "graph_buildable": graph_ok,
        "service_available": service_ok,
        "langgraph": lg,
        "note": note,
    }
