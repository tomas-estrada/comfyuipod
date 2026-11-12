"""Read-only search over the reviewed Blender Extensions metadata snapshot."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "blender-extensions"
    / "catalog-5.2.4-linux-x64.json"
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_'-]*", re.IGNORECASE)


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, Any]:
    payload = json.loads(_CATALOG.read_text(encoding="utf-8"))
    if payload.get("blender_version") != "5.2.4" or payload.get("platform") != "linux-x64":
        raise ValueError("Blender extension catalogue target mismatch")
    if not isinstance(payload.get("extensions"), list):
        raise ValueError("Blender extension catalogue has no extension list")
    return payload


def search_extensions(
    query: str,
    *,
    category: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Find candidates; results are metadata only and cannot install code."""
    terms = set(token.lower() for token in _TOKEN.findall(query))
    catalog = _load_catalog()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for entry in catalog["extensions"]:
        tags = entry.get("tags") or []
        if category and category.casefold() not in {str(tag).casefold() for tag in tags}:
            continue
        searchable = " ".join(
            str(value)
            for value in (
                entry.get("id"),
                entry.get("name"),
                entry.get("tagline"),
                " ".join(map(str, tags)),
                " ".join(map(str, entry.get("license") or [])),
            )
        ).lower().replace("_", " ").replace("-", " ")
        score = sum(
            3 if term in str(entry.get("name", "")).lower() else 1
            for term in terms
            if term in searchable
        )
        if score or not terms:
            ranked.append((score, entry))
    ranked.sort(key=lambda item: (-item[0], str(item[1].get("name", "")).casefold()))
    return {
        "catalog": {
            "source": catalog["source"],
            "retrieved_at": catalog["retrieved_at"],
            "blender_version": catalog["blender_version"],
            "platform": catalog["platform"],
            "total_extensions": len(catalog["extensions"]),
        },
        "query": query,
        "category": category,
        "total_matches": len(ranked),
        "results": [
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "version": entry.get("version"),
                "tagline": entry.get("tagline"),
                "archive_size": entry.get("archive_size"),
                "archive_hash": entry.get("archive_hash"),
                "archive_url": entry.get("archive_url"),
                "blender_version_min": entry.get("blender_version_min"),
                "blender_version_max": entry.get("blender_version_max"),
                "license": entry.get("license") or [],
                "tags": entry.get("tags") or [],
                "permissions": entry.get("permissions") or {},
                "website": entry.get("website"),
                "discovery_only": True,
            }
            for _, entry in ranked[:limit]
        ],
        "notice": (
            "Catalogue metadata is for discovery only. A listing does not authorize "
            "installation or prove headless operation. Production use requires an "
            "immutable reviewed lock and an operation-specific smoke test."
        ),
    }
