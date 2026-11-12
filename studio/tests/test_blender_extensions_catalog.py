from __future__ import annotations

from comfy_studio.agent.tools import (
    create_default_tool_belt,
    create_restricted_belt,
)
from comfy_studio.blender_extensions import search_extensions


def test_catalog_search_returns_version_hash_and_discovery_boundary() -> None:
    result = search_extensions("layered paint texture", limit=5)

    assert result["catalog"]["blender_version"] == "5.2.4"
    assert result["catalog"]["platform"] == "linux-x64"
    assert result["catalog"]["total_extensions"] == 1162
    assert result["results"]
    assert all(item["archive_hash"].startswith("sha256:") for item in result["results"])
    assert all(item["discovery_only"] for item in result["results"])
    assert "does not authorize" in result["notice"]


def test_catalog_category_filter_and_empty_query_are_bounded() -> None:
    result = search_extensions("", category="Rigging", limit=3)

    assert result["total_matches"] > len(result["results"]) == 3
    assert all("Rigging" in item["tags"] for item in result["results"])


def test_blender_catalog_search_is_available_only_to_artist3d() -> None:
    belt = create_default_tool_belt()

    assert belt.get("search_blender_extensions") is not None
    assert "search_blender_extensions" in create_restricted_belt(belt, "artist3d").list_names()
    assert "search_blender_extensions" not in create_restricted_belt(belt, "director").list_names()
    assert "search_blender_extensions" not in create_restricted_belt(belt, "editor").list_names()
