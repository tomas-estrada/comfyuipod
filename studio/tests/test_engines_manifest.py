"""Validador del manifiesto de engines (plan Ultra, W07 / acceptance A07).

Release gate de coherencia sobre upgrades-ultra-version/engines.json: separa
incoherencias reales (fatales) de bloqueos de release esperados (locks sin fijar).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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


def test_counts_mismatch_is_a_problem() -> None:
    mod = _load_module()
    manifest = {"counts": {"engines": 99}, "engines": [{"id": "a", "disposition": "required"}]}
    problems, _ = mod.validate(manifest, set())
    assert any("counts.engines" in p for p in problems)


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


def test_a07_accepts_only_complete_declared_component_pins() -> None:
    mod = _load_module()
    manifest = {
        "engines": [
            {
                "id": "engine",
                "disposition": "required",
                "build_selected": True,
                "terms_review": "reviewed",
                "lock_required_before_build": True,
                "code_revision": "a" * 40,
                "dependency_lock_hashes": {"requirements.txt": "b" * 64},
                "redistribution_decision": "allowed",
                "notices_reviewed": True,
                "implementation_status": "implemented",
            }
        ]
    }
    problems, blockers = mod.validate(manifest, set())
    assert problems == []
    assert blockers == []


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
    assert by_id["ffmpeg"]["code_revision"] == "n9.0.2"
    assert by_id["whisperx"]["code_revision"] == "v3.8.6"


def test_lock_opentimelineio_tiene_lock_fijado() -> None:
    """El engine opentimelineio tiene su code_revision fijado a la release oficial."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    assert by_id["opentimelineio"]["code_revision"] == "v0.18.1"


def test_lock_opencolorio_tiene_lock_fijado() -> None:
    """El engine opencolorio tiene su code_revision fijado a la release oficial."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    assert by_id["opencolorio"]["code_revision"] == "v2.6.0"


def test_lock_remotion_tiene_lock_fijado() -> None:
    """El engine, el manifest, npm lockfile y el build usan el mismo pin."""
    mod = _load_module()
    manifest = mod.load_json(mod.DEFAULT_MANIFEST)
    by_id = {e["id"]: e for e in manifest["engines"]}
    revision = by_id["remotion"]["code_revision"]
    assert revision == "v4.0.541"

    package = json.loads((REPO_ROOT / "studio" / "remotion" / "package.json").read_text())
    assert package["dependencies"]["remotion"] == revision.removeprefix("v")
    assert package["dependencies"]["@remotion/cli"] == revision.removeprefix("v")

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
