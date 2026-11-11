"""Shim: re-exporta el módulo de reparación localizada desde edl/."""

from __future__ import annotations

from .edl.localized_repair import *  # noqa: F401,F403
from .edl.localized_repair import (  # noqa: F401
    DEFAULT_FRAME_TOLERANCE,
    LocalizedRepairError,
    build_localized_repair_video,
    localized_repair,
    localized_repair_video,
    perform_localized_repair,
    repair_localized,
    verify_frame_provenance,
    verify_source_frame_provenance,
)
