"""Real production graph and ComfyUI submit wiring with a fake backend.

The test exercises the installed LangGraph, production service, recipe binding,
template rendering and ComfyUIEngine. Only the network/GPU boundary is replaced;
the fake submitter does not prove model inference or visual quality.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from comfy_studio.comfyui_client import ComfyUIOutputFile, PromptResult
from comfy_studio.engines import EngineResult
from comfy_studio.engines.comfyui import ComfyUIEngine
from comfy_studio.orchestration.adapters import build_server_collaborators
from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.graph import langgraph_available
from comfy_studio.orchestration.service import ProductionService
from comfy_studio.templates.registry import TemplateRegistry


class LocalLlmFixture:
    async def complete(self, messages, *, temperature):
        if "director técnico" in messages[0]["content"]:
            content = json.dumps({
                "sequences": [{
                    "title": "fixture sequence",
                    "shots": [{
                        "description": "solid color test frame",
                        "duration_s": 1.0,
                        "layers": [{
                            "kind": "gen",
                            "engine": "comfyui",
                            "params": {
                                "capability": "video.text_to_video",
                                "prompt": "a teal test frame",
                                "width": 832,
                                "height": 480,
                                "frames": 81,
                            },
                        }],
                    }],
                }],
            })
        else:
            content = '{"semantic": 1.0, "reason": "fixture response"}'
        return SimpleNamespace(content=content)


class LocalMediaFixture:
    def __init__(self, *, engine: str, output: Path, payload: dict) -> None:
        self.engine = engine
        self.output = output
        self.payload = payload
        self.calls: list[dict] = []

    def run(self, spec):
        self.calls.append(spec.job)
        return EngineResult(
            ok=True,
            engine=self.engine,
            verb="run",
            payload={**self.payload, "output_path": str(self.output)},
        )


class FakeComfyUIClient:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.submissions: list[tuple[dict, dict]] = []

    async def execute_prompt_job(self, graph: dict, **kwargs) -> PromptResult:
        self.submissions.append((graph, kwargs))
        return PromptResult(
            prompt_id="fake-prompt",
            outputs=[ComfyUIOutputFile(
                node_id="save_video",
                kind="video",
                filename=str(self.output),
            )],
        )


def _fixture_video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "CPU integration fixture requires ffmpeg"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=teal:s=320x180:r=24:d=1", "-an", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_langgraph_binds_recipe_renders_template_and_submits_comfyui_graph(
    tmp_path: Path,
) -> None:
    assert langgraph_available(), "production test environment must install LangGraph"
    media = tmp_path / "fixture.mp4"
    _fixture_video(media)

    llm = LocalLlmFixture()
    comfy_client = FakeComfyUIClient(media)
    generation = ComfyUIEngine(comfy_client)
    qa = LocalMediaFixture(engine="qa", output=media, payload={"passed": True})
    editor = LocalMediaFixture(engine="editor", output=media, payload={})
    templates = TemplateRegistry()
    collaborators = build_server_collaborators(
        llm_client=llm,
        engines={"comfyui": generation},
        qa_engine=qa,
        editor_engine=editor,
        template_registry=templates,
        data_root=tmp_path,
    )
    db_path = tmp_path / "production.sqlite3"
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path),
        collaborators=collaborators,
        resource_vram_budget_mb=96 * 1024,
    )

    planned = service.plan_production("fixture production")
    production_id = planned["production_id"]
    assert planned["phase"] == "awaiting_approval"
    assert planned["plan"]["metadata"]["execution_mode"] == "real"
    binding = planned["plan"]["metadata"]["recipe_binding"]
    assert binding["bound"] == 1
    layer = planned["plan"]["sequences"][0]["shots"][0]["layers"][0]
    assert layer["params"]["recipe_id"] == "wan22_t2v_480p"
    assert layer["template_id"] == "wan_t2v"
    assert layer["params"]["recipe_engine"] == "wan22"

    result = service.approve_plan(production_id)
    assert result["phase"] == "done"
    assert result["status"]["artifact_ready"] is True
    assert len(comfy_client.submissions) == 1
    graph, submit = comfy_client.submissions[0]
    assert graph and all(isinstance(node.get("class_type"), str) for node in graph.values())
    assert all("{{" not in json.dumps(node) for node in graph.values())
    assert {"project_id", "job_id", "cancel_event"} <= submit.keys()
    assert qa.calls and editor.calls
    assert media.is_file() and media.stat().st_size > 0

    recovered = ProductionService(
        checkpointer=ProductionCheckpointer(db_path),
        collaborators=collaborators,
        resource_vram_budget_mb=96 * 1024,
    )
    assert recovered.production_status(production_id)["phase"] == "done"
