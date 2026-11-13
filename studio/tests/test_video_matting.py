"""Bounded CPU chroma-key matting real (C29/A17).

La ruta es chroma-key declarado: el usuario elige un color plano explícito de
fondo y ese color se vuelve transparente en cada fotograma. No hay matting
semántico ni de modelo; los tests exigen evidencia de píxeles reales (alpha
medido) y provenance (sha256 de la fuente).
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from comfy_studio.media_bins import find_ffmpeg

_NEEDS_FFMPEG = find_ffmpeg() is None or shutil.which("ffprobe") is None

pytestmark = pytest.mark.skipif(_NEEDS_FFMPEG, reason="chroma-key real requiere ffmpeg y ffprobe")

_KEY_COLOR = "#22B14C"  # verde plano sintético
_GREEN = (34, 177, 76)
_RED = (200, 30, 30)


def _upload_chroma_video(client: TestClient, project_id: str, shift: int = 0) -> str:
    """Sube un MP4 real de 4 fotogramas: fondo verde plano + cuadrado rojo móvil."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for i in range(4):
            img = Image.new("RGB", (64, 48), _GREEN)
            x = 8 + i * 12 + shift
            for px in range(x, min(x + 16, 64)):
                for py in range(16, 32):
                    img.putpixel((px, py), _RED)
            img.save(tmp_dir / f"f_{i:03d}.png", format="PNG")
        encoded = tmp_dir / "in.mp4"
        subprocess.run(
            [
                find_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-r",
                "4",
                "-i",
                str(tmp_dir / "f_%03d.png"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(encoded),
            ],
            check=True,
            capture_output=True,
        )
        data = encoded.read_bytes()
    response = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("chroma-in.mp4", data, "video/mp4")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _decode_rgba(video_path: Path, out_dir: Path) -> list[Image.Image]:
    subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            str(out_dir / "out_%03d.png"),
        ],
        check=True,
        capture_output=True,
    )
    frames = []
    for path in sorted(out_dir.glob("out_*.png")):
        with Image.open(path) as im:
            im.load()
            assert im.mode == "RGBA"
            frames.append(im.copy())
    return frames


def test_chroma_key_produces_alpha_frames_and_provenance(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Chroma"}).json()["id"]
    asset_id = _upload_chroma_video(client, project_id)
    response = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/chroma-key",
        json={"asset_id": asset_id, "key_color": _KEY_COLOR},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    # Nombre explícito y reclamos acotados: chroma-key, no matting semántico.
    assert body["matting"] == "chroma_key_flat_color"
    assert body["semantic_matting"] == "not_claimed"
    evidence = body["evidence"]
    assert evidence["renderer"] == "ffmpeg-colorkey-cpu"
    assert evidence["matting_scope"] == "explicit_user_selected_flat_background_color"
    assert evidence["key_color"].lower() == "0x22b14c"
    assert evidence["source"]["asset_id"] == asset_id
    assert evidence["source"]["sha256"]

    # Provenance persistido en el metadato del asset de salida.
    stored = client.get(f"/studio/api/projects/{project_id}/assets/{body['asset']['id']}").json()
    assert stored["metadata"]["chroma_key"]["source_sha256"]

    # Salida portadora de alpha verificada con ffprobe: ffv1 rgba/bgra sin audio.
    out_asset = body["asset"]
    assert out_asset["kind"] == "video"
    video_response = client.get(out_asset["preview_url"])
    assert video_response.status_code == 200
    probe = evidence["output_probe"]
    assert probe["codec_name"] == "ffv1"
    assert probe["pix_fmt"] in {"rgba", "bgra"}
    assert probe["has_audio"] is False
    assert probe["frame_count"] == 4

    # Frames RGBA reales: fondo claveado alpha=0, sujeto alpha=255.
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.mkv"
        out_path.write_bytes(video_response.content)
        frames = _decode_rgba(out_path, Path(tmp))
        assert len(frames) == 4
        for i, frame in enumerate(frames):
            x = 10 + i * 12  # centro del cuadrado rojo en cada fotograma
            assert frame.getpixel((x, 24))[3] == 255, (i, frame.getpixel((x, 24)))
            assert frame.getpixel((2, 2))[3] == 0, (i, frame.getpixel((2, 2)))

    # Cobertura medible sobre la secuencia completa.
    coverage = evidence["coverage"]
    assert 0.0 < coverage["keyed_mean"] < 1.0
    assert 0.0 < coverage["opaque_mean"] < 1.0
    assert coverage["sampled_frames"] == 4

    # Frame representativo persistido como PNG RGBA.
    frame_asset = body["frame_asset"]
    assert frame_asset["kind"] == "image"
    frame_response = client.get(frame_asset["preview_url"])
    assert frame_response.status_code == 200
    with Image.open(io.BytesIO(frame_response.content)) as frame:
        frame.load()
        assert frame.mode == "RGBA"
        assert frame.getpixel((2, 2))[3] == 0


def test_chroma_key_rejects_cross_project_missing_and_bad_inputs(
    client: TestClient,
) -> None:
    first_id = client.post("/studio/api/projects", json={"name": "A"}).json()["id"]
    second_id = client.post("/studio/api/projects", json={"name": "B"}).json()["id"]
    video_id = _upload_chroma_video(client, first_id)

    url = f"/studio/api/projects/{second_id}/raster-layers/chroma-key"
    cross_project = client.post(url, json={"asset_id": video_id, "key_color": _KEY_COLOR})
    assert cross_project.status_code == 422
    assert "proyecto" in cross_project.json()["detail"]

    colorless = client.post(
        f"/studio/api/projects/{first_id}/raster-layers/chroma-key",
        json={"asset_id": video_id, "key_color": "not-a-color"},
    )
    assert colorless.status_code == 422

    missing = client.post(
        f"/studio/api/projects/{first_id}/raster-layers/chroma-key",
        json={"asset_id": "doesnotexist", "key_color": _KEY_COLOR},
    )
    assert missing.status_code == 422

    bad_range = client.post(
        f"/studio/api/projects/{first_id}/raster-layers/chroma-key",
        json={"asset_id": video_id, "key_color": _KEY_COLOR, "similarity": 7.0},
    )
    assert bad_range.status_code == 422


def test_chroma_key_rejects_non_video_asset(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "No video"}).json()["id"]
    png = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
    output = io.BytesIO()
    png.save(output, format="PNG")
    upload = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("flat.png", output.getvalue(), "image/png")},
    )
    assert upload.status_code == 201
    response = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/chroma-key",
        json={"asset_id": upload.json()["id"], "key_color": _KEY_COLOR},
    )
    assert response.status_code == 422
    assert "chroma-key" in response.json()["detail"]


def test_chroma_key_rerun_marks_previous_outputs_stale(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Chroma reruns"}).json()["id"]
    first_source = _upload_chroma_video(client, project_id)
    url = f"/studio/api/projects/{project_id}/raster-layers/chroma-key"

    first = client.post(url, json={"asset_id": first_source, "key_color": _KEY_COLOR})
    assert first.status_code == 201, first.text
    first_body = first.json()
    first_ids = {first_body["asset"]["id"], first_body["frame_asset"]["id"]}
    assert first_body["invalidated_outputs"] == []

    changed_params = client.post(
        url,
        json={"asset_id": first_source, "key_color": _KEY_COLOR, "similarity": 0.5},
    )
    assert changed_params.status_code == 201, changed_params.text
    second_body = changed_params.json()
    assert set(second_body["invalidated_outputs"]) == first_ids
    for asset_id in first_ids:
        metadata = client.get(f"/studio/api/projects/{project_id}/assets/{asset_id}").json()[
            "metadata"
        ]
        assert metadata["stale"] is True
        assert metadata["stale_reason"] == "chroma_key_input_changed"
        assert metadata["chroma_key_changed_fields"] == ["similarity"]

    unchanged = client.post(
        url,
        json={"asset_id": first_source, "key_color": _KEY_COLOR, "similarity": 0.5},
    )
    assert unchanged.status_code == 201, unchanged.text
    third_body = unchanged.json()
    second_ids = {second_body["asset"]["id"], second_body["frame_asset"]["id"]}
    assert set(third_body["invalidated_outputs"]) == set()
    for asset_id in second_ids:
        metadata = client.get(f"/studio/api/projects/{project_id}/assets/{asset_id}").json()[
            "metadata"
        ]
        assert "stale" not in metadata

    second_source = _upload_chroma_video(client, project_id, shift=1)
    changed_source = client.post(
        url,
        json={"asset_id": second_source, "key_color": _KEY_COLOR, "similarity": 0.5},
    )
    assert changed_source.status_code == 201, changed_source.text
    fourth_body = changed_source.json()
    third_ids = {third_body["asset"]["id"], third_body["frame_asset"]["id"]}
    assert set(fourth_body["invalidated_outputs"]) == third_ids
    for asset_id in third_ids:
        metadata = client.get(f"/studio/api/projects/{project_id}/assets/{asset_id}").json()[
            "metadata"
        ]
        assert metadata["stale"] is True
        assert metadata["chroma_key_changed_fields"] == ["source_asset_id", "source_sha256"]
