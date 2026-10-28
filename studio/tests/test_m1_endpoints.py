"""Tests de endpoint y de postura que faltaban (M1.8: M3-IDEA-020/021/022).

Cubre los huecos señalados por la auditoría:
- `identities` PATCH/DELETE y `POST faces/index` / `POST search/combined` a nivel endpoint.
- Guarda anti-SSRF contra la IP de metadatos `169.254.169.254`.
- `require_auth` sin contraseña ⇒ `edge_auth: "open"` visible en health.
- Carrera del cliente ComfyUI: `node=None` no debe cerrar el job antes de persistir outputs.
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig
from comfy_studio.webhooks import WebhookUrlError, validate_webhook_url


def _png_bytes(
    width: int = 24,
    height: int = 16,
    rgb: tuple[int, int, int] = (200, 30, 30),
) -> bytes:
    import struct
    import zlib

    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _project_with_reference(client: TestClient) -> tuple[str, str]:
    project_id = client.post("/studio/api/projects", json={"name": "Identidades"}).json()["id"]
    asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("ref.png", _png_bytes(), "image/png")},
    ).json()
    return project_id, asset["id"]


def test_identities_endpoints_crud_roundtrip(client: TestClient) -> None:
    """M3-IDEA-020: PATCH/DELETE de identidades solo se ejercitaban vía store."""
    project_id, asset_id = _project_with_reference(client)

    created = client.post(
        f"/studio/api/projects/{project_id}/identities",
        json={"name": "Persona_A", "reference_asset_id": asset_id},
    )
    assert created.status_code == 201, created.text
    identity_id = created.json()["id"]
    assert created.json()["license_notice"]

    listed = client.get(f"/studio/api/projects/{project_id}/identities").json()
    assert [i["name"] for i in listed["identities"]] == ["Persona_A"]

    renamed = client.patch(
        f"/studio/api/projects/{project_id}/identities/{identity_id}",
        json={"name": "Persona_B"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Persona_B"

    missing = client.patch(
        f"/studio/api/projects/{project_id}/identities/does-not-exist",
        json={"name": "X"},
    )
    assert missing.status_code == 404

    deleted = client.delete(f"/studio/api/projects/{project_id}/identities/{identity_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": identity_id}
    assert client.get(f"/studio/api/projects/{project_id}/identities").json()["count"] == 0

    again = client.delete(f"/studio/api/projects/{project_id}/identities/{identity_id}")
    assert again.status_code == 404


def test_faces_index_and_combined_search_endpoints(client: TestClient) -> None:
    """M3-IDEA-020: `POST faces/index` y `POST search/combined` a nivel endpoint."""
    project_id, asset_id = _project_with_reference(client)

    indexed = client.post(
        f"/studio/api/projects/{project_id}/faces/index",
        json={"name": "Ana", "reference_asset_id": asset_id},
    )
    assert indexed.status_code == 200, indexed.text
    body = indexed.json()
    assert body["simulated"] is True
    assert body["license_notice"]
    assert body["identity"]["name"] == "Ana"

    combined = client.post(
        f"/studio/api/projects/{project_id}/search/combined",
        json={"person_name": "Ana", "text_query": "rojo", "limit": 5},
    )
    assert combined.status_code == 200, combined.text
    combined_body = combined.json()
    assert combined_body["mode"] == "combined"
    assert combined_body["simulated"] is True
    assert isinstance(combined_body["shots"], list)

    missing_project = client.post(
        "/studio/api/projects/no-such-project/search/combined",
        json={"person_name": "Ana"},
    )
    assert missing_project.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://127.0.0.1:8080/hook",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://localhost/hook",
    ],
)
def test_ssrf_guard_rejects_metadata_and_internal_targets(url: str) -> None:
    """M1.8: la guarda debe cubrir la IP de metadatos de instancia, no solo loopback."""
    with pytest.raises(WebhookUrlError):
        validate_webhook_url(url)


def test_ssrf_guard_rejects_hostname_resolving_to_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding por DNS hacia `169.254.169.254` (no solo literales IP)."""

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "evil.example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(WebhookUrlError, match="red interna"):
        validate_webhook_url("http://evil.example.com/hook")


def test_health_reports_edge_auth_open_when_require_auth_has_no_password(
    tmp_path: Path,
    stub_comfyui_transport: httpx.ASGITransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3-SEC-002 / M1.8: el fail-open debe ser observable en `/studio/api/health`."""
    monkeypatch.delenv("COMFY_PASS", raising=False)
    monkeypatch.delenv("STUDIO_AUTH_PASS", raising=False)
    cfg = StudioConfig(
        studio_data_dir=tmp_path / "data",
        studio_stub_llm=True,
        studio_comfyui_url="http://stub-comfyui.local:39172",
        comfy_user="admin",
        comfy_pass=None,
        require_auth=True,
    )
    with TestClient(create_app(config=cfg, comfyui_transport=stub_comfyui_transport)) as client:
        health = client.get("/studio/api/health")
        assert health.status_code == 200, health.text
        assert health.json()["edge_auth"] == "open"


def test_ws_event_without_node_does_not_complete_job_prematurely(
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """M3-IDEA-022: un `execution_complete` sin `node` antes de persistir outputs no
    debe marcar el job como `done` (regresión del race en `comfyui_client`)."""
    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.progress import ProgressBroker
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Race WS")
    job = store.create_job(kind="render", project_id=project["id"])
    client = ComfyUIClient(
        base_url="http://stub-comfyui.local:39172",
        transport=stub_comfyui_transport,
        store=store,
        broker=ProgressBroker(),
    )
    # `executing` con `node=None` es el fin de ejecución del grafo en ComfyUI, pero el job
    # solo puede pasar a `done` por la ruta que descarga y persiste las salidas
    # (M2-SCR-002). Marcar `done` aquí reintroduce la carrera de M3-IDEA-022.
    event = client.handle_ws_event(
        {"type": "executing", "data": {"prompt_id": "pid-1", "node": None}},
        job_id=job["id"],
        project_id=project["id"],
    )
    assert event is not None, "el evento debe parsearse"
    refreshed = store.get_job(job["id"])
    assert refreshed is not None
    assert refreshed["status"] != "done", (
        "un `executing` con node=None no debe cerrar el job antes de persistir outputs"
    )
