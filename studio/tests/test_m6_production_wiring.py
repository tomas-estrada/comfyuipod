"""Tests del wiring del servicio de producción (rutas HTTP + tools MCP).

Verifica SIN GPU (colaboradores stub por defecto) el cableado de la fase 17:

  - rutas HTTP públicas: plan -> awaiting_approval, get, status, approve corre
    el lazo stub pero lo informa como `simulated`, sin artefacto descargable;
  - la puerta de gasto: approve de un id inexistente da 404;
  - GOBERNANZA (M6-AGT-001): el agente (MCP) puede planear/leer/estado pero
    `approve_plan` NO está en el catálogo MCP ni es invocable por el agente.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

MCP_URL = "/studio/api/mcp"


def _rpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1):
    payload: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        payload["params"] = params
    return client.post(MCP_URL, json=payload)


def _call_tool(client: TestClient, name: str, arguments: dict | None = None):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})


# --------------------------------------------------------------------------
# Rutas HTTP públicas (accionadas por el humano)
# --------------------------------------------------------------------------


def test_plan_leaves_production_awaiting_approval(client: TestClient) -> None:
    resp = client.post(
        "/studio/api/productions",
        json={"idea": "un dron sobre la ciudad de noche", "cost_cap_usd": 2.0},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["phase"] == "awaiting_approval"
    assert body["production_id"]
    assert body["plan"]["sequences"]


def test_get_and_status_routes(client: TestClient) -> None:
    pid = client.post(
        "/studio/api/productions", json={"idea": "gato skater"}
    ).json()["production_id"]

    got = client.get(f"/studio/api/productions/{pid}")
    assert got.status_code == 200
    assert got.json()["plan"]["id"] == pid

    status = client.get(f"/studio/api/productions/{pid}/status")
    assert status.status_code == 200
    assert status.json()["phase"] == "awaiting_approval"


def test_stub_approval_does_not_claim_a_real_render(client: TestClient) -> None:
    pid = client.post(
        "/studio/api/productions", json={"idea": "playa al amanecer"}
    ).json()["production_id"]

    resp = client.post(f"/studio/api/productions/{pid}/approve")
    assert resp.status_code == 200
    body = resp.json()
    # con stubs deterministas el lazo no debe afirmar que produjo un reel real.
    assert body["phase"] == "simulated"
    assert body["final_output_path"] is None
    assert body["output_asset_ids"] == []
    assert body["status"]["simulated"] is True
    assert body["status"]["artifact_ready"] is False


def test_normal_studio_project_flow_publishes_cpu_video_artifact(
    client: TestClient, tmp_path: Path
) -> None:
    """Project → production API → FFmpeg artifact → gallery/download, CPU only."""
    from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
    from comfy_studio.orchestration.conductor import Collaborators
    from comfy_studio.orchestration.service import ProductionService

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "CI installs ffmpeg before running the Studio suite"

    class Planner:
        def plan(self, idea: str, *, context: dict | None = None) -> dict:
            return {
                "sequences": [{
                    "title": "CPU fixture",
                    "shots": [{
                        "description": idea,
                        "duration_s": 1.0,
                        "layers": [{"kind": "gen", "engine": "ffmpeg_fixture", "params": {}}],
                    }],
                }]
            }

    class Runner:
        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            clip = tmp_path / "shot.mp4"
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "color=c=teal:s=320x180:r=24:d=1",
                 "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                check=True,
                capture_output=True,
                text=True,
            )
            return {"ok": True, "output_path": str(clip)}

    class Evaluator:
        def evaluate(self, _shot, *, cancel_event=None):
            return {"vbench": 1.0, "semantic": 1.0}

    class Assembler:
        def assemble(self, _production_id, clips, *, cancel_event=None):
            final = tmp_path / "final.mp4"
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", clips[0]["path"], "-t", "1", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", str(final)],
                check=True,
                capture_output=True,
                text=True,
            )
            return {"ok": True, "output_path": str(final)}

    client.app.state.production_service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "productions.sqlite3"),
        collaborators=Collaborators(
            planner=Planner(),
            runner=Runner(),
            evaluator=Evaluator(),
            reselect_template=lambda _shot: False,
            replan_shot=lambda _shot, _state: False,
            assembler=Assembler(),
        ),
        store=client.app.state.store,
    )
    project = client.post("/studio/api/projects", json={"name": "CPU render"})
    assert project.status_code == 201
    project_id = project.json()["id"]

    planned = client.post(
        "/studio/api/productions",
        json={"idea": "una escena de prueba", "project_id": project_id},
    )
    assert planned.status_code == 201
    production_id = planned.json()["production_id"]
    assert planned.json()["phase"] == "awaiting_approval"

    approved = client.post(f"/studio/api/productions/{production_id}/approve")
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert result["phase"] == "done"
    assert result["status"]["artifact_ready"] is True
    assert len(result["output_asset_ids"]) == 2

    gallery = client.get(f"/studio/api/projects/{project_id}/assets?category=outputs")
    assert gallery.status_code == 200
    assets = gallery.json()["assets"]
    assert len(assets) == 2
    assert {a["metadata"].get("is_final_reel", False) for a in assets} == {False, True}
    reel = next(a for a in assets if a["metadata"].get("is_final_reel"))
    downloaded = client.get(
        f"/studio/api/projects/{project_id}/assets/{reel['id']}/file"
    )
    assert downloaded.status_code == 200
    assert downloaded.content[4:8] == b"ftyp"


def test_approve_unknown_production_is_404(client: TestClient) -> None:
    resp = client.post("/studio/api/productions/prod_nope/approve")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# GOBERNANZA MCP (M6-AGT-001): el agente NUNCA aprueba
# --------------------------------------------------------------------------


def test_agent_can_plan_and_read_via_mcp(client: TestClient) -> None:
    # plan_production vía MCP deja la producción esperando aprobación.
    resp = _call_tool(client, "plan_production", {"idea": "reel de prueba MCP"})
    result = resp.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    pid = payload["production_id"]
    assert payload["phase"] == "awaiting_approval"

    # get_plan y production_status también funcionan para el agente.
    got = _call_tool(client, "get_plan", {"production_id": pid})
    assert got.json()["result"]["isError"] is False
    st = _call_tool(client, "production_status", {"production_id": pid})
    assert st.json()["result"]["isError"] is False


def test_agent_cannot_approve_via_mcp(client: TestClient) -> None:
    # la puerta de gasto humano NO es invocable por el agente: approve_plan no
    # existe en el catálogo -> "no disponible".
    pid = _call_tool(client, "plan_production", {"idea": "intento de auto-aprobar"})
    payload = json.loads(pid.json()["result"]["content"][0]["text"])
    production_id = payload["production_id"]

    resp = _call_tool(client, "approve_plan", {"production_id": production_id})
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "no disponible" in result["content"][0]["text"]

    # y tras el intento fallido, la producción sigue SIN aprobar.
    st = _call_tool(client, "production_status", {"production_id": production_id})
    st_payload = json.loads(st.json()["result"]["content"][0]["text"])
    assert st_payload["storyboard_approved"] is False


# --------------------------------------------------------------------------
# Cableado del backend de producción y simulación explícita — sin GPU
# --------------------------------------------------------------------------


def _verified_hardware_profile():
    from types import SimpleNamespace

    detected = SimpleNamespace(
        gpu_available=True,
        vram_total_mb=96 * 1024,
        ram_limit_bytes=256 * 1024**3,
    )
    return SimpleNamespace(
        verified=True,
        detected=detected,
        effective=SimpleNamespace(
            vram_usable_mb=90 * 1024,
            ram_usable_mb=240 * 1024,
        ),
    )


def test_studio_profile_defaults_to_real_backend_through_production_api(
    monkeypatch, studio_config, stub_comfyui_transport
) -> None:
    """Studio deployment uses live adapters, recipe binding, and budgets by default."""
    from comfy_studio.app import create_app
    from comfy_studio.orchestration.adapters import (
        EngineAssembler,
        EngineRunnerAdapter,
        SemanticQaEvaluator,
    )
    from comfy_studio.orchestration.leased_runner import LeasedEngineRunner

    monkeypatch.setenv("MODEL_PROFILE", "studio")
    monkeypatch.delenv("STUDIO_ORCH_REAL_ENGINES", raising=False)
    monkeypatch.setenv("STUDIO_STUB_LLM", "1")
    monkeypatch.setenv("STUDIO_STUB_COMFYUI", "1")
    monkeypatch.setattr(
        "comfy_studio.app.effective_profile", _verified_hardware_profile
    )
    app = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)

    collab = app.state.production_service.collaborators
    assert app.state.orchestration_mode == "real"
    assert isinstance(collab.runner, LeasedEngineRunner)
    assert isinstance(collab.runner._inner, EngineRunnerAdapter)  # noqa: SLF001
    assert isinstance(collab.evaluator, SemanticQaEvaluator)
    assert collab.evaluator._vision is not None  # noqa: SLF001
    assert isinstance(collab.assembler, EngineAssembler)
    assert collab.planner is not None
    assert collab.reselect_template is not None

    # Exercise the normal human production HTTP route. Planning remains cheap:
    # it admits resources and binds a recipe without starting a GPU engine.
    client = TestClient(app)
    client.auth = ("admin", "changeme-test-pass-42")
    response = client.post(
        "/studio/api/productions",
        json={"idea": "un plano de dron sobre Medellín"},
    )
    assert response.status_code == 201, response.text
    plan = response.json()["plan"]
    assert plan["metadata"]["execution_mode"] == "real"
    assert plan["metadata"]["simulated"] is False
    binding = plan["metadata"]["recipe_binding"]
    assert binding["vram_budget_mb"] == 90 * 1024
    assert binding["estimate_only"] is True


def test_studio_profile_fails_closed_without_verified_hardware(
    monkeypatch, studio_config, stub_comfyui_transport
) -> None:
    from types import SimpleNamespace

    import pytest

    from comfy_studio.app import create_app

    monkeypatch.setenv("MODEL_PROFILE", "studio")
    monkeypatch.delenv("STUDIO_ORCH_REAL_ENGINES", raising=False)
    monkeypatch.setattr(
        "comfy_studio.app.effective_profile",
        lambda: SimpleNamespace(
            verified=False,
            detected=SimpleNamespace(
                gpu_available=False,
                vram_total_mb=None,
                ram_limit_bytes=None,
            ),
            effective=SimpleNamespace(vram_usable_mb=0, ram_usable_mb=0),
        ),
    )
    with pytest.raises(RuntimeError, match="orquestación real requerida"):
        create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)


def test_real_backend_setup_error_does_not_fall_back_to_stubs(
    monkeypatch, studio_config, stub_comfyui_transport
) -> None:
    import pytest

    from comfy_studio.app import create_app

    monkeypatch.setenv("MODEL_PROFILE", "studio")
    monkeypatch.setenv("STUDIO_ORCH_REAL_ENGINES", "1")
    monkeypatch.setattr(
        "comfy_studio.app.effective_profile", _verified_hardware_profile
    )

    def fail_to_build(**_kwargs):
        raise ValueError("adapter setup failed")

    monkeypatch.setattr("comfy_studio.app.build_server_collaborators", fail_to_build)
    with pytest.raises(RuntimeError, match="orquestación real requerida"):
        create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)


def test_explicit_simulated_mode_keeps_stub_collaborators(
    monkeypatch, studio_config, stub_comfyui_transport
) -> None:
    from comfy_studio.app import create_app

    monkeypatch.setenv("MODEL_PROFILE", "studio")
    monkeypatch.setenv("STUDIO_ORCH_REAL_ENGINES", "0")
    app = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)

    collab = app.state.production_service.collaborators
    assert app.state.orchestration_mode == "simulated"
    assert collab.runner is None
    assert collab.evaluator is None
    assert collab.assembler is None


def test_real_orchestration_threads_the_data_root_to_every_engine_entry_point(
    monkeypatch, studio_config, stub_comfyui_transport
) -> None:
    """El confinamiento de rutas debe llegar a los TRES puntos de entrada de motor.

    Regresión: `run_layer` lo tenía, pero `QaShotEvaluator.evaluate` y
    `EngineAssembler.assemble` llamaban a `_run_engine_sync` directamente, y
    `shot.output_path` se restaura tal cual del checkpoint de SQLite.
    """
    from comfy_studio.app import create_app

    monkeypatch.setenv("MODEL_PROFILE", "studio")
    monkeypatch.delenv("STUDIO_ORCH_REAL_ENGINES", raising=False)
    monkeypatch.setenv("STUDIO_STUB_LLM", "1")
    monkeypatch.setenv("STUDIO_STUB_COMFYUI", "1")
    monkeypatch.setattr(
        "comfy_studio.app.effective_profile", _verified_hardware_profile
    )
    app = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)

    collab = app.state.production_service.collaborators
    runner = getattr(collab.runner, "_inner", None)
    assert runner is not None, "el runner con lease debe envolver al EngineRunnerAdapter"
    assert runner._data_root is not None
    assert runner._allowed_roots, "el runner necesita raíces permitidas"
    tech = getattr(collab.evaluator, "_tech", None)
    assert tech is not None and tech._data_root is not None, (
        "QaShotEvaluator debe recibir data_root"
    )
    assert tech._allowed_roots, "QaShotEvaluator necesita raíces permitidas"
    assert collab.assembler._data_root is not None, (
        "EngineAssembler debe recibir data_root"
    )
    assert collab.assembler._allowed_roots, "EngineAssembler necesita raíces permitidas"
