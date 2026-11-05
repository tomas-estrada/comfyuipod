"""Avatar facial editable e identidad persistente (plan Ultra, W14 / A16).

Identidad persistente entre planos; cambiar camara/luz no regenera audio; gated
model con estado claro. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.face import (
    AssetAvailability,
    FaceError,
    FaceIdentity,
    FacePerformance,
    assert_gated_available,
    assert_identity_persists,
    edit_performance,
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
