from __future__ import annotations

from pathlib import Path

from comfy_studio.perception.store import CLIP_EMBED_DIM, PerceptionVectorStore


def _vector(*values: float) -> list[float]:
    return [*values, *([0.0] * (CLIP_EMBED_DIM - len(values)))]


def _add_shot(
    store: PerceptionVectorStore,
    *,
    project: str,
    asset: str,
    timestamp: float,
    embedding: list[float],
) -> None:
    store.upsert_clip_shot(
        project_id=project,
        source_asset_id=asset,
        keyframe_asset_id=f"kf-{project}-{asset}-{timestamp}",
        timestamp_sec=timestamp,
        shot_start_sec=timestamp,
        shot_end_sec=timestamp + 1.0,
        embedding=embedding,
        caption=f"shot {project}/{asset}/{timestamp}",
    )


def test_search_returns_nearest_shots_only_from_requested_project(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    _add_shot(store, project="project-a", asset="asset-a", timestamp=1.0, embedding=_vector(1, 0))
    _add_shot(
        store,
        project="project-a",
        asset="asset-a",
        timestamp=2.0,
        embedding=_vector(0.8, 0.6),
    )
    _add_shot(store, project="project-b", asset="asset-b", timestamp=1.0, embedding=_vector(1, 0))

    hits = store.search_clip_shots("project-a", _vector(1, 0), top_k=2)

    assert len(hits) == 2
    assert all(hit["project_id"] == "project-a" for hit in hits)
    assert hits[0]["timestamp_sec"] == 1.0
    assert hits[0]["similarity"] > hits[1]["similarity"]


def test_search_respects_source_filter_and_can_expand_past_other_projects(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    for index in range(12):
        # Distractors in another project rank ahead of the requested source.
        _add_shot(
            store,
            project="other-project",
            asset="other-source",
            timestamp=float(index),
            embedding=_vector(1, index / 100),
        )
    _add_shot(store, project="target", asset="source-1", timestamp=1.0, embedding=_vector(0, 1))
    _add_shot(store, project="target", asset="source-2", timestamp=2.0, embedding=_vector(0.1, 0.9))

    hits = store.search_clip_shots(
        "target",
        _vector(0, 1),
        top_k=2,
        source_asset_id="source-2",
    )

    assert len(hits) == 1
    assert hits[0]["project_id"] == "target"
    assert hits[0]["source_asset_id"] == "source-2"
    assert hits[0]["timestamp_sec"] == 2.0
