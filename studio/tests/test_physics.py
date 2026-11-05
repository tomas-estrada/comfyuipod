"""Física y articulación proxy (plan Ultra, W12 / A14).

Solver cinemático determinista CPU; joints/limits/timebase; estado ligado por
hash; es proxy (la fidelidad neural no se deduce). Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.physics import (
    Joint,
    PhysicsError,
    SimSpec,
    assert_returns_to_initial,
    simulate,
)


def _spec(**kw) -> SimSpec:
    defaults = {
        "maneuver": "caida_libre",
        "seed": 7,
        "fps": 24,
        "duration_s": 2.0,
        "initial_height_m": 10.0,
        "joints": [Joint(name="brazo", axis="z", min_deg=0.0, max_deg=90.0)],
    }
    defaults.update(kw)
    return SimSpec(**defaults)


def test_frame_count_follows_timebase() -> None:
    assert _spec(fps=24, duration_s=2.0).frame_count() == 48


def test_fall_reaches_contact_with_ground() -> None:
    res = simulate(_spec(initial_height_m=5.0, duration_s=2.0))
    cf = res.contact_frame()
    assert cf is not None, "el objeto debe tocar el suelo en 2s desde 5m"
    # Tras el contacto, altura 0 y velocidad 0.
    last = res.frames[-1]
    assert last.height_m == 0.0 and last.velocity_mps == 0.0


def test_joint_angles_stay_within_limits() -> None:
    res = simulate(_spec(joints=[Joint(name="j", axis="x", min_deg=10.0, max_deg=80.0)]))
    for f in res.frames:
        assert 10.0 <= f.joint_angles_deg["j"] <= 80.0


def test_simulation_is_deterministic_by_seed_and_params() -> None:
    a = simulate(_spec(seed=42))
    b = simulate(_spec(seed=42))
    assert a.spec_hash == b.spec_hash
    assert a.to_dict() == b.to_dict()  # mismo seed/params -> mismo resultado


def test_different_params_change_the_state_hash() -> None:
    a = simulate(_spec(initial_height_m=10.0))
    b = simulate(_spec(initial_height_m=20.0))
    assert a.spec_hash != b.spec_hash  # estado ligado a params (A14)


def test_result_is_marked_as_proxy_not_neural_fidelity() -> None:
    """A14: la fidelidad tras traducción neural NO se deduce del proxy."""
    res = simulate(_spec())
    d = res.to_dict()
    assert d["is_proxy"] is True
    assert "NO se deduce" in d["proxy_note"]


def test_invalid_joint_limits_rejected() -> None:
    with pytest.raises(PhysicsError, match="min_deg > max_deg"):
        simulate(_spec(joints=[Joint(name="bad", axis="x", min_deg=90.0, max_deg=10.0)]))


def test_zero_fps_rejected() -> None:
    with pytest.raises(PhysicsError):
        simulate(_spec(fps=0))


def test_articulated_product_returns_to_initial_state() -> None:
    """A14: joints/vuelta al estado inicial comprobados (reproducible)."""
    a = simulate(_spec(seed=1))
    b = simulate(_spec(seed=1))
    assert_returns_to_initial(a, b)  # no lanza: mismo estado inicial


def test_incomparable_specs_rejected() -> None:
    a = simulate(_spec(seed=1))
    b = simulate(_spec(seed=2))
    with pytest.raises(PhysicsError, match="no comparables"):
        assert_returns_to_initial(a, b)
