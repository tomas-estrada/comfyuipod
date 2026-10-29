"""Tests del motor de vídeo pesado (M4-SCR-002, F5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.heavy_video import HeavyVideoEngine


def _job(tmp_path: Path) -> dict:
    src = tmp_path / "blender_render.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return {
        "input_video": str(src),
        "output_path": str(tmp_path / "leveled.mp4"),
        "prompt": "photoreal car at dusk",
        "provider": "minimax",
        "strength": 0.6,
        "max_seconds": 7.0,
    }


def test_heavy_video_is_installable_tier() -> None:
    assert HeavyVideoEngine().tier == EngineTier.INSTALLABLE


def test_list_reports_providers() -> None:
    res = HeavyVideoEngine().list()
    assert res.ok is True
    assert "minimax" in res.payload["providers"]


def test_stub_run_writes_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_HEAVY_VIDEO", "1")
    eng = HeavyVideoEngine()
    res = asyncio.run(eng.run(EngineRunSpec(job=_job(tmp_path))))
    assert res.ok is True
    assert res.simulated is True
    out = Path(res.payload["output"])
    assert out.exists() and out.read_bytes()[:4] == b"\x00\x00\x00\x18"


def test_verify_reports_credential_without_leaking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_HEAVY_VIDEO", raising=False)
    monkeypatch.delenv("STUDIO_HEAVY_VIDEO_KEY", raising=False)
    res = HeavyVideoEngine().verify("any")
    assert res.ok is False
    assert res.payload["has_credential"] is False
    # el payload nunca contiene el valor de la key, solo el booleano.
    assert "has_credential" in res.payload and "key" not in res.payload
    monkeypatch.setenv("STUDIO_HEAVY_VIDEO_KEY", "super-secret-value")
    ok = HeavyVideoEngine().verify("any")
    assert ok.ok is True
    assert "super-secret-value" not in str(ok.to_public_dict())


def test_bad_provider_rejected(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job["provider"] = "totally-made-up"
    res = asyncio.run(HeavyVideoEngine().run(EngineRunSpec(job=job)))
    assert res.ok is False
    assert "provider" in (res.error or "")


def test_bad_strength_rejected(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job["strength"] = 5.0
    res = asyncio.run(HeavyVideoEngine().run(EngineRunSpec(job=job)))
    assert res.ok is False


def test_missing_key_blocks_real_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_HEAVY_VIDEO", raising=False)
    monkeypatch.delenv("STUDIO_HEAVY_VIDEO_KEY", raising=False)
    res = asyncio.run(HeavyVideoEngine().run(EngineRunSpec(job=_job(tmp_path))))
    assert res.ok is False
    assert "credencial" in (res.error or "")


def test_real_path_typed_failure_and_no_key_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Backend ahora cableado (Minimax). Con una key presente pero el proveedor
    # devolviendo error, debe fallar TIPADO y nunca filtrar la key. Se mockea
    # httpx para no tocar la red.
    monkeypatch.delenv("STUDIO_STUB_HEAVY_VIDEO", raising=False)
    monkeypatch.setenv("STUDIO_HEAVY_VIDEO_KEY", "super-secret-key-value")

    import httpx

    class _Resp:
        status_code = 500
        text = "upstream boom"

        def json(self) -> dict:
            return {}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, *a, **k):
            return _Resp()

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    res = asyncio.run(HeavyVideoEngine().run(EngineRunSpec(job=_job(tmp_path))))
    assert res.ok is False
    assert "500" in (res.error or "")
    # La key nunca aparece en el resultado expuesto.
    assert "super-secret-key-value" not in str(res.to_public_dict())
