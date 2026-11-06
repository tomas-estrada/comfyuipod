"""Tests del motor scene-optimizer (plan Ultra, W11 / A13 / C20).

CPU-only, determinista.
"""

from __future__ import annotations

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.scene_optimizer import SceneOptimizerEngine


def _engine() -> SceneOptimizerEngine:
    return SceneOptimizerEngine()


def _spec(**job) -> EngineRunSpec:
    return EngineRunSpec(job=job)


def _plan_kwargs(kf_pos=(5.0, 5.0, 5.0), focal=35.0) -> dict:
    return {
        "shot_id": "shot_1",
        "keyframes": [
            {"t": 0.0, "position": list(kf_pos), "target": [0.0, 0.0, 0.0]},
        ],
        "focal_mm": float(focal),
        "sensor_mm": 36.0,
        "duration_s": 5.0,
    }


# -- verify ------------------------------------------------------------------

def test_verify_valid_plan() -> None:
    plan = _plan_kwargs(kf_pos=(10.0, 10.0, 10.0))
    res = _engine().verify(plan)
    assert res.ok
    assert res.verb == "verify"
    assert "plan" in res.payload


def test_verify_bad_focal_fails() -> None:
    """Un plan con focal <= 0 es inválido."""
    plan = _plan_kwargs(focal=0.0)
    res = _engine().verify(plan)
    assert not res.ok
    assert "focal" in (res.error or "").lower() or "plan" in (res.error or "").lower()


# -- run ---------------------------------------------------------------------

def test_check_plan_valid_returns_ok() -> None:
    """Plan válido, cámara fuera de la zona del sujeto → ok=True."""
    job = {
        "op": "check_plan",
        "plan": _plan_kwargs(kf_pos=(10.0, 10.0, 10.0)),
        "subject_position": [0.0, 0.0, 0.0],
        "subject_radius": 0.5,
    }
    res = _engine().run(_spec(**job))
    assert res.ok, f"run falló: {res.error}"
    assert res.simulated is False
    report = res.payload["report"]
    assert report["ok"] is True
    assert report["collisions"] == []
    assert report["occlusions"] == []


def test_check_plan_inside_subject_radius_collision() -> None:
    """Keyframe dentro del radio del sujeto → colisión reportada."""
    job = {
        "op": "check_plan",
        "plan": _plan_kwargs(kf_pos=(0.3, 0.0, 0.0)),
        "subject_position": [0.0, 0.0, 0.0],
        "subject_radius": 0.5,
    }
    res = _engine().run(_spec(**job))
    assert not res.ok
    assert res.simulated is False
    report = res.payload["report"]
    assert len(report["collisions"]) > 0


def test_check_plan_bad_focal_typed_failure() -> None:
    """Plan con focal inválida → ok=False tipificado, no raise."""
    job = {
        "op": "check_plan",
        "plan": _plan_kwargs(focal=-1.0),
        "subject_position": [0.0, 0.0, 0.0],
        "subject_radius": 0.5,
    }
    res = _engine().run(_spec(**job))
    assert not res.ok


def test_run_unknown_op_fails() -> None:
    res = _engine().run(_spec(op="no_existe"))
    assert not res.ok
    assert "desconocida" in (res.error or "")
