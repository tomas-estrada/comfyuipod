"""ASR sin audio real: parseo WhisperX/FunASR a modelo de datos (sin GPU)."""

from __future__ import annotations

import pytest

from comfy_studio.asr import (
    UNKNOWN_SPEAKER,
    merge_diarization_with_transcription,
    parse_asr_payload,
    parse_diarization_payload,
)

WHISPERX_FIJO = {
    "text": "Bienvenidos al estudio. Aquí presentamos el cierre.",
    "language": "es",
    "backend": "faster-whisper",
    "model": "large-v3",
    "simulated": False,
    "segments": [
        {
            "start": 0.0,
            "end": 1.8,
            "text": "Bienvenidos al estudio.",
            "no_speech_prob": 0.05,
            "words": [
                {"start": 0.0, "end": 0.5, "word": "Bienvenidos", "probability": 0.98},
                {"start": 0.5, "end": 1.0, "word": "al", "probability": 0.97},
                {"start": 1.0, "end": 1.8, "word": "estudio.", "probability": 0.96},
            ],
        },
        {
            "start": 2.0,
            "end": 3.5,
            "text": "Aquí presentamos el cierre.",
            "words": [
                {"start": 2.0, "end": 2.4, "word": "Aquí", "probability": 0.95},
                {"start": 2.4, "end": 3.5, "word": "presentamos", "probability": 0.94},
            ],
        },
    ],
    "words": [
        {"start": 0.0, "end": 0.5, "word": "Bienvenidos", "probability": 0.98},
        {"start": 2.0, "end": 2.4, "word": "Aquí", "probability": 0.95},
    ],
}

FUNASR_FIJO = {
    "backend": "funasr_campplus",
    "simulated": False,
    "speakers": [{"id": "SPEAKER_00", "total_sec": 1.8, "turns": 1}],
    "turns": [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.8},
        {"speaker": "SPEAKER_01", "start": 2.0, "end": 3.5},
    ],
    "by_speaker": {"SPEAKER_00": {"total_sec": 1.8, "turns": 1}},
}


def test_parse_whisperx_segmentos_tiempos_y_palabras() -> None:
    parsed = parse_asr_payload(WHISPERX_FIJO)
    assert parsed["language"] == "es"
    assert parsed["text"] == "Bienvenidos al estudio. Aquí presentamos el cierre."
    assert len(parsed["segments"]) == 2
    seg0 = parsed["segments"][0]
    assert seg0["id"] == "seg-001"
    assert seg0["start"] == 0.0
    assert seg0["end"] == 1.8
    assert seg0["text"] == "Bienvenidos al estudio."
    assert seg0["no_speech_prob"] == pytest.approx(0.05)
    assert [w["word"] for w in seg0["words"]] == ["Bienvenidos", "al", "estudio."]
    assert seg0["words"][0]["probability"] == pytest.approx(0.98)
    assert seg0["words"][0]["score"] == pytest.approx(0.98)
    assert len(parsed["words"]) == 5
    assert parsed["words"][0]["word"] == "Bienvenidos"
    assert parsed["words"][-1]["word"] == "presentamos"


def test_parse_whisperx_sin_palabras_no_inventa() -> None:
    parsed = parse_asr_payload(
        {
            "text": "Sin palabras.",
            "language": "es",
            "segments": [{"start": 0.0, "end": 1.0, "text": "Sin palabras."}],
        }
    )
    assert parsed["segments"][0]["words"] == []
    assert parsed["words"] == []
    assert parsed["text"] == "Sin palabras."


def test_parse_whisperx_tiempo_invalido_tipado() -> None:
    with pytest.raises(ValueError, match="debe ser >"):
        parse_asr_payload(
            {"segments": [{"start": 2.0, "end": 1.0, "text": "mal"}]}
        )
    with pytest.raises(ValueError, match="debe ser >"):
        parse_asr_payload(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "mal",
                        "words": [{"start": 1.0, "end": 0.5, "word": "x"}],
                    }
                ]
            }
        )


def test_parse_funasr_turnos_y_resumen() -> None:
    parsed = parse_diarization_payload(FUNASR_FIJO)
    assert parsed["turns"] == [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.8},
        {"speaker": "SPEAKER_01", "start": 2.0, "end": 3.5},
    ]
    assert parsed["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert parsed["by_speaker"]["SPEAKER_00"] == {"total_sec": 1.8, "turns": 1}
    assert parsed["by_speaker"]["SPEAKER_01"] == {"total_sec": 1.5, "turns": 1}


def test_parse_funasr_vacio_y_tiempo_invalido() -> None:
    assert parse_diarization_payload({}) == {
        "speakers": [],
        "turns": [],
        "by_speaker": {},
    }
    with pytest.raises(ValueError, match="debe ser >"):
        parse_diarization_payload({"turns": [{"speaker": "SPEAKER_00", "start": 3.0, "end": 2.0}]})


def test_diarizacion_ausente_hablante_desconocido_no_inventado() -> None:
    parsed = parse_asr_payload(WHISPERX_FIJO)
    merged = merge_diarization_with_transcription(parsed, [])
    assert merged["speakers"] == [UNKNOWN_SPEAKER]
    assert merged["segments"][0]["speaker"] == UNKNOWN_SPEAKER
    assert all(w["speaker"] == UNKNOWN_SPEAKER for w in merged["words"])
    assert UNKNOWN_SPEAKER in merged["by_speaker"]
    assert "SPEAKER_00" not in merged["by_speaker"]


def test_merge_con_turnos_asigna_hablante_por_intervalo() -> None:
    parsed = parse_asr_payload(WHISPERX_FIJO)
    turns = parse_diarization_payload(FUNASR_FIJO)["turns"]
    merged = merge_diarization_with_transcription(parsed, turns)
    assert merged["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert merged["segments"][0]["speaker"] == "SPEAKER_00"
    assert merged["segments"][1]["speaker"] == "SPEAKER_01"
    assert all(w["speaker"] in ("SPEAKER_00", "SPEAKER_01") for w in merged["words"])
    assert merged["by_speaker"]["SPEAKER_00"]["word_count"] == 3
    assert merged["by_speaker"]["SPEAKER_01"]["word_count"] == 2
