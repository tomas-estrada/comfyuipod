"""Tests de recuperación y planificación del montaje desde brief (M7).

Cubre de forma determinista y solo-CPU (sin GPU, red, pesos, ffprobe ni LLM)
las funciones de `comfy_studio/montage/brief.py` que no tienen test directo:

- `retrieve_transcript_candidates`: puntuación idf, clamp de `time_range`,
  filtrado por `asset_ids`, truncado por `max_segments`, informes tempranos
  (`sin_términos` / `sin_transcripciones`) y evidencia trazable.
- `retrieve_visual_candidates` / `retrieve_identity_candidates`: guardas de
  índice nulo, llamadas a `find_shots_text` / `find_shots_face`, deduplicación,
  timecodes de respaldo, clamp de similitud y de `time_range`.
- `probe_source_durations`: `sin_ruta` sin invocar ffprobe y mapeo del doble
  de `probe_media_info`.
- `plan_montage_from_brief`: ramas de percepción no disponible / obsoleta,
  identidad no resuelta (`ValueError`), persistencia del EDL y errores.

Los índices CLIP/facial y el store son falsos en memoria; un fixture autouse
impide cualquier invocación real de `probe_media_info` (ffprobe).
"""

from __future__ import annotations

from typing import Any

import pytest

from comfy_studio.montage import brief as brief_module
from comfy_studio.montage.brief import (
    plan_montage_from_brief,
    probe_source_durations,
    retrieve_identity_candidates,
    retrieve_transcript_candidates,
    retrieve_visual_candidates,
)

PROJECT_ID = "proj-1"


@pytest.fixture(autouse=True)
def _forbid_real_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test de este archivo debe ejecutar ffprobe de verdad."""

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("probe_media_info real no debe invocarse en tests CPU")

    monkeypatch.setattr(brief_module, "probe_media_info", _boom)


# ---------------------------------------------------------------------------
# Falsos en memoria: store, índice CLIP e índice facial
# ---------------------------------------------------------------------------


class FakeStore:
    """Doble en memoria del store de proyectos con la superficie que usa `plan`."""

    def __init__(
        self,
        *,
        project_id: str = PROJECT_ID,
        transcriptions: list[dict[str, Any]] | None = None,
        assets: list[dict[str, Any]] | None = None,
        identities: list[dict[str, Any]] | None = None,
        asset_paths: dict[str, str] | None = None,
    ) -> None:
        self.project_id = project_id
        self.transcriptions = transcriptions or []
        self.assets = assets or []
        self.identities = identities or []
        self.asset_paths = asset_paths or {}
        self.saved_edls: list[dict[str, Any]] = []

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        if project_id != self.project_id:
            return None
        return {"id": project_id, "name": "Proyecto de prueba"}

    def list_transcriptions(
        self, project_id: str, *, asset_id: str | None = None
    ) -> list[dict[str, Any]]:
        return list(self.transcriptions)

    def list_assets(self, project_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        return list(self.assets)

    def list_identities(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.identities)

    def get_identity_by_name(self, project_id: str, name: str) -> dict[str, Any] | None:
        target = (name or "").strip().lower()
        for identity in self.identities:
            if str(identity.get("name") or "").strip().lower() == target:
                return identity
        return None

    def get_asset_path(self, project_id: str, asset_id: str) -> str | None:
        return self.asset_paths.get(asset_id)

    def save_edl_version(
        self,
        project_id: str,
        *,
        edl: dict[str, Any],
        output_asset_id: str | None = None,
    ) -> dict[str, Any]:
        version = len(self.saved_edls) + 1
        saved = {
            "id": f"edl-test-{version}",
            "project_id": project_id,
            "version": version,
            "edl": dict(edl),
        }
        self.saved_edls.append(saved)
        return dict(saved)


class FakeClipIndex:
    """Doble de `ClipIndex.find_shots_text` que registra cada llamada."""

    def __init__(self, shots: list[dict[str, Any]] | None = None) -> None:
        self.shots = shots or []
        self.calls: list[dict[str, Any]] = []

    def find_shots_text(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int = 5,
        limit: int | None = None,
        source_asset_id: str | None = None,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "project_id": project_id,
                "query": query,
                "top_k": top_k,
                "limit": limit,
                "source_asset_id": source_asset_id,
                "asset_id": asset_id,
            }
        )
        effective = int(limit) if limit is not None else int(top_k)
        hits = [
            s for s in self.shots if asset_id is None or s.get("source_asset_id") == asset_id
        ]
        return [dict(s) for s in hits[:effective]]


class FakeFaceIndex:
    """Doble de `FaceIndex.find_shots_face`; puede simular identidad no resuelta."""

    def __init__(
        self,
        shots: list[dict[str, Any]] | None = None,
        *,
        raise_for_names: set[str] | None = None,
    ) -> None:
        self.shots = shots or []
        self.raise_for_names = raise_for_names or set()
        self.calls: list[dict[str, Any]] = []

    def find_shots_face(
        self,
        project_id: str,
        *,
        identity_id: str | None = None,
        identity_name: str | None = None,
        person_name: str | None = None,
        reference_asset_id: str | None = None,
        text_query: str | None = None,
        top_k: int = 10,
        limit: int | None = None,
        min_similarity: float = 0.60,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "project_id": project_id,
                "identity_id": identity_id,
                "identity_name": identity_name,
                "person_name": person_name,
                "reference_asset_id": reference_asset_id,
                "text_query": text_query,
                "top_k": top_k,
                "limit": limit,
                "min_similarity": min_similarity,
            }
        )
        if identity_name in self.raise_for_names:
            # Mismo error real que lanza `FaceIndex.find_shots_face` cuando la
            # identidad no está indexada (face_index.py:429/445).
            raise ValueError(f"Identidad no encontrada: {identity_name!r}")
        effective = int(limit) if limit is not None else int(top_k)
        return [dict(s) for s in self.shots[:effective]]


def _transcription(
    asset_id: str, segments: list[Any], tr_id: str = "tr-1"
) -> dict[str, Any]:
    return {"id": tr_id, "asset_id": asset_id, "segments": segments}


def _default_store(**overrides: Any) -> FakeStore:
    kwargs: dict[str, Any] = {
        "transcriptions": [
            _transcription(
                "a1",
                [
                    {
                        "id": "seg-001",
                        "start": 0.0,
                        "end": 3.0,
                        "text": "el bosque verde brilla",
                        "speaker": "S0",
                    }
                ],
            )
        ],
        "assets": [{"id": "a1"}, {"id": "a2"}],
        "identities": [],
    }
    kwargs.update(overrides)
    return FakeStore(**kwargs)


DEFAULT_CLIP_SHOT: dict[str, Any] = {
    "shot_id": "sh-9",
    "source_asset_id": "a1",
    "shot_start_sec": 1.0,
    "shot_end_sec": 3.0,
    "timestamp_sec": 1.5,
    "similarity": 0.77,
    "keyframe_asset_id": "kf-9",
    "caption": "bosque",
    "visual_tags": ["bosque"],
}


# ---------------------------------------------------------------------------
# retrieve_transcript_candidates
# ---------------------------------------------------------------------------


def test_transcript_sin_terminos_returna_informe_temprano() -> None:
    cands, report = retrieve_transcript_candidates(FakeStore(), PROJECT_ID, [])
    assert cands == []
    assert report == {"transcript_segments": 0, "matched": 0, "skipped": "sin_términos"}


def test_transcript_sin_transcripciones_o_filtradas_returna_informe_temprano() -> None:
    store = FakeStore(transcriptions=[])
    cands, report = retrieve_transcript_candidates(store, PROJECT_ID, ["bosque"])
    assert cands == []
    assert report == {
        "transcript_segments": 0,
        "matched": 0,
        "skipped": "sin_transcripciones",
    }

    # asset_ids que excluye todas las transcripciones -> mismo informe temprano.
    store = FakeStore(
        transcriptions=[_transcription("a1", [{"start": 0.0, "end": 2.0, "text": "bosque"}])]
    )
    cands, report = retrieve_transcript_candidates(
        store, PROJECT_ID, ["bosque"], asset_ids={"a2"}
    )
    assert cands == []
    assert report["skipped"] == "sin_transcripciones"
    assert report["transcript_segments"] == 0


def test_transcript_score_idf_es_cobertura_ponderada_del_brief() -> None:
    # 2 segmentos: n=2; idf(alpha)=ln(3/3)+1=1.0 (df=2), idf(beta)=idf(gamma)
    # = ln(3/2)+1 (df=1). total = 1 + 2*1.4054651081081644 = 3.810930216216329.
    store = FakeStore(
        transcriptions=[
            _transcription(
                "a1",
                [
                    {"start": 0.0, "end": 2.0, "text": "alpha beta gamma"},
                    {"start": 2.0, "end": 4.0, "text": "alpha"},
                ],
            )
        ]
    )
    cands, report = retrieve_transcript_candidates(
        store, PROJECT_ID, ["alpha", "beta", "gamma"]
    )
    assert report == {"transcript_segments": 2, "matched": 2, "truncated": 0}
    assert cands[0]["text"] == "alpha beta gamma"
    assert cands[0]["score"] == 1.0  # todos los términos del brief presentes
    assert cands[0]["evidence"][0]["matched_terms"] == ["alpha", "beta", "gamma"]
    # 1.0 / 3.810930216216329 = 0.262403... -> 0.2624 (round del contrato)
    assert cands[1]["text"] == "alpha"
    assert cands[1]["score"] == 0.2624
    assert cands[1]["evidence"][0]["matched_terms"] == ["alpha"]


def test_transcript_termino_ausente_del_corpus_usa_idf_por_defecto() -> None:
    # n=1; idf(alpha)=ln(2/2)+1=1.0; "omega" no aparece en ningún segmento,
    # así que term_idf usa ln(n+1)+1 = ln(2)+1 = 1.6931471805599453.
    # total = 2.6931471805599453 -> 1.0/total = 0.3713127... -> 0.3713.
    store = FakeStore(
        transcriptions=[
            _transcription("a1", [{"start": 0.0, "end": 2.0, "text": "alpha"}])
        ]
    )
    cands, _report = retrieve_transcript_candidates(store, PROJECT_ID, ["alpha", "omega"])
    assert len(cands) == 1
    assert cands[0]["score"] == 0.3713
    assert cands[0]["evidence"][0]["matched_terms"] == ["alpha"]


def test_transcript_time_range_acota_y_descarta_segmentos_fuera_del_rango() -> None:
    store = FakeStore(
        transcriptions=[
            _transcription(
                "a1",
                [
                    {"start": 0.0, "end": 1.0, "text": "camino corto"},
                    {"start": 1.0, "end": 2.5, "text": "camino largo"},
                    {"start": 2.0, "end": 4.0, "text": "camino verde"},
                    {"start": 5.0, "end": 6.0, "text": "camino alto"},
                ],
            )
        ]
    )
    cands, report = retrieve_transcript_candidates(
        store, PROJECT_ID, ["camino"], time_range=(1.5, 5.5)
    )
    # (0,1) queda fuera por `end <= lo`; el resto se acota al rango.
    assert [(c["in_sec"], c["out_sec"]) for c in cands] == [
        (1.5, 2.5),
        (2.0, 4.0),
        (5.0, 5.5),
    ]
    assert report["transcript_segments"] == 4
    assert report["matched"] == 3

    # Rango que excluye todo: start >= hi descarta el segmento (5,6).
    cands, report = retrieve_transcript_candidates(
        store, PROJECT_ID, ["camino"], time_range=(0.0, 5.0)
    )
    assert all(c["out_sec"] <= 5.0 for c in cands)
    assert report["matched"] == 3  # (5,6) fuera; (0,1) acotado a (0,1)


def test_transcript_asset_ids_filtra_transcripciones_antes_del_idf() -> None:
    store = FakeStore(
        transcriptions=[
            _transcription("a1", [{"start": 0.0, "end": 2.0, "text": "alpha solo"}]),
            _transcription(
                "a2", [{"start": 0.0, "end": 2.0, "text": "alpha dos"}], tr_id="tr-2"
            ),
        ]
    )
    cands, report = retrieve_transcript_candidates(store, PROJECT_ID, ["alpha"], asset_ids={"a1"})
    assert report["transcript_segments"] == 1  # la transcripción de a2 no cuenta ni en n_docs
    assert report["matched"] == 1
    assert [c["asset_id"] for c in cands] == ["a1"]
    assert cands[0]["evidence"][0]["transcription_id"] == "tr-1"


def test_transcript_max_segments_trunca_y_reporta_el_excedente() -> None:
    store = FakeStore(
        transcriptions=[
            _transcription(
                "a1",
                [
                    {"start": 0.0, "end": 1.0, "text": "alpha beta gamma"},
                    {"start": 1.0, "end": 2.0, "text": "alpha beta"},
                    {"start": 2.0, "end": 3.0, "text": "alpha"},
                ],
            )
        ]
    )
    cands, report = retrieve_transcript_candidates(
        store, PROJECT_ID, ["alpha", "beta", "gamma"], max_segments=2
    )
    assert len(cands) == 2
    # `matched` cuenta los candidatos ANTES de truncar; `truncated` el excedente.
    assert report == {"transcript_segments": 3, "matched": 3, "truncated": 1}
    assert [c["text"] for c in cands] == ["alpha beta gamma", "alpha beta"]


def test_transcript_evidencia_segment_id_fallback_y_descarta_segmentos_degenerados() -> None:
    store = FakeStore(
        transcriptions=[
            _transcription(
                "a1",
                [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": "charla interesante",
                        "speaker": "SPEAKER_07",
                    },
                    {"start": 2.0, "end": 2.0, "text": "charla repetida"},
                    "segmento-no-dict",
                    {"start": 4.0, "end": 5.0, "text": "   "},
                ],
                tr_id="tr-42",
            )
        ]
    )
    cands, report = retrieve_transcript_candidates(store, PROJECT_ID, ["charla"])
    # No-dict y texto vacío no cuentan; el degenerado (end<=start) sí cuenta en
    # n_docs pero no produce candidato.
    assert report == {"transcript_segments": 2, "matched": 1, "truncated": 0}
    assert len(cands) == 1
    cand = cands[0]
    assert cand["asset_id"] == "a1"
    assert (cand["in_sec"], cand["out_sec"]) == (0.0, 2.0)
    assert cand["text"] == "charla interesante"
    assert cand["speaker"] == "SPEAKER_07"
    evidence = cand["evidence"][0]
    assert evidence["mode"] == "transcript"
    assert evidence["transcription_id"] == "tr-42"
    assert evidence["segment_id"] == "seg-001"  # fallback sin id propio
    assert evidence["segment_index"] == 0
    assert evidence["matched_terms"] == ["charla"]
    assert evidence["score"] == cand["score"]


def test_transcript_orden_por_score_asset_y_timecode() -> None:
    store = FakeStore(
        transcriptions=[
            _transcription(
                "a1",
                [
                    {"start": 2.0, "end": 3.0, "text": "alpha beta"},
                    {"start": 0.5, "end": 1.5, "text": "alpha beta"},
                ],
            ),
            _transcription(
                "a2", [{"start": 0.0, "end": 1.0, "text": "alpha beta"}], tr_id="tr-2"
            ),
        ]
    )
    cands, _report = retrieve_transcript_candidates(store, PROJECT_ID, ["alpha", "beta"])
    assert [(c["asset_id"], c["in_sec"]) for c in cands] == [
        ("a1", 0.5),
        ("a1", 2.0),
        ("a2", 0.0),
    ]


# ---------------------------------------------------------------------------
# retrieve_visual_candidates
# ---------------------------------------------------------------------------


def test_visual_sin_indice_clip_returna_informe_temprano() -> None:
    cands, report = retrieve_visual_candidates(None, PROJECT_ID, "bosque")
    assert cands == []
    assert report == {"visual_shots": 0, "skipped": "sin_indice_clip"}


def test_visual_asset_ids_consulta_una_vez_por_asset_ordenado_y_deduplica() -> None:
    clip = FakeClipIndex(
        [
            {
                "shot_id": "sh-1",
                "source_asset_id": "a1",
                "timestamp_sec": 1.0,
                "similarity": 0.5,
            },
            {
                "shot_id": "sh-2",
                "source_asset_id": "a1",
                "timestamp_sec": 3.0,
                "similarity": 0.5,
            },
            # Misma shot_id devuelta por la consulta de a2: se deduplica y se
            # conserva la PRIMERA aparición (la de a1, timestamp 1.0).
            {
                "shot_id": "sh-1",
                "source_asset_id": "a2",
                "timestamp_sec": 99.0,
                "similarity": 0.5,
            },
            {
                "shot_id": "sh-3",
                "source_asset_id": "a2",
                "timestamp_sec": 2.0,
                "similarity": 0.5,
            },
        ]
    )
    cands, report = retrieve_visual_candidates(
        clip, PROJECT_ID, "bosque", asset_ids={"a2", "a1"}, limit=7
    )
    assert [c["asset_id"] for c in clip.calls] == ["a1", "a2"]  # orden sorted()
    assert all(c["limit"] == 7 for c in clip.calls)
    assert clip.calls[0]["project_id"] == PROJECT_ID
    assert clip.calls[0]["query"] == "bosque"
    assert len(cands) == 3
    assert report == {"visual_shots": 3}
    assert [(c["asset_id"], c["in_sec"]) for c in cands] == [
        ("a1", 1.0),
        ("a1", 3.0),
        ("a2", 2.0),
    ]


def test_visual_timecodes_respaldados_y_shot_sin_id_se_descarta() -> None:
    clip = FakeClipIndex(
        [
            # Sin shot_start_sec/shot_end_sec: usa timestamp_sec y +2.0.
            {"shot_id": "s1", "source_asset_id": "a1", "timestamp_sec": 10.0},
            # end <= start se repara a start + 2.0; sin similarity -> 0.0.
            {
                "shot_id": "s2",
                "source_asset_id": "a1",
                "shot_start_sec": 5.0,
                "shot_end_sec": 4.0,
            },
            {"source_asset_id": "a1", "timestamp_sec": 0.0},
            {"shot_id": "", "source_asset_id": "a1", "timestamp_sec": 0.0},
        ]
    )
    cands, report = retrieve_visual_candidates(clip, PROJECT_ID, "bosque")
    assert report == {"visual_shots": 2}
    by_id = {c["evidence"][0]["shot_id"]: c for c in cands}
    assert (by_id["s1"]["in_sec"], by_id["s1"]["out_sec"]) == (10.0, 12.0)
    assert (by_id["s2"]["in_sec"], by_id["s2"]["out_sec"]) == (5.0, 7.0)
    assert by_id["s1"]["score"] == 0.0  # similitud ausente -> 0.0
    assert by_id["s2"]["score"] == 0.0
    # Sin asset_ids se hace UNA sola llamada sin asset_id.
    assert len(clip.calls) == 1
    assert clip.calls[0]["asset_id"] is None
    assert clip.calls[0]["limit"] == 30  # default del contrato de retrieve_*


def test_visual_score_acota_similitud_y_evidencia_conserva_el_valor_crudo() -> None:
    clip = FakeClipIndex(
        [
            {
                "shot_id": "hi",
                "source_asset_id": "a1",
                "shot_start_sec": 0.0,
                "shot_end_sec": 2.0,
                "similarity": 1.7,
                "keyframe_asset_id": "kf-hi",
                "timestamp_sec": 0.5,
                "caption": "algo",
                "visual_tags": ["fuego"],
            },
            {
                "shot_id": "lo",
                "source_asset_id": "a1",
                "shot_start_sec": 4.0,
                "shot_end_sec": 6.0,
                "similarity": -0.3,
                "keyframe_asset_id": "kf-lo",
            },
        ]
    )
    cands, _report = retrieve_visual_candidates(clip, PROJECT_ID, "fuego")
    by_id = {c["evidence"][0]["shot_id"]: c for c in cands}
    assert by_id["hi"]["score"] == 1.0
    # Contrato actual: el score se acota a [0,1] pero la evidencia registra la
    # similitud cruda redondeada (round(1.7, 4) == 1.7).
    assert by_id["hi"]["evidence"][0]["similarity"] == 1.7
    assert by_id["lo"]["score"] == 0.0
    assert by_id["lo"]["evidence"][0]["similarity"] == -0.3
    evidence = by_id["hi"]["evidence"][0]
    assert evidence["mode"] == "visual"
    assert evidence["keyframe_asset_id"] == "kf-hi"
    assert evidence["timestamp_sec"] == 0.5
    assert evidence["caption"] == "algo"
    assert evidence["visual_tags"] == ["fuego"]
    # Sin shot_end_sec la evidencia no lo incluye; el corte usa start+2.0.
    assert by_id["lo"]["out_sec"] == 6.0


def test_visual_time_range_acota_y_descarta_planos_fuera_del_rango() -> None:
    clip = FakeClipIndex(
        [
            {
                "shot_id": "s-in",
                "source_asset_id": "a1",
                "shot_start_sec": 2.0,
                "shot_end_sec": 4.0,
                "similarity": 0.5,
            },
            {
                "shot_id": "s-before",
                "source_asset_id": "a1",
                "shot_start_sec": 0.0,
                "shot_end_sec": 1.0,
                "similarity": 0.9,
            },
            {
                "shot_id": "s-after",
                "source_asset_id": "a1",
                "shot_start_sec": 5.0,
                "shot_end_sec": 6.0,
                "similarity": 0.9,
            },
        ]
    )
    cands, report = retrieve_visual_candidates(
        clip, PROJECT_ID, "bosque", time_range=(3.0, 5.0)
    )
    assert report == {"visual_shots": 1}
    assert len(cands) == 1
    assert cands[0]["evidence"][0]["shot_id"] == "s-in"
    assert (cands[0]["in_sec"], cands[0]["out_sec"]) == (3.0, 4.0)


# ---------------------------------------------------------------------------
# retrieve_identity_candidates
# ---------------------------------------------------------------------------


def test_identity_sin_indice_o_sin_persona_returna_informe_temprano() -> None:
    cands, report = retrieve_identity_candidates(None, PROJECT_ID, identity_name="Ana")
    assert cands == []
    assert report == {"identity_shots": 0, "skipped": "sin_indice_facial"}

    cands, report = retrieve_identity_candidates(FakeFaceIndex(), PROJECT_ID)
    assert cands == []
    assert report == {"identity_shots": 0, "skipped": "sin_indice_facial"}


def test_identity_llama_find_shots_face_con_los_argumentos_del_contrato() -> None:
    face = FakeFaceIndex(
        [
            {
                "identity_id": "id-1",
                "person_name": "Ana",
                "keyframe_asset_id": "kf-1",
                "source_asset_id": "a1",
                "timestamp_sec": 3.0,
                "shot_start_sec": 2.0,
                "shot_end_sec": 4.0,
                "face_similarity": 0.88,
            },
            # Misma keyframe: deduplicada.
            {
                "identity_id": "id-1",
                "person_name": "Ana",
                "keyframe_asset_id": "kf-1",
                "source_asset_id": "a1",
                "timestamp_sec": 9.0,
                "face_similarity": 0.99,
            },
            # Sin keyframe: descartada.
            {
                "identity_id": "id-1",
                "person_name": "Ana",
                "keyframe_asset_id": "",
                "source_asset_id": "a1",
                "timestamp_sec": 4.0,
                "face_similarity": 0.7,
            },
        ]
    )
    cands, report = retrieve_identity_candidates(
        face,
        PROJECT_ID,
        identity_id="id-1",
        identity_name="Ana",
        text_query="Ana en el bosque",
        limit=5,
    )
    assert len(face.calls) == 1
    call = face.calls[0]
    assert call["project_id"] == PROJECT_ID
    assert call["identity_id"] == "id-1"
    assert call["identity_name"] == "Ana"
    assert call["text_query"] == "Ana en el bosque"
    assert call["limit"] == 5
    assert report == {"identity_shots": 1}
    cand = cands[0]
    assert (cand["in_sec"], cand["out_sec"]) == (2.0, 4.0)
    assert cand["score"] == 0.88
    evidence = cand["evidence"][0]
    assert evidence["mode"] == "identity"
    assert evidence["identity_id"] == "id-1"
    assert evidence["identity_name"] == "Ana"  # viene de person_name del hit
    assert evidence["keyframe_asset_id"] == "kf-1"
    assert evidence["timestamp_sec"] == 3.0
    assert evidence["similarity"] == 0.88


def test_identity_similitud_fallback_face_similarity_y_acotacion() -> None:
    face = FakeFaceIndex(
        [
            {
                "keyframe_asset_id": "k1",
                "source_asset_id": "a1",
                "timestamp_sec": 1.0,
                "shot_start_sec": 1.0,
                "shot_end_sec": 2.0,
                "face_similarity": 0.65,
            },
            {
                "keyframe_asset_id": "k2",
                "source_asset_id": "a1",
                "timestamp_sec": 2.0,
                "shot_start_sec": 2.0,
                "shot_end_sec": 3.0,
                "similarity": 0.5,
                "face_similarity": 0.9,
            },
            {
                "keyframe_asset_id": "k3",
                "source_asset_id": "a1",
                "timestamp_sec": 3.0,
                "shot_start_sec": 3.0,
                "shot_end_sec": 4.0,
                "similarity": 1.4,
            },
            {
                "keyframe_asset_id": "k4",
                "source_asset_id": "a1",
                "timestamp_sec": 4.0,
                "shot_start_sec": 4.0,
                "shot_end_sec": 5.0,
            },
        ]
    )
    cands, report = retrieve_identity_candidates(face, PROJECT_ID, identity_name="Ana")
    assert report == {"identity_shots": 4}
    scores = {c["evidence"][0]["keyframe_asset_id"]: c["score"] for c in cands}
    assert scores["k1"] == 0.65  # fallback a face_similarity
    assert scores["k2"] == 0.5  # `similarity` gana si ambas claves existen
    assert scores["k3"] == 1.0  # acotado a 1.0
    assert scores["k4"] == 0.0  # sin similitud -> 0.0


def test_identity_time_range_acota_y_descarta_planos_fuera_del_rango() -> None:
    def _shot(kf: str, start: float, end: float) -> dict[str, Any]:
        return {
            "keyframe_asset_id": kf,
            "source_asset_id": "a1",
            "timestamp_sec": start,
            "shot_start_sec": start,
            "shot_end_sec": end,
            "face_similarity": 0.7,
        }

    face = FakeFaceIndex(
        [_shot("k-in", 1.0, 3.0), _shot("k-before", 0.0, 1.0), _shot("k-after", 5.0, 6.0)]
    )
    cands, report = retrieve_identity_candidates(
        face, PROJECT_ID, identity_name="Ana", time_range=(2.0, 5.0)
    )
    assert report == {"identity_shots": 1}
    assert len(cands) == 1
    assert cands[0]["evidence"][0]["keyframe_asset_id"] == "k-in"
    assert (cands[0]["in_sec"], cands[0]["out_sec"]) == (2.0, 3.0)


# ---------------------------------------------------------------------------
# probe_source_durations
# ---------------------------------------------------------------------------


def test_probe_asset_sin_ruta_no_invoca_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(asset_paths={})  # get_asset_path devuelve None para a1/a2
    durations = probe_source_durations(store, PROJECT_ID, ["a1", "a2"])
    # El fixture autouse deja probe_media_info como un doble que revienta: si
    # se llegara a invocar, el test fallaría antes de llegar a estas aserciones.
    assert durations == {
        "a1": {"duration_sec": 0.0, "probe_ok": False, "reason": "sin_ruta"},
        "a2": {"duration_sec": 0.0, "probe_ok": False, "reason": "sin_ruta"},
    }


def test_probe_usa_doble_probe_media_info_y_pasa_el_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def _fake_probe(path: Any, *, timeout_s: float = 20.0) -> dict[str, Any]:
        calls.append((str(path), timeout_s))
        return {"duration_sec": 7.5, "probe_ok": True}

    monkeypatch.setattr(brief_module, "probe_media_info", _fake_probe)
    store = FakeStore(asset_paths={"a1": "C:/media/a1.mp4"})
    durations = probe_source_durations(store, PROJECT_ID, ["a1"], ffprobe_timeout_s=3.5)
    assert durations == {"a1": {"duration_sec": 7.5, "probe_ok": True}}
    assert calls == [("C:/media/a1.mp4", 3.5)]


def test_probe_fallido_mapea_duration_cero_y_probe_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_probe(path: Any, *, timeout_s: float = 20.0) -> dict[str, Any]:
        return {"duration_sec": None, "probe_ok": False}

    monkeypatch.setattr(brief_module, "probe_media_info", _fake_probe)
    store = FakeStore(asset_paths={"a1": "C:/media/a1.mp4"})
    durations = probe_source_durations(store, PROJECT_ID, ["a1"])
    assert durations == {"a1": {"duration_sec": 0.0, "probe_ok": False}}


# ---------------------------------------------------------------------------
# plan_montage_from_brief
# ---------------------------------------------------------------------------


def test_plan_persiste_edl_con_evidencia_fusionada_y_sin_ruta_de_probe() -> None:
    store = _default_store()
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="bosque verde",
        clip_index=clip,
    )
    assert result["status"] == "planned"
    assert store.saved_edls and store.saved_edls[0]["version"] == 1
    assert result["edl_id"] == "edl-test-1"
    assert result["edl"]["schema"] == "comfy_studio.edl.v1"
    assert result["edl"]["kind"] == "montage"
    assert result["query_terms"] == ["bosque", "verde"]
    assert result["persons"] == []

    # transcript (0,3) + visual (1,3) del mismo asset se fusionan (IoU 0.67).
    assert len(result["cuts"]) == 1
    cut = result["cuts"][0]
    assert cut["asset_id"] == "a1"
    assert (cut["in_sec"], cut["out_sec"]) == (0.0, 3.0)
    assert cut["score"] == 1.0
    assert [e["mode"] for e in cut["evidence"]] == ["transcript", "visual"]
    # get_asset_path -> None => sin ruta => timecodes NO verificados (honradez).
    for ev in cut["evidence"]:
        assert ev["timecode_verified"] is False
        assert ev["simulated"] is True
    assert result["cuts_timecode_verified"] == 0
    assert result["selection"]["timecode_verified"] is False
    assert result["selection"]["selected"] == 1

    retrieval = result["retrieval"]
    assert retrieval["transcript_segments"] == 1
    assert retrieval["matched"] == 1
    assert retrieval["visual_shots"] == 1
    assert retrieval["identity_shots"] == 0
    assert retrieval["candidates"] == 2
    assert retrieval["merged_candidates"] == 1
    # Contrato actual: `skipped` del informe fusionado SIEMPRE es el de
    # identity_report (brief.py:632), que se esparce el último (brief.py:714).
    assert retrieval["skipped"] == "sin_persona"
    assert result["edl"]["montage"]["retrieval"] == retrieval
    assert result["simulated"] is True
    assert result["simulation_notice"]
    assert result["perception_stale"] is False


def test_plan_perception_stale_no_consulta_clip_y_motiva_skipped_de_identidad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _default_store()
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])

    def _boom(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("con perception_stale no debe consultarse el índice CLIP")

    monkeypatch.setattr(clip, "find_shots_text", _boom)

    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="bosque verde",
        clip_index=clip,
        perception_stale=True,
    )
    assert clip.calls == []  # la rama `elif perception_stale` (brief.py:629) no consulta
    assert result["perception_stale"] is True
    assert result["edl"]["montage"]["perception_stale"] is True
    retrieval = result["retrieval"]
    assert retrieval["visual_shots"] == 0
    # COMPORTAMIENTO ACTUAL (bug de informe): el motivo "indice_stale" que se
    # asigna en brief.py:629 nunca llega al informe fusionado, porque
    # identity_report siempre lleva `skipped` (brief.py:632) y se esparce el
    # último (brief.py:714). Sin personas el motivo observable es "sin_persona".
    assert retrieval["skipped"] == "sin_persona"
    assert "indice_stale" not in set(retrieval.values())
    # El corte sigue saliendo de la transcripción (la rama stale no la toca).
    assert result["cuts"]


def test_plan_perception_stale_con_persona_usa_rama_percepcion_no_disponible() -> None:
    store = _default_store(
        identities=[{"id": "id-ana", "name": "Ana"}],
    )
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    face = FakeFaceIndex(
        [
            {
                "identity_id": "id-ana",
                "person_name": "Ana",
                "keyframe_asset_id": "kf-ana",
                "source_asset_id": "a1",
                "timestamp_sec": 1.0,
                "shot_start_sec": 1.0,
                "shot_end_sec": 3.0,
                "face_similarity": 0.9,
            }
        ]
    )
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="Ana bosque",
        clip_index=clip,
        face_index=face,
        perception_stale=True,
    )
    assert clip.calls == []
    assert face.calls == []
    retrieval = result["retrieval"]
    assert retrieval["visual_shots"] == 0
    assert retrieval["identity_shots"] == 0
    # COMPORTAMIENTO ACTUAL (bug de informe): brief.py:629 asigna "indice_stale"
    # a visual_report, pero con personas la rama de brief.py:656 lo pisa en el
    # informe fusionado (brief.py:714) y el motivo observable es este.
    assert retrieval["skipped"] == "percepcion_no_disponible"
    assert "indice_stale" not in set(retrieval.values())
    assert result["cuts"]


def test_plan_percepcion_no_disponible_no_consulta_ningun_indice() -> None:
    store = _default_store(identities=[{"id": "id-ana", "name": "Ana"}])
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    face = FakeFaceIndex([])
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="Ana bosque",
        clip_index=clip,
        face_index=face,
        perception_available=False,
    )
    assert clip.calls == []
    assert face.calls == []
    retrieval = result["retrieval"]
    assert retrieval["visual_shots"] == 0
    assert retrieval["identity_shots"] == 0
    # COMPORTAMIENTO ACTUAL: el "no_disponible" de visual_report (brief.py:619)
    # también queda enmascarado por identity_report (brief.py:656 -> 714).
    assert retrieval["skipped"] == "percepcion_no_disponible"
    assert result["cuts"]


def test_plan_identidad_no_resuelta_single_persona_queda_enmascarada() -> None:
    store = _default_store(identities=[{"id": "id-ana", "name": "Ana"}])
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    # find_shots_face lanza el ValueError real de identidad no indexada.
    face = FakeFaceIndex([], raise_for_names={"Ana"})

    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="Ana bosque",
        clip_index=clip,
        face_index=face,
    )
    assert len(face.calls) == 1  # la rama `except ValueError` (brief.py:646) corrió
    retrieval = result["retrieval"]
    assert retrieval["identity_shots"] == 0
    # COMPORTAMIENTO ACTUAL (bug de informe): el motivo "identidad_no_resuelta:"
    # que brief.py:646-648 construye se sobrescribe en brief.py:653-654 porque
    # no hubo candidatos, así que con una sola persona nunca es observable.
    assert retrieval["skipped"] == "sin_coincidencias"
    assert not any(str(v).startswith("identidad_no_resuelta") for v in retrieval.values())
    assert result["cuts"]


def test_plan_identidad_no_resuelta_se_preserva_con_segunda_persona_con_candidatos() -> None:
    store = _default_store(
        identities=[{"id": "id-ana", "name": "Ana"}, {"id": "id-beto", "name": "Beto"}]
    )
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    face = FakeFaceIndex(
        [
            {
                "identity_id": "id-beto",
                "person_name": "Beto",
                "keyframe_asset_id": "kf-beto",
                "source_asset_id": "a1",
                "timestamp_sec": 1.0,
                "shot_start_sec": 1.0,
                "shot_end_sec": 2.5,
                "face_similarity": 0.9,
            }
        ],
        raise_for_names={"Ana"},
    )

    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="Ana y Beto bosque",
        clip_index=clip,
        face_index=face,
    )
    assert len(face.calls) == 2
    retrieval = result["retrieval"]
    # Con al menos un candidato de otra persona, brief.py:653 no dispara y el
    # motivo del `except` (brief.py:646-648) sí queda visible.
    assert retrieval["identity_shots"] == 1
    assert retrieval["skipped"] == (
        "identidad_no_resuelta: Identidad no encontrada: 'Ana'"
    )
    identity_evidence = [
        e for c in result["cuts"] for e in c["evidence"] if e["mode"] == "identity"
    ]
    assert identity_evidence and identity_evidence[0]["identity_name"] == "Beto"


def test_plan_identidad_resuelta_con_exito_reporta_sin_persona_con_identity_shots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _default_store(identities=[{"id": "id-ana", "name": "Ana"}])
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    face = FakeFaceIndex(
        [
            {
                "identity_id": "id-ana",
                "person_name": "Ana",
                "keyframe_asset_id": "kf-ana",
                "source_asset_id": "a1",
                "timestamp_sec": 1.0,
                "shot_start_sec": 1.0,
                "shot_end_sec": 3.0,
                "face_similarity": 0.9,
            }
        ]
    )
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="Ana bosque",
        clip_index=clip,
        face_index=face,
    )
    call = face.calls[0]
    assert call["identity_id"] == "id-ana"  # resuelta vía store.get_identity_by_name
    assert call["identity_name"] == "Ana"
    retrieval = result["retrieval"]
    # COMPORTAMIENTO ACTUAL (bug de informe): identity_report nace con
    # "skipped": "sin_persona" (brief.py:632) y solo se le suma identity_shots
    # (brief.py:652), así que el motivo nunca se limpia aunque haya tiros.
    assert retrieval["identity_shots"] == 1
    assert retrieval["skipped"] == "sin_persona"
    modes = {e["mode"] for c in result["cuts"] for e in c["evidence"]}
    assert "identity" in modes


def test_plan_probe_exitoso_acota_corte_a_duracion_y_verifica_timecodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def _fake_probe(path: Any, *, timeout_s: float = 20.0) -> dict[str, Any]:
        calls.append((str(path), timeout_s))
        return {"duration_sec": 2.0, "probe_ok": True}

    monkeypatch.setattr(brief_module, "probe_media_info", _fake_probe)
    store = _default_store(asset_paths={"a1": "C:/media/a1.mp4"})
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="bosque verde",
        clip_index=None,
        ffprobe_timeout_s=4.0,
    )
    assert calls == [("C:/media/a1.mp4", 4.0)]
    assert len(result["cuts"]) == 1
    # El candidato (0,3) se acota a la duración medida (2.0).
    assert (result["cuts"][0]["in_sec"], result["cuts"][0]["out_sec"]) == (0.0, 2.0)
    assert result["selection"]["clamped_by_probe"] == 1
    assert result["selection"]["timecode_verified"] is True
    assert result["cuts_timecode_verified"] == 1
    for ev in result["cuts"][0]["evidence"]:
        assert ev["timecode_verified"] is True


def test_plan_asset_ids_acota_transcripcion_e_indice_visual() -> None:
    store = _default_store(
        transcriptions=[
            _transcription("a1", [{"start": 0.0, "end": 2.0, "text": "alpha uno"}]),
            _transcription(
                "a2", [{"start": 0.0, "end": 2.0, "text": "alpha dos"}], tr_id="tr-2"
            ),
        ]
    )
    clip = FakeClipIndex(
        [
            {
                "shot_id": "sh-a1",
                "source_asset_id": "a1",
                "shot_start_sec": 0.0,
                "shot_end_sec": 2.0,
                "similarity": 0.6,
            },
            {
                "shot_id": "sh-a2",
                "source_asset_id": "a2",
                "shot_start_sec": 0.0,
                "shot_end_sec": 2.0,
                "similarity": 0.95,
            },
        ]
    )
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="alpha",
        clip_index=clip,
        asset_ids=["a1"],
    )
    # Una sola consulta CLIP y solo para el asset incluido.
    assert len(clip.calls) == 1
    assert clip.calls[0]["asset_id"] == "a1"
    retrieval = result["retrieval"]
    assert retrieval["transcript_segments"] == 1  # la transcripción de a2 se excluye
    assert retrieval["matched"] == 1
    assert retrieval["visual_shots"] == 1
    assert result["cuts"], "debe haber corte solo con material de a1"
    assert all(c["asset_id"] == "a1" for c in result["cuts"])


def test_plan_asset_ids_desconocido_lanza_value_error() -> None:
    store = _default_store()
    with pytest.raises(ValueError, match="Assets no encontrados"):
        plan_montage_from_brief(store, PROJECT_ID, brief="bosque", asset_ids=["nope"])


def test_plan_time_range_del_brief_acota_los_cortes() -> None:
    store = _default_store(
        transcriptions=[
            _transcription(
                "a1",
                [
                    {
                        "id": "seg-largo",
                        "start": 0.0,
                        "end": 6.0,
                        "text": "bosque por todo el camino",
                    }
                ],
            )
        ]
    )
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="bosque del segundo 2 al 4",
        clip_index=None,
    )
    assert result["query_terms"] == ["bosque"]
    assert result["time_range_sec"] == [2.0, 4.0]
    assert result["edl"]["montage"]["time_range_sec"] == [2.0, 4.0]
    assert len(result["cuts"]) == 1
    assert (result["cuts"][0]["in_sec"], result["cuts"][0]["out_sec"]) == (2.0, 4.0)


def test_plan_parametro_person_sin_identidad_registrada_usa_solo_el_nombre() -> None:
    store = _default_store()  # sin identidades registradas
    clip = FakeClipIndex([dict(DEFAULT_CLIP_SHOT)])
    face = FakeFaceIndex(
        [
            {
                "person_name": "Ana",
                "keyframe_asset_id": "kf-ana",
                "source_asset_id": "a1",
                "timestamp_sec": 1.0,
                "shot_start_sec": 1.0,
                "shot_end_sec": 3.0,
                "face_similarity": 0.9,
            }
        ]
    )
    result = plan_montage_from_brief(
        store,
        PROJECT_ID,
        brief="bosque",
        clip_index=clip,
        face_index=face,
        person="Ana",
    )
    assert result["persons"] == ["Ana"]
    assert len(face.calls) == 1
    # Sin identidad registrada no hay identity_id que resolver.
    assert face.calls[0]["identity_id"] is None
    assert face.calls[0]["identity_name"] == "Ana"
    assert result["retrieval"]["identity_shots"] == 1


def test_plan_lanza_value_error_sin_proyecto_brief_corto_o_sin_cortes() -> None:
    with pytest.raises(ValueError, match="Proyecto no encontrado"):
        plan_montage_from_brief(FakeStore(), "proj-fantasma", brief="bosque verde")

    with pytest.raises(ValueError, match="al menos 3 caracteres"):
        plan_montage_from_brief(_default_store(), PROJECT_ID, brief="no")

    # Proyecto válido pero sin material: ningún candidato -> sin cortes.
    empty_store = _default_store(transcriptions=[])
    with pytest.raises(ValueError, match="no produjo ningún corte"):
        plan_montage_from_brief(empty_store, PROJECT_ID, brief="bosque verde", clip_index=None)
