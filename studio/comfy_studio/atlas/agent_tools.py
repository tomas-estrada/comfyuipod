"""Herramientas del agente LLM para operar el atlas (tool-belt + MCP).

Equivalentes a: listar/buscar lugares, resolver identidad, consultar cobertura
y vistas, seleccionar cámara, preparar un plano y validar fidelidad. Las de
sólo lectura son `parallel_safe=True`; `atlas_prepare_shot` escribe la selección
activa (fichero `selection.json` del atlas) y NO es parallel-safe.

La selección/ubicación explícita nunca se sustituye en silencio: si el prompt
dice un lugar, se respeta; si hay ambigüedad se devuelve la lista de candidatos.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from comfy_studio.atlas import default_atlas_root
from comfy_studio.atlas.service import AtlasService, AtlasUnavailableError

_SHARED: dict[str, AtlasService] = {}
_SHARED_LOCK = threading.Lock()


def shared_atlas_service() -> AtlasService:
    """Servicio compartido (mismo raíz que el servidor: $STUDIO_ATLAS_DIR)."""
    root = str(default_atlas_root())
    with _SHARED_LOCK:
        svc = _SHARED.get(root)
        if svc is None:
            svc = AtlasService(root)
            _SHARED[root] = svc
        return svc


def reset_shared_atlas_service() -> None:
    with _SHARED_LOCK:
        _SHARED.clear()


def _svc() -> AtlasService:
    return shared_atlas_service()


class AtlasStatsArgs(BaseModel):
    pass


class AtlasSearchPlacesArgs(BaseModel):
    q: str | None = Field(default=None, description="texto libre (nombre/alias)")
    municipality: str | None = None
    environment: str | None = Field(default=None, description="bridge|road_corridor|plaza|...")
    min_views: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=200)


class AtlasResolvePlaceArgs(BaseModel):
    query: str
    municipality: str | None = None


class AtlasPlaceDetailArgs(BaseModel):
    place_id: str


class AtlasSelectViewArgs(BaseModel):
    place_id: str
    lat: float | None = None
    lon: float | None = None
    heading: float | None = Field(default=None, description="rumbo pedido en grados (opcional)")
    fov: float | None = None


class AtlasPrepareShotArgs(BaseModel):
    place: str | None = Field(default=None, description="nombre del lugar (si el usuario lo dio)")
    place_id: str | None = None
    view_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    heading: float | None = None
    fov: float | None = None
    prompt: str = Field(default="", description="prompt del usuario para este plano")


class AtlasComposeShotArgs(BaseModel):
    place: str | None = Field(
        default=None, description="lugar explícito (prevalece sobre la selección)"
    )
    place_id: str | None = None
    view_id: str | None = None
    prompt: str = Field(default="", description="prompt del plano (queda en la producción)")
    foreground_asset_id: str | None = Field(
        default=None, description="asset de proyecto con alfa para componer sobre el fondo"
    )
    scale: float = Field(default=0.35, gt=0.0, le=1.0)


class AtlasVerifyArgs(BaseModel):
    quick: bool = Field(default=False, description="true = sin recomputar hashes sha256")


async def handle_atlas_compose_shot(args: AtlasComposeShotArgs, context: Any) -> dict[str, Any]:
    """Composición CPU real sobre una fotografía del atlas (preserva el fondo).

    prompt -> lugar -> vista -> fondo local verificado (sha256) -> composición
    PIL con alfa -> archivo de salida en outputs del proyecto + producción
    registrada en el atlas con procedencia (ref, hash, licencia).
    """
    try:
        svc = _svc()
        if not svc.available():
            return {"ok": False, "error": "atlas no disponible"}
        shot = svc.prepare_shot(
            place=args.place,
            place_id=args.place_id,
            view_id=args.view_id,
            prompt=args.prompt,
            verify_file=True,
        )
        if shot.get("status") != "ok":
            return {"ok": False, **shot}
        project_id = getattr(context, "project_id", "default") or "default"
        store = getattr(context, "store", None)
        cfg = getattr(context, "config", None)
        fg_path = None
        if args.foreground_asset_id and store is not None:
            asset = store.get_asset(project_id, args.foreground_asset_id)
            fg_path = store.get_asset_path(project_id, args.foreground_asset_id)
            if asset is None or fg_path is None:
                return {"ok": False, "status": "foreground_missing",
                        "error": f"asset {args.foreground_asset_id!r} no existe en el proyecto"}
        data_dir = Path(cfg.studio_data_dir) if cfg is not None else svc.root
        out_path = (
            data_dir / "projects" / project_id / "outputs"
            / f"comp_{shot['ref_id'][:24]}.png"
        )
        comp = svc.compose_over_background(
            background_abs=shot["background_abs"],
            out_path=str(out_path),
            foreground_abs=fg_path,
            scale=args.scale,
        )
        asset = None
        if store is not None:
            out_bytes = Path(comp["out_path"]).read_bytes()
            asset = store.save_upload(
                project_id,
                filename=Path(comp["out_path"]).name,
                content_type="image/png",
                data=out_bytes,
                max_upload_bytes=512 * 1024 * 1024,
                category="outputs",
                metadata={
                    "atlas": {
                        "place_id": shot["place_id"],
                        "view_id": shot["view_id"],
                        "ref_id": shot["ref_id"],
                        "background_sha256": shot["reference"]["sha256"],
                        "license": shot["reference"]["license"],
                        "attribution": shot["reference"]["attribution"],
                        "atlas_version": shot["atlas_version"],
                    },
                    "composition": comp,
                },
            )
        # registrar la producción en el atlas (recuperable)
        from comfy_studio.atlas.models import ProductionRecord

        svc.store.record_production(ProductionRecord(
            prod_id=f"prod_comp_{shot['ref_id'][:16]}",
            atlas_version=shot["atlas_version"],
            place_id=shot["place_id"],
            view_id=shot["view_id"],
            camera=shot["camera"],
            params={
                "prompt": args.prompt[:500],
                "foreground_asset_id": args.foreground_asset_id,
                "mode": "compose_over_photo_cpu",
            },
            result={"output_asset_id": (asset or {}).get("id"), "composition": comp},
        ))
        return {
            "ok": True,
            "status": "ok",
            "place_id": shot["place_id"],
            "view_id": shot["view_id"],
            "ref_id": shot["ref_id"],
            "background_sha256": shot["reference"]["sha256"],
            "license": shot["reference"]["license"],
            "attribution": shot["reference"]["attribution"],
            "output_asset_id": (asset or {}).get("id"),
            "composition": comp,
        }
    except AtlasUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}


async def handle_atlas_stats(args: AtlasStatsArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        if not svc.available():
            return {"ok": False, "error": "atlas no disponible (sin catálogo en STUDIO_ATLAS_DIR)"}
        return {"ok": True, "atlas_version": svc.atlas_version(), **svc.stats()}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def handle_atlas_search_places(args: AtlasSearchPlacesArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        places = svc.search_places(
            q=args.q,
            municipality=args.municipality,
            environment=args.environment,
            min_views=args.min_views,
            limit=args.limit,
        )
        return {"ok": True, "count": len(places), "places": places}
    except AtlasUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def handle_atlas_resolve_place(args: AtlasResolvePlaceArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        res = svc.resolve_place(args.query, municipality=args.municipality)
        res["ok"] = res["status"] == "confirmed"
        return res
    except AtlasUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def handle_atlas_place_detail(args: AtlasPlaceDetailArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        return {"ok": True, **svc.place_detail(args.place_id)}
    except KeyError as exc:
        return {"ok": False, "error": f"lugar desconocido: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def handle_atlas_select_view(args: AtlasSelectViewArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        sel = svc.select_view(
            args.place_id,
            lat=args.lat,
            lon=args.lon,
            heading_deg=args.heading,
            fov_deg=args.fov,
        )
        sel["ok"] = sel.get("status") == "ok"
        return sel
    except KeyError as exc:
        return {"ok": False, "error": f"lugar desconocido: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def handle_atlas_prepare_shot(args: AtlasPrepareShotArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        shot = svc.prepare_shot(
            place=args.place,
            place_id=args.place_id,
            view_id=args.view_id,
            lat=args.lat,
            lon=args.lon,
            heading_deg=args.heading,
            fov_deg=args.fov,
            prompt=args.prompt,
            verify_file=True,
        )
        shot["ok"] = shot.get("status") == "ok"
        if shot.get("status") == "ok":
            # contexto estructurado para el planner (state.metadata["atlas"])
            # Lleva procedencia del fondo para que el grafo final cite la
            # fuente real aunque el catálogo cambie entre plan y ejecución
            # (el adapter revalida hash y ruta al ejecutar; nunca confía).
            shot["atlas_context"] = {
                "place_id": shot["place_id"],
                "view_id": shot["view_id"],
                "ref_id": shot["ref_id"],
                "background_path": shot["background_path"],
                "background_sha256": shot["reference"]["sha256"],
                "license": shot["reference"]["license"],
                "attribution": shot["reference"]["attribution"],
                "capture_date": shot["reference"]["capture_date"],
                "atlas_version": shot["atlas_version"],
                "camera": shot["camera"],
                "limitations": shot["limitations"],
            }
        return shot
    except AtlasUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


async def handle_atlas_verify(args: AtlasVerifyArgs, context: Any) -> dict[str, Any]:
    try:
        svc = _svc()
        report = svc.verify(require_files=True, check_hashes=not args.quick)
        report["ok"] = report["ok"]
        return report
    except AtlasUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


#: (nombre, descripción, args_model, handler, parallel_safe)
ATLAS_TOOL_SPECS: tuple[tuple[str, str, type[BaseModel], Any, bool], ...] = (
    (
        "atlas_stats",
        "Inventario medido del atlas georreferenciado del Valle de Aburrá: lugares, "
        "lugares con vistas utilizables, referencias por municipio/licencia/fecha.",
        AtlasStatsArgs,
        handle_atlas_stats,
        True,
    ),
    (
        "atlas_search_places",
        "Lista/busca lugares del atlas por texto, municipio, tipo de entorno y cobertura "
        "mínima de vistas. Devuelve place_id, coordenadas, municipios y cobertura.",
        AtlasSearchPlacesArgs,
        handle_atlas_search_places,
        True,
    ),
    (
        "atlas_resolve_place",
        "Resuelve un nombre de lugar a su identidad georreferenciada. NUNCA sustituye en "
        "silencio: si hay ambigüedad devuelve status=ambiguous con los candidatos.",
        AtlasResolvePlaceArgs,
        handle_atlas_resolve_place,
        True,
    ),
    (
        "atlas_place_detail",
        "Detalle de un lugar: cobertura medida, vistas/cámaras disponibles (con preview, "
        "licencia y fecha de captura) y escenarios.",
        AtlasPlaceDetailArgs,
        handle_atlas_place_detail,
        True,
    ),
    (
        "atlas_select_view",
        "Selecciona la vista/cámara respaldada para un lugar (y opcionalmente una posición "
        "o rumbo pedidos). Si la cámara pedida no está respaldada devuelve limitaciones "
        "tipadas y las vistas disponibles del mismo lugar.",
        AtlasSelectViewArgs,
        handle_atlas_select_view,
        True,
    ),
    (
        "atlas_prepare_shot",
        "Prepara un plano real del atlas: resuelve el lugar (explícito o selección activa o "
        "mejor cobertura), selecciona vista, verifica el archivo local (sha256) y devuelve "
        "background_path + contexto de cámara + atribución. Registra la selección activa.",
        AtlasPrepareShotArgs,
        handle_atlas_prepare_shot,
        False,
    ),
    (
        "atlas_compose_shot",
        "Composición CPU REAL sobre una fotografía del atlas: resuelve lugar/vista, "
        "verifica el fondo (sha256), pega un asset con alfa (opcional) preservando los "
        "píxeles del fondo fuera de la región del asset, guarda la salida en outputs "
        "del proyecto y registra la producción con procedencia en el atlas.",
        AtlasComposeShotArgs,
        handle_atlas_compose_shot,
        False,
    ),
    (
        "atlas_verify",
        "Verifica la integridad del atlas local: archivos presentes, hashes, dimensiones, "
        "licencias registradas (bloquea promoción si falla). quick=true omite hashes.",
        AtlasVerifyArgs,
        handle_atlas_verify,
        True,
    ),
)


def register_atlas_tools(belt: Any) -> None:
    """Registra las herramientas del atlas en un ToolBelt existente."""
    from comfy_studio.agent.tools import ToolSpec

    for name, description, args_model, handler, parallel_safe in ATLAS_TOOL_SPECS:
        belt.register(
            ToolSpec(
                name=name,
                description=description,
                args_model=args_model,
                handler=handler,
                parallel_safe=parallel_safe,
            )
        )
