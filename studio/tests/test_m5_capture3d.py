"""Tests del motor de captura 3D / Nerfstudio (M5-SCR, fase 9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.capture3d import Capture3dEngine


def test_capture3d_is_installable_tier() -> None:
    assert Capture3dEngine().tier == EngineTier.INSTALLABLE


def test_list_reports_ops_and_formats() -> None:
    res = Capture3dEngine().list()
    assert res.ok is True
    assert set(res.payload["ops"]) == {"train", "export"}
    assert set(res.payload["export_formats"]) == {"ply", "glb"}


def test_stub_train_writes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_CAPTURE3D", "1")
    src = tmp_path / "photos"
    src.mkdir()
    job = {"op": "train", "input_path": str(src), "output_path": str(tmp_path / "model")}
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).exists()


def test_stub_export_writes_ply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_CAPTURE3D", "1")
    model = tmp_path / "config.yml"
    model.write_text("x", encoding="utf-8")
    job = {
        "op": "export",
        "model_path": str(model),
        "export_format": "ply",
        "output_path": str(tmp_path / "out.ply"),
    }
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is True
    assert Path(res.payload["output"]).read_bytes().startswith(b"ply")


def test_train_missing_input_rejected(tmp_path: Path) -> None:
    job = {"op": "train", "output_path": str(tmp_path / "m")}
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "input_path" in (res.error or "")


def test_bad_export_format_rejected(tmp_path: Path) -> None:
    model = tmp_path / "config.yml"
    model.write_text("x", encoding="utf-8")
    job = {
        "op": "export",
        "model_path": str(model),
        "export_format": "obj",
        "output_path": str(tmp_path / "o.obj"),
    }
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is False


def test_no_binary_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_CAPTURE3D", raising=False)
    monkeypatch.setenv("STUDIO_CAPTURE3D_BIN_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("PATH", "")
    src = tmp_path / "photos"
    src.mkdir()
    job = {"op": "train", "input_path": str(src), "output_path": str(tmp_path / "model")}
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "Nerfstudio no disponible" in (res.error or "")
