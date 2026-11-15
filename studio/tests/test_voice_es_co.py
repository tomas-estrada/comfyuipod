"""Deterministic Spanish-Colombian text preparation; no ASR/TTS claims (C27)."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.voice import VoiceError
from comfy_studio.voice_es_co import normalize_es_co_text, number_to_words

FIXTURE = Path(__file__).parent / "fixtures" / "c27_voice_es_co_acceptance.json"


def _expected_report(fixture: dict) -> dict:
    remaining = {key: value for key, value in fixture.items() if key != "text"}
    return {"raw_text": fixture["text"], **remaining}


def test_es_co_acceptance_fixture_exact_output_and_provenance() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    report = normalize_es_co_text(expected["text"]).to_dict()
    assert report == _expected_report(expected)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "cero"),
        (21, "veintiuno"),
        (21_000, "veintiún mil"),
        (31_000, "treinta y un mil"),
        (21_000_000, "veintiún millones"),
        (31_000_000, "treinta y un millones"),
        (101_000, "ciento un mil"),
        (1_000_000, "un millón"),
        (1_000_000_000, "mil millones"),
        (999_999_999_999, "novecientos noventa y nueve mil novecientos noventa y nueve millones "
         "novecientos noventa y nueve mil novecientos noventa y nueve"),
    ],
)
def test_integer_to_words_spanish_group_boundaries(value: int, expected: str) -> None:
    assert number_to_words(value) == expected


@pytest.mark.parametrize(
    "text",
    [
        "fecha 12/10/26",
        "fecha 29/02/2027",
        "fecha 00/01/2026",
        "fecha 01/01/0000",
        "valor 3.5",
        "valor 0012",
        "valor 012.000",
        "valor .5",
        "valor 0.123",
        "valor ٠١٢",
        "valor " + ("9" * 4500),
    ],
)
def test_ambiguous_or_invalid_numeric_patterns_fail_closed(text: str) -> None:
    with pytest.raises(VoiceError):
        normalize_es_co_text(text)


def test_leap_day_and_nfc_spelling_are_preserved() -> None:
    decomposed = unicodedata.normalize("NFD", "Itagüí")
    report = normalize_es_co_text(f"{decomposed}, 29/02/2024")
    assert report.raw_text == "Itagüí, 29/02/2024"
    assert report.spoken_text == "Itagüí, veintinueve de febrero de dos mil veinticuatro"
    assert report.lexicon_matches[0].name == "Itagüí"


def test_spanish_opening_punctuation_and_closing_quotes_preserve_matches_and_pauses() -> None:
    report = normalize_es_co_text("¿12/10/2026? ¡Sabaneta! «Itagüí.»")
    assert report.spoken_text == (
        "¿doce de octubre de dos mil veintiséis? ¡Sabaneta! «Itagüí.»"
    )
    assert [match.name for match in report.lexicon_matches] == ["Sabaneta", "Itagüí"]
    assert [(pause.marker, pause.type) for pause in report.pause_markers] == [
        ("?", "question"),
        ("!", "exclamation"),
        (".", "full_stop"),
    ]


def test_empty_text_is_rejected() -> None:
    with pytest.raises(VoiceError, match="vacío"):
        normalize_es_co_text("  \n\t")


def test_project_route_normalizes_fixture_and_discloses_limits(client: TestClient) -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    project_id = client.post("/studio/api/projects", json={"name": "Voz C27"}).json()["id"]
    response = client.post(
        f"/studio/api/projects/{project_id}/voice/lexicons/es-CO/normalize",
        json={"text": expected["text"]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == _expected_report(expected)
    assert client.get(f"/studio/api/projects/{project_id}/voice").json()["synthesis"] == "not_run"


def test_project_route_rejects_unknown_locale_invalid_text_and_missing_project(
    client: TestClient,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Voz inválida"}).json()["id"]
    base = f"/studio/api/projects/{project_id}/voice/lexicons"
    unsupported = client.post(f"{base}/es-ES/normalize", json={"text": "hola"})
    assert unsupported.status_code == 422
    invalid = client.post(f"{base}/es-CO/normalize", json={"text": "fecha 29/02/2027"})
    assert invalid.status_code == 422
    missing = client.post(
        "/studio/api/projects/no-such-project/voice/lexicons/es-CO/normalize",
        json={"text": "hola"},
    )
    assert missing.status_code == 404


def test_project_route_rejects_oversized_number_without_server_error(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Voz larga"}).json()["id"]
    response = client.post(
        f"/studio/api/projects/{project_id}/voice/lexicons/es-CO/normalize",
        json={"text": "9" * 4500},
    )
    assert response.status_code == 422, response.text
