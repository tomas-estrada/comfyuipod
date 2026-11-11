"""Módulos de EDL (generación de highlights y ejecución determinista sobre `ffmpeg`)."""

from __future__ import annotations

from .ffmpeg_edl import (
    ASPECT_DIMENSIONS,
    execute_edl_ffmpeg,
    probe_media_file,
    resolve_aspect_dimensions,
)
from .highlights import (
    AspectRatioType,
    build_edl_cuts_from_transcription,
    build_highlights_cuts_from_transcription,
    generate_highlights_edl,
)
from .localized_repair import (
    LocalizedRepairError,
    build_localized_repair_video,
    localized_repair,
    verify_source_frame_provenance,
)

__all__ = [
    "ASPECT_DIMENSIONS",
    "AspectRatioType",
    "build_edl_cuts_from_transcription",
    "build_highlights_cuts_from_transcription",
    "execute_edl_ffmpeg",
    "generate_highlights_edl",
    "probe_media_file",
    "resolve_aspect_dimensions",
    "LocalizedRepairError",
    "build_localized_repair_video",
    "localized_repair",
    "verify_source_frame_provenance",
]
