"""Tests de webhooks salientes (M3-B tarea 15): notificador, firma HMAC,
redacción de secretos, reintentos, backlog acotado, dispatcher del broker y
configuración vía env."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from comfy_studio.app import create_app
from comfy_studio.config import SECRET_PLACEHOLDER, StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.webhooks import (
    BrokerWebhookDispatcher,
    WebhookNotifier,
    WebhookUrlError,
    create_webhook_notifier,
    map_broker_event,
    validate_webhook_url,
)


class ReceiverState:
    def __init__(self, fail_times: int = 0) -> None:
        self.received: list[dict[str, Any]] = []
        self.fail_times = fail_times
        self.attempts = 0


def _make_receiver(state: ReceiverState) -> Starlette:
    async def hook(request: Request) -> JSONResponse:
        raw = await request.body()
        state.attempts += 1
        if state.fail_times > 0:
            state.fail_times -= 1
            return JSONResponse({"error": "boom"}, status_code=500)
        state.received.append(
            {
                "body": json.loads(raw.decode("utf-8")),
                "raw": raw,
                "signature": request.headers.get("X-Studio-Signature"),
                "content_type": request.headers.get("Content-Type"),
            }
        )
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/hook", hook, methods=["POST"])])


def _make_notifier(
    state: ReceiverState,
    *,
    secret: str | None = None,
    retries: int = 3,
    backlog: int = 128,
) -> WebhookNotifier:
    cfg = StudioConfig(
        studio_data_dir=Path("."),
        comfy_pass="super-secret-pass-xyz",
        studio_webhook_url="http://webhook.local/hook",
        studio_webhook_secret=secret,
    )
    transport = httpx.ASGITransport(app=_make_receiver(state))
    return WebhookNotifier(
        url=cfg.studio_webhook_url or "",
        secret=cfg.studio_webhook_secret,
        redactor=cfg.redactor(),
        transport=transport,
        retries=retries,
        retry_base_delay_s=0.01,
        backlog=backlog,
    )


def test_map_broker_event_terminal_jobs_and_agent_done() -> None:
    assert map_broker_event("job", {"status": "running", "job_id": "j1"}) is None
    done = map_broker_event("job", {"status": "done", "job_id": "j1", "project_id": "p1"})
    assert done is not None
    assert done[0] == "job_finished"
    assert done[1] == {"job_id": "j1", "project_id": "p1", "status": "done"}

    failed = map_broker_event("job", {"status": "failed", "job_id": "j2", "error": "OOM"})
    assert failed is not None
    assert failed[0] == "job_failed"
    assert failed[1]["error"] == "OOM"

    cancelled = map_broker_event("job", {"status": "cancelled", "job_id": "j3"})
    assert cancelled is not None
    assert cancelled[0] == "job_failed"
    assert cancelled[1]["status"] == "cancelled"

    interrupted = map_broker_event(
        "job", {"status": "interrupted", "job_id": "j4", "project_id": "p1"}
    )
    assert interrupted is not None
    assert interrupted[0] == "job_interrupted"
    assert interrupted[1] == {"job_id": "j4", "project_id": "p1", "status": "interrupted"}

    turn = map_broker_event(
        "done",
        {"project_id": "p1", "iterations": 2, "stop_reason": "completed", "reply": "hola"},
    )
    assert turn is not None
    assert turn[0] == "agent_turn_done"
    assert turn[1] == {"project_id": "p1", "iterations": 2, "stop_reason": "completed"}
    assert "reply" not in turn[1]

    assert map_broker_event("heartbeat", {}) is None


@pytest.mark.anyio
async def test_notifier_delivers_with_hmac_signature() -> None:
    state = ReceiverState()
    notifier = _make_notifier(state, secret="whsec-123456")
    notifier.fire("job_finished", {"job_id": "j1"})
    await notifier.stop()

    assert notifier.sent_count == 1
    assert len(state.received) == 1
    entry = state.received[0]
    assert entry["body"]["event"] == "job_finished"
    assert entry["body"]["job_id"] == "j1"
    assert entry["content_type"] == "application/json"
    expected_sig = hmac.new(
        b"whsec-123456",
        entry["raw"],
        hashlib.sha256,
    ).hexdigest()
    assert entry["signature"] == expected_sig


@pytest.mark.anyio
async def test_notifier_redacts_secrets_in_payload() -> None:
    state = ReceiverState()
    notifier = _make_notifier(state)
    notifier.fire(
        "agent_turn_done",
        {"detail": "fallo con pass super-secret-pass-xyz en el entorno"},
    )
    await notifier.stop()

    assert notifier.sent_count == 1
    raw_text = state.received[0]["raw"].decode("utf-8")
    assert "super-secret-pass-xyz" not in raw_text
    assert SECRET_PLACEHOLDER in raw_text
    assert state.received[0]["signature"] is None


@pytest.mark.anyio
async def test_notifier_retries_with_backoff_then_succeeds() -> None:
    state = ReceiverState(fail_times=2)
    notifier = _make_notifier(state, retries=3)
    notifier.fire("job_failed", {"job_id": "j9"})
    await notifier.stop()

    assert state.attempts == 3
    assert notifier.sent_count == 1
    assert notifier.failed_count == 0


@pytest.mark.anyio
async def test_notifier_gives_up_after_retries() -> None:
    state = ReceiverState(fail_times=10)
    notifier = _make_notifier(state, retries=2)
    notifier.fire("job_failed", {"job_id": "j9"})
    await notifier.stop()

    assert state.attempts == 2
    assert notifier.sent_count == 0
    assert notifier.failed_count == 1


@pytest.mark.anyio
async def test_notifier_drops_when_backlog_full() -> None:
    state = ReceiverState()
    notifier = _make_notifier(state, backlog=1)
    # Sin ceder el event loop entre llamadas, el consumidor no drena: backlog (1) lleno.
    assert notifier.fire("job_finished", {"job_id": "j1"}) is True
    assert notifier.fire("job_finished", {"job_id": "j2"}) is False
    assert notifier.dropped_count == 1
    await notifier.stop()


@pytest.mark.anyio
async def test_broker_dispatcher_forwards_terminal_events() -> None:
    state = ReceiverState()
    notifier = _make_notifier(state)
    broker = ProgressBroker()
    dispatcher = BrokerWebhookDispatcher(broker, notifier)
    dispatcher.start()

    broker.publish("job", {"job_id": "j1", "project_id": "p1", "status": "running"})
    broker.publish("job", {"job_id": "j1", "project_id": "p1", "status": "done"})
    broker.publish("done", {"project_id": "p1", "iterations": 1, "stop_reason": "completed"})

    for _ in range(200):
        if notifier.sent_count >= 2:
            break
        await asyncio.sleep(0.01)

    await dispatcher.stop()
    await notifier.stop()

    events = [entry["body"]["event"] for entry in state.received]
    assert "job_finished" in events
    assert "agent_turn_done" in events
    assert all(e != "job_running" for e in events)


def test_config_from_env_and_public_dict_mask_webhook() -> None:
    cfg = StudioConfig.from_env(
        {
            "STUDIO_WEBHOOK_URL": " https://example.com/hook ",
            "STUDIO_WEBHOOK_SECRET": "whsec-abc",
            "STUDIO_DATA_DIR": "/tmp/studio",  # noqa: S108
        }
    )
    assert cfg.studio_webhook_url == "https://example.com/hook"
    assert cfg.studio_webhook_secret == "whsec-abc"

    public = cfg.to_public_dict()
    assert public["STUDIO_WEBHOOK_URL"] == "<definida>"
    assert public["STUDIO_WEBHOOK_SECRET"] == "<definida>"
    assert "whsec-abc" not in json.dumps(public)

    redactor = cfg.redactor()
    assert "whsec-abc" not in redactor.redact("fuga whsec-abc en log")

    empty = StudioConfig.from_env({"STUDIO_WEBHOOK_URL": "   ", "STUDIO_DATA_DIR": "/tmp/x"})  # noqa: S108
    assert empty.studio_webhook_url is None
    assert create_webhook_notifier(empty) is None


def test_app_wires_notifier_only_when_configured(studio_data_dir, stub_comfyui_transport) -> None:
    cfg_plain = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_stub_llm=True,
    )
    app_plain = create_app(config=cfg_plain, comfyui_transport=stub_comfyui_transport)
    assert app_plain.state.webhook_notifier is None
    assert app_plain.state.webhook_dispatcher is None

    cfg_wh = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_stub_llm=True,
        studio_webhook_url="http://webhook.local/hook",
        studio_webhook_secret="whsec-app",
    )
    app_wh = create_app(config=cfg_wh, comfyui_transport=stub_comfyui_transport)
    assert isinstance(app_wh.state.webhook_notifier, WebhookNotifier)
    assert isinstance(app_wh.state.webhook_dispatcher, BrokerWebhookDispatcher)


# --- M3-SEC-001: guarda anti-SSRF de STUDIO_WEBHOOK_URL ---------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:18190/studio/api/health",
        "http://[::1]/hook",
        "http://localhost/hook",
        "http://LOCALHOST./hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/hook",
        "http://192.168.1.1/hook",
        "http://172.16.0.9/hook",
        "http://0.0.0.0/hook",
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "   ",
    ],
)
def test_validate_webhook_url_rejects_internal_targets(bad_url: str) -> None:
    with pytest.raises(WebhookUrlError):
        validate_webhook_url(bad_url)


def test_validate_webhook_url_allows_public_and_unresolvable_hosts() -> None:
    assert validate_webhook_url("https://example.com/hook") == "https://example.com/hook"
    # Sin DNS (hostname de test con transporte ASGI): no bloquea el arranque
    assert validate_webhook_url("http://webhook.local/hook") == "http://webhook.local/hook"


def test_validate_webhook_url_rejects_dns_rebinding_to_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        "comfy_studio.webhooks.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://evil.example.com/hook")


def test_notifier_and_factory_reject_internal_urls(studio_data_dir: Path) -> None:
    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        comfy_pass="super-secret-pass-xyz",
        studio_webhook_url="http://127.0.0.1:18189/report.md",
    )
    # La fábrica degrada a None (webhooks desactivados) sin tumbar el arranque
    assert create_webhook_notifier(cfg) is None
    # El notifier directo lanza
    with pytest.raises(WebhookUrlError):
        WebhookNotifier(
            url="http://169.254.169.254/latest/meta-data/",
            redactor=cfg.redactor(),
        )


# --- M3-SCR-015: no reintentar 4xx permanentes -------------------------------


@pytest.mark.anyio
async def test_notifier_does_not_retry_permanent_4xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401)

    cfg = StudioConfig(studio_data_dir=Path("."), studio_webhook_url="http://webhook.local/hook")
    notifier = WebhookNotifier(
        url=cfg.studio_webhook_url or "",
        redactor=cfg.redactor(),
        transport=httpx.MockTransport(handler),
        retries=3,
        retry_base_delay_s=0.01,
        validate_url=False,
    )
    notifier.fire("job_finished", {"job_id": "j1"})
    await notifier.stop()

    assert attempts["n"] == 1
    assert notifier.sent_count == 0
    assert notifier.failed_count == 1
