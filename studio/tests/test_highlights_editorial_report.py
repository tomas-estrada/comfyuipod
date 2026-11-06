"""Tests CPU deterministas de las ramas editoriales de `edl/highlights.py`.

`test_hybrid_refinements.py` cubre solo `apply_hybrid_refinements` y
`_snap_to_scene_boundary`; aquí se cubre el resto de ramas con expectativas
derivadas de los docstrings del propio módulo:

- `_is_word_filler`: flag explícito, léxico español y tokens con espacios.
- `build_edl_cuts_from_transcription`: guard M3-SCR-019 de `speaker_filter`,
  `remove_fillers=False`, descarte por palabras muletilla (>= 2 y >= 25%),
  recorte de muletillas en extremos, cota `max_duration_sec` y fallback.
- `_collect_scene_boundaries`: synthetic, valores inválidos y deduplicación.
- `generate_highlights_edl`: happy path sin híbrido, ValueErrors, ramificación a
  `transcribe_project_asset` y las tres caras del modo `hybrid`.

Sin GPU, red, pesos, ffmpeg real ni LLM: `analyze_acoustic_energy` y
`transcribe_project_asset` se sustituyen por fakes en memoria y el estado vive
en un `ProjectStore` real sobre `tmp_path`. Varias aserciones anclan el
comportamiento ACTUAL documentado como candidato a bug (ver comentarios).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from comfy_studio.edl import highlights as highlights_mod
from comfy_studio.edl.highlights import (
    _collect_scene_boundaries,
    _is_word_filler,
    build_edl_cuts_from_transcription,
    generate_highlights_edl,
)
from comfy_studio.projects import ProjectStore

# ---------------------------------------------------------------------------
# Helpers (datos en memoria, sin mocks vacíos)
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path / "studio_data")


def _seg(
    start: float,
    end: float,
    text: str,
    *,
    speaker: str | None = None,
    words: list[dict[str, Any]] | None = None,
    is_filler_segment: bool = False,
) -> dict[str, Any]:
    seg: dict[str, Any] = {"start": start, "end": end, "text": text}
    if speaker is not None:
        seg["speaker"] = speaker
    if words is not None:
        seg["words"] = words
    if is_filler_segment:
        seg["is_filler_segment"] = True
    return seg


def _word(
    token: str,
    start: float,
    end: float,
    *,
    filler: bool | None = None,
) -> dict[str, Any]:
    word: dict[str, Any] = {"word": token, "start": start, "end": end}
    if filler is not None:
        word["is_filler"] = filler
    return word


def _add_asset(
    store: ProjectStore,
    project_id: str,
    *,
    filename: str = "clip.mp4",
    content_type: str = "video/mp4",
    category: str = "uploads",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return store.save_upload(
        project_id,
        filename=filename,
        content_type=content_type,
        data=b"fake-media-bytes",
        max_upload_bytes=1 << 20,
        category=category,
        metadata=metadata,
    )


def _seed_project_with_transcription(
    store: ProjectStore,
    segments: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    project = store.create_project(name="Highlights pruebas")
    pid = project["id"]
    asset = _add_asset(store, pid)
    tr = store.save_transcription(pid, asset_id=asset["id"], segments=segments)
    return pid, asset, tr


# ---------------------------------------------------------------------------
# _is_word_filler
# ---------------------------------------------------------------------------


def test_is_word_filler_detecta_flag_y_lexico() -> None:
    assert _is_word_filler({"is_filler": True})
    assert _is_word_filler({"word": "EH,"})
    assert _is_word_filler({"word": "¡este!"})
    assert _is_word_filler({"word": "mmm"})
    assert not _is_word_filler({"word": "hola"})
    assert not _is_word_filler({"word": "mundo", "is_filler": False})
    assert not _is_word_filler({})


def test_is_word_filler_no_detecta_token_con_espacios() -> None:
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL (candidato a bug, ver reporte):
    # highlights.py:43 no normaliza espacios, y `parse_asr_payload`
    # (asr/whisperx.py:436-443) no copia el flag `is_filler`, así que un token
    # real de whisperx con espacio inicial (" este") no se reconoce como muletilla.
    assert not _is_word_filler({"word": " este"})
    assert not _is_word_filler({"word": "este "})
    # Con el flag presente el espacio no importa:
    assert _is_word_filler({"word": " este", "is_filler": True})
    assert _is_word_filler({"word": "este"})


# ---------------------------------------------------------------------------
# build_edl_cuts_from_transcription: speaker_filter (guard M3-SCR-019)
# ---------------------------------------------------------------------------


def test_speaker_filter_descartado_no_altera_cadena_de_pausas() -> None:
    transcription = {
        "segments": [
            _seg(0.0, 2.0, "uno", speaker="SPK_A"),
            _seg(2.5, 6.0, "dos", speaker="SPK_B"),
            _seg(7.0, 8.0, "tres", speaker="SPK_A"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, speaker_filter="SPK_A")

    assert [c["text"] for c in result["cuts"]] == ["uno", "tres"]
    assert all(c["speaker"] == "SPK_A" for c in result["cuts"])
    # M3-SCR-019 (highlights.py:194-197): el segmento filtrado no actualiza
    # `prev_end` ni genera su propia entrada de pausa; la única pausa se mide de
    # superviviente a superviviente (2.0 -> 7.0), tragando el hueco del filtrado.
    assert result["removed_pauses"] == [
        {"in_sec": 2.0, "out_sec": 7.0, "duration_sec": 5.0, "reason": "eliminar_pausa"}
    ]
    assert result["total_duration_sec"] == 3.0
    assert result["notes"] == "Highlights: 2 cortes (3.00s). Pausas eliminadas: 1."


def test_speaker_filter_en_primer_segmento_no_deja_pausa_previa() -> None:
    transcription = {
        "segments": [
            _seg(0.0, 5.0, "otra voz", speaker="SPK_B"),
            _seg(5.5, 7.0, "voz elegida", speaker="SPK_A"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, speaker_filter="SPK_A")

    # El guard de highlights.py:196-199 está ANTES de la cadena de pausas: el
    # segmento filtrado no fija `prev_end`, así que no se registra la pausa
    # 5.0->5.5 que existiría si el descartado encadenara silencios.
    assert [c["text"] for c in result["cuts"]] == ["voz elegida"]
    assert result["removed_pauses"] == []
    assert result["total_duration_sec"] == 1.5


def test_speaker_filter_conserva_segmentos_sin_hablante() -> None:
    # Comportamiento actual: el guard de highlights.py:196 exige `s_speaker`
    # truthy; un segmento sin diarización sobrevive al filtro.
    transcription = {
        "segments": [
            _seg(0.0, 1.0, "sin diarisacion"),
            _seg(1.5, 2.5, "voz excluida", speaker="SPK_B"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, speaker_filter="SPK_A")

    assert [c["text"] for c in result["cuts"]] == ["sin diarisacion"]
    assert "speaker" not in result["cuts"][0]
    assert result["removed_pauses"] == []


# ---------------------------------------------------------------------------
# build_edl_cuts_from_transcription: remove_fillers / muletillas
# ---------------------------------------------------------------------------


def test_remove_fillers_false_conserva_muletillas() -> None:
    transcription = {
        "segments": [
            _seg(0.0, 1.0, "eh mmm este", is_filler_segment=True),
            _seg(
                1.5,
                3.0,
                "eh hola um",
                words=[
                    _word("eh", 1.5, 1.8, filler=True),
                    _word("hola", 1.8, 2.4),
                    _word("um", 2.4, 3.0, filler=True),
                ],
            ),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, remove_fillers=False)

    # Con remove_fillers=False la rama completa de highlights.py:210-271 se salta:
    # ni segmento muletilla se descarta ni se inspeccionan palabras.
    assert [c["text"] for c in result["cuts"]] == ["eh mmm este", "eh hola um"]
    assert (result["cuts"][1]["in_sec"], result["cuts"][1]["out_sec"]) == (1.5, 3.0)
    assert result["removed_fillers"] == []


def test_palabra_muletilla_descarta_con_dos_y_25_por_ciento() -> None:
    words = [
        _word("eh", 0.0, 0.25, filler=True),
        _word("hola", 0.25, 0.5),
        _word("mundo", 0.5, 0.75),
        _word("cruel", 0.75, 1.0),
        _word("este", 1.0, 1.25, filler=True),
        _word("mundo", 1.25, 1.5),
        _word("que", 1.5, 1.75),
        _word("ves", 1.75, 2.0),
    ]
    transcription = {
        "segments": [
            _seg(0.0, 2.0, "eh hola mundo cruel este mundo que ves", words=words),
            _seg(2.5, 3.5, "cierre normal"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription)

    # 2 muletillas sobre 8 palabras (25%): se cumplen AMBOS umbrales de
    # highlights.py:226-227 y el segmento entero se descarta.
    assert [c["text"] for c in result["cuts"]] == ["cierre normal"]
    assert result["removed_fillers"] == [
        {
            "in_sec": 0.0,
            "out_sec": 2.0,
            "text": "eh hola mundo cruel este mundo que ves",
            "reason": "eliminar_muletilla",
        }
    ]


def test_palabra_muletilla_conserva_si_falta_un_umbral() -> None:
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL: el comentario de
    # highlights.py:225 dice ">= 40% de las palabras ... o contiene >= 2
    # muletillas", pero el código (L226-227) exige AMBOS umbrales con 25%.
    tokens = ["uno", "dos", "tres", "cuatro", "eh", "seis", "siete", "um", "nueve", "diez"]
    words = [
        _word(tok, i * 0.2, (i + 1) * 0.2, filler=tok in {"eh", "um"})
        for i, tok in enumerate(tokens)
    ]
    seg_diez = _seg(0.0, 2.0, " ".join(tokens), words=words)
    result_diez = build_edl_cuts_from_transcription({"segments": [seg_diez]})

    # 2 muletillas en 10 palabras (20%): el ratio no llega a 25% -> se conserva.
    assert len(result_diez["cuts"]) == 1
    assert result_diez["removed_fillers"] == []
    assert (result_diez["cuts"][0]["in_sec"], result_diez["cuts"][0]["out_sec"]) == (0.0, 2.0)
    assert result_diez["cuts"][0]["text"] == "uno dos tres cuatro seis siete nueve diez"

    seg_dos = _seg(
        1.5,
        2.4,
        "eh hola",
        words=[_word("eh", 1.5, 1.8, filler=True), _word("hola", 1.8, 2.4)],
    )
    result_dos = build_edl_cuts_from_transcription({"segments": [seg_dos]})

    # 1 muletilla en 2 palabras (50%): el ratio llega pero el recuento no -> se
    # conserva y solo se recorta la muletilla inicial.
    assert len(result_dos["cuts"]) == 1
    assert (result_dos["cuts"][0]["in_sec"], result_dos["cuts"][0]["out_sec"]) == (1.8, 2.4)
    assert result_dos["cuts"][0]["text"] == "hola"
    assert result_dos["removed_fillers"] == [
        {"in_sec": 1.5, "out_sec": 1.8, "text": "eh", "reason": "eliminar_muletilla"}
    ]


def test_muletillas_de_extremos_se_recortan_y_se_reportan() -> None:
    tokens = ["eh", "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "um"]
    words = [
        _word(tok, i * 0.3, (i + 1) * 0.3, filler=tok in {"eh", "um"})
        for i, tok in enumerate(tokens)
    ]
    transcription = {
        "segments": [
            _seg(0.0, 3.0, " ".join(tokens), words=words),
            _seg(3.5, 4.5, "siguiente"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription)

    # 2 muletillas en 10 palabras (20%): el segmento no se descarta; el bucle de
    # highlights.py:239-271 recorta el extremo inicial y el final, uno por uno,
    # dejando cada recorte en `removed_fillers`.
    assert len(result["cuts"]) == 2
    cut = result["cuts"][0]
    assert (cut["in_sec"], cut["out_sec"]) == (0.3, 2.7)
    assert cut["text"] == "alpha beta gamma delta epsilon zeta eta theta"
    assert result["removed_fillers"] == [
        {"in_sec": 0.0, "out_sec": 0.3, "text": "eh", "reason": "eliminar_muletilla"},
        {"in_sec": 2.7, "out_sec": 3.0, "text": "um", "reason": "eliminar_muletilla"},
    ]


def test_muletilla_interior_fuera_del_texto_pero_dentro_del_tiempo() -> None:
    transcription = {
        "segments": [
            _seg(
                0.0,
                1.2,
                "hola este mundo",
                words=[
                    _word("hola", 0.0, 0.4),
                    _word("este", 0.4, 0.8, filler=True),
                    _word("mundo", 0.8, 1.2),
                ],
            )
        ]
    }
    result = build_edl_cuts_from_transcription(transcription)

    # Solo 1 muletilla de 3: no se descarta ni se recortan extremos, pero el
    # texto del corte excluye la muletilla interior (highlights.py:266-271).
    assert len(result["cuts"]) == 1
    assert (result["cuts"][0]["in_sec"], result["cuts"][0]["out_sec"]) == (0.0, 1.2)
    assert result["cuts"][0]["text"] == "hola mundo"
    assert result["removed_fillers"] == []


def test_segmento_solo_muletillas_descarta_por_palabras_limpias_vacias() -> None:
    transcription = {
        "segments": [
            _seg(0.0, 0.3, "eh", words=[_word("eh", 0.0, 0.3, filler=True)]),
            _seg(1.0, 2.0, "contenido real"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription)

    # highlights.py:226: `not clean_words` descarta aunque el recuento de
    # muletillas sea 1 (la condición de ratio no haría falta aquí).
    assert [c["text"] for c in result["cuts"]] == ["contenido real"]
    assert result["removed_fillers"] == [
        {"in_sec": 0.0, "out_sec": 0.3, "text": "eh", "reason": "eliminar_muletilla"}
    ]


# ---------------------------------------------------------------------------
# build_edl_cuts_from_transcription: max_duration_sec y fallback
# ---------------------------------------------------------------------------


def test_max_duration_sec_acorta_ultimo_corte_y_detiene() -> None:
    transcription = {
        "segments": [
            _seg(0.0, 3.0, "a"),
            _seg(3.5, 5.0, "b"),
            _seg(5.5, 9.0, "c"),
            _seg(9.5, 11.0, "d"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, max_duration_sec=5.0)

    # El tercer corte se recorta al remanente (0.5 s >= 0.25) y el cuarto se
    # corta con `break` (remanente 0.0 < 0.25), highlights.py:277-283.
    assert [(c["in_sec"], c["out_sec"]) for c in result["cuts"]] == [
        (0.0, 3.0),
        (3.5, 5.0),
        (5.5, 6.0),
    ]
    assert result["total_duration_sec"] == 5.0
    # Comportamiento actual: la pausa que precede al segmento rechazado por la
    # cota se registra igualmente (la comprobación de pausa, L199-208, precede
    # a la cota, L277-283).
    assert len(result["removed_pauses"]) == 3
    assert result["notes"] == "Highlights: 3 cortes (5.00s). Pausas eliminadas: 3."


def test_max_duration_sec_corta_antes_del_margen_minimo() -> None:
    transcription = {
        "segments": [
            _seg(0.0, 3.0, "a"),
            _seg(3.5, 5.0, "b"),
            _seg(5.5, 9.0, "c"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, max_duration_sec=4.7)

    # Remanente 0.2 s < 0.25 s: el segmento entero se rechaza y el bucle para.
    assert [(c["in_sec"], c["out_sec"]) for c in result["cuts"]] == [(0.0, 3.0), (3.5, 5.0)]
    assert result["total_duration_sec"] == 4.5


def test_fallback_no_respeta_max_duration_sec() -> None:
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL (candidato a bug, ver reporte): la
    # cota de highlights.py:277-283 rompe el bucle sin cortes y el fallback de
    # L301-316 reinserta el primer segmento completo, superando la cota pedida.
    transcription = {"segments": [_seg(0.0, 3.0, "unico segmento")]}
    result = build_edl_cuts_from_transcription(transcription, max_duration_sec=0.2)

    assert len(result["cuts"]) == 1
    assert (result["cuts"][0]["in_sec"], result["cuts"][0]["out_sec"]) == (0.0, 3.0)
    assert result["total_duration_sec"] == 3.0
    assert result["total_duration_sec"] > 0.2
    assert result["notes"] == "Highlights: 1 cortes (3.00s)."


def test_fallback_no_respeta_speaker_filter() -> None:
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL (candidato a bug, ver reporte): si
    # ningún segmento sobrevive al guard de highlights.py:196-197, el fallback
    # de L301-316 devuelve el primer segmento, la voz que el filtro excluía.
    transcription = {
        "segments": [
            _seg(0.0, 1.0, "voz excluida", speaker="SPK_B"),
            _seg(1.5, 2.5, "otra voz excluida", speaker="SPK_B"),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription, speaker_filter="SPK_A")

    assert len(result["cuts"]) == 1
    assert result["cuts"][0]["text"] == "voz excluida"
    assert "speaker" not in result["cuts"][0]
    assert result["removed_pauses"] == []


def test_fallback_reintegra_muletillas_y_no_limpia_el_informe() -> None:
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL (mismo candidato de bug que el
    # fallback anterior): con todo descartado por muletillas, el fallback de
    # highlights.py:301-316 reintegra el primer segmento tal cual (aquí incluso
    # con out_sec 0.5 > fin real 0.3, por `max(s0 + 0.5, end)` en L304), mientras
    # `removed_fillers` sigue declarando esa misma muletilla como eliminada.
    transcription = {
        "segments": [
            _seg(0.0, 0.3, "eh", words=[_word("eh", 0.0, 0.3, filler=True)]),
            _seg(0.6, 0.9, "mmm", words=[_word("mmm", 0.6, 0.9, filler=True)]),
        ]
    }
    result = build_edl_cuts_from_transcription(transcription)

    assert len(result["cuts"]) == 1
    assert result["cuts"][0]["text"] == "eh"
    assert (result["cuts"][0]["in_sec"], result["cuts"][0]["out_sec"]) == (0.0, 0.5)
    assert len(result["removed_fillers"]) == 2
    assert result["removed_pauses"] == []


# ---------------------------------------------------------------------------
# _collect_scene_boundaries
# ---------------------------------------------------------------------------


def test_collect_scene_boundaries_filtra_invalidos_y_deduplica(store: ProjectStore) -> None:
    project = store.create_project(name="Keyframes")
    pid = project["id"]
    src = _add_asset(store, pid)["id"]
    other = _add_asset(store, pid, filename="otro.mp4")["id"]

    def keyframe(metadata: dict[str, Any]) -> None:
        _add_asset(
            store,
            pid,
            filename="frame.png",
            content_type="image/png",
            category="keyframes",
            metadata=metadata,
        )

    keyframe({"source_asset_id": src, "shot_start_sec": 1.5})  # válido
    keyframe({"source_asset_id": src, "shot_start_sec": 1.5})  # duplicado
    keyframe({"source_asset_id": src, "shot_start_sec": 9.0, "synthetic": True})
    keyframe({"source_asset_id": src, "shot_start_sec": "no-numero"})
    keyframe({"source_asset_id": src, "shot_start_sec": None, "timestamp_sec": 7.25})
    keyframe({"source_asset_id": src, "timestamp_sec": 4.0})  # fallback timestamp_sec
    keyframe({"source_asset_id": other, "shot_start_sec": 100.0})  # otra fuente

    boundaries = _collect_scene_boundaries(store, pid, src)

    # synthetic y valores no convertibles se saltan (highlights.py:352-358);
    # los repetidos se deduplican; `timestamp_sec` solo se usa si la clave
    # `shot_start_sec` no existe.
    assert boundaries == [1.5, 4.0]
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL (candidato a bug, ver reporte):
    # highlights.py:354 hace `meta.get("shot_start_sec", fallback)`, de modo que
    # un `shot_start_sec` presente pero None anula el fallback y 7.25 se pierde.
    assert 7.25 not in boundaries


def test_collect_scene_boundaries_sin_asset_origen_o_keyframes(store: ProjectStore) -> None:
    project = store.create_project(name="Sin keyframes")
    pid = project["id"]

    assert _collect_scene_boundaries(store, pid, None) == []
    src = _add_asset(store, pid)["id"]
    assert _collect_scene_boundaries(store, pid, src) == []


# ---------------------------------------------------------------------------
# generate_highlights_edl: flujo sin híbrido y ValueErrors
# ---------------------------------------------------------------------------


def test_generate_highlights_edl_sin_hibrido_persiste_informe_completo(
    store: ProjectStore,
) -> None:
    pid, asset, tr = _seed_project_with_transcription(
        store,
        [
            _seg(0.0, 0.8, "eh este", is_filler_segment=True),
            _seg(1.3, 3.0, "hola mundo", speaker="SPK_A"),
            _seg(3.5, 5.0, "adios", speaker="SPK_A"),
        ],
    )

    saved = generate_highlights_edl(store, pid, title="Reel de prueba", aspect_ratio="9:16")
    edl = saved["edl"]

    assert saved["version"] == 1
    assert edl["schema"] == "comfy_studio.edl.v1"
    assert edl["title"] == "Reel de prueba"
    assert edl["aspect_ratio"] == "9:16"
    assert edl["transcription_id"] == tr["id"]
    assert edl["source_asset_id"] == asset["id"]
    assert [(c["in_sec"], c["out_sec"]) for c in edl["cuts"]] == [(1.3, 3.0), (3.5, 5.0)]
    assert edl["cuts"][0]["source"] == asset["filename"]
    assert edl["cuts"][0]["asset_id"] == asset["id"]
    assert edl["cuts"][0]["speaker"] == "SPK_A"

    report = edl["editorial_report"]
    assert report["hybrid"] is None
    # Sin clave `simulated` en la transcripción, el default es True
    # (highlights.py:455).
    assert report["transcription_simulated"] is True
    assert report["removed_fillers"] == [
        {
            "in_sec": 0.0,
            "out_sec": 0.8,
            "text": "eh este",
            "reason": "eliminar_muletilla",
        }
    ]
    assert report["removed_pauses"] == [
        {"in_sec": 0.8, "out_sec": 1.3, "duration_sec": 0.5, "reason": "eliminar_pausa"},
        {"in_sec": 3.0, "out_sec": 3.5, "duration_sec": 0.5, "reason": "eliminar_pausa"},
    ]
    assert "Muletillas eliminadas: 1." in edl["notes"]
    assert "Pausas eliminadas: 2." in edl["notes"]

    latest = store.get_latest_edl(pid)
    assert latest is not None
    assert latest["id"] == saved["id"]


def test_generate_highlights_edl_transcripcion_inexistente_lanza_value_error(
    store: ProjectStore,
) -> None:
    pid = store.create_project(name="Sin transcripcion")["id"]

    with pytest.raises(ValueError, match="Transcripción no encontrada"):
        generate_highlights_edl(store, pid, transcription_id="tr-noexiste")


def test_generate_highlights_edl_proyecto_inexistente_lanza_value_error(
    store: ProjectStore,
) -> None:
    with pytest.raises(ValueError, match="Proyecto no encontrado"):
        generate_highlights_edl(store, "proyecto-inexistente")


def test_generate_highlights_edl_transcribe_cuando_no_hay_transcripcion(
    store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = store.create_project(name="Fallback ASR")
    pid = project["id"]
    asset = _add_asset(store, pid)
    calls: list[dict[str, Any]] = []

    def fake_transcribe(
        store_arg: Any,
        project_id: str,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        calls.append({"store": store_arg, "project_id": project_id, "asset_id": asset_id})
        return {
            "id": "tr-simulada-test",
            "asset_id": asset_id,
            "segments": [_seg(0.0, 2.0, "transcripcion simulada")],
            "simulated": False,
        }

    monkeypatch.setattr(highlights_mod, "transcribe_project_asset", fake_transcribe)

    saved = generate_highlights_edl(store, pid, asset_id=asset["id"])

    assert calls == [{"store": store, "project_id": pid, "asset_id": asset["id"]}]
    assert saved["edl"]["transcription_id"] == "tr-simulada-test"
    assert saved["edl"]["source_asset_id"] == asset["id"]
    assert saved["edl"]["cuts"][0]["source"] == asset["filename"]
    assert [(c["in_sec"], c["out_sec"]) for c in saved["edl"]["cuts"]] == [(0.0, 2.0)]
    # El valor `simulated=False` del backend real se propaga (no hay default).
    assert saved["edl"]["editorial_report"]["transcription_simulated"] is False


# ---------------------------------------------------------------------------
# generate_highlights_edl: modo hybrid
# ---------------------------------------------------------------------------


def test_generate_highlights_edl_hybrid_sin_asset_origen_documenta_salto(
    store: ProjectStore,
) -> None:
    pid = store.create_project(name="Hibrido sin asset")["id"]
    store.save_transcription(pid, segments=[_seg(0.0, 2.0, "sin asset de origen")])

    saved = generate_highlights_edl(store, pid, hybrid=True)

    assert saved["edl"]["source_asset_id"] is None
    assert len(saved["edl"]["cuts"]) == 1
    # Rama skipped_reason de highlights.py:417-419.
    assert saved["edl"]["editorial_report"]["hybrid"] == {
        "requested": True,
        "applied": False,
        "skipped_reason": "El EDL no tiene asset de origen para medir audio.",
    }


def test_generate_highlights_edl_hybrid_sin_senal_acustica_marca_como_refinado(
    store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ANCLAMIENTO DEL COMPORTAMIENTO ACTUAL (candidato a bug, ver reporte):
    # con asset presente pero señal acústica no disponible, la rama de
    # highlights.py:428-438 marca `applied: True` y reescribe `notes` con
    # "Refinado híbrido ... (señal acústica: no ...)" aunque ningún borde se
    # movió, pese al comentario de L411-413 ("se deja el EDL textual intacto").
    pid, _asset, _tr = _seed_project_with_transcription(
        store,
        [_seg(0.0, 2.0, "uno"), _seg(2.5, 4.0, "dos")],
    )
    seen: list[str] = []

    def fake_energy(media_path: Path | str) -> dict[str, Any]:
        seen.append(str(media_path))
        return {"available": False, "duration_sec": 0.0, "spans": []}

    monkeypatch.setattr(highlights_mod, "analyze_acoustic_energy", fake_energy)

    saved = generate_highlights_edl(store, pid, hybrid=True)
    edl = saved["edl"]
    hybrid = edl["editorial_report"]["hybrid"]

    assert len(seen) == 1
    assert Path(seen[0]).is_file()
    assert hybrid["requested"] is True
    assert hybrid["applied"] is True
    assert hybrid["acoustic_available"] is False
    assert hybrid["scene_boundary_count"] == 0
    assert "Refinado híbrido: 2 cortes (señal acústica: no, escenas: 0)" in edl["notes"]
    assert [(c["in_sec"], c["out_sec"]) for c in edl["cuts"]] == [(0.0, 2.0), (2.5, 4.0)]
    assert edl["total_duration_sec"] == 3.5


def test_generate_highlights_edl_hybrid_cortes_cortos_no_aplica(
    store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rama de highlights.py:428 sin cumplirse: con la única propuesta (< 0.2 s)
    # descartada por `apply_hybrid_refinements`, `refined` queda ausente y L438
    # fija `applied: False`; notas y cortes quedan intactos.
    pid, _asset, _tr = _seed_project_with_transcription(store, [_seg(0.0, 0.1, "micra")])

    def fake_energy(media_path: Path | str) -> dict[str, Any]:
        assert Path(media_path).is_file()
        return {"available": False, "duration_sec": 0.0, "spans": []}

    monkeypatch.setattr(highlights_mod, "analyze_acoustic_energy", fake_energy)

    saved = generate_highlights_edl(store, pid, hybrid=True)
    edl = saved["edl"]
    hybrid = edl["editorial_report"]["hybrid"]

    assert hybrid["requested"] is True
    assert hybrid["applied"] is False
    assert "refined" not in hybrid
    assert hybrid["dropped_too_short"] == 1
    assert "Refinado híbrido" not in edl["notes"]
    assert edl["notes"] == "Highlights: 1 cortes (0.10s)."
    assert [(c["in_sec"], c["out_sec"]) for c in edl["cuts"]] == [(0.0, 0.1)]
    assert edl["total_duration_sec"] == 0.1


def test_generate_highlights_edl_hybrid_refina_y_documenta(
    store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, asset, _tr = _seed_project_with_transcription(
        store,
        [_seg(0.0, 2.0, "uno"), _seg(2.5, 4.0, "dos")],
    )
    seen: list[str] = []

    def fake_energy(media_path: Path | str) -> dict[str, Any]:
        seen.append(str(media_path))
        return {
            "available": True,
            "duration_sec": 4.0,
            "spans": [{"start": 0.1, "end": 1.9}, {"start": 2.6, "end": 3.8}],
        }

    monkeypatch.setattr(highlights_mod, "analyze_acoustic_energy", fake_energy)

    saved = generate_highlights_edl(store, pid, hybrid=True)
    edl = saved["edl"]
    hybrid = edl["editorial_report"]["hybrid"]

    assert len(seen) == 1
    assert Path(seen[0]) == store.get_asset_path(pid, asset["id"])
    assert hybrid["requested"] is True
    assert hybrid["applied"] is True
    assert hybrid["acoustic_available"] is True
    assert hybrid["acoustic_span_count"] == 2
    assert hybrid["scene_boundary_count"] == 0
    assert hybrid["trimmed_by_acoustic"] == 2
    assert hybrid["dropped_too_short"] == 0
    # Los cortes se acotan a los tramos medidos (nunca se alargan) y el total se
    # recalcula: (1.9-0.1) + (3.8-2.6) = 3.0 s.
    assert [(c["in_sec"], c["out_sec"]) for c in edl["cuts"]] == [(0.1, 1.9), (2.6, 3.8)]
    assert edl["total_duration_sec"] == 3.0
    assert "Refinado híbrido: 2 cortes (señal acústica: sí, escenas: 0)" in edl["notes"]
