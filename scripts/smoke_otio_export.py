#!/usr/bin/env python3
"""Build-time smoke for the pinned OpenTimelineIO installation and export path."""

from __future__ import annotations

from importlib.metadata import distribution
from pathlib import Path

import opentimelineio as otio
from comfy_studio.edl.otio_export import edl_to_otio_text


def main() -> None:
    installed = distribution("opentimelineio")
    license_names = {Path(path).name for path in installed.files or []}
    if not {"LICENSE.txt", "NOTICE.txt"}.issubset(license_names):
        raise SystemExit("OpenTimelineIO wheel is missing LICENSE.txt or NOTICE.txt")

    serialized = edl_to_otio_text(
        {
            "schema": "comfy_studio.edl.v1",
            "title": "Image build smoke",
            "fps": 24,
            "source_asset_id": "smoke-asset",
            "cuts": [{"in_sec": 0.0, "out_sec": 1.0}],
        },
        project_id="build-smoke",
    )
    timeline = otio.adapters.read_from_string(serialized)
    if not isinstance(timeline, otio.schema.Timeline) or len(timeline.tracks[0]) != 1:
        raise SystemExit("OpenTimelineIO export did not round-trip one EDL cut")
    print("[OK] OpenTimelineIO pinned export and bundled notices")


if __name__ == "__main__":
    main()
