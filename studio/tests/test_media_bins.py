from __future__ import annotations

from pathlib import Path

from comfy_studio.media_bins import find_ffmpeg, find_ffprobe


def test_configured_ffmpeg_path_is_resolved_without_platform_assumptions(
    monkeypatch,
) -> None:
    monkeypatch.setenv("STUDIO_FFMPEG_BIN", "/custom tools/ffmpeg")
    monkeypatch.setattr(
        "comfy_studio.media_bins.shutil.which",
        lambda candidate: candidate if candidate == "/custom tools/ffmpeg" else None,
    )
    assert find_ffmpeg() == "/custom tools/ffmpeg"


def test_configured_ffprobe_override_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_FFPROBE_BIN", "/custom tools/ffprobe")
    monkeypatch.setattr(
        "comfy_studio.media_bins.shutil.which",
        lambda candidate: candidate if candidate == "/custom tools/ffprobe" else None,
    )
    assert find_ffprobe() == "/custom tools/ffprobe"


def test_ffprobe_is_found_beside_configured_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.touch()
    ffprobe.touch()
    monkeypatch.setenv("STUDIO_FFMPEG_BIN", str(ffmpeg))
    monkeypatch.delenv("STUDIO_FFPROBE_BIN", raising=False)
    monkeypatch.setattr(
        "comfy_studio.media_bins.shutil.which",
        lambda candidate: str(ffmpeg) if candidate == str(ffmpeg) else None,
    )
    assert find_ffprobe() == str(ffprobe)


def test_invalid_explicit_override_does_not_silently_use_another_binary(monkeypatch) -> None:
    monkeypatch.setenv("STUDIO_FFMPEG_BIN", "/missing/custom-ffmpeg")
    monkeypatch.setattr(
        "comfy_studio.media_bins.shutil.which",
        lambda candidate: "/usr/bin/ffmpeg" if candidate == "ffmpeg" else None,
    )
    assert find_ffmpeg() is None
