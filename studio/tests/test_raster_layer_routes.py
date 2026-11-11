"""CPU raster composition uses project PNG assets and persists editable layers."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image


def _png(pixels: list[tuple[int, int, int, int]]) -> bytes:
    image = Image.new("RGBA", (len(pixels), 1))
    image.putdata(pixels)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_project_raster_layers_persist_and_composite_real_pixels(
    client: TestClient,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Capas raster"}).json()[
        "id"
    ]
    base = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("base.png", _png([(10, 20, 30, 255), (10, 20, 30, 255)]), "image/png")},
    )
    overlay = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={
            "file": (
                "overlay.png",
                _png([(255, 0, 0, 0), (210, 30, 20, 128)]),
                "image/png",
            )
        },
    )
    assert base.status_code == overlay.status_code == 201
    url = f"/studio/api/projects/{project_id}/raster-layers"
    body = {
        "layers": [
            {"layer_id": "base", "asset_id": base.json()["id"], "order": 0},
            {
                "layer_id": "overlay",
                "asset_id": overlay.json()["id"],
                "caused_by": "base",
                "region": "foreground",
                "order": 1,
            },
        ]
    }
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.text
    assert client.get(url).json()["layers"] == saved.json()["layers"]

    rendered = client.post(f"{url}/composite")
    assert rendered.status_code == 201, rendered.text
    result_asset = rendered.json()["asset"]
    file_response = client.get(result_asset["preview_url"])
    assert file_response.status_code == 200
    with Image.open(io.BytesIO(file_response.content)) as result:
        result.load()
        assert result.mode == "RGBA"
        assert result.size == (2, 1)
        assert result.getpixel((0, 0)) == (10, 20, 30, 255)
        assert result.getpixel((1, 0)) == Image.alpha_composite(
            Image.new("RGBA", (1, 1), (10, 20, 30, 255)),
            Image.new("RGBA", (1, 1), (210, 30, 20, 128)),
        ).getpixel((0, 0))
    assert rendered.json()["evidence"]["renderer"] == "pillow-alpha-composite-cpu"
    assert rendered.json()["evidence"]["matting"] == "not_run"


def test_raster_layers_reject_cycles_and_cross_project_assets(client: TestClient) -> None:
    first_id = client.post("/studio/api/projects", json={"name": "A"}).json()["id"]
    second_id = client.post("/studio/api/projects", json={"name": "B"}).json()["id"]
    image = client.post(
        f"/studio/api/projects/{first_id}/uploads",
        files={"file": ("layer.png", _png([(0, 0, 0, 255)]), "image/png")},
    )
    asset_id = image.json()["id"]
    url = f"/studio/api/projects/{second_id}/raster-layers"
    cross_project = client.put(
        url,
        json={"layers": [{"layer_id": "one", "asset_id": asset_id, "order": 0}]},
    )
    assert cross_project.status_code == 422
    cycle = client.put(
        f"/studio/api/projects/{first_id}/raster-layers",
        json={
            "layers": [
                {
                    "layer_id": "one",
                    "asset_id": asset_id,
                    "caused_by": "two",
                    "order": 0,
                },
                {
                    "layer_id": "two",
                    "asset_id": asset_id,
                    "caused_by": "one",
                    "order": 1,
                },
            ]
        },
    )
    assert cycle.status_code == 422


def test_raster_compositor_rejects_mismatched_canvas_sizes(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Dimensiones"}).json()[
        "id"
    ]
    assets = []
    for filename, pixels in (
        ("small.png", [(0, 0, 0, 255)]),
        ("wide.png", [(0, 0, 0, 255), (1, 1, 1, 255)]),
    ):
        response = client.post(
            f"/studio/api/projects/{project_id}/uploads",
            files={"file": (filename, _png(pixels), "image/png")},
        )
        assert response.status_code == 201
        assets.append(response.json()["id"])
    url = f"/studio/api/projects/{project_id}/raster-layers"
    saved = client.put(
        url,
        json={
            "layers": [
                {"layer_id": "small", "asset_id": assets[0], "order": 0},
                {"layer_id": "wide", "asset_id": assets[1], "order": 1},
            ]
        },
    )
    assert saved.status_code == 200
    rendered = client.post(f"{url}/composite")
    assert rendered.status_code == 422
    assert "mismas dimensiones" in rendered.json()["detail"]
