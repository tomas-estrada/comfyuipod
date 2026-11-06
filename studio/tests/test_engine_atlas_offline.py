"""Tests del motor atlas-offline (plan Ultra, W07 / C07).

CPU-only. Los bundles se construyen en tmp_path usando el patrón de
``test_atlas_verify.py`` y ``test_atlas_core.py``.
"""

from __future__ import annotations

# Reutilizar helpers de test_atlas_core.py
import sys
from pathlib import Path

import pytest

from comfy_studio.atlas.models import Environment, Place, View
from comfy_studio.atlas.store import AtlasStore
from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.atlas_offline import AtlasOfflineEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_atlas_core import _ref, make_png  # noqa: E402


def _make_minimal_bundle(tmp_path: Path) -> Path:
    """Crea un atlas mínimo válido usando AtlasStore (mismo patrón que verify_root)."""
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    (root / "osm").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_a", municipality="Medellín",
                             name="Puente A", kind=Environment.BRIDGE,
                             lat=6.20, lon=-75.58))
    # Use files/ prefix so verify_bundle can find the file.
    r = _ref("ref_a", "plc_a", 6.20, -75.58, title="Puente A")
    # file_path is relative to files_root (root/files/), so just "aa/ref_a.png"
    file_rel = r.file_path  # "aa/ref_a.png"
    make_png(root / "files" / file_rel, 64, 48)
    data = (root / "files" / file_rel).read_bytes()
    import hashlib
    r.sha256 = hashlib.sha256(data).hexdigest()
    r.bytes = len(data)
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    store.upsert_view(View(view_id="view_a", place_id="plc_a", ref_id="ref_a",
                           lat=6.20, lon=-75.58))
    store.close()

    # manifest.json
    rel_path = r.file_path  # "aa/ref_a.png" (relative to files/)
    manifest = {
        "version": "1.0-test",
        "files": [
            {
                "path": f"files/{rel_path}",
                "sha256": r.sha256,
                "ref_id": "ref_a",
            }
        ],
    }
    (root / "manifest.json").write_text(
        __import__("json").dumps(manifest), encoding="utf-8"
    )
    (root / "ATTRIBUTION.md").write_text("# Atribuciones\n", encoding="utf-8")
    (root / "sources.json").write_text(
        __import__("json").dumps({"sources": []}), encoding="utf-8"
    )
    # Fake OSM file (required by verify_bundle)
    (root / "osm" / "aburra-valley.osm.pbf").write_bytes(b"\x00" * 16)
    return root


def _engine() -> AtlasOfflineEngine:
    return AtlasOfflineEngine()


def _spec(**job) -> EngineRunSpec:
    return EngineRunSpec(job=job)


def test_verify_missing_root_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin STUDIO_ATLAS_ROOT, verify devuelve ok=False tipificado."""
    monkeypatch.delenv("STUDIO_ATLAS_ROOT", raising=False)
    res = AtlasOfflineEngine().verify("")
    assert not res.ok
    assert "raíz" in (res.error or "").lower() or "configurada" in (res.error or "").lower()


def test_verify_bundle_missing_catalog_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raíz configurada pero sin catalog.db → ok=False."""
    root = tmp_path / "empty_atlas"
    root.mkdir()
    monkeypatch.setenv("STUDIO_ATLAS_ROOT", str(root))
    try:
        res = AtlasOfflineEngine().verify("")
        assert not res.ok
    finally:
        monkeypatch.delenv("STUDIO_ATLAS_ROOT", raising=False)


def test_verify_bundle_valid_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundle mínimo válido → ok=True, simulated=False."""
    root = _make_minimal_bundle(tmp_path)
    monkeypatch.setenv("STUDIO_ATLAS_ROOT", str(root))
    try:
        res = AtlasOfflineEngine().verify("")
        assert res.ok, f"verify falló: {res.error}"
        assert res.simulated is False
        assert "bundle_ok" in res.payload
        assert "checks" in res.payload
    finally:
        monkeypatch.delenv("STUDIO_ATLAS_ROOT", raising=False)


def test_run_verify_bundle_op_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run con op='verify_bundle' sobre bundle válido → ok=True."""
    root = _make_minimal_bundle(tmp_path)
    monkeypatch.setenv("STUDIO_ATLAS_ROOT", str(root))
    try:
        res = AtlasOfflineEngine().run(_spec(op="verify_bundle"))
        assert res.ok, f"run falló: {res.error}"
        assert res.simulated is False
        assert "report" in res.payload
        assert res.payload["report"].get("ok") is True
    finally:
        monkeypatch.delenv("STUDIO_ATLAS_ROOT", raising=False)


def test_run_unknown_op_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_ATLAS_ROOT", raising=False)
    res = AtlasOfflineEngine().run(_spec(op="no_existe"))
    assert not res.ok
    assert "desconocida" in (res.error or "")
