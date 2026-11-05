"""Tests del núcleo del atlas: store (RTree+FTS5), imaging, geo y servicio."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from comfy_studio.atlas.geo import haversine_m, latlon_to_xy, normalize_text
from comfy_studio.atlas.imaging import image_dimensions, sniff_mime
from comfy_studio.atlas.models import (
    Coverage,
    DateReliability,
    Environment,
    Place,
    Reference,
    ResolutionClass,
    View,
)
from comfy_studio.atlas.service import AtlasService
from comfy_studio.atlas.store import AtlasStore, sha256_file


def make_png(path: Path, w: int, h: int, color: tuple[int, int, int] = (200, 30, 30)) -> None:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def make_jpeg(path: Path, w: int, h: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sof = b"\xff\xc0" + struct.pack(">H", 17) + bytes([8]) + struct.pack(">HH", h, w)
    sof += b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    path.write_bytes(b"\xff\xd8" + sof + b"\xff\xd9")


def _ref(ref_id: str, place_id: str, lat: float, lon: float, title: str = "Foto") -> Reference:
    return Reference(
        ref_id=ref_id,
        title=title,
        file_path=f"aa/{ref_id}.png",
        sha256="0" * 64,
        source="wikimedia_commons",
        place_id=place_id,
        source_page=f"https://commons.wikimedia.org/wiki/File:{ref_id}.png",
        author="Autor de prueba",
        license="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
        attribution="Autor de prueba, CC BY-SA 4.0",
        capture_date="2024-05-01",
        date_reliability=DateReliability.CAPTURED,
        width=64,
        height=48,
        bytes=0,
        mime="image/png",
        resolution_class=ResolutionClass.ORIGINAL,
        gps_lat=lat,
        gps_lon=lon,
        quality=0.8,
        usable=True,
    )


@pytest.fixture()
def atlas_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")

    p_bridge = Place(
        place_id="plc_puente_4sur",
        municipality="Medellín",
        name="Puente de la 4 Sur",
        kind=Environment.BRIDGE,
        aliases=["puente 4 sur", "puente de la cuarta sur"],
        lat=6.2088,
        lon=-75.5670,
    )
    p_plaza = Place(
        place_id="plc_plaza_botero",
        municipality="Medellín",
        name="Plaza Botero",
        kind=Environment.PLAZA,
        aliases=["parque botero"],
        lat=6.2518,
        lon=-75.5686,
    )
    p_envigado = Place(
        place_id="plc_parque_envigado",
        municipality="Envigado",
        name="Parque de Envigado",
        kind=Environment.PARK,
        aliases=["parque principal envigado"],
        lat=6.1699,
        lon=-75.5920,
    )
    # nombre repetido en otro municipio para probar ambigüedad
    p_plaza_sab = Place(
        place_id="plc_plaza_sabaneta",
        municipality="Sabaneta",
        name="Parque de Envigado",
        kind=Environment.PARK,
        aliases=[],
        lat=6.1520,
        lon=-75.6170,
    )
    for p in (p_bridge, p_plaza, p_envigado, p_plaza_sab):
        store.upsert_place(p)

    refs = [
        _ref("ref_a", "plc_puente_4sur", 6.2089, -75.5671, "Puente 4 Sur vista norte"),
        _ref("ref_b", "plc_puente_4sur", 6.2087, -75.5669, "Puente 4 Sur vista sur"),
        _ref("ref_c", "plc_plaza_botero", 6.2519, -75.5687, "Plaza Botero"),
        _ref("ref_d", "plc_parque_envigado", 6.1700, -75.5921, "Parque Envigado"),
    ]
    for r in refs:
        fpath = root / "files" / r.file_path
        make_png(fpath, 64, 48)
        r.sha256 = sha256_file(fpath)
        r.bytes = fpath.stat().st_size
        store.upsert_reference(r)
        store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
        store.upsert_view(View(
            view_id=f"view_{r.ref_id}", place_id=r.place_id, ref_id=r.ref_id,
            label="vista gps", lat=r.gps_lat, lon=r.gps_lon,
        ))
    store.upsert_coverage(Coverage(
        place_id="plc_puente_4sur", views_observed=2,
        backed={"vistas": ["norte", "sur"]}, unobserved={"cara_este": True},
        evidence_newest="2024-05-01", evidence_oldest="2023-01-01",
        confidence_basis="2 vistas con GPS verificado",
    ))
    store.close()
    return root


# ---------------------------------------------------------------------------
# imaging / geo
# ---------------------------------------------------------------------------


def test_imaging_png_y_jpeg(tmp_path: Path) -> None:
    png = tmp_path / "a.png"
    jpg = tmp_path / "b.jpg"
    make_png(png, 64, 48)
    make_jpeg(jpg, 800, 600)
    assert sniff_mime(png) == "image/png"
    assert image_dimensions(png) == (64, 48)
    assert sniff_mime(jpg) == "image/jpeg"
    assert image_dimensions(jpg) == (800, 600)
    (tmp_path / "nope.bin").write_bytes(b"xx")
    assert sniff_mime(tmp_path / "nope.bin") is None


def test_geo_helpers() -> None:
    assert normalize_text("Puente de la 4 Sur, Medellín!") == "puente de la 4 sur medellin"
    x, y = latlon_to_xy(6.25, -75.57, 6.2501, -75.5699)
    assert x > 0 and y > 0
    d = haversine_m(6.25, -75.57, 6.25, -75.56)
    assert 1000 < d < 1200  # 0.01° lon a 6.25°N ≈ 1106 m
    assert abs(haversine_m(6.25, -75.57, 6.2501, -75.57) - 11.06) < 0.5


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_places_fts_rtree(atlas_root: Path) -> None:
    store = AtlasStore(atlas_root / "catalog.db")
    exact = store.get_place_by_norm_name(normalize_text("Puente de la 4 Sur"))
    assert len(exact) == 1 and exact[0].place_id == "plc_puente_4sur"
    fts = store.search_places_fts("botero")
    assert any(p.place_id == "plc_plaza_botero" for p in fts)
    near = store.nearest_places(6.2088, -75.5670, 500)
    assert near[0][0].place_id == "plc_puente_4sur"
    refs_near = store.nearest_references(6.2088, -75.5670, 100)
    assert {r.ref_id for r, _ in refs_near} == {"ref_a", "ref_b"}
    store.close()


def test_store_verify_ok_y_fallos(atlas_root: Path) -> None:
    store = AtlasStore(atlas_root / "catalog.db")
    files_root = atlas_root / "files"
    report = store.verify(files_root)
    assert report["ok"], report["errors"]
    # archivo ausente -> error
    (files_root / "aa" / "ref_a.png").unlink()
    report = store.verify(files_root)
    assert not report["ok"]
    assert any(e["ref_id"] == "ref_a" and e["error"] == "archivo ausente" for e in report["errors"])
    # hash incorrecto -> error (restaurar archivo con otros bytes)
    make_png(files_root / "aa" / "ref_a.png", 64, 48, color=(1, 2, 3))
    report = store.verify(files_root)
    assert any(
        e["ref_id"] == "ref_a" and e["error"] == "sha256 no coincide"
        for e in report["errors"]
    )
    # sin exigir archivos, solo metadatos: pasa (restaurar PNG idéntico)
    make_png(files_root / "aa" / "ref_a.png", 64, 48)
    report = store.verify(files_root, require_files=False, check_hashes=False)
    assert report["ok"]
    store.close()


def test_store_licenses_y_stats(atlas_root: Path) -> None:
    store = AtlasStore(atlas_root / "catalog.db")
    lic = store.get_license("ref_a")
    assert lic and lic["license"] == "CC BY-SA 4.0"
    stats = store.stats()
    assert stats["places"] == 4
    assert stats["references_usable"] == 4
    assert stats["places_with_usable_views"] == 3
    assert stats["places_by_municipality"]["Medellín"] == 2
    assert stats["references_by_municipality"]["Envigado"] == 1
    store.close()


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def test_resolve_place_estados(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    ok = svc.resolve_place("Puente de la 4 Sur")
    assert ok["status"] == "confirmed"
    assert ok["place"]["place_id"] == "plc_puente_4sur"
    amb = svc.resolve_place("Parque de Envigado")
    assert amb["status"] == "ambiguous"
    cand_ids = {c["place_id"] for c in amb["candidates"]}
    assert cand_ids == {"plc_parque_envigado", "plc_plaza_sabaneta"}
    ok2 = svc.resolve_place("Parque de Envigado", municipality="Sabaneta")
    assert ok2["status"] == "confirmed" and ok2["place"]["place_id"] == "plc_plaza_sabaneta"
    nf = svc.resolve_place("La Luna en un cohete")
    assert nf["status"] == "not_found"


def test_select_view_limitaciones(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    sel = svc.select_view("plc_puente_4sur", lat=6.2088, lon=-75.5670, heading_deg=90)
    assert sel["status"] == "ok"
    assert sel["view"]["place_id"] == "plc_puente_4sur"
    codes = [lim["code"] for lim in sel["limitations"]]
    assert "heading_unknown" in codes  # rumbo desconocido -> limitación tipada, no 0
    far = svc.select_view("plc_puente_4sur", lat=6.2518, lon=-75.5686)
    assert any(lim["code"] == "camera_far_from_views" for lim in far["limitations"])


def test_prepare_shot_explicito_y_auto(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    shot = svc.prepare_shot(place="Puente de la 4 Sur", prompt="carro sobre el puente")
    assert shot["status"] == "ok"
    assert shot["place_id"] == "plc_puente_4sur"
    assert shot["background_path"].startswith("aa/")
    assert Path(shot["background_abs"]).is_file()
    assert shot["reference"]["license"] == "CC BY-SA 4.0"
    # selección activa persistida
    sel = svc.get_selection()
    assert sel["place_id"] == "plc_puente_4sur" and sel["view_id"] == shot["view_id"]
    # sin ubicación: usa selección activa
    shot2 = svc.prepare_shot(prompt="otro plano")
    assert shot2["status"] == "ok" and shot2["place_id"] == "plc_puente_4sur"
    assert any(lim["code"] == "using_active_selection" for lim in shot2["limitations"])


def test_prepare_shot_ambiguo_no_sustituye(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    out = svc.prepare_shot(place="Parque de Envigado")
    assert out["status"] == "ambiguous_place"
    assert len(out["candidates"]) == 2


def test_prepare_shot_hash_mismatch(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    make_png(atlas_root / "files" / "aa" / "ref_a.png", 64, 48, color=(9, 9, 9))
    out = svc.prepare_shot(place_id="plc_puente_4sur", view_id="view_ref_a")
    assert out["status"] == "hash_mismatch"


def test_manifest_y_mapa(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    manifest = svc.export_manifest()
    assert manifest["counts"]["reference_files"] == 4
    assert all(f["sha256"] for f in manifest["files"])
    path = svc.write_manifest()
    assert json.loads(path.read_text(encoding="utf-8"))["atlas_version"] == "1.0.0-test"
    svg = svc.map_svg()
    assert svg.startswith("<svg") and "plc" not in svg  # markers usan <title>, no ids
    assert "Puente de la 4 Sur" in svg


def test_acquisition_resumable_items(atlas_root: Path) -> None:
    store = AtlasStore(atlas_root / "catalog.db")
    store.begin_run("run1", "commons")
    store.acq_item("run1", "commons", "File:X.jpg", "done")
    assert store.acq_item_status("run1", "commons", "File:X.jpg") == "done"
    assert store.count_done_items("run1", "commons") == 1
    store.acq_item("run1", "commons", "File:X.jpg", "failed", "429")
    assert store.acq_item_status("run1", "commons", "File:X.jpg") == "failed"
    store.end_run("run1", "commons", "suspended", {"reason": "429"}, error="rate limited")
    store.close()


def test_verify_service_report(atlas_root: Path) -> None:
    svc = AtlasService(atlas_root)
    report = svc.verify()
    assert report["ok"] and report["stats"]["places"] == 4
