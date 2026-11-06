"""Índice de percepción: vacío honesto, determinismo y esquema sqlite-vec."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.perception.keyframes import (
    classify_rgb_semantic_tags,
    keyframe_extraction_cache_key,
)
from comfy_studio.perception.store import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    PerceptionUnavailableError,
    PerceptionVectorStore,
    cosine_similarity,
    load_sqlite_vec,
    pack_float32_vector,
    unpack_float32_vector,
)


def _vec(dim: int, *values: float) -> list[float]:
    return [*values, *([0.0] * (dim - len(values)))]


def _add_clip(store: PerceptionVectorStore, *, kf: str, ts: float, emb: list[float]) -> dict:
    return store.upsert_clip_shot(
        project_id="proj-1",
        source_asset_id="src-1",
        keyframe_asset_id=kf,
        timestamp_sec=ts,
        shot_start_sec=ts,
        shot_end_sec=ts + 1.0,
        embedding=emb,
        caption=f"cap {kf}",
    )


def test_indice_vacio_devuelve_vacio_honesto(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    assert store.search_clip_shots("proj-1", _vec(CLIP_EMBED_DIM, 1.0)) == []
    assert (
        store.search_face_detections("proj-1", _vec(FACE_EMBED_DIM, 1.0)) == []
    )
    assert store.list_clip_shots("proj-1") == []
    assert store.get_clip_shot_by_keyframe("inexistente") is None


def test_busqueda_determinista_ids_y_distancias(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    _add_clip(store, kf="kf-a", ts=1.0, emb=_vec(CLIP_EMBED_DIM, 1.0, 0.0))
    _add_clip(store, kf="kf-b", ts=2.0, emb=_vec(CLIP_EMBED_DIM, 0.8, 0.6))
    _add_clip(store, kf="kf-c", ts=3.0, emb=_vec(CLIP_EMBED_DIM, 0.0, 1.0))

    q = _vec(CLIP_EMBED_DIM, 1.0, 0.0)
    first = store.search_clip_shots("proj-1", q, top_k=3)
    second = store.search_clip_shots("proj-1", q, top_k=3)
    assert [h["keyframe_asset_id"] for h in first] == ["kf-a", "kf-b", "kf-c"]
    assert first == second
    assert first[0]["similarity"] == 1.0
    assert first[0]["similarity"] > first[1]["similarity"] > first[2]["similarity"]
    # distance coherente con coseno: d^2 = 2 - 2*sim (aprox. tras redondeo)
    for hit in first:
        assert hit["distance"] >= 0.0


def test_upsert_idempotente_por_keyframe(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    rec1 = _add_clip(store, kf="kf-a", ts=1.0, emb=_vec(CLIP_EMBED_DIM, 1.0))
    rec2 = _add_clip(store, kf="kf-a", ts=1.5, emb=_vec(CLIP_EMBED_DIM, 0.0, 1.0))
    assert rec1["shot_id"] == rec2["shot_id"]
    assert rec1["keyframe_asset_id"] == rec2["keyframe_asset_id"] == "kf-a"
    rows = store.list_clip_shots("proj-1")
    assert len(rows) == 1
    assert rows[0]["timestamp_sec"] == 1.5
    # la búsqueda refleja el embedding actualizado (ahora cerca de [0,1])
    hits = store.search_clip_shots("proj-1", _vec(CLIP_EMBED_DIM, 0.0, 1.0), top_k=1)
    assert hits[0]["keyframe_asset_id"] == "kf-a"
    assert hits[0]["similarity"] == 1.0


def test_filtro_proyecto_y_fuente(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    store.upsert_clip_shot(
        project_id="p-a",
        source_asset_id="s-1",
        keyframe_asset_id="kf-1",
        timestamp_sec=1.0,
        shot_start_sec=1.0,
        shot_end_sec=2.0,
        embedding=_vec(CLIP_EMBED_DIM, 1.0),
    )
    store.upsert_clip_shot(
        project_id="p-b",
        source_asset_id="s-2",
        keyframe_asset_id="kf-2",
        timestamp_sec=1.0,
        shot_start_sec=1.0,
        shot_end_sec=2.0,
        embedding=_vec(CLIP_EMBED_DIM, 1.0),
    )
    assert store.search_clip_shots("p-a", _vec(CLIP_EMBED_DIM, 1.0))[
        0
    ]["project_id"] == "p-a"
    assert (
        store.search_clip_shots("p-a", _vec(CLIP_EMBED_DIM, 1.0), source_asset_id="s-2")
        == []
    )


def test_caras_busqueda_filtrada_y_determinista(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    store.upsert_face_identity(
        identity_id="id-ana",
        project_id="proj-1",
        name="Ana",
        reference_asset_id=None,
        embedding=_vec(FACE_EMBED_DIM, 1.0, 0.0),
    )
    store.add_face_detection(
        project_id="proj-1",
        source_asset_id="src-1",
        keyframe_asset_id="kf-1",
        timestamp_sec=1.0,
        bbox=[0.2, 0.1, 0.8, 0.9],
        det_score=0.99,
        embedding=_vec(FACE_EMBED_DIM, 1.0, 0.0),
    )
    store.add_face_detection(
        project_id="proj-1",
        source_asset_id="src-1",
        keyframe_asset_id="kf-2",
        timestamp_sec=2.0,
        bbox=[0.2, 0.1, 0.8, 0.9],
        det_score=0.99,
        embedding=_vec(FACE_EMBED_DIM, 0.0, 1.0),
    )
    hits = store.search_face_detections(
        "proj-1", _vec(FACE_EMBED_DIM, 1.0, 0.0), top_k=5, min_similarity=0.55
    )
    assert [h["keyframe_asset_id"] for h in hits] == ["kf-1"]
    assert hits[0]["similarity"] == 1.0
    assert "embedding" not in hits[0]
    identity = store.get_face_identity("proj-1", "id-ana")
    assert identity is not None
    assert identity["name"] == "Ana"
    assert store.get_face_identity_by_name("proj-1", "ana") is not None


def test_clear_keyframe_elimina_detecciones(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    store.add_face_detection(
        project_id="proj-1",
        source_asset_id="src-1",
        keyframe_asset_id="kf-1",
        timestamp_sec=1.0,
        bbox=[0.2, 0.1, 0.8, 0.9],
        det_score=0.99,
        embedding=_vec(FACE_EMBED_DIM, 1.0),
    )
    store.clear_keyframe_face_detections("proj-1", "kf-1")
    assert store.search_face_detections("proj-1", _vec(FACE_EMBED_DIM, 1.0)) == []


def test_dimension_invalida_rechaza_sin_truncar(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    with pytest.raises(ValueError, match="vacío"):
        _add_clip(store, kf="kf-x", ts=1.0, emb=[])
    with pytest.raises(ValueError, match="excede"):
        _add_clip(
            store, kf="kf-x", ts=1.0, emb=[0.0] * (CLIP_EMBED_DIM + 1)
        )
    with pytest.raises(ValueError, match="excede"):
        store.search_clip_shots("p", [0.0] * (CLIP_EMBED_DIM + 1))
    with pytest.raises(ValueError, match="vacío"):
        store.search_face_detections("p", [])


def test_pack_unpack_y_calc_coseno(tmp_path: Path) -> None:
    del tmp_path
    blob = pack_float32_vector([3.0, 4.0], CLIP_EMBED_DIM)
    assert len(blob) == CLIP_EMBED_DIM * 4
    rt = unpack_float32_vector(blob, CLIP_EMBED_DIM)
    assert rt[0] == pytest.approx(0.6)
    assert rt[1] == pytest.approx(0.8)
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0]) == 0.0


def test_sqlite_vec_ausente_error_tipado(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    with pytest.raises(PerceptionUnavailableError):
        load_sqlite_vec()


def test_cache_key_y_tags_sin_gpu(tmp_path: Path) -> None:
    video = tmp_path / "clip.bin"
    video.write_bytes(b"contenido-fijo-de-test")
    k1 = keyframe_extraction_cache_key(video, scene_threshold=0.25, interval_sec=2.0)
    k2 = keyframe_extraction_cache_key(video, scene_threshold=0.25, interval_sec=2.0)
    k3 = keyframe_extraction_cache_key(video, scene_threshold=0.5, interval_sec=2.0)
    assert k1 is not None
    assert k1 == k2
    assert k3 != k1
    assert keyframe_extraction_cache_key(tmp_path / "ausente.bin") is None
    assert "rojo" in classify_rgb_semantic_tags(200.0, 30.0, 30.0)
    assert "azul" in classify_rgb_semantic_tags(30.0, 30.0, 200.0)
    assert "verde" in classify_rgb_semantic_tags(30.0, 200.0, 30.0)
