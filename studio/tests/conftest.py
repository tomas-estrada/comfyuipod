"""Fixtures compartidas para los tests in-process de comfy_studio."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app


@pytest.fixture()
def symlink_or_skip():
    """Crea un symlink de directorio, o salta el test si el SO no lo permite.

    En Windows `os.symlink` exige `SeCreateSymbolicLinkPrivilege` (Developer
    Mode o elevación). El comportamiento bajo prueba —rechazar un destino que
    escapa de la raíz aprobada— es el mismo sin ese privilegio, así que el test
    se salta con motivo en vez de fallar por el entorno. En Linux/macOS (el CI
    que manda) siempre corre.
    """

    def _make(link: Path, target: Path, *, directory: bool = True) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip(
                    "symlink requiere Developer Mode o Administrador en Windows "
                    "(WinError 1314); el caso se cubre en Linux/macOS"
                )
            raise

    return _make


@pytest.fixture()
def stub_comfyui_state() -> ComfyUIStubState:
    return ComfyUIStubState(healthy=True)


@pytest.fixture()
def stub_comfyui_transport(stub_comfyui_state: ComfyUIStubState) -> httpx.ASGITransport:
    stub_app = create_stub_comfyui_app(stub_comfyui_state)
    return httpx.ASGITransport(app=stub_app)


@pytest.fixture()
def studio_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "studio_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture()
def studio_config(studio_data_dir: Path) -> StudioConfig:
    return StudioConfig(
        model_profile="studio",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_data_dir=studio_data_dir,
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass="changeme-test-pass-42",
        require_auth=True,
        max_upload_bytes=2 * 1024 * 1024,  # 2 MiB para tests rápidos de límite
        sse_heartbeat_seconds=0.05,
    )


@pytest.fixture()
def app(
    studio_config: StudioConfig,
    stub_comfyui_transport: httpx.ASGITransport,
):
    return create_app(
        config=studio_config,
        comfyui_transport=stub_comfyui_transport,
    )


@pytest.fixture()
def auth_headers() -> tuple[str, str]:
    return ("admin", "changeme-test-pass-42")


@pytest.fixture()
def client(app, auth_headers: tuple[str, str]) -> TestClient:
    tc = TestClient(app)
    tc.auth = auth_headers
    return tc


@pytest.fixture()
def anon_client(app) -> TestClient:
    return TestClient(app)
