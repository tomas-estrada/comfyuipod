"""Las búsquedas de percepción declaran el backend que realmente las sirvió (T3).

Los endpoints de búsqueda fijaban `simulated: True` y `simulation_notice` en
todas sus respuestas. En modo `real` eso mentía en la dirección contraria: un
índice real se anunciaba como simulado y arrastraba un aviso que no aplicaba.
La fuente correcta es `app.state.perception_backend`, que `create_app` resuelve
desde `STUDIO_PERCEPTION_BACKEND` y el probe de arranque.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from comfy_studio.perception import ClipVisualIndex, InsightFaceIndex


def _set_backend(app, backend: str) -> None:
    app.state.perception_backend = backend
    app.state.perception_available = True
    app.state.perception_stale = False
    app.state.perception_probe_result = {"probe_ok": True}


@pytest.fixture()
def project_id(app) -> str:
    return app.state.store.create_project(name="perception-flag")["id"]


def test_text_search_reports_real_backend_as_not_simulated(
    app,
    client: TestClient,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_backend(app, "real")
    monkeypatch.setattr(ClipVisualIndex, "find_shots_text", lambda self, *a, **k: [])

    resp = client.get(
        f"/studio/api/projects/{project_id}/perception/search",
        params={"query": "plano en el bosque"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simulated"] is False, "un índice real no puede anunciarse como simulado"
    assert "simulation_notice" not in body


def test_text_search_reports_sim_backend_as_simulated(
    app,
    client: TestClient,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_backend(app, "sim")
    monkeypatch.setattr(ClipVisualIndex, "find_shots_text", lambda self, *a, **k: [])

    resp = client.get(
        f"/studio/api/projects/{project_id}/perception/search",
        params={"query": "plano en el bosque"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simulated"] is True
    assert body["simulation_notice"], "el modo simulado debe declararse con su aviso"


def test_stale_index_still_reports_real_backend(
    app,
    client: TestClient,
    project_id: str,
) -> None:
    _set_backend(app, "real")
    app.state.perception_stale = True

    resp = client.get(
        f"/studio/api/projects/{project_id}/perception/search",
        params={"query": "plano en el bosque"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stale"] is True
    assert body["simulated"] is False


def test_face_search_reports_real_backend_as_not_simulated(
    app,
    client: TestClient,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_backend(app, "real")
    monkeypatch.setattr(InsightFaceIndex, "find_shots_face", lambda self, *a, **k: [])

    resp = client.post(
        f"/studio/api/projects/{project_id}/search/faces",
        json={"identity_name": "Ana"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["simulated"] is False


def test_combined_search_reports_real_backend_as_not_simulated(
    app,
    client: TestClient,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_backend(app, "real")
    monkeypatch.setattr(InsightFaceIndex, "combined_search", lambda self, *a, **k: [])

    resp = client.post(
        f"/studio/api/projects/{project_id}/search/combined",
        json={"query": "playa", "identity_name": "Ana"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["simulated"] is False
