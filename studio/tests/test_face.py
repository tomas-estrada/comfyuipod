"""Avatar facial editable e identidad persistente (plan Ultra, W14 / A16).

Identidad persistente entre planos; cambiar camara/luz no regenera audio; gated
model con estado claro. Puro CPU.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from comfy_studio.face import (
    AssetAvailability,
    FaceError,
    FaceIdentity,
    FacePerformance,
    assert_gated_available,
    assert_identity_persists,
    compile_spanish_curve,
    edit_performance,
    measure_speaker_fixture,
    render_morph_glb,
    spanish_visemes,
    speaker_tracks,
)


def _perf(shot: str, camera: str = "cam1", light: str = "key_warm") -> FacePerformance:
    return FacePerformance(
        performance_id=f"perf_{shot}",
        identity_id="actor_ana",
        audio_track_id="voz_ana",
        frames=[{"jaw_open": 0.4, "smile": 0.2}],
        camera_id=camera,
        light_id=light,
    )


def test_identity_persists_across_shots() -> None:
    """A16: memoria/identidad entre planos."""
    assert_identity_persists(_perf("s1"), _perf("s2"))  # mismo actor: no lanza


def test_identity_mismatch_rejected() -> None:
    a = _perf("s1")
    b = _perf("s2")
    b.identity_id = "otro_actor"
    with pytest.raises(FaceError, match="no persistente"):
        assert_identity_persists(a, b)


def test_camera_change_does_not_regenerate_audio() -> None:
    """A16: cambio de camara/luz sin regenerar audio."""
    assert edit_performance(_perf("s1"), change="camera") == {"render"}
    assert edit_performance(_perf("s1"), change="light") == {"render"}


def test_script_change_recomputes_lipsync() -> None:
    """Cambiar el guion (audio) si recomputa la actuacion labial."""
    out = edit_performance(_perf("s1"), change="script")
    assert "audio" in out and "lipsync" in out


def test_unknown_edit_rejected() -> None:
    with pytest.raises(FaceError, match="desconocido"):
        edit_performance(_perf("s1"), change="telepathy")


def test_morph_weights_validated() -> None:
    perf = _perf("s1")
    perf.frames = [{"jaw_open": 1.5}]  # fuera de [0,1]
    with pytest.raises(FaceError, match="fuera de"):
        perf.validate()


def test_gated_model_missing_gives_clear_status_not_fake_free() -> None:
    """A16: un modelo gated ausente devuelve estado claro, no se finge libre."""
    gated = FaceIdentity(
        identity_id="celebrity",
        name="X",
        availability=AssetAvailability.GATED_MISSING,
    )
    with pytest.raises(FaceError, match="gated ausente"):
        assert_gated_available(gated)


def test_available_identity_passes() -> None:
    ok = FaceIdentity(identity_id="ana", name="Ana")
    assert_gated_available(ok)  # no lanza


def test_weights_license_is_declared_non_commercial() -> None:
    """Los pesos faciales (ReActor/InsightFace) son non-commercial, declarado."""
    ana = FaceIdentity(identity_id="ana", name="Ana")
    assert ana.weights_license == "non-commercial"


def test_spanish_two_speaker_tracks_preserve_turns_and_map_visemes() -> None:
    turns = [
        {
            "speaker_id": "ana",
            "audio_track_id": "stem_ana",
            "start": 0.0,
            "end": 1.0,
            "audio_asset_id": "asset_a",
            "audio_sha256": "a" * 64,
            "text": "Hola, Medellín",
        },
        {
            "speaker_id": "luis",
            "audio_track_id": "stem_luis",
            "start": 1.0,
            "end": 2.0,
            "audio_asset_id": "asset_b",
            "audio_sha256": "b" * 64,
            "text": "¿Qué tal?",
        },
    ]
    tracks = speaker_tracks(turns)
    assert list(tracks) == ["ana", "luis"]
    assert tracks["ana"][0]["audio_asset_id"] == "asset_a"
    assert tracks["ana"][0]["audio_sha256"] == "a" * 64
    assert tracks["ana"][0]["end"] == tracks["luis"][0]["start"]
    visemes = spanish_visemes("Hola", start=0.0, end=0.8)
    assert [v["viseme"] for v in visemes] == ["closed", "round", "closed", "open"]
    assert visemes[0]["start"] == 0.0 and visemes[-1]["end"] == 0.8


def test_blender_exports_editable_shape_key_animation(tmp_path: Path) -> None:
    """CPU artifact integration on a synthetic rig fixture, never model quality evidence."""
    blender = shutil.which("blender")
    if not blender:
        pytest.skip("Blender not installed")
    source, output = tmp_path / "rig.glb", tmp_path / "animated.glb"
    expr = (
        "import bpy; bpy.ops.mesh.primitive_cube_add(); o=bpy.context.object; "
        + "o.shape_key_add(name='Basis'); k=o.shape_key_add(name='jaw_open'); "
        + "[setattr(v.co,'z',v.co.z+0.2) for v in k.data]; "
        + f"bpy.ops.export_scene.gltf(filepath={str(source)!r},"
        + "export_format='GLB',export_morph=True)"
    )
    subprocess.run(
        [blender, "--background", "--python-expr", expr],
        check=True,
        capture_output=True,
        timeout=120,
    )
    provenance = render_morph_glb(
        str(source),
        str(output),
        {
            "curve_id": "hola",
            "speaker_id": "ana",
            "audio_track_id": "voz_ana",
            "times": [0.0, 0.5],
            "weights": [{"jaw_open": 0.0}, {"jaw_open": 0.8}],
        },
        blender_bin=blender,
    )
    assert provenance["cpu_artifact"] is True
    raw = output.read_bytes()
    assert raw[:4] == b"glTF"
    size, kind = struct.unpack_from("<II", raw, 12)
    doc = json.loads(raw[20 : 20 + size].decode("utf-8").rstrip(" \0"))
    assert doc["animations"]
    animated_meshes = [mesh for mesh in doc["meshes"] if mesh["primitives"][0].get("targets")]
    assert animated_meshes
    assert animated_meshes[0]["extras"]["targetNames"] == ["jaw_open"]
    assert any(
        channel["target"]["path"] == "weights"
        for animation in doc["animations"]
        for channel in animation["channels"]
    )


def test_two_speaker_fixture_metrics_are_explicitly_fixture_only() -> None:
    result = measure_speaker_fixture(
        predicted_speakers=["ana", "luis"],
        expected_speakers=["ana", "luis"],
        predicted_boundaries=[1.04],
        expected_boundaries=[1.0],
        viseme_boundaries=[0.48, 1.55],
        audio_boundaries=[0.5, 1.5],
    )
    assert result == {
        "scope": "fixture_only",
        "speaker_identity_accuracy": 1.0,
        "turn_boundary_mae_seconds": pytest.approx(0.04),
        "lip_drift_mae_seconds": pytest.approx(0.035),
    }


def test_spanish_visemes_compile_to_declared_rig_morph_keys() -> None:
    curve = compile_spanish_curve(
        curve_id="hola",
        speaker_id="ana",
        audio_track_id="voz_ana",
        text="Hola",
        start=2.0,
        end=3.0,
        rig_mapping={"closed": "mouth_closed", "round": "mouth_round", "open": "jaw_open"},
    )
    assert curve.audio_track_id == "voz_ana"
    assert curve.times == pytest.approx([2.125, 2.375, 2.625, 2.875])
    assert curve.weights[1]["mouth_round"] == 0.8
