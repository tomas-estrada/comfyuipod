from __future__ import annotations

from pathlib import Path
from typing import Any

from comfy_studio.media_bins import find_ffmpeg
from comfy_studio.perception import clip_index
from comfy_studio.perception.clip_index import ClipVisualIndex
from comfy_studio.perception.keyframes import keyframe_extraction_cache_key
from comfy_studio.perception.store import CLIP_EMBED_DIM, PerceptionVectorStore
from comfy_studio.projects import ProjectStore


def _embedding(*values: float) -> list[float]:
    return [*values, *([0.0] * (CLIP_EMBED_DIM - len(values)))]


def _make_clip_index(monkeypatch: Any, tmp_path: Path) -> tuple[ProjectStore, ClipVisualIndex]:
    store = ProjectStore(tmp_path / "studio")
    monkeypatch.setattr(
        clip_index,
        "extract_image_rgb_signature",
        lambda _path: {
            "rgb_mean": [40.0, 80.0, 120.0],
            "visual_tags": ["azul"],
        },
    )
    index = ClipVisualIndex(store, PerceptionVectorStore(store.data_dir / "index.db"))
    return store, index


def test_reindex_unchanged_image_reuses_cached_embedding_and_mutations_invalidate(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls = 0

    def fake_embed(*_args: Any, **_kwargs: Any) -> list[float]:
        nonlocal calls
        calls += 1
        return _embedding(1.0, 0.0)

    monkeypatch.setattr(clip_index, "embed_image_clip", fake_embed)
    store, index = _make_clip_index(monkeypatch, tmp_path)
    project = store.create_project(name="embedding cache")
    image = store.save_upload(
        project["id"],
        filename="frame.png",
        content_type="image/png",
        data=b"image-v1",
        max_upload_bytes=1024,
    )

    first = index.index_project_media(project["id"])
    second = index.index_project_media(project["id"])
    assert first["shots_indexed"] == second["shots_indexed"] == 1
    assert calls == 1

    image_path = store.get_asset_path(project["id"], image["id"])
    assert image_path is not None
    image_path.write_bytes(b"image-v2-content-changed")
    index.index_project_media(project["id"])
    assert calls == 2


def test_targeted_video_reindex_skips_current_frames_and_invalidates_changed_source(
    monkeypatch: Any, tmp_path: Path
) -> None:
    embed_calls = 0
    extraction_calls = 0

    def fake_embed(*_args: Any, **_kwargs: Any) -> list[float]:
        nonlocal embed_calls
        embed_calls += 1
        return _embedding(1.0, 0.0)

    monkeypatch.setattr(clip_index, "embed_image_clip", fake_embed)
    monkeypatch.setattr(
        clip_index,
        "extract_image_rgb_signature",
        lambda _path: {"rgb_mean": [40.0, 80.0, 120.0], "visual_tags": ["azul"]},
    )
    store = ProjectStore(tmp_path / "studio")
    index = ClipVisualIndex(store, PerceptionVectorStore(store.data_dir / "index.db"))
    project = store.create_project(name="video cache")
    video = store.save_upload(
        project["id"],
        filename="source.mp4",
        content_type="video/mp4",
        data=b"video-v1",
        max_upload_bytes=1024,
    )
    video_path = store.get_asset_path(project["id"], video["id"])
    assert video_path is not None

    def fake_extract(
        _store: ProjectStore,
        project_id: str,
        source_id: str,
        *,
        scene_threshold: float,
        interval_sec: float,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        nonlocal extraction_calls
        extraction_calls += 1
        for old in _store.list_assets(project_id, category="keyframes"):
            if (old.get("metadata") or {}).get("source_asset_id") == source_id:
                _store.delete_asset(project_id, old["id"])
        cache_key = keyframe_extraction_cache_key(
            video_path,
            scene_threshold=scene_threshold,
            interval_sec=interval_sec,
            ffmpeg_bin=find_ffmpeg(),
        )
        frame = _store.save_upload(
            project_id,
            filename=f"frame-{extraction_calls}.png",
            content_type="image/png",
            data=b"frame-bytes",
            max_upload_bytes=1024,
            category="keyframes",
            metadata={
                "source_asset_id": source_id,
                "timestamp_sec": 0.0,
                "caption": "frame caption",
                "visual_tags": ["azul"],
                "rgb_mean": [40.0, 80.0, 120.0],
                "keyframe_extraction_cache_key": cache_key,
            },
        )
        return [{"asset_id": frame["id"]}]

    monkeypatch.setattr(clip_index, "extract_video_keyframes", fake_extract)
    first = index.index_asset(project["id"], video["id"])
    second = index.index_asset(project["id"], video["id"])
    assert first["extracted_keyframes_count"] == 1
    assert second["extracted_keyframes_count"] == 0
    assert extraction_calls == 1
    assert embed_calls == 1

    changed_settings = index.index_asset(project["id"], video["id"], interval_sec=4.0)
    assert changed_settings["extracted_keyframes_count"] == 1
    assert extraction_calls == 2

    video_path.write_bytes(b"video-v2-content-changed")
    third = index.index_asset(project["id"], video["id"])
    assert third["extracted_keyframes_count"] == 1
    assert extraction_calls == 3
