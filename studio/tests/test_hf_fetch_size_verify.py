"""hf_fetch size verification: shell expected_bytes must match the manifest.

Regression for the legacy ``test -s`` skip in ``scripts/download-models.sh``:
a non-empty but truncated file used to count as present and ``hf download``
ran without ``--revision``. ``hf_fetch`` now delegates to
``comfy_studio.asset_acquire_cli`` (manifest revision + digest), and forwards
its ``expected_bytes`` argument as ``--expected-bytes`` so a caller/manifest
drift fails closed instead of downloading the wrong asset.

The truncated-destination test pins the shared criterion: a short file is
never reported as ``skipped``; it is re-downloaded and verified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from comfy_studio import asset_acquire, asset_acquire_cli


def _write_manifest(path: Path, *, content: bytes) -> Path:
    payload = {
        "models": [
            {
                "repo_id": "org/model",
                "remote_path": "weights.bin",
                "repo_type": "model",
                "dest_path": "models/checkpoints/weights.bin",
                "filename": "weights.bin",
                "bytes": len(content),
                "revision": "b" * 40,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _env_for(data: Path, manifest: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["COMFY_DATA_DIR"] = str(data)
    env["STUDIO_CORE_MANIFEST"] = str(manifest)
    env["STUDIO_MANIFEST"] = ""
    env["HF_CLI"] = "hf"
    env["HF_DISK_GUARD"] = "0"
    return env


def test_cli_rejects_expected_bytes_mismatch(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, content=b"data")
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(manifest))
    monkeypatch.setenv("STUDIO_MANIFEST", "")

    called = []

    def fake_acquire(needs, **kwargs):  # must not run on mismatch
        called.append(needs)
        raise AssertionError("acquire must not run when sizes disagree")

    monkeypatch.setattr(asset_acquire_cli, "acquire", fake_acquire)
    dest = data / "models/checkpoints/weights.bin"
    result = asset_acquire_cli.main(
        [
            "--repo",
            "org/model",
            "--remote",
            "weights.bin",
            "--dest",
            str(dest),
            "--stage",
            str(tmp_path / "stage"),
            "--expected-bytes",
            "2",
        ]
    )
    assert result == 2
    assert called == []


def test_cli_accepts_matching_expected_bytes(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, content=b"data")
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(manifest))
    monkeypatch.setenv("STUDIO_MANIFEST", "")
    seen = {}

    def fake_acquire(needs, **kwargs):
        from comfy_studio.asset_acquire import AcquiredEntry, AcquisitionReport

        seen["bytes"] = needs[0].bytes
        dest = data / "models/checkpoints/weights.bin"
        return AcquisitionReport(entries=[AcquiredEntry("weights.bin", str(dest), "downloaded", 4)])

    monkeypatch.setattr(asset_acquire_cli, "acquire", fake_acquire)
    dest = data / "models/checkpoints/weights.bin"
    result = asset_acquire_cli.main(
        [
            "--repo",
            "org/model",
            "--remote",
            "weights.bin",
            "--dest",
            str(dest),
            "--stage",
            str(tmp_path / "stage"),
            "--expected-bytes",
            "4",
        ]
    )
    assert result == 0
    assert seen["bytes"] == 4


def test_truncated_dest_is_redownloaded_not_skipped(tmp_path: Path, monkeypatch) -> None:
    """A short non-empty file must not count as present (old ``test -s`` bug)."""
    full = b"0123456789abcdef"
    assert len(full) == 16
    data = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, content=full)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(manifest))
    monkeypatch.setenv("STUDIO_MANIFEST", "")
    monkeypatch.setenv("HF_CLI", "hf")
    monkeypatch.setenv("HF_DISK_GUARD", "0")
    dest = data / "models/checkpoints/weights.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(full[:5])  # truncated but non-empty: old skip would stop here

    def fake_download(argv, *, timeout, should_cancel):
        # Emulate `hf download`: materialise the full verified content.
        local_dir = Path(argv[argv.index("--local-dir") + 1])
        staged = local_dir / "weights.bin"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(full)
        return 0

    monkeypatch.setattr(asset_acquire, "_run_hf_download", fake_download)
    monkeypatch.setattr(asset_acquire, "resolve_hf_cli", lambda env=None: "hf")

    from comfy_studio import model_assets

    dest_path = model_assets.resolve_dest(
        "models/checkpoints/weights.bin", {"COMFY_DATA_DIR": str(data)}
    )
    need = asset_acquire_cli._find_need("org/model", "weights.bin", dest_path, "model")
    report = asset_acquire.acquire([need], env=_env_for(data, manifest))
    entry = report.entries[0]
    assert entry.status == "downloaded", entry
    assert entry.bytes == len(full)
    assert dest.read_bytes() == full


def test_shell_forwards_expected_bytes() -> None:
    """The shell wrapper must pass its size argument through to the CLI."""
    text = Path("scripts/download-models.sh").read_text(encoding="utf-8")
    assert '--expected-bytes "${expected_bytes}"' in text
    assert 'test -s "${dest_file}"' not in text
    assert 'test -s "${STAGE_DIR}' not in text
    # No /bin/bash in this test: pure-Python structural check that hf_fetch
    # still delegates to the manifest-backed CLI (revision + digest enforced
    # there) instead of raw `hf download`.
    assert "asset_acquire_cli" in text
    assert "--repo-type" in text
