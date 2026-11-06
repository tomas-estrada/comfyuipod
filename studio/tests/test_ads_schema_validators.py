"""Cobertura unitaria de `comfy_studio.ads.schema` sin I/O (validadores y documento).

Razón de ser: `test_m7_ad_studio.py` ejercita el schema a través del pipeline
(`create_ad_doc`/`attach_storyboard`) pero no ancla directamente el
`field_validator` de `AdBrief`, la normalización de `AdBrief.clean()` ni los
bordes de `ad_doc_dict`. Aquí se comprueban esos caminos puros directamente.
Toda aserción PINNA el comportamiento real del código actual (no lo "corrige"):
son pruebas de caracterización.

Cubre cinco cortes:
1. `_strip_nonempty` (field_validator de `product_name`/`key_message`/
   `audience`/`cta`): recorta espacios y rechaza valores solo con espacios.
2. Estructura de `AdBrief`: campo ausente, campo extra (`extra="forbid"`),
   patrón de `language`, tope de `product_asset_ids` y cota inferior de
   `target_duration_s` (la cota superior ya la cubre el test M7).
3. `AdBrief.clean()`: normaliza `language` (minúsculas), filtra ids de asset
   vacíos, recorta campos de texto y es idempotente.
4. `_quantize`: redondeo a medios segundos con suelo en
   `MIN_SCENE_DURATION_S`, con redondeo bancario (Python `round`).
5. `ad_doc_dict`: valores por defecto de secciones opcionales, serialización
   con alias de `script`/`storyboard`, copia defensiva de `variants` y el
   borde `gate={}` (falsy -> drafting).

Además ancla (sin parchear producción) el caso documentado en el informe:
con `target_duration_s` corto (< 8s) el suelo de 2s por escena domina y el
guion termina por encima del objetivo, pese al comentario de `compose_script`
que promete suma exacta.

Determinista, sin GPU, sin red, sin LLM y sin pesos.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from comfy_studio.ads.schema import (
    AD_DOC_SCHEMA,
    AD_SCRIPT_SCHEMA,
    AD_STORYBOARD_SCHEMA,
    AdBrief,
    _quantize,
    ad_doc_dict,
    build_storyboard,
    compose_script,
)

_BRIEF: dict[str, object] = {
    "product_name": "Cafetera Nova",
    "key_message": "El café perfecto cada mañana en 30 segundos.",
    "audience": "profesionales ocupados",
    "tone": "cercano",
    "target_duration_s": 12.0,
    "cta": "Pruébala hoy con un 20% de descuento.",
    "language": "es",
    "product_asset_ids": [],
}

_TEXT_FIELDS = ("product_name", "key_message", "audience", "cta")


def _brief(**overrides: object) -> AdBrief:
    return AdBrief.model_validate({**_BRIEF, **overrides})


def _doc_kwargs(brief: AdBrief) -> dict[str, object]:
    return {
        "ad_id": "ad123",
        "project_id": "proj9",
        "brief": brief,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# 1. field_validator `_strip_nonempty`: normalización y errores
# ---------------------------------------------------------------------------


def test_field_validator_strips_surrounding_whitespace() -> None:
    brief = AdBrief.model_validate(
        {
            **_BRIEF,
            "product_name": "  Cafetera Nova  ",
            "key_message": "\t mensaje clave \n",
            "audience": "  profesionales ocupados ",
            "cta": "   Cta con espacios   ",
        }
    )
    assert brief.product_name == "Cafetera Nova"
    assert brief.key_message == "mensaje clave"
    assert brief.audience == "profesionales ocupados"
    assert brief.cta == "Cta con espacios"


@pytest.mark.parametrize("field", _TEXT_FIELDS)
def test_field_validator_rejects_whitespace_only_value(field: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdBrief.model_validate({**_BRIEF, field: "   \t  "})
    assert "no puede estar vacío" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. Estructura de AdBrief: ausentes, extra, patrones y topes
# ---------------------------------------------------------------------------


def test_missing_required_field_is_rejected() -> None:
    payload = dict(_BRIEF)
    del payload["cta"]
    with pytest.raises(ValidationError) as excinfo:
        AdBrief.model_validate(payload)
    assert "cta" in str(excinfo.value)


def test_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdBrief.model_validate({**_BRIEF, "budget_eur": 1000})
    assert "budget_eur" in str(excinfo.value)


@pytest.mark.parametrize("bad_language", ["ES", "espa", "es-", "e5"])
def test_language_pattern_is_enforced(bad_language: str) -> None:
    with pytest.raises(ValidationError):
        AdBrief.model_validate({**_BRIEF, "language": bad_language})


def test_language_accepts_region_variant() -> None:
    brief = AdBrief.model_validate({**_BRIEF, "language": "es-ES"})
    assert brief.language == "es-ES"


def test_product_asset_ids_max_length_is_enforced() -> None:
    with pytest.raises(ValidationError):
        AdBrief.model_validate({**_BRIEF, "product_asset_ids": [f"a{i}" for i in range(9)]})


def test_target_duration_below_minimum_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AdBrief.model_validate({**_BRIEF, "target_duration_s": 4.9})


# ---------------------------------------------------------------------------
# 3. AdBrief.clean(): normalización defensiva
# ---------------------------------------------------------------------------


def test_clean_normalizes_language_and_filters_blank_asset_ids() -> None:
    brief = AdBrief.model_validate(
        {**_BRIEF, "language": "es-ES", "product_asset_ids": [" a1 ", "", "   ", "a2"]}
    )
    # El validador de campo no toca los ids: siguen crudos en la instancia.
    assert brief.product_asset_ids == [" a1 ", "", "   ", "a2"]
    cleaned = brief.clean()
    assert isinstance(cleaned, AdBrief)
    assert cleaned.language == "es-es"
    assert cleaned.product_asset_ids == ["a1", "a2"]
    # `clean` devuelve una copia; el original queda intacto.
    assert brief.language == "es-ES"
    assert cleaned is not brief


def test_clean_strips_text_fields_bypassing_field_validation() -> None:
    # `model_construct` omite validadores: documenta que `clean` recorta los
    # campos de texto también en instancias construidas sin pasar por ellos.
    dirty = AdBrief.model_construct(
        product_name="  X  ",
        key_message="  mensaje  ",
        audience="  publico  ",
        cta="  compra  ",
        language=" ES ",
        product_asset_ids=[" z "],
    )
    assert dirty.product_name == "  X  "
    cleaned = dirty.clean()
    assert cleaned.product_name == "X"
    assert cleaned.key_message == "mensaje"
    assert cleaned.audience == "publico"
    assert cleaned.cta == "compra"
    assert cleaned.language == "es"
    assert cleaned.product_asset_ids == ["z"]


def test_clean_is_idempotent_and_keeps_semantic_fields() -> None:
    brief = _brief()
    once = brief.clean()
    assert once.clean() == once
    assert once.tone == brief.tone
    assert once.target_duration_s == brief.target_duration_s
    assert once.cta == brief.cta


# ---------------------------------------------------------------------------
# 4. `_quantize`: medios segundos con suelo
# ---------------------------------------------------------------------------


def test_quantize_rounds_to_half_seconds_with_floor() -> None:
    assert _quantize(3.7) == 3.5  # redondeo al medio más cercano
    assert _quantize(2.0) == 2.0
    assert _quantize(1.0) == 2.0  # suelo en MIN_SCENE_DURATION_S
    assert _quantize(0.0) == 2.0
    assert _quantize(-4.0) == 2.0  # negativos: el suelo también aplica


def test_quantize_uses_bankers_rounding() -> None:
    # `round(4.5) == 4` y `round(5.5) == 6` en Python (redondeo bancario):
    # 2.25 -> 2.0 (no 2.5) fija ese comportamiento de forma inequívoca.
    assert _quantize(2.25) == 2.0
    assert _quantize(2.75) == 3.0


# ---------------------------------------------------------------------------
# 5. ad_doc_dict: bordes del documento persistido
# ---------------------------------------------------------------------------


def test_ad_doc_dict_defaults_for_optional_sections() -> None:
    brief = _brief()
    doc = ad_doc_dict(**_doc_kwargs(brief))  # type: ignore[arg-type]
    assert set(doc) == {
        "schema",
        "id",
        "project_id",
        "brief",
        "script",
        "storyboard",
        "variants",
        "gate",
        "created_at",
        "updated_at",
    }
    assert doc["schema"] == AD_DOC_SCHEMA
    assert doc["id"] == "ad123"
    assert doc["project_id"] == "proj9"
    assert doc["created_at"] == "2026-01-01T00:00:00+00:00"
    assert doc["updated_at"] == "2026-01-01T00:00:00+00:00"
    assert doc["brief"] == brief.model_dump(mode="json")
    assert doc["script"] is None
    assert doc["storyboard"] is None
    assert doc["variants"] == []
    assert doc["gate"] == {"state": "drafting"}


def test_ad_doc_dict_serializes_script_storyboard_and_gate() -> None:
    script = compose_script(_brief(product_asset_ids=["a1", "a2"]))
    storyboard = build_storyboard(script, ad_id="ad123")
    variants = [{"aspect_ratio": "9:16", "path": "/tmp/v.mp4"}]
    gate = {"state": "awaiting_approval", "reviewed_by": "user1"}
    doc = ad_doc_dict(
        **_doc_kwargs(script.brief),  # type: ignore[arg-type]
        script=script,
        storyboard=storyboard,
        variants=variants,
        gate=gate,
    )
    assert doc["script"] == script.to_dict()
    assert doc["storyboard"] == storyboard.to_dict()
    # Serialización con alias: la clave pública es `schema`, no `schema_version`.
    assert doc["script"]["schema"] == AD_SCRIPT_SCHEMA  # type: ignore[index]
    assert "schema_version" not in doc["script"]  # type: ignore[operator]
    assert doc["storyboard"]["schema"] == AD_STORYBOARD_SCHEMA  # type: ignore[index]
    assert doc["variants"] == variants
    assert doc["gate"] == gate


def test_ad_doc_dict_copies_variants_and_treats_empty_gate_as_missing() -> None:
    variants = [{"aspect_ratio": "1:1"}]
    doc = ad_doc_dict(
        **_doc_kwargs(_brief()),  # type: ignore[arg-type]
        variants=variants,
        gate={},
    )
    # Copia defensiva: mutar la lista de entrada no altera el documento.
    variants.append({"aspect_ratio": "16:9"})
    assert doc["variants"] == [{"aspect_ratio": "1:1"}]
    # Borde anclado: `gate or {...}` trata el dict vacío como ausente.
    assert doc["gate"] == {"state": "drafting"}


def test_ad_doc_dict_allows_storyboard_without_script() -> None:
    script = compose_script(_brief())
    storyboard = build_storyboard(script, ad_id="ad123")
    doc = ad_doc_dict(**_doc_kwargs(_brief()), storyboard=storyboard)  # type: ignore[arg-type]
    assert doc["script"] is None
    assert doc["storyboard"]["ad_id"] == "ad123"  # type: ignore[index]
    assert doc["storyboard"]["shots"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# 6. Caracterización: suelo de escena corta el objetivo (bug documentado)
# ---------------------------------------------------------------------------


def test_compose_script_short_target_hits_scene_floor_and_overshoots() -> None:
    # CARACTERIZACIÓN (bug documentado, NO parcheado): el comentario de
    # `compose_script` promete "la suma sea exactamente el objetivo", pero con
    # `target_duration_s` < 8s (4 escenas x suelo de 2s) el suelo domina y el
    # guion termina en 8.0s, por encima del objetivo de 5.0s.
    script = compose_script(_brief(target_duration_s=5.0))
    assert [scene.duration_s for scene in script.scenes] == [2.0, 2.0, 2.0, 2.0]
    assert script.total_duration_s == 8.0
