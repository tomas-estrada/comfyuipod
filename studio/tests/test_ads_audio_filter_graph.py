from pathlib import Path

import pytest

from comfy_studio.ads.pipeline import _Segment, _ffmpeg_variant_cmd


@pytest.mark.parametrize(
    ("voice", "music", "required_filters"),
    [
        (True, False, ["[1:a]aresample=44100[a_voice]", "[a_voice]anull[aout]"]),
        (False, True, ["[1:a]aresample=44100[a_music]", "[a_music]anull[aout]"]),
        (
            True,
            True,
            [
                "[1:a]aresample=44100[a_voice]",
                "[2:a]aresample=44100[a_music]",
                "[a_voice][a_music]amix=inputs=2:duration=shortest:dropout_transition=0[aout]",
            ],
        ),
    ],
)
def test_audio_filter_complex_contains_each_producer_before_consumer(
    tmp_path: Path,
    voice: bool,
    music: bool,
    required_filters: list[str],
) -> None:
    cmd = _ffmpeg_variant_cmd(
        [_Segment(path=tmp_path / "video.mp4", kind="video", duration_s=2.0)],
        width=1280,
        height=720,
        fps=24,
        out_path=tmp_path / "out.mp4",
        voice_path=tmp_path / "voice.wav" if voice else None,
        music_path=tmp_path / "music.wav" if music else None,
        total_duration_s=2.0,
    )
    graph = cmd[cmd.index("-filter_complex") + 1].split(";")

    positions = [graph.index(part) for part in required_filters]
    assert positions == sorted(positions)
    assert cmd[cmd.index("-map") + 2 : cmd.index("-c:v")] == [
        "-map",
        "[aout]",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
    ]


def test_video_only_filter_graph_has_no_audio_stages(tmp_path: Path) -> None:
    cmd = _ffmpeg_variant_cmd(
        [_Segment(path=tmp_path / "video.mp4", kind="video", duration_s=2.0)],
        width=1280,
        height=720,
        fps=24,
        out_path=tmp_path / "out.mp4",
        voice_path=None,
        music_path=None,
        total_duration_s=2.0,
    )

    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "aresample" not in graph
    assert "amix" not in graph
    assert "-an" in cmd
