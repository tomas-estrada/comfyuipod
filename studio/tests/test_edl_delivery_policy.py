"""Pruebas CPU para fps, rango y margen de imagen del render EDL."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfy_studio.edl.ffmpeg_edl import _resolve_bt709_source_matrix, execute_edl_ffmpeg
from comfy_studio.media_bins import find_ffmpeg
from comfy_studio.perception import keyframes
from comfy_studio.perception.keyframes import probe_media_info
from comfy_studio.projects import ProjectStore

FFMPEG = find_ffmpeg()
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="requiere ffmpeg")


@pytest.fixture()
def store_and_project(tmp_path: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(studio_data_dir=tmp_path / "studio")
    project = store.create_project(name="EDL delivery policy")
    return store, project["id"]


def _make_video(path: Path, source: str = "testsrc") -> bytes:
    red_matrix: str | None = None
    if source == "black":
        video_input = "color=c=black:s=128x72:r=24:d=2"
    elif source == "white":
        video_input = "color=c=white:s=128x72:r=24:d=2"
    elif source == "testsrc":
        video_input = "testsrc=duration=2:size=128x72:rate=24"
    elif source == "full-black":
        # Igual que en HEAD: 'full-black' reutiliza la fuente de test; el
        # `-vf` de más abajo convierte los niveles a rango completo.
        video_input = "testsrc=duration=2:size=128x72:rate=24"
    elif source == "red-untagged":
        # Rojo sin VUI de color: ffprobe no reporta color_space/transfer/primaries.
        # 1280x720: `_rgb_at` lee coordenadas del lienzo de entrega (1280x720).
        video_input = "color=c=red:s=1280x720:r=24:d=1"
    elif source == "red-smpte170m":
        # Rojo con matriz BT.601 (SMPTE170M). En VUI H.264 solo el tag de matriz
        # es válido para smpte170m; primaries/transfer smpte170m no se escriben.
        video_input = "color=c=red:s=1280x720:r=24:d=1"
        red_matrix = "smpte170m"
    elif source == "red-bt709":
        video_input = "color=c=red:s=1280x720:r=24:d=1"
        red_matrix = "bt709"
    else:
        raise AssertionError(f"source de test desconocido: {source!r}")
    cmd = [
        FFMPEG,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_input,
    ]
    if source == "full-black":
        # The color lavfi source emits limited-range black (Y=16). Convert its
        # samples before signaling full range so the test source is valid (Y=0).
        cmd.extend(["-vf", "scale=in_range=tv:out_range=pc"])
    elif red_matrix is not None:
        # El RGB→YUV debe usar la misma matriz que el tag declarado.
        cmd.extend(["-vf", f"scale=out_color_matrix={red_matrix}"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "pc" if source == "full-black" else "tv",
            "-bsf:v",
            (
                "h264_metadata=video_full_range_flag=1"
                if source == "full-black"
                else "h264_metadata=video_full_range_flag=0"
            ),
        ]
    )
    if red_matrix == "smpte170m":
        cmd.extend(["-colorspace", "smpte170m"])
    elif red_matrix == "bt709":
        cmd.extend(["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"])
    cmd.append(str(path))
    subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path.read_bytes()


def _upload_source(store: ProjectStore, project_id: str, data: bytes) -> str:
    asset = store.save_upload(
        project_id,
        filename="source.mp4",
        content_type="video/mp4",
        data=data,
        max_upload_bytes=10 * 1024 * 1024,
        category="uploads",
    )
    return asset["id"]


def _save_edl(store: ProjectStore, project_id: str, asset_id: str) -> None:
    store.save_edl_version(
        project_id,
        edl={
            "schema": "comfy_studio.edl.v1",
            "title": "Delivery policy",
            "aspect_ratio": "16:9",
            "source_asset_id": asset_id,
            "cuts": [{"in_sec": 0.0, "out_sec": 1.0}],
        },
    )


def _first_y(path: Path) -> int:
    # Decode to a pixel format matching the stream's declared range; forcing
    # limited yuv420p for full-range output silently remaps the black level.
    pixel_format = "yuvj420p" if probe_media_info(path).get("color_range") == "pc" else "yuv420p"
    proc = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            pixel_format,
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert proc.stdout, proc.stderr.decode("utf-8", errors="replace")
    return proc.stdout[0]


def _rgb_at(path: Path, x: int, y: int) -> tuple[int, int, int]:
    proc = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    width = 1280
    offset = (y * width + x) * 3
    assert len(proc.stdout) >= offset + 3
    return tuple(proc.stdout[offset : offset + 3])


def test_edl_applies_fps_range_and_picture_inset(tmp_path, store_and_project):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "source.mp4", source="white")
    )
    _save_edl(store, project_id, source_id)

    result = execute_edl_ffmpeg(
        store,
        project_id,
        fps=12,
        color_range="full",
        picture_inset_percent=10.0,
    )
    output = Path(store.get_asset_path(project_id, result["output_asset"]["id"]))
    probe = probe_media_info(output)

    assert probe["probe_ok"] is True
    assert result["verification"]["fps"] == 12
    assert probe["fps"] == pytest.approx(12.0, abs=0.5)
    assert result["verification"]["color_range"] == "full"
    assert probe["color_range"] == "pc"
    assert result["verification"]["picture_inset_percent"] == 10.0
    assert _rgb_at(output, 0, 0) == (0, 0, 0)
    assert min(_rgb_at(output, 640, 360)) > 220


def test_color_range_converts_black_level_not_only_metadata(tmp_path, store_and_project):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "black.mp4", source="black")
    )
    _save_edl(store, project_id, source_id)

    full = execute_edl_ffmpeg(store, project_id, color_range="full")
    limited = execute_edl_ffmpeg(store, project_id, color_range="limited")
    full_path = Path(store.get_asset_path(project_id, full["output_asset"]["id"]))
    limited_path = Path(store.get_asset_path(project_id, limited["output_asset"]["id"]))

    assert probe_media_info(full_path)["color_range"] == "pc"
    assert probe_media_info(limited_path)["color_range"] == "tv"
    assert _first_y(full_path) < _first_y(limited_path)


def test_full_range_source_is_converted_to_limited_pixel_levels(tmp_path, store_and_project):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "full-black.mp4", source="full-black")
    )
    _save_edl(store, project_id, source_id)

    limited = execute_edl_ffmpeg(store, project_id, color_range="limited")
    output = Path(store.get_asset_path(project_id, limited["output_asset"]["id"]))

    assert probe_media_info(output)["color_range"] == "tv"
    assert _first_y(output) == pytest.approx(16, abs=2)


@pytest.mark.parametrize(("alias", "canonical"), [("mpeg", "tv"), ("jpeg", "pc")])
def test_probe_normalizes_equivalent_color_range_aliases(tmp_path, monkeypatch, alias, canonical):
    media = tmp_path / "media.mp4"
    media.write_bytes(b"probe fixture")
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "width": 128,
                "height": 72,
                "avg_frame_rate": "24/1",
                "color_range": alias,
            }
        ],
        "format": {"duration": "1.0"},
    }
    monkeypatch.setattr(keyframes, "find_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(
        keyframes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )

    assert keyframes.probe_media_info(media)["color_range"] == canonical


def test_invalid_delivery_policy_is_rejected_before_render(store_and_project):
    store, project_id = store_and_project
    with pytest.raises(ValueError, match="color_range"):
        execute_edl_ffmpeg(
            store, project_id, cuts=[{"in_sec": 0.0, "out_sec": 1.0}], color_range="p3"
        )
    with pytest.raises(ValueError, match="picture_inset_percent"):
        execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
            picture_inset_percent=26.0,
        )
    with pytest.raises(ValueError, match="fps"):
        execute_edl_ffmpeg(store, project_id, cuts=[{"in_sec": 0.0, "out_sec": 1.0}], fps=61)


# --- A22: política de color de entrega BT.709 ----------------------------------
# La petición EDL declara `color_space: "bt709"`; el render debe convertir de
# verdad cada fuente con su matriz conocida, etiquetar la salida (VUI) y
# verificar matriz/primarios/transferencia en el probe. Sin conversión solo
# reetiquetando VUI el rojo decodifica mal: BT.601 reetiquetado como BT.709
# produce (255,24,0) en vez del rojo de referencia (~252,0,0).


def _save_edl_with_color_policy(
    store: ProjectStore, project_id: str, asset_id: str, *, color_range: str
) -> None:
    store.save_edl_version(
        project_id,
        edl={
            "schema": "comfy_studio.edl.v1",
            "title": "Delivery color policy",
            "aspect_ratio": "16:9",
            "source_asset_id": asset_id,
            "color_space": "bt709",
            "color_range": color_range,
            "cuts": [{"in_sec": 0.0, "out_sec": 1.0}],
        },
    )


def _assert_close_rgb(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> None:
    # Tolerancia por reencode lossy ultrafast sobre un campo rojo plano: la
    # referencia directa y la salida convertida difieren ~2; un reetiquetado
    # sin conversión desvía el canal verde ~24, muy por encima de la cota.
    for channel, (got, want) in enumerate(zip(actual, expected, strict=True)):
        assert got == pytest.approx(want, abs=10), f"canal RGB #{channel}"


@pytest.mark.parametrize("color_range", ["limited", "full"])
def test_bt709_policy_converts_known_matrix_source_and_verifies_output_tags(
    tmp_path, store_and_project, color_range
):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "source.mp4", source="red-smpte170m")
    )
    _save_edl_with_color_policy(store, project_id, source_id, color_range=color_range)

    result = execute_edl_ffmpeg(store, project_id, color_range=color_range)
    output = Path(store.get_asset_path(project_id, result["output_asset"]["id"]))
    probe = probe_media_info(output)

    assert probe["color_space"] == "bt709"
    assert probe["color_transfer"] == "bt709"
    assert probe["color_primaries"] == "bt709"
    assert probe["color_range"] == ("pc" if color_range == "full" else "tv")

    verification = result["verification"]
    assert verification["color_space"] == "bt709"
    assert verification["probed_color_space"] == "bt709"
    assert verification["probed_color_transfer"] == "bt709"
    assert verification["probed_color_primaries"] == "bt709"
    assert verification["source_color_metadata"] == [
        {
            "asset_id": source_id,
            "color_space": "smpte170m",
            "color_transfer": None,
            "color_primaries": None,
        }
    ]

    reference_path = tmp_path / "ref709.mp4"
    _make_video(reference_path, source="red-bt709")
    _assert_close_rgb(_rgb_at(output, 640, 360), _rgb_at(reference_path, 640, 360))


def test_bt709_policy_fails_closed_on_untagged_source_metadata(tmp_path, store_and_project):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "source.mp4", source="red-untagged")
    )
    _save_edl_with_color_policy(store, project_id, source_id, color_range="limited")

    with pytest.raises(RuntimeError, match="color_space"):
        execute_edl_ffmpeg(store, project_id, color_range="limited")


@pytest.mark.parametrize(
    ("source_info", "tag"),
    [
        (
            {"color_space": "bt709", "color_transfer": "smpte2084", "color_primaries": "bt709"},
            "smpte2084",
        ),
        (
            {"color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt2020"},
            "bt2020",
        ),
    ],
)
def test_bt709_policy_fails_closed_on_hdr_metadata_despite_bt709_matrix_tag(
    source_info, tag
):
    # Regresión A22: la comprobación HDR debe preceder al early return de
    # matriz bt709; este test usa el resultado ffprobe tipado directamente,
    # porque libx264 no serializa todos los tags HDR de forma portable.
    with pytest.raises(RuntimeError, match=tag):
        _resolve_bt709_source_matrix("hdr-source", source_info)


def test_unsupported_color_space_policy_is_rejected_before_render(store_and_project):
    store, project_id = store_and_project
    with pytest.raises(ValueError, match="color_space"):
        execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
            color_space="p3",
        )


def test_edl_build_route_threads_color_space_policy(client, tmp_path):
    """A22: `/edl/build` pasa `color_space` del body a `execute_edl_ffmpeg`.

    Si el campo no se hilvanara, la política activa no llegaría al ejecutor y
    el render heredado devolvería 200; con el hilo correcto el fail-closed de
    la fuente sin matriz declarada llega como HTTP 400. El mismo render sin
    política confirma que `None` conserva el comportamiento heredado en ruta.
    """
    project_id = client.post("/studio/api/projects", json={"name": "Policy route"}).json()["id"]
    data = _make_video(tmp_path / "source.mp4", source="red-untagged")
    asset_id = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("source.mp4", data, "video/mp4")},
    ).json()["id"]
    body = {"source_asset_id": asset_id, "cuts": [{"in_sec": 0.0, "out_sec": 1.0}]}

    resp_policy = client.post(
        f"/studio/api/projects/{project_id}/edl/build",
        json={**body, "color_space": "bt709"},
    )
    assert resp_policy.status_code == 400, resp_policy.text
    assert "color_space" in resp_policy.json()["detail"]

    resp_legacy = client.post(f"/studio/api/projects/{project_id}/edl/build", json=body)
    assert resp_legacy.status_code == 200, resp_legacy.text
    assert "color_space" not in resp_legacy.json()["verification"]
