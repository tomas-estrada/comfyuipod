"""Tests de integración y unidad para VAL-SERVER-003: Cliente HTTP de ComfyUI contra stub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.comfyui_client import (
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
    assert any(
        p.get("status") == "done" and p.get("job_id") == job["id"]
        for p in job_payloads
    )

    final_job = store.get_job(job["id"])
    assert final_job is not None
    assert final_job["status"] == "done"
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



