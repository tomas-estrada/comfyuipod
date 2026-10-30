"""Tests del motor qa (M6-SCR, Bloque 2). Stub + parsers FFmpeg + contrato."""

from __future__ import annotations

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.qa import QaEngine, _count_freeze, _sum_black


def test_qa_tier_and_name() -> None:
    eng = QaEngine()
    assert eng.name == "qa"
    assert eng.tier == EngineTier.WARM


def test_qa_stub_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_QA", "1")
    eng = QaEngine()
    res = eng.run(EngineRunSpec(job={"video_path": "/x/shot.mp4", "acceptance": {}}))
    assert res.ok and res.simulated
    assert res.payload["passed"] is True
    assert res.payload["vbench"] is None


def test_qa_invalid_spec_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_QA", "1")
    res = QaEngine().run(EngineRunSpec(job={}))
    assert res.ok is False
    assert "spec inválido" in (res.error or "")


def test_qa_no_ffmpeg_fails_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sin stub y sin ffmpeg (env vacío + which None) -> falla TIPADO, nunca lanza.
    monkeypatch.delenv("STUDIO_STUB_QA", raising=False)
    monkeypatch.delenv("STUDIO_FFMPEG_BIN", raising=False)
    monkeypatch.setattr("comfy_studio.engines.qa.shutil.which", lambda _b: None)
    res = QaEngine().run(EngineRunSpec(job={"video_path": "/x/shot.mp4"}))
    assert res.ok is False
    assert "FFmpeg no disponible" in (res.error or "")


def test_qa_verify_reports_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_QA", "1")
    res = QaEngine().verify("")
    assert res.ok
    assert "ffmpeg" in res.payload and "vbench" in res.payload


def test_qa_list_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_QA", "1")
    res = QaEngine().list()
    assert res.ok
    assert "flicker" in res.payload["dimensions_level1"]


def test_qa_install_refused() -> None:
    res = QaEngine().install("vbench")
    assert res.ok is False


def test_blackdetect_parser() -> None:
    stderr = (
        "[blackdetect @ 0x1] black_start:0 black_end:0.5 black_duration:0.5\n"
        "[blackdetect @ 0x1] black_start:2 black_end:2.2 black_duration:0.2\n"
    )
    assert _sum_black(stderr) == 0.7


def test_freezedetect_parser() -> None:
    stderr = (
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 1.0\n"
        "[freezedetect @ 0x1] freeze_duration:0.5\n"
    )
    # Solo el token que empieza por freeze_duration: cuenta; el primero lleva prefijo lavfi.
    assert _count_freeze(stderr) == 0.5
