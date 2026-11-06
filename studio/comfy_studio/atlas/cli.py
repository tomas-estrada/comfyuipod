"""CLI portable del atlas: consultar, verificar, empaquetar y probar desde terminal.

Uso (macOS/Windows/Linux, sin helpers exclusivos de un sistema; los motores CUDA
corren en el pod Linux, no se prometen en clientes):

    python -m comfy_studio.atlas stats [--root DIR]
    python -m comfy_studio.atlas search "puente" [--municipality Medellín]
    python -m comfy_studio.atlas place <place_id>
    python -m comfy_studio.atlas select-view <place_id> [--lat .. --lon .. --heading ..]
    python -m comfy_studio.atlas prepare-shot [--place ..] [--view-id ..] [--prompt ..]
    python -m comfy_studio.atlas verify [--root DIR] [--no-files] [--no-hashes]
    python -m comfy_studio.atlas manifest [--root DIR]
    python -m comfy_studio.atlas map [--root DIR] [--out map.svg]
    python -m comfy_studio.atlas acquire [--root DIR] [--source commons|osm|geomedellin|all]
                                          [--max-refs N] [--dry-run]
    python -m comfy_studio.atlas package --root DIR --dest DIR [--max-shipped 620]
    python -m comfy_studio.atlas verify-bundle [--root DIR] [--no-files]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from comfy_studio.atlas.service import AtlasService, AtlasUnavailableError


def _svc(args: argparse.Namespace) -> AtlasService:
    return AtlasService(args.root) if getattr(args, "root", None) else AtlasService()


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_stats(args: argparse.Namespace) -> int:
    _print(_svc(args).stats())
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    svc = _svc(args)
    out = svc.search_places(
        q=args.q,
        municipality=args.municipality,
        environment=args.environment,
        min_views=args.min_views,
        limit=args.limit,
    )
    _print({"count": len(out), "places": out})
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    _print(_svc(args).resolve_place(args.query, municipality=args.municipality))
    return 0


def cmd_place(args: argparse.Namespace) -> int:
    _print(_svc(args).place_detail(args.place_id))
    return 0


def cmd_select_view(args: argparse.Namespace) -> int:
    _print(
        _svc(args).select_view(
            args.place_id,
            lat=args.lat,
            lon=args.lon,
            heading_deg=args.heading,
            fov_deg=args.fov,
        )
    )
    return 0


def cmd_prepare_shot(args: argparse.Namespace) -> int:
    _print(
        _svc(args).prepare_shot(
            place=args.place,
            place_id=args.place_id,
            view_id=args.view_id,
            lat=args.lat,
            lon=args.lon,
            heading_deg=args.heading,
            fov_deg=args.fov,
            prompt=args.prompt or "",
            verify_file=not args.no_verify,
        )
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    report = _svc(args).verify(
        require_files=not args.no_files, check_hashes=not args.no_hashes
    )
    _print(report)
    return 0 if report["ok"] else 1


def cmd_manifest(args: argparse.Namespace) -> int:
    svc = _svc(args)
    path = svc.write_manifest()
    print(f"manifest escrito en {path}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    svc = _svc(args)
    svg = svc.map_svg()
    if args.out:
        Path(args.out).write_text(svg, encoding="utf-8")
        print(f"mapa escrito en {args.out}")
    else:
        print(svg)
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    from comfy_studio.atlas.acquire.pipeline import run_acquisition

    result = run_acquisition(
        root=Path(args.root) if args.root else None,
        sources=args.source,
        max_refs=args.max_refs,
        dry_run=args.dry_run,
    )
    _print(result)
    return 0 if result.get("ok", False) else 1


def cmd_package(args: argparse.Namespace) -> int:
    from comfy_studio.atlas.packaging import build_bundle

    src = Path(args.root) if args.root else None
    dest = Path(args.dest)
    stats = build_bundle(src, dest, max_shipped=args.max_shipped)
    print(f"bundle construido en {dest}: {json.dumps(stats, default=str)}")
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    from comfy_studio.atlas.packaging import verify_bundle

    root = Path(args.root) if args.root else None
    report = verify_bundle(root, require_files=not args.no_files)
    _print(report)
    return 0 if report["ok"] else 1


def cmd_serve_check(args: argparse.Namespace) -> int:
    """Diagnóstico del atlas dentro del contenedor (sin egress a las fuentes)."""
    svc = _svc(args)
    if not svc.available():
        _print({"ok": False, "error": f"sin catálogo en {svc.db_path}"})
        return 1
    stats = svc.stats()
    sample = svc.search_places(min_views=1, limit=5)
    shot = svc.prepare_shot(place_id=sample[0]["place_id"]) if sample else {"status": "empty"}
    report = {
        "ok": shot.get("status") == "ok",
        "atlas_version": svc.atlas_version(),
        "root": str(svc.root),
        "stats": stats,
        "sample_shot_status": shot.get("status"),
        "sample_place": sample[0]["name"] if sample else None,
        "offline": True,
    }
    _print(report)
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m comfy_studio.atlas",
        description="CLI del atlas georreferenciado del Valle de Aburrá",
    )
    parser.add_argument(
        "--root", default=None,
        help="raíz del atlas (default: $STUDIO_ATLAS_DIR o /opt/studio-atlas)",
    )
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS para no pisar el --root global cuando va antes del subcomando.
    common.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    def sub_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text)

    p = sub_cmd("stats", "inventario medido del atlas")
    p.set_defaults(func=cmd_stats)

    p = sub_cmd("search", "busca lugares por texto/municipio/entorno")
    p.add_argument("q", nargs="?", default=None)
    p.add_argument("--municipality")
    p.add_argument("--environment")
    p.add_argument("--min-views", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub_cmd("resolve", "resuelve un nombre a lugar (sin sustituir en silencio)")
    p.add_argument("query")
    p.add_argument("--municipality")
    p.set_defaults(func=cmd_resolve)

    p = sub_cmd("place", "detalle de un lugar (cobertura, vistas, escenarios)")
    p.add_argument("place_id")
    p.set_defaults(func=cmd_place)

    p = sub_cmd("select-view", "elige vista/cámara para un lugar")
    p.add_argument("place_id")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--heading", type=float, dest="heading")
    p.add_argument("--fov", type=float)
    p.set_defaults(func=cmd_select_view)

    p = sub_cmd("prepare-shot", "prepara un plano con fondo local verificado")
    p.add_argument("--place")
    p.add_argument("--place-id")
    p.add_argument("--view-id")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--heading", type=float, dest="heading")
    p.add_argument("--fov", type=float)
    p.add_argument("--prompt", default="")
    p.add_argument("--no-verify", action="store_true")
    p.set_defaults(func=cmd_prepare_shot)

    p = sub_cmd(
        "verify",
        "integridad del catálogo y archivos (bloquea promoción si falla)",
    )
    p.add_argument(
        "--no-files", action="store_true", help="no exigir bytes de fotos (solo metadatos)"
    )
    p.add_argument("--no-hashes", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub_cmd("manifest", "escribe manifest.json con sha256 de todo el bundle")
    p.set_defaults(func=cmd_manifest)

    p = sub_cmd("map", "renderiza el mapa SVG local (datos propios)")
    p.add_argument("--out")
    p.set_defaults(func=cmd_map)

    p = sub_cmd("acquire", "adquisición reanudable desde fuentes gratuitas")
    p.add_argument("--source", default="all", help="commons|osm|geomedellin|all")
    p.add_argument("--max-refs", type=int, default=650)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_acquire)

    p = sub_cmd("package", "cura y empaqueta el bundle publicable (atlas/)")
    p.add_argument("--dest", required=True)
    p.add_argument("--max-shipped", type=int, default=620)
    p.set_defaults(func=cmd_package)

    p = sub_cmd(
        "verify-bundle",
        "verificación completa del bundle (bloquea promoción si falla)",
    )
    p.add_argument("--no-files", action="store_true")
    p.set_defaults(func=cmd_verify_bundle)

    p = sub_cmd("serve-check", "diagnóstico del atlas dentro del pod (offline)")
    p.set_defaults(func=cmd_serve_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AtlasUnavailableError as exc:
        print(f"atlas no disponible: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"no encontrado: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
