"""Export the supported EDL v1 cut list as an OpenTimelineIO document."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote


def edl_to_otio_text(edl: dict[str, Any], *, project_id: str) -> str:
    """Serialize a cut-only EDL as OTIO, using Studio asset URLs as media references.

    EDL v1 has no transition/effect interchange contract. Reject those fields
    rather than silently dropping editorial intent during export.
    """
    try:
        import opentimelineio as otio
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise RuntimeError("OpenTimelineIO no está instalado en este entorno") from exc

    cuts = edl.get("cuts")
    if not isinstance(cuts, list) or not cuts:
        raise ValueError("EDL sin cortes exportables")
    unsupported_document_fields = {
        key for key in edl if key in {"transition", "transitions", "effect", "effects"}
    }
    if unsupported_document_fields:
        raise ValueError(
            "EDL v1 no exporta " + ", ".join(sorted(unsupported_document_fields))
        )
    fps = edl.get("fps", 24)
    if (
        isinstance(fps, bool)
        or not isinstance(fps, (int, float))
        or not math.isfinite(float(fps))
        or fps <= 0
        or fps > 240
    ):
        raise ValueError("fps del EDL debe estar en el rango (0, 240]")
    rate = float(fps)
    timeline = otio.schema.Timeline(name=str(edl.get("title") or "ComfyUIPod EDL"))
    track = otio.schema.Track(name="Video", kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)
    encoded_project = quote(str(project_id), safe="")
    default_asset_id = edl.get("source_asset_id")

    for index, cut in enumerate(cuts):
        if not isinstance(cut, dict):
            raise ValueError(f"Corte #{index + 1}: se esperaba un objeto")
        unsupported = {key for key in cut if key in {"transition", "effect", "effects"}}
        if unsupported:
            raise ValueError(
                f"Corte #{index + 1}: OTIO v1 no exporta {', '.join(sorted(unsupported))}"
            )
        asset_id = cut.get("asset_id") or default_asset_id
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError(f"Corte #{index + 1}: falta asset_id de origen")
        try:
            source_start = float(cut["in_sec"])
            source_end = float(cut["out_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Corte #{index + 1}: in_sec/out_sec inválidos") from exc
        if (
            not math.isfinite(source_start)
            or not math.isfinite(source_end)
            or source_start < 0
            or source_end <= source_start
        ):
            raise ValueError(f"Corte #{index + 1}: rango fuente inválido")

        start_frame = round(source_start * rate)
        end_frame = round(source_end * rate)
        duration_frames = end_frame - start_frame
        if duration_frames <= 0:
            raise ValueError(f"Corte #{index + 1}: rango menor a un cuadro")
        media_url = (
            f"/studio/api/projects/{encoded_project}/assets/"
            f"{quote(asset_id, safe='')}/file"
        )
        reference = otio.schema.ExternalReference(target_url=media_url)
        clip = otio.schema.Clip(
            name=f"Corte {index + 1}",
            media_reference=reference,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(start_frame, rate),
                duration=otio.opentime.RationalTime(duration_frames, rate),
            ),
        )
        track.append(clip)

    return otio.adapters.write_to_string(timeline, adapter_name="otio_json")


def edl_to_otio_json(edl: dict[str, Any], *, project_id: str) -> dict[str, Any]:
    """Return the OTIO document object when a JSON package is preferred."""
    return json.loads(edl_to_otio_text(edl, project_id=project_id))
