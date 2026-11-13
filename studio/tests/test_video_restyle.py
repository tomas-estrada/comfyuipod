"""Restilización CPU dirigida por el usuario (C22): gradación con LUT local.

Ruta explícita y acotada: el usuario aporta el contenido ``.cube`` de un LUT 3D
local y opcionalmente una máscara (asset de imagen del proyecto). La salida es
un artefacto real (PNG o ffv1 lossless) con provenance (sha256 de fuente, LUT y
máscara) y una métrica temporal de estabilidad medida sobre píxeles
decodificados. Esto NO es transferencia de estilo neuronal ni ningún modelo:
solo la transformación de color declarada por el usuario.
"""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from comfy_studio.media_bins import find_ffmpeg
from comfy_studio.video_restyle import parse_lut_cube

_NEEDS_FFMPEG = find_ffmpeg() is None or shutil.which("ffprobe") is None

pytestmark = pytest.mark.skipif(
    _NEEDS_FFMPEG, reason="lut-grade real requiere ffmpeg y ffprobe"
)

_GREEN = (34, 177, 76)
_RED = (200, 30, 30)


def _channel_swap_cube(size: int = 2) -> str:
    """LUT 3D determinista: salida = (B, R, G) del valor de entrada."""
    lines = [f"LUT_3D_SIZE {size}"]
    for b_index in range(size):
        for g_index in range(size):
            for r_index in range(size):
                r = r_index / (size - 1)
                g = g_index / (size - 1)
                b = b_index / (size - 1)
                # salida (B, R, G): la primera columna cambia más rápido (R)
                lines.append(f"{b:.6f} {r:.6f} {g:.6f}")
    return "\n".join(lines) + "\n"


def _upload_video(
    client: TestClient, project_id: str, *, fps: int = 4, frames: int = 4
) -> str:
    """Sube un MP4 real: fondo verde estático + cuadrado rojo en movimiento."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for i in range(frames):
            img = Image.new("RGB", (64, 48), _GREEN)
            x = 8 + i * 12
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
                str(fps),
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
        files={"file": ("restyle-in.mp4", data, "video/mp4")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload_image(client: TestClient, project_id: str, img: Image.Image) -> str:
    output = io.BytesIO()
    img.save(output, format="PNG")
    response = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": ("mask.png", output.getvalue(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _decode_rgb(video_path: Path, out_dir: Path) -> list[Image.Image]:
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
            frames.append(im.convert("RGB").copy())
    return frames


def _assert_blueish(pixel: tuple[int, int, int]) -> None:
    # (34,177,76) tras el intercambio de canales ~ (76,34,177): azul dominante
    assert pixel[2] > 120 and pixel[2] > pixel[1] and pixel[2] > pixel[0], pixel


def test_lut_grade_video_produces_graded_output_and_provenance(
    client: TestClient,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Restyle"}).json()["id"]
    asset_id = _upload_video(client, project_id)
    response = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/lut-grade",
        json={"asset_id": asset_id, "lut_cube": _channel_swap_cube()},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    # Reclamos acotados: gradación con LUT declarada por el usuario.
    assert body["style"] == "user_directed_lut_grade"
    assert body["neural_style_transfer"] == "not_claimed"
    evidence = body["evidence"]
    assert evidence["renderer"] == "ffmpeg-lut3d-cpu"
    assert evidence["style"] == "user_directed_lut_grade"
    assert evidence["lut"]["grid"] == 2
    assert len(evidence["lut"]["sha256"]) == 64
    assert evidence["source"]["asset_id"] == asset_id
    assert evidence["source"]["sha256"]

    # Provenance persistido en el metadato del asset de salida.
    stored = client.get(
        f"/studio/api/projects/{project_id}/assets/{body['asset']['id']}"
    ).json()
    assert stored["metadata"]["lut_grade"]["source_sha256"] == evidence["source"]["sha256"]
    assert stored["metadata"]["lut_grade"]["lut_sha256"] == evidence["lut"]["sha256"]

    # Salida real verificada con ffprobe: ffv1 lossless sin audio.
    out_asset = body["asset"]
    assert out_asset["kind"] == "video"
    video_response = client.get(out_asset["preview_url"])
    assert video_response.status_code == 200
    probe = evidence["output_probe"]
    assert probe["codec_name"] == "ffv1"
    assert probe["frame_count"] == 4
    assert probe["has_audio"] is False

    # Píxeles reales: fondo verde gradado a azul dominante y estable en el tiempo.
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.mkv"
        out_path.write_bytes(video_response.content)
        frames = _decode_rgb(out_path, Path(tmp))
        assert len(frames) == 4
        for _i, frame in enumerate(frames):
            _assert_blueish(frame.getpixel((2, 2)))
        assert frames[0].getpixel((2, 2)) == frames[3].getpixel((2, 2))
        # El cuadrado rojo en movimiento también se gradó (r->g).
        for i in range(4):
            x = 8 + i * 12 + 4
            pixel = frames[i].getpixel((x, 24))
            assert pixel[1] > pixel[0], (i, pixel)

    # Métrica temporal de estabilidad medida sobre pares adyacentes reales.
    stability = evidence["temporal_stability"]
    assert stability["method"] == "adjacent_frame_rgb_mean_abs_diff"
    assert stability["sampled_pairs"] >= 2
    assert math.isfinite(stability["mean_abs_diff"])
    assert math.isfinite(stability["max_abs_diff"])
    # El cuadrado se mueve: la métrica debe reflejar movimiento real (> 0).
    assert stability["mean_abs_diff"] > 0.0

    # Frame de referencia persistido como PNG real.
    frame_asset = body["frame_asset"]
    assert frame_asset["kind"] == "image"
    frame_response = client.get(frame_asset["preview_url"])
    assert frame_response.status_code == 200
    with Image.open(io.BytesIO(frame_response.content)) as frame:
        frame.load()
        _assert_blueish(frame.convert("RGB").getpixel((2, 2)))


def test_lut_grade_image_with_alpha_mask(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Mask"}).json()["id"]
    image = Image.new("RGB", (32, 16), _GREEN)
    upload = _upload_image(client, project_id, image)
    # Máscara en luma: mitad izquierda 0 (queda original), mitad derecha 255 (gradada).
    mask = Image.new("L", (32, 16), 0)
    for px in range(16, 32):
        for py in range(16):
            mask.putpixel((px, py), 255)
    mask_id = _upload_image(client, project_id, mask)

    response = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/lut-grade",
        json={
            "asset_id": upload,
            "lut_cube": _channel_swap_cube(),
            "mask_asset_id": mask_id,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    out_asset = body["asset"]
    assert out_asset["kind"] == "image"
    assert body["evidence"]["mask"]["asset_id"] == mask_id
    assert len(body["evidence"]["mask"]["sha256"]) == 64

    image_response = client.get(out_asset["preview_url"])
    assert image_response.status_code == 200
    with Image.open(io.BytesIO(image_response.content)) as graded:
        graded.load()
        rgb = graded.convert("RGB")
        # Izquierda sin máscara: verde original intacto.
        assert rgb.getpixel((4, 8)) == _GREEN
        # Derecha gradada: azul dominante.
        _assert_blueish(rgb.getpixel((24, 8)))

    # Sin máscara y sobre imagen: todo el cuadro queda gradado.
    plain = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/lut-grade",
        json={"asset_id": upload, "lut_cube": _channel_swap_cube()},
    )
    assert plain.status_code == 201, plain.text
    plain_url = plain.json()["asset"]["preview_url"]
    with Image.open(io.BytesIO(client.get(plain_url).content)) as graded:
        graded.load()
        _assert_blueish(graded.convert("RGB").getpixel((4, 8)))


def test_lut_grade_strength_zero_returns_original_pixels(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Strength"}).json()["id"]
    asset_id = _upload_video(client, project_id)
    response = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/lut-grade",
        json={"asset_id": asset_id, "lut_cube": _channel_swap_cube(), "strength": 0.0},
    )
    assert response.status_code == 201, response.text
    out_url = response.json()["asset"]["preview_url"]
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.mkv"
        out_path.write_bytes(client.get(out_url).content)
        frames = _decode_rgb(out_path, Path(tmp))
        for frame in frames:
            pixel = frame.getpixel((2, 2))
            assert all(abs(pixel[c] - _GREEN[c]) <= 6 for c in range(3)), pixel


def test_lut_grade_is_deterministic(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Det"}).json()["id"]
    asset_id = _upload_video(client, project_id)
    url = f"/studio/api/projects/{project_id}/raster-layers/lut-grade"
    first = client.post(url, json={"asset_id": asset_id, "lut_cube": _channel_swap_cube()})
    second = client.post(url, json={"asset_id": asset_id, "lut_cube": _channel_swap_cube()})
    assert first.status_code == second.status_code == 201
    first_sha = first.json()["evidence"]["output"]["sha256"]
    second_sha = second.json()["evidence"]["output"]["sha256"]
    assert first_sha == second_sha


def test_lut_grade_rerun_marks_previous_outputs_stale(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Restyle reruns"}).json()["id"]
    source = _upload_video(client, project_id)
    url = f"/studio/api/projects/{project_id}/raster-layers/lut-grade"
    lut = _channel_swap_cube()

    first = client.post(url, json={"asset_id": source, "lut_cube": lut})
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["invalidated_outputs"] == []
    first_ids = {first_body["asset"]["id"], first_body["frame_asset"]["id"]}

    changed = client.post(
        url, json={"asset_id": source, "lut_cube": _channel_swap_cube(4)}
    )
    assert changed.status_code == 201, changed.text
    second_ids = {changed.json()["asset"]["id"], changed.json()["frame_asset"]["id"]}
    assert set(changed.json()["invalidated_outputs"]) == first_ids
    for asset_id in first_ids:
        metadata = client.get(
            f"/studio/api/projects/{project_id}/assets/{asset_id}"
        ).json()["metadata"]
        assert metadata["stale"] is True
        assert metadata["stale_reason"] == "lut_grade_input_changed"
        assert metadata["lut_grade_changed_fields"] == ["lut_sha256"]

    unchanged = client.post(url, json={"asset_id": source, "lut_cube": _channel_swap_cube(4)})
    assert unchanged.status_code == 201, unchanged.text
    assert set(unchanged.json()["invalidated_outputs"]) == second_ids


def test_lut_grade_rejects_bad_inputs(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Bad"}).json()["id"]
    asset_id = _upload_video(client, project_id)
    url = f"/studio/api/projects/{project_id}/raster-layers/lut-grade"

    missing_asset = client.post(url, json={"asset_id": "nope", "lut_cube": _channel_swap_cube()})
    assert missing_asset.status_code == 422

    empty_lut = client.post(url, json={"asset_id": asset_id, "lut_cube": "LUT_3D_SIZE 2\n"})
    assert empty_lut.status_code == 422
    assert "LUT" in empty_lut.json()["detail"]

    one_d = client.post(
        url, json={"asset_id": asset_id, "lut_cube": "LUT_1D_SIZE 8\n0 0 0\n1 1 1\n"}
    )
    assert one_d.status_code == 422

    bad_strength = client.post(
        url,
        json={"asset_id": asset_id, "lut_cube": _channel_swap_cube(), "strength": 1.5},
    )
    assert bad_strength.status_code == 422

    # Máscara de otro proyecto: rechazada.
    other = client.post("/studio/api/projects", json={"name": "Otro"}).json()["id"]
    mask_id = _upload_image(client, other, Image.new("L", (64, 48), 255))
    cross = client.post(
        url,
        json={"asset_id": asset_id, "lut_cube": _channel_swap_cube(), "mask_asset_id": mask_id},
    )
    assert cross.status_code == 422
    assert "proyecto" in cross.json()["detail"]

    # Máscara con dimensiones distintas a la fuente: rechazada.
    wrong_dims = _upload_image(client, project_id, Image.new("L", (8, 8), 255))
    mismatch = client.post(
        url,
        json={
            "asset_id": asset_id,
            "lut_cube": _channel_swap_cube(),
            "mask_asset_id": wrong_dims,
        },
    )
    assert mismatch.status_code == 422
    assert "dimensiones" in mismatch.json()["detail"]

    # Máscara no imagen (un vídeo subido como máscara): rechazada.
    video_as_mask = client.post(
        url,
        json={"asset_id": asset_id, "lut_cube": _channel_swap_cube(), "mask_asset_id": asset_id},
    )
    assert video_as_mask.status_code == 422


def test_lut_grade_rejects_video_beyond_duration_budget(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Largo"}).json()["id"]
    # 31 s a 2 fps: 62 fotogramas pequeños, rápidos de codificar.
    asset_id = _upload_video(client, project_id, fps=2, frames=62)
    response = client.post(
        f"/studio/api/projects/{project_id}/raster-layers/lut-grade",
        json={"asset_id": asset_id, "lut_cube": _channel_swap_cube()},
    )
    assert response.status_code == 422
    assert "presupuesto" in response.json()["detail"]


def test_parse_lut_cube_unit_validation() -> None:
    info = parse_lut_cube(_channel_swap_cube(2))
    assert info["grid"] == 2
    assert info["lines"] == 8

    with pytest.raises(ValueError):
        parse_lut_cube("LUT_1D_SIZE 8\n0 0 0\n")
    with pytest.raises(ValueError):
        parse_lut_cube("LUT_3D_SIZE 2\n0 0 0\n")  # faltan líneas
    with pytest.raises(ValueError):
        parse_lut_cube("LUT_3D_SIZE 2\n0 0 nan\n" + "0 0 0\n" * 7)
    with pytest.raises(ValueError):
        parse_lut_cube("no es un lut")
    with pytest.raises(ValueError):
        parse_lut_cube("LUT_3D_SIZE 200\n" + "0 0 0\n" * 8)
