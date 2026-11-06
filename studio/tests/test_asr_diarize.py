"""Diarización pura (sin audio/GPU/red): asignación de hablantes y bordes de fusión.

Cubre funciones puras de `comfy_studio.asr.diarize`:

- `_assign_speakers_from_spans`: clustering 1D determinista por frecuencia.
- `_find_best_speaker_for_interval`: elección por solape o vecindad al punto medio.
- `parse_diarization_payload` y `merge_diarization_with_transcription`: bordes
  complementarios a los ya cubiertos en `test_asr_parse.py`.

Sin subprocess, sin red, sin GPU y sin lectura de pesos: todas las entradas son
dicts/listas en memoria.
"""

from __future__ import annotations

import pytest

from comfy_studio.asr import (
    UNKNOWN_SPEAKER,
    merge_diarization_with_transcription,
    parse_diarization_payload,
)
from comfy_studio.asr.diarize import _assign_speakers_from_spans, _find_best_speaker_for_interval

# --- _assign_speakers_from_spans -------------------------------------------------


def test_assign_spans_vacios_devuelve_vacio() -> None:
    assert _assign_speakers_from_spans([]) == []


def test_assign_span_unico_speaker00_con_redondeo_y_freq_por_defecto() -> None:
    # Ruta de un solo tramo: siempre SPEAKER_00, freq ausente → 190.0, tiempos a 3 decimales.
    turns = _assign_speakers_from_spans([{"start": 0.123456, "end": 2.9999}])
    assert turns == [
        {"speaker": "SPEAKER_00", "start": 0.123, "end": 3.0, "freq_hz": 190.0}
    ]


def test_assign_span_unico_ignora_expected_speakers() -> None:
    # Con un solo tramo no hay clustering que controlar: expected_speakers no cambia nada.
    spans = [{"start": 0.0, "end": 1.0, "freq_hz": 330.0}]
    turns = _assign_speakers_from_spans(spans, expected_speakers=3)
    assert turns == [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0, "freq_hz": 330.0}
    ]


def test_assign_span_unico_freq_cero_o_ausente_usa_190() -> None:
    # `freq_hz or 190.0`: 0 y ausente son falsy y caen al default de la ruta única.
    sin_freq = _assign_speakers_from_spans([{"start": 0.0, "end": 1.0}])
    freq_cero = _assign_speakers_from_spans(
        [{"start": 0.0, "end": 1.0, "freq_hz": 0}]
    )
    assert sin_freq[0]["freq_hz"] == 190.0
    assert freq_cero[0]["freq_hz"] == 190.0


def test_assign_multi_sin_freq_usa_200_y_round_robin() -> None:
    # Ruta multi-tramo: freq ausente → 200.0 (no 190.0) y frecuencias iguales → round-robin.
    turns = _assign_speakers_from_spans(
        [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]
    )
    assert [t["speaker"] for t in turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert [t["freq_hz"] for t in turns] == [200.0, 200.0]


def test_assign_multi_freq_cero_o_none_usa_200() -> None:
    # `freq_hz or 200.0`: 0 y None son falsy y caen al default de la ruta multi.
    turns = _assign_speakers_from_spans(
        [
            {"start": 0.0, "end": 1.0, "freq_hz": 0},
            {"start": 1.0, "end": 2.0, "freq_hz": None},
        ]
    )
    assert [t["freq_hz"] for t in turns] == [200.0, 200.0]
    assert [t["speaker"] for t in turns] == ["SPEAKER_00", "SPEAKER_01"]


def test_assign_frecuencias_cercas_round_robin() -> None:
    # Spread < 35 Hz → round-robin por índice (0,1,0), no por valor de frecuencia.
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 200.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 201.0},
        {"start": 2.0, "end": 3.0, "freq_hz": 200.0},
    ]
    turns = _assign_speakers_from_spans(spans)
    assert [t["speaker"] for t in turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_assign_frecuencias_separadas_agrupa_por_bucket() -> None:
    # Spread ≥ 35 Hz → buckets lineales; el máximo (310) se recalva al último bucket.
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 120.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 130.0},
        {"start": 2.0, "end": 3.0, "freq_hz": 300.0},
        {"start": 3.0, "end": 4.0, "freq_hz": 310.0},
    ]
    turns = _assign_speakers_from_spans(spans)
    assert [t["speaker"] for t in turns] == [
        "SPEAKER_00",
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_01",
    ]


def test_assign_spread_exactamente_35_usa_buckets() -> None:
    # Frontera: spread == 35.0 entra en buckets ([0,0,1]); round-robin daría [0,1,0].
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 100.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 100.0},
        {"start": 2.0, "end": 3.0, "freq_hz": 135.0},
    ]
    turns = _assign_speakers_from_spans(spans)
    assert [t["speaker"] for t in turns] == [
        "SPEAKER_00",
        "SPEAKER_00",
        "SPEAKER_01",
    ]


def test_assign_spread_34_9_usa_round_robin() -> None:
    # Frontera por debajo: spread == 34.0 < 35.0 → round-robin ([0,1,0]).
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 100.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 100.0},
        {"start": 2.0, "end": 3.0, "freq_hz": 134.0},
    ]
    turns = _assign_speakers_from_spans(spans)
    assert [t["speaker"] for t in turns] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]


def test_assign_renumera_por_primera_aparicion() -> None:
    # SPEAKER_00 es el primer tramo en aparecer, aunque sea el de mayor frecuencia.
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 300.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 100.0},
    ]
    turns = _assign_speakers_from_spans(spans)
    assert [t["speaker"] for t in turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert [t["freq_hz"] for t in turns] == [300.0, 100.0]


def test_assign_expected_speakers_uno_forzado() -> None:
    # expected_speakers=1 desactiva el clustering aunque el spread sea enorme.
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 100.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 300.0},
    ]
    turns = _assign_speakers_from_spans(spans, expected_speakers=1)
    assert [t["speaker"] for t in turns] == ["SPEAKER_00", "SPEAKER_00"]
    assert [t["freq_hz"] for t in turns] == [100.0, 300.0]


def test_assign_expected_speakers_negativo_se_comprime_a_uno() -> None:
    # int(-3) → clamp a 1 → ruta k==1 (que usa el default de freq 190.0, no 200.0).
    spans = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]
    turns = _assign_speakers_from_spans(spans, expected_speakers=-3)
    assert [t["speaker"] for t in turns] == ["SPEAKER_00", "SPEAKER_00"]
    assert turns[0]["freq_hz"] == 190.0


def test_assign_expected_speakers_cero_es_equivalente_a_none() -> None:
    # 0 es falsy: `expected_speakers or default` lo descarta y usa el default (2 tramos → 2).
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 200.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 201.0},
    ]
    assert _assign_speakers_from_spans(spans, expected_speakers=0) == (
        _assign_speakers_from_spans(spans)
    )
    assert [t["speaker"] for t in _assign_speakers_from_spans(spans)] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]


def test_assign_expected_speakers_se_limita_a_6() -> None:
    # Clamp superior: 10 → 6 clústeres; el séptimo tramo envuelve al primero.
    spans = [
        {"start": float(i), "end": float(i) + 0.5, "freq_hz": 200.0} for i in range(7)
    ]
    turns = _assign_speakers_from_spans(spans, expected_speakers=10)
    assert [t["speaker"] for t in turns] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_02",
        "SPEAKER_03",
        "SPEAKER_04",
        "SPEAKER_05",
        "SPEAKER_00",
    ]


def test_assign_expected_tres_buckets_por_frecuencia() -> None:
    # expected_speakers=3 con spread ≥ 35 Hz → tres buckets lineales (0,1,2).
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 100.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 150.0},
        {"start": 2.0, "end": 3.0, "freq_hz": 200.0},
    ]
    turns = _assign_speakers_from_spans(spans, expected_speakers=3)
    assert [t["speaker"] for t in turns] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_02",
    ]


def test_assign_redondeo_de_tiempos_y_frecuencia() -> None:
    # Tiempos → 3 decimales; freq → 1 decimal (150.44→150.4, 150.46→150.5).
    spans = [
        {"start": 0.123456, "end": 2.9999, "freq_hz": 150.44},
        {"start": 3.0, "end": 4.0, "freq_hz": 150.46},
    ]
    turns = _assign_speakers_from_spans(spans)
    assert turns[0] == {
        "speaker": "SPEAKER_00",
        "start": 0.123,
        "end": 3.0,
        "freq_hz": 150.4,
    }
    assert turns[1]["freq_hz"] == 150.5
    assert (turns[1]["start"], turns[1]["end"]) == (3.0, 4.0)


def test_assign_es_determinista_con_misma_entrada() -> None:
    # Misma entrada → misma salida byte a byte (sin aleatoriedad ni estado).
    spans = [
        {"start": 0.0, "end": 1.0, "freq_hz": 140.0},
        {"start": 1.0, "end": 2.0, "freq_hz": 260.0},
        {"start": 2.0, "end": 3.0, "freq_hz": 141.0},
    ]
    primero = _assign_speakers_from_spans(spans)
    segundo = _assign_speakers_from_spans(spans)
    assert primero == segundo
    assert len(primero) == 3


# --- _find_best_speaker_for_interval ---------------------------------------------


def test_find_sin_turnos_devuelve_desconocido() -> None:
    # Sin turnos nunca se inventa un hablante.
    assert _find_best_speaker_for_interval(0.0, 1.0, []) == UNKNOWN_SPEAKER


def test_find_intervalo_dentro_de_un_turno() -> None:
    turns = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0}]
    assert _find_best_speaker_for_interval(0.5, 1.5, turns) == "SPEAKER_00"


def test_find_mayor_solape_gana_sobre_vecindad_al_medio() -> None:
    # SPEAKER_00 solapa 10 s y SPEAKER_01 solo 1 s, pero el punto medio (5.0)
    # cae dentro de SPEAKER_01: gana el solape, no la vecindad.
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
        {"speaker": "SPEAKER_01", "start": 4.5, "end": 5.5},
    ]
    assert _find_best_speaker_for_interval(0.0, 10.0, turns) == "SPEAKER_00"


def test_find_solape_empate_gana_el_primero() -> None:
    # Empate de solape (0.5 vs 0.5): estrictamente mayor conserva el primer turno.
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
        {"speaker": "SPEAKER_01", "start": 3.0, "end": 5.0},
    ]
    assert _find_best_speaker_for_interval(1.5, 3.5, turns) == "SPEAKER_00"


def test_find_sin_solape_elige_mas_cercano_al_medio() -> None:
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
        {"speaker": "SPEAKER_01", "start": 10.0, "end": 11.0},
    ]
    # Medio 4.5: distancias 3.5 vs 5.5 → SPEAKER_00.
    assert _find_best_speaker_for_interval(4.0, 5.0, turns) == "SPEAKER_00"


def test_find_cercania_empate_gana_el_primero() -> None:
    # Empate de vecindad (3.0 vs 3.0): min() estable conserva el primer turno.
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
        {"speaker": "SPEAKER_01", "start": 8.0, "end": 10.0},
    ]
    assert _find_best_speaker_for_interval(4.0, 6.0, turns) == "SPEAKER_00"


def test_find_toques_de_borde_no_cuentan_solape() -> None:
    # [2,3] toca los bordes de ambos turnos sin solapar (solape 0): vecindad con
    # empate → primer turno. Un borde compartido no es solape.
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
        {"speaker": "SPEAKER_01", "start": 3.0, "end": 5.0},
    ]
    assert _find_best_speaker_for_interval(2.0, 3.0, turns) == "SPEAKER_00"


def test_find_intervalo_cero_largo_en_borde_del_turno() -> None:
    # Intervalo degenerado [2,2] sobre el borde: solape 0, vecindad exacta → ese turno.
    turns = [{"speaker": "SPEAKER_00", "start": 2.0, "end": 4.0}]
    assert _find_best_speaker_for_interval(2.0, 2.0, turns) == "SPEAKER_00"


def test_find_turno_sin_tiempos_se_trata_como_cero() -> None:
    # `turn.get("start"/"end", 0.0)`: un turno sin tiempos vive en t=0 y es candidato.
    turns = [{"speaker": "SPEAKER_00"}]
    assert _find_best_speaker_for_interval(0.5, 1.5, turns) == "SPEAKER_00"


def test_find_turno_sin_speaker_en_vecindad_desconocido() -> None:
    # En la ruta de vecindad, `nearest.get("speaker") or UNKNOWN` no inventa hablante.
    turns = [{"start": 5.0, "end": 6.0}]
    assert _find_best_speaker_for_interval(0.0, 1.0, turns) == UNKNOWN_SPEAKER


# --- parse_diarization_payload (bordes complementarios a test_asr_parse.py) ------


def test_parse_turno_no_objeto_tipado() -> None:
    with pytest.raises(ValueError, match="no es un objeto"):
        parse_diarization_payload({"turns": ["no-dict"]})


def test_parse_sin_end_es_invalido() -> None:
    # end ausente → end = start → `end > start` falla (nunca se inventa duración).
    with pytest.raises(ValueError, match="debe ser >"):
        parse_diarization_payload(
            {"turns": [{"speaker": "SPEAKER_00", "start": 1.0}]}
        )


def test_parse_end_igual_a_start_invalido() -> None:
    # Duración cero también es inválida (la regla es estrictamente >).
    with pytest.raises(ValueError, match="debe ser >"):
        parse_diarization_payload(
            {"turns": [{"speaker": "SPEAKER_00", "start": 3.0, "end": 3.0}]}
        )


def test_parse_speaker_ausente_o_vacio_es_desconocido() -> None:
    parsed = parse_diarization_payload(
        {
            "turns": [
                {"start": 0.0, "end": 1.0},
                {"speaker": "", "start": 1.0, "end": 2.0},
            ]
        }
    )
    assert [t["speaker"] for t in parsed["turns"]] == [
        UNKNOWN_SPEAKER,
        UNKNOWN_SPEAKER,
    ]
    assert parsed["speakers"] == [UNKNOWN_SPEAKER]
    assert parsed["by_speaker"][UNKNOWN_SPEAKER] == {"total_sec": 2.0, "turns": 2}


def test_parse_orden_speakers_redondeo_y_acumulacion() -> None:
    # speakers/by_speaker siempre ordenados; tiempos a 3 decimales; total_sec
    # se acumula redondeando en cada turno (no al final).
    parsed = parse_diarization_payload(
        {
            "turns": [
                {"speaker": "SPEAKER_01", "start": 0.0, "end": 1.0},
                {"speaker": "SPEAKER_00", "start": 0.123456, "end": 1.123456},
                {"speaker": "SPEAKER_00", "start": 2.0, "end": 2.001},
            ]
        }
    )
    assert parsed["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert list(parsed["by_speaker"]) == ["SPEAKER_00", "SPEAKER_01"]
    assert parsed["turns"][1] == {
        "speaker": "SPEAKER_00",
        "start": 0.123,
        "end": 1.123,
    }
    assert parsed["by_speaker"]["SPEAKER_00"]["turns"] == 2
    assert parsed["by_speaker"]["SPEAKER_00"]["total_sec"] == pytest.approx(1.001)
    assert parsed["by_speaker"]["SPEAKER_01"] == {"total_sec": 1.0, "turns": 1}


# --- merge_diarization_with_transcription (bordes complementarios) -----------------


def test_merge_sin_segmentos_devuelve_estructura_vacia() -> None:
    assert merge_diarization_with_transcription({}, []) == {
        "segments": [],
        "words": [],
        "speakers": [],
        "by_speaker": {},
    }


def test_merge_palabra_con_speaker_explicito_prevalece() -> None:
    # Un speaker ya asignado en la palabra no se re-asigna por solape.
    turns = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 6.0}]
    merged = merge_diarization_with_transcription(
        {
            "segments": [
                {
                    "start": 4.0,
                    "end": 5.0,
                    "text": "hola",
                    "words": [
                        {
                            "start": 4.0,
                            "end": 5.0,
                            "word": "hola",
                            "speaker": "SPEAKER_99",
                        }
                    ],
                }
            ]
        },
        turns,
    )
    assert merged["words"][0]["speaker"] == "SPEAKER_99"
    assert merged["segments"][0]["speaker"] == "SPEAKER_99"
    assert merged["speakers"] == ["SPEAKER_99"]
    assert "SPEAKER_00" not in merged["by_speaker"]


def test_merge_mayoria_de_palabras_define_speaker_del_segmento() -> None:
    merged = merge_diarization_with_transcription(
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "x",
                    "words": [
                        {"start": 0.0, "end": 0.5, "word": "a", "speaker": "SPEAKER_00"},
                        {"start": 0.5, "end": 1.0, "word": "b", "speaker": "SPEAKER_01"},
                        {"start": 1.0, "end": 1.5, "word": "c", "speaker": "SPEAKER_00"},
                    ],
                }
            ]
        },
        [],
    )
    assert merged["segments"][0]["speaker"] == "SPEAKER_00"
    assert [w["speaker"] for w in merged["words"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]
    # by_speaker se agrega a nivel de segmento: word_count suma TODAS las palabras
    # del segmento atribuido a SPEAKER_00 (incluida la de SPEAKER_01) y SPEAKER_01
    # no recibe bucket porque ningún segmento se le atribuyó.
    assert merged["by_speaker"]["SPEAKER_00"]["word_count"] == 3
    assert merged["speakers"] == ["SPEAKER_00"]
    assert "SPEAKER_01" not in merged["by_speaker"]


def test_merge_empate_de_mayoria_gana_el_primer_hablante() -> None:
    # max() sobre conteos empatados conserva el primer hablante visto en la palabra.
    def _speaker_de_segmento(word_speakers: list[str]) -> str:
        merged = merge_diarization_with_transcription(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": "x",
                        "words": [
                            {
                                "start": 0.0,
                                "end": 1.0,
                                "word": "w",
                                "speaker": spk,
                            }
                            for spk in word_speakers
                        ],
                    }
                ]
            },
            [],
        )
        return merged["segments"][0]["speaker"]

    assert _speaker_de_segmento(["SPEAKER_00", "SPEAKER_01"]) == "SPEAKER_00"
    assert _speaker_de_segmento(["SPEAKER_01", "SPEAKER_00"]) == "SPEAKER_01"


def test_merge_segmento_sin_palabras_usa_intervalo() -> None:
    # Sin palabras no hay conteo: se busca el turno por solape/vecindad del segmento.
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
        {"speaker": "SPEAKER_01", "start": 10.0, "end": 11.0},
    ]
    merged = merge_diarization_with_transcription(
        {"segments": [{"start": 4.0, "end": 5.0, "text": "x"}]}, turns
    )
    assert merged["segments"][0]["speaker"] == "SPEAKER_00"
    assert merged["by_speaker"]["SPEAKER_00"]["word_count"] == 0


def test_merge_segmento_sin_palabras_con_speaker_explicito() -> None:
    # El speaker explícito del segmento se respeta y no se consulta ningún turno.
    turns = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 6.0}]
    merged = merge_diarization_with_transcription(
        {"segments": [{"start": 4.0, "end": 5.0, "text": "x", "speaker": "SPEAKER_07"}]},
        turns,
    )
    assert merged["segments"][0]["speaker"] == "SPEAKER_07"
    assert merged["speakers"] == ["SPEAKER_07"]


def test_merge_palabra_sin_tiempos_usa_limites_del_segmento() -> None:
    # `w.get("start"/"end", s_start/s_end)`: la palabra sin tiempos se evalúa
    # sobre el intervalo completo del segmento (aquí solapa SPEAKER_00).
    turns = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 6.0},
        {"speaker": "SPEAKER_01", "start": 10.0, "end": 11.0},
    ]
    merged = merge_diarization_with_transcription(
        {"segments": [{"start": 4.0, "end": 5.0, "text": "x", "words": [{"word": "hola"}]}]},
        turns,
    )
    assert merged["words"][0]["speaker"] == "SPEAKER_00"


def test_merge_acumula_duracion_texto_y_rangos() -> None:
    # by_speaker acumula duración redondeada, word_count y el rango con texto/id.
    merged = merge_diarization_with_transcription(
        {
            "segments": [
                {
                    "id": "seg-001",
                    "start": 0.0,
                    "end": 1.8,
                    "text": "Bienvenidos",
                    "words": [
                        {"start": 0.0, "end": 0.5, "word": "Bienvenidos", "speaker": "SPEAKER_00"}
                    ],
                }
            ]
        },
        [],
    )
    bucket = merged["by_speaker"]["SPEAKER_00"]
    assert bucket["total_duration_sec"] == pytest.approx(1.8)
    assert bucket["word_count"] == 1
    assert bucket["ranges"] == [
        {"start": 0.0, "end": 1.8, "text": "Bienvenidos", "segment_id": "seg-001"}
    ]
    # La copia del segmento conserva sus claves originales además de `speaker`.
    assert merged["segments"][0]["id"] == "seg-001"


def test_merge_segmento_invertido_no_valida_y_acumula_cero() -> None:
    # A diferencia de parse_diarization_payload, merge no valida tiempos:
    # end < start acumula duración 0 (comportamiento actual, sin parchear).
    merged = merge_diarization_with_transcription(
        {"segments": [{"start": 5.0, "end": 1.0, "text": "x"}]}, []
    )
    assert merged["segments"][0]["speaker"] == UNKNOWN_SPEAKER
    assert merged["by_speaker"][UNKNOWN_SPEAKER]["total_duration_sec"] == 0.0
