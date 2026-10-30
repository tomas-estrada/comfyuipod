"""Tests del motor de upscaler (M5-SCR, fase 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.upscaler import UpscalerEngine


def _job(tmp_path: Path, scale: int = 4) -> dict:
    src = tmp_path / "in.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    return {"input_path": str(src), "output_path": str(tmp_path / "out.png"), "scale": scale}


def test_upscaler_is_warm_tier() -> None:
    assert UpscalerEngine().tier == EngineTier.WARM


def test_list_reports_scales() -> None:
    res = UpscalerEngine().list()
    assert res.ok is True
    assert set(res.payload["scales"]) == {2, 3, 4}


def test_stub_run_writes_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_UPSCALER", "1")
    res = UpscalerEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert res.payload["scale"] == 4


def test_bad_scale_rejected(tmp_path: Path) -> None:
    res = UpscalerEngine().run(EngineRunSpec(job=_job(tmp_path, scale=7)))
    assert res.ok is False
    assert "scale" in (res.error or "")


def test_no_model_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_UPSCALER", raising=False)
    monkeypatch.delenv("STUDIO_UPSCALER_MODEL_DIR", raising=False)
    res = UpscalerEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "modelo de upscaler" in (res.error or "")


def test_verify_reports_model_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_UPSCALER", raising=False)
    monkeypatch.delenv("STUDIO_UPSCALER_MODEL_DIR", raising=False)
    res = UpscalerEngine().verify("any")
    assert res.ok is False
    assert res.payload["model_available"] is False


def _video_job(tmp_path: Path) -> dict:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return {
        "input_path": str(src),
        "output_path": str(tmp_path / "out.mp4"),
        "scale": 2,
        "mode": "video",
    }


def test_list_reports_modes_and_video_submotor() -> None:
    res = UpscalerEngine().list()
    assert set(res.payload["modes"]) == {"image", "video"}
    assert "SeedVR2" in res.payload["video_submotor"]


def test_video_stub_writes_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_UPSCALER", "1")
    res = UpscalerEngine().run(EngineRunSpec(job=_video_job(tmp_path)))
    assert res.ok is True and res.simulated is True
    assert res.payload["mode"] == "video"
    assert b"ftyp" in Path(res.payload["output"]).read_bytes()


def test_video_mode_typed_fail_without_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fuera de stub, el modo vídeo (SeedVR2) falla TIPADO con remediación, no finge.
    monkeypatch.delenv("STUDIO_STUB_UPSCALER", raising=False)
    res = UpscalerEngine().run(EngineRunSpec(job=_video_job(tmp_path)))
    assert res.ok is False
    assert "SeedVR2" in (res.error or "")


def test_bad_mode_rejected(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job["mode"] = "holograma"
    res = UpscalerEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "mode" in (res.error or "")
