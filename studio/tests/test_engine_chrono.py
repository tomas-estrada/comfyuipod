"""Tests del motor chrono (plan Ultra, W12 / A14 / C18).

CPU-only, determinista. Verifica honestidad de proxy y reproducibilidad.
"""

from __future__ import annotations

import os

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.chrono import ChronoEngine


def _engine() -> ChronoEngine:
    return ChronoEngine()


def _spec(**job) -> EngineRunSpec:
    return EngineRunSpec(job=job)


def _free_fall_job(**kw) -> dict:
    base = {
        "op": "simulate",
        "spec": {
            "maneuver": "free_fall",
            "seed": 42,
            "fps": 24,
            "duration_s": 2.0,
            "initial_height_m": 10.0,
            "joints": [],
        },
    }
    base["spec"].update(kw)
    return base


def test_simulate_free_fall_ok() -> None:
    job = _free_fall_job()
    res = _engine().run(_spec(**job))
    assert res.ok, f"run falló: {res.error}"
    assert res.simulated is False
    assert res.payload["is_proxy"] is True
    assert "NO se deduce" in res.payload["proxy_note"]
    assert res.payload["frame_count"] > 0


def test_reproducibility_same_spec_same_hash() -> None:
    """Dos corridas con la misma spec producen el mismo state_hash."""
    job = _free_fall_job(seed=7)
    res1 = _engine().run(_spec(**job))
    res2 = _engine().run(_spec(**job))
    assert res1.ok and res2.ok
    assert res1.payload["spec_hash"] == res2.payload["spec_hash"]


def test_simulated_flag_false_on_real_run() -> None:
    """El run real ejecuta simulate(): trae frames medidos, no un flag por defecto.

    El test anterior solo afirmaba ``res.simulated is False``, que es el valor
    por defecto de ``EngineResult`` incluso cuando ``simulate`` no hace nada.
    Aquí se exige evidencia de ejecución real.
    """
    res = _engine().run(_spec(**_free_fall_job()))
    assert res.ok, f"run falló: {res.error}"
    assert res.simulated is False
    assert res.payload["simulated"] is False
    assert res.payload["is_proxy"] is True
    frames = res.payload["frames"]
    assert len(frames) == res.payload["frame_count"] > 0
    # Caída libre desde 10 m: el último frame toca el suelo (altura 0).
    assert frames[-1]["height_m"] == 0.0
    assert frames[-1]["in_contact"] is True
    # El hash liga la simulación real; si simulate no corriera, no habría hash.
    assert len(res.payload["spec_hash"]) == 16


def test_stub_mode_produces_simulated() -> None:
    os.environ["STUDIO_STUB_CHRONO"] = "1"
    try:
        job = _free_fall_job()
        res = _engine().run(_spec(**job))
        assert res.ok
        assert res.simulated is True
    finally:
        del os.environ["STUDIO_STUB_CHRONO"]


def test_run_unknown_op_fails() -> None:
    res = _engine().run(_spec(op="no_existe"))
    assert not res.ok
    assert "desconocida" in (res.error or "")


def test_verify_valid_spec() -> None:
    spec_dict = {
        "maneuver": "free_fall",
        "seed": 0,
        "fps": 24,
        "duration_s": 1.0,
        "initial_height_m": 5.0,
        "joints": [],
    }
    res = _engine().verify(spec_dict)
    assert res.ok
    assert res.payload["is_proxy"] is True
    assert res.payload["proxy_note"]
    assert "frame_count" in res.payload
