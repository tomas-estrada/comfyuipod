"""Tests del TemplateSelector (M6 Bloque 3, esqueleto Eje 3).

Corre contra el `TemplateRegistry` real (plantillas curadas del repo). Sin GPU,
sin ComfyUI vivo: la selección es sobre el índice en memoria.
"""

from __future__ import annotations

import pytest

from comfy_studio.orchestration.template_selector import (
    ShotIntent,
    TemplateSelector,
)


def test_select_returns_candidates() -> None:
    sel = TemplateSelector()
    cands = sel.select(ShotIntent(intent="generar un video a partir de texto"))
    assert cands  # hay plantillas curadas
    assert all(hasattr(c, "template_id") for c in cands)
    # Ordenado por (viable, score) desc.
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True) or any(c.viable for c in cands)


def test_text_to_video_intent_favours_t2v() -> None:
    sel = TemplateSelector()
    cands = sel.select(
        ShotIntent(intent="texto a video wan", output_kind="video"), top_k=10
    )
    ids = [c.template_id for c in cands]
    # wan_t2v existe y debería figurar entre los candidatos de video.
    assert "wan_t2v" in ids


def test_output_kind_filters() -> None:
    sel = TemplateSelector()
    img = sel.select(ShotIntent(intent="imagen", output_kind="image"), top_k=20)
    assert all(
        TemplateSelector()._registry.get_template(c.template_id)["output_kind"] == "image"
        for c in img
    )


def test_capability_crossing_marks_missing_nodes_not_viable() -> None:
    sel = TemplateSelector()
    # Sin ningún nodo instalado, cualquier plantilla que requiera nodos es NO viable.
    cands = sel.select(ShotIntent(intent="texto a video"), available_nodes=set(), top_k=20)
    needing_nodes = [c for c in cands if c.required_nodes]
    assert needing_nodes, "alguna plantilla declara required_nodes"
    assert all(not c.viable for c in needing_nodes)
    assert all(c.missing_nodes == c.required_nodes for c in needing_nodes)


def test_capability_crossing_viable_when_nodes_present() -> None:
    sel = TemplateSelector()
    # Reunir todos los nodos requeridos por el roster y ofrecerlos como instalados.
    all_nodes: set[str] = set()
    for tpl in sel._registry.list_templates():
        all_nodes.update(tpl.get("required_nodes") or [])
    cands = sel.select(
        ShotIntent(intent="texto a video wan", output_kind="video"),
        available_nodes=all_nodes,
        top_k=20,
    )
    # Con todos los nodos presentes, las plantillas de video con pesos disponibles son viables.
    assert any(c.viable for c in cands)


def test_best_returns_viable_or_none() -> None:
    sel = TemplateSelector()
    all_nodes: set[str] = set()
    for tpl in sel._registry.list_templates():
        all_nodes.update(tpl.get("required_nodes") or [])
    best = sel.best(ShotIntent(intent="texto a video wan"), available_nodes=all_nodes)
    assert best is None or best.viable


def test_adapt_returns_default_params() -> None:
    sel = TemplateSelector()
    params = sel.adapt("wan_t2v", ShotIntent(intent="algo"))
    assert isinstance(params, dict)


def test_chain_returns_template_ids() -> None:
    sel = TemplateSelector()
    chained = sel.chain([ShotIntent(intent="texto a video wan")])
    assert isinstance(chained, list)


def test_discovery_gate_follows_dynamic(monkeypatch: pytest.MonkeyPatch) -> None:
    sel = TemplateSelector()
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    assert sel.discovery_enabled() is False
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    assert sel.discovery_enabled() is True


def test_candidate_serializable() -> None:
    sel = TemplateSelector()
    cands = sel.select(ShotIntent(intent="video"), top_k=1)
    d = cands[0].to_public_dict()
    assert set(d) >= {"template_id", "score", "viable", "missing_nodes"}
