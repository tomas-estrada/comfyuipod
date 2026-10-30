"""Tests del motor roto / SAM 3 (M6-SCR, Bloque 2). Stub + contrato + typed-fail."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.roto import RotoEngine


def _job(tmp_path: Path, prompts=("el robot",)) -> dict:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return {
        "video_path": str(src),
        "prompts": list(prompts),
        "output_dir": str(tmp_path / "masks"),
    }


def test_roto_tier_and_name() -> None:
    assert RotoEngine().name == "roto"
    assert RotoEngine().tier == EngineTier.WARM


def test_roto_stub_writes_masks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_ROTO", "1")
    res = RotoEngine().run(EngineRunSpec(job=_job(tmp_path, ("el robot", "los escombros"))))
    assert res.ok and res.simulated
    masks = list((tmp_path / "masks").glob("mask_*.png"))
    assert len(masks) == 2  # una por prompt


def test_roto_requires_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_ROTO", "1")
    job = _job(tmp_path, prompts=[])
    res = RotoEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "prompts" in (res.error or "")


def test_roto_accepts_single_string_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_ROTO", "1")
    job = _job(tmp_path)
    job["prompts"] = "el robot"
    res = RotoEngine().run(EngineRunSpec(job=job))
    assert res.ok is True


def test_roto_no_package_fails_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Sin stub y sin sam3 instalado (CI) -> falla TIPADO, nunca lanza.
    monkeypatch.delenv("STUDIO_STUB_ROTO", raising=False)
    monkeypatch.setattr(RotoEngine, "_package_available", lambda _self: False)
    res = RotoEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "SAM 3" in (res.error or "")


def test_roto_package_but_no_weights_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_ROTO", raising=False)
    monkeypatch.delenv("STUDIO_ROTO_MODEL_DIR", raising=False)
    monkeypatch.setattr(RotoEngine, "_package_available", lambda _self: True)
    res = RotoEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "pesos" in (res.error or "") and "HF-gated" in (res.error or "")


def test_roto_install_refused() -> None:
    res = RotoEngine().install("sam3")
    assert res.ok is False


def test_roto_list_reports_text_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_ROTO", "1")
    res = RotoEngine().list()
    assert res.payload["mode"] == "text-prompt"
