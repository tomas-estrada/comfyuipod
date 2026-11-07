"""Salud de la orquestación y selección del engine efectivo."""

from __future__ import annotations

from comfy_studio.orchestration.health import orchestration_health_report


def test_health_ready_with_effective_engine():
    rep = orchestration_health_report()
    assert rep["state"] == "ready"
    assert rep["graph_buildable"] is True
    assert rep["service_available"] is True
    # La imagen usa LangGraph; los entornos mínimos pueden usar el fallback.
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
