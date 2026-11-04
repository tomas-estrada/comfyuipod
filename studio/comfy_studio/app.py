"""Aplicación FastAPI principal de Comfy Studio (127.0.0.1:18190, /studio/api/*).

M7-SCR-002: `create_app` solo cablea estado, middlewares y lifespan; las rutas
viven en `comfy_studio/routers/` y los helpers/modelos en `comfy_studio/webutil.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil  # noqa: F401 (re-export: tests parchean app.shutil, módulo compartido con webutil)
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from comfy_studio.agent import OpenAICompatibleLLMClient, StudioAgent, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import (
    StudioConfig,
    install_log_redaction,
)
from comfy_studio.engines.comfyui import ComfyUIEngine
from comfy_studio.engines.editor import EditorEngine
from comfy_studio.engines.qa import QaEngine
from comfy_studio.engines.upscaler import UpscalerEngine
from comfy_studio.mcp_server import McpContext, create_mcp_router
from comfy_studio.ml import probe_capabilities
from comfy_studio.observability import (
    REQUEST_ID_HEADER,
    bind_request_id,
    new_request_id,
    unbind_request_id,
)
from comfy_studio.orchestration.adapters import build_server_collaborators
from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.service import (
    ProductionService,
)
from comfy_studio.perception import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    ClipVisualIndex,
    InsightFaceIndex,
    PerceptionVectorStore,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.recovery import recover_interrupted_jobs
from comfy_studio.routers import register_all_routes
from comfy_studio.stubs.llm import StubLLMClient
from comfy_studio.templates import (
    get_default_registry,
)
from comfy_studio.webhooks import (
    BrokerWebhookDispatcher,
    WebhookNotifier,
    create_webhook_notifier,
)
from comfy_studio.webutil import (  # noqa: F401 (re-export: compatibilidad de imports previos)
    _MULTIPART_ENVELOPE_OVERHEAD_BYTES,
    EdlBuildRequest,
    _BoundedUploadAPIRoute,
    _check_upload_content_length,
    _check_upload_free_disk,
    _edge_auth_effective_pass,
    _edge_auth_user,
    _is_media_upload_request,
    _verify_basic_auth,
)

logger = logging.getLogger(__name__)


def create_app(
    config: StudioConfig | None = None,
    *,
    comfyui_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    cfg = config if config is not None else StudioConfig.from_env()
    redactor = cfg.redactor()
    install_log_redaction(redactor)
    install_log_redaction(redactor, logger=logging.getLogger("comfy_studio"))

    # M3-SEC-002: sin contraseña exigible no hay lockout posible (ver test de
    # regresión), pero la postura fail-open debe ser VISIBLE: WARN en arranque +
    # `edge_auth: "open"` en /studio/api/health.
    if _edge_auth_effective_pass(cfg) is None and (
        cfg.require_auth
        or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
    ):
        logger.warning(
            "STUDIO: require_auth activo pero sin COMFY_PASS/STUDIO_AUTH_PASS exigible; "
            "los endpoints /studio se sirven SIN autenticación (fail-open visible, "
            "edge_auth=open en /studio/api/health). Define COMFY_PASS para activar Basic Auth."
        )

    store = ProjectStore(cfg.studio_data_dir)
    broker = ProgressBroker(redactor=redactor)
    stub_llm = StubLLMClient(model=cfg.studio_llm_model) if cfg.studio_stub_llm else None

    webhook_notifier: WebhookNotifier | None = create_webhook_notifier(cfg)
    webhook_dispatcher: BrokerWebhookDispatcher | None = (
        BrokerWebhookDispatcher(broker, webhook_notifier)
        if webhook_notifier is not None
        else None
    )

    @asynccontextmanager
    async def _webhook_lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        # El lazo de orquestación corre en threadpool: vincula el loop al broker para
        # que sus eventos (production_step, etc.) lleguen al bus SSE de forma segura.
        broker.bind_loop(asyncio.get_running_loop())
        if webhook_notifier is not None:
            webhook_notifier.start()
        if webhook_dispatcher is not None:
            webhook_dispatcher.start()

        async def _run_job_recovery() -> None:
            try:
                report = await recover_interrupted_jobs(
                    store=store,
                    comfyui_client=_app.state.comfyui_client,
                    broker=broker,
                    resume=cfg.studio_resume_jobs,
                    start_delay_s=(
                        cfg.studio_resume_delay_s if cfg.studio_resume_jobs else 0.0
                    ),
                )
                if report.get("stale"):
                    logger.info("Recuperación de jobs tras reinicio: %s", report)
            except Exception:
                logger.exception("Error en la recuperación de jobs interrumpidos")

        recovery_task: asyncio.Task[None] = asyncio.create_task(_run_job_recovery())

        async def _run_reconcile_loop() -> None:
            """Watchdog periódico: reconcilia jobs en vuelo contra /queue+/history de ComfyUI.

            Si ComfyUI se reinició (OOM CUDA exit 137) y un prompt desapareció, el job
            queda `interrupted`/reencolado en vivo en vez de esperar al siguiente arranque
            de Studio o al timeout de poll. Con `STUDIO_RECONCILE_INTERVAL_S=0` está off.
            """
            interval = float(getattr(cfg, "studio_reconcile_interval_s", 30.0))
            if interval <= 0:
                return
            while True:
                await asyncio.sleep(interval)
                try:
                    report = await recover_interrupted_jobs(
                        store=store,
                        comfyui_client=_app.state.comfyui_client,
                        broker=broker,
                        resume=cfg.studio_resume_jobs,
                        start_delay_s=0.0,
                        watchdog=True,
                    )
                    if any(
                        report.get(k) for k in ("requeued", "attached", "interrupted", "failed")
                    ):
                        logger.info("Reconciliación en vivo de jobs vs ComfyUI: %s", report)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error en la reconciliación periódica de jobs")

        reconcile_task: asyncio.Task[None] | None = None
        if float(getattr(cfg, "studio_reconcile_interval_s", 30.0)) > 0:
            reconcile_task = asyncio.create_task(_run_reconcile_loop())
        try:
            yield
        finally:
            if reconcile_task is not None:
                reconcile_task.cancel()
                try:
                    await reconcile_task
                except asyncio.CancelledError:
                    pass
            # M1.6: drenar la recuperación acotadamente en vez de cancelarla en seco. Un
            # resume a mitad de `execute_prompt_job` quedaba en estado no terminal sin
            # reconciliación; ahora se le da `STUDIO_RECOVERY_DRAIN_S` y, si no termina,
            # el job queda `interrupted` antes de cancelar.
            if not recovery_task.done():
                drain_s = max(0.0, float(cfg.studio_recovery_drain_s))
                try:
                    await asyncio.wait_for(asyncio.shield(recovery_task), timeout=drain_s)
                except (TimeoutError, asyncio.CancelledError):
                    recovery_task.cancel()
                    try:
                        await recovery_task
                    except asyncio.CancelledError:
                        pass
                    logger.warning(
                        "STUDIO: la recuperación de jobs no terminó en %.0fs; tarea cancelada.",
                        drain_s,
                    )
            if webhook_dispatcher is not None:
                await webhook_dispatcher.stop()
            if webhook_notifier is not None:
                await webhook_notifier.stop()
            # M7-SCR-001: cancelar los renders en segundo plano pendientes; los jobs
            # quedan en estado en vuelo y `recovery.py` los reconcilia al arrancar.
            render_tasks = list(getattr(_app.state, "render_tasks", set()))
            for render_task in render_tasks:
                render_task.cancel()
            if render_tasks:
                await asyncio.gather(*render_tasks, return_exceptions=True)
                logger.info(
                    "STUDIO: %d render(s) en segundo plano cancelados por el apagado "
                    "(se recuperan al arrancar).",
                    len(render_tasks),
                )

    app = FastAPI(
        title="Comfy Studio Server",
        version="0.1.0",
        lifespan=_webhook_lifespan,
    )
    app.router.route_class = _BoundedUploadAPIRoute
    app.state.config = cfg
    app.state.redactor = redactor
    app.state.store = store
    app.state.broker = broker
    app.state.stub_llm = stub_llm
    app.state.webhook_notifier = webhook_notifier
    app.state.webhook_dispatcher = webhook_dispatcher
    app.state.comfyui_transport = comfyui_transport
    app.state.comfyui_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url,
        transport=comfyui_transport,
        store=store,
        broker=broker,
        redactor=redactor,
    )
    # M7-SCR-001: tareas de render en segundo plano (submit async) y su semáforo.
    app.state.render_tasks = set()
    app.state.render_semaphore = asyncio.Semaphore(cfg.max_concurrent_renders)
    # (El ProductionService se arma más abajo, tras construir app.state.llm_client,
    # porque el cableado de motores reales lo necesita — M6-AGT-001.)
    # M1.2: la construcción de la percepción vectorial NO puede tumbar la app. Si
    # `sqlite-vec` falta o falla, se degrada a `perception_available=False`: los
    # endpoints de percepción responden 503 con el motivo y el resto de Studio
    # (proyectos, chat, renders, galería) sigue funcionando.
    #
    # M3.2: `STUDIO_PERCEPTION_BACKEND` (auto|real|sim) selecciona el backend:
    #   - sim: rutas deterministas actuales, `simulated: true`, sin probe.
    #   - auto: probe al arranque; si pasa -> real, si no -> sim (WARN visible).
    #   - real: sin fallback; si el probe falla, perception_available=False y
    #     los endpoints responden 503 con el motivo.
    perception_error: str | None = None
    vector_store: PerceptionVectorStore | None = None
    perception_backend: str = "sim"
    probe_result: dict[str, Any] = {}
    try:
        vector_store = PerceptionVectorStore(cfg.studio_data_dir / "index.db")
    except Exception as exc:
        perception_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "STUDIO: percepción vectorial DESHABILITADA (%s). Los endpoints de percepción "
            "responderán 503; el resto de Studio sigue operativo.",
            redactor.redact(perception_error),
        )

    requested_backend = (cfg.studio_perception_backend or "auto").strip().lower()
    if requested_backend == "sim":
        perception_backend = "sim"
    elif requested_backend == "real":
        # Fallo cerrado: sin fallback a sim. Ejecutar probe para reportar motivo.
        caps = probe_capabilities(redactor=redactor)
        probe_result = caps.to_public_dict()
        if caps.probe_ok and vector_store is not None:
            perception_backend = "real"
        else:
            perception_backend = "real"
            if not caps.probe_ok:
                perception_error = probe_result.get("reason") or "probe no pasó"
                if vector_store is not None:
                    perception_error = (perception_error or "") + " (sqlite-vec OK)"
                logger.warning(
                    "STUDIO: percepción en modo REAL pero el probe ML falló (%s). "
                    "Los endpoints de percepción responderán 503; sin fallback a sim.",
                    redactor.redact(perception_error),
                )
            if vector_store is None:
                perception_error = (perception_error or "") + "; sqlite-vec no disponible"
    else:
        # auto: probe al arranque; real si pasa, sim si no (fallback visible).
        caps = probe_capabilities(redactor=redactor)
        probe_result = caps.to_public_dict()
        if caps.probe_ok and vector_store is not None:
            perception_backend = "real"
        else:
            perception_backend = "sim"
            if not caps.probe_ok:
                logger.warning(
                    "STUDIO: percepción en modo SIMULACIÓN (fallback de auto; "
                    "probe ML: %s). Usa STUDIO_PERCEPTION_BACKEND=real para exigir "
                    "backends reales o =sim para silenciar este aviso.",
                    redactor.redact(probe_result.get("reason") or "probe no pasó"),
                )

    app.state.perception_available = vector_store is not None
    app.state.perception_error = perception_error
    app.state.perception_backend = perception_backend
    app.state.perception_probe_result = probe_result
    app.state.vector_store = vector_store

    # M3.3: comparar versión del índice almacenada con las dims activas.
    # Desajuste (p.ej. 64 -> 768/512 al migrar a backends reales) ⇒ stale:
    # lecturas devuelven vacío con stale=true, escrituras rechazan 409.
    perception_stale = False
    if vector_store is not None:
        stored_clip, stored_face = vector_store.stored_dims()
        active_clip = CLIP_EMBED_DIM
        active_face = FACE_EMBED_DIM
        if stored_clip is not None and (stored_clip != active_clip or stored_face != active_face):
            perception_stale = True
            logger.warning(
                "STUDIO: índice de percepción STALE (dims almacenados clip=%s face=%s, "
                "activas clip=%s face=%s). Lecturas devolverán vacío con stale=true; "
                "escrituras rechazan 409. Ejecuta POST /studio/api/perception/reindex.",
                stored_clip, stored_face, active_clip, active_face,
            )
        elif stored_clip is None and vector_store.get_schema_meta() == {}:
            # Índice nuevo: registrar meta de versión en el primer alta.
            pass
    app.state.perception_stale = perception_stale
    app.state.clip_index = (
        ClipVisualIndex(
            store,
            vector_store,
            ffmpeg_timeout_s=cfg.studio_ffmpeg_timeout_s,
            ffprobe_timeout_s=cfg.studio_ffprobe_timeout_s,
        )
        if vector_store
        else None
    )
    app.state.face_index = InsightFaceIndex(store, vector_store) if vector_store else None
    app.state.template_registry = get_default_registry()
    app.state.tool_belt = create_default_tool_belt()
    app.state.llm_client = OpenAICompatibleLLMClient(
        config=cfg,
        stub_client=stub_llm,
        redactor=redactor,
    )
    app.state.agent = StudioAgent(
        config=cfg,
        store=store,
        comfyui_client=app.state.comfyui_client,
        broker=broker,
        llm_client=app.state.llm_client,
        tool_belt=app.state.tool_belt,
        redactor=redactor,
    )

    # Servicio de producción (orquestación M6). La StoryboardGate es la puerta
    # de gasto humano: approve_plan NUNCA se expone al agente LLM (ver
    # mcp_server.py / M6-AGT-001), solo la API pública la acciona.
    #
    # Colaboradores: por defecto STUB (CI/tests sin GPU). En el pod, con
    # STUDIO_ORCH_REAL_ENGINES=1, se cablean los adaptadores reales a los
    # motores vivos (requiere llm_client ya construido, de ahí esta posición).
    # El cableado es TOLERANTE a fallos: si construir un motor real falla, esa
    # fase cae a su stub y el servicio arranca igual (nunca tumba el pod).
    orch_collaborators = None
    if (os.environ.get("STUDIO_ORCH_REAL_ENGINES") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        try:
            orch_collaborators = build_server_collaborators(
                llm_client=app.state.llm_client,
                engines={
                    "comfyui": ComfyUIEngine(app.state.comfyui_client),
                    "upscaler": UpscalerEngine(),
                },
                qa_engine=QaEngine(),
                editor_engine=EditorEngine(backend="remotion"),
                template_registry=app.state.template_registry,
            )
            logger.info(
                "STUDIO: orquestación con motores REALES (STUDIO_ORCH_REAL_ENGINES=1)."
            )
        except Exception as exc:
            logger.warning(
                "STUDIO: no se pudieron cablear los motores reales de orquestación "
                "(%s); se usan stubs.",
                redactor.redact(str(exc)),
            )
            orch_collaborators = None
    # Checkpointer de producciones arraigado en el data-dir de la config (en prod
    # equivale a _default_checkpoint_dir; en tests aísla cada app en su tmp_path).
    # STUDIO_ORCH_CHECKPOINT_DIR sigue como override explícito.
    orch_ckpt_env = os.environ.get("STUDIO_ORCH_CHECKPOINT_DIR", "").strip()
    orch_checkpointer = ProductionCheckpointer(
        Path(orch_ckpt_env) / "checkpoints.sqlite3"
        if orch_ckpt_env
        else cfg.studio_data_dir / "orchestration" / "checkpoints.sqlite3"
    )
    app.state.production_service = ProductionService(
        checkpointer=orch_checkpointer,
        collaborators=orch_collaborators,
        store=store,
        broker=broker,
    )

    mcp_ctx = McpContext(
        store=store,
        comfyui_client=app.state.comfyui_client,
        broker=broker,
        config=cfg,
        tool_belt=app.state.tool_belt,
        redactor=redactor,
        production_service=app.state.production_service,
    )
    app.state.mcp_context = mcp_ctx
    app.include_router(create_mcp_router(mcp_ctx))
    # M7-SCR-002: rutas extraídas a comfy_studio/routers/ (mismo mapa).
    register_all_routes(app)

    @app.middleware("http")
    async def edge_auth_and_redaction_middleware(request: Request, call_next):
        active_cfg: StudioConfig = request.app.state.config
        env_user = _edge_auth_user(active_cfg)
        effective_pass = _edge_auth_effective_pass(active_cfg)
        require_auth = (
            active_cfg.require_auth
            or bool(effective_pass)
            or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
            or request.headers.get("X-Studio-Simulate-Edge") == "1"
        )

        # Sin contraseña configurada no hay credencial válida posible: exigir auth
        # bloquearía todas las peticiones (lockout total). Se omite la exigencia
        # (fail-open VISIBLE: WARN en arranque + edge_auth=open en health, M3-SEC-002).
        if require_auth and effective_pass and request.url.path.startswith(("/studio", "/health")):
            auth_header = request.headers.get("Authorization")
            # M3-C: los clientes máquina del endpoint MCP pueden autenticarse con
            # `Authorization: Bearer <STUDIO_API_TOKEN>` en lugar de Basic Auth
            # (nginx sigue exigiendo Basic Auth en el borde público).
            mcp_bearer_ok = False
            api_token = active_cfg.get_api_token()
            if (
                api_token
                and request.url.path.startswith("/studio/api/mcp")
                and auth_header
            ):
                parts = auth_header.split()
                mcp_bearer_ok = (
                    len(parts) == 2
                    and parts[0].lower() == "bearer"
                    and secrets.compare_digest(parts[1].encode("utf-8"), api_token.encode("utf-8"))
                )
            if not mcp_bearer_ok and not _verify_basic_auth(auth_header, env_user, effective_pass):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Credenciales requeridas"},
                    headers={"WWW-Authenticate": 'Basic realm="ComfyUI Vast.ai Access"'},
                )

        if _is_media_upload_request(request):
            try:
                _check_upload_content_length(
                    request.headers,
                    active_cfg.max_upload_bytes,
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )

        response = await call_next(request)

        # M3-SCR-008 (regla 3 de AGENTS.md): TODA respuesta JSON de /studio y /health pasa
        # por SecretRedactor de forma centralizada, incluidos `detail=str(exc)`
        # de HTTPException y dicts crudos de los endpoints M3 (percepción, ASR,
        # EDL). SSE, archivos y media quedan fuera (no son application/json).
        if request.url.path.startswith(("/studio", "/health")):
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                body = b"".join([chunk async for chunk in response.body_iterator])
                if body:
                    redacted = active_cfg.redactor().redact(body.decode("utf-8"))
                    headers = dict(response.headers)
                    headers["content-length"] = str(len(redacted.encode("utf-8")))
                    return Response(
                        content=redacted,
                        status_code=response.status_code,
                        headers=headers,
                        media_type="application/json",
                    )
        return response

    # M7-OPS-005: request ID por petición (cabecera X-Request-Id entrante segura o
    # uuid4 generado), reflejado en la respuesta y disponible en el ContextVar para
    # los logs. Se registra DESPUÉS del middleware de edge auth para quedar como
    # middleware más externo: la cabecera también se devuelve en 401.
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = new_request_id(request)
        token = bind_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            unbind_request_id(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


    #
    # GOBERNANZA (M6-AGT-001): la puerta de gasto humano. `plan` deja la
    # producción en `awaiting_approval` SIN gastar GPU; el lazo caro (ejecución
    # + ensamblaje) solo corre tras `approve`, y `approve` SOLO vive aquí, en la
    # API pública accionada por el usuario. El agente LLM NUNCA ve `approve`
    # (ver BELT_TOOLS_EXPOSED en mcp_server.py). `approve` despacha el lazo como
    # tarea de fondo (run_in_threadpool) para no bloquear Nginx (504).


    @app.api_route(
        "/studio/api/{unknown_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def unknown_api_route(unknown_path: str) -> JSONResponse:
        raise HTTPException(
            status_code=404,
            detail=f"Ruta de API no encontrada: /studio/api/{unknown_path}",
        )

    panel_dir = _resolve_panel_dir()
    if panel_dir is not None:
        index_file = panel_dir / "index.html"

        @app.get("/studio", include_in_schema=False)
        async def serve_studio_root_no_slash() -> FileResponse:
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        @app.get("/studio/", include_in_schema=False)
        async def serve_studio_root_slash() -> FileResponse:
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        app.mount(
            "/studio",
            StaticFiles(directory=str(panel_dir), html=True),
            name="studio-panel",
        )

    return app


def _resolve_panel_dir() -> Path | None:
    """Localiza el directorio estático panel/ en entorno local o contenedor."""
    candidates: list[Path] = []
    env_dir = os.environ.get("STUDIO_PANEL_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(
        [
            Path(__file__).resolve().parents[2] / "panel",
            Path("/etc/comfyui/panel"),
            Path("/opt/studio/panel"),
            Path.cwd() / "panel",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


app = create_app()
