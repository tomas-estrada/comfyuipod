"""Curva de morph por hablante, editable en el tiempo (W14 / A16, CPU).

La curva liga morfos a una pista de audio por `audio_track_id`: claves de
tiempo finitas, no negativas y ordenadas; pesos finitos en [0,1]. Cambiar
cámara/luz NO toca audio ni lip-sync; cambiar el guion SÍ invalida el motion
labial. Símbolico: no es evidencia de calidad neural.
"""

from __future__ import annotations

import math

import pytest

from comfy_studio.face import (
    FaceError,
    FacePerformance,
    SpeakerMorphCurve,
    assert_curve_matches_track,
    edit_performance,
)


def _curve(
    times: list[float] | None = None,
    weights: list[dict[str, float]] | None = None,
    audio_track_id: str = "voz_ana",
) -> SpeakerMorphCurve:
    return SpeakerMorphCurve(
        curve_id="curva_ana",
        speaker_id="actor_ana",
        audio_track_id=audio_track_id,
        times=[0.0, 1.0, 2.0] if times is None else times,
        weights=[
            {"jaw_open": 0.0, "smile": 0.1},
            {"jaw_open": 0.8, "smile": 0.2},
            {"jaw_open": 0.2, "smile": 0.0},
        ]
        if weights is None
        else weights,
    )


def _perf(audio_track_id: str = "voz_ana") -> FacePerformance:
    return FacePerformance(
        performance_id="perf_s1",
        identity_id="actor_ana",
        audio_track_id=audio_track_id,
        frames=[{"jaw_open": 0.4}],
    )


# --- validación del contrato de la curva ------------------------------------


def test_valid_curve_passes() -> None:
    _curve().validate()  # no lanza


def test_negative_time_key_rejected() -> None:
    with pytest.raises(FaceError, match="negativo"):
        _curve(times=[-0.5, 1.0, 2.0]).validate()


def test_nonfinite_time_keys_rejected() -> None:
    for bad in (float("nan"), float("inf")):
        with pytest.raises(FaceError, match="finito"):
            _curve(times=[0.0, bad, 2.0]).validate()


def test_unordered_or_duplicate_time_keys_rejected() -> None:
    with pytest.raises(FaceError, match="orden"):
        _curve(times=[2.0, 1.0, 0.0]).validate()
    with pytest.raises(FaceError, match="orden"):
        _curve(times=[0.0, 1.0, 1.0]).validate()


def test_empty_curve_rejected() -> None:
    with pytest.raises(FaceError, match="sin claves"):
        _curve(times=[], weights=[]).validate()


def test_time_weight_length_mismatch_rejected() -> None:
    with pytest.raises(FaceError, match="misma longitud"):
        _curve(times=[0.0, 1.0]).validate()


def test_weight_out_of_range_rejected() -> None:
    with pytest.raises(FaceError, match="fuera de"):
        _curve(weights=[{"jaw_open": 1.5}, {"jaw_open": 0.0}, {"jaw_open": 0.0}]).validate()


def test_nonfinite_weight_rejected() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(FaceError, match="finito"):
            _curve(
                weights=[{"jaw_open": bad}, {"jaw_open": 0.0}, {"jaw_open": 0.0}]
            ).validate()


# --- muestreo determinista ---------------------------------------------------


def test_sample_interpolates_linearly_and_clamps() -> None:
    curve = _curve()
    assert curve.sample(0.0) == {"jaw_open": 0.0, "smile": 0.1}
    assert curve.sample(1.0) == {"jaw_open": 0.8, "smile": 0.2}
    assert math.isclose(curve.sample(0.5)["jaw_open"], 0.4)
    assert math.isclose(curve.sample(0.5)["smile"], 0.15)
    # fuera de rango: clamp a los extremos
    assert curve.sample(-1.0) == {"jaw_open": 0.0, "smile": 0.1}
    assert curve.sample(9.0) == {"jaw_open": 0.2, "smile": 0.0}


def test_sample_unions_targets_across_keys() -> None:
    curve = _curve(
        times=[0.0, 1.0, 2.0],
        weights=[{"a": 0.0, "b": 1.0}, {"a": 1.0, "b": 0.0}, {"c": 1.0}],
    )
    assert curve.sample(0.5) == {"a": 0.5, "b": 0.5, "c": 0.0}
    assert curve.sample(1.5) == {"a": 0.5, "b": 0.0, "c": 0.5}


def test_sample_before_validation_is_safe() -> None:
    curve = _curve()
    curve.times = [3.0, 1.0]  # corrupto: sin validar no se muestrea
    with pytest.raises(FaceError, match="orden"):
        curve.sample(1.0)


# --- ligadura a la pista de audio y causalidad selectiva (A16) ---------------


def test_curve_linked_to_audio_track_matches_performance() -> None:
    assert_curve_matches_track(_curve(), _perf())  # misma pista: no lanza
    with pytest.raises(FaceError, match="pista de audio"):
        assert_curve_matches_track(_curve(audio_track_id="otra_voz"), _perf())


def test_camera_light_edit_preserves_audio_and_lip_motion() -> None:
    """A16: cámara/luz no regeneran audio ni la actuación labial."""
    curve = _curve()
    before = curve.sample(0.5)
    assert edit_performance(_perf(), change="camera") == {"render"}
    assert edit_performance(_perf(), change="light") == {"render"}
    assert curve.sample(0.5) == before  # la curva queda intacta
    assert curve.audio_track_id == _perf().audio_track_id


def test_script_edit_invalidates_lip_motion() -> None:
    out = edit_performance(_perf(), change="script")
    assert "audio" in out and "lipsync" in out


# --- persistencia simbólica --------------------------------------------------


def test_curve_round_trips_through_dict() -> None:
    curve = _curve()
    restored = SpeakerMorphCurve.from_dict(curve.to_dict())
    assert restored == curve
    assert restored.to_dict() == curve.to_dict()


def test_curve_from_dict_rejects_invalid_payload() -> None:
    bad = _curve().to_dict()
    bad["times"] = [1.0, 0.0]
    with pytest.raises(FaceError, match="orden"):
        SpeakerMorphCurve.from_dict(bad)
