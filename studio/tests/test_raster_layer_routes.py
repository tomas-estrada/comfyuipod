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


def test_rgba_roundtrip_is_exact_for_transparent_partial_and_opaque(
    client: TestClient,
) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "RGBA roundtrip"}
    ).json()["id"]
    pixels = [(123, 45, 67, 0), (200, 30, 10, 128), (5, 6, 7, 255)]
    upload = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("source.png", _png(pixels), "image/png")},
    )
    assert upload.status_code == 201
    asset_id = upload.json()["id"]
    url = f"/studio/api/projects/{project_id}/raster-layers/rgba-roundtrip"
    declared = "unassociated-straight-alpha-8bit"

    missing_semantics = client.post(url, json={"asset_id": asset_id})
    assert missing_semantics.status_code == 422
    unsupported_semantics = client.post(
        url, json={"asset_id": asset_id, "semantics": "premultiplied-alpha-8bit"}
    )
    assert unsupported_semantics.status_code == 422
    wrong_pin = client.post(
        url,
        json={
            "asset_id": asset_id,
            "semantics": declared,
            "source_sha256": "0" * 64,
        },
    )
    assert wrong_pin.status_code == 409

    roundtrip = client.post(url, json={"asset_id": asset_id, "semantics": declared})
    assert roundtrip.status_code == 201, roundtrip.text
    evidence = roundtrip.json()["evidence"]
    assert evidence["exact_rgba_roundtrip"] is True
    assert evidence["semantics"]["alpha_encoding"] == "unassociated (straight) alpha"
    assert evidence["semantics"]["premultiplied"] is False
    assert evidence["rgb_under_alpha_zero"] == "preserved"
    assert evidence["source"]["sha256"] != "0" * 64
    assert evidence["reconstructed_sha256"] == evidence["source_rgba_sha256"]

    color_response = client.get(evidence["color_plane"]["preview_url"])
    assert color_response.status_code == 200
    with Image.open(io.BytesIO(color_response.content)) as color_plane:
        color_plane.load()
        assert color_plane.mode == "RGBA"
        # RGB under alpha=0 survives the decompose step exactly.
        assert color_plane.getpixel((0, 0)) == (123, 45, 67, 255)
    alpha_response = client.get(evidence["alpha_plane"]["preview_url"])
    assert alpha_response.status_code == 200
    with Image.open(io.BytesIO(alpha_response.content)) as alpha_plane:
        alpha_plane.load()
        assert alpha_plane.mode == "L"
        assert [alpha_plane.getpixel((x, 0)) for x in range(3)] == [0, 128, 255]

    source_path = client.app.state.store.get_asset_path(project_id, asset_id)
    assert source_path is not None
    original_bytes = source_path.read_bytes()
    try:
        source_path.write_bytes(b"not a png")
        malformed = client.post(url, json={"asset_id": asset_id, "semantics": declared})
        assert malformed.status_code == 422
        assert "ilegible" in malformed.json()["detail"]

        sixteen_bit = io.BytesIO()
        Image.new("I;16", (1, 1), 700).save(sixteen_bit, format="PNG")
        source_path.write_bytes(sixteen_bit.getvalue())
        rejected_depth = client.post(
            url, json={"asset_id": asset_id, "semantics": declared}
        )
        assert rejected_depth.status_code == 422
        assert "no soportado" in rejected_depth.json()["detail"]
    finally:
        source_path.write_bytes(original_bytes)


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

    replacement = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("replacement.png", _png([(80, 40, 20, 255)] * 2), "image/png")},
    )
    assert replacement.status_code == 201
    edited = client.patch(
        f"{url}/base", json={"asset_id": replacement.json()["id"]}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["invalidated_layer_ids"] == ["base", "overlay"]
    assert edited.json()["recomposition"] == "required_not_run"
    prior_output = client.get(result_asset["preview_url"].replace("/file", "")).json()
    assert prior_output["metadata"]["stale"] is True

    rerendered = client.post(f"{url}/composite")
    assert rerendered.status_code == 201
    changed_layers = edited.json()["layers"]
    changed_layers[1]["region"] = "shadow-region"
    replaced = client.put(url, json={"layers": changed_layers})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["invalidated_layer_ids"] == ["overlay"]
    previous_asset_url = rerendered.json()["asset"]["preview_url"].replace("/file", "")
    previous_asset = client.get(previous_asset_url).json()
    assert previous_asset["metadata"]["stale"] is True


def test_raster_layer_source_hash_detects_changed_project_bytes(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Provenance"}).json()[
        "id"
    ]
    original_bytes = _png([(12, 34, 56, 255)])
    upload = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("layer.png", original_bytes, "image/png")},
    )
    asset_id = upload.json()["id"]
    url = f"/studio/api/projects/{project_id}/raster-layers"
    body = {"layers": [{"layer_id": "plate", "asset_id": asset_id, "order": 0}]}
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.text
    pinned_hash = saved.json()["layers"][0]["source_sha256"]
    assert len(pinned_hash) == 64

    rendered = client.post(f"{url}/composite")
    assert rendered.status_code == 201, rendered.text
    path = client.app.state.store.get_asset_path(project_id, asset_id)
    assert path is not None
    path.write_bytes(_png([(90, 80, 70, 255)]))

    current = client.get(url)
    assert current.status_code == 200
    assert current.json()["stale_layer_ids"] == ["plate"]
    prior_output_url = rendered.json()["asset"]["preview_url"].replace("/file", "")
    prior_output = client.get(prior_output_url)
    assert prior_output.json()["metadata"]["stale"] is True
    rejected = client.post(f"{url}/composite")
    assert rejected.status_code == 409
    assert "fuente cambió" in rejected.json()["detail"]

    rebound = client.put(url, json=body)
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["layers"][0]["source_sha256"] != pinned_hash
    assert client.get(url).json()["stale_layer_ids"] == []


def test_legacy_raster_layer_without_hash_is_unknown_and_cannot_render(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Legacy integrity"}).json()["id"]
    upload = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("layer.png", _png([(12, 34, 56, 255)]), "image/png")},
    )
    asset_id = upload.json()["id"]
    url = f"/studio/api/projects/{project_id}/raster-layers"
    saved = client.put(
        url,
        json={"layers": [{"layer_id": "plate", "asset_id": asset_id, "order": 0}]},
    )
    assert saved.status_code == 200
    rendered = client.post(f"{url}/composite")
    assert rendered.status_code == 201

    # Simulate a persisted pre-hash manifest, then mutate the project-owned bytes.
    legacy = {
        "schema": saved.json()["schema"],
        "layers": [{key: value for key, value in saved.json()["layers"][0].items()
                    if key != "source_sha256"}],
    }
    client.app.state.store.set_meta(project_id, "ultra_raster_layer_set_v1", legacy)
    path = client.app.state.store.get_asset_path(project_id, asset_id)
    assert path is not None
    path.write_bytes(_png([(90, 80, 70, 255)]))

    current = client.get(url).json()
    assert current["stale_layer_ids"] == []
    assert current["integrity_unknown_layer_ids"] == ["plate"]
    prior_url = rendered.json()["asset"]["preview_url"].replace("/file", "")
    prior_output = client.get(prior_url).json()
    assert prior_output["metadata"]["stale"] is True
    assert prior_output["metadata"]["stale_reason"] == "raster_layer_source_hash_missing"
    rejected = client.post(f"{url}/composite")
    assert rejected.status_code == 409
    assert "integrity unknown" in rejected.json()["detail"]


def test_raster_layer_set_has_a_fixed_layer_count_limit(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Bounded"}).json()[
        "id"
    ]
    upload = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("layer.png", _png([(0, 0, 0, 255)]), "image/png")},
    )
    url = f"/studio/api/projects/{project_id}/raster-layers"
    response = client.put(
        url,
        json={
            "layers": [
                {"layer_id": f"layer-{index}", "asset_id": upload.json()["id"], "order": index % 32}
                for index in range(33)
            ]
        },
    )
    assert response.status_code == 422


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
