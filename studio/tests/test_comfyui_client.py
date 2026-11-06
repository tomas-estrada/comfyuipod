"""Tests de integración y unidad para VAL-SERVER-003: Cliente HTTP de ComfyUI contra stub."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.comfyui_client import (
    ComfyUICancelledError,
    ComfyUIClient,
    ComfyUIConnectionError,
    ComfyUIError,
    ComfyUIExecutionError,
    ComfyUITimeoutError,
    parse_ws_event,
)
from comfy_studio.config import SecretRedactor
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_execute_prompt_job_cancels_only_its_prompt_and_unloads(
    studio_data_dir: Path,
) -> None:
    state = ComfyUIStubState(healthy=True, poll_ticks_before_complete=100)
    stub_app = create_stub_comfyui_app(state)
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Cancel active render")
    job = store.create_job(kind="render", project_id=project["id"])
    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39173",
        transport=httpx.ASGITransport(app=stub_app),
        store=store,
        poll_interval=0.01,
    )
    cancel = threading.Event()

    async def _run() -> None:
        task = asyncio.create_task(
            client.execute_prompt_job(
                {"1": {"class_type": "KSampler", "inputs": {}}},
                job_id=job["id"],
                project_id=project["id"],
                cancel_event=cancel,
            )
        )
        for _ in range(100):
            if state.prompts:
                break
            await asyncio.sleep(0.01)
        assert state.prompts
        prompt_id = state.prompts[0]["prompt_id"]
        cancel.set()
        with pytest.raises(ComfyUICancelledError):
            await task
        assert prompt_id not in state.pending_prompts
        assert state.interrupted is True
        assert store.get_job(job["id"])["status"] == "cancelled"
        assert state.freed_count == 1

    asyncio.run(_run())


@pytest.fixture()
def sample_workflow() -> dict[str, Any]:
    return json.loads(
        (FIXTURES_DIR / "workflow_api_trivial.json").read_text(encoding="utf-8")
    )


@pytest.fixture()
def sample_png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )


@pytest.fixture()
def sample_mp4_bytes() -> bytes:
    return (
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        b"\x00\x00\x00\x08free\x00\x00\x00\x20mdatFAKEVIDEOFRAME"
    )


def test_full_lifecycle_submit_poll_history_view_against_stub(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """Ciclo completo contra el stub: submit -> prompt_id -> poll (/queue + /history) -> /view."""
    state = ComfyUIStubState(
        healthy=True,
        poll_ticks_before_complete=2,
    )
    stub_app = create_stub_comfyui_app(state)
    transport = httpx.ASGITransport(app=stub_app)
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()

    project = store.create_project(name="Proyecto Render Test")
    job = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
        params={"prompt": "astronauta en estudio"},
    )

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=transport,
        store=store,
        broker=broker,
        poll_interval=0.05,
        poll_timeout=5.0,
        sleep_func=fake_sleep,
    )

    import asyncio

    async def _run() -> None:
        stats = await client.system_stats()
        assert "devices" in stats

        submission = await client.submit_prompt(
            sample_workflow,
            job_id=job["id"],
            project_id=project["id"],
        )
        assert submission.prompt_id.startswith("stub-")
        assert submission["prompt_id"] == submission.prompt_id

        # En el primer instante aún está en cola / ejecutándose
        queue_now = await client.get_queue()
        assert queue_now.is_prompt_active(submission.prompt_id)

        # Sondear hasta completar
        result = await client.poll_until_complete(
            submission.prompt_id,
            job_id=job["id"],
            project_id=project["id"],
        )
        assert result.completed is True
        assert result.prompt_id == submission.prompt_id
        assert len(result.outputs) >= 1

        # Recuperar bytes de salida vía GET /view
        video_out = next(o for o in result.outputs if o.filename.endswith(".mp4"))
        raw_bytes = await client.view_output(
            video_out.filename,
            subfolder=video_out.subfolder,
            storage_type=video_out.storage_type,
        )
        assert raw_bytes.startswith(b"\x00\x00\x00\x18ftypmp42")

    asyncio.run(_run())

    # Verificar que hizo espera entre sondeos mientras el prompt estaba en cola
    assert len(sleep_delays) >= 2

    # Verificar que el job en SQLite quedó en estado done con resultado y prompt_id
    updated_job = store.get_job(job["id"])
    assert updated_job is not None
    assert updated_job["status"] == "done"
    assert updated_job["prompt_id"].startswith("stub-")
    assert updated_job["progress_value"] == updated_job["progress_max"]
    assert len(updated_job["result"].get("outputs", [])) >= 1

    # Verificar eventos emitidos al canal SSE
    events = broker.recent_events()
    event_types = [etype for etype, _ in events]
    assert "progress" in event_types
    assert "job" in event_types
    done_events = [
        payload
        for etype, payload in events
        if etype == "job" and payload.get("status") == "done"
    ]
    assert len(done_events) >= 1
    assert done_events[-1]["job_id"] == job["id"]


def test_uploads_image_and_vhs_video_and_retrieves_via_view(
    sample_png_bytes: bytes,
    sample_mp4_bytes: bytes,
) -> None:
    """Subidas de imagen (/upload/image) y vídeo VHS + lectura posterior por /view."""
    state = ComfyUIStubState(healthy=True)
    stub_app = create_stub_comfyui_app(state)
    transport = httpx.ASGITransport(app=stub_app)
    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=transport,
    )

    import asyncio

    async def _run() -> None:
        img_res = await client.upload_image(
            sample_png_bytes,
            filename="ref_subject.png",
            subfolder="refs",
            overwrite=True,
        )
        assert img_res.name == "ref_subject.png"
        assert img_res.subfolder == "refs"
        assert img_res.storage_type == "input"

        vid_res = await client.upload_video(
            sample_mp4_bytes,
            filename="take_a.mp4",
            subfolder="clips",
            overwrite=True,
        )
        assert vid_res.name == "take_a.mp4"
        assert vid_res.subfolder == "clips"
        assert vid_res.storage_type == "input"

        fetched_img = await client.view_output(
            "ref_subject.png",
            subfolder="refs",
            storage_type="input",
        )
        assert fetched_img == sample_png_bytes

        fetched_vid = await client.view_output(
            "take_a.mp4",
            subfolder="clips",
            storage_type="input",
        )
        assert fetched_vid == sample_mp4_bytes

    asyncio.run(_run())


def test_ws_events_parsed_and_mapped_to_studio_sse(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """Eventos /ws (status, executing, progress, executed) mapeados al SSE del estudio."""
    state = ComfyUIStubState(healthy=True)
    stub_app = create_stub_comfyui_app(state)
    transport = httpx.ASGITransport(app=stub_app)
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()

    project = store.create_project(name="WS Project")
    job = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="vace_inpaint",
    )

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=transport,
        store=store,
        broker=broker,
    )

    # Conectarse al endpoint /ws del stub y consumir los eventos con el cliente
    with TestClient(stub_app) as tc:
        submit_resp = tc.post(
            "/prompt",
            json={"prompt": sample_workflow, "client_id": "test-ws-client"},
        )
        prompt_id = submit_resp.json()["prompt_id"]

        with tc.websocket_connect(
            f"/ws?clientId=test-ws-client&promptId={prompt_id}"
        ) as ws:
            raw_messages: list[str] = []
            while True:
                msg = ws.receive_text()
                raw_messages.append(msg)
                parsed = parse_ws_event(msg)
                assert parsed is not None
                client.handle_ws_event(
                    msg,
                    job_id=job["id"],
                    project_id=project["id"],
                )
                if parsed.event_type == "executing" and parsed.node is None:
                    break

    assert len(raw_messages) >= 4
    sse_events = broker.recent_events()
    progress_payloads = [p for t, p in sse_events if t == "progress"]
    job_payloads = [p for t, p in sse_events if t == "job"]

    assert any(
        p.get("value") == 4 and p.get("max") == 4 and p.get("job_id") == job["id"]
        for p in progress_payloads
    )
    # executing node=None señala fin de ejecución, pero el job NO debe marcarse
    # done desde el WS: done solo llega vía _complete_job tras persistir salidas.
    assert any(
        p.get("execution_complete") is True and p.get("job_id") == job["id"]
        for p in progress_payloads
    )
    assert not any(
        p.get("status") == "done" and p.get("job_id") == job["id"]
        for p in job_payloads
    )

    final_job = store.get_job(job["id"])
    assert final_job is not None
    assert final_job["status"] == "running"
    assert final_job["progress_value"] == 4
    assert final_job["progress_max"] == 4


def test_retries_with_exponential_backoff_on_transient_failures(
    sample_workflow: dict[str, Any],
) -> None:
    """Reintentos con backoff exponencial cuando ComfyUI devuelve 503 o error de transporte."""
    state = ComfyUIStubState(
        healthy=True,
        transient_failures_remaining=2,
        transient_failure_status=503,
    )
    stub_app = create_stub_comfyui_app(state)
    transport = httpx.ASGITransport(app=stub_app)

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=transport,
        max_retries=3,
        backoff_base=0.1,
        backoff_factor=2.0,
        sleep_func=fake_sleep,
    )

    import asyncio

    async def _run() -> None:
        sub = await client.submit_prompt(sample_workflow)
        assert sub.prompt_id.startswith("stub-")

    asyncio.run(_run())

    # Dos fallos transitorios -> dos esperas con backoff exponencial (0.1, 0.2)
    assert len(delays) == 2
    assert delays[0] == pytest.approx(0.1)
    assert delays[1] == pytest.approx(0.2)


def test_exhausted_retries_and_comfyui_errors_surfaced_to_job(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """Los errores de validación, ejecución o red agotada se elevan al job y no se silencian."""
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Error Surfacing Project")

    # Caso 1: Error de validación de nodos en POST /prompt (400 node_errors)
    state_400 = ComfyUIStubState(
        healthy=True,
        fail_next_prompt_error="Prompt outputs failed validation",
        fail_next_node_errors={"2": {"errors": [{"message": "Invalid sampler_name"}]}},
    )
    stub_app_400 = create_stub_comfyui_app(state_400)
    job_400 = store.create_job(kind="render", project_id=project["id"])
    client_400 = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app_400),
        store=store,
        broker=broker,
        max_retries=2,
    )

    import asyncio

    with pytest.raises(ComfyUIExecutionError) as exc_info:
        asyncio.run(
            client_400.submit_prompt(
                sample_workflow,
                job_id=job_400["id"],
                project_id=project["id"],
            )
        )
    assert "Invalid sampler_name" in str(exc_info.value) or "validation" in str(
        exc_info.value
    )
    stored_400 = store.get_job(job_400["id"])
    assert stored_400 is not None
    assert stored_400["status"] == "error"
    assert stored_400["error"]

    # Caso 2: Error de ejecución durante el render en /history/{prompt_id}
    state_exec_err = ComfyUIStubState(
        healthy=True,
        fail_execution_error="CUDA out of memory in KSampler",
    )
    stub_app_exec = create_stub_comfyui_app(state_exec_err)
    job_exec = store.create_job(kind="render", project_id=project["id"])
    client_exec = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app_exec),
        store=store,
        broker=broker,
        poll_interval=0.01,
    )

    async def _run_exec_err() -> None:
        sub = await client_exec.submit_prompt(
            sample_workflow,
            job_id=job_exec["id"],
            project_id=project["id"],
        )
        await client_exec.poll_until_complete(
            sub.prompt_id,
            job_id=job_exec["id"],
            project_id=project["id"],
        )

    with pytest.raises(ComfyUIExecutionError) as exec_err_info:
        asyncio.run(_run_exec_err())
    assert "CUDA out of memory" in str(exec_err_info.value)
    stored_exec = store.get_job(job_exec["id"])
    assert stored_exec is not None
    assert stored_exec["status"] == "error"
    assert "CUDA out of memory" in (stored_exec["error"] or "")


def test_poll_and_request_timeouts_surfaced_to_job(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """El timeout de sondeo y el timeout de petición HTTP elevan ComfyUITimeoutError al job."""
    state = ComfyUIStubState(
        healthy=True,
        poll_ticks_before_complete=100,  # nunca completa dentro del timeout corto
    )
    stub_app = create_stub_comfyui_app(state)
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Timeout Project")
    job = store.create_job(kind="render", project_id=project["id"])

    async def fast_sleep(_delay: float) -> None:
        return None

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app),
        store=store,
        broker=broker,
        poll_interval=0.05,
        poll_timeout=0.12,
        sleep_func=fast_sleep,
    )

    import asyncio

    async def _run_timeout() -> None:
        sub = await client.submit_prompt(
            sample_workflow,
            job_id=job["id"],
            project_id=project["id"],
        )
        await client.poll_until_complete(
            sub.prompt_id,
            job_id=job["id"],
            project_id=project["id"],
            timeout=0.05,
        )

    with pytest.raises(ComfyUITimeoutError):
        asyncio.run(_run_timeout())

    stored_job = store.get_job(job["id"])
    assert stored_job is not None
    assert stored_job["status"] == "error"
    assert "timeout" in (stored_job["error"] or "").lower()


def test_transport_timeout_retries_and_raises_timeout_error() -> None:
    """Un timeout de red en httpx reintenta con backoff y finalmente lanza ComfyUITimeoutError."""
    attempts = 0

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout(f"Read timed out on {request.url}")

    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.MockTransport(timeout_handler),
        max_retries=2,
        backoff_base=0.05,
        sleep_func=fake_sleep,
    )

    import asyncio

    with pytest.raises(ComfyUITimeoutError):
        asyncio.run(client.system_stats())

    assert attempts == 3  # 1 intento inicial + 2 reintentos
    assert len(delays) == 2


def test_secret_redaction_in_comfyui_errors(studio_data_dir: Path) -> None:
    """Cualquier error elevado por ComfyUIClient pasa por SecretRedactor."""
    fake_secret = "sk-test-secret-do-not-leak-999999"
    redactor = SecretRedactor(studio_llm_key=fake_secret)
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Redaction Project")
    job = store.create_job(kind="render", project_id=project["id"])

    state = ComfyUIStubState(
        healthy=True,
        fail_next_prompt_error=f"Fallo con token accidental {fake_secret}",
    )
    stub_app = create_stub_comfyui_app(state)
    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app),
        store=store,
        redactor=redactor,
    )

    import asyncio

    with pytest.raises(ComfyUIError) as exc_info:
        asyncio.run(
            client.submit_prompt(
                {"1": {"class_type": "KSampler", "inputs": {}}},
                job_id=job["id"],
                project_id=project["id"],
            )
        )

    assert fake_secret not in str(exc_info.value)
    stored = store.get_job(job["id"])
    assert stored is not None
    assert fake_secret not in (stored["error"] or "")


def test_execute_prompt_job_persists_outputs_and_streams_to_studio_sse_endpoint(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """Ciclo end-to-end: execute_prompt_job descarga salidas y alimenta /studio/api/progress."""
    from comfy_studio.app import create_app
    from comfy_studio.config import StudioConfig

    state = ComfyUIStubState(healthy=True, poll_ticks_before_complete=1)
    stub_app = create_stub_comfyui_app(state)
    transport = httpx.ASGITransport(app=stub_app)

    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_stub_llm=True,
        require_auth=False,
    )
    app = create_app(cfg, comfyui_transport=transport)
    comfy_client: ComfyUIClient = app.state.comfyui_client
    store: ProjectStore = app.state.store

    project = store.create_project(name="E2E Gallery Project")
    job = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
    )

    import asyncio

    result = asyncio.run(
        comfy_client.execute_prompt_job(
            sample_workflow,
            job_id=job["id"],
            project_id=project["id"],
            poll_interval=0.01,
            persist_outputs=True,
        )
    )
    assert result.completed is True
    assert len(result.outputs) == 2

    # Verificar que los archivos descargados por /view se guardaron en outputs del proyecto
    output_assets = store.list_assets(project["id"], category="outputs")
    assert len(output_assets) == 2
    filenames = {a["filename"] for a in output_assets}
    assert any(f.endswith(".mp4") for f in filenames)
    assert any(f.endswith(".png") for f in filenames)

    # Verificar lectura de los eventos desde el endpoint SSE real /studio/api/progress
    with TestClient(app) as tc:
        resp = tc.get("/studio/api/progress?replay=1&max_events=4")
        assert resp.status_code == 200
        assert "event: progress" in resp.text or "event: job" in resp.text
        assert job["id"] in resp.text


def test_object_info_interrupt_free_and_connection_error() -> None:
    """Introspección /object_info, /interrupt, /free y manejo de ComfyUIConnectionError."""
    state = ComfyUIStubState(healthy=True)
    stub_app = create_stub_comfyui_app(state)
    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app),
    )

    import asyncio

    async def _run() -> None:
        all_info = await client.get_object_info()
        assert "KSampler" in all_info
        assert "VHS_LoadVideo" in all_info

        ksampler_info = await client.get_object_info("KSampler")
        assert "KSampler" in ksampler_info

        int_res = await client.interrupt()
        assert int_res.get("interrupted") is True
        assert state.interrupted is True

        free_res = await client.free_memory(unload_models=True, free_memory=True)
        assert free_res.get("freed") is True
        assert state.freed_count == 1

    asyncio.run(_run())

    # Verificar ComfyUIConnectionError tras agotar reintentos
    async def conn_err_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Connection refused to {request.url}")

    async def fast_sleep(_d: float) -> None:
        return None

    bad_client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.MockTransport(conn_err_handler),
        max_retries=1,
        sleep_func=fast_sleep,
    )
    with pytest.raises(ComfyUIConnectionError):
        asyncio.run(bad_client.system_stats())
    assert asyncio.run(bad_client.health_check()) is False

    # Frames binarios de preview en /ws se ignoran sin lanzar excepción
    assert parse_ws_event(b"\x00\x01\xff\xd8binary-jpeg-preview") is None


def test_free_memory_accepts_comfyui_empty_success_response() -> None:
    """ComfyUI's /free endpoint responds with an empty HTTP 200 body."""
    import asyncio

    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=transport,
    )

    result = asyncio.run(client.free_memory(unload_models=True, free_memory=True))

    assert result == {"freed": True}


@pytest.mark.anyio
async def test_execute_prompt_job_history_succeeds_view_fails_marks_job_error_never_done(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """Regresión M2-SCR-002: si /history completa pero GET /view falla (404, 5xx o timeout),
    el job queda en status=error con SSE de error (nunca done) y sin salidas en galería.
    """
    async def no_sleep(_d: float) -> None:
        return None

    for mode in ("404", "503", "timeout"):
        state = ComfyUIStubState(
            healthy=True,
            poll_ticks_before_complete=1,
            fail_view_status=404 if mode == "404" else (503 if mode == "503" else None),
            fail_view_error=f"Output missing or unavailable ({mode})",
            fail_view_timeout=(mode == "timeout"),
        )
        stub_app = create_stub_comfyui_app(state)
        store = ProjectStore(studio_data_dir / f"case_{mode}")
        broker = ProgressBroker()
        project = store.create_project(name=f"View Failure {mode}")
        job = store.create_job(
            kind="render",
            project_id=project["id"],
            template_id="wan_t2v",
        )

        client = ComfyUIClient(
            base_url="http://stub-comfyui.local:39172",
            transport=httpx.ASGITransport(app=stub_app),
            store=store,
            broker=broker,
            timeout=0.05 if mode == "timeout" else 5.0,
            max_retries=2,
            sleep_func=no_sleep,
        )

        expected_exc = ComfyUITimeoutError if mode == "timeout" else ComfyUIError
        with pytest.raises(expected_exc):
            await client.execute_prompt_job(
                sample_workflow,
                job_id=job["id"],
                project_id=project["id"],
                template_id="wan_t2v",
                poll_interval=0.001,
                persist_outputs=True,
            )

        updated_job = store.get_job(job["id"])
        assert updated_job is not None
        assert updated_job["status"] == "error", (
            f"Job quedó en {updated_job['status']} para modo {mode}"
        )
        assert updated_job["error"]
        assert updated_job["prompt_id"] is not None

        events = broker.recent_events()
        done_events = [
            payload
            for ev_type, payload in events
            if ev_type in ("job", "progress") and payload.get("status") == "done"
        ]
        assert done_events == [], f"Se emitió SSE done indebido en modo {mode}: {done_events}"

        err_job_events = [
            payload
            for ev_type, payload in events
            if ev_type == "job" and payload.get("status") == "error"
        ]
        assert len(err_job_events) == 1
        assert err_job_events[0]["job_id"] == job["id"]
        assert err_job_events[0]["error"]

        output_assets = store.list_assets(project["id"], category="outputs")
        assert output_assets == []


@pytest.mark.anyio
async def test_execute_prompt_job_emits_done_only_after_outputs_persisted(
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """Completitud (done + SSE done) solo ocurre tras recuperar y persistir salidas."""
    state = ComfyUIStubState(healthy=True, poll_ticks_before_complete=1)
    stub_app = create_stub_comfyui_app(state)
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Completion Order Check")
    job = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
    )

    observed_assets_on_done: list[int] = []
    observed_db_status_on_done: list[str] = []
    orig_publish = broker.publish

    def spy_publish(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        if event_type == "job" and data.get("status") == "done":
            assets_now = store.list_assets(project["id"], category="outputs")
            observed_assets_on_done.append(len(assets_now))
            job_now = store.get_job(job["id"])
            observed_db_status_on_done.append(job_now["status"] if job_now else "missing")
        return orig_publish(event_type, data)

    broker.publish = spy_publish  # type: ignore[method-assign]

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app),
        store=store,
        broker=broker,
    )

    result = await client.execute_prompt_job(
        sample_workflow,
        job_id=job["id"],
        project_id=project["id"],
        template_id="wan_t2v",
        poll_interval=0.001,
        persist_outputs=True,
    )
    assert result.completed is True
    assert observed_assets_on_done == [len(result.outputs)]
    assert observed_db_status_on_done == ["done"]

    # Si la persistencia falla a mitad (ej. 2º archivo), se revierte el 1º y queda en error
    job_persist_fail = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
    )
    broker_fail = ProgressBroker()
    client_fail = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=httpx.ASGITransport(app=stub_app),
        store=store,
        broker=broker_fail,
    )
    orig_save_upload = store.save_upload
    call_count = 0

    def flaky_save_upload(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("Simulated disk full while persisting second output")
        return orig_save_upload(*args, **kwargs)

    store.save_upload = flaky_save_upload  # type: ignore[method-assign]
    try:
        with pytest.raises(ComfyUIError, match="persistiendo salidas"):
            await client_fail.execute_prompt_job(
                sample_workflow,
                job_id=job_persist_fail["id"],
                project_id=project["id"],
                template_id="wan_t2v",
                poll_interval=0.001,
                persist_outputs=True,
            )
    finally:
        store.save_upload = orig_save_upload  # type: ignore[method-assign]

    failed_persist_job = store.get_job(job_persist_fail["id"])
    assert failed_persist_job is not None
    assert failed_persist_job["status"] == "error"
    fail_events = broker_fail.recent_events()
    assert any(
        ev == "job" and p.get("status") == "error" and p.get("job_id") == job_persist_fail["id"]
        for ev, p in fail_events
    )
    assert not any(p.get("status") == "done" for _, p in fail_events)
    assert [
        a
        for a in store.list_assets(project["id"], category="outputs")
        if a.get("job_id") == job_persist_fail["id"]
    ] == []





# --------------------------------------------------------------------------
# Exclusión mutua del trabajo GPU pesado (política, punto 3)
# --------------------------------------------------------------------------


class _StubSubmission:
    prompt_id = "prompt-1"


class _StubResult:
    prompt_id = "prompt-1"
    outputs: list = []


def _gpu_client(**kwargs: object) -> ComfyUIClient:
    """Cliente sin transporte: los métodos GPU y la liberación se sustituyen."""
    import asyncio

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        poll_interval=0.01,
        poll_timeout=5.0,
        sleep_func=kwargs.pop("sleep_func", asyncio.sleep),
        **kwargs,  # type: ignore[arg-type]
    )

    async def _no_free(**__: object) -> dict:
        return {"freed": True}

    client.free_memory = _no_free
    return client


@pytest.mark.anyio
async def test_gpu_work_is_serialized_one_at_a_time(monkeypatch) -> None:
    """«Un trabajo GPU pesado a la vez»: dos renders nunca se solapan en GPU."""
    import asyncio

    from comfy_studio import comfyui_client as client_mod

    client = _gpu_client()

    state = {"active": 0, "peak": 0}

    async def _slow_submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _slow_poll(prompt_id: str, **_: object) -> object:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.05)
        finally:
            state["active"] -= 1
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _slow_submit)
    monkeypatch.setattr(client, "poll_until_complete", _slow_poll)
    monkeypatch.setattr(
        client_mod, "sample_system_stats_peak", _noop_sampler
    )

    await asyncio.gather(
        client.execute_prompt_job({"a": 1}),
        client.execute_prompt_job({"a": 2}),
        client.execute_prompt_job({"a": 3}),
    )
    assert state["peak"] == 1, "los renders GPU se solaparon"


async def _noop_sampler(client: object, *, stop: object, interval: float = 2.0) -> dict:
    # R65: el doble tiene que honrar el contrato del muestreador real que
    # introdujo R55. Desde entonces «medido» es «la muestra de cierre salió
    # bien» (`closing_ok`), no «entró al menos una muestra». Sin la clave, un
    # render que salió BIEN se marcaba `measured=False` y la fila se escribía
    # con `status="error"` y el motivo de drenaje perdido: el doble fabricaba
    # una medición perdida para un render correcto.
    return {
        "vram_peak_mb": None,
        "ram_peak_mb": None,
        "samples": 0,
        "closing_ok": True,
    }


@pytest.mark.anyio
async def test_a_successful_render_with_the_noop_sampler_records_ok(
    monkeypatch,
    studio_data_dir: Path,
) -> None:
    """R65: el doble `_noop_sampler` no puede fabricar una medición perdida.

    Desde R55 «medido» es «la muestra de cierre salió bien» (`closing_ok`). El
    doble devolvía un pico sin esa clave, así que `_drain_sampler` lo leía como
    `measured=False` y la fila se escribía con `status="error"` y el motivo de
    drenaje perdido... para un render que salió BIEN. Latente mientras nadie
    pasaba `template_id` (sin plantilla no se escribe fila). Aquí sí se pasa, y
    la fila tiene que quedar como lo que fue: una medición correcta.
    """
    import asyncio

    from comfy_studio import comfyui_client as client_mod

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Noop OK")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.02)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)

    await client.execute_prompt_job(
        {"a": 1},
        job_id=job["id"],
        project_id=project["id"],
        template_id="wan_t2v",
        poll_interval=0.01,
    )

    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok", (
        "el doble del muestreador fabricó una medición perdida para un render "
        "que salió bien"
    )
    assert rows[0]["error"] is None


@pytest.mark.anyio
async def test_gpu_gate_is_released_after_a_failure(monkeypatch) -> None:
    """Un render que falla no debe dejar el cerrojo cogido para siempre."""
    import asyncio

    from comfy_studio import comfyui_client as client_mod

    client = _gpu_client()

    async def _boom_submit(prompt: dict, **_: object) -> object:
        raise RuntimeError("ComfyUI rechazó el prompt")

    async def _ok_poll(prompt_id: str, **_: object) -> object:
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _boom_submit)
    monkeypatch.setattr(client, "poll_until_complete", _ok_poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await client.execute_prompt_job({"a": 1})

    # El cerrojo sigue utilizable: un render posterior entra sin quedarse colgado.
    async def _ok_submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    monkeypatch.setattr(client, "submit_prompt", _ok_submit)
    result = await asyncio.wait_for(client.execute_prompt_job({"a": 2}), timeout=5.0)
    assert result.prompt_id == "prompt-1"


@pytest.mark.anyio
async def test_gpu_gate_is_released_after_cancellation(monkeypatch) -> None:
    """Cancelar un render libera la reserva; el siguiente no queda bloqueado."""
    import asyncio

    from comfy_studio import comfyui_client as client_mod

    client = _gpu_client()

    async def _slow_submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _hang_poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(30)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _slow_submit)
    monkeypatch.setattr(client, "poll_until_complete", _hang_poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)

    task = asyncio.create_task(client.execute_prompt_job({"a": 1}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def _ok_poll(prompt_id: str, **_: object) -> object:
        return _StubResult()

    monkeypatch.setattr(client, "poll_until_complete", _ok_poll)
    result = await asyncio.wait_for(client.execute_prompt_job({"a": 2}), timeout=5.0)
    assert result.prompt_id == "prompt-1"


@pytest.mark.anyio
async def test_gpu_gate_can_be_injected() -> None:
    """La infraestructura existente puede compartir un único `_GpuGate`."""

    from comfy_studio import comfyui_client as client_mod
    from comfy_studio.comfyui_client import _GpuGate

    shared = _GpuGate()
    client = _gpu_client(gpu_gate=shared)

    async def _ok_submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _ok_poll(prompt_id: str, **_: object) -> object:
        return _StubResult()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(client, "submit_prompt", _ok_submit)
    monkeypatch.setattr(client, "poll_until_complete", _ok_poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)
    try:
        assert client._acquire_gpu_gate() is shared
        # Y se usa de verdad: el render pasa por el cerrojo inyectado.
        await client.execute_prompt_job({"a": 1})
        assert not shared.locked
    finally:
        monkeypatch.undo()


def test_a_foreign_semaphore_is_rejected_as_gpu_gate() -> None:
    """R5: un `asyncio.Semaphore` ajeno sigue siendo el peligro de la F1.

    Compartido entre el loop principal de la app y el `asyncio.run` de la
    orquestación se cuelga (el waiter del segundo loop no se despierta nunca).
    El constructor ya no lo acepta: el tipo no puede ser la única defensa.
    """
    import asyncio

    with pytest.raises(TypeError, match="_GpuGate"):
        ComfyUIClient(gpu_gate=asyncio.Semaphore(1))  # type: ignore[arg-type]


def _pending_tasks(*coro_names: str) -> list:
    """Tareas asyncio vivas cuyo coro contenga alguno de esos nombres."""
    import asyncio

    pending: list = []
    for task in asyncio.all_tasks():
        if task.done():
            continue
        qualname = getattr(task.get_coro(), "__qualname__", "")
        if any(name in qualname for name in coro_names):
            pending.append(task)
    return pending


def _stub_system_stats(vram_mb: dict[str, int], samples: list[int]) -> object:
    """`system_stats` de stub: publica la VRAM que marque `vram_mb["mb"]`."""

    async def _stats() -> dict:
        samples.append(vram_mb["mb"])
        return {
            "devices": [
                {
                    "name": "Stub GPU",
                    "vram_total": 24000 * 1024 * 1024,
                    "vram_used": vram_mb["mb"] * 1024 * 1024,
                }
            ],
            "system": {"ram_used": 8 * 1024 * 1024 * 1024},
        }

    return _stats


@pytest.mark.anyio
async def test_gpu_gate_excludes_renders_across_event_loops(monkeypatch) -> None:
    """El cerrojo no puede rehacerse por loop: producción usa dos.

    El loop principal de la app sirve los renders HTTP/agente y la
    orquestación ejecuta el motor con `asyncio.run` dentro de un threadpool.
    Un `asyncio.Semaphore` compartido entre ambos se cuelga; el cerrojo real
    (threading.Lock) los serializa.
    """
    import asyncio

    from comfy_studio import comfyui_client as client_mod

    client = _gpu_client()
    state = {"active": 0, "peak": 0}

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.05)
        finally:
            state["active"] -= 1
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)

    gate_before = client._acquire_gpu_gate()

    # Render del loop principal (HTTP / agente).
    main_task = asyncio.create_task(client.execute_prompt_job({"a": 1}))
    await asyncio.sleep(0.02)

    # Render de orquestación: OTRO event loop, dentro de un hilo del pool.
    errors: list[BaseException] = []

    def _orchestration_render() -> None:
        try:
            asyncio.run(client.execute_prompt_job({"a": 2}))
        except BaseException as exc:  # noqa: BLE001 - se comprueba abajo
            errors.append(exc)

    await asyncio.get_running_loop().run_in_executor(None, _orchestration_render)
    await main_task

    assert errors == [], f"el render de orquestación falló: {errors!r}"
    assert state["peak"] == 1, "dos renders GPU se solaparon entre loops distintos"
    # El cerrojo es el mismo antes y después: no se rehace por loop.
    assert client._acquire_gpu_gate() is gate_before


@pytest.mark.anyio
async def test_queued_render_measures_its_own_vram_and_duration(
    monkeypatch, studio_data_dir: Path
) -> None:
    """Un render en cola mide SU VRAM y SU tiempo, no los del anterior.

    El muestreo arrancaba antes de coger el cerrojo, así que medía la VRAM del
    render en curso mientras esperaba, y `duration_s` incluía la espera.
    """
    import asyncio

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Cola GPU")
    job_a = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )
    job_b = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    vram_mb = {"mb": 20000}
    samples: list[int] = []

    async def _submit(prompt: dict, *, job_id: str | None = None, **_: object) -> object:
        # Cada render deja su propia VRAM residente mientras trabaja.
        vram_mb["mb"] = 20000 if job_id == job_a["id"] else 1000
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.3 if vram_mb["mb"] == 20000 else 0.05)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    # El muestreador es el real: no se sustituye.
    monkeypatch.setattr(client, "system_stats", _stub_system_stats(vram_mb, samples))

    task_a = asyncio.create_task(
        client.execute_prompt_job(
            {"a": 1},
            job_id=job_a["id"],
            project_id=project["id"],
            template_id="wan_t2v",
            poll_interval=0.01,
        )
    )
    await asyncio.sleep(0.05)  # A ya está dentro del cerrojo
    samples_before_queue = len(samples)

    task_b = asyncio.create_task(
        client.execute_prompt_job(
            {"a": 2},
            job_id=job_b["id"],
            project_id=project["id"],
            template_id="wan_t2v",
            poll_interval=0.01,
        )
    )
    # B espera el cerrojo de A (~0.25 s más): mientras espera NO muestrea.
    await asyncio.sleep(0.1)
    assert len(samples) == samples_before_queue, (
        "el muestreador tomó muestras antes de adquirir el cerrojo"
    )

    await asyncio.gather(task_a, task_b)

    rows = store.list_render_measurements(template_id="wan_t2v")
    row_b = next(r for r in rows if r["job_id"] == job_b["id"])
    assert row_b["status"] == "ok"
    assert row_b["vram_peak_mb"] == 1000, "midió la VRAM del render anterior"
    assert row_b["duration_s"] is not None
    assert row_b["duration_s"] < 0.2, "duration_s incluyó la espera en cola"


@pytest.mark.anyio
async def test_cancelled_render_is_not_recorded_as_a_measurement(
    monkeypatch, studio_data_dir: Path
) -> None:
    """Un render cancelado se cancela: no se registra como medición correcta.

    `CancelledError` se tragaba en el `await` del muestreador, así que el job
    quedaba en `done` y se insertaba una fila `status='ok'` sin VRAM.
    """
    import asyncio

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Cancel GPU")
    job = store.create_job(kind="render", project_id=project["id"], template_id="wan_t2v")

    stats_entered = asyncio.Event()

    async def _hanging_stats() -> dict:
        stats_entered.set()
        await asyncio.sleep(30)
        return {"devices": [], "system": {}}

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        # Cede el control para que el muestreador arranque y se quede colgado
        # dentro de `system_stats`: la cancelación cae entonces en el drenaje.
        await asyncio.sleep(0.05)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    # Muestreador real, colgado dentro de `system_stats`: la cancelación cae
    # justo en el `await` del drenaje.
    monkeypatch.setattr(client, "system_stats", _hanging_stats)

    task = asyncio.create_task(
        client.execute_prompt_job(
            {"a": 1},
            job_id=job["id"],
            project_id=project["id"],
            template_id="wan_t2v",
        )
    )
    await stats_entered.wait()
    await asyncio.sleep(0.1)  # el poll acabó y el drenaje está en curso
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.list_render_measurements(template_id="wan_t2v") == []
    assert store.get_job(job["id"])["status"] != "done"
    await asyncio.sleep(0.05)
    assert _pending_tasks("sample_system_stats_peak") == [], (
        "el muestreador quedó colgado tras la cancelación"
    )


@pytest.mark.anyio
async def test_sampler_task_does_not_leak_when_the_render_fails(monkeypatch) -> None:
    """El drenaje del muestreador va en `finally`: un fallo también lo para."""
    import asyncio

    vram_mb = {"mb": 1000}
    samples: list[int] = []

    async def _boom_submit(prompt: dict, **_: object) -> object:
        raise RuntimeError("ComfyUI rechazó el prompt")

    async def _ok_poll(prompt_id: str, **_: object) -> object:
        return _StubResult()

    client = _gpu_client()
    monkeypatch.setattr(client, "submit_prompt", _boom_submit)
    monkeypatch.setattr(client, "poll_until_complete", _ok_poll)
    monkeypatch.setattr(client, "system_stats", _stub_system_stats(vram_mb, samples))

    with pytest.raises(RuntimeError):
        await client.execute_prompt_job({"a": 1})

    await asyncio.sleep(0.05)
    assert _pending_tasks("sample_system_stats_peak") == [], (
        "el muestreador sigue girando tras el fallo"
    )


@pytest.mark.anyio
async def test_free_memory_runs_inside_the_gpu_gate(
    monkeypatch, studio_data_dir: Path
) -> None:
    """`POST /free` se hace DENTRO del cerrojo: nunca toca la GPU de otro.

    El cliente lleva un `ProjectStore` REAL a propósito: con un cliente sin
    store, `_has_pending_work` falla cerrado y las tres llamadas a `/free` (y la
    aserción de `gate.locked`) saldrían por el camino de «no sé si queda
    trabajo», no por una determinación real de que la GPU está ociosa.
    """
    import asyncio

    from comfy_studio import comfyui_client as client_mod
    from comfy_studio.comfyui_client import _GpuGate

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    state = {"active": 0, "peak": 0}
    locked_at_free: list[bool] = []

    async def _free(**_: object) -> dict:
        gate = client._acquire_gpu_gate()
        assert isinstance(gate, _GpuGate)
        locked_at_free.append(gate.locked)
        return {"freed": True}

    client.free_memory = _free  # type: ignore[method-assign]

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.05)
        finally:
            state["active"] -= 1
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)

    await asyncio.gather(
        client.execute_prompt_job({"a": 1}),
        client.execute_prompt_job({"a": 2}),
        client.execute_prompt_job({"a": 3}),
    )

    assert state["peak"] == 1
    assert len(locked_at_free) == 3
    assert all(locked_at_free), "POST /free se llamó con el cerrojo ya soltado"


@pytest.mark.anyio
async def test_pending_work_check_uses_the_store_existence_query() -> None:
    """R3: el guard delega en la consulta de existencia del store.

    `list_jobs()` sin filtros materializaba TODOS los jobs y `_row_to_job` hace
    `json.loads` de `params_json`/`result_json` por fila; eso se ejecutaba bajo
    el cerrojo GPU y serializaba la cola detrás de un barrido de SQLite que crece
    con cada render. `ProjectStore.list_jobs` no acepta `limit`, así que el
    cliente no puede acotarlo: quien tiene que acotar es el store, con su propio
    `SELECT ... LIMIT 1`. Este store falla a propósito si alguien pide filas.
    """

    from comfy_studio.comfyui_client import ComfyUIClient

    class _ExistenceStore:
        """Store que SOLO sabe responder «¿queda trabajo?»."""

        def __init__(self, pending: bool) -> None:
            self.pending = pending
            self.calls: list[str | None] = []

        def has_pending_work(self, exclude_job_id: str | None = None) -> bool:
            self.calls.append(exclude_job_id)
            return self.pending

        def list_jobs(self, **_kwargs: object) -> list[dict]:
            raise AssertionError("el cliente materializó filas de jobs")

    client = _gpu_client(store=_ExistenceStore(pending=True))

    calls: list[dict] = []

    async def _free(**kwargs: object) -> dict:
        calls.append(kwargs)  # type: ignore[arg-type]
        return {"freed": True}

    client.free_memory = _free  # type: ignore[method-assign]

    assert client._has_pending_work() is True
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [], "con un job en vuelo no se descargan los modelos"

    # El render que acaba de terminar todavía no es terminal en el store: hay que
    # excluirlo o nunca se descargaría nada.
    await ComfyUIClient._release_gpu_when_idle(client, exclude_job_id="job-1")
    assert calls == [], "la exclusión no llegó al store"
    assert client._store.calls == [None, None, "job-1"]

    # Sin trabajo pendiente (ya excluido) SÍ se descarga.
    idle = _gpu_client(store=_ExistenceStore(pending=False))
    idle.free_memory = _free  # type: ignore[method-assign]
    await ComfyUIClient._release_gpu_when_idle(idle, exclude_job_id="job-1")
    assert calls[-1] == {"unload_models": True, "free_memory": True}


@pytest.mark.anyio
async def test_a_client_without_a_store_does_not_unload_models() -> None:
    """R53: sin store NO se puede saber si queda trabajo, así que no se descarga.

    Antes devolvía False («no queda nada») y un cliente sin store descargaba los
    modelos tras CADA render aunque la cola estuviera llena: el mismo lado
    inseguro de la R4, resuelto al revés. Sin store no hay conocimiento de la
    cola, y la duda se resuelve a favor de no tocar la GPU.
    """
    from comfy_studio.comfyui_client import ComfyUIClient

    client = _gpu_client()
    assert client._store is None

    calls: list[dict] = []

    async def _free(**kwargs: object) -> dict:
        calls.append(kwargs)  # type: ignore[arg-type]
        return {"freed": True}

    client.free_memory = _free  # type: ignore[method-assign]

    assert client._has_pending_work() is True
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [], "sin store se descargaron los modelos"


@pytest.mark.anyio
async def test_pending_work_check_fails_closed_on_a_store_without_the_query() -> None:
    """R3 + R4: un store que no implementa la consulta falla CERRADO.

    No se descarga nada: el lado inseguro es dar por hecho que la GPU está libre.
    El `AttributeError` de un store antiguo entra por el mismo camino que un
    `sqlite3.OperationalError` del store real.
    """

    from comfy_studio.comfyui_client import ComfyUIClient

    class _LegacyStore:
        def list_jobs(self, **_kwargs: object) -> list[dict]:
            return []

    client = _gpu_client(store=_LegacyStore())
    calls: list[dict] = []

    async def _free(**kwargs: object) -> dict:
        calls.append(kwargs)  # type: ignore[arg-type]
        return {"freed": True}

    client.free_memory = _free  # type: ignore[method-assign]

    assert client._has_pending_work() is True
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [], "sin poder consultar el store se descargaron los modelos"


@pytest.mark.anyio
async def test_store_has_pending_work_is_bounded_and_excludes_the_job(
    studio_data_dir: Path,
) -> None:
    """R3: la consulta del store es de existencia, no un barrido de filas.

    Con 400 jobs terminales alrededor (cada uno con `params_json` de 2 KB, que
    es lo que `_row_to_job` interpreta), `has_pending_work` sigue respondiendo
    SIN materializar ninguna fila: `_row_to_job` se hace explotar a propósito y
    la consulta no lo nota.
    """

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Cola Larga")
    big_params = {"blob": "x" * 2048}
    history = [
        store.create_job(
            kind="render",
            project_id=project["id"],
            template_id="wan_t2v",
            params=big_params,
            status="done",
        )
        for _ in range(400)
    ]
    running = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )
    queued = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
        status="paused",
    )

    # A partir de aquí, materializar una fila de job es un fallo del test.
    # `_row_to_job` es un `staticmethod`: se restaura el descriptor original, no
    # la función envuelta, para no romper los tests siguientes.
    original = ProjectStore.__dict__["_row_to_job"]

    def _explode(row: object) -> dict:
        raise AssertionError("has_pending_work materializó filas de jobs")

    ProjectStore._row_to_job = staticmethod(_explode)  # type: ignore[assignment]
    try:
        assert store.has_pending_work() is True, "un job en vuelo no se vio"
        # El render que acaba de terminar se excluye, pero queda el otro.
        assert store.has_pending_work(running["id"]) is True
        assert store.has_pending_work(f"{running['id']}-no-existe") is True
    finally:
        ProjectStore._row_to_job = original  # type: ignore[assignment]

    # Con TODOS los jobs terminales no queda nada (excluyendo los dos en vuelo).
    for job in (running, queued):
        store.update_job(job["id"], status="done")
    assert store.has_pending_work() is False
    assert store.has_pending_work(running["id"]) is False
    assert len(history) == 400


@pytest.mark.anyio
async def test_pending_work_check_fails_closed_when_the_store_breaks() -> None:
    """R4: si el store falla se asume que SÍ queda trabajo.

    El store usa `timeout=15.0` + WAL, así que un escritor concurrente (hilo de
    orquestación, reconcile, jobs de ads) puede lanzar `sqlite3.OperationalError`.
    Devolver «no queda nada» en ese camino significaba descargar modelos dando por
    hecho que no había nada en vuelo.
    """
    import sqlite3

    from comfy_studio.comfyui_client import ComfyUIClient

    class _BrokenStore:
        def has_pending_work(self, exclude_job_id: str | None = None) -> bool:
            raise sqlite3.OperationalError("database is locked")

    client = _gpu_client(store=_BrokenStore())

    calls: list[dict] = []

    async def _free(**kwargs: object) -> dict:
        calls.append(kwargs)  # type: ignore[arg-type]
        return {"freed": True}

    client.free_memory = _free  # type: ignore[method-assign]

    assert client._has_pending_work() is True
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [], "con el store roto no se descargan los modelos"


@pytest.mark.anyio
async def test_a_broken_system_stats_payload_does_not_break_a_finished_render(
    monkeypatch,
    studio_data_dir: Path,
    sample_workflow: dict[str, Any],
) -> None:
    """R1: el muestreador revienta, pero el render YA había terminado.

    `sample_system_stats_peak` interpreta el payload de `/system_stats` FUERA de
    su propio `try`, así que un `vram_used` no numérico hace que la tarea termine
    con `TypeError`. Si el drenaje solo capturara `TimeoutError`, esa excepción
    saldría de `execute_prompt_job`: el job se quedaría en `queued`, no habría
    fila de medición, las salidas no se descargarían ni persistirían y el cerrojo
    quedaría suelto. `engines/comfyui.py` tampoco la captura, así que el grafo de
    orquestación se caería en lugar de recibir `EngineResult(ok=False)`.
    """
    import asyncio

    state = ComfyUIStubState(healthy=True, poll_ticks_before_complete=1)
    transport = httpx.ASGITransport(app=create_stub_comfyui_app(state))
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Sampler Roto")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    async def _broken_stats() -> dict:
        return {
            "devices": [
                {
                    "name": "Stub GPU",
                    "vram_total": 24000 * 1024 * 1024,
                    "vram_used": "no-es-un-numero",
                }
            ],
            "system": {},
        }

    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=transport,
        store=store,
        poll_interval=0.01,
        poll_timeout=5.0,
    )
    monkeypatch.setattr(client, "system_stats", _broken_stats)

    result = await asyncio.wait_for(
        client.execute_prompt_job(
            sample_workflow,
            job_id=job["id"],
            project_id=project["id"],
            template_id="wan_t2v",
            persist_outputs=True,
        ),
        timeout=10.0,
    )

    assert result.completed is True
    assert store.get_job(job["id"])["status"] == "done"
    assert len(store.list_assets(project["id"], category="outputs")) == len(
        result.outputs
    )
    # La medición se registra igual: sin VRAM, pero sin tumbarse. Y NO como
    # buena: el muestreador reventó, así que la fila queda fuera del filtro
    # `status == "ok"` de `recipe_for_template` y lleva el motivo.
    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 1
    assert rows[0]["status"] != "ok"
    assert rows[0]["vram_peak_mb"] is None
    assert rows[0]["error"]


@pytest.mark.anyio
async def test_a_render_shorter_than_the_sample_interval_records_its_own_peak(
    monkeypatch,
    studio_data_dir: Path,
) -> None:
    """R6: un render de ~0.1 s medía la VRAM ociosa, no su propio pico.

    La primera muestra del muestreador sale junto al `submit_prompt`, antes de que
    ComfyUI cargue los modelos, y con `interval=2.0` un render corto no llega a
    tomar una segunda: el pico persistido era el valor ocioso. El muestreador toma
    ahora una última muestra al parar, y el cliente la espera sin soltar el
    cerrojo.
    """
    import asyncio

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Render Corto")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    vram_mb = {"mb": 100}  # VRAM ociosa: aún no se ha cargado nada
    samples: list[int] = []

    async def _submit(prompt: dict, **_: object) -> object:
        # El POST tarda: el muestreador toma su primera muestra AHORA.
        await asyncio.sleep(0.01)
        vram_mb["mb"] = 9000  # ComfyUI carga los modelos
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.05)  # render más corto que el intervalo de 2 s
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    # El muestreador es el REAL: no se sustituye.
    monkeypatch.setattr(client, "system_stats", _stub_system_stats(vram_mb, samples))

    await client.execute_prompt_job(
        {"a": 1},
        job_id=job["id"],
        project_id=project["id"],
        template_id="wan_t2v",
        poll_interval=0.01,
    )

    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 1
    assert rows[0]["vram_peak_mb"] == 9000, "midió la VRAM ociosa del render corto"
    assert rows[0]["duration_s"] is not None
    assert rows[0]["duration_s"] < 1.0


@pytest.mark.anyio
async def test_cancellation_releases_the_gpu_gate_without_waiting_for_the_sampler(
    monkeypatch,
) -> None:
    """R2: el drenaje del muestreador no puede retener el cerrojo.

    El drenaje iba en el `finally` dentro del `async with`, así que cancelar un
    render con el muestreador colgado se quedaba esperando los 5 s del drenaje
    ANTES de soltar el cerrojo: durante el apagado de la app (que reúne las tareas
    de render canceladas) eso se acumulaba.
    """
    import asyncio
    import time

    from comfy_studio import comfyui_client as client_mod

    client = _gpu_client()
    stats_entered = asyncio.Event()

    async def _hanging_stats() -> dict:
        stats_entered.set()
        await asyncio.sleep(30)
        return {"devices": [], "system": {}}

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await stats_entered.wait()
        await asyncio.sleep(30)  # el render sigue en curso
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client, "system_stats", _hanging_stats)

    task = asyncio.create_task(client.execute_prompt_job({"a": 1}))
    await stats_entered.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"la cancelación tardó {elapsed:.2f} s en salir"

    # El cerrojo quedó libre: el siguiente render entra sin esperar al muestreador.
    gate = client._acquire_gpu_gate()
    assert await asyncio.wait_for(gate.acquire(timeout=1.0), timeout=2.0) is True
    gate.release()

    async def _ok_poll(prompt_id: str, **_: object) -> object:
        return _StubResult()

    monkeypatch.setattr(client, "poll_until_complete", _ok_poll)
    monkeypatch.setattr(client, "system_stats", _stub_system_stats({"mb": 1000}, []))
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)
    result = await asyncio.wait_for(client.execute_prompt_job({"a": 2}), timeout=5.0)
    assert result.prompt_id == "prompt-1"


@pytest.mark.anyio
async def test_a_hung_free_does_not_hold_the_gpu_queue(
    monkeypatch, studio_data_dir: Path
) -> None:
    """R2: `POST /free` colgado no secuestra la GPU de la cola.

    El POST se hace con el cerrojo cogido (F3: no puede coincidir con el cómputo
    de otro render) pero con espera acotada para entrar y presupuesto propio para
    el POST. Antes el camino largo eran `max_retries=3` con backoff de hasta 5 s y
    30 s por intento: ~2 minutos de GPU secuestrada con toda la cola girando.

    Lo que se prueba AQUÍ es el mecanismo, con el presupuesto acortado para no
    esperar 5 s de verdad. El límite de producción se mide aparte, con la
    constante real, en `test_the_free_budget_that_stalls_the_queue_is_the_real_one`.
    """
    import asyncio

    from comfy_studio import comfyui_client as client_mod

    # El presupuesto real se mide en producción; aquí se acorta para no esperar
    # 5 s de verdad. Lo que se prueba es el mecanismo: el POST está acotado.
    monkeypatch.setattr(client_mod, "_FREE_MEMORY_BUDGET_S", 0.3)

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    free_entered = asyncio.Event()

    async def _hung_free(**_: object) -> dict:
        free_entered.set()
        await asyncio.sleep(30)
        return {"freed": True}

    client.free_memory = _hung_free  # type: ignore[method-assign]

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.02)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)

    first = await asyncio.wait_for(client.execute_prompt_job({"a": 1}), timeout=5.0)
    assert free_entered.is_set(), "no se intentó descargar los modelos"
    # El render siguiente no espera al presupuesto de reintentos de `/free`.
    second = await asyncio.wait_for(client.execute_prompt_job({"a": 2}), timeout=5.0)
    assert first.prompt_id == second.prompt_id == "prompt-1"


@pytest.mark.anyio
async def test_the_free_budget_that_stalls_the_queue_is_the_real_one(
    studio_data_dir: Path,
) -> None:
    """R52: el límite que se afirma es el de producción, no uno acortado.

    `test_a_hung_free_does_not_hold_the_gpu_queue` acorta `_FREE_MEMORY_BUDGET_S`
    a 0.3 s para ser rápido, así que por sí solo NO dice cuánto retiene la GPU un
    `/free` colgado de verdad. Aquí se usa la constante REAL y se mide: el
    cerrojo se suelta dentro de ese presupuesto, y el render siguiente entra
    justo después. Con las constantes de antes del arreglo (~2 minutos de
    presupuesto de reintentos) este test no cabría en ningún timeout razonable.
    """
    import asyncio
    import time

    from comfy_studio import comfyui_client as client_mod

    budget = client_mod._FREE_MEMORY_BUDGET_S
    assert 0.0 < budget <= 5.0, (
        f"el presupuesto de /free es {budget} s: la cola esperaría demasiado"
    )
    assert client_mod._GPU_UNLOAD_WAIT_S <= budget, (
        "se espera más por el cerrojo que por el POST: el orden está invertido"
    )

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    free_entered = asyncio.Event()

    async def _hung_free(**_: object) -> dict:
        free_entered.set()
        await asyncio.sleep(budget * 10)
        return {"freed": True}

    client.free_memory = _hung_free  # type: ignore[method-assign]

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.02)
        return _StubResult()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client_mod, "sample_system_stats_peak", _noop_sampler)
    try:
        started = time.monotonic()
        await asyncio.wait_for(
            client.execute_prompt_job({"a": 1}), timeout=budget + 5.0
        )
        elapsed = time.monotonic() - started
        assert free_entered.is_set(), "no se intentó descargar los modelos"
        assert elapsed <= budget + 1.0, (
            f"un /free colgado retuvo la GPU {elapsed:.2f} s "
            f"(presupuesto real: {budget} s)"
        )

        # El render siguiente espera como mucho ese mismo presupuesto.
        started = time.monotonic()
        second = await asyncio.wait_for(
            client.execute_prompt_job({"a": 2}), timeout=budget + 5.0
        )
        waited = time.monotonic() - started
        assert second.prompt_id == "prompt-1"
        assert waited <= budget + 1.0, (
            f"la cola esperó {waited:.2f} s detrás del /free colgado"
        )
    finally:
        monkeypatch.undo()


def test_the_unload_docstring_states_one_bound() -> None:
    """R57: el docstring de `_unload_after_render` prometía dos cosas opuestas.

    Afirmaba «la cola nunca espera detrás de una descarga» y seis líneas más
    abajo la retiraba («No es «la cola nunca espera»: es «la cola espera como
    máximo el presupuesto»»). El código siempre hizo la segunda: el POST se hace
    CON el cerrojo cogido, así que retiene la GPU `_FREE_MEMORY_BUDGET_S` y eso
    está medido en `test_the_free_budget_that_stalls_the_queue_is_the_real_one`.
    La frase sobrante se ha borrado: el docstring declara UN solo límite.
    """
    from comfy_studio.comfyui_client import ComfyUIClient

    # El docstring está envuelto a 100 columnas: se normaliza el espacio para no
    # depender de por dónde corta la línea.
    doc = " ".join((ComfyUIClient._unload_after_render.__doc__ or "").split())
    assert "y la cola nunca espera detrás de una descarga" not in doc, (
        "el docstring vuelve a prometer que la cola no espera nunca"
    )
    assert "la cola espera como máximo el presupuesto" in doc, (
        "el docstring perdió la retracción, que es el límite real"
    )


@pytest.mark.anyio
async def test_a_slow_system_stats_does_not_inflate_the_measured_duration(
    monkeypatch,
    studio_data_dir: Path,
) -> None:
    """R48: `duration_s` mide el trabajo GPU, no el drenaje del muestreador.

    Medido con un `ProjectStore` real: 20 ms de GPU y un `/system_stats` que
    tarda 1.5 s daban `duration_s = 2.03`. Ese número alimentaba
    `duration_s_avg/_max/_last` de `recipe_for_template`, el modelo de coste y la
    herramienta `render_recipes` del agente: la receta cobraba un render 100
    veces más largo de lo que fue.
    """
    import asyncio
    import time

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Drenaje Lento")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    vram_mb = {"mb": 100}
    first_call = {"done": False}

    async def _slow_stats() -> dict:
        # Solo la PRIMERA llamada es lenta: es la que pilla el drenaje, porque
        # el muestreador arranca junto al `submit_prompt`.
        if not first_call["done"]:
            first_call["done"] = True
            await asyncio.sleep(1.5)
        samples.append(vram_mb["mb"])
        return {
            "devices": [
                {
                    "name": "Stub GPU",
                    "vram_total": 24000 * 1024 * 1024,
                    "vram_used": vram_mb["mb"] * 1024 * 1024,
                }
            ],
            "system": {"ram_used": 8 * 1024 * 1024 * 1024},
        }

    samples: list[int] = []

    async def _submit(prompt: dict, **_: object) -> object:
        await asyncio.sleep(0.01)
        vram_mb["mb"] = 9000  # ComfyUI carga los modelos
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.02)  # el trabajo GPU dura 20 ms
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client, "system_stats", _slow_stats)

    started = time.monotonic()
    await client.execute_prompt_job(
        {"a": 1},
        job_id=job["id"],
        project_id=project["id"],
        template_id="wan_t2v",
        poll_interval=0.01,
    )
    elapsed = time.monotonic() - started

    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 1
    row = rows[0]
    # El render entero SÍ esperó al muestreador: el drenaje ocurrió.
    assert elapsed > 1.4, f"el drenaje no ocurrió ({elapsed:.2f} s)"
    # ...pero el tiempo medido es el del trabajo GPU, no el del drenaje.
    assert row["duration_s"] is not None
    assert row["duration_s"] < 0.5, (
        f"duration_s = {row['duration_s']} s incluyó el drenaje "
        f"({elapsed:.2f} s de render)"
    )
    assert elapsed - row["duration_s"] > 1.0, "el drenaje no se excluyó"
    # R6 sigue en pie: el pico medido es el de ESTE render, no el ocioso.
    assert row["vram_peak_mb"] == 9000
    assert row["status"] == "ok"


@pytest.mark.anyio
async def test_a_hung_system_stats_does_not_hold_the_gpu_gate(
    monkeypatch,
    studio_data_dir: Path,
) -> None:
    """R49: en el camino CORRECTO el drenaje también retiene el cerrojo.

    El muestreador se drena dentro del span GPU (F2/R6: su última muestra es la
    que cierra la ventana), así que un `/system_stats` colgado mantiene la GPU
    `_SAMPLER_DRAIN_TIMEOUT_S` — no para siempre. La medición se pierde, pero el
    render siguiente entra en seguida.
    """
    import asyncio
    import time

    from comfy_studio import comfyui_client as client_mod

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    stats_entered = asyncio.Event()

    async def _hanging_stats() -> dict:
        stats_entered.set()
        await asyncio.sleep(30)
        return {"devices": [], "system": {}}

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.02)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client, "system_stats", _hanging_stats)

    started = time.monotonic()
    await asyncio.wait_for(
        client.execute_prompt_job({"a": 1}),
        timeout=client_mod._SAMPLER_DRAIN_TIMEOUT_S + 5.0,
    )
    held = time.monotonic() - started
    assert held <= client_mod._SAMPLER_DRAIN_TIMEOUT_S + 1.0, (
        f"el cerrojo se retuvo {held:.2f} s con /system_stats colgado"
    )

    # El siguiente render no espera al muestreador colgado: el muestreador es
    # nuevo, así que vale con que `/system_stats` responda ya.
    monkeypatch.setattr(
        client, "system_stats", _stub_system_stats({"mb": 1000}, [])
    )
    started = time.monotonic()
    second = await asyncio.wait_for(
        client.execute_prompt_job({"a": 2}), timeout=5.0
    )
    waited = time.monotonic() - started
    assert second.prompt_id == "prompt-1"
    assert waited < client_mod._SAMPLER_DRAIN_TIMEOUT_S, (
        f"el render siguiente esperó {waited:.2f} s al muestreador colgado"
    )
    await asyncio.sleep(0.05)
    assert _pending_tasks("sample_system_stats_peak") == [], (
        "el muestreador colgado quedó girando tras el drenaje"
    )


@pytest.mark.anyio
async def test_a_lost_measurement_is_not_recorded_as_a_good_one(
    monkeypatch,
    studio_data_dir: Path,
) -> None:
    """R50: una medición perdida NO se escribe con `status="ok"`.

    `recipe_for_template` filtra por `status == "ok"`, así que una fila sin VRAM
    marcada como buena hacía que la receta y `assess_vram_fit` razonaran sin
    dato, sin un solo aviso. Ahora tiene su propio estado y la receta la ignora.
    """
    import asyncio

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Muestra Perdida")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    async def _broken_stats() -> dict:
        return {
            "devices": [
                {
                    "name": "Stub GPU",
                    "vram_total": 24000 * 1024 * 1024,
                    "vram_used": "no-es-un-numero",
                }
            ],
            "system": {},
        }

    async def _submit(prompt: dict, **_: object) -> object:
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.02)
        return _StubResult()

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client, "system_stats", _broken_stats)

    await client.execute_prompt_job(
        {"a": 1},
        job_id=job["id"],
        project_id=project["id"],
        template_id="wan_t2v",
        poll_interval=0.01,
    )

    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 1
    assert rows[0]["status"] != "ok", (
        "una medición perdida se registró como buena"
    )
    assert rows[0]["vram_peak_mb"] is None
    assert rows[0]["error"]

    # La receta no puede tratar esa fila como una muestra válida.
    recipe = store.recipe_for_template("wan_t2v")
    assert recipe is not None
    assert recipe["measured"] is False
    assert recipe["samples"] == 1
    assert "vram_peak_mb_max" not in recipe


@pytest.mark.anyio
async def test_a_fetching_job_counts_as_pending_work_for_the_real_store(
    studio_data_dir: Path,
) -> None:
    """R51: «fetching» es un job en vuelo, y los desconocidos también.

    `recovery.py` y `routers/core.py` tratan «fetching» como activo. La consulta
    del store es ahora una lista blanca de estados TERMINALES, así que un estado
    que no esté enumerado queda como «en vuelo»: es el sentido seguro.
    """
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Fetching")
    fetching = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
        status="fetching",
    )
    unknown = store.create_job(
        kind="render",
        project_id=project["id"],
        template_id="wan_t2v",
        status="estado-que-nadie-enumero",
    )
    # R54: «failed» es terminal. Lo escriben `routers/ads.py` (tres rutas, tras
    # `AdPipelineError`) y `agent/tools.py` (motor de ads), y ninguno de ellos
    # vuelve a tocar el job.
    failed = store.create_job(
        kind="ad_variants",
        project_id=project["id"],
        status="failed",
    )
    assert store.has_pending_work() is True, "«fetching» no contó como en vuelo"
    assert store.has_pending_work(fetching["id"]) is True, (
        "el job desconocido no contó como en vuelo"
    )
    assert store.has_pending_work(unknown["id"]) is True, (
        "«fetching» dejó de contar al excluir el otro job"
    )
    # El job de ads fallado NO queda en vuelo para siempre.
    for job in (fetching, unknown):
        store.update_job(job["id"], status="done")
    assert store.has_pending_work() is False, (
        "«failed» no es terminal: un job de ads fallado apagaría POST /free "
        "para el resto de la vida del pod"
    )
    assert store.has_pending_work(failed["id"]) is False


@pytest.mark.anyio
async def test_a_failed_ad_job_does_not_disable_the_free_call(
    studio_data_dir: Path,
) -> None:
    """R54: la consecuencia de que «failed» no fuera terminal.

    `POST /free` solo se llama si `has_pending_work` da False. Con un job
    `kind="ad_variants"` en `failed` —terminal, y escrito así por las rutas de
    ads después de un `AdPipelineError`— la consulta respondía «sigue habiendo
    trabajo» para siempre, así que la liberación de VRAM dejaba de ocurrir en
    producción sin que nadie lo registrara. `exclude_job_id` solo quita el render
    que acaba: el job de ads fallado se quedaba.
    """
    from comfy_studio.comfyui_client import ComfyUIClient

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Ads Fallidos")
    render = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )
    store.create_job(kind="ad_variants", project_id=project["id"], status="failed")

    client = _gpu_client(store=store)
    calls: list[dict] = []

    async def _free(**kwargs: object) -> dict:
        calls.append(kwargs)  # type: ignore[arg-type]
        return {"freed": True}

    client.free_memory = _free  # type: ignore[method-assign]

    assert client._has_pending_work(exclude_job_id=render["id"]) is False
    await ComfyUIClient._release_gpu_when_idle(client, exclude_job_id=render["id"])
    assert calls == [{"unload_models": True, "free_memory": True}], (
        "un job de ads fallado mantuvo has_pending_work en True para siempre"
    )


def test_the_terminal_set_and_the_in_flight_vocabulary_do_not_drift() -> None:
    """R54: `TERMINAL_STATUSES` y el vocabulario en vuelo son complementarios.

    Tres lectores dependen de esa partición y ninguno puede imponerse a los
    otros: `ProjectStore.has_pending_work` (lista blanca de terminales),
    `recovery.py` (`STALE_STATUSES`, lo que se recupera tras un reinicio) y
    `routers/core.py` (`jobs_active`, lo que mira el watchdog de idle). Un
    estado terminal que se olvidara en la lista blanca —`failed`— apagaba
    `POST /free` sin avisar.

    `routers/core.py` mantiene su propia copia literal del vocabulario, así que
    se lee del fuente: si las dos copias se separan, falla aquí.
    """
    import ast

    from comfy_studio import projects as projects_module
    from comfy_studio.recovery import STALE_STATUSES
    from comfy_studio.routers import core as core_module

    terminal = set(projects_module.TERMINAL_STATUSES)
    in_flight = set(STALE_STATUSES)

    core_source = Path(core_module.__file__).read_text(encoding="utf-8")
    core_vocabularies = [
        {elt.value for elt in node.elts}
        for node in ast.walk(ast.parse(core_source))
        if isinstance(node, ast.Tuple)
        and node.elts
        and all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in node.elts)
    ]
    assert in_flight in core_vocabularies, (
        "routers/core.py ya no enumera el vocabulario en vuelo de recovery.py: "
        "el watchdog de idle y la recuperación mirarían estados distintos"
    )
    assert terminal.isdisjoint(in_flight), (
        "un estado no puede ser terminal y en vuelo a la vez"
    )
    # La partición tiene que cubrir el vocabulario REAL de estados de job. Un
    # estado nuevo en cualquiera de los dos lados entra aquí o es desconocido
    # (y los desconocados cuentan como en vuelo, que es el sentido seguro).
    assert terminal | in_flight == {
        "done",
        "error",
        "cancelled",
        "interrupted",
        "failed",
        "queued",
        "running",
        "cancelling",
        "fetching",
    }



@pytest.mark.anyio
async def test_a_failed_closing_sample_is_not_a_measurement(
    monkeypatch,
    studio_data_dir: Path,
) -> None:
    """R55: la muestra de cierre es la única que ve el pico real.

    La primera muestra sale junto al `submit_prompt`, es decir ANTES de que
    ComfyUI cargue los modelos: su valor es el ocioso. Si la muestra de cierre
    falla, el pico que queda es ese valor ocioso, y con `measured = samples >= 1`
    la fila se escribía con `status="ok"`. De ahí salía un veredicto de encaje
    inventado: `vram_peak_mb_max` con la VRAM ociosa, `vram_fit.fits=True` y
    headroom de ~96 GB, alimentando el modelo de coste y `render_recipes`.
    """
    import asyncio

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Cierre Roto")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    vram_mb = {"mb": 100}  # VRAM ociosa: aún no se ha cargado nada
    samples: list[int] = []

    async def _submit(prompt: dict, **_: object) -> object:
        # El POST tarda: el muestreador toma su primera muestra AHORA, ociosa.
        await asyncio.sleep(0.01)
        vram_mb["mb"] = 9000  # ComfyUI carga los modelos
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.05)  # render más corto que el intervalo de 2 s
        return _StubResult()

    async def _stats() -> dict:
        samples.append(vram_mb["mb"])
        if len(samples) > 1:
            # La muestra de cierre —la que cierra la ventana de medición— falla.
            raise OSError("ComfyUI no responde al cerrar la medición")
        return {
            "devices": [
                {
                    "name": "Stub GPU",
                    "vram_total": 24000 * 1024 * 1024,
                    "vram_used": vram_mb["mb"] * 1024 * 1024,
                }
            ],
            "system": {"ram_used": 8 * 1024 * 1024 * 1024},
        }

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    # El muestreador es el REAL: no se sustituye.
    monkeypatch.setattr(client, "system_stats", _stats)

    await client.execute_prompt_job(
        {"a": 1},
        job_id=job["id"],
        project_id=project["id"],
        template_id="wan_t2v",
        poll_interval=0.01,
    )

    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 1
    assert len(samples) >= 2, "la muestra de cierre no se llegó a tomar"
    # El pico que quedó es el OCIOSO, y por eso no puede marcarse como bueno.
    assert rows[0]["vram_peak_mb"] == 100
    assert rows[0]["status"] != "ok", (
        "una muestra de cierre fallida dejó la fila marcada como medición buena"
    )
    assert rows[0]["error"]

    # La receta no juzga el encaje con el valor ocioso.
    recipe = store.recipe_for_template("wan_t2v")
    assert recipe is not None
    assert recipe["measured"] is False
    assert "vram_peak_mb_max" not in recipe
    assert "vram_fit" not in recipe


@pytest.mark.anyio
async def test_a_lost_measurement_is_announced_above_debug(
    monkeypatch,
    studio_data_dir: Path,
    caplog,
) -> None:
    """R66: una medición perdida deja rastro en el log, no solo en la fila.

    La muestra de cierre puede fallar sin ninguna excepción visible: devuelve
    `closing_ok=False` y el camino acaba normal (job `done`, render dado por
    bueno). El único rastro era un `logger.debug`, que producción descarta
    (uvicorn corre con `log_level='info'`), así que el operador no se enteraba
    de que la VRAM/RAM de este render no se midió. Ahora se emite un WARNING que
    lleva `_DRAIN_LOST_REASON`, el mismo motivo que la fila.
    """
    import asyncio
    import logging

    from comfy_studio import comfyui_client as client_mod

    store = ProjectStore(studio_data_dir)
    client = _gpu_client(store=store)
    project = store.create_project(name="Cierre Roto Log")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="wan_t2v"
    )

    vram_mb = {"mb": 100}
    samples: list[int] = []

    async def _submit(prompt: dict, **_: object) -> object:
        await asyncio.sleep(0.01)
        vram_mb["mb"] = 9000
        return _StubSubmission()

    async def _poll(prompt_id: str, **_: object) -> object:
        await asyncio.sleep(0.05)
        return _StubResult()

    async def _stats() -> dict:
        samples.append(vram_mb["mb"])
        if len(samples) > 1:
            # La muestra de cierre —la única que ve el pico real— falla, pero
            # SIN ruido: `sample_system_stats_peak` la captura y devuelve el
            # pico ocioso con `closing_ok=False`.
            raise OSError("ComfyUI no responde al cerrar la medición")
        return {
            "devices": [
                {
                    "name": "Stub GPU",
                    "vram_total": 24000 * 1024 * 1024,
                    "vram_used": vram_mb["mb"] * 1024 * 1024,
                }
            ],
            "system": {"ram_used": 8 * 1024 * 1024 * 1024},
        }

    monkeypatch.setattr(client, "submit_prompt", _submit)
    monkeypatch.setattr(client, "poll_until_complete", _poll)
    monkeypatch.setattr(client, "system_stats", _stats)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.comfyui_client"):
        await client.execute_prompt_job(
            {"a": 1},
            job_id=job["id"],
            project_id=project["id"],
            template_id="wan_t2v",
            poll_interval=0.01,
        )

    # La fila dice que la medición se perdió...
    rows = store.list_render_measurements(template_id="wan_t2v")
    assert rows[0]["status"] != "ok"
    # ...y el log lo dice TAMBIÉN, por encima de DEBUG y con el mismo motivo,
    # para que el log y la fila no se contradigan.
    announced = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and client_mod._DRAIN_LOST_REASON in r.getMessage()
    ]
    assert announced, (
        "una medición perdida no dejó ningún WARNING: el operador no la vería "
        "en producción (log_level='info')"
    )


@pytest.mark.anyio
async def test_render_recipes_publishes_why_a_measurement_is_missing(
    studio_data_dir: Path,
    studio_config,
) -> None:
    """R56: `status: "error"` en una medición no significa «el render falló».

    El render salió bien y lo que se perdió fue la medición (muestreador
    agotado o roto). `render_recipes` publicaba el estado y no el motivo, así que
    un agente o un operador leían «error» sin explicación y concluían que el
    render había fallado.
    """
    from comfy_studio.agent.tools import (
        RenderRecipesArgs,
        ToolContext,
        _handle_render_recipes,
    )
    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.progress import ProgressBroker
    from comfy_studio.projects import MEASUREMENT_STATUS_LOST, MEASUREMENT_STATUS_OK

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Recetas")
    store.record_render_measurement(
        template_id="wan_t2v",
        project_id=project["id"],
        duration_s=12.5,
        vram_peak_mb=38000,
        status=MEASUREMENT_STATUS_OK,
    )
    store.record_render_measurement(
        template_id="wan_t2v",
        project_id=project["id"],
        duration_s=11.0,
        status=MEASUREMENT_STATUS_LOST,
        error="muestreador de /system_stats perdido",
    )
    context = ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=ComfyUIClient(
            base_url="http://stub-comfyui.local:39172", transport=None
        ),
        broker=ProgressBroker(),
        config=studio_config,
    )

    payload = await _handle_render_recipes(
        RenderRecipesArgs(template_id="wan_t2v"), context
    )
    by_status = {m["measurement_status"]: m for m in payload["measurements"]}

    assert by_status["measured"]["error"] is None
    assert by_status["measured"]["vram_peak_mb"] == 38000
    lost = by_status["lost"]
    assert lost["status"] == MEASUREMENT_STATUS_LOST
    assert lost["error"] == "muestreador de /system_stats perdido", (
        "la herramienta publica el estado pero no el motivo de la medición perdida"
    )
    # La receta mide con la muestra buena y la perdida queda fuera del filtro.
    assert payload["recipes"][0]["measured"] is True


@pytest.mark.anyio
async def test_an_unmeasured_recipe_publishes_the_reason(
    studio_data_dir: Path,
) -> None:
    """R67: una receta sin medición publica el PORQUÉ, no solo `measured: False`.

    `recipe_for_template` devolvía `{..., measured: False}` sin motivo, así que
    `recipes[]` (en `render_recipes` y en GET /studio/api/status) no podía
    explicarse a sí misma: el porqué vivía solo en la lista paralela
    `measurements[]`. La rama sin medición lleva ahora el error de la fila no-ok
    más reciente, así que la receta, la fila y el log dicen lo mismo.
    """
    from comfy_studio.projects import MEASUREMENT_STATUS_LOST

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Receta Sin Motivo")
    store.record_render_measurement(
        template_id="wan_t2v",
        project_id=project["id"],
        duration_s=12.0,
        status=MEASUREMENT_STATUS_LOST,
        error="muestreador de /system_stats perdido",
    )

    recipe = store.recipe_for_template("wan_t2v")
    assert recipe is not None
    assert recipe["measured"] is False
    assert recipe["reason"] == "muestreador de /system_stats perdido", (
        "una receta sin medición no publica el porqué: recipes[] no puede "
        "explicarse a sí misma"
    )
    assert "vram_peak_mb_max" not in recipe
    assert "vram_fit" not in recipe


def test_the_drain_timeout_comment_states_the_real_reason() -> None:
    """R68: el comentario del `except TimeoutError` afirmaba un hecho falso.

    Decía que `TimeoutError` «es subclase de `OSError`, no de `Exception`, en
    Python 3.11+», y de ahí concluía que hay que nombrarlo antes del `except
    Exception` para que el timeout fuera alcanzable. Es falso: su MRO pasa por
    `Exception` (`TimeoutError -> OSError -> Exception`), así que el `except
    Exception` de abajo YA lo cubre. El orden solo existe para emitir el mensaje
    propio del drenaje sin traceback. El docstring treinta líneas más arriba lo
    dice bien; el comentario y el docstring se contradecían.
    """
    import inspect

    from comfy_studio.comfyui_client import ComfyUIClient

    src = inspect.getsource(ComfyUIClient._drain_sampler)
    # La afirmación falsa sobre la jerarquía no puede volver.
    assert "no de `Exception`" not in src, (
        "el comentario vuelve a afirmar que TimeoutError no es subclase de "
        "Exception: es falso, su MRO pasa por Exception y el `except Exception` "
        "ya lo cubre"
    )
    # Y la razón REAL del orden tiene que estar dicha, no un comentario vacío.
    assert "MRO" in src, (
        "el comentario perdió la razón real del orden (el mensaje propio sin "
        "traceback), que no es la alcanzabilidad del timeout"
    )
