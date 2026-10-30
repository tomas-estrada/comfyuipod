"""Salud de la capa de orquestación (M6 Bloque 5, fase 18 + auditoría M6-OPS-003).

El runner stdlib es el camino OFICIAL de producción (LangGraph no se hornea por
diseño), así que su ausencia NO degrada el reporte: debe ser `ready` con el motor
en los metadatos, y `degraded`/`down` quedan para fallos reales.
"""

from __future__ import annotations

from comfy_studio.orchestration.health import orchestration_health_report


def test_health_ready_with_stdlib_engine():
    rep = orchestration_health_report()
    assert rep["state"] == "ready"
    assert rep["graph_buildable"] is True
    assert rep["service_available"] is True
    # sin LangGraph horneado, el motor es stdlib pero el estado sigue ready.
    assert rep["engine"] in ("stdlib", "langgraph")
    if not rep["langgraph"]:
        assert rep["engine"] == "stdlib"
        assert rep["state"] == "ready"  # NO degraded


def test_health_report_is_serializable():
    import json

    rep = orchestration_health_report()
    # no debe lanzar: todas las claves son JSON-serializables.
    json.dumps(rep)
    assert set(rep) >= {
        "state",
        "graph_buildable",
        "service_available",
        "langgraph",
        "engine",
        "note",
    }
