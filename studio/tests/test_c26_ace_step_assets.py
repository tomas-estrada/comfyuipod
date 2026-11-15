"""Pinned opt-in model asset contract for directed music generation."""

from __future__ import annotations

import json
from pathlib import Path

from comfy_studio import model_assets
from comfy_studio.config import StudioConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_MANIFEST = REPO_ROOT / "config" / "models-studio.json"


def test_ace_step_aio_is_a_pinned_optional_music_asset() -> None:
    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    rows = [
        row
        for row in manifest["models"]
        if row.get("filename") == "ace_step_1.5_turbo_aio.safetensors"
    ]

    assert len(rows) == 1
    entry = rows[0]
    assert entry == {
        "category": "music_opt_in",
        "repo_id": "Comfy-Org/ACE-Step-1.5",
        "remote_path": "checkpoints/ace_step_1.5_turbo_aio.safetensors",
        "url": (
            "https://huggingface.co/Comfy-Org/ACE-Step-1.5/resolve/"
            "371bb1680f8c88d8c1083f6ffb6330d3d55bff76/"
            "checkpoints/ace_step_1.5_turbo_aio.safetensors"
        ),
        "bytes": 10025478736,
        "target_dir": "models/checkpoints",
        "filename": "ace_step_1.5_turbo_aio.safetensors",
        "dest_path": "models/checkpoints/ace_step_1.5_turbo_aio.safetensors",
        "license": "Apache-2.0",
        "class": "optional",
        "opt_in": "STUDIO_INCLUDE_MUSIC",
        "revision": "371bb1680f8c88d8c1083f6ffb6330d3d55bff76",
        "sha256": "67b0f43aa5c51c840bd0228e6a935d8ff416ec87e5df2fc0637da17a561252bc",
    }

    assert manifest["meta"]["categories"]["music_opt_in"] == {
        "entries": 1,
        "bytes": 10025478736,
        "license": "Apache-2.0",
    }
    assert manifest["meta"]["integrity"]["pinned_entries"] == 91
    assert manifest["meta"]["integrity"]["total_entries"] == 91
    assert manifest["meta"]["integrity"]["by_algorithm"]["sha256"] == 62

    assert model_assets.OPT_IN_FLAGS["STUDIO_INCLUDE_MUSIC"] == "music"
    env = {"STUDIO_MANIFEST": str(MODEL_MANIFEST)}
    assert model_assets.entries_for_flag("STUDIO_INCLUDE_MUSIC", env) == [entry]
    animate_entries = model_assets.entries_for_flag("STUDIO_INCLUDE_ANIMATE", env)
    assert animate_entries
    assert all(row["filename"] != entry["filename"] for row in animate_entries)


def test_music_weights_availability_is_an_on_disk_fact(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("STUDIO_INCLUDE_MUSIC", "1")
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path))
    config = StudioConfig.from_env()

    assert config.music_weights_expected() is True
    assert config.music_weights_available() is False
    checkpoint = tmp_path / "models" / "checkpoints" / "ace_step_1.5_turbo_aio.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"fixture")
    assert config.music_weights_available() is True
