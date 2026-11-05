"""Rutas HTTP del atlas bajo /studio/api/atlas (misma auth edge existente).

Seguridad: los archivos se sirven SOLO desde rutas resueltas dentro de
`files/`/`previews/` del atlas (path traversal rechazado); los ids se validan
contra el catálogo; nada descarga de terceros en runtime.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from comfy_studio.atlas.service import AtlasService, AtlasUnavailableError


def _atlas(request: Request) -> AtlasService:
    svc = getattr(request.app.state, "atlas", None)
    if svc is None or not svc.available():
        raise HTTPException(
            status_code=503,
            detail="Atlas no disponible en este pod (falta el catálogo en STUDIO_ATLAS_DIR)",
        )
    return svc


def _safe_file(svc: AtlasService, base: str, rel: str):
    root = (svc.root / base).resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="ruta fuera del atlas") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="archivo no encontrado en el atlas")
    return path


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    try:
        parts = [float(x) for x in raw.split(",")]
        if len(parts) != 4:
            raise ValueError
        return (parts[0], parts[1], parts[2], parts[3])  # min_lat,max_lat,min_lon,max_lon
    except ValueError:
        raise HTTPException(
            status_code=422, detail="bbox inválido: 'min_lat,max_lat,min_lon,max_lon'"
        ) from None


def register_routes(app: FastAPI) -> None:
    """Registra las rutas del atlas directamente sobre `app` (mismo route_class)."""

    @app.get("/studio/api/atlas/health")
    async def atlas_health(request: Request) -> dict[str, Any]:
        svc = getattr(request.app.state, "atlas", None)
        if svc is None or not svc.available():
            return {"available": False, "root": str(svc.root) if svc else None}
        st = svc.stats()
        return {
            "available": True,
            "atlas_version": svc.atlas_version(),
            "root": str(svc.root),
            "places": st["places"],
            "places_with_usable_views": st["places_with_usable_views"],
            "references_usable": st["references_usable"],
            "views_usable": st["views_usable"],
        }

    @app.get("/studio/api/atlas/stats")
    async def atlas_stats(request: Request) -> dict[str, Any]:
        svc = _atlas(request)
        return {"atlas_version": svc.atlas_version(), **svc.stats()}

    @app.get("/studio/api/atlas/places")
    async def atlas_places(
        request: Request,
        q: str | None = Query(default=None),
        municipality: str | None = Query(default=None),
        environment: str | None = Query(default=None),
        min_views: int = Query(default=0, ge=0),
        bbox: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        svc = _atlas(request)
        places = svc.search_places(
            q=q,
            municipality=municipality,
            environment=environment,
            bbox=_parse_bbox(bbox),
            min_views=min_views,
            limit=limit,
            offset=offset,
        )
        return {"count": len(places), "places": places}

    @app.get("/studio/api/atlas/resolve")
    async def atlas_resolve(
        request: Request,
        q: str = Query(...),
        municipality: str | None = Query(default=None),
    ) -> dict[str, Any]:
        svc = _atlas(request)
        try:
            return svc.resolve_place(q, municipality=municipality)
        except AtlasUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/studio/api/atlas/places/{place_id}")
    async def atlas_place_detail(request: Request, place_id: str) -> dict[str, Any]:
        svc = _atlas(request)
        try:
            return svc.place_detail(place_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Lugar no encontrado") from None

    @app.get("/studio/api/atlas/places/{place_id}/coverage")
    async def atlas_place_coverage(request: Request, place_id: str) -> dict[str, Any]:
        svc = _atlas(request)
        cov = svc.store.get_coverage(place_id)
        if cov is None:
            raise HTTPException(status_code=404, detail="Lugar sin cobertura registrada")
        return cov.to_dict()

    @app.get("/studio/api/atlas/places/{place_id}/views")
    async def atlas_place_views(request: Request, place_id: str) -> dict[str, Any]:
        svc = _atlas(request)
        if svc.store.get_place(place_id) is None:
            raise HTTPException(status_code=404, detail="Lugar no encontrado")
        sel = svc.select_view(place_id)
        return {"place_id": place_id, **sel}

    @app.get("/studio/api/atlas/search")
    async def atlas_search(
        request: Request,
        q: str = Query(...),
        limit: int = Query(default=30, ge=1, le=200),
    ) -> dict[str, Any]:
        svc = _atlas(request)
        refs = svc.store.search_references_fts(q, limit=limit)
        places = svc.search_places(q=q, limit=limit)
        return {
            "places": places,
            "references": [r.to_dict() for r in refs],
        }

    @app.post("/studio/api/atlas/select-view")
    async def atlas_select_view(
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        svc = _atlas(request)
        project_id = str(payload.get("project_id") or "") or None
        place_id = str(payload.get("place_id") or "")
        if not place_id or svc.store.get_place(place_id) is None:
            raise HTTPException(status_code=404, detail="place_id desconocido")
        lat = payload.get("lat")
        lon = payload.get("lon")
        sel = svc.select_view(
            place_id,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            heading_deg=float(payload["heading"]) if payload.get("heading") is not None else None,
            fov_deg=float(payload["fov"]) if payload.get("fov") is not None else None,
        )
        if sel.get("status") != "ok":
            return sel
        svc.set_selection(place_id, sel["view"]["view_id"], note=str(payload.get("note") or ""),
                          project_id=project_id)
        return sel

    @app.get("/studio/api/atlas/selection")
    async def atlas_selection(
        request: Request, project_id: str | None = Query(default=None)
    ) -> dict[str, Any]:
        svc = _atlas(request)
        return svc.get_selection(project_id)

    @app.get("/studio/api/atlas/references/{ref_id}")
    async def atlas_reference(request: Request, ref_id: str) -> dict[str, Any]:
        svc = _atlas(request)
        ref = svc.store.get_reference(ref_id)
        if ref is None:
            raise HTTPException(status_code=404, detail="Referencia no encontrada")
        d = ref.to_dict()
        d["preview"] = f"/studio/api/atlas/references/{ref_id}/preview"
        d["file"] = f"/studio/api/atlas/references/{ref_id}/file"
        d["license"] = svc.store.get_license(ref_id)
        return d

    @app.get("/studio/api/atlas/references/{ref_id}/file")
    async def atlas_reference_file(request: Request, ref_id: str) -> Response:
        svc = _atlas(request)
        ref = svc.store.get_reference(ref_id)
        if ref is None:
            raise HTTPException(status_code=404, detail="Referencia no encontrada")
        path = _safe_file(svc, "files", ref.file_path)
        return Response(
            content=path.read_bytes(),
            media_type=ref.mime or "application/octet-stream",
            headers={
                "Content-Disposition": f'inline; filename="{ref_id}{path.suffix}"',
                "X-Atlas-Sha256": ref.sha256,
            },
        )

    @app.get("/studio/api/atlas/references/{ref_id}/preview")
    async def atlas_reference_preview(request: Request, ref_id: str) -> Response:
        svc = _atlas(request)
        if svc.store.get_reference(ref_id) is None:
            raise HTTPException(status_code=404, detail="Referencia no encontrada")
        try:
            path = _safe_file(svc, "previews", f"{ref_id}.jpg")
        except HTTPException:
            raise HTTPException(
                status_code=404, detail="preview no disponible para esta referencia"
            ) from None
        return Response(content=path.read_bytes(), media_type="image/jpeg")

    @app.get("/studio/api/atlas/references/{ref_id}/license")
    async def atlas_reference_license(request: Request, ref_id: str) -> Response:
        svc = _atlas(request)
        lic = svc.store.get_license(ref_id)
        if lic is None:
            raise HTTPException(status_code=404, detail="Licencia no registrada")
        text = (
            f"licencia: {lic['license']}\n"
            f"url: {lic.get('license_url') or 'n/d'}\n"
            f"atribución: {lic.get('attribution') or 'n/d'}\n"
        )
        return Response(content=text, media_type="text/plain; charset=utf-8")

    @app.get("/studio/api/atlas/map.svg")
    async def atlas_map(request: Request) -> Response:
        svc = _atlas(request)
        return Response(content=svc.map_svg(), media_type="image/svg+xml")

    @app.get("/studio/api/atlas/verify")
    async def atlas_verify(
        request: Request,
        quick: bool = Query(default=False, description="sin recomputar hashes"),
    ) -> dict[str, Any]:
        svc = _atlas(request)
        return svc.verify(require_files=True, check_hashes=not quick)
