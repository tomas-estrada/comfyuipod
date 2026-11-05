"""Voz en español y pronunciación (plan Ultra, W19 / A21).

Lexicon colombiano explícito (acento no inferido del idioma); pistas por
hablante; ASR como indicador. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.voice import (
    PronunciationLexicon,
    Utterance,
    VoiceError,
    VoiceTrack,
    assert_approved_variant,
    sync_report,
)

SAMPLE = ["Envigado", "Sabaneta", "Itagüí", "4 Sur", "Las Palmas"]


def _paisa_lexicon() -> PronunciationLexicon:
    lex = PronunciationLexicon(locale="es-CO-paisa", approved_variant=True)
    for place in SAMPLE:
        lex.add(place, f"/{place.lower()}/")
    return lex


def test_approved_variant_with_coverage_passes() -> None:
    assert_approved_variant(_paisa_lexicon(), SAMPLE)  # no lanza


def test_mere_language_support_is_not_an_accent() -> None:
    """A21: no inferir acento paisa por idioma español."""
    base = PronunciationLexicon(locale="es", approved_variant=False)
    for place in SAMPLE:
        base.add(place, f"/{place}/")
    with pytest.raises(VoiceError, match="NO variante aprobada"):
        assert_approved_variant(base, SAMPLE)


def test_approved_variant_without_coverage_fails() -> None:
    """Una variante aprobada pero sin datos suficientes no acredita el acento."""
    lex = PronunciationLexicon(locale="es-CO-paisa", approved_variant=True)
    lex.add("Envigado", "/envigado/")  # solo 1 de 5
    with pytest.raises(VoiceError, match="cobertura"):
        assert_approved_variant(lex, SAMPLE)


def test_lexicon_is_correctable() -> None:
    lex = _paisa_lexicon()
    lex.add("Envigado", "/en.bi.ˈɣa.ðo/")  # corrección
    assert lex.entries["envigado"] == "/en.bi.ˈɣa.ðo/"


def test_voice_track_rejects_mixed_speakers() -> None:
    """Pistas separadas por hablante (W19: no generación conjunta)."""
    track = VoiceTrack(
        speaker="ana",
        utterances=[Utterance(speaker="carlos", text="hola", start_s=0.0, end_s=1.0)],
    )
    with pytest.raises(VoiceError, match="mezcla de hablantes"):
        track.validate()


def test_voice_track_rejects_overlapping_turns() -> None:
    track = VoiceTrack(
        speaker="ana",
        utterances=[
            Utterance(speaker="ana", text="uno", start_s=0.0, end_s=2.0),
            Utterance(speaker="ana", text="dos", start_s=1.0, end_s=3.0),  # solapa
        ],
    )
    with pytest.raises(VoiceError, match="solapados"):
        track.validate()


def test_valid_track_passes() -> None:
    track = VoiceTrack(
        speaker="ana",
        utterances=[
            Utterance(speaker="ana", text="uno", start_s=0.0, end_s=1.0),
            Utterance(speaker="ana", text="dos", start_s=1.0, end_s=2.0),
        ],
    )
    track.validate()  # no lanza


def test_sync_report_is_indicator_not_judge() -> None:
    """A21: las métricas ASR son indicadores, no juez perfecto."""
    u = Utterance(speaker="ana", text="el dron sobrevuela la ciudad", start_s=0.0, end_s=3.0)
    rep = sync_report(u, "el dron sobrevuela ciudad")
    assert 0.0 <= rep["word_match_ratio"] <= 1.0
    assert "NO es juez perfecto" in rep["note"]
