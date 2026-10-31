"""Servidor MCP (Model Context Protocol) de Comfy Studio — M3-C.

Implementación mínima y autocontenida de JSON-RPC 2.0 sobre HTTP
(streamable-HTTP con respuestas `application/json`), sin dependencias nuevas:

- `initialize` / `notifications/initialized` / `ping`
- `tools/list` — expone un subconjunto seguro del tool-belt del agente más
  `list_projects` y `list_assets`.
- `tools/call` — ejecuta la herramienta contra el proyecto indicado
  (`project_id` opcional; por defecto el proyecto `default` o el primero).

Seguridad:
- Montado bajo `/studio/api/mcp`, protegido por el Basic Auth de nginx y, si
  `STUDIO_API_TOKEN` está definido, por `Authorization: Bearer <token>`.
- Todo resultado pasa por `SecretRedactor` antes de serializarse.
- Solo loopback (127.0.0.1:18190) + proxy nginx; nunca se expone un puerto nuevo.

Métodos MCP soportados (protocolVersion 2025-03-26). Los eventos SSE
servidor→cliente y el sampling no se implementan en esta versión.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .agent.tools import ToolBelt, ToolContext
from .comfyui_client import ComfyUIClient
from .config import SecretRedactor, StudioConfig
from .progress import ProgressBroker
from .projects import ProjectStore

logger = logging.getLogger("comfy_studio.mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_NAME = "comfy-studio-mcp"
MCP_SERVER_VERSION = "0.1.0"

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603

# Herramientas del tool-belt expuestas vía MCP (subconjunto seguro y headless).
BELT_TOOLS_EXPOSED: tuple[str, ...] = (
    "list_templates",
    "search_assets",
    "enqueue_render",
    "job_status",
    "export_edl",
    "system_status",
    "tag_asset",
)

_EXTRA_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": "Lista los proyectos de Comfy Studio con su id, nombre y aspect ratio.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Opcional; valida que el proyecto exista (no filtra).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_assets",
        "description": (
            "Lista los assets de un proyecto (uploads, outputs, keyframes, voice), "
            "filtrable por kind y category."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Proyecto (default: activo)."},
                "kind": {"type": "string", "description": "Filtro por tipo de medio."},
                "category": {"type": "string", "description": "Filtro por categoría."},
            },
            "required": [],
        },
    },
    {
        "name": "plan_production",
        "description": (
            "Planifica una producción (idea -> secuencias -> shots) y la deja "
            "ESPERANDO APROBACIÓN HUMANA. Es un borrador barato: NO gasta GPU y NO "
            "ejecuta nada. Solo un humano aprueba el gasto pesado desde el panel; "
            "el agente no puede aprobar."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "idea": {"type": "string", "description": "La idea del reel a producir."},
                "cost_cap_usd": {
                    "type": "number",
                    "description": "Tope de coste total en USD a repartir por shot (opcional).",
                },
            },
            "required": ["idea"],
        },
    },
    {
        "name": "get_plan",
        "description": "Devuelve el plan serializado de una producción por su production_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Id de la producción."},
            },
            "required": ["production_id"],
        },
    },
    {
        "name": "production_status",
        "description": (
            "Estado de una producción: fase, progreso de shots y ruta del reel final."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Id de la producción."},
            },
            "required": ["production_id"],
        },
    },
]

#: tools de producción expuestas al agente vía MCP. `approve_plan` está
#: DELIBERADAMENTE AUSENTE: es la puerta de gasto humano (M6-AGT-001), accionada
#: solo por el usuario en la API pública. Nunca se añade aquí.
PRODUCTION_TOOLS_EXPOSED: frozenset[str] = frozenset(
    {"plan_production", "get_plan", "production_status"}
)


@dataclass(slots=True)
class McpContext:
    """Dependencias inyectadas al router MCP."""

    store: ProjectStore
    comfyui_client: ComfyUIClient
    broker: ProgressBroker
    config: StudioConfig
    tool_belt: ToolBelt
    redactor: SecretRedactor
    #: servicio de producción (orquestación M6). Opcional: si no se inyecta, las
    #: tools de producción no se ofrecen. NUNCA expone `approve_plan` (la puerta
    #: de gasto humano vive solo en la API pública — M6-AGT-001).
    production_service: Any | None = None


class McpToolError(Exception):
    """Error de ejecución de una herramienta MCP (isError=true, no error JSON-RPC)."""


def _resolve_project_id(store: ProjectStore, requested: str | None) -> str:
    if requested and str(requested).strip():
        pid = str(requested).strip()
        if store.get_project(pid) is None:
            raise McpToolError(f"Proyecto no encontrado: {pid!r}")
        return pid
    if store.get_project("default") is not None:
        return "default"
    projects = store.list_projects()
    if not projects:
        raise McpToolError("No hay ningún proyecto; crea uno primero (POST /studio/api/projects).")
    return str(projects[0]["id"])


def build_mcp_tools(belt: ToolBelt) -> list[dict[str, Any]]:
    """Construye el catálogo MCP (tools/list) desde el tool-belt + herramientas extra."""
    tools: list[dict[str, Any]] = list(_EXTRA_TOOLS_SCHEMA)
    for name in BELT_TOOLS_EXPOSED:
        spec = belt.get(name)
        if spec is None:
            continue
        raw_schema = spec.args_model.model_json_schema()
        schema: dict[str, Any] = {
            "type": "object",
            "properties": dict(raw_schema.get("properties") or {}),
            "required": list(raw_schema.get("required") or []),
        }
        if "$defs" in raw_schema:
            schema["$defs"] = raw_schema["$defs"]
        # project_id opcional en todas las herramientas del belt vía MCP
        schema["properties"].setdefault(
            "project_id",
            {"type": "string", "description": "Proyecto destino (default: proyecto activo)."},
        )
        tools.append(
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": schema,
            }
        )
    return tools


async def _call_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    ctx: McpContext,
) -> dict[str, Any]:
    args = dict(arguments or {})

    if name == "list_projects":
        requested = args.pop("project_id", None)
        if args:
            raise McpToolError(f"Argumentos no soportados por list_projects: {sorted(args)}")
        if requested and str(requested).strip():
            await run_in_threadpool(_resolve_project_id, ctx.store, requested)  # valida existencia
        projects = await run_in_threadpool(ctx.store.list_projects)
        return {"projects": projects}

    # tools de producción (orquestación M6). GOBERNANZA: solo las 3 de lectura/
    # planificación; `approve_plan` NUNCA llega aquí (puerta de gasto humano,
    # M6-AGT-001). Un intento de llamarla cae en "no disponible".
    if name in PRODUCTION_TOOLS_EXPOSED:
        service = ctx.production_service
        if service is None:
            raise McpToolError("El servicio de producción no está disponible.")
        try:
            if name == "plan_production":
                idea = str(args.get("idea") or "").strip()
                if not idea:
                    raise McpToolError("plan_production requiere 'idea'.")
                cost_cap = float(args.get("cost_cap_usd", 0.0) or 0.0)
                return await run_in_threadpool(
                    service.plan_production, idea, cost_cap_usd=cost_cap
                )
            production_id = str(args.get("production_id") or "").strip()
            if not production_id:
                raise McpToolError(f"{name} requiere 'production_id'.")
            if name == "get_plan":
                return await run_in_threadpool(service.get_plan, production_id)
            return await run_in_threadpool(service.production_status, production_id)
        except McpToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - tipificar hacia el cliente MCP
            raise McpToolError(str(exc)) from exc

    belt_available = name in BELT_TOOLS_EXPOSED and ctx.tool_belt.get(name) is not None
    if name != "list_assets" and not belt_available:
        raise McpToolError(f"Herramienta MCP no disponible: {name!r}")

    project_id = await run_in_threadpool(
        _resolve_project_id, ctx.store, args.pop("project_id", None)
    )

    if name == "list_assets":
        kind = args.pop("kind", None)
        category = args.pop("category", None)
        if args:
            raise McpToolError(f"Argumentos no soportados por list_assets: {sorted(args)}")
        assets = await run_in_threadpool(
            ctx.store.list_assets,
            project_id,
            kind=str(kind) if kind else None,
            category=str(category) if category else None,
        )
        return {"project_id": project_id, "count": len(assets), "assets": assets}

    tool_ctx = ToolContext(
        project_id=project_id,
        store=ctx.store,
        comfyui_client=ctx.comfyui_client,
        broker=ctx.broker,
        config=ctx.config,
    )
    try:
        return await ctx.tool_belt.execute(name, args, tool_ctx)
    except Exception as exc:  # noqa: BLE001 — ToolExecutionError y fallos de backend
        raise McpToolError(str(exc)) from exc


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _rpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def handle_rpc_message(
    message: dict[str, Any],
    ctx: McpContext,
) -> dict[str, Any] | None:
    """Procesa un mensaje JSON-RPC; devuelve la respuesta o None (notificación)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(message.get("id") if isinstance(message, dict) else None,
                          _JSONRPC_INVALID_REQUEST, "Solicitud JSON-RPC 2.0 inválida")

    method = str(message.get("method") or "")
    req_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    is_notification = "id" not in message

    if method == "initialize":
        return _rpc_result(
            req_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else _rpc_result(req_id, {})

    if method == "tools/list":
        return _rpc_result(req_id, {"tools": build_mcp_tools(ctx.tool_belt)})

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments")
        if not tool_name or not isinstance(arguments, dict | None):
            return _rpc_error(
                req_id, _JSONRPC_INVALID_PARAMS, "tools/call requiere name y arguments"
            )
        try:
            raw = await _call_mcp_tool(tool_name, arguments or {}, ctx)
            safe = ctx.redactor.redact_value(raw)
            return _rpc_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(safe, ensure_ascii=False, default=str),
                        }
                    ],
                    "isError": False,
                },
            )
        except McpToolError as exc:
            safe_msg = ctx.redactor.redact(str(exc))
            return _rpc_result(
                req_id,
                {
                    "content": [{"type": "text", "text": safe_msg}],
                    "isError": True,
                },
            )

    if is_notification:
        return None
    return _rpc_error(req_id, _JSONRPC_METHOD_NOT_FOUND, f"Método MCP no soportado: {method!r}")


def _check_bearer_token(request: Request, ctx: McpContext) -> JSONResponse | None:
    """Exige `STUDIO_API_TOKEN` cuando está configurado.

    Acepta la cabecera dedicada `X-Studio-Token` (compatible con el Basic Auth de
    nginx, que ya ocupa `Authorization`) o, como alternativa para clientes
    loopback sin Basic Auth, `Authorization: Bearer <token>`.
    """
    expected = ctx.config.studio_api_token
    if not expected:
        return None
    expected_bytes = expected.encode("utf-8")
    dedicated = (request.headers.get("x-studio-token") or "").strip()
    if dedicated and secrets.compare_digest(dedicated.encode("utf-8"), expected_bytes):
        return None
    header = request.headers.get("authorization") or ""
    parts = header.split()
    if (
        len(parts) == 2
        and parts[0].lower() == "bearer"
        and secrets.compare_digest(parts[1].encode("utf-8"), expected_bytes)
    ):
        return None
    return JSONResponse(
        status_code=401,
        content={
            "detail": (
                "Token de API ausente o inválido (X-Studio-Token: <STUDIO_API_TOKEN> "
                "o Authorization: Bearer <STUDIO_API_TOKEN>)."
            )
        },
    )


def create_mcp_router(ctx: McpContext) -> APIRouter:
    """Router FastAPI con los endpoints MCP bajo /studio/api/mcp."""
    router = APIRouter()

    @router.get("/studio/api/mcp/health")
    async def mcp_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mcp_enabled": bool(ctx.config.studio_enable_mcp),
            "server": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "token_required": bool(ctx.config.studio_api_token),
        }

    @router.post("/studio/api/mcp")
    async def mcp_rpc(request: Request) -> Response:
        if not ctx.config.studio_enable_mcp:
            return JSONResponse(
                status_code=404,
                content={"detail": "MCP deshabilitado (STUDIO_ENABLE_MCP=0)."},
            )
        denied = _check_bearer_token(request, ctx)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                status_code=400,
                content=_rpc_error(None, _JSONRPC_PARSE_ERROR, "JSON inválido"),
            )

        if isinstance(payload, list):
            if not payload:
                return JSONResponse(
                    status_code=400,
                    content=_rpc_error(None, _JSONRPC_INVALID_REQUEST, "Batch vacío"),
                )
            responses: list[dict[str, Any]] = []
            for item in payload:
                try:
                    single = await handle_rpc_message(item, ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Error interno procesando batch MCP")
                    single = _rpc_error(
                        item.get("id") if isinstance(item, dict) else None,
                        _JSONRPC_INTERNAL_ERROR,
                        ctx.redactor.redact(str(exc)),
                    )
                if single is not None:
                    responses.append(single)
            if not responses:
                return Response(status_code=202)
            return JSONResponse(
                content=ctx.redactor.redact_value(responses),
                headers={"Cache-Control": "no-store"},
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400,
                content=_rpc_error(None, _JSONRPC_INVALID_REQUEST, "Solicitud JSON-RPC inválida"),
            )

        try:
            response = await handle_rpc_message(payload, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error interno procesando mensaje MCP")
            response = _rpc_error(
                payload.get("id"),
                _JSONRPC_INTERNAL_ERROR,
                ctx.redactor.redact(str(exc)),
            )
        if response is None:
            return Response(status_code=202)
        return JSONResponse(
            content=ctx.redactor.redact_value(response),
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/studio/api/mcp")
    async def mcp_get() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    return router
