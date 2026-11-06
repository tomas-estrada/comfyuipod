"""Tests de `comfy_studio.creative.direction` (M7, dirección editorial).

Cobertura determinista y CPU-only (sin GPU, red, pesos, ffmpeg ni LLM) de las
funciones públicas y auxiliares: `_norm`, `_fact_asset_ids`, `_select_material`,
`_build_structure` y `build_direction`. Incluye entradas vacías, claves
ausentes y entradas degeneradas, más anclas del comportamiento ACTUAL para los
defectos documentados (DIR-001..DIR-007, ver comentarios): este paquete solo
añade tests, no parchea producción.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfy_studio.creative.direction import (
    _CTA_BY_PIECE,
    _PIECE_PROMISE,
    _build_structure,
    _fact_asset_ids,
    _norm,
    _select_material,
    build_direction,
)

# --------------------------------------------------------------------------- #
# Helpers (fixtures en memoria; los helpers reciben dicts planos, sin store)
# --------------------------------------------------------------------------- #


def _asset(asset_id: str = "a1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "asset_id": asset_id,
        "filename": f"{asset_id}.mp4",
        "original_filename": None,
        "sha256": None,
        "status": "ok",
        "probe": None,
    }
    base.update(kw)
    return base


def _fact(fact_id: str, kind: str, asset_id: str | None, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": fact_id,
        "kind": kind,
        "asset_id": asset_id,
        "interval_sec": None,
        "claim": "",
        "evidence": {},
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# _norm (L49)
# --------------------------------------------------------------------------- #


def test_norm_lowercases_and_collapses_separators() -> None:
    assert _norm("  ¡Hola, MUNDO!! ") == "hola mundo"
    assert _norm("hello-world_2!") == "hello world 2"
    assert _norm("a   b\tc\n") == "a b c"


def test_norm_keeps_accents_digits_and_strips_non_latin() -> None:
    assert _norm("Café 123") == "café 123"
    assert _norm("ÁÉÍÓÚÑ") == "áéíóúñ"
    assert _norm("日本語 café") == "café"  # no-latin se colapsa a espacio


def test_norm_empty_and_none_are_empty_string() -> None:
    # (text or "") protege también contra None pese a la anotación str.
    assert _norm("") == ""
    assert _norm(None) == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _fact_asset_ids (L53)
# --------------------------------------------------------------------------- #


def test_fact_asset_ids_empty_facts() -> None:
    assert _fact_asset_ids([]) == set()


def test_fact_asset_ids_skips_missing_and_falsy() -> None:
    facts = [
        _fact("f1", "clip_shot", "a1"),
        _fact("f2", "transcript_segment", "a2"),
        _fact("f3", "media_metadata", None),
        {"id": "f4", "kind": "edl"},  # sin asset_id
        _fact("f5", "edl", ""),  # vacío es falsy y se descarta
    ]
    assert _fact_asset_ids(facts) == {"a1", "a2"}


def test_fact_asset_ids_deduplicates() -> None:
    facts = [_fact("f1", "clip_shot", "a1"), _fact("f2", "clip_shot", "a1")]
    assert _fact_asset_ids(facts) == {"a1"}


# --------------------------------------------------------------------------- #
# _select_material (L57)
# --------------------------------------------------------------------------- #


def test_select_material_empty_inputs() -> None:
    assert _select_material({}, {}) == {"use": [], "skip": [], "method": "heuristic-v1"}


def test_select_material_without_keywords_uses_everything() -> None:
    understanding = {"assets": [_asset("a1"), _asset("a2")], "facts": []}
    result = _select_material({}, understanding)
    assert [u["asset_id"] for u in result["use"]] == ["a1", "a2"]  # orden de entrada
    assert result["skip"] == []
    for entry in result["use"]:
        assert entry["reason"] == "material general del proyecto"
        assert entry["relevance_score"] == 0.0
        assert entry["evidence_refs"] == []


def test_select_material_filename_keyword_scores_two_points() -> None:
    intent = {"keywords": ["toma"]}
    understanding = {"assets": [_asset("a1", filename="toma1.mp4")], "facts": []}
    result = _select_material(intent, understanding)
    entry = result["use"][0]
    assert entry["relevance_score"] == 2.0
    assert entry["reason"] == "nombre contiene: toma"
    assert result["skip"] == []


def test_select_material_short_keywords_are_ignored() -> None:
    # L67: keywords con len < 3 se filtran; sin keywords "sobra" no aplica.
    intent = {"keywords": ["ok", "go"]}
    understanding = {"assets": [_asset("a1", filename="otro.mp4")], "facts": []}
    result = _select_material(intent, understanding)
    assert [u["asset_id"] for u in result["use"]] == ["a1"]
    assert result["use"][0]["reason"] == "material general del proyecto"


def test_select_material_keyword_matching_is_case_and_punctuation_sensitive() -> None:
    # DIR-001 (L67/L80/L87): el nombre se normaliza con _norm (minúsculas) pero
    # la keyword entra cruda. Ancla del comportamiento actual: mayúsculas o
    # puntuación en la keyword anulan la afinidad y el asset se descarta.
    understanding = {"assets": [_asset("a1", filename="toma1.mp4")], "facts": []}
    upper = _select_material({"keywords": ["Toma"]}, understanding)
    assert upper["use"] == []
    assert upper["skip"][0]["reason"] == (
        "sin afinidad con la intención del prompt; sobra para esta pieza"
    )
    punct = _select_material({"keywords": ["¡toma!"]}, understanding)
    assert punct["use"] == []
    lower = _select_material({"keywords": ["toma"]}, understanding)
    assert [u["asset_id"] for u in lower["use"]] == ["a1"]
    assert lower["use"][0]["relevance_score"] == 2.0


def test_select_material_missing_file_excluded_even_with_affinity() -> None:
    intent = {"keywords": ["toma"]}
    understanding = {
        "assets": [_asset("a1", filename="toma1.mp4", status="missing_file")],
        "facts": [],
    }
    result = _select_material(intent, understanding)
    assert result["use"] == []
    assert result["skip"][0]["reason"] == (
        "archivo físico ausente; se excluye sin romper el proyecto"
    )


def test_select_material_dedupes_exact_sha256() -> None:
    understanding = {
        "assets": [
            _asset("a1", sha256="sha-x"),
            _asset("a2", sha256="sha-x"),
            _asset("a3", sha256="sha-y"),
        ],
        "facts": [],
    }
    result = _select_material({}, understanding)
    assert [u["asset_id"] for u in result["use"]] == ["a1", "a3"]
    assert result["skip"][0] == {
        "asset_id": "a2",
        "reason": "duplicado exacto (sha256) de otro asset ya seleccionado",
    }


def test_select_material_probe_penalty_keeps_asset_with_lower_score() -> None:
    intent = {"keywords": ["toma"]}
    understanding = {
        "assets": [_asset("a1", filename="toma1.mp4", status="probe_unverified")],
        "facts": [],
    }
    result = _select_material(intent, understanding)
    entry = result["use"][0]
    assert entry["relevance_score"] == 1.0  # 2.0 (nombre) - 1.0 (penalización)
    assert entry["reason"] == (
        "nombre contiene: toma; sin verificación ffprobe (probe_ok=false)"
    )


def test_select_material_probe_unverified_without_keywords_scores_negative() -> None:
    # DIR-006a (L124-127): sin keywords entra por `not keywords` aunque la
    # penalización deje el score en negativo: queda en `use` con -1.0.
    understanding = {
        "assets": [_asset("a1", status="probe_unverified")],
        "facts": [],
    }
    result = _select_material({}, understanding)
    assert len(result["use"]) == 1
    assert result["use"][0]["relevance_score"] == -1.0
    assert "sin verificación ffprobe" in result["use"][0]["reason"]


def test_select_material_skip_reason_hides_probe_problem() -> None:
    # DIR-006b (L136-140): con keywords, la penalización empuja el score a <= 0
    # y el `skip` usa la razón fija "sin afinidad..." sin mencionar el problema
    # de ffprobe que sí se registró en `why`.
    understanding = {
        "assets": [_asset("a1", filename="sin_afinidad.mp4", status="probe_unverified")],
        "facts": [],
    }
    result = _select_material({"keywords": ["toma"]}, understanding)
    assert result["use"] == []
    assert result["skip"][0]["reason"] == (
        "sin afinidad con la intención del prompt; sobra para esta pieza"
    )


def test_select_material_transcript_and_clip_affinity() -> None:
    intent = {"keywords": ["toma"]}
    understanding = {
        "assets": [_asset("a1"), _asset("a2"), _asset("a3")],
        "facts": [
            _fact(
                "f1",
                "transcript_segment",
                "a1",
                claim="Dice: «buena toma del producto»",
            ),
            _fact(
                "f2",
                "clip_shot",
                "a2",
                claim="Plano genérico",
                evidence={"caption": "toma aérea"},
            ),
            _fact("f3", "media_metadata", "a3", claim="1920x1080, duración 30s"),
        ],
    }
    result = _select_material(intent, understanding)
    by_id = {u["asset_id"]: u for u in result["use"]}
    assert by_id["a1"]["relevance_score"] == 2.5
    assert by_id["a1"]["reason"] == "diálogo afín a: toma"
    assert by_id["a2"]["relevance_score"] == 1.5  # el caption también cuenta
    assert by_id["a2"]["reason"] == "plano afín a: toma"
    # media_metadata no aporta afinidad y sin keywords de más: se descarta.
    assert "a3" not in by_id
    assert [s["asset_id"] for s in result["skip"]] == ["a3"]


def test_select_material_evidence_refs_capped_and_include_unscored_kinds() -> None:
    facts = [_fact(f"fact-{i:04d}", "media_metadata", "a1") for i in range(1, 26)]
    understanding = {"assets": [_asset("a1")], "facts": facts}
    result = _select_material({}, understanding)
    refs = result["use"][0]["evidence_refs"]
    assert len(refs) == 20  # tope [:20] en L133
    assert refs == [f"fact-{i:04d}" for i in range(1, 21)]


def test_select_material_sorted_by_relevance_desc_with_stable_ties() -> None:
    intent = {"keywords": ["toma"]}
    understanding = {
        "assets": [
            _asset("a1", filename="toma1.mp4"),  # 2.0
            _asset("a2", filename="extra.mp4"),  # 2.5 vía transcript
            _asset("a4", filename="toma2.mp4"),  # 2.0 (empate con a1)
        ],
        "facts": [
            _fact("f1", "transcript_segment", "a2", claim="buena toma del producto"),
        ],
    }
    result = _select_material(intent, understanding)
    # Descendente; los empates conservan el orden de entrada (sort estable).
    assert [u["asset_id"] for u in result["use"]] == ["a2", "a1", "a4"]


def test_select_material_missing_asset_id_raises_keyerror() -> None:
    # DIR-004 (L76/L106): `asset["asset_id"]` indexa directo (el resto del
    # módulo usa .get). Ancla actual: KeyError tanto en el auxiliar como en la
    # entrada pública build_direction.
    understanding = {"assets": [{"filename": "x.mp4"}], "facts": []}
    with pytest.raises(KeyError):
        _select_material({}, understanding)
    with pytest.raises(KeyError):
        build_direction({}, understanding)


def test_select_material_no_affinity_skip_reason() -> None:
    understanding = {"assets": [_asset("a1", filename="otro.mp4")], "facts": []}
    result = _select_material({"keywords": ["toma"]}, understanding)
    assert result["use"] == []
    assert result["skip"][0]["reason"] == (
        "sin afinidad con la intención del prompt; sobra para esta pieza"
    )


# --------------------------------------------------------------------------- #
# _build_structure (L145)
# --------------------------------------------------------------------------- #


def _interview_facts() -> list[dict[str, Any]]:
    return [
        _fact(
            "f1",
            "transcript_segment",
            "a1",
            interval_sec=[20.0, 22.0],
            claim="Hablamos de camtasia todo el día",
        ),
        _fact(
            "f2",
            "transcript_segment",
            "a1",
            interval_sec=[0.0, 2.0],
            claim="primera declaración",
        ),
        _fact(
            "f3",
            "transcript_segment",
            "a1",
            interval_sec=[4.0, 6.0],
            claim="segunda declaración",
        ),
    ]


def test_structure_interview_ranks_affinity_over_temporal_order() -> None:
    # El comentario de L158 dice que el orden temporal se conserva, pero la
    # clave de orden es (-afinidad, inicio): el segmento afín sube aunque sea
    # el último en el tiempo (comportamiento actual documentado).
    intent = {"piece_type": "interview", "keywords": ["camtasia"]}
    structure = _build_structure(intent, {"facts": _interview_facts()})
    assert [m["moment"] for m in structure] == ["momento-1", "momento-2", "momento-3"]
    assert structure[0]["interval_sec"] == [20.0, 22.0]  # afín primero
    assert structure[1]["interval_sec"] == [0.0, 2.0]  # luego orden temporal
    assert structure[0]["asset_refs"] == ["a1"]
    assert structure[0]["evidence_refs"] == ["f1"]
    assert structure[0]["description"].startswith("Declaración del material: ")


def test_structure_interview_uppercase_keyword_ignores_affinity() -> None:
    # DIR-002 (L159/L163): kw entra cruda contra _norm(claim); "Camtasia"
    # no matchea "camtasia" → todos cuentan 0 y solo manda el tiempo.
    intent = {"piece_type": "interview", "keywords": ["Camtasia"]}
    structure = _build_structure(intent, {"facts": _interview_facts()})
    assert structure[0]["interval_sec"] == [0.0, 2.0]  # el afín queda último
    assert structure[-1]["interval_sec"] == [20.0, 22.0]


def test_structure_interview_caps_six_moments() -> None:
    facts = [
        _fact(
            f"f{i}",
            "transcript_segment",
            "a1",
            interval_sec=[float(2 * i), float(2 * i + 2)],
            claim=f"declaración {i}",
        )
        for i in range(8)
    ]
    structure = _build_structure({"piece_type": "interview"}, {"facts": facts})
    assert len(structure) == 6  # ranked[:6] en L167
    assert [m["moment"] for m in structure] == [f"momento-{n}" for n in range(1, 7)]
    assert structure[-1]["interval_sec"] == [10.0, 12.0]


def test_structure_interview_truncates_claim_to_140_chars() -> None:
    facts = [
        _fact(
            "f1",
            "transcript_segment",
            "a1",
            interval_sec=[0.0, 1.0],
            claim="x" * 200,
        )
    ]
    structure = _build_structure({"piece_type": "interview"}, {"facts": facts})
    assert structure[0]["description"] == "Declaración del material: " + "x" * 140


def test_structure_event_uses_transcript_branch() -> None:
    structure = _build_structure({"piece_type": "event"}, {"facts": _interview_facts()})
    assert structure[0]["moment"] == "momento-1"
    assert len(structure) == 3


def test_structure_interview_without_transcripts_falls_back_to_shots() -> None:
    facts = [
        _fact("s1", "clip_shot", "a1", interval_sec=[0.0, 2.0]),
        _fact("s2", "clip_shot", "a2", interval_sec=[5.0, 7.0]),
    ]
    structure = _build_structure({"piece_type": "interview"}, {"facts": facts})
    assert [m["moment"] for m in structure] == ["apertura", "cierre"]
    assert structure[0]["evidence_refs"] == ["s1"]


def test_structure_single_shot_only_apertura() -> None:
    facts = [_fact("s1", "clip_shot", "a1", interval_sec=[3.0, 4.0])]
    structure = _build_structure({}, {"facts": facts})
    assert [m["moment"] for m in structure] == ["apertura"]
    assert structure[0]["interval_sec"] == [3.0, 4.0]


def test_structure_two_shots_apertura_and_cierre_without_middle() -> None:
    facts = [
        _fact("s1", "clip_shot", "a1", interval_sec=[5.0, 6.0]),
        _fact("s2", "clip_shot", "a1", interval_sec=[0.0, 1.0]),
    ]
    structure = _build_structure({}, {"facts": facts})
    # Se ordena por inicio: el cierre más tardío cierra, no hay "principal".
    assert [m["moment"] for m in structure] == ["apertura", "cierre"]
    assert structure[0]["interval_sec"] == [0.0, 1.0]
    assert structure[1]["interval_sec"] == [5.0, 6.0]


def test_structure_three_shots_sorted_by_interval() -> None:
    facts = [
        _fact("s1", "clip_shot", "a1", interval_sec=[10.0, 12.0]),
        _fact("s2", "clip_shot", "a1", interval_sec=[0.0, 2.0]),
        _fact("s3", "clip_shot", "a1", interval_sec=[5.0, 7.0]),
    ]
    structure = _build_structure({}, {"facts": facts})
    assert [m["moment"] for m in structure] == [
        "apertura",
        "momento principal",
        "cierre",
    ]
    assert [m["interval_sec"][0] for m in structure] == [0.0, 5.0, 10.0]
    assert structure[1]["evidence_refs"] == ["s3"]  # middle = shots[len//2]


def test_structure_five_shots_middle_is_index_two() -> None:
    facts = [
        _fact(f"s{i}", "clip_shot", "a1", interval_sec=[float(10 * i), float(10 * i + 2)])
        for i in range(5)
    ]
    structure = _build_structure({}, {"facts": facts})
    middle = next(m for m in structure if m["moment"] == "momento principal")
    assert middle["interval_sec"] == [20.0, 22.0]  # shots[5 // 2]


def test_structure_shot_missing_interval_keeps_input_order() -> None:
    # Degenerado: interval_sec None → clave (None or [0])[0] == 0 para todos;
    # sort estable conserva el orden de los facts.
    facts = [
        _fact("s1", "clip_shot", "a1", interval_sec=None),
        _fact("s2", "clip_shot", "a2", interval_sec=None),
    ]
    structure = _build_structure({}, {"facts": facts})
    assert [m["evidence_refs"] for m in structure] == [["s1"], ["s2"]]


def test_structure_identical_fact_object_yields_only_apertura() -> None:
    # Degenerado: el mismo dict repetido → middle/cierre son `is opening`
    # (guarda de identidad L189/L197) y se omiten.
    only = _fact("s1", "clip_shot", "a1", interval_sec=[0.0, 1.0])
    structure = _build_structure({}, {"facts": [only, only, only]})
    assert [m["moment"] for m in structure] == ["apertura"]


def test_structure_fallback_excludes_missing_file_and_caps_three() -> None:
    understanding = {
        "assets": [
            _asset("m1", original_filename="uno.mp4"),
            _asset("m2", original_filename="dos.mp4", status="missing_file"),
            _asset("m3"),  # sin original_filename → usa asset_id
            _asset("m4", original_filename="cuatro.mp4"),
            _asset("m5", original_filename="cinco.mp4"),  # queda fuera del tope 3
        ],
        "facts": [],  # sin transcripts ni shots → rama mínima (L207)
    }
    structure = _build_structure({}, understanding)
    assert [m["moment"] for m in structure] == ["momento-1", "momento-2", "momento-3"]
    assert [m["asset_refs"] for m in structure] == [["m1"], ["m3"], ["m4"]]
    assert structure[0]["description"] == "Material uno.mp4"
    assert structure[1]["description"] == "Material m3"
    for m in structure:
        assert m["interval_sec"] is None
        assert m["evidence_refs"] == []


def test_structure_fallback_empty_when_all_assets_missing_or_absent() -> None:
    only_missing = {"assets": [_asset("m1", status="missing_file")], "facts": []}
    assert _build_structure({}, only_missing) == []
    assert _build_structure({}, {}) == []


def test_structure_story_piece_ignores_transcript_facts() -> None:
    # Ancla actual: los transcripts solo estructuran piezas interview/event
    # (L157); con piece_type "story" se cae a la rama mínima de assets.
    understanding = {
        "assets": [_asset("a1", original_filename="clip.mp4")],
        "facts": [
            _fact("f1", "transcript_segment", "a1", interval_sec=[0.0, 2.0], claim="hola")
        ],
    }
    structure = _build_structure({"piece_type": "story"}, understanding)
    assert len(structure) == 1
    assert structure[0]["moment"] == "momento-1"
    assert structure[0]["interval_sec"] is None
    assert structure[0]["evidence_refs"] == []


# --------------------------------------------------------------------------- #
# build_direction (L222)
# --------------------------------------------------------------------------- #


def _happy_intent() -> dict[str, Any]:
    return {
        "piece_type": "product",
        "tone": "cercano",
        "platform": "instagram",
        "keywords": ["café"],
        "music": False,
        "subtitles": True,
        "cta": True,
    }


def _happy_understanding() -> dict[str, Any]:
    return {
        "assets": [
            _asset(
                "a1",
                original_filename="Promo Café.mp4",
                sha256="sha-a1",
                probe={"probe_ok": True, "has_audio": True},
            ),
            _asset("a2", original_filename="B-roll.mp4", sha256="sha-a2"),
        ],
        "facts": [
            _fact(
                "fact-0001",
                "media_metadata",
                "a1",
                claim="video 1920x1080, duración 30s, audio=sí",
            ),
            _fact(
                "fact-0002",
                "transcript_segment",
                "a1",
                interval_sec=[3.0, 6.0],
                claim="Dice: «me encanta este café»",
            ),
            _fact(
                "fact-0003",
                "clip_shot",
                "a1",
                interval_sec=[0.0, 3.0],
                claim="Plano en t=1.5s",
                evidence={"caption": "plano del café"},
            ),
        ],
    }


def test_build_direction_full_schema_happy_path() -> None:
    intent, understanding = _happy_intent(), _happy_understanding()
    result = build_direction(intent, understanding)

    assert set(result) == {
        "method",
        "promise",
        "concept",
        "emotion",
        "hook",
        "structure",
        "material",
        "audio_plan",
        "cta",
        "consistency_refs",
    }
    assert result["method"] == "heuristic-v1"
    assert result["promise"] == _PIECE_PROMISE["product"]
    assert result["concept"] == (
        "Pieza de tipo 'product' con tono 'cercano' para instagram."
    )
    assert result["emotion"] == "cercano"

    # Hook: primer fact de tipo transcript/clip en orden del documento.
    assert result["hook"] == {
        "description": (
            "Abrir con el primer momento relevante del material (a1 @ [3.0, 6.0])"
        ),
        "evidence_refs": ["fact-0002"],
        "method": "heuristic-v1",
    }

    # Composición: los subcálculos no se rederivan de otra forma.
    assert result["structure"] == _build_structure(intent, understanding)
    assert result["material"] == _select_material(intent, understanding)
    assert [m["moment"] for m in result["structure"]] == ["apertura"]
    assert result["structure"][0]["evidence_refs"] == ["fact-0003"]

    material = result["material"]
    assert set(material) == {"use", "skip", "method"}
    assert set(material["use"][0]) == {
        "asset_id",
        "reason",
        "evidence_refs",
        "relevance_score",
    }
    assert [u["asset_id"] for u in material["use"]] == ["a1"]
    # nombre (2.0) + transcript (2.5) + clip (1.5); a2 sin afinidad se descarta.
    assert material["use"][0]["relevance_score"] == 6.0
    assert [s["asset_id"] for s in material["skip"]] == ["a2"]

    audio = result["audio_plan"]
    assert set(audio) == {
        "dialogue",
        "dialogue_policy",
        "music",
        "music_policy",
        "voice_over",
        "silence",
        "subtitles",
        "subtitles_policy",
        "has_audio_in_sources",
    }
    assert audio["dialogue"] == "conservar la voz original del material (hay transcripción)"
    assert audio["dialogue_policy"] == "preserve_if_present"
    assert audio["music"] == "sin música añadida (restricción del usuario)"
    assert audio["music_policy"] == "prohibited"
    assert audio["subtitles"] == "subtítulos obligatorios"
    assert audio["subtitles_policy"] == "required"
    assert audio["has_audio_in_sources"] is True
    assert audio["silence"] == "solo pausas del montaje"

    assert result["cta"] == _CTA_BY_PIECE["product"]
    # consistency_refs sale de los facts, no de la lista de assets.
    assert result["consistency_refs"] == ["a1"]


def test_build_direction_empty_inputs_defaults() -> None:
    result = build_direction({}, {})
    assert result["method"] == "heuristic-v1"
    assert result["promise"] == _PIECE_PROMISE["other"]
    assert result["concept"] == (
        "Pieza de tipo 'other' con tono 'neutro' para plataforma no especificada."
    )
    assert result["emotion"] == "neutro"
    # DIR-005 (L284): sin fact relevantes la descripción queda con espacio
    # final sobrante; ancla del string exacto actual.
    assert result["hook"]["description"] == (
        "Abrir con el primer momento relevante del material "
    )
    assert result["hook"]["evidence_refs"] == []
    assert result["structure"] == []
    assert result["material"] == {"use": [], "skip": [], "method": "heuristic-v1"}
    assert result["audio_plan"]["dialogue"] == "sin diálogo disponible en el material"
    assert result["audio_plan"]["dialogue_policy"] == "none"
    assert result["audio_plan"]["music_policy"] == "optional"
    assert result["audio_plan"]["subtitles"] == "sin subtítulos"
    assert result["audio_plan"]["subtitles_policy"] == "disabled"
    assert result["audio_plan"]["has_audio_in_sources"] is False
    assert result["cta"] is None
    assert result["consistency_refs"] == []


def test_build_direction_music_tri_state() -> None:
    understanding: dict[str, Any] = {}
    on = build_direction({"music": True}, understanding)["audio_plan"]
    assert on["music"] == "música incidental permitida según el tono"
    assert on["music_policy"] == "required"
    off = build_direction({"music": False}, understanding)["audio_plan"]
    assert off["music"] == "sin música añadida (restricción del usuario)"
    assert off["music_policy"] == "prohibited"
    absent = build_direction({}, understanding)["audio_plan"]
    assert absent["music"] == "priorizar audio original; música solo si el material la trae"
    assert absent["music_policy"] == "optional"


def test_build_direction_subtitles_auto_recommended_but_worded_obligatorios() -> None:
    # DIR-007 (L267 vs L268-274): sin flag explícito, tener transcripción
    # enciende los subtítulos con texto "obligatorios" pero política
    # "recommended"; ancla de ambos valores actuales.
    understanding = {
        "assets": [_asset("a1")],
        "facts": [_fact("f1", "transcript_segment", "a1", interval_sec=[0.0, 1.0])],
    }
    audio = build_direction({}, understanding)["audio_plan"]
    assert audio["subtitles"] == "subtítulos obligatorios"
    assert audio["subtitles_policy"] == "recommended"


def test_build_direction_subtitles_explicit_off_wins_over_speech() -> None:
    understanding = {
        "assets": [_asset("a1")],
        "facts": [_fact("f1", "transcript_segment", "a1", interval_sec=[0.0, 1.0])],
    }
    audio = build_direction({"subtitles": False}, understanding)["audio_plan"]
    assert audio["subtitles"] == "sin subtítulos"
    assert audio["subtitles_policy"] == "disabled"


def test_build_direction_has_audio_requires_probe_ok() -> None:
    # Solo cuentan probes con probe_ok truthy (L230-234).
    unverified = build_direction(
        {},
        {"assets": [_asset("a1", probe={"probe_ok": False, "has_audio": True})]},
    )["audio_plan"]
    assert unverified["has_audio_in_sources"] is False
    assert unverified["dialogue_policy"] == "none"
    verified = build_direction(
        {},
        {"assets": [_asset("a1", probe={"probe_ok": True, "has_audio": True})]},
    )["audio_plan"]
    assert verified["has_audio_in_sources"] is True
    assert verified["dialogue_policy"] == "preserve_if_present"


def test_build_direction_cta_only_for_three_piece_types() -> None:
    # DIR-003 (L307 + L42-51): cta pedido por el usuario, pero
    # _CTA_BY_PIECE solo define product/event/educational → el resto de tipos
    # devuelve None y la petición se pierde en silencio.
    for piece in ("product", "event", "educational"):
        result = build_direction({"piece_type": piece, "cta": True}, {})
        assert result["cta"] == _CTA_BY_PIECE[piece], piece
    for piece in ("story", "trend", "interview", "ambient", "reel", "other"):
        result = build_direction({"piece_type": piece, "cta": True}, {})
        assert result["cta"] is None, piece
    # Sin cta ni el tipo con definición lo tiene.
    assert build_direction({"piece_type": "product"}, {})["cta"] is None


def test_build_direction_unknown_piece_type_not_clamped() -> None:
    # A diferencia de build_brief (que valida contra PIECE_TYPES),
    # build_direction refleja el tipo crudo en el concepto y promesa "other".
    result = build_direction({"piece_type": "mystery"}, {})
    assert result["promise"] == _PIECE_PROMISE["other"]
    assert result["concept"] == (
        "Pieza de tipo 'mystery' con tono 'neutro' para plataforma no especificada."
    )


def test_build_direction_ambient_silence() -> None:
    audio = build_direction({"piece_type": "ambient"}, {})["audio_plan"]
    assert audio["silence"] == "permitida en piezas de ambiente"
    other = build_direction({"piece_type": "story"}, {})["audio_plan"]
    assert other["silence"] == "solo pausas del montaje"


def test_build_direction_hook_skips_metadata_and_formats_interval() -> None:
    understanding = {
        "assets": [_asset("a1"), _asset("a2")],
        "facts": [
            _fact("fact-0001", "media_metadata", "a1", claim="1920x1080"),
            _fact(
                "fact-0002",
                "clip_shot",
                "a2",
                interval_sec=[8.0, 9.0],
                claim="Plano en t=8.5s",
            ),
        ],
    }
    hook = build_direction({}, understanding)["hook"]
    assert hook["description"] == (
        "Abrir con el primer momento relevante del material (a2 @ [8.0, 9.0])"
    )
    assert hook["evidence_refs"] == ["fact-0002"]
    assert hook["method"] == "heuristic-v1"


def test_build_direction_consistency_refs_sorted_deduped_and_truthy_only() -> None:
    facts = [
        _fact("f1", "clip_shot", "z1"),
        _fact("f2", "transcript_segment", "a1"),
        _fact("f3", "media_metadata", None),
        _fact("f4", "edl", ""),
        _fact("f5", "clip_shot", "z1"),
        _fact("f6", "face_identity", "m2"),
    ]
    result = build_direction({}, {"facts": facts})
    assert result["consistency_refs"] == ["a1", "m2", "z1"]


def test_build_direction_dialogue_presence() -> None:
    with_speech = build_direction(
        {},
        {"assets": [], "facts": [_fact("f1", "transcript_segment", "a1")]},
    )["audio_plan"]
    assert with_speech["dialogue"] == "conservar la voz original del material (hay transcripción)"
    without = build_direction({}, {"assets": [], "facts": []})["audio_plan"]
    assert without["dialogue"] == "sin diálogo disponible en el material"
    assert without["voice_over"] == "no prevista salvo que el usuario la pida"
