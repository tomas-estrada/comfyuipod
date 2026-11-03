"""Cross-platform discovery for the FFmpeg toolchain used by Studio."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _configured_binary(env_name: str, default_name: str) -> str | None:
    """Resolve an explicit executable override, otherwise search the current PATH."""
    override = (os.environ.get(env_name) or "").strip()
    if override:
        # shutil.which handles bare commands, absolute paths, PATHEXT on Windows,
        # and executable checks consistently across supported platforms.
        return shutil.which(override)
    return shutil.which(default_name)


def find_ffmpeg() -> str | None:
    """Return the configured ffmpeg executable or the one found on PATH."""
    return _configured_binary("STUDIO_FFMPEG_BIN", "ffmpeg")


def find_ffprobe() -> str | None:
    """Return the configured ffprobe, or its ffmpeg sibling, then PATH fallback."""
    override = (os.environ.get("STUDIO_FFPROBE_BIN") or "").strip()
    if override:
        return shutil.which(override)

    ffmpeg = find_ffmpeg()
    if ffmpeg:
        suffix = Path(ffmpeg).suffix
        sibling = Path(ffmpeg).with_name(f"ffprobe{suffix}")
        if sibling.is_file():
            return str(sibling)

    return shutil.which("ffprobe")
