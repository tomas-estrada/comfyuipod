"""Tests para VAL-SERVER-007: CRUD de proyectos y subida de medios con validación."""

from __future__ import annotations

import base64
import errno
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from tempfile import SpooledTemporaryFile

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import _verify_basic_auth, create_app
from comfy_studio.config import StudioConfig


def test_projects_crud_lifecycle(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    # 1. Crear proyecto
    create_resp = client.post(
        "/studio/api/projects",
        json={
            "name": "Campaña Otoño",
            "description": "Spot de 15s vertical",
            "aspect_ratio": "9:16",
        },
    )
    assert create_resp.status_code in (200, 201)
    project = create_resp.json()
    project_id = project["id"]
    assert project["name"] == "Campaña Otoño"
    assert project["description"] == "Spot de 15s vertical"
    assert project["aspect_ratio"] == "9:16"

    # Verificar estructura de carpetas en STUDIO_DATA_DIR/projects/<id>/
    proj_dir = studio_data_dir / "projects" / project_id
    assert proj_dir.is_dir()
    for subdir in ("uploads", "outputs", "keyframes", "voice"):
        assert (proj_dir / subdir).is_dir()

    # 2. Listar proyectos
    list_resp = client.get("/studio/api/projects")
    assert list_resp.status_code == 200
    projects = list_resp.json()["projects"]
    assert any(p["id"] == project_id for p in projects)

    # 3. Obtener detalle de proyecto
    get_resp = client.get(f"/studio/api/projects/{project_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "Campaña Otoño"

    # 4. Actualizar proyecto
    patch_resp = client.patch(
        f"/studio/api/projects/{project_id}",
        json={"name": "Campaña Otoño v2", "description": "Actualizado"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "Campaña Otoño v2"
    assert patch_resp.json()["description"] == "Actualizado"

    # 5. Borrar proyecto
    del_resp = client.delete(f"/studio/api/projects/{project_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True
    assert not proj_dir.exists()

    # 6. Verificar 404 tras borrado
    missing_resp = client.get(f"/studio/api/projects/{project_id}")
    assert missing_resp.status_code == 404


def test_upload_video_photo_audio_and_asset_lifecycle(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Medios"},
    ).json()
    project_id = proj["id"]
    proj_dir = (studio_data_dir / "projects" / project_id).resolve()

    media_samples = [
        ("clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom", "video/mp4", "video"),
        ("ref.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png", "image"),
        ("voz.wav", b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00", "audio/wav", "audio"),
    ]

    uploaded_ids: list[str] = []
    for filename, payload, mime, expected_kind in media_samples:
        resp = client.post(
            f"/studio/api/projects/{project_id}/assets",
            files={"file": (filename, payload, mime)},
        )
        assert resp.status_code in (200, 201), resp.text
        asset = resp.json()
        uploaded_ids.append(asset["id"])
        assert asset["project_id"] == project_id
        assert asset["kind"] == expected_kind
        assert asset["mime_type"] == mime
        assert asset["size_bytes"] == len(payload)

        # Verificar que el archivo vive bajo STUDIO_DATA_DIR/projects/<id>/
        disk_path = (studio_data_dir / asset["rel_path"]).resolve()
        assert disk_path.is_file()
        assert disk_path.is_relative_to(proj_dir)
        assert disk_path.read_bytes() == payload

        # Descargar el contenido vía API
        dl_resp = client.get(
            f"/studio/api/projects/{project_id}/assets/{asset['id']}/file"
        )
        assert dl_resp.status_code == 200
        assert dl_resp.content == payload

    # Listado de medios del proyecto
    list_resp = client.get(f"/studio/api/projects/{project_id}/assets")
    assert list_resp.status_code == 200
    assets = list_resp.json()["assets"]
    assert len(assets) == 3

    # Filtrado por tipo
    video_only = client.get(
        f"/studio/api/projects/{project_id}/assets?kind=video"
    ).json()["assets"]
    assert len(video_only) == 1
    assert video_only[0]["kind"] == "video"

    # Borrado de un medio individual elimina el archivo de disco y fila SQLite
    first_id = uploaded_ids[0]
    first_asset = next(a for a in assets if a["id"] == first_id)
    first_path = studio_data_dir / first_asset["rel_path"]
    assert first_path.is_file()

    del_asset_resp = client.delete(
        f"/studio/api/projects/{project_id}/assets/{first_id}"
    )
    assert del_asset_resp.status_code == 200
    assert del_asset_resp.json()["deleted"] is True
    assert not first_path.exists()

    remaining = client.get(f"/studio/api/projects/{project_id}/assets").json()["assets"]
    assert len(remaining) == 2


def test_upload_rejects_invalid_mime_size_limit_and_path_traversal(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Validaciones Medios"},
    ).json()
    project_id = proj["id"]
    proj_dir = (studio_data_dir / "projects" / project_id).resolve()

    # 1. MIME no permitido (ejecutable / script / texto)
    bad_mime_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("malware.exe", b"MZ\x90\x00", "application/x-msdownload")},
    )
    assert bad_mime_resp.status_code == 415

    # 2. Extensión peligrosa disfrazada con MIME de vídeo
    bad_ext_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("script.sh", b"#!/bin/sh\necho pwn", "video/mp4")},
    )
    assert bad_ext_resp.status_code == 415

    # 3. Archivo vacío (0 bytes)
    empty_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("empty.mp4", b"", "video/mp4")},
    )
    assert empty_resp.status_code == 400

    # 4. Supera límite de tamaño (2 MiB configurados en fixture studio_config)
    oversized_payload = b"A" * (2 * 1024 * 1024 + 1024)
    too_large_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("huge.mp4", oversized_payload, "video/mp4")},
    )
    assert too_large_resp.status_code == 413

    # Verificar que los rechazos no dejaron archivos huérfanos en el proyecto
    assert list((proj_dir / "uploads").iterdir()) == []

    # 5. Intento de path traversal en filename se sanitiza dentro de projects/<id>/uploads/
    traversal_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("../../../escape_attempt.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert traversal_resp.status_code in (200, 201)
    saved_asset = traversal_resp.json()
    saved_path = (studio_data_dir / saved_asset["rel_path"]).resolve()
    assert saved_path.is_relative_to(proj_dir / "uploads")
    assert not (studio_data_dir / "escape_attempt.png").exists()


async def _aiter_multipart_chunks(
    *,
    boundary: str,
    filename: str,
    content_type: str,
    chunk_size: int,
    total_file_bytes: int,
    progress: dict[str, int],
) -> AsyncIterator[bytes]:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    footer = f"\r\n--{boundary}--\r\n".encode()

    progress["bytes_yielded"] += len(header)
    yield header

    remaining = total_file_bytes
    block = b"V" * chunk_size
    while remaining > 0:
        piece = block if remaining >= chunk_size else b"V" * remaining
        remaining -= len(piece)
        progress["chunks_yielded"] += 1
        progress["bytes_yielded"] += len(piece)
        yield piece

    progress["bytes_yielded"] += len(footer)
    yield footer


def test_upload_rejects_oversized_content_length_without_spooling_to_disk(
    client: TestClient,
    studio_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Hardening Content-Length"},
    ).json()
    project_id = proj["id"]
    limit = 2 * 1024 * 1024  # 2 MiB en fixture studio_config

    rollover_calls = 0
    orig_rollover = SpooledTemporaryFile.rollover

    def _spy_rollover(self: SpooledTemporaryFile[bytes]) -> None:
        nonlocal rollover_calls
        rollover_calls += 1
        return orig_rollover(self)

    monkeypatch.setattr(SpooledTemporaryFile, "rollover", _spy_rollover)

    # 1. Subida multipart con Content-Length > límite: 413 inmediato sin hacer rollover a disco
    oversized_payload = b"A" * (limit + 1024)
    resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("huge.mp4", oversized_payload, "video/mp4")},
    )
    assert resp.status_code == 413
    assert rollover_calls == 0

    # 2. Si Content-Length ya supera el límite, ni siquiera debe consumirse el stream entrante
    stream_reads = 0

    def _exploding_stream() -> Iterator[bytes]:
        nonlocal stream_reads
        stream_reads += 1
        yield b"--boundary\r\n"

    early_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        content=_exploding_stream(),
        headers={
            "Content-Type": "multipart/form-data; boundary=boundary",
            "Content-Length": str(limit + 4096),
        },
    )
    assert early_resp.status_code == 413
    assert stream_reads == 0
    assert rollover_calls == 0
    assert list((studio_data_dir / "projects" / project_id / "uploads").iterdir()) == []


def test_upload_rejects_chunked_stream_exceeding_limit_without_unbounded_spool_or_400(
    app,
    client: TestClient,
    studio_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Hardening Chunked"},
    ).json()
    project_id = proj["id"]
    limit = 2 * 1024 * 1024  # 2 MiB
    chunk_size = 64 * 1024  # 64 KiB por chunk
    total_attempted_bytes = 8 * 1024 * 1024  # 8 MiB (4x el límite)
    boundary = "studio-chunked-boundary-99"

    # Si el disco temporal estuviera lleno (ENOSPC) al intentar rollover,
    # nunca debe acabar en 400 ni agotar el espacio temporal.
    def _enospc_rollover(self: SpooledTemporaryFile[bytes]) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(SpooledTemporaryFile, "rollover", _enospc_rollover)

    progress = {"chunks_yielded": 0, "bytes_yielded": 0}

    async def _run_chunked_upload() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            auth=("admin", "changeme-test-pass-42"),
        ) as async_client:
            return await async_client.post(
                f"/studio/api/projects/{project_id}/assets",
                content=_aiter_multipart_chunks(
                    boundary=boundary,
                    filename="recording.mp4",
                    content_type="video/mp4",
                    chunk_size=chunk_size,
                    total_file_bytes=total_attempted_bytes,
                    progress=progress,
                ),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

    resp = anyio.run(_run_chunked_upload)
    assert resp.status_code == 413, f"Esperado 413, recibido {resp.status_code}: {resp.text}"
    # El stream debe cortarse en cuanto cruza el límite (~2 MiB), nunca leer los 8 MiB completos
    assert progress["bytes_yielded"] <= limit + (2 * chunk_size)
    assert list((studio_data_dir / "projects" / project_id / "uploads").iterdir()) == []


def test_basic_auth_handles_utf8_credentials_and_non_ascii_token(
    tmp_path: Path,
) -> None:
    cfg = StudioConfig(
        studio_data_dir=tmp_path / "data",
        comfy_user="admin",
        comfy_pass="contraseña-segura",
        require_auth=True,
        studio_stub_llm=True,
    )
    app = create_app(config=cfg)
    with TestClient(app) as utf8_client:
        valid_token = base64.b64encode("admin:contraseña-segura".encode()).decode("ascii")
        ok_resp = utf8_client.get(
            "/studio/api/health",
            headers={"Authorization": f"Basic {valid_token}"},
        )
        assert ok_resp.status_code == 200

        wrong_token = base64.b64encode("admin:otra-contraseña".encode()).decode("ascii")
        bad_resp = utf8_client.get(
            "/studio/api/health",
            headers={"Authorization": f"Basic {wrong_token}"},
        )
        assert bad_resp.status_code == 401

    assert _verify_basic_auth("Basic token-con-ñ-no-ascii", "admin", "contraseña-segura") is False

