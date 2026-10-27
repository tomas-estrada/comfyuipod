"""Tests para VAL-SERVER-007: CRUD de proyectos y subida de medios con validación."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


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
