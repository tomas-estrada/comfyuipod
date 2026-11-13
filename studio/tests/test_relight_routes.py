"""Persistencia e invalidación selectiva de pases AOV (W16/A18, CPU)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app


def test_aovs_persist_and_relight_invalidation_preserves_unrelated_passes(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "AOV persistente"}).json()["id"]
    url = f"/studio/api/projects/{project_id}/aovs"
    stored = {
        "aovs": {
            name: {"validity": "valid", "content_hash": f"hash-{name}"}
            for name in ("albedo", "normal", "depth", "roughness", "id", "shading", "shadow")
        }
    }
    response = client.put(url, json=stored)
    assert response.status_code == 200, response.text
    assert response.json()["render_validation"] == "not_run"

    relight = client.post(f"{url}/invalidate", json={"task": "relight"})
    assert relight.status_code == 200, relight.text
    assert set(relight.json()["invalidated"]) == {"shading", "shadow"}
    assert relight.json()["aovs"]["normal"]["validity"] == "valid"
    assert relight.json()["aovs"]["shading"]["content_hash"] is None

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        after_restart = resumed.get(url)
        assert after_restart.status_code == 200
        assert after_restart.json() == {
            key: value
            for key, value in relight.json().items()
            if key not in {"task", "invalidated"}
        }
        restyle = resumed.post(f"{url}/invalidate", json={"task": "restyle"})
        assert restyle.status_code == 200
        assert set(restyle.json()["invalidated"]) == {
            "albedo",
            "roughness",
            "shading",
            "shadow",
        }
        assert restyle.json()["aovs"]["depth"]["validity"] == "valid"


def test_corrupt_persisted_aov_set_fails_closed(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "AOV corrupto"}).json()["id"]
    client.app.state.store.set_meta(
        project_id,
        "ultra_aov_set_v1",
        {"schema": "unknown"},
    )
    assert client.get(f"/studio/api/projects/{project_id}/aovs").status_code == 409


def test_relight_render_route_persists_deterministic_png_asset(
    client: TestClient,
) -> None:
    import base64
    import hashlib

    from comfy_studio.relight import render_relight

    project_id = client.post("/studio/api/projects", json={"name": "Relight render"}).json()["id"]
    width, height = 2, 1
    albedo = bytes((255, 0, 0, 255, 0, 255, 0, 255))
    shading = bytes((255, 128))
    expected = render_relight(albedo, shading, width, height)
    response = client.post(
        f"/studio/api/projects/{project_id}/aovs/relight/render",
        json={
            "albedo": base64.b64encode(albedo).decode("ascii"),
            "shading": base64.b64encode(shading).decode("ascii"),
            "width": width,
            "height": height,
            "intensity": 1.0,
            "ambient": 0.0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    asset = body["asset"]
    assert asset["category"] == "outputs"
    assert asset["mime_type"] == "image/png"
    assert asset["size_bytes"] == len(expected["png"])
    evidence = body["evidence"]
    assert evidence["sha256"] == hashlib.sha256(expected["png"]).hexdigest()
    assert evidence["albedo_sha256"] == expected["albedo_sha256"]
    assert evidence["shading_sha256"] == expected["shading_sha256"]

    # Longitud de buffer inconsistente → 422 (falla cerrada).
    invalid = client.post(
        f"/studio/api/projects/{project_id}/aovs/relight/render",
        json={
            "albedo": base64.b64encode(albedo[:-1]).decode("ascii"),
            "shading": base64.b64encode(shading).decode("ascii"),
            "width": width,
            "height": height,
        },
    )
    assert invalid.status_code == 422
    # Proyecto inexistente → 404 antes de tocar píxeles.
    missing = client.post(
        f"/studio/api/projects/{project_id}-missing/aovs/relight/render",
        json={
            "albedo": base64.b64encode(albedo).decode("ascii"),
            "shading": base64.b64encode(shading).decode("ascii"),
            "width": width,
            "height": height,
        },
    )
    assert missing.status_code == 404
