"""Cobertura unitaria de `comfy_studio.creative.brief` sin I/O (contrato congelado
`comfy_studio.brief.v1`).

Razón de ser: `test_m7_creative_brief.py` ejercita el módulo a través de la API y
aquí se comprueba el constructor/validador directamente, que es donde vive la
forma del contrato congelado. Toda aserción PINNA el comportamiento real del
código actual (no lo "corrige"): son pruebas de caracterización.

Cubre tres cortes:
1. Derivación `min_sec`/`max_sec` = 0.9x / 1.1x del objetivo, y su degradación
   cuando el prompt no fija duración o la fija en 0.
2. Conversión de unidades de duración en `parse_prompt` (`_DURATION_PATTERNS`):
   prefijo `s`/`seg`/`sec`, minutos (con coma decimal y en mayúsculas) y las
   constante textuales «medio minuto» / «un minuto».
3. `build_brief` aplica `overrides` solo sobre `EDITABLE_FIELDS` y levanta el
   `ValueError` correspondiente a cada campo validado; `validate_brief` cubre
   todas sus ramas; y el documento sobrevive un round-trip JSON.

Determinista, sin GPU, sin red, sin LLM y sin pesos.

Nota sobre build_brief:579 (`status` especial-casado además de estar en
`EDITABLE_FIELDS`): la rama es redundante, no contradictoria. Ver
`test_status_override_reaches_validation_through_editable_fields`.
"""

from __future__ import annotations

import json
import re

import pytest

from comfy_studio.creative.brief import (
    BRIEF_SCHEMA,
    BRIEF_STATUSES,
    EDITABLE_FIELDS,
    PIECE_TYPES,
    build_brief,
    new_brief,
    parse_prompt,
    validate_brief,
)

NO_DURATION_EDITORIAL: dict[str, object] = {}


def _build(prompt: str, **kwargs: object) -> dict[str, object]:
    """Brief mínimo válido con evidencia vacía (sin hechos, sin diálogo)."""
    evidence = kwargs.pop("evidence", None)
    if evidence is None:
        evidence = {"facts": [], "intent": None}
    editorial = kwargs.pop("editorial", None)
    if editorial is None:
        editorial = {}
    return build_brief(
        project_id="p-1",
        prompt=prompt,
        evidence=evidence,  # type: ignore[arg-type]
        editorial=editorial,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _constraints(doc: dict[str, object]) -> dict[str, object]:
    """user_constraints indexados por `key` (conserva el orden del primero)."""
    by_key: dict[str, object] = {}
    for constraint in doc["user_constraints"]:  # type: ignore[index]
        by_key.setdefault(str(constraint["key"]), constraint["value"])  # type: ignore[index]
    return by_key


def _ac_ids(doc: dict[str, object]) -> list[str]:
    return [str(ac["id"]) for ac in doc["acceptance_criteria"]]  # type: ignore[index]


# ---------------------------------------------------------------------------
# 1. Derivación de min/max de duración (0.9x / 1.1x)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "target", "minimum", "maximum"),
    [
        ("reel de 15 segundos", 15.0, 13.5, 16.5),
        ("video de 90s", 90.0, 81.0, 99.0),
        ("clip de 2 minutos", 120.0, 108.0, 132.0),
        ("pieza de 7 segundos", 7.0, 6.3, 7.7),
    ],
)
def test_duration_min_max_derive_from_target_at_0_9_and_1_1(
    prompt: str, target: float, minimum: float, maximum: float
) -> None:
    duration = _build(prompt)["duration"]

    assert duration["target_sec"] == target  # type: ignore[index]
    assert duration["min_sec"] == minimum  # type: ignore[index]
    assert duration["max_sec"] == maximum  # type: ignore[index]
    assert duration["min_sec"] < duration["target_sec"] < duration["max_sec"]  # type: ignore[index]


def test_duration_min_max_are_none_when_prompt_sets_no_duration() -> None:
    """Sin restricción explícita: `target_sec` None y por tanto min/max None."""
    duration = _build("hazme una pieza bonita")["duration"]

    assert duration == {"target_sec": None, "min_sec": None, "max_sec": None}


def test_zero_second_duration_yields_none_bounds_not_zero_bounds() -> None:
    """`duration_sec` 0.0 es falsy en el `if` que deriva los límites, así que los
    límites quedan en None aunque el objetivo sí quede fijado en 0.0."""
    duration = _build("video de 0 segundos")["duration"]

    assert duration["target_sec"] == 0.0  # type: ignore[index]
    assert duration["min_sec"] is None  # type: ignore[index]
    assert duration["max_sec"] is None  # type: ignore[index]


def test_duration_constraint_reaches_acceptance_criteria_with_ten_percent_target() -> None:
    doc = _build("video de 90s")

    by_key = _constraints(doc)
    assert by_key["duration_sec"] == 90.0
    duration_ac = next(ac for ac in doc["acceptance_criteria"] if ac["id"] == "ac-duration")  # type: ignore[index]
    assert duration_ac["target"] == "90.0s ±10%"
    assert duration_ac["how_verified"] == "ffprobe sobre el render final"


def test_no_duration_constraint_adds_no_ac_duration_but_adds_assumption() -> None:
    doc = _build("hazme una pieza bonita")

    assert "ac-duration" not in _ac_ids(doc)
    assert any(
        "Duración no especificada" in str(a["text"]) for a in doc["assumptions"]  # type: ignore[index]
    )
    # Sin más restricciones el brief arranca con las dos validaciones base.
    assert _ac_ids(doc) == ["ac-no-invented-facts", "ac-render-integrity"]


def test_first_duration_pattern_wins_when_prompt_carries_two() -> None:
    """Los patrones se prueban en orden y se corta en el primero que casa."""
    intent = parse_prompt("video de 90 s y 3 min")

    assert intent["duration_sec"] == 90.0
    # Y solo se registra una restricción de duración.
    assert [c for c in intent["constraints"] if c["key"] == "duration_sec"] == [
        {"key": "duration_sec", "value": 90.0, "text": "90 s", "source": "user"}
    ]


# ---------------------------------------------------------------------------
# 2. Conversión de unidades de duración en parse_prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "seconds", "matched_text"),
    [
        ("video de 90s", 90.0, "90s"),
        ("corto de 45 S", 45.0, "45 s"),
        ("video de 15 segundos", 15.0, "15 segundos"),
        ("clip de 30 seg", 30.0, "30 seg"),
        ("pieza de 5 sec", 5.0, "5 sec"),
        ("clip de 2 minutos", 120.0, "2 minutos"),
        ("reunion de 3 min", 180.0, "3 min"),
        ("clip de 2 MIN", 120.0, "2 min"),
        ("recopilacion de 60 minutos", 3600.0, "60 minutos"),
        ("pieza de 1.5 minutos", 90.0, "1.5 minutos"),
        ("pieza de 1,5 minutos", 90.0, "1,5 minutos"),
    ],
)
def test_numeric_duration_units_convert_to_seconds(
    prompt: str, seconds: float, matched_text: str
) -> None:
    intent = parse_prompt(prompt)

    assert intent["duration_sec"] == seconds
    duration_constraint = next(c for c in intent["constraints"] if c["key"] == "duration_sec")
    assert duration_constraint["value"] == seconds
    # El fragmento original se conserva tal cual aparece en el prompt (minúsculas).
    assert duration_constraint["text"] == matched_text
    assert duration_constraint["source"] == "user"


@pytest.mark.parametrize(
    ("prompt", "seconds", "matched_text"),
    [
        ("necesito medio minuto", 30.0, "medio minuto"),
        ("dame un minuto de resumen", 60.0, "un minuto"),
    ],
)
def test_group_less_duration_patterns_use_bare_multiplier(
    prompt: str, seconds: float, matched_text: str
) -> None:
    """«medio minuto»/«un minuto» no capturan dígitos: usan `multiplier` directo."""
    intent = parse_prompt(prompt)

    assert intent["duration_sec"] == seconds
    duration_constraint = next(c for c in intent["constraints"] if c["key"] == "duration_sec")
    assert duration_constraint["text"] == matched_text


def test_unsupported_hour_unit_is_not_parsed_as_duration() -> None:
    """No hay patrón para horas: «1.5 hours» no fija duración ni restricción.

    Pinna el límite real del parser (solo segundos y minutos), no un objetivo.
    """
    intent = parse_prompt("un documental de 1.5 hours")

    assert intent["duration_sec"] is None
    assert not [c for c in intent["constraints"] if c["key"] == "duration_sec"]


def test_parse_prompt_is_deterministic_across_calls() -> None:
    assert parse_prompt("reel de 15 segundos") == parse_prompt("reel de 15 segundos")


# ---------------------------------------------------------------------------
# 3. build_brief: overrides y validación
# ---------------------------------------------------------------------------


def test_build_brief_emits_frozen_schema_and_accepts_it_back() -> None:
    doc = _build("reel de 15 segundos")

    assert doc["schema"] == "comfy_studio.brief.v1"
    assert doc["schema"] == BRIEF_SCHEMA
    # El validador devuelve el MISMO objeto cuando pasa (no una copia).
    assert validate_brief(doc) is doc


def test_build_brief_default_status_is_draft_and_argument_is_whitelisted() -> None:
    assert _build("clip de 2 minutos")["status"] == "draft"
    assert _build("clip de 2 minutos", status="approved")["status"] == "approved"
    # Un status fuera de BRIEF_STATUSES se degrada a "draft", no se propaga.
    assert _build("clip de 2 minutos", status="published")["status"] == "draft"


def test_overrides_outside_editable_fields_are_ignored() -> None:
    doc = _build(
        "clip de 2 minutos",
        overrides={"schema": "malo", "project_id": "otro", "created_at": "falso", "version": 99},
    )

    assert doc["schema"] == BRIEF_SCHEMA
    assert doc["project_id"] == "p-1"
    assert "version" not in doc
    # `asset_facts` tampoco es editable vía overrides.
    assert doc["asset_facts"] == []


def test_override_replaces_editable_field_and_immunizes_validator_input() -> None:
    doc = _build(
        "clip de 2 minutos",
        overrides={
            "purpose": "promesa editada",
            "piece_type": "interview",
            "audience": {"hypothesis": "jóvenes", "is_hypothesis": True},
            "duration": {"target_sec": 8.0, "min_sec": 7.2, "max_sec": 8.8},
            "aspect_ratio": "1:1",
            "concept": "concepto editado",
            "tone": "íntimo",
            "emotion": "esperanza",
            "user_constraints": [{"key": "tone", "value": "íntimo", "source": "user"}],
            "acceptance_criteria": [{"id": "ac-duration", "check": "c", "target": "t"}],
            "editorial": {"promise": "p"},
        },
    )

    assert doc["purpose"] == "promesa editada"
    assert doc["piece_type"] == "interview"
    assert doc["audience"] == {"hypothesis": "jóvenes", "is_hypothesis": True}
    assert doc["duration"] == {"target_sec": 8.0, "min_sec": 7.2, "max_sec": 8.8}
    assert doc["aspect_ratio"] == "1:1"
    assert doc["concept"] == "concepto editado"
    assert doc["tone"] == "íntimo"
    assert doc["emotion"] == "esperanza"


def test_status_override_reaches_validation_through_editable_fields() -> None:
    """`status` ya figura en `EDITABLE_FIELDS`, de modo que el primer bucle de
    `build_brief` lo aplica y luego la validación decide.

    Consecuencias pinadas de esa precedencia:
    - un status válido se aplica igual que cualquier otro campo editable;
    - un status inválido NO se queda en "draft" (el valor se asignó antes de
      validar) sino que levanta `status inválido` de forma idéntica a como lo
      haría un `piece_type` inválido.
    """
    assert "status" in EDITABLE_FIELDS
    assert set(BRIEF_STATUSES) == {"draft", "proposed", "approved", "rejected"}
    assert _build("clip de 2 minutos", overrides={"status": "approved"})["status"] == "approved"

    with pytest.raises(ValueError, match=r"status inválido: 'weird'"):
        _build("clip de 2 minutos", overrides={"status": "weird"})
    with pytest.raises(ValueError, match=r"piece_type inválido: None"):
        _build("clip de 2 minutos", overrides={"piece_type": None})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"piece_type": "documental"}, r"piece_type inválido: 'documental'"),
        ({"user_constraints": {"a": 1}}, "user_constraints debe ser una lista"),
        ({"editorial": "no-dict"}, "editorial debe ser un objeto"),
        ({"acceptance_criteria": {}}, "acceptance_criteria debe ser una lista"),
    ],
)
def test_invalid_override_raises_value_error(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _build("clip de 2 minutos", overrides=overrides)


def test_evidence_intent_precedes_prompt_parsing() -> None:
    evidence = {  # type: ignore[var-annotated]
        "facts": [],
        "intent": {
            "piece_type": "interview",
            "constraints": [
                {"key": "duration_sec", "value": 30.0, "text": "30s", "source": "user"},
                {"key": "language", "value": "pt", "text": "", "source": "user"},
                {"key": "subtitles", "value": False, "text": "", "source": "user"},
                {"key": "music", "value": True, "text": "", "source": "user"},
            ],
            "tone": "minimalista",
            "keywords": ["alpha", "beta"],
            "audience_hint": "jóvenes",
        },
    }
    doc = _build("reel de 15 segundos en inglés", evidence=evidence)

    assert doc["piece_type"] == "interview"
    assert doc["duration"] == {"target_sec": 30.0, "min_sec": 27.0, "max_sec": 33.0}
    assert doc["audience"]["language"] == "pt"
    assert doc["audience"]["hypothesis"] == "jóvenes"
    assert doc["audience"]["is_hypothesis"] is True
    assert doc["tone"] == "minimalista"
    assert doc["keywords"] == ["alpha", "beta"]
    assert doc["user_constraints"] == evidence["intent"]["constraints"]
    # Aspecto por defecto: 16:9 y por tanto sin assumption de formato.
    assert doc["aspect_ratio"] == "16:9"
    assert not any("Formato no especificado" in str(a["text"]) for a in doc["assumptions"])
    assert {"ac-duration", "ac-music", "ac-language"} <= set(_ac_ids(doc))
    assert "ac-subtitles" not in _ac_ids(doc)
    assert "ac-no-music" not in _ac_ids(doc)


def test_unknown_piece_type_in_intent_falls_back_to_other() -> None:
    doc = new_brief(
        project_id="p-1",
        prompt="x",
        intent={"piece_type": "documental"},
        evidence={"facts": []},
        editorial={},
    )

    assert doc["piece_type"] == "other"
    assert all(pt in PIECE_TYPES for pt in PIECE_TYPES)


def test_evidence_facts_are_carried_with_evidence_attached() -> None:
    evidence = {  # type: ignore[var-annotated]
        "facts": [
            {
                "id": "f-1",
                "kind": "transcript_segment",
                "asset_id": "a-1",
                "interval_sec": [0.0, 3.0],
                "method": "clip",
                "simulated": True,
                "evidence": {"score": 0.8},
                "claim": "hay diálogo",
            }
        ],
        "intent": None,
    }
    doc = _build("clip de 2 minutos", evidence=evidence)

    assert doc["asset_facts"] == [evidence["facts"][0]]
    # Con transcripción presente, los subtítulos se asumen (sin restricción explícita).
    assert any(
        "Hay diálogo en el material" in str(a["text"]) for a in doc["assumptions"]
    )
    assert "ac-subtitles" in _ac_ids(doc)


def test_incomplete_asset_fact_is_rejected_before_the_brief_is_returned() -> None:
    evidence = {"facts": [{"kind": "visual", "asset_id": ""}], "intent": None}

    with pytest.raises(ValueError, match="Cada asset_fact requiere asset_id"):
        _build("clip de 2 minutos", evidence=evidence)


# ---------------------------------------------------------------------------
# validate_brief: ramas directas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, [], "brief", 42, 3.5, True])
def test_validate_brief_rejects_non_object(payload: object) -> None:
    with pytest.raises(ValueError, match="El brief debe ser un objeto JSON"):
        validate_brief(payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("schema", [None, "", "comfy_studio.brief.v2"])
def test_validate_brief_rejects_unexpected_schema(schema: object) -> None:
    with pytest.raises(ValueError, match="schema inválido: .*esperado comfy_studio.brief.v1"):
        validate_brief({"schema": schema})


def test_validate_brief_walks_checks_in_order_and_returns_same_object() -> None:
    """Cada guarda se alcanza solo cuando las anteriores ya pasaron."""
    for piece_type in (None, "reel", "zz"):
        doc: dict[str, object] = {"schema": BRIEF_SCHEMA}
        if piece_type is not None:
            doc["piece_type"] = piece_type
        if piece_type == "zz":
            with pytest.raises(ValueError, match=r"piece_type inválido: 'zz'"):
                validate_brief(doc)
        elif piece_type == "reel":
            with pytest.raises(ValueError, match=r"status inválido: None"):
                validate_brief(doc)
        else:
            with pytest.raises(ValueError, match=r"piece_type inválido: None"):
                validate_brief(doc)

    full = {
        "schema": BRIEF_SCHEMA,
        "piece_type": "reel",
        "status": "draft",
        "asset_facts": [{"asset_id": "a-1"}],
        "user_constraints": [],
        "editorial": {},
        "acceptance_criteria": [],
    }
    # `asset_facts` here es str, así que la comprobación por fact no llega a correr
    # y el mensaje es el genérico de "lista" para todos menos `editorial`.
    expected = {
        "asset_facts": "asset_facts debe ser una lista",
        "user_constraints": "user_constraints debe ser una lista",
        "editorial": "editorial debe ser un objeto",
        "acceptance_criteria": "acceptance_criteria debe ser una lista",
    }
    for field, message in expected.items():
        broken = dict(full)
        broken[field] = "no-esperado"
        with pytest.raises(ValueError, match=re.escape(message)):
            validate_brief(broken)

    assert validate_brief(full) is full


def test_validate_brief_accepts_every_whitelisted_status_and_piece_type() -> None:
    for status in BRIEF_STATUSES:
        for piece_type in PIECE_TYPES:
            doc = {
                "schema": BRIEF_SCHEMA,
                "piece_type": piece_type,
                "status": status,
                "asset_facts": [{"asset_id": "a-1"}],
                "user_constraints": [],
                "editorial": {},
                "acceptance_criteria": [],
            }
            assert validate_brief(doc) is doc


# ---------------------------------------------------------------------------
# Round-trip de serialización
# ---------------------------------------------------------------------------


def test_brief_survives_json_round_trip_with_utf8_preserved() -> None:
    doc = _build("reel brutal de 15 segundos para jóvenes")

    restored = json.loads(json.dumps(doc, ensure_ascii=False))

    assert restored == doc
    assert restored["piece_type"] == doc["piece_type"]
    assert restored["tone"] == "impactante"
    assert validate_brief(restored)["schema"] == BRIEF_SCHEMA


def test_brief_top_level_shape_is_stable() -> None:
    doc = _build("reel de 15 segundos")

    assert sorted(doc) == [
        "acceptance_criteria",
        "aspect_ratio",
        "asset_facts",
        "assumptions",
        "audience",
        "budget",
        "concept",
        "created_at",
        "duration",
        "editorial",
        "emotion",
        "feedback",
        "keywords",
        "main_message",
        "pending",
        "piece_type",
        "project_id",
        "prompt",
        "purpose",
        "schema",
        "status",
        "style_refs",
        "tone",
        "user_constraints",
    ]
    assert isinstance(doc["created_at"], str)
    assert doc["created_at"].startswith("20")
    assert doc["budget"]["cost_cap_usd"] == 0.0
    assert doc["feedback"] == []
    assert doc["pending"] == []
    assert NO_DURATION_EDITORIAL == {}
