"""PR-3 atlas-verify-duro: integridad referencial del catálogo."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_atlas_core import _ref, make_png

from comfy_studio.atlas.models import Environment, Place, View
from comfy_studio.atlas.store import AtlasStore


def _png64(root: Path, rel: str) -> tuple[str, int]:
    """Crea un PNG de 64x48 (coincide con `_ref`) y devuelve (sha256, bytes)."""
    import hashlib

    fpath = root / "files" / rel
    make_png(fpath, 64, 48)
    data = fpath.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _raw_execute(db: Path, sql: str, params: tuple = ()) -> None:
    """Escritura SQL directa sin FK: simula un catálogo corrupto/legado."""
    import sqlite3

    con = sqlite3.connect(str(db))
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(sql, params)
    con.commit()
    con.close()


@pytest.fixture()
def verify_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_a", municipality="Medellín",
                             name="Puente A", kind=Environment.BRIDGE,
                             lat=6.20, lon=-75.58))
    store.upsert_place(Place(place_id="plc_b", municipality="Medellín",
                             name="Puente B", kind=Environment.BRIDGE,
                             lat=6.21, lon=-75.58))
    r = _ref("ref_ok", "plc_a", 6.20, -75.58, title="Puente A")
    r.sha256, r.bytes = _png64(root, r.file_path)
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    store.upsert_view(View(view_id="view_ok", place_id="plc_a", ref_id="ref_ok",
                           lat=6.20, lon=-75.58))
    store.close()
    return root


def _codes(report: dict, ref_id: str | None = None,
           view_id: str | None = None) -> list[str]:
    out = []
    for e in report["errors"]:
        if ref_id is not None and e.get("ref_id") != ref_id:
            continue
        if view_id is not None and e.get("view_id") != view_id:
            continue
        out.append(e["error"])
    return out


def test_verify_ok_mide_vistas(verify_root: Path) -> None:
    store = AtlasStore(verify_root / "catalog.db")
    report = store.verify(verify_root / "files")
    assert report["ok"], report["errors"]
    assert report["checked_references"] == 1
    assert report["checked_views"] == 1
    assert report["warning_count"] == 0
    store.close()


def test_vista_sin_referencia(verify_root: Path) -> None:
    _raw_execute(
        verify_root / "catalog.db",
        "INSERT INTO views(view_id, place_id, ref_id, label, calib_state, method,"
        " usable, created_ts) VALUES (?,?,?,?,?,?,?,?)",
        ("view_fantasma", "plc_a", "ref_inexistente", "", "uncalibrated",
         "gps_metadata", 1, 0.0),
    )
    store = AtlasStore(verify_root / "catalog.db")
    report = store.verify(verify_root / "files")
    assert not report["ok"]
    assert "vista sin referencia" in _codes(report, view_id="view_fantasma")
    store.close()


def test_vista_sin_lugar(verify_root: Path) -> None:
    _raw_execute(
        verify_root / "catalog.db",
        "INSERT INTO views(view_id, place_id, ref_id, label, calib_state, method,"
        " usable, created_ts) VALUES (?,?,?,?,?,?,?,?)",
        ("view_sin_lugar", "plc_inexistente", "ref_ok", "", "uncalibrated",
         "gps_metadata", 1, 0.0),
    )
    store = AtlasStore(verify_root / "catalog.db")
    report = store.verify(verify_root / "files")
    assert not report["ok"]
    assert "vista sin lugar" in _codes(report, view_id="view_sin_lugar")
    store.close()


def test_vista_y_referencia_en_lugares_distintos(verify_root: Path) -> None:
    store = AtlasStore(verify_root / "catalog.db")
    store.upsert_view(View(view_id="view_cruzada", place_id="plc_b",
                           ref_id="ref_ok"))
    report = store.verify(verify_root / "files")
    assert not report["ok"]
    assert "vista y referencia en lugares distintos" in _codes(
        report, view_id="view_cruzada")
    store.close()


def test_referencia_sin_lugar(verify_root: Path) -> None:
    sha, size = _png64(verify_root, "aa/ref_huerfana.png")
    _raw_execute(
        verify_root / "catalog.db",
        "INSERT INTO atlas_refs(ref_id, place_id, title, file_path, sha256, source,"
        " license, date_reliability, fetched_ts, width, height, bytes, mime, usable,"
        " content_class) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ref_huerfana", "plc_fantasma", "Huérfana", "aa/ref_huerfana.png", sha,
         "wikimedia_commons", "CC BY-SA 4.0", "captured", 0.0, 64, 48, size,
         "image/png", 1, "fondo_urbano"),
    )
    store = AtlasStore(verify_root / "catalog.db")
    store.set_license("ref_huerfana", "CC BY-SA 4.0",
                      "https://creativecommons.org/licenses/by-sa/4.0/",
                      "Autora, CC BY-SA 4.0", None)
    report = store.verify(verify_root / "files")
    assert not report["ok"]
    assert "referencia sin lugar" in _codes(report, ref_id="ref_huerfana")
    store.close()


def test_file_path_duplicado_con_sha_distinto(verify_root: Path) -> None:
    store = AtlasStore(verify_root / "catalog.db")
    # ref_ok ya usa aa/ref_ok.png con su sha; registrar otra ref al mismo path
    # con un sha distinto (contenido diferente) es corrupción potencial.
    r = _ref("ref_dup", "plc_a", 6.20, -75.58, title="Duplicada")
    r.file_path = "aa/ref_ok.png"
    import hashlib

    # mismo tamaño que el archivo real pero sha distinto: el error que debe
    # saltar es el duplicado (más el sha que ya no coincide).
    r.bytes = (verify_root / "files" / "aa" / "ref_ok.png").stat().st_size
    r.sha256 = hashlib.sha256(b"contenido-distinto").hexdigest()
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    report = store.verify(verify_root / "files")
    assert not report["ok"]
    assert "file_path duplicado con sha256 distinto" in _codes(
        report, ref_id="ref_ok")
    assert "file_path duplicado con sha256 distinto" in _codes(
        report, ref_id="ref_dup")
    store.close()


def test_licencia_no_registrada_y_desconocida(verify_root: Path) -> None:
    store = AtlasStore(verify_root / "catalog.db")
    r = _ref("ref_sin_lic", "plc_a", 6.20, -75.58, title="Sin licencia")
    r.sha256, r.bytes = _png64(verify_root, r.file_path)
    store.upsert_reference(r)
    # sin fila en licenses -> error
    report = store.verify(verify_root / "files")
    assert "licencia no registrada" in _codes(report, ref_id="ref_sin_lic")
    store.set_license(r.ref_id, "UNKNOWN", None, None, None)
    report = store.verify(verify_root / "files")
    assert "licencia desconocida" in _codes(report, ref_id="ref_sin_lic")
    store.close()
