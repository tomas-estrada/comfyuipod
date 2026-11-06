"""PR-3 atlas-package-reproducible: doble build idéntico, alterado bloqueado."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from test_atlas_core import _ref, make_png

from comfy_studio.atlas.models import Environment, Place, SourceRecord, View
from comfy_studio.atlas.packaging import (
    build_bundle,
    diff_fingerprints,
    fingerprint_bundle,
    verify_bundle,
)
from comfy_studio.atlas.store import AtlasStore, sha256_file


def _src_atlas(root: Path) -> Path:
    src = root / "src"
    (src / "files" / "aa").mkdir(parents=True)
    (src / "previews").mkdir(parents=True)
    (src / "licenses").mkdir(parents=True)
    (src / "osm").mkdir(parents=True)
    store = AtlasStore(src / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_pkg", municipality="Medellín",
                             name="Puente Paquete", kind=Environment.BRIDGE,
                             lat=6.2088, lon=-75.5670))
    r = _ref("ref_pkg", "plc_pkg", 6.2089, -75.5671, title="Puente Paquete")
    fpath = src / "files" / r.file_path
    make_png(fpath, 64, 48)
    r.sha256 = sha256_file(fpath)
    r.bytes = fpath.stat().st_size
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    store.upsert_view(View(view_id="view_pkg", place_id="plc_pkg",
                           ref_id="ref_pkg", lat=6.2089, lon=-75.5671))
    store.upsert_source(SourceRecord(
        source_id="osm", name="OSM", kind="osm_extract",
        url="https://download.geofabrik.de/x.osm.pbf", license="ODbL"))
    store.close()
    (src / "osm" / "aburra-valley.osm.pbf").write_bytes(b"fake-pbf")
    (src / "sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
    return src


def test_doble_build_reproducible(tmp_path: Path) -> None:
    src = _src_atlas(tmp_path)
    s1 = build_bundle(src, tmp_path / "b1", max_shipped=10)
    s2 = build_bundle(src, tmp_path / "b2", max_shipped=10)
    assert s1["shipped"] == s2["shipped"] == 1
    f1, f2 = fingerprint_bundle(tmp_path / "b1"), fingerprint_bundle(tmp_path / "b2")
    assert diff_fingerprints(f1, f2) == [], diff_fingerprints(f1, f2)
    assert f1["files"] and f1["db"], "la huella no debe salir vacía"
    assert verify_bundle(tmp_path / "b1")["ok"]


def _copia(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copytree(tmp_path / "b1", dest)
    return dest


def test_paquete_alterado_bloquea_con_mensaje(tmp_path: Path) -> None:
    src = _src_atlas(tmp_path)
    build_bundle(src, tmp_path / "b1", max_shipped=10)

    t_hash = _copia(tmp_path, "t_hash")
    shipped = next((t_hash / "files").rglob("*.jpg"))
    data = bytearray(shipped.read_bytes())
    data[10] ^= 0xFF
    shipped.write_bytes(bytes(data))
    rep = verify_bundle(t_hash)
    assert not rep["ok"]
    assert any("hash" in e for e in rep["errors"]), rep["errors"]

    t_del = _copia(tmp_path, "t_del")
    next((t_del / "files").rglob("*.jpg")).unlink()
    rep = verify_bundle(t_del)
    assert not rep["ok"]
    assert any("ausente" in e for e in rep["errors"]), rep["errors"]

    t_nom = _copia(tmp_path, "t_nom")
    (t_nom / "manifest.json").unlink()
    rep = verify_bundle(t_nom)
    assert not rep["ok"]
    assert any("manifest" in e for e in rep["errors"]), rep["errors"]
