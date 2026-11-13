"""Build lock and bounded-resource policy for the llama.cpp CPU binary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts" / "build_llm_server.py"
LOCK_PATH = ROOT / "upgrades-ultra-version" / "locks" / "llm-server-ubuntu24.04-amd64.lock.json"

spec = importlib.util.spec_from_file_location("build_llm_server", BUILD_SCRIPT)
assert spec is not None and spec.loader is not None
build_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_module)


def test_build_parallelism_scales_with_memory_and_cpu() -> None:
    gib = 1024**3
    assert build_module.build_jobs(4 * gib, cpu_count=32) == 1
    assert build_module.build_jobs(12 * gib, cpu_count=32) == 3
    assert build_module.build_jobs(128 * gib, cpu_count=8) == 8


def test_llama_build_lock_is_pinned_cpu_only_and_preserves_notices() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["revision"] == "cd959bae99ff4bed22bef2604f45241c09bbb8c8"
    assert lock["cmake_options"]["GGML_CUDA"] == "OFF"
    assert lock["cmake_options"]["LLAMA_BUILD_TOOLS"] == "ON"
    assert lock["cmake_options"]["LLAMA_BUILD_SERVER"] == "ON"
    assert len(lock["license_files"]) >= 8
    for relative in lock["license_files"]:
        assert not Path(relative).is_absolute()
        assert ".." not in Path(relative).parts


def test_llm_server_manifest_lock_hash_matches_file() -> None:
    manifest = json.loads((ROOT / "upgrades-ultra-version" / "engines.json").read_text())
    engine = next(item for item in manifest["engines"] if item["id"] == "llm-server")
    expected = engine["dependency_lock_hashes"][LOCK_PATH.relative_to(ROOT).as_posix()]
    assert hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest() == expected
    assert engine["redistribution_decision"] == "conditional"
    assert engine["notices_reviewed"] is False
