"""Plan de sonido por escena acotado en CPU (C25 / A20).

TDD: el plan tipado mapea acciones declaradas del video fuente (pasos,
frenado, ambiente) y sus intervalos a muestras de audio PROPIAS del proyecto
y stems editables. Exige onset dentro de la ventana de su acción (con
tolerancia), alineación al timebase/grid de frames del video fuente, mide el
onset real renderizado y el loudness/true peak de salida, y persiste
procedencia con hashes de fuente/acción/muestra/salida. Sin generación
neural ni separación por modelo (CPU + FFmpeg acotado por argv).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from comfy_studio.audio import SampleValidationError
from comfy_studio.scene_sound import (
    DEFAULT_ONSET_TOLERANCE_S,
    MAX_ACTIONS,
    MAX_CUES,
    MAX_ONSET_TOLERANCE_S,
    SCENE_SOUND_PLAN_SCHEMA,
    SCENE_SOUND_PROVENANCE_SCHEMA,
    SceneSoundCue,
    SceneSoundError,
    SceneSoundPlan,
    VideoAction,
    build_mix,
    frame_of_cue,
    measure_first_onset_s,
    render_scene_sound,
    verify_source_timebase,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

FF_REQUIRED = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe requeridos para el render real de sonido por escena",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _actions() -> list[VideoAction]:
    return [
        VideoAction("walk-a", "steps", 1.0, 6.0),
        VideoAction("brake-a", "braking", 7.0, 9.0),
        VideoAction("room-a", "ambience", 0.0, 15.0),
    ]


def _cues() -> list[SceneSoundCue]:
    rights = {"sample_license": "user-owned", "sample_attribution": "Provided by project owner"}
    return [
        SceneSoundCue("amb-on-1", "room-a", "ambience", "amb", 0.5, **rights),
        SceneSoundCue("step-on-1", "walk-a", "steps", "step", 2.0, **rights),
        SceneSoundCue("step-on-2", "walk-a", "steps", "step", 2.5, **rights),
        SceneSoundCue("brake-on-1", "brake-a", "braking", "brake", 7.5, **rights),
    ]


def _cue(cue_id: str, action_id: str, onset: float) -> SceneSoundCue:
    return SceneSoundCue(
        cue_id,
        action_id,
        "steps",
        "step",
        onset,
        sample_license="user-owned",
        sample_attribution="Provided by project owner",
    )


def _plan(**overrides) -> SceneSoundPlan:
    fields = {
        "source_video_asset_id": "video-1",
        "fps": 24,
        "duration_s": 15.0,
        "actions": _actions(),
        "cues": _cues(),
    }
    fields.update(overrides)
    return SceneSoundPlan(**fields)


def _make_sample(path: Path, *, freq: int = 440, seconds: float = 0.4, gain: float = 4.0) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-af",
            f"volume={gain},aformat=sample_rates=48000:channel_layouts=stereo",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _make_video(path: Path, *, seconds: float = 15.0, fps: int = 24) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate={fps}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            "-q:v",
            "4",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _independent_loudness(path: Path) -> tuple[float, float]:
    """Medición independiente (loudnorm JSON) para comparar con lo reportado."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-v",
            "info",
            "-i",
            str(path),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    blocks = [b for b in re.findall(r"\{[^{}]*\}", proc.stderr) if '"input_i"' in b]
    assert blocks, "loudnorm no devolvió medición JSON"
    data = json.loads(blocks[-1])
    return float(data["input_i"]), float(data["input_tp"])


# --- Unidades puras (sin ffmpeg) --------------------------------------------------


def test_plan_rejects_onset_outside_action_window_and_tolerance() -> None:
    """El onset debe caer dentro de la ventana de su acción ± tolerancia."""
    with pytest.raises(SceneSoundError, match="ventana"):
        _plan(cues=[_cues()[0], _cue("step-late", "walk-a", 6.5)]).validate()
    # Dentro de la tolerancia (0.25 s) sí es válido: 6.2 <= 6.0 + 0.25.
    plan = _plan(cues=[_cues()[0], _cue("step-edge", "walk-a", 6.2)])
    plan.validate()


def test_plan_validation_rejects_invalid_declarations() -> None:
    with pytest.raises(SceneSoundError, match="kind"):
        _plan(actions=[VideoAction("a", "explosion", 0.0, 1.0)]).validate()
    with pytest.raises(SceneSoundError, match="duplicad"):
        _plan(
            actions=[
                VideoAction("walk-a", "steps", 0.0, 2.0),
                VideoAction("walk-a", "steps", 3.0, 4.0),
            ]
        ).validate()
    with pytest.raises(SceneSoundError, match="unknown-action"):
        _plan(cues=[SceneSoundCue(
            "x", "unknown-action", "steps", "step", 1.0,
            sample_license="user-owned", sample_attribution="Provided by project owner",
        )]).validate()
    with pytest.raises(SceneSoundError, match="ventana"):
        _plan(actions=[VideoAction("walk-a", "steps", 5.0, 3.0)]).validate()
    with pytest.raises(SceneSoundError, match="tolerancia"):
        _plan(onset_tolerance_s=MAX_ONSET_TOLERANCE_S + 0.1).validate()
    assert MAX_ONSET_TOLERANCE_S >= DEFAULT_ONSET_TOLERANCE_S > 0
    with pytest.raises(SceneSoundError, match="fps"):
        _plan(fps=0).validate()
    with pytest.raises(SceneSoundError, match="duration_s"):
        _plan(duration_s=0.0).validate()
    too_many_actions = [VideoAction(f"a{i}", "steps", 0.0, 1.0) for i in range(MAX_ACTIONS + 1)]
    with pytest.raises(SceneSoundError, match="demasiadas acciones"):
        _plan(actions=too_many_actions).validate()
    too_many_cues = [
        SceneSoundCue(
            f"c{i}", "walk-a", "steps", "step", 1.0 + i * 0.01,
            sample_license="user-owned", sample_attribution="Provided by project owner",
        )
        for i in range(MAX_CUES + 1)
    ]
    with pytest.raises(SceneSoundError, match="demasiados cues"):
        _plan(cues=too_many_cues).validate()
    with pytest.raises(SceneSoundError, match="sample_license"):
        _plan(cues=[SceneSoundCue("x", "walk-a", "steps", "step", 2.0)]).validate()


def test_plan_round_trips_and_records_frame_placement() -> None:
    plan = _plan()
    data = plan.to_dict()
    assert data["schema"] == SCENE_SOUND_PLAN_SCHEMA
    restored = SceneSoundPlan.from_dict(data)
    assert restored.to_dict() == data
    # 2.0 s @24fps -> frame 48; 7.5 s -> frame 180.
    assert frame_of_cue(restored.cues[1], 24) == 48
    assert frame_of_cue(restored.cues[3], 24) == 180


def test_build_mix_separates_stems_and_snaps_onsets_to_frame_grid() -> None:
    from comfy_studio.audio import assert_separate_stems, mix_digest

    mix, placement = build_mix(_plan())
    assert {stem.name for stem in mix.stems} == {"steps", "braking", "ambience"}
    assert_separate_stems(mix)
    events = {event.event_id: event for event in mix.all_events()}
    assert events["step-on-1"].onset_s == 2.0
    assert events["step-on-1"].caused_by == "walk-a"
    assert events["step-on-1"].sample_asset_id == "step"
    assert events["brake-on-1"].caused_by == "brake-a"
    assert events["amb-on-1"].onset_s == 0.5
    # Cada cue guarda su colocación efectiva (onset alineado al frame + frame).
    assert placement["step-on-1"] == (2.0, 48)
    assert placement["brake-on-1"] == (7.5, 180)
    assert placement["amb-on-1"] == (0.5, 12)
    assert mix_digest(mix)


def test_build_mix_rejects_frame_rounding_outside_action_window() -> None:
    plan = _plan(
        actions=[VideoAction("walk-a", "steps", 0.5005, 4.0)],
        cues=[_cue("step-on-1", "walk-a", 0.5005)],
        onset_tolerance_s=0.0001,
    )
    with pytest.raises(SceneSoundError, match="onset alineado"):
        build_mix(plan)


def test_source_timebase_fails_closed_for_fractional_fps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "comfy_studio.scene_sound.probe_source_timebase",
        lambda *args, **kwargs: {
            "fps": 29.97002997,
            "r_frame_rate": "30000/1001",
            "duration_s": 15.0,
            "frame_count": 450,
        },
    )
    with pytest.raises(SceneSoundError, match="timebase del video fuente incompatible"):
        verify_source_timebase(_plan(), "unused-video.mp4")


def test_measure_first_onset_s_rejects_silence_or_bad_file(tmp_path: Path) -> None:
    silence = tmp_path / "silence.wav"
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error", "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
            "-c:a", "pcm_s16le", str(silence),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert measure_first_onset_s(silence, ffmpeg_bin=FFMPEG) is None
    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"no es audio")
    with pytest.raises(SceneSoundError, match="legible"):
        measure_first_onset_s(fake, ffmpeg_bin=FFMPEG)


@FF_REQUIRED
def test_measure_first_onset_accepts_quiet_signal_below_absolute_floor(tmp_path: Path) -> None:
    quiet = tmp_path / "quiet.wav"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-af",
            "volume=0.02,aformat=sample_rates=48000:channel_layouts=stereo",
            "-c:a",
            "pcm_s16le",
            str(quiet),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    onset = measure_first_onset_s(quiet, ffmpeg_bin=FFMPEG)
    assert onset is not None
    assert onset <= 0.01


# --- Ensamble real con ffmpeg ------------------------------------------------------


def _render_kwargs() -> dict:
    return {"ffmpeg_bin": FFMPEG, "ffprobe_bin": FFPROBE, "timeout_s": 60.0}


@FF_REQUIRED
def test_render_scene_sound_maps_actions_to_editable_stems_and_measures_onsets(
    tmp_path: Path,
) -> None:
    video = tmp_path / "walk15.mp4"
    _make_video(video)
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    step = samples_dir / "step.wav"
    brake = samples_dir / "brake.wav"
    amb = samples_dir / "amb.wav"
    _make_sample(step, freq=440, seconds=0.4)
    _make_sample(brake, freq=880, seconds=1.0)
    _make_sample(amb, freq=220, seconds=15.0, gain=2.0)
    samples = {"step": step, "brake": brake, "amb": amb}
    out_dir = tmp_path / "out"

    result = render_scene_sound(_plan(), samples, video, out_dir, **_render_kwargs())

    # Stems separados y editables: un WAV distinto por stem (pasos/frenado/ambiente).
    stem_files = {entry.stem: entry.path for entry in result.rendered}
    assert set(stem_files) == {"steps", "braking", "ambience"}
    assert len({p.read_bytes() for p in stem_files.values()}) == 3

    # Cada evento aterriza en su acción: onset medido ≈ onset previsto (frame grid).
    measured = {event["cue_id"]: event for event in result.events}
    for cue_id, expected in (
        ("step-on-1", 2.0),
        ("step-on-2", 2.5),
        ("brake-on-1", 7.5),
        ("amb-on-1", 0.5),
    ):
        record = measured[cue_id]
        assert record["frame"] == round(expected * 24)
        assert abs(record["measured_onset_s"] - expected) <= 0.01
        assert record["persisted_stem_verified"] is True
        assert record["persisted_sample_correlation"] >= 0.8
        assert record["verified"] is True

    # El onset real del stem de pasos es el más temprano de sus eventos.
    assert abs(measured["step-on-1"]["measured_onset_s"] - 2.0) <= 0.01

    # Loudness/true peak reportados coinciden con una medición independiente.
    for entry in result.rendered:
        assert entry.integrated_loudness_lufs <= -0.1
        assert entry.true_peak_dbfs < -0.1
        independent_i, independent_tp = _independent_loudness(entry.path)
        assert abs(independent_i - entry.integrated_loudness_lufs) <= 0.5
        assert abs(independent_tp - entry.true_peak_dbfs) <= 0.5

    # Procedencia persistida con hashes de fuente/acción/muestra/salida.
    provenance_path = out_dir / "scene_sound_provenance.json"
    assert provenance_path.is_file()
    provenance = json.loads(provenance_path.read_text())
    assert provenance["schema"] == SCENE_SOUND_PROVENANCE_SCHEMA
    assert provenance["source_video"]["sha256"] == _sha256(video)
    assert provenance["source_video"]["name"] == "walk15.mp4"
    assert provenance["timebase"]["fps"] == 24
    assert provenance["timebase"]["source_probe"]["frame_count"] == 360
    assert [a["action_id"] for a in provenance["actions"]] == ["walk-a", "brake-a", "room-a"]
    assert provenance["samples"]["step"]["sha256"] == _sha256(step)
    assert provenance["samples"]["amb"]["license"] == "user-owned"
    stem_hashes = {s["stem"]: s["sha256"] for s in provenance["stems"]}
    assert stem_hashes["steps"] == _sha256(stem_files["steps"])
    assert provenance["events"] == result.events


@FF_REQUIRED
def test_render_scene_sound_is_byte_deterministic(tmp_path: Path) -> None:
    video = tmp_path / "walk15.mp4"
    _make_video(video)
    step = tmp_path / "step.wav"
    _make_sample(step, freq=440, seconds=0.4)
    plan = _plan(
        actions=[VideoAction("walk-a", "steps", 1.0, 6.0)],
        cues=[_cue("step-on-1", "walk-a", 2.0)],
    )
    samples = {"step": step}
    first = render_scene_sound(plan, samples, video, tmp_path / "out1", **_render_kwargs())
    second = render_scene_sound(plan, samples, video, tmp_path / "out2", **_render_kwargs())
    assert (tmp_path / "out1" / "scene_sound_provenance.json").read_bytes() == (
        tmp_path / "out2" / "scene_sound_provenance.json"
    ).read_bytes()
    for a, b in zip(first.rendered, second.rendered, strict=False):
        assert a.path.read_bytes() == b.path.read_bytes()


@FF_REQUIRED
def test_render_scene_sound_rejects_mismatched_source_timebase(tmp_path: Path) -> None:
    video = tmp_path / "walk15.mp4"
    _make_video(video)
    step = tmp_path / "step.wav"
    _make_sample(step, freq=440, seconds=0.4)
    samples = {"step": step}

    def _single_plan(**overrides) -> SceneSoundPlan:
        return _plan(
            actions=[VideoAction("walk-a", "steps", 1.0, 6.0)],
            cues=[_cue("step-on-1", "walk-a", 2.0)],
            **overrides,
        )

    with pytest.raises(SceneSoundError, match="timebase"):
        render_scene_sound(
            _single_plan(fps=30), samples, video, tmp_path / "out", **_render_kwargs()
        )
    with pytest.raises(SceneSoundError, match="incompatible"):
        render_scene_sound(
            _single_plan(duration_s=20.0), samples, video, tmp_path / "out", **_render_kwargs()
        )
    # Nada persistido tras el rechazo.
    assert not (tmp_path / "out" / "scene_sound_provenance.json").exists()


@FF_REQUIRED
def test_render_scene_sound_rejects_missing_sample(tmp_path: Path) -> None:
    video = tmp_path / "walk15.mp4"
    _make_video(video)
    with pytest.raises(SampleValidationError):
        render_scene_sound(
            _plan(),
            {
                "step": tmp_path / "missing.wav",
                "brake": tmp_path / "b.wav",
                "amb": tmp_path / "a.wav",
            },
            video,
            tmp_path / "out",
            **_render_kwargs(),
        )


@FF_REQUIRED
def test_render_scene_sound_rejects_missing_source_video(tmp_path: Path) -> None:
    step = tmp_path / "step.wav"
    _make_sample(step, freq=440, seconds=0.4)
    with pytest.raises(SceneSoundError, match="fuente"):
        render_scene_sound(
            _plan(
                actions=[VideoAction("walk-a", "steps", 1.0, 6.0)],
                cues=[_cue("step-on-1", "walk-a", 2.0)],
            ),
            {"step": step},
            tmp_path / "missing.mp4",
            tmp_path / "out",
            **_render_kwargs(),
        )
