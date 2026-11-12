"""D05 CPU evidence through the normal Studio project and production APIs.

The injected collaborators encode a local synthetic upload with FFmpeg. This
proves API/checkpoint/artifact plumbing only; it is not neural or D05 acceptance.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig
from comfy_studio.orchestration import Collaborators, ProductionCheckpointer, ProductionService
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("D05 CPU product-flow evidence requires local ffmpeg")
    return binary


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_d05_cpu_project_upload_to_production_artifact_via_api(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("D05 CPU product-flow evidence requires local ffprobe")

    data_dir = tmp_path / "studio-data"
    config = StudioConfig(
        model_profile="studio",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_data_dir=data_dir,
        studio_stub_llm=True,
        require_auth=False,
    )
    app = create_app(
        config=config,
        comfyui_transport=httpx.ASGITransport(
            app=create_stub_comfyui_app(ComfyUIStubState(healthy=True))
        ),
    )

    with TestClient(app) as client:
        created = client.post("/studio/api/projects", json={"name": "D05 CPU fixture"})
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]

        source_path = tmp_path / "synthetic-source.mp4"
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=8:duration=1",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source_path),
        ])
        source_bytes = source_path.read_bytes()
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        uploaded = client.post(
            f"/studio/api/projects/{project_id}/media",
            files={"file": ("synthetic-source.mp4", source_bytes, "video/mp4")},
        )
        assert uploaded.status_code == 201, uploaded.text
        source_asset_id = uploaded.json()["id"]
        stored_source = app.state.store.get_asset_path(project_id, source_asset_id)
        assert stored_source is not None
        stored_source_sha = hashlib.sha256(stored_source.read_bytes()).hexdigest()
        assert stored_source_sha == source_sha

        output_dir = tmp_path / "cpu-evidence"
        output_dir.mkdir()

        class FixturePlanner:
            def plan(self, idea: str, *, context=None):
                return {"sequences": [{"title": "CPU fixture", "shots": [{
                    "description": idea,
                    "layers": [{
                        "kind": "gen",
                        "engine": "cpu_fixture",
                        "params": {
                            "source_asset_id": source_asset_id,
                            "source_sha256": source_sha,
                            "evidence_scope": "synthetic_cpu_fixture",
                        },
                    }],
                }]}]}

        class FixtureRunner:
            def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
                assert engine == "cpu_fixture"
                # The executor flattens layer parameters into the engine job.
                params = job
                assert params["source_asset_id"] == source_asset_id
                current_source_sha = hashlib.sha256(stored_source.read_bytes()).hexdigest()
                assert current_source_sha == params["source_sha256"]
                output = output_dir / f"{job['shot']}.mp4"
                _run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(stored_source), "-vf", "hue=h=35,scale=96:64",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
                ])
                return {"ok": True, "output_path": str(output)}

        class FixtureEvaluator:
            def evaluate(self, shot, *, cancel_event=None):
                assert shot.output_path and Path(shot.output_path).is_file()
                probe = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                     "stream=width,height", "-of", "json", shot.output_path],
                    capture_output=True, text=True, timeout=10,
                )
                assert probe.returncode == 0, probe.stderr
                stream = json.loads(probe.stdout)["streams"][0]
                assert (int(stream["width"]), int(stream["height"])) == (96, 64)
                return {"vbench": 1.0, "semantic": 1.0}

        class FixtureAssembler:
            def assemble(self, production_id, clips, *, cancel_event=None):
                assert len(clips) == 1
                output = output_dir / f"{production_id}-reel.mp4"
                _run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", clips[0]["path"], "-c", "copy", str(output),
                ])
                return {"ok": True, "output_path": str(output)}

        collaborators = Collaborators(
            planner=FixturePlanner(),
            runner=FixtureRunner(),
            evaluator=FixtureEvaluator(),
            reselect_template=lambda shot: False,
            replan_shot=lambda shot, state: False,
            assembler=FixtureAssembler(),
        )
        app.state.production_service = ProductionService(
            checkpointer=ProductionCheckpointer(db_path=tmp_path / "production.sqlite3"),
            collaborators=collaborators,
            store=app.state.store,
        )

        planned = client.post("/studio/api/productions", json={
            "idea": f"Synthetic D05 delivery from asset {source_asset_id}",
            "project_id": project_id,
            "cost_cap_usd": 0,
        })
        assert planned.status_code == 201, planned.text
        plan_body = planned.json()
        production_id = plan_body["production_id"]
        assert plan_body["phase"] == "awaiting_approval"
        assert plan_body["plan"]["metadata"]["project_id"] == project_id

        approved = client.post(f"/studio/api/productions/{production_id}/approve")
        assert approved.status_code == 200, approved.text
        result = approved.json()
        assert result["phase"] == "done"
        assert result["status"]["artifact_ready"] is True
        final_path = Path(result["final_output_path"])
        assert final_path.is_file() and final_path.stat().st_size > 0

        plan_response = client.get(f"/studio/api/productions/{production_id}")
        assert plan_response.status_code == 200
        persisted = plan_response.json()["plan"]
        shot = persisted["sequences"][0]["shots"][0]
        source_params = shot["layers"][0]["params"]
        assert source_params["source_asset_id"] == source_asset_id
        assert source_params["source_sha256"] == source_sha
        assert source_params["evidence_scope"] == "synthetic_cpu_fixture"
        assert persisted["metadata"]["artifact_verified"] is True

        gallery = client.get(f"/studio/api/projects/{project_id}/gallery")
        assert gallery.status_code == 200
        generated = [asset for asset in gallery.json()["assets"]
                     if asset["metadata"].get("production_id") == production_id]
        assert len(generated) == 2  # accepted shot and assembled reel
        reel_asset = next(asset for asset in generated if asset["metadata"].get("is_final_reel"))
        reel_path = app.state.store.get_asset_path(project_id, reel_asset["id"])
        assert reel_path is not None
        stored_reel_sha = hashlib.sha256(reel_path.read_bytes()).hexdigest()
        final_path_sha = hashlib.sha256(final_path.read_bytes()).hexdigest()
        assert stored_reel_sha == final_path_sha
        output_probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "json", str(reel_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert output_probe.returncode == 0, output_probe.stderr
        output_stream = json.loads(output_probe.stdout)["streams"][0]
        assert (int(output_stream["width"]), int(output_stream["height"])) == (96, 64)

        evidence = {
            "schema": "comfy_studio.d05_cpu_test_evidence.v1",
            "scope": "cpu_test_only_no_production_or_quality_acceptance",
            "production_id": production_id,
            "source_asset_id": source_asset_id,
            "source_sha256": source_sha,
            "final_asset_id": reel_asset["id"],
            "final_sha256": hashlib.sha256(reel_path.read_bytes()).hexdigest(),
            "video": {"width": 96, "height": 64, "synthetic": True},
        }
        evidence_path = tmp_path / "d05_cpu_test_evidence.json"
        evidence_path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
        reopened_evidence = json.loads(evidence_path.read_text())
        assert reopened_evidence["scope"] == "cpu_test_only_no_production_or_quality_acceptance"
        assert reopened_evidence["source_sha256"] == source_sha
        assert reopened_evidence["final_sha256"] == stored_reel_sha
