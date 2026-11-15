"""Build lock and bounded-resource policy for the llama.cpp CPU binary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

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
    assert lock["cmake_options"]["BUILD_SHARED_LIBS"] == "OFF"
    assert lock["cmake_options"]["GGML_CUDA"] == "OFF"
    assert lock["cmake_options"]["LLAMA_BUILD_TOOLS"] == "ON"
    assert lock["cmake_options"]["LLAMA_BUILD_SERVER"] == "ON"
    assert len(lock["license_files"]) >= 8
    for relative in lock["license_files"]:
        assert not Path(relative).is_absolute()
        assert ".." not in Path(relative).parts
    supplemental = lock["supplemental_license"]
    license_path = ROOT / supplemental["source"]
    assert license_path.is_file()
    assert hashlib.sha256(license_path.read_bytes()).hexdigest() == supplemental["sha256"]
    assert supplemental["destination"] == "Third-Party-LICENSES.md"
    notice_text = license_path.read_text(encoding="utf-8")
    assert "ALTERNATIVE 2 - MIT No Attribution" in notice_text
    assert "ALTERNATIVE A - MIT License" in notice_text
    assert "ALTERNATIVE B - Public Domain" in notice_text
    assert "Unicode License V3" in notice_text
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "COPY docs/licenses/llama-cpp-notices.md /tmp/llama-cpp-notices.md" in dockerfile
    assert "--supplemental-license /tmp/llama-cpp-notices.md" in dockerfile
    assert "test -s /usr/share/licenses/llama.cpp/Third-Party-LICENSES.md" in dockerfile
    assert "test -s /usr/share/licenses/llama.cpp/system-libraries/manifest.json" in dockerfile
    assert "!docs/licenses/llama-cpp-notices.md" in dockerignore


def test_supplemental_notice_is_hash_checked_and_installed(tmp_path: Path) -> None:
    source = tmp_path / "unicode.md"
    source.write_text("Unicode notice\n", encoding="utf-8")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "licenses" / "Third-Party-LICENSES.md"

    build_module.install_supplemental_notice(source, expected, destination)
    assert destination.read_text(encoding="utf-8") == "Unicode notice\n"
    with pytest.raises(ValueError, match="SHA-256"):
        build_module.install_supplemental_notice(source, "0" * 64, destination)
    assert destination.read_text(encoding="utf-8") == "Unicode notice\n"


def test_system_library_notices_follow_the_final_elf_dependency_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "lib" / "libexample.so.1"
    library.parent.mkdir()
    library.write_bytes(b"ELF fixture")
    copyright_file = tmp_path / "usr" / "share" / "doc" / "libexample1" / "copyright"
    copyright_file.parent.mkdir(parents=True)
    copyright_file.write_text("Example system library license\n", encoding="utf-8")

    def fake_check_output(command: list[str], *, text: bool) -> str:
        if command[0] == "ldd":
            return f"libexample.so.1 => {library} (0x0001)\nlinux-vdso.so.1 (0x0000)\n"
        if command[0:2] == ["dpkg-query", "-S"]:
            return f"libexample1:amd64: {library}\n"
        if command[0:2] == ["dpkg-query", "-L"]:
            return f"{copyright_file}\n"
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(build_module.subprocess, "check_output", fake_check_output)
    license_root = tmp_path / "licenses" / "llama.cpp"
    records = build_module.install_system_library_notices(library, license_root)

    assert records == [{
        "library": str(library.resolve()),
        "package": "libexample1:amd64",
        "notice": "system-libraries/libexample1_amd64.copyright",
        "sha256": hashlib.sha256(copyright_file.read_bytes()).hexdigest(),
    }]
    notice = license_root / records[0]["notice"]
    assert notice.read_text(encoding="utf-8") == "Example system library license\n"
    inventory = json.loads((license_root / "system-libraries" / "manifest.json").read_text())
    assert inventory["libraries"] == records


def test_parse_ldd_rejects_missing_or_empty_dependency_lists() -> None:
    with pytest.raises(RuntimeError, match="ausente"):
        build_module.parse_ldd_dependencies("libbad.so => not found\n")
    with pytest.raises(RuntimeError, match="no reportó"):
        build_module.parse_ldd_dependencies("linux-vdso.so.1 (0x0000)\n")


def test_build_rejects_missing_supplemental_notice_before_fetching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("source fetch started before supplemental notice validation")

    monkeypatch.setattr(build_module, "_run", unexpected_fetch)
    with pytest.raises(ValueError, match="supplemental-license"):
        build_module.build(LOCK_PATH, tmp_path / "bin")


@pytest.mark.parametrize("destination", ["../escape", "/tmp/escape", "LICENSE"])
def test_build_rejects_unsafe_supplemental_destination_before_fetching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    lock["supplemental_license"]["destination"] = destination
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    def unexpected_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("source fetch started before destination validation")

    monkeypatch.setattr(build_module, "_run", unexpected_fetch)
    with pytest.raises(ValueError, match="destination"):
        build_module.build(
            lock_path,
            tmp_path / "bin",
            supplemental_license=ROOT / lock["supplemental_license"]["source"],
        )


def test_llm_server_manifest_lock_hash_matches_file() -> None:
    manifest = json.loads((ROOT / "upgrades-ultra-version" / "engines.json").read_text())
    engine = next(item for item in manifest["engines"] if item["id"] == "llm-server")
    expected = engine["dependency_lock_hashes"][LOCK_PATH.relative_to(ROOT).as_posix()]
    assert hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest() == expected
    assert engine["redistribution_decision"] == "conditional"
    assert engine["notices_reviewed"] is True
