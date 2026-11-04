"""Brief creativo, comprensión multimodal, revisión y entrega (M7, Worker 7).

Flujo: assets + prompt → `POST .../brief` (comprensión con evidencia + brief
borrador con supuestos visibles) → edición vía `PATCH .../brief` (cada cambio
crea una versión nueva; las decisiones del usuario prevalecen) → aprobación
humana `POST .../brief/{version}/approve` → producción → `POST .../brief/review`
sobre el render real → `GET .../brief/delivery` (paquete final).

Estado persistente en `studio.db` (tabla `creative_briefs`, append-only) y
`project_meta` (understanding, reviews, feedback): reanudable tras reinicio.
Eventos SSE: `brief` y `review`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from comfy_studio.creative import (
    build_brief,
    build_delivery_package,
    build_direction,
    build_understanding,
    review_output,
    validate_brief,
)
from comfy_studio.creative.brief import BRIEF_STATUSES, EDITABLE_FIELDS
from comfy_studio.creative.review import save_review
from comfy_studio.ml import is_real_backend
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    BriefCreateRequest,
    BriefFeedbackRequest,
    BriefPatchRequest,
    BriefReviewRequest,
    BriefUnderstandRequest,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _vector_store(request: Request) -> Any | None:
    return getattr(request.app.state, "vector_store", None)


def _ml_simulated() -> bool:
    """True cuando el pod corre backends ML simulados (sin pesos reales)."""
    try:
        return not is_real_backend()
    except Exception:  # noqa: BLE001
        return True


def _publish(request: Request, event_type: str, data: dict[str, Any]) -> None:
    broker = getattr(request.app.state, "broker", None)
    if broker is not None:
        broker.publish(event_type, data)


def _learned_preferences(store: ProjectStore, project_id: str) -> list[dict[str, Any]]:
    prefs = store.get_meta(project_id, "creative_feedback", [])
    return prefs if isinstance(prefs, list) else []


def register_routes(app: FastAPI) -> None:
    """Registra las rutas del flujo de brief creativa sobre `app`."""

    @app.post("/studio/api/projects/{project_id}/brief/understand")
    async def understand_project_endpoint(
        project_id: str,
        body: BriefUnderstandRequest,
        request: Request,
    ) -> dict[str, Any]:
        """(Re)construye la comprensión multimodal del proyecto con evidencia trazable."""
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        try:
            understanding = await run_in_threadpool(
                build_understanding,
                store,
                project_id,
                asset_ids=body.asset_ids or None,
                prompt=body.prompt,
                vector_store=_vector_store(request),
                ml_simulated=_ml_simulated(),
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.set_meta(project_id, "creative_understanding", understanding)
        _publish(
            request,
            "brief",
            {
                "project_id": project_id,
                "action": "understood",
                "assets": understanding["counts"]["assets"],
                "facts": understanding["counts"]["facts"],
                "duplicates": understanding["counts"]["duplicates"],
                "invalid": len(understanding["invalid"]),
            },
        )
        return redactor.redact_value({"project_id": project_id, "understanding": understanding})

    @app.post("/studio/api/projects/{project_id}/brief", status_code=201)
    async def create_brief_endpoint(
        project_id: str,
        body: BriefCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Crea un brief creativo nuevo (v1) desde prompt + assets del proyecto.

        El prompt puede ser ambiguo: se toman decisiones editoriales razonables y
        quedan visibles en `assumptions`; las restricciones explícitas quedan en
        `user_constraints` y prevalecen sobre las inferencias.
        """
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        try:
            understanding = await run_in_threadpool(
                build_understanding,
                store,
                project_id,
                asset_ids=body.asset_ids or None,
                prompt=body.prompt,
                vector_store=_vector_store(request),
                ml_simulated=_ml_simulated(),
            )
            editorial = await run_in_threadpool(
                build_direction, understanding.get("intent") or {}, understanding
            )
            brief = await run_in_threadpool(
                build_brief,
                project_id=project_id,
                prompt=body.prompt,
                evidence=understanding,
                editorial=editorial,
                overrides=body.overrides,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        brief["learned_preferences"] = _learned_preferences(store, project_id)
        saved = store.save_creative_brief(
            project_id,
            prompt=body.prompt,
            brief=brief,
            evidence=understanding,
            changes=[
                {
                    "action": "created",
                    "source": "system",
                    "at": _utc_now_iso(),
                    "note": "brief generado desde prompt + evidencia multimodal",
                }
            ],
            status=brief.get("status") or "draft",
        )
        store.set_meta(project_id, "creative_understanding", understanding)
        _publish(
            request,
            "brief",
            {
                "project_id": project_id,
                "action": "created",
                "brief_id": saved["id"],
                "version": saved["version"],
                "status": saved["status"],
                "piece_type": brief.get("piece_type"),
            },
        )
        return redactor.redact_value(saved)

    @app.get("/studio/api/projects/{project_id}/brief")
    async def get_active_brief_endpoint(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        latest = store.get_latest_creative_brief(project_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="El proyecto no tiene brief todavía")
        understanding = store.get_meta(project_id, "creative_understanding")
        payload = {
            "project_id": project_id,
            "brief": latest,
            "understanding": understanding if isinstance(understanding, dict) else None,
            "learned_preferences": _learned_preferences(store, project_id),
        }
        return redactor.redact_value(payload)

    @app.get("/studio/api/projects/{project_id}/briefs")
    async def list_briefs_endpoint(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        versions = store.list_creative_briefs(project_id)
        return redactor.redact_value(
            {
                "project_id": project_id,
                "count": len(versions),
                "briefs": [
                    {
                        "id": v["id"],
                        "version": v["version"],
                        "status": v["status"],
                        "prompt": v["prompt"],
                        "changes": v["changes"],
                        "created_at": v["created_at"],
                    }
                    for v in versions
                ],
            }
        )

    @app.get("/studio/api/projects/{project_id}/brief/delivery")
    async def get_delivery_package_endpoint(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        latest = store.get_latest_creative_brief(project_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="El proyecto no tiene brief todavía")
        try:
            package = await run_in_threadpool(
                build_delivery_package, store, project_id, latest
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return redactor.redact_value(package)

    @app.get("/studio/api/projects/{project_id}/brief/{version}")
    async def get_brief_version_endpoint(
        project_id: str,
        version: int,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        row = store.get_creative_brief(project_id, version)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Versión {version} no encontrada")
        return redactor.redact_value(row)

    @app.patch("/studio/api/projects/{project_id}/brief")
    async def patch_brief_endpoint(
        project_id: str,
        body: BriefPatchRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Aplica ediciones del usuario sobre el brief activo (nueva versión).

        Solo campos editables; cada cambio queda en `changes` con source=user.
        Las instrucciones explícitas del usuario prevalecen sobre inferencias.
        """
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        latest = store.get_latest_creative_brief(project_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="El proyecto no tiene brief todavía")
        brief = dict(latest["brief"])
        changes: list[dict[str, Any]] = []
        for key, value in body.changes.items():
            if key not in EDITABLE_FIELDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Campo no editable: {key!r}. Editables: {sorted(EDITABLE_FIELDS)}",
                )
            changes.append({
                "field": key,
                "from": brief.get(key),
                "to": value,
                "source": "user",
                "at": _utc_now_iso(),
            })
            brief[key] = value
        status = brief.get("status") if brief.get("status") in BRIEF_STATUSES else "draft"
        if "status" in body.changes and body.changes["status"] not in BRIEF_STATUSES:
            raise HTTPException(status_code=400, detail="status inválido")
        try:
            validate_brief(brief)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved = store.save_creative_brief(
            project_id,
            prompt=latest["prompt"],
            brief=brief,
            evidence=latest["evidence"],
            changes=changes,
            status=status,
        )
        _publish(
            request,
            "brief",
            {
                "project_id": project_id,
                "action": "updated",
                "brief_id": saved["id"],
                "version": saved["version"],
                "fields": [c["field"] for c in changes],
                "source": "user",
            },
        )
        return redactor.redact_value(saved)

    @app.post("/studio/api/projects/{project_id}/brief/{version}/approve")
    async def approve_brief_endpoint(
        project_id: str,
        version: int,
        request: Request,
    ) -> dict[str, Any]:
        """Aprobación humana del brief (decisión del usuario, persistida)."""
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        row = store.get_creative_brief(project_id, version)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Versión {version} no encontrada")
        brief = dict(row["brief"])
        brief["status"] = "approved"
        changes = list(row.get("changes") or []) + [
            {
                "field": "status",
                "from": row.get("status"),
                "to": "approved",
                "source": "user",
                "at": _utc_now_iso(),
            }
        ]
        saved = store.save_creative_brief(
            project_id,
            prompt=row["prompt"],
            brief=brief,
            evidence=row["evidence"],
            changes=changes,
            status="approved",
        )
        _publish(
            request,
            "brief",
            {
                "project_id": project_id,
                "action": "approved",
                "version": saved["version"],
                "approved_source_version": version,
            },
        )
        return redactor.redact_value(saved)

    @app.post("/studio/api/projects/{project_id}/brief/feedback")
    async def brief_feedback_endpoint(
        project_id: str,
        body: BriefFeedbackRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Registra feedback del usuario: queda en el brief y en preferencias del proyecto."""
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        latest = store.get_latest_creative_brief(project_id)
        entry = {
            "text": body.text.strip(),
            "applies_to": body.applies_to,
            "project_id": project_id,
            "created_at": _utc_now_iso(),
            "applied": False,
        }
        prefs = _learned_preferences(store, project_id)
        prefs.append(entry)
        store.set_meta(project_id, "creative_feedback", prefs)
        if latest is None:
            return redactor.redact_value(
                {"project_id": project_id, "stored": True, "brief_version": None, "entry": entry}
            )
        brief = dict(latest["brief"])
        feedback = list(brief.get("feedback") or [])
        feedback.append({**entry, "brief_version": latest["version"]})
        brief["feedback"] = feedback
        changes = list(latest.get("changes") or []) + [
            {
                "field": "feedback",
                "from": len(feedback) - 1,
                "to": len(feedback),
                "source": "user",
                "at": _utc_now_iso(),
            }
        ]
        saved = store.save_creative_brief(
            project_id,
            prompt=latest["prompt"],
            brief=brief,
            evidence=latest["evidence"],
            changes=changes,
            status=latest["status"],
        )
        _publish(
            request,
            "brief",
            {
                "project_id": project_id,
                "action": "feedback",
                "version": saved["version"],
            },
        )
        return redactor.redact_value({"project_id": project_id, "stored": True, "brief": saved})

    @app.post("/studio/api/projects/{project_id}/brief/review")
    async def review_output_endpoint(
        project_id: str,
        body: BriefReviewRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Revisa el render REAL de un asset contra el brief activo y guarda el informe."""
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        _require_project(store, project_id)
        latest = store.get_latest_creative_brief(project_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="El proyecto no tiene brief todavía")
        try:
            report = await run_in_threadpool(
                review_output,
                store,
                project_id,
                latest["brief"],
                body.asset_id.strip(),
            )
            await run_in_threadpool(save_review, store, project_id, body.asset_id.strip(), report)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _publish(
            request,
            "review",
            {
                "project_id": project_id,
                "asset_id": body.asset_id,
                "passed": report["summary"]["passed"],
                "errors": report["summary"]["errors"],
                "warnings": report["summary"]["warnings"],
            },
        )
        return redactor.redact_value(report)
