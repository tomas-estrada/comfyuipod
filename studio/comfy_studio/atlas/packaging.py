"""Empaquetado del atlas para el repositorio y la imagen (/opt/studio-atlas).

El bundle publicado vive en `atlas/` (git) y se copia tal cual a la imagen:

- `catalog.db` con el inventario (todas las referencias adquiridas; las que se
  envían en el bundle quedan `usable=1` con su archivo real; el resto queda
  registrado como no enviado por tamaño, nunca como presente).
- `files/` con copias ≤ MAX_SHIP_WIDTH px (JPEG) de las referencias curadas.
  Se registran las dimensiones nativas del original (native_width/height), el
  sha256 del archivo enviado y `resolution_class=derived_downscale` con la URL
  del original: nunca si simula detalle observado mediante upscaling.
- `previews/`, `licenses/`, `osm/`, `sources.json`, `manifest.json`,
  `ATTRIBUTION.md`.

`verify_bundle` es el comprobador que corre en CI y dentro de la imagen: archivo
ausente/corrupto/sin licencia bloquea la promoción de la candidata.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from comfy_studio.atlas.geo import ABURRA_BBOX, in_bbox
from comfy_studio.atlas.models import ResolutionClass
from comfy_studio.atlas.service import ATLAS_VERSION, AtlasService
from comfy_studio.atlas.store import AtlasStore, sha256_file

MAX_SHIP_WIDTH = 2048
JPEG_QUALITY = 88
PREVIEW_MAX = 384
DEFAULT_MAX_SHIPPED = 620


def _pil():
    try:
        from PIL import Image  # type: ignore

        return Image
    except Exception:  # noqa: BLE001
        return None


def curate_refs(store: AtlasStore, max_shipped: int) -> list:
    """Selección curada para publicar: primero GPS (vistas), luego calidad,
    con diversidad de municipios (round-robin) para no concentrar todo en Medellín."""
    refs = store.all_references(usable_only=True)
    # sólo se publican referencias georreferenciadas dentro del Valle de Aburrá
    with_gps = [
        r for r in refs
        if r.gps_lat is not None and r.gps_lon is not None
        and in_bbox(r.gps_lat, r.gps_lon, ABURRA_BBOX)
    ]
    without_gps: list = []
    with_gps.sort(key=lambda r: (r.quality, r.width or 0), reverse=True)
    # primero: la mejor referencia de CADA lugar con vistas, para que ningún
    # lugar fotOGRáficamente utilizable se quede fuera del bundle
    best_per_place: dict[str, object] = {}
    for r in with_gps:
        if r.place_id and r.place_id not in best_per_place:
            best_per_place[r.place_id] = r
    guaranteed = list(best_per_place.values())
    rest = [r for r in with_gps if r not in guaranteed]
    with_gps = guaranteed + rest
    # round-robin por municipio sobre los GPS para diversidad real
    by_mun: dict[str, list] = {}
    for r in with_gps:
        place = store.get_place(r.place_id) if r.place_id else None
        by_mun.setdefault(place.municipality if place else "?", []).append(r)
    ordered: list = []
    queues = list(by_mun.values())
    while queues and len(ordered) < max_shipped:
        for q in list(queues):
            if q and len(ordered) < max_shipped:
                ordered.append(q.pop(0))
            if not q:
                queues.remove(q)
    for r in without_gps:
        if len(ordered) >= max_shipped:
            break
        ordered.append(r)
    return ordered[:max_shipped]


def build_bundle(
    src_root: Path,
    dest: Path,
    *,
    max_shipped: int = DEFAULT_MAX_SHIPPED,
    max_width: int = MAX_SHIP_WIDTH,
) -> dict[str, Any]:
    """Construye el bundle publicable desde el atlas de trabajo (src_root)."""
    src_root = Path(src_root)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    for sub in ("files", "previews", "licenses", "osm", "geodata"):
        (dest / sub).mkdir(parents=True, exist_ok=True)

    # catálogo copiado y reescrito (checkpoint WAL primero: sin él se pierden commits)
    src_store = AtlasStore(src_root / "catalog.db")
    src_store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src_store.close()
    shutil.copy2(src_root / "catalog.db", dest / "catalog.db")
    store = AtlasStore(dest / "catalog.db")
    shipped = curate_refs(store, max_shipped)
    shipped_ids = {r.ref_id for r in shipped}
    Image = _pil()

    stats: dict[str, Any] = {"shipped": 0, "skipped_no_pil": 0, "bytes_shipped": 0,
                             "total_usable": len(store.all_references(usable_only=True))}
    for ref in store.all_references(usable_only=True):
        if ref.ref_id not in shipped_ids:
            ref.usable = False
            ref.reject_reason = "adquirida pero fuera del bundle publicado (cap de tamaño)"
            store.upsert_reference(ref)

    for ref in shipped:
        src_file = src_root / "files" / ref.file_path
        if not src_file.is_file():
            ref.usable = False
            ref.reject_reason = "archivo fuente ausente en el atlas de trabajo"
            store.upsert_reference(ref)
            continue
        out_rel = f"{ref.ref_id[:2]}/{ref.ref_id}.jpg"
        out_path = dest / "files" / out_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        native_w, native_h = ref.width, ref.height
        if Image is not None:
            try:
                with Image.open(src_file) as im:
                    im = im.convert("RGB")
                    if max(im.size) > max_width:
                        im.thumbnail((max_width, max_width))
                    im.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
                    # preview
                    prev = im.copy()
                    prev.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
                    prev_path = dest / "previews" / f"{ref.ref_id}.jpg"
                    prev_path.parent.mkdir(parents=True, exist_ok=True)
                    prev.save(prev_path, "JPEG", quality=80)
            except Exception:  # noqa: BLE001
                shutil.copy2(src_file, out_path)
        else:
            shutil.copy2(src_file, out_path)
        ref.file_path = out_rel
        ref.sha256 = sha256_file(out_path)
        ref.bytes = out_path.stat().st_size
        from comfy_studio.atlas.imaging import image_dimensions, sniff_mime

        dims = image_dimensions(out_path)
        ref.width, ref.height = dims if dims else (native_w, native_h)
        ref.native_width, ref.native_height = native_w, native_h
        ref.mime = sniff_mime(out_path) or "image/jpeg"
        if ref.resolution_class == ResolutionClass.ORIGINAL and (
            ref.width != native_w or ref.height != native_h
        ):
            ref.resolution_class = ResolutionClass.DERIVED_DOWNSCALE
            ref.derived_from_url = ref.source_file_url
        store.upsert_reference(ref)
        # licencia junto al archivo enviado
        lic = store.get_license(ref.ref_id)
        if lic is not None:
            (dest / "licenses" / f"{ref.ref_id}.txt").write_text(
                f"Referencia: {ref.title}\n"
                f"Fuente: {ref.source_page}\n"
                f"Archivo original: {ref.source_file_url}\n"
                f"Autor: {ref.author or 'desconocido'}\n"
                f"Licencia: {ref.license} ({ref.license_url or 'sin URL'})\n"
                f"Captura: {ref.capture_date or 'desconocida'} ({ref.date_reliability})\n"
                f"Subida: {ref.upload_date or 'desconocida'}\n"
                f"sha256 enviado: {ref.sha256}\n"
                f"sha1 original: {ref.sha1_source or 'n/d'}\n"
                f"Original: {ref.native_width}x{ref.native_height}; "
                f"enviado: {ref.width}x{ref.height} ({ref.resolution_class})\n"
                f"Atribución: {ref.attribution}\n",
                encoding="utf-8",
            )
        stats["shipped"] += 1
        stats["bytes_shipped"] += ref.bytes or 0

    # OSM (sin caché) y fuentes
    for name in ("aburra-valley.osm.pbf", "roads.geojson.gz", "places_osm.geojson.gz"):
        p = src_root / "osm" / name
        if p.is_file():
            shutil.copy2(p, dest / "osm" / name)
    src_json = src_root / "sources.json"
    if src_json.is_file():
        shutil.copy2(src_json, dest / "sources.json")
    store.set_meta("atlas_version", ATLAS_VERSION)
    store.set_meta("bundle", json.dumps({
        "max_shipped": max_shipped,
        "max_width": max_width,
        "shipped": stats["shipped"],
        "bytes_shipped": stats["bytes_shipped"],
    }))
    store.close()

    svc = AtlasService(dest)
    svc.write_manifest()
    # ATTRIBUTION solo de lo enviado
    store2 = svc.store
    lines = [
        "# Atribuciones del Atlas audiovisual del Valle de Aburrá",
        "",
        "Cada referencia conserva autor, licencia, fechas y procedencia.",
        "Ver también `licenses/<ref_id>.txt` y `sources.json`.",
        "",
        "## Fuentes geográficas",
        "",
    ]
    for s_ in store2.list_sources():
        lines.append(f"- **{s_.name}** — {s_.license} — {s_.url} — {s_.terms or ''}")
    lines += ["", "## Referencias fotográficas enviadas (Wikimedia Commons)", ""]
    for r in store2.all_references(usable_only=True):
        lic = store2.get_license(r.ref_id)
        lines.append(
            f"- `{r.ref_id}` — {r.title} — {r.author or 'autor desconocido'} — {r.license} — "
            f"captura {r.capture_date or '?'} ({r.date_reliability}) — {r.source_page}"
        )
    (dest / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    store2.close()
    return stats


def _stable_file_hashes(root: Path, *subdirs: str) -> list[dict[str, Any]]:
    """Lista ordenada path->sha256 de archivos bajo subdirs (huella estable)."""
    out: list[dict[str, Any]] = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                out.append({
                    "path": str(p.relative_to(root)),
                    "sha256": sha256_file(p),
                    "bytes": p.stat().st_size,
                })
    out.sort(key=lambda e: e["path"])
    return out


def _stable_db_dump(db_path: Path) -> dict[str, str]:
    """Volcado lógico ordenado del catálogo (estable aunque el binario varíe).

    El archivo SQLite puede diferir byte a byte entre construcciones idénticas
    (layout de páginas, freelist); el contenido lógico ordenado por rowid no.
    Se excluyen tablas internas de SQLite.
    """
    import hashlib
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        dump: dict[str, str] = {}
        for table in tables:
            # las tablas FTS5/rtree no exponen rowid ordenable por SQL: se
            # ordena en Python por el contenido serializado (determinista).
            try:
                rows = con.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            except sqlite3.OperationalError:
                rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
            serial = sorted(
                json.dumps([str(v) for v in row], ensure_ascii=False)
                for row in rows
            )
            h = hashlib.sha256()
            h.update(json.dumps(serial, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            dump[table] = h.hexdigest()
        return dump
    finally:
        con.close()


def fingerprint_bundle(root: Path) -> dict[str, Any]:
    """Huella estable de un bundle construido (para probar reproducibilidad).

    Excluye a propósito `manifest.json` (lleva `generated_ts`) y los bytes de
    `catalog.db` (layout binario); en su lugar se vuelcan las tablas en orden.
    Todo lo demás (files/, previews/, licenses/, osm/, sources.json,
    ATTRIBUTION.md) entra por path+sha256 ordenados.
    """
    root = Path(root)
    top: list[dict[str, Any]] = []
    for name in ("sources.json", "ATTRIBUTION.md"):
        p = root / name
        if p.is_file():
            top.append({"path": name, "sha256": sha256_file(p),
                        "bytes": p.stat().st_size})
    return {
        "files": _stable_file_hashes(root, "files", "previews", "licenses", "osm"),
        "top": top,
        "db": _stable_db_dump(root / "catalog.db"),
    }


def diff_fingerprints(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Diferencias entre dos huellas (`[]` == reproducibles)."""
    diffs: list[str] = []
    for section in ("files", "top", "db"):
        ma = {e["path"]: e["sha256"] for e in a.get(section, [])} if isinstance(
            a.get(section), list) else dict(a.get(section, {}))
        mb = {e["path"]: e["sha256"] for e in b.get(section, [])} if isinstance(
            b.get(section), list) else dict(b.get(section, {}))
        for path in sorted(set(ma) | set(mb)):
            if path not in ma:
                diffs.append(f"solo en B: {path}")
            elif path not in mb:
                diffs.append(f"solo en A: {path}")
            elif ma[path] != mb[path]:
                diffs.append(f"distinto: {path}")
    return diffs


def verify_bundle(root: Path, *, require_files: bool = True) -> dict[str, Any]:
    """Verificación completa del bundle (CI + smoke test dentro de la imagen)."""
    root = Path(root)
    report: dict[str, Any] = {"root": str(root), "ok": True, "errors": [], "checks": {}}
    svc = AtlasService(root)
    if not svc.available():
        return {"root": str(root), "ok": False, "errors": ["sin catalog.db"], "checks": {}}
    v = svc.verify(require_files=require_files, check_hashes=True)
    report["checks"]["catalog_integrity"] = {
        "ok": v["ok"],
        "checked": v["checked_references"],
        "error_count": v["error_count"],
    }
    if not v["ok"]:
        report["ok"] = False
        report["errors"].extend(f"{e}" for e in v["errors"][:50])
    # manifest vs disco
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        report["ok"] = False
        report["errors"].append("falta manifest.json")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mismatches = []
        for entry in manifest.get("files", []):
            p = root / entry["path"]
            if not p.is_file():
                mismatches.append(f"ausente: {entry['path']}")
            elif sha256_file(p) != entry["sha256"]:
                mismatches.append(f"hash: {entry['path']}")
        report["checks"]["manifest_files"] = {
            "ok": not mismatches,
            "files": len(manifest.get("files", [])),
            "mismatches": mismatches[:50],
        }
        if mismatches:
            report["ok"] = False
            report["errors"].extend(mismatches[:50])
    # licencias y atribución presentes
    attr = root / "ATTRIBUTION.md"
    sources = root / "sources.json"
    report["checks"]["licenses_docs"] = {
        "attribution": attr.is_file(),
        "sources": sources.is_file(),
        "ok": attr.is_file() and sources.is_file(),
    }
    if not (attr.is_file() and sources.is_file()):
        report["ok"] = False
        report["errors"].append("faltan ATTRIBUTION.md o sources.json")
    # OSM con atribución ODbL
    osm = root / "osm"
    has_osm = any(osm.glob("*.osm.pbf")) if osm.is_dir() else False
    report["checks"]["osm"] = {"present": has_osm, "ok": has_osm}
    if not has_osm:
        report["ok"] = False
        report["errors"].append("falta el extracto OSM recortado")
    report["stats"] = svc.stats()
    return report
