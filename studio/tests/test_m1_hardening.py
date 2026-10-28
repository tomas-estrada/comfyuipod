"""Regresiones M1: degradación de percepción, techo total de subida y honestidad.

- M1.2: sin `sqlite-vec` el servidor de Studio sigue vivo y solo la percepción
  responde 503 con el motivo.
- M1.7 (M3-SEC-003): el total de bytes de archivo de una petición multipart está
  acotado, no solo el de cada parte.
- M1.5: los timeouts de ffmpeg/ffprobe son configurables y el corte es proporcional.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig
from comfy_studio.edl.ffmpeg_edl import (
    DEFAULT_FFMPEG_TIMEOUT_S,
    MAX_FFMPEG_TIMEOUT_S,
    cut_subprocess_timeout,
)


@pytest.fixture()
def no_sqlite_vec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simula un entorno donde la extensión `sqlite-vec` no está instalada."""
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    # Cualquier `import sqlite_vec` falla con ImportError, igual que en un venv sin
    # la extensión; `None` en `sys.modules` es el mecanismo canónico de Python.
    monkeypatch.delitem(sys.modules, "sqlite_vec", raising=False)
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)


def _make_app(
    data_dir: Path,
    transport: httpx.ASGITransport,
    **overrides,
) -> TestClient:
    kwargs = {
        "model_profile": "studio",
        "studio_llm_key": "YOUR_STUDIO_API_KEY_HERE",
        "studio_llm_fallback_key": "YOUR_STUDIO_FALLBACK_KEY_HERE",
        "studio_comfyui_url": "http://stub-comfyui.local:39172",
        "studio_data_dir": data_dir,
        "studio_stub_llm": True,
        "comfy_user": "admin",
        "comfy_pass": "changeme-test-pass-42",
        "require_auth": True,
        "max_upload_bytes": 2 * 1024 * 1024,
    }
    kwargs.update(overrides)
    cfg = StudioConfig(**kwargs)
    tc = TestClient(create_app(config=cfg, comfyui_transport=transport))
    tc.auth = ("admin", "changeme-test-pass-42")
    return tc


def test_app_survives_missing_sqlite_vec_and_degrades_perception_only(
    no_sqlite_vec: None,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """M1.2: antes de este cambio, `import sqlite_vec` a nivel de módulo tumbaba
    TODO `create_app` (proyectos, chat, renders), no solo la percepción."""
    with _make_app(studio_data_dir, stub_comfyui_transport) as client:
        health = client.get("/studio/api/health")
        assert health.status_code == 200, health.text
        body = health.json()
        assert body["status"] == "ok"
        assert body["perception"]["available"] is False
        assert body["perception"]["reason"]
        assert "sqlite" in body["perception"]["reason"].lower()

        # El resto de Studio sigue funcionando.
        created = client.post("/studio/api/projects", json={"name": "Proyecto sin vec"})
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        assert client.get("/studio/api/projects").status_code == 200

        # Los endpoints de percepción degradan de forma explícita, nunca con 500.
        for resp in (
            client.post(f"/studio/api/projects/{project_id}/perception/index", json={}),
            client.post(f"/studio/api/projects/{project_id}/faces/index", json={}),
            client.get(f"/studio/api/projects/{project_id}/perception/search?query=rojo"),
            client.post(
                f"/studio/api/projects/{project_id}/search/faces",
                json={"identity_name": "Ana"},
            ),
            client.post(
                f"/studio/api/projects/{project_id}/search/combined",
                json={"person_name": "Ana", "text_query": "rojo"},
            ),
        ):
            assert resp.status_code == 503, f"{resp.request.url} -> {resp.status_code}"
            assert "Percepción vectorial no disponible" in resp.json()["detail"]


def test_perception_health_reports_available_when_sqlite_vec_present(
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    with _make_app(studio_data_dir, stub_comfyui_transport) as client:
        body = client.get("/studio/api/health").json()
        assert body["perception"]["available"] is True
        assert body["perception"]["reason"] is None


def test_upload_total_request_ceiling_rejects_many_parts_under_per_file_limit(
    tmp_path: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """M1.7 (M3-SEC-003): 4 partes de 10 KiB con límite por archivo de 1 MiB y límite
    total de 32 KiB deben rechazarse con 413 aunque cada parte esté bajo el límite."""
    per_file = 1024 * 1024  # 1 MiB
    total = 32 * 1024  # 32 KiB
    with _make_app(
        tmp_path / "data_total",
        stub_comfyui_transport,
        max_upload_bytes=per_file,
        upload_max_total_bytes=total,
    ) as client:
        project_id = client.post(
            "/studio/api/projects", json={"name": "Total ceiling"}
        ).json()["id"]
        header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        payload = header + b"A" * (10 * 1024 - len(header))
        files = [
            (f"file{i}", (f"clip{i}.mp4", payload, "video/mp4")) for i in range(4)
        ]
        resp = client.post(f"/studio/api/projects/{project_id}/assets", files=files)
        assert resp.status_code == 413, resp.text
        assert "límite TOTAL de subida" in resp.json()["detail"]
        uploads = tmp_path / "data_total" / "projects" / project_id / "uploads"
        assert not uploads.exists() or list(uploads.iterdir()) == []


def test_cut_timeout_scales_with_duration_and_is_capped() -> None:
    """M1.5: un timeout fijo de 60 s por corte fallaba con clips largos.

    Fórmula documentada: `max(base, min(cap, 4×duración + base))`.
    """
    short = cut_subprocess_timeout(3.0, base_s=60.0)
    long = cut_subprocess_timeout(90.0, base_s=60.0)
    assert short >= 60.0  # nunca por debajo de la base
    assert long > short
    assert long == 4.0 * 90.0 + 60.0
    assert cut_subprocess_timeout(10_000.0) == MAX_FFMPEG_TIMEOUT_S
    assert cut_subprocess_timeout(0.0) == DEFAULT_FFMPEG_TIMEOUT_S


def test_config_parses_new_timeout_and_ceiling_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_FFMPEG_TIMEOUT_S", "120")
    monkeypatch.setenv("STUDIO_FFPROBE_TIMEOUT_S", "45")
    monkeypatch.setenv("STUDIO_UPLOAD_MAX_TOTAL_MB", "64")
    monkeypatch.setenv("STUDIO_RECOVERY_DRAIN_S", "3")
    cfg = StudioConfig.from_env()
    assert cfg.studio_ffmpeg_timeout_s == 120.0
    assert cfg.studio_ffprobe_timeout_s == 45.0
    assert cfg.upload_max_total_bytes == 64 * 1024 * 1024
    assert cfg.studio_recovery_drain_s == 3.0
    public = cfg.to_public_dict()
    assert public["STUDIO_FFMPEG_TIMEOUT_S"] == "120.0"
    assert public["STUDIO_UPLOAD_MAX_TOTAL_MB"] == "64"


def test_config_ignores_non_positive_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_FFMPEG_TIMEOUT_S", "0")
    monkeypatch.setenv("STUDIO_UPLOAD_MAX_TOTAL_MB", "-5")
    monkeypatch.setenv("STUDIO_RECOVERY_DRAIN_S", "abc")
    cfg = StudioConfig.from_env()
    assert cfg.studio_ffmpeg_timeout_s > 0
    assert cfg.upload_max_total_bytes > 0
    assert cfg.studio_recovery_drain_s > 0


def test_synthetic_keyframe_is_excluded_from_clip_index(
    studio_data_dir: Path,
) -> None:
    """M1.7 (M3-SCR-007): un PNG de relleno marcado `synthetic` no debe entrar en el
    índice CLIP, o la búsqueda semántica "encontraría" planos inexistentes."""
    from comfy_studio.perception.clip_index import ClipVisualIndex
    from comfy_studio.perception.store import PerceptionVectorStore
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="Keyframes sintéticos")
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    asset = store.save_upload(
        project["id"],
        filename="marker.png",
        content_type="image/png",
        data=fake_png,
        max_upload_bytes=1024 * 1024,
        category="keyframes",
        metadata={"source_asset_id": "vid-1", "synthetic": True, "caption": "[SINTÉTICO]"},
    )
    clip = ClipVisualIndex(store, PerceptionVectorStore(studio_data_dir / "index.db"))
    result = clip.index_project_media(project["id"], extract_missing_keyframes=False)
    assert result["shots_indexed"] == 0
    assert result["skipped_synthetic_count"] >= 1
    assert asset["id"] not in {s.get("keyframe_asset_id") for s in result["shots"]}


def test_unverifiable_edl_output_fails_instead_of_reporting_fake_metadata(
    monkeypatch: pytest.MonkeyPatch,
    studio_data_dir: Path,
    tmp_path: Path,
) -> None:
    """M1.7 (M3-IDEA-010): un MP4 que ffprobe no puede verificar no puede registrarse
    como entregable verificado con duración/dimensiones inventadas."""
    import shutil as _shutil
    import subprocess

    from comfy_studio.edl import ffmpeg_edl
    from comfy_studio.projects import ProjectStore

    ffmpeg_bin = _shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        pytest.skip("ffmpeg no disponible en este runner")

    src = tmp_path / "src.mp4"
    proc = subprocess.run(
        [
            ffmpeg_bin, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=#10b981:s=320x180:d=2:r=24",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(src),
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="EDL no verificable")
    fake = store.save_upload(
        project["id"],
        filename="src.mp4",
        content_type="video/mp4",
        data=src.read_bytes(),
        max_upload_bytes=16 * 1024 * 1024,
        category="uploads",
    )
    monkeypatch.setattr(
        ffmpeg_edl,
        "probe_media_info",
        lambda *a, **k: {
            "duration_sec": 0.0,
            "width": 320,
            "height": 180,
            "has_video": True,
            "has_audio": False,
            "probe_ok": False,
        },
    )
    with pytest.raises(RuntimeError, match="no pudo verificarse"):
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project["id"],
            source_asset_id=fake["id"],
            cuts=[{"in_sec": 0.0, "out_sec": 1.0, "reason": "test"}],
        )


def test_parallel_safe_defaults_to_serial_and_readonly_tools_opt_in() -> None:
    """M1.3: el fail-safe es serializar. Solo las herramientas de lectura se marcan."""
    from comfy_studio.agent.tools import create_default_tool_belt

    belt = create_default_tool_belt()
    readonly = {
        "search_assets",
        "find_shots_text",
        "find_shots_face",
        "list_templates",
        "job_status",
        "system_status",
        "export_edl",
        "export_subtitles",
    }
    spec_default = type(belt.get("tag_asset")).__dataclass_fields__["parallel_safe"]
    assert spec_default.default is False
    for name in readonly:
        assert belt.get(name).parallel_safe is True, name
    for name in belt.list_names():
        if name in readonly:
            continue
        assert belt.get(name).parallel_safe is False, f"{name} debe serializarse"
