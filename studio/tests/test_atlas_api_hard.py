"""PR-5 atlas-api-harding: 404 honestos, 422/400 y redactor sobre TestClient."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_atlas_core import _ref, make_png

from comfy_studio.app import create_app
from comfy_studio.atlas.agent_tools import reset_shared_atlas_service
from comfy_studio.atlas.models import Environment, Place, View
from comfy_studio.atlas.store import AtlasStore, sha256_file
from comfy_studio.config import StudioConfig
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app

FAKE_TOKEN = "sk-test-fake-token-abcdef123456"


@pytest.fixture()
def hard_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_hard", municipality="Medellín",
                             name="Puente Duro", kind=Environment.BRIDGE,
                             lat=6.2088, lon=-75.5670))
    r = _ref("ref_hard", "plc_hard", 6.2089, -75.5671, title="Puente Duro")
    r.attribution = f"Autora con {FAKE_TOKEN}, CC BY-SA 4.0"
    fpath = root / "files" / r.file_path
    make_png(fpath, 64, 48)
    r.sha256 = sha256_file(fpath)
    r.bytes = fpath.stat().st_size
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    make_png(root / "previews" / "ref_hard.jpg", 32, 24)
    store.upsert_view(View(view_id="view_hard", place_id="plc_hard",
                           ref_id="ref_hard", lat=6.2089, lon=-75.5671))
    evil = _ref("ref_evil", "plc_hard", 6.2088, -75.5670, title="Evil")
    evil.file_path = "../../atlas_evil.png"
    evil.usable = False
    evil.reject_reason = "fixture de prueba de traversal (no usable)"
    store.upsert_reference(evil)
    store.set_license(evil.ref_id, evil.license, None, "evil", None)
    store.close()
    return root


@pytest.fixture()
def hclient(tmp_path: Path, hard_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(hard_root))
    reset_shared_atlas_service()
    cfg = StudioConfig(
        model_profile="studio",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_data_dir=tmp_path / "data",
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass="changeme-test-pass-42",
        require_auth=True,
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    stub = ComfyUIStubState(healthy=True)
    transport = httpx.ASGITransport(app=create_stub_comfyui_app(stub))
    app = create_app(config=cfg, comfyui_transport=transport)
    tc = TestClient(app)
    tc.auth = ("admin", "changeme-test-pass-42")
    return tc


def test_404_honestos_por_endpoint(hclient: TestClient) -> None:
    cases = [
        ("get", "/studio/api/atlas/places/plc_nope", None, "Lugar no encontrado"),
        ("get", "/studio/api/atlas/places/plc_nope/coverage", None, "Lugar no encontrado"),
        ("get", "/studio/api/atlas/places/plc_nope/views", None, "Lugar no encontrado"),
        ("get", "/studio/api/atlas/references/ref_nope", None, "Referencia no encontrada"),
        ("get", "/studio/api/atlas/references/ref_nope/file", None, "Referencia no encontrada"),
        ("get", "/studio/api/atlas/references/ref_nope/preview", None, "Referencia no encontrada"),
        ("get", "/studio/api/atlas/references/ref_hard/license", None, None),
        ("get", "/studio/api/atlas/references/ref_nope/license", None, "Licencia no registrada"),
    ]
    for method, path, body, detail in cases:
        res = hclient.get(path) if method == "get" else hclient.post(path, json=body)
        if detail is None:
            assert res.status_code == 200, path
        else:
            assert res.status_code == 404, path
            assert res.json()["detail"] == detail, path
    res = hclient.post("/studio/api/atlas/select-view", json={"place_id": "plc_nope"})
    assert res.status_code == 404
    assert res.json()["detail"] == "place_id desconocido"
    res = hclient.post("/studio/api/atlas/select-view", json={})
    assert res.status_code == 404


def test_bbox_invalido_422(hclient: TestClient) -> None:
    res = hclient.get("/studio/api/atlas/places", params={"bbox": "no-es-bbox"})
    assert res.status_code == 422
    res = hclient.get("/studio/api/atlas/places",
                      params={"bbox": "6.2,-75.5"})
    assert res.status_code == 422


def test_traversal_en_file_400(hclient: TestClient) -> None:
    res = hclient.get("/studio/api/atlas/references/ref_evil/file")
    assert res.status_code == 400
    assert res.json()["detail"] == "ruta fuera del atlas"


def test_redactor_cubre_atlas(hclient: TestClient) -> None:
    res = hclient.get("/studio/api/atlas/references/ref_hard")
    assert res.status_code == 200
    assert FAKE_TOKEN not in res.text
    assert "[REDACTED]" in res.text
    res = hclient.get("/studio/api/atlas/places/plc_hard")
    assert res.status_code == 200
    assert FAKE_TOKEN not in res.text
    assert "[REDACTED]" in res.text


def test_select_view_preview_verify_basicos(hclient: TestClient) -> None:
    res = hclient.post("/studio/api/atlas/select-view",
                       json={"place_id": "plc_hard"})
    assert res.status_code == 200 and res.json()["status"] == "ok"
    res = hclient.get("/studio/api/atlas/references/ref_hard/preview")
    assert res.status_code == 200
    res = hclient.get("/studio/api/atlas/verify", params={"quick": "true"})
    assert res.status_code == 200 and res.json()["ok"] is True
    res = hclient.get("/studio/api/atlas/selection")
    assert res.status_code == 200
    res = hclient.get("/studio/api/atlas/search", params={"q": "duro"})
    assert res.status_code == 200
    assert res.json()["places"], "la búsqueda debe devolver el lugar"
