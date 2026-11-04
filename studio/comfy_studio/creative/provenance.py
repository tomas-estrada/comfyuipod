"""Provenance helpers shared by review and delivery assembly."""

from __future__ import annotations

from typing import Any


def source_asset_ids_for_output(
    store: Any, project_id: str, output_asset_id: str | None
) -> list[str]:
    """Return source asset IDs from the EDL version that produced this output."""
    if not output_asset_id:
        return []
    for version in store.list_edl_versions(project_id):
        if version.get("output_asset_id") != output_asset_id:
            continue
        edl = version.get("edl") or {}
        cuts = edl.get("cuts") or []
        source_ids: list[str] = []
        for cut in cuts:
            if not isinstance(cut, dict):
                continue
            asset_id = cut.get("asset_id") or cut.get("source_asset_id")
            if asset_id and str(asset_id) not in source_ids:
                source_ids.append(str(asset_id))
        return source_ids
    return []


def linked_transcriptions(
    store: Any, project_id: str, output_asset_id: str | None
) -> list[dict[str, Any]]:
    """Find exportable transcriptions belonging to sources in an output's EDL."""
    transcriptions: list[dict[str, Any]] = []
    for source_id in source_asset_ids_for_output(store, project_id, output_asset_id):
        for item in store.list_transcriptions(project_id, asset_id=source_id):
            segments = item.get("segments") or []
            if any(
                isinstance(segment, dict)
                and str(segment.get("text") or "").strip()
                and segment.get("start") is not None
                and segment.get("end") is not None
                for segment in segments
            ):
                transcriptions.append(item)
    return transcriptions
