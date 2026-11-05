from __future__ import annotations

import hashlib
import json
from pathlib import Path

from comfy_studio import asset_acquire_cli
from comfy_studio.asset_acquire import AcquisitionReport, AcquiredEntry


def _write_manifest(path: Path, *, revision: str = "a" * 40, digest: str | None = None) -> None:
    payload = {
        "models": [
            {
                "repo_id": "org/model",
                "remote_path": "weights.bin",
                "repo_type": "model",
                "dest_path": "models/checkpoints/weights.bin",
                "filename": "weights.bin",
                "bytes": 4,
                "revision": revision,
                "sha256": digest or hashlib.sha256(b"data").hexdigest(),
            }
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_find_need_uses_manifest_revision_digest_and_exact_destination(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(manifest))
    monkeypatch.setenv("STUDIO_MANIFEST", "")

    need = asset_acquire_cli._find_need(
        "org/model", "weights.bin", data / "models/checkpoints/weights.bin", "model"
    )
    assert need.revision == "a" * 40
    assert need.sha256 == hashlib.sha256(b"data").hexdigest()
    assert need.bytes == 4


def test_find_need_rejects_unpinned_or_wrong_destination(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, revision="")
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(manifest))
    monkeypatch.setenv("STUDIO_MANIFEST", "")

    try:
        asset_acquire_cli._find_need(
            "org/model", "weights.bin", data / "models/checkpoints/weights.bin", "model"
        )
    except ValueError as exc:
        assert "pinned revision" in str(exc)
    else:
        raise AssertionError("an unpinned manifest entry must fail closed")

    try:
        asset_acquire_cli._find_need(
            "org/model", "weights.bin", data / "models/other/weights.bin", "model"
        )
    except ValueError as exc:
        assert "not declared" in str(exc)
    else:
        raise AssertionError("a different destination must not match")


def test_cli_calls_shared_acquirer_with_manifest_need(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    dest = data / "models/checkpoints/weights.bin"
    _write_manifest(manifest)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(manifest))
    monkeypatch.setenv("STUDIO_MANIFEST", "")
    seen = {}

    def fake_acquire(needs, **kwargs):
        seen["need"] = needs[0]
        seen["kwargs"] = kwargs
        return AcquisitionReport(entries=[AcquiredEntry("weights.bin", str(dest), "downloaded", 4)])

    monkeypatch.setattr(asset_acquire_cli, "acquire", fake_acquire)
    result = asset_acquire_cli.main(
        ["--repo", "org/model", "--remote", "weights.bin", "--dest", str(dest), "--stage", str(tmp_path / "stage")]
    )
    assert result == 0
    assert seen["need"].revision == "a" * 40
    assert seen["need"].sha256 == hashlib.sha256(b"data").hexdigest()
    assert seen["kwargs"]["max_attempts"] == 1
