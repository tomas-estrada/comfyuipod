"""Validador del manifiesto de engines (plan Ultra, W07 / acceptance A07).

Release gate de coherencia sobre upgrades-ultra-version/engines.json: separa
incoherencias reales (fatales) de bloqueos de release esperados (locks sin fijar).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_engines_manifest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_engines_manifest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_manifest_is_coherent() -> None:
    """El manifiesto real no tiene incoherencias (los locks pendientes NO lo son)."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    cap_ids = mod._capability_ids(mod.DEFAULT_CAPS)
    problems, blockers = mod.validate(manifest, cap_ids)
    assert problems == [], f"incoherencias inesperadas: {problems}"
    # Hay bloqueos de release esperados (locks de código por fijar antes del build).
    assert len(blockers) > 0


def test_source_lock_hashes_are_rederived_from_the_pinned_revision() -> None:
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    engine = next(item for item in manifest["engines"] if item["id"] == "mission-compiler")
    blockers = mod._source_lock_blockers(
        "mission-compiler",
        {
            "source_revision": engine["code_revision"],
            "pinned_source_sha256": {
                "studio/comfy_studio/mission_compiler.py": "0" * 64,
            },
        },
        repo_root=REPO_ROOT,
    )

    assert len(blockers) == 1
    assert "no coincide con source_revision" in blockers[0]


def test_source_lock_verifies_internal_sources_at_the_pinned_revision() -> None:
    """internal_source_sha256 is verified, not ignored like before this fix."""
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "production-memory",
        {
            "source_revision": "8de272798924116b383e6802868dc0dfeab5b4ff",
            "internal_source_sha256": {
                "studio/comfy_studio/production_memory.py": "0" * 64,
            },
        },
        repo_root=REPO_ROOT,
    )

    assert len(blockers) == 1
    assert "production_memory.py" in blockers[0]
    assert "no coincide con source_revision" in blockers[0]


def test_source_lock_rejects_source_missing_from_pinned_revision() -> None:
    """git show failure blocks: a worktree file absent from the revision must not pass."""
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "mission-compiler",
        {
            "source_revision": "8de272798924116b383e6802868dc0dfeab5b4ff",
            "pinned_source_sha256": {
                "studio/comfy_studio/mission_compiler.py": "c" * 64,
            },
        },
        repo_root=REPO_ROOT,
    )

    assert len(blockers) == 1
    assert "mission_compiler.py" in blockers[0]
    assert "no existe en source_revision 8de272798924116b383e6802868dc0dfeab5b4ff" in blockers[0]


def test_source_lock_internal_sources_validated_paths_and_hashes() -> None:
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "production-memory",
        {
            "source_revision": "8de272798924116b383e6802868dc0dfeab5b4ff",
            "internal_source_sha256": {
                "/etc/passwd": "a" * 64,
                "../escape.py": "b" * 64,
                "studio/comfy_studio/config.py": "not-a-hash",
            },
        },
        repo_root=REPO_ROOT,
    )

    assert len(blockers) == 3
    assert any("ruta inválida /etc/passwd" in b for b in blockers)
    assert any("ruta inválida ../escape.py" in b for b in blockers)
    assert any("SHA-256 inválido para studio/comfy_studio/config.py" in b for b in blockers)


def test_source_lock_requires_immutable_revision_for_internal_sources() -> None:
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "production-memory",
        {
            "source_revision": "v1.2.3",
            "internal_source_sha256": {
                "studio/comfy_studio/config.py": "a" * 64,
            },
        },
        repo_root=REPO_ROOT,
    )

    assert any("requiere source_revision SHA inmutable" in b for b in blockers)


def test_source_lock_requires_revision_when_pinned_sources_present() -> None:
    """Absent source_revision with pinned sources is a blocker, not a skip."""
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "production-memory",
        {
            "internal_source_sha256": {
                "studio/comfy_studio/config.py": "a" * 64,
            },
        },
        repo_root=REPO_ROOT,
    )

    assert len(blockers) == 1
    assert "requiere source_revision SHA inmutable" in blockers[0]


def test_source_lock_internal_sources_must_be_a_mapping() -> None:
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "production-memory",
        {
            "source_revision": "8de272798924116b383e6802868dc0dfeab5b4ff",
            "internal_source_sha256": ["studio/comfy_studio/config.py"],
        },
        repo_root=REPO_ROOT,
    )

    assert any("internal_source_sha256 debe ser un objeto" in b for b in blockers)


def test_source_lock_pinned_sources_must_be_a_mapping() -> None:
    mod = _load_module()
    blockers = mod._source_lock_blockers(
        "mission-compiler",
        {
            "source_revision": "8de272798924116b383e6802868dc0dfeab5b4ff",
            "pinned_source_sha256": "studio/comfy_studio/mission_compiler.py",
        },
        repo_root=REPO_ROOT,
    )

    assert any("pinned_source_sha256 debe ser un objeto" in b for b in blockers)


def test_production_memory_internal_sources_verified_through_the_validator() -> None:
    """The real lock passes strict verification, and any tampering is caught."""
    mod = _load_module()
    lock = mod.load_json(
        mod.REPO_ROOT / "upgrades-ultra-version" / "locks" / "production-memory.lock.json"
    )

    assert mod._source_lock_blockers("production-memory", lock, repo_root=REPO_ROOT) == []

    tampered = dict(lock)
    tampered["internal_source_sha256"] = dict(lock["internal_source_sha256"])
    tampered["internal_source_sha256"]["studio/pyproject.toml"] = "f" * 64
    blockers = mod._source_lock_blockers("production-memory", tampered, repo_root=REPO_ROOT)

    assert len(blockers) == 1
    assert "studio/pyproject.toml" in blockers[0]
    assert "no coincide con source_revision" in blockers[0]


def test_scene_optimizer_has_complete_a07_manifest_evidence() -> None:
    """The integrated CPU scene optimizer must satisfy its declared A07 evidence."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    cap_ids = mod._capability_ids(mod.DEFAULT_CAPS)
    scene_optimizer = next(
        engine for engine in manifest["engines"] if engine["id"] == "scene-optimizer"
    )

    problems, blockers = mod.validate({"engines": [scene_optimizer]}, cap_ids)

    assert problems == []
    assert blockers == []


def test_ffmpeg_has_complete_a07_manifest_evidence() -> None:
    """The installed Ubuntu FFmpeg package has auditable A07 evidence."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    cap_ids = mod._capability_ids(mod.DEFAULT_CAPS)
    ffmpeg = next(engine for engine in manifest["engines"] if engine["id"] == "ffmpeg")
    lock_name, lock_hash = next(iter(ffmpeg["dependency_lock_hashes"].items()))
    lock_path = mod.REPO_ROOT / lock_name
    lock = mod.load_json(lock_path)
    dockerfile = (mod.REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    problems, blockers = mod.validate({"engines": [ffmpeg]}, cap_ids)

    assert problems == []
    assert blockers == []
    assert hashlib.sha256(lock_path.read_bytes()).hexdigest() == lock_hash
    assert lock["engine_id"] == "ffmpeg"
    assert lock["package"]["version"] == "7:6.1.1-3ubuntu5"
    assert lock["package"]["url"] in dockerfile
    assert lock["package"]["sha256"] in dockerfile
    assert "/tmp/ffmpeg.deb \\" in dockerfile
    assert "dpkg-query -W ffmpeg" in dockerfile
    assert lock["copyright"]["sha256"] in dockerfile


def test_reference_local_has_complete_a07_manifest_evidence() -> None:
    """First-party adapter: pinned code SHA + the already-locked FFmpeg component.

    reference-local es código propio (commit 1a11cec5) y su única dependencia
    binaria es ffmpeg/ffprobe del paquete Ubuntu ya fijado y verificado por
    hash, así que ya no debe disparar los gates pin/lock/terms de A07.
    """
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    cap_ids = mod._capability_ids(mod.DEFAULT_CAPS)
    engine = next(item for item in manifest["engines"] if item["id"] == "reference-local")
    lock_name, lock_hash = next(iter(engine["dependency_lock_hashes"].items()))
    lock_path = mod.REPO_ROOT / lock_name

    problems, blockers = mod.validate({"engines": [engine]}, cap_ids)

    assert problems == []
    assert blockers == [], f"gates A07 sin cerrar: {blockers}"
    assert engine["code_revision"] == "1a11cec5a95144578e5fdaad4c2901ad14d55911"
    assert engine["implementation_status"] == "implemented"
    assert engine["evidence_entrypoint"] == "studio/comfy_studio/engines/reference_local.py"
    assert engine["evidence_test"] == "studio/tests/test_engine_reference_local.py"
    # El lock adjuntado es el FFmpeg ya fijado, versionado, y su hash cuadra.
    assert lock_name == "upgrades-ultra-version/locks/ffmpeg-ubuntu24.04-amd64.lock.json"
    assert lock_path.is_file()
    assert lock_name in (mod._git_tracked_paths(mod.REPO_ROOT) or set())
    assert hashlib.sha256(lock_path.read_bytes()).hexdigest() == lock_hash
    assert mod.load_json(lock_path)["engine_id"] == "ffmpeg"
    # La redistribución hereda la decisión condicional del componente ffmpeg.
    assert engine["redistribution_decision"] == "conditional"
    assert engine["notices_reviewed"] is True

    # El cierre de reference-local no prepara el release del manifiesto completo.
    full_problems, full_blockers = mod.validate(manifest, cap_ids)
    assert full_problems == []
    assert full_blockers, "el manifiesto completo debe seguir con bloqueos pendientes"
    assert not any("reference-local" in blocker for blocker in full_blockers)


def test_counts_mismatch_is_a_problem() -> None:
    mod = _load_module()
    manifest = {"counts": {"engines": 99}, "engines": [{"id": "a", "disposition": "required"}]}
    problems, _ = mod.validate(manifest, set())
    assert any("counts.engines" in p for p in problems)


def test_manifest_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    mod = _load_module()
    manifest_path = tmp_path / "duplicate.json"
    manifest_path.write_text('{"engine":{"id":"first","id":"second"}}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key 'id'"):
        mod.load_json(manifest_path)


def test_excluded_cannot_be_build_selected() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {"id": "x", "disposition": "excluded", "build_selected": True, "terms_review": "t"}
        ]
    }
    problems, _ = mod.validate(manifest, set())
    assert any("no puede estar build_selected" in p for p in problems)


def test_selected_without_license_review_is_a_problem() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {"id": "y", "disposition": "required", "build_selected": True, "terms_review": ""}
        ]
    }
    problems, _ = mod.validate(manifest, set())
    assert any("terms_review" in p for p in problems)


def test_unknown_capability_reference_is_a_problem() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {
                "id": "z",
                "disposition": "required",
                "build_selected": True,
                "terms_review": "ok",
                "capabilities": ["C99"],
            }
        ]
    }
    problems, _ = mod.validate(manifest, {"C01", "C02"})
    assert any("capability inexistente C99" in p for p in problems)


def test_pending_lock_is_a_release_blocker_not_a_coherence_error() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {
                "id": "w",
                "disposition": "required",
                "build_selected": True,
                "terms_review": "ok",
                "lock_required_before_build": True,
                "code_revision": None,
            }
        ]
    }
    problems, blockers = mod.validate(manifest, set())
    assert problems == []  # no es incoherencia
    assert any("lock de código pendiente" in b for b in blockers)


def test_a07_requires_immutable_revision_and_component_evidence() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {
                "id": "engine",
                "disposition": "required",
                "build_selected": True,
                "terms_review": "reviewed",
                "lock_required_before_build": True,
                "code_revision": "v1.2.3",
                "dependency_lock_hashes": {"requirements.txt": "not-a-hash"},
                "redistribution_decision": None,
                "notices_reviewed": False,
                "implementation_status": "specified_not_implemented_here",
            }
        ]
    }
    problems, blockers = mod.validate(manifest, set())
    assert problems == []
    assert any("SHA inmutable de 40 hex" in blocker for blocker in blockers)
    assert any("hashes SHA-256" in blocker for blocker in blockers)
    assert any("decisión explícita de redistribución" in blocker for blocker in blockers)
    assert any("revisión explícita de notices" in blocker for blocker in blockers)
    assert any("entrypoint real" in blocker for blocker in blockers)


def test_a07_accepts_only_complete_declared_component_pins(tmp_path: Path) -> None:
    mod = _load_module()
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("package==1.2.3 --hash=sha256:abc\n", encoding="utf-8")
    entrypoint_path = tmp_path / "src" / "engine.py"
    entrypoint_path.parent.mkdir()
    entrypoint_path.write_text("def run(): return True\n", encoding="utf-8")
    test_path = tmp_path / "tests" / "test_engine.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_run(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            "requirements.lock",
            "src/engine.py",
            "tests/test_engine.py",
        ],
        check=True,
    )
    manifest = {
        "engines": [
            {
                "id": "engine",
                "disposition": "required",
                "build_selected": True,
                "terms_review": "reviewed",
                "lock_required_before_build": True,
                "code_revision": "a" * 40,
                "dependency_lock_hashes": {
                    "requirements.lock": hashlib.sha256(lock_path.read_bytes()).hexdigest()
                },
                "redistribution_decision": "allowed",
                "notices_reviewed": True,
                "implementation_status": "implemented",
                "evidence_entrypoint": "src/engine.py",
                "evidence_test": "tests/test_engine.py",
            }
        ]
    }
    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=True
    )
    assert problems == []
    assert blockers == []


def test_a07_blocks_implemented_engine_without_tracked_entrypoint_and_test(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("package==1.2.3 --hash=sha256:abc\n", encoding="utf-8")
    entrypoint_path = tmp_path / "src" / "engine.py"
    entrypoint_path.parent.mkdir()
    entrypoint_path.write_text("def run(): return True\n", encoding="utf-8")
    test_path = tmp_path / "tests" / "test_engine.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_run(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "requirements.lock"], check=True)
    manifest = {
        "engines": [
            {
                "id": "engine",
                "disposition": "required",
                "build_selected": True,
                "terms_review": "reviewed",
                "lock_required_before_build": True,
                "code_revision": "a" * 40,
                "dependency_lock_hashes": {
                    "requirements.lock": hashlib.sha256(lock_path.read_bytes()).hexdigest()
                },
                "redistribution_decision": "allowed",
                "notices_reviewed": True,
                "implementation_status": "implemented",
                "evidence_entrypoint": "src/engine.py",
                "evidence_test": "tests/test_engine.py",
            }
        ]
    }

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=True
    )

    assert problems == []
    assert any("entrypoint src/engine.py no está versionado" in item for item in blockers)
    assert any("test tests/test_engine.py no está versionado" in item for item in blockers)


def test_a07_blocks_untracked_lock_file(tmp_path: Path) -> None:
    mod = _load_module()
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("package==1.2.3\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {
            "requirements.lock": hashlib.sha256(lock_path.read_bytes()).hexdigest()
        },
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path
    )

    assert problems == []
    assert any("lock file requirements.lock no está versionado" in blocker
               for blocker in blockers)


def test_a07_blocks_lock_hash_when_file_is_missing(tmp_path: Path) -> None:
    mod = _load_module()
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {"missing.lock": "b" * 64},
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("lock file missing.lock no existe" in blocker for blocker in blockers)


def test_a07_blocks_lock_hash_when_file_bytes_change(tmp_path: Path) -> None:
    mod = _load_module()
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("package==1.2.4\n", encoding="utf-8")
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {"requirements.lock": "b" * 64},
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("SHA-256 no coincide" in blocker for blocker in blockers)


def test_a07_reports_unreadable_lock_as_a_release_blocker(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module()
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("package==1.2.3\n", encoding="utf-8")
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {"requirements.lock": "b" * 64},
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    def fail_read(_path: Path) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(mod, "_sha256_file", fail_read)
    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("lock file requirements.lock no es legible" in blocker for blocker in blockers)


def test_a07_blocks_lock_path_that_escapes_repository_root(tmp_path: Path) -> None:
    mod = _load_module()
    outside = tmp_path.parent / "outside.lock"
    outside.write_text("package==1.2.3\n", encoding="utf-8")
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {
            "../outside.lock": hashlib.sha256(outside.read_bytes()).hexdigest()
        },
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("fuera de la raíz del repositorio" in blocker for blocker in blockers)


def test_a07_blocks_lock_symlink_that_escapes_repository_root(tmp_path: Path) -> None:
    mod = _load_module()
    outside = tmp_path.parent / "outside-symlink.lock"
    outside.write_text("package==1.2.3\n", encoding="utf-8")
    lock_link = tmp_path / "external.lock"
    try:
        lock_link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is not available")
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {
            "external.lock": hashlib.sha256(outside.read_bytes()).hexdigest()
        },
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("fuera de la raíz del repositorio" in blocker for blocker in blockers)


def test_a07_blocks_tracked_symlink_with_untracked_lock_target(tmp_path: Path) -> None:
    mod = _load_module()
    target = tmp_path / "actual.lock"
    target.write_text("package==1.2.3\n", encoding="utf-8")
    lock_link = tmp_path / "dependency.lock"
    try:
        lock_link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is not available")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "dependency.lock"], check=True)
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {
            "dependency.lock": hashlib.sha256(target.read_bytes()).hexdigest()
        },
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(manifest, set(), repo_root=tmp_path)

    assert problems == []
    assert any("target de lock actual.lock no está versionado" in blocker
               for blocker in blockers)


def test_a07_blocks_absolute_lock_path(tmp_path: Path) -> None:
    mod = _load_module()
    absolute_lock = tmp_path.parent / "absolute.lock"
    absolute_lock.write_text("package==1.2.3\n", encoding="utf-8")
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {
            str(absolute_lock): hashlib.sha256(absolute_lock.read_bytes()).hexdigest()
        },
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("dependency lock" in blocker and "fuera de la raíz" in blocker
               for blocker in blockers)


@pytest.mark.parametrize("lock_name", ["bad\x00.lock", "loop.lock"])
def test_a07_reports_invalid_lock_paths_as_release_blockers(
    tmp_path: Path, lock_name: str
) -> None:
    mod = _load_module()
    if lock_name == "loop.lock":
        try:
            (tmp_path / lock_name).symlink_to(tmp_path / lock_name)
        except (NotImplementedError, OSError):
            pytest.skip("symlink creation is not available")
    manifest = {"engines": [{
        "id": "engine",
        "disposition": "required",
        "build_selected": True,
        "terms_review": "reviewed",
        "lock_required_before_build": True,
        "code_revision": "a" * 40,
        "dependency_lock_hashes": {lock_name: "b" * 64},
        "redistribution_decision": "allowed",
        "notices_reviewed": True,
        "implementation_status": "implemented",
    }]}

    problems, blockers = mod.validate(
        manifest, set(), repo_root=tmp_path, require_git_tracked=False
    )

    assert problems == []
    assert any("lock file" in blocker and "ruta inválida" in blocker for blocker in blockers)


def test_blender_artifact_lock_matches_manifest_and_docker_build() -> None:
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    blender = next(engine for engine in manifest["engines"] if engine["id"] == "blender")
    lock_name, lock_hash = next(iter(blender["dependency_lock_hashes"].items()))
    lock_path = mod.REPO_ROOT / lock_name
    lock = mod.load_json(lock_path)
    dockerfile = (mod.REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    problems, blockers = mod.validate(manifest, mod._capability_ids(mod.DEFAULT_CAPS))

    assert problems == []
    assert not any(blocker.startswith("blender:") for blocker in blockers)
    assert hashlib.sha256(lock_path.read_bytes()).hexdigest() == lock_hash
    assert lock["engine_id"] == blender["id"]
    assert lock["artifact"]["url"] == blender["binary_artifact_url"]
    assert lock["artifact"]["sha256"] == blender["binary_artifact_sha256"]
    assert lock["artifact"]["url"] in dockerfile
    assert lock["artifact"]["sha256"] in dockerfile
    assert f"bpy.app.version_string == '{lock['artifact']['version']}'" in dockerfile


def test_duplicate_id_is_a_problem() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {"id": "dup", "disposition": "required"},
            {"id": "dup", "disposition": "alternative"},
        ]
    }
    problems, _ = mod.validate(manifest, set())
    assert any("id duplicado" in p for p in problems)


def test_verified_public_engines_have_code_revision() -> None:
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    assert by_id["ffmpeg"]["code_revision"] == "e38092ef9395d7049f871ef4d5411eb410e283e0"
    assert by_id["whisperx"]["code_revision"] == "3ccc17b8de34f305300f8a3fd3c9f76ba820c0d0"


def test_lock_opentimelineio_tiene_lock_fijado() -> None:
    """El engine opentimelineio usa el commit inmutable de la release oficial."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    revision = by_id["opentimelineio"]["code_revision"]
    assert revision == "44236713c1db295a6ffc66189ae98dbdfd0cb9c4"
    assert len(revision) == 40


def test_lock_opencolorio_tiene_lock_fijado() -> None:
    """El engine opencolorio usa el commit inmutable de la release oficial."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    revision = by_id["opencolorio"]["code_revision"]
    assert revision == "10a43ec1df03d072ca9a9e2fb3f26e454b0900d8"
    assert len(revision) == 40


def test_lock_remotion_tiene_lock_fijado() -> None:
    """El engine pinneado, versión npm, lockfile y build permanecen coherentes."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    revision = by_id["remotion"]["code_revision"]
    assert revision == "e6df941441f3a2c8b4677a3dc5f1326746ac9db3"

    package = json.loads((REPO_ROOT / "studio" / "remotion" / "package.json").read_text())
    version = package["dependencies"]["remotion"]
    assert version == "4.0.541"
    assert package["dependencies"]["@remotion/cli"] == version

    lock = json.loads(
        (REPO_ROOT / "studio" / "remotion" / "package-lock.json").read_text()
    )
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]
    for name, version in package["dependencies"].items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == version

    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    remotion_copy = dockerfile.index("COPY studio/remotion/ /opt/remotion/")
    remotion_build = dockerfile.index("RUN npm ci --no-audit --no-fund", remotion_copy)
    assert remotion_build > remotion_copy
    dockerignore = (REPO_ROOT / ".dockerignore").read_text()
    assert "!docs/licenses/remotion.md" in dockerignore


def test_production_memory_engine_has_locked_cpu_entrypoint_and_test() -> None:
    """The project-memory engine pins its internal source dependencies and tests."""
    manifest = json.loads(
        (REPO_ROOT / "upgrades-ultra-version" / "engines.json").read_text()
    )
    engine = next(item for item in manifest["engines"] if item["id"] == "production-memory")
    capabilities = json.loads(
        (REPO_ROOT / "upgrades-ultra-version" / "capabilities.json").read_text()
    )
    c04 = next(item for item in capabilities["capabilities"] if item["id"] == "C04")

    assert engine["implementation_status"] == "implemented"
    assert c04["status"] == "implemented_pre_gpu; gpu_validation_pending"
    assert engine["code_revision"] == "8de272798924116b383e6802868dc0dfeab5b4ff"
    assert engine["redistribution_decision"] == "allowed"
    assert engine["notices_reviewed"] is True
    assert engine["evidence_entrypoint"] == "studio/comfy_studio/agent/tools.py"
    assert engine["evidence_test"] == "studio/tests/agent/test_tools.py"

    lock_path = "upgrades-ultra-version/locks/production-memory.lock.json"
    lock = json.loads((REPO_ROOT / lock_path).read_text())
    assert lock["engine_id"] == "production-memory"
    assert lock["source_revision"] == engine["code_revision"]
    assert lock["runtime"]["additional_external_distributions"] == []
    expected_lock_hash = hashlib.sha256((REPO_ROOT / lock_path).read_bytes()).hexdigest()
    assert engine["dependency_lock_hashes"] == {lock_path: expected_lock_hash}

    for source_path, expected_hash in lock["internal_source_sha256"].items():
        actual_hash = hashlib.sha256((REPO_ROOT / source_path).read_bytes()).hexdigest()
        assert actual_hash == expected_hash, source_path
    for source_path, expected_hash in lock.get("integration_source_sha256", {}).items():
        actual_hash = hashlib.sha256((REPO_ROOT / source_path).read_bytes()).hexdigest()
        assert actual_hash == expected_hash, source_path
