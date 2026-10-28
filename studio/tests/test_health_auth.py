"""Tests para VAL-SERVER-001: Health y política de autenticación del edge."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig
from comfy_studio.stubs.comfyui import ComfyUIStubState


def test_health_returns_200_with_comfyui_ok(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    stub_comfyui_state.healthy = True
    resp = client.get("/studio/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["server"] == "ok"
    assert body["comfyui"] == "ok"
    assert body["comfyui_reachable"] is True
    assert body["stub_llm"] is True
    assert body["llm_key_status"] == "<definida>"


def test_health_returns_200_degraded_when_comfyui_down(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    stub_comfyui_state.healthy = False
    resp = client.get("/studio/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["server"] == "ok"
    assert body["comfyui"] == "degraded"
    assert body["comfyui_reachable"] is False


def test_health_returns_200_degraded_when_comfyui_unreachable_network(
    studio_data_dir,
) -> None:
    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_comfyui_url="http://127.0.0.1:39175",  # puerto sin escucha
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass="changeme-pass",
        require_auth=True,
    )
    app = create_app(config=cfg)
    with TestClient(app) as c:
        c.auth = ("admin", "changeme-pass")
        resp = c.get("/studio/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["comfyui"] == "degraded"
        assert body["comfyui_reachable"] is False


def test_edge_auth_rejects_unauthenticated_requests_with_401(
    anon_client: TestClient,
) -> None:
    protected_routes = [
        ("GET", "/studio/api/health"),
        ("GET", "/studio/api/status"),
        ("GET", "/studio/api/projects"),
        ("POST", "/studio/api/projects"),
        ("GET", "/studio/api/jobs"),
        ("GET", "/studio/api/progress?max_events=1"),
    ]
    for method, path in protected_routes:
        resp = anon_client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} debería devolver 401 sin auth"
        assert "Basic" in resp.headers.get("WWW-Authenticate", "")


def test_edge_auth_rejects_invalid_credentials_with_401(app) -> None:
    bad_pass_client = TestClient(app)
    bad_pass_client.auth = ("admin", "wrong-password")
    resp = bad_pass_client.get("/studio/api/health")
    assert resp.status_code == 401

    bad_user_client = TestClient(app)
    bad_user_client.auth = ("intruder", "changeme-test-pass-42")
    resp2 = bad_user_client.get("/studio/api/projects")
    assert resp2.status_code == 401

    raw_client = TestClient(app)
    resp3 = raw_client.get(
        "/studio/api/health",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp3.status_code == 401

    resp4 = raw_client.get(
        "/studio/api/health",
        headers={"Authorization": "Basic %%%not-base64%%%"},
    )
    assert resp4.status_code == 401


def test_edge_auth_no_lockout_when_require_auth_true_but_no_password(
    studio_data_dir,
    stub_comfyui_transport,
    monkeypatch,
) -> None:
    """Regresión: require_auth=True sin contraseña configurada no debe bloquear todas
    las peticiones con 401 (lockout total); sin credencial exigible se omite la auth."""
    for var in ("COMFY_PASS", "STUDIO_AUTH_PASS", "COMFY_USER", "STUDIO_AUTH_USER"):
        monkeypatch.delenv(var, raising=False)

    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass=None,
        require_auth=True,
    )
    app = create_app(config=cfg, comfyui_transport=stub_comfyui_transport)
    with TestClient(app) as c:
        resp = c.get("/studio/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # M3-SEC-002: el fail-open sin contraseña exigible debe ser VISIBLE
        assert resp.json()["edge_auth"] == "open"

        resp_edge = c.get(
            "/studio/api/health",
            headers={"X-Studio-Simulate-Edge": "1"},
        )
        assert resp_edge.status_code == 200

        resp_projects = c.get("/studio/api/projects")
        assert resp_projects.status_code == 200


def test_health_reports_edge_auth_basic_when_password_enforced(
    client: TestClient,
) -> None:
    """M3-SEC-002: con COMFY_PASS exigible, health reporta edge_auth=basic."""
    resp = client.get("/studio/api/health")
    assert resp.status_code == 200
    assert resp.json()["edge_auth"] == "basic"


def test_json_responses_redacted_centralally_at_edge(client: TestClient) -> None:
    """M3-SCR-008: toda respuesta JSON de /studio pasa por SecretRedactor,
    incluidos dicts crudos persistidos con secretos incrustados."""
    secret = "changeme-test-pass-42"
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Fuga", "description": f"pass visible: {secret}"},
    ).json()
    assert secret not in str(proj)

    listed = client.get("/studio/api/projects").json()
    assert secret not in str(listed)
    assert "[REDACTED]" in str(listed)

    detail = client.get(f"/studio/api/projects/{proj['id']}").json()
    assert secret not in str(detail)


def test_edge_auth_allows_loopback_when_no_comfy_pass_configured(
    studio_data_dir,
    stub_comfyui_transport,
) -> None:
    """En el pod real y en studio-local, supervisor quita COMFY_PASS tras nginx."""
    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_stub_llm=True,
        comfy_pass=None,
        require_auth=False,
    )
    app = create_app(config=cfg, comfyui_transport=stub_comfyui_transport)
    with TestClient(app) as c:
        resp = c.get("/studio/api/health")
        assert resp.status_code == 200
        assert resp.json()["comfyui"] == "ok"
