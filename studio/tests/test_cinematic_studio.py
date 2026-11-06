"""Cinematic prompt controls use the existing local SDXL template and project pipeline."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from comfy_studio.creative.cinematic import (
    CINEMATIC_CHOICES,
    CINEMATIC_DEFAULTS,
    CinematicRequestError,
    compose_cinematic_prompt,
    prepare_cinematic_render,
)
from comfy_studio.stubs.comfyui import ComfyUIStubState


def _wait_terminal_job(client: TestClient, job_id: str, timeout_s: float = 15.0) -> dict[str, Any]:
    terminal = {"done", "error", "failed", "cancelled", "interrupted"}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/studio/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in terminal:
            return job
        time.sleep(0.1)
    raise AssertionError(f"El job {job_id} no llegó a estado terminal en {timeout_s}s")


def test_cinematic_prompt_uses_source_order_and_trims_scene() -> None:
    prompt = compose_cinematic_prompt(
        "  Una fotógrafa cruza un puente bajo la lluvia.  ",
        {
            "shot": "Close-up portrait",
            "camera": "85mm portrait lens, soft background",
            "light": "Blue-hour practical lights",
            "look": "High-contrast noir",
        },
    )
    assert prompt == (
        "Close-up portrait. Una fotógrafa cruza un puente bajo la lluvia. "
        "85mm portrait lens, soft background. Blue-hour practical lights. "
        "High-contrast noir"
    )


def test_prepare_cinematic_render_applies_local_sdxl_defaults_and_aspect_ratio() -> None:
    prepared = prepare_cinematic_render(
        {
            "scene": "Un tren atraviesa una selva cubierta de neblina",
            "aspect_ratio": "9:16",
            "random_seed": False,
            "seed": 1234,
        }
    )
    params = prepared["params"]
    assert prepared["aspect_ratio"] == "9:16"
    assert prepared["direction"] == CINEMATIC_DEFAULTS
    assert params["prompt"].startswith("Wide establishing shot. Un tren")
    assert (params["width"], params["height"]) == (768, 1344)
    assert params["steps"] == 35
    assert params["cfg"] == 5.0
    assert params["seed"] == 1234


def test_prepare_cinematic_render_generates_seed_in_comfyui_range() -> None:
    prepared = prepare_cinematic_render({"scene": "Un faro al borde del océano"})
    assert 0 <= prepared["params"]["seed"] <= 2_147_483_647


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"scene": "ab"}, "3 caracteres"),
        ({"scene": "escena", "shot": "Dutch angle"}, "shot"),
        ({"scene": "escena", "aspect_ratio": "2:1"}, "aspecto"),
        ({"scene": "escena", "steps": True}, "pasos"),
        ({"scene": "escena", "cfg": float("nan")}, "CFG"),
        ({"scene": "escena", "random_seed": False, "seed": -1}, "semilla"),
        ({"scene": "x" * 901}, "900 caracteres"),
    ],
)
def test_prepare_cinematic_render_rejects_invalid_inputs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(CinematicRequestError, match=message):
        prepare_cinematic_render(payload)


def test_cinematic_options_api_exposes_direction_catalog(client: TestClient) -> None:
    response = client.get("/studio/api/cinematic/options")
    assert response.status_code == 200
    data = response.json()
    assert data["template_id"] == "sdxl_image"
    assert data["choices"] == {key: list(values) for key, values in CINEMATIC_CHOICES.items()}
    assert data["defaults"]["shot"] == CINEMATIC_DEFAULTS["shot"]
    assert {item["value"] for item in data["aspect_ratios"]} == {
        "16:9",
        "9:16",
        "1:1",
        "4:5",
    }


def test_cinematic_submit_uses_local_sdxl_queue_and_persists_project_output(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    project_response = client.post("/studio/api/projects", json={"name": "Cinematic test"})
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]

    response = client.post(
        f"/studio/api/projects/{project_id}/cinematic/submit",
        json={
            "scene": "Una astronauta contempla la Tierra desde una estación espacial",
            "shot": "Medium shot",
            "camera": "35mm lens, shallow depth of field",
            "light": "Golden-hour backlight",
            "look": "Warm cinematic film",
            "aspect_ratio": "16:9",
            "steps": 32,
            "cfg": 5.5,
            "random_seed": False,
            "seed": 9876,
        },
    )
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["async"] is True
    assert queued["status"] == "queued"
    assert queued["project_id"] == project_id
    assert queued["template_id"] == "sdxl_image"
    assert queued["aspect_ratio"] == "16:9"
    assert (queued["width"], queued["height"]) == (1344, 768)
    assert queued["seed"] == 9876

    job = _wait_terminal_job(client, queued["job_id"])
    assert job["status"] == "done", job
    assert job["template_id"] == "sdxl_image"
    expected_prompt = (
        "Medium shot. Una astronauta contempla la Tierra desde una estación espacial. "
        "35mm lens, shallow depth of field. Golden-hour backlight. Warm cinematic film"
    )
    assert job["params"]["prompt"] == expected_prompt
    assert job["params"]["width"] == 1344
    assert job["params"]["height"] == 768
    assert len(stub_comfyui_state.prompts) == 1
    graph = stub_comfyui_state.prompts[0]["prompt"]
    assert graph["2"]["inputs"]["text"] == expected_prompt
    assert graph["5"]["inputs"]["seed"] == 9876

    gallery = client.get(
        f"/studio/api/projects/{project_id}/gallery?category=outputs"
    ).json()
    assert any(asset.get("job_id") == queued["job_id"] for asset in gallery["assets"])


def test_cinematic_submit_rejects_invalid_direction_before_enqueue(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    project_response = client.post("/studio/api/projects", json={"name": "Invalid cinematic"})
    project_id = project_response.json()["id"]
    response = client.post(
        f"/studio/api/projects/{project_id}/cinematic/submit",
        json={"scene": "A scene", "look": "unlicensed option"},
    )
    assert response.status_code == 422
    assert "look" in response.json()["detail"]
    assert stub_comfyui_state.prompts == []
    assert client.get(f"/studio/api/jobs?project_id={project_id}").json()["jobs"] == []


def test_cinematic_submit_requires_existing_project(client: TestClient) -> None:
    response = client.post(
        "/studio/api/projects/not-a-project/cinematic/submit",
        json={"scene": "Una ciudad de noche"},
    )
    assert response.status_code == 404
