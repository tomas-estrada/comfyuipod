"""Production service integration through the installed LangGraph runtime.

The local fixtures replace only network/GPU-facing dependencies. ProductionService,
server collaborator composition, the conductor, recipe binding, and SQLite remain
the real application path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from comfy_studio.engines import EngineResult
from comfy_studio.orchestration.adapters import build_server_collaborators
from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.graph import ProductionGraph, langgraph_available
from comfy_studio.orchestration.service import ProductionService


class LocalLlmFixture:
    """Deterministic local response fixture for planner and semantic adapter calls."""

    async def complete(self, messages, *, temperature):
        system = messages[0]["content"]
        if "director técnico" in system:
            content = json.dumps({
                "sequences": [{
                    "title": "fixture sequence",
                    "shots": [{
                        "description": "solid color test frame",
                        "duration_s": 1.0,
                        "layers": [{
                            "kind": "gen",
                            "engine": "qwen-image21",
                            "params": {
                                "capability": "image.generate",
                                "width": 512,
                                "height": 512,
                                "frames": 1,
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


def test_production_service_uses_langgraph_with_composed_collaborators_and_sqlite(
    tmp_path: Path, monkeypatch,
) -> None:
    assert langgraph_available(), "production test environment must install LangGraph"

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "CPU integration fixture requires ffmpeg"
    media = tmp_path / "fixture.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=teal:s=320x180:r=24:d=1", "-an", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(media)],
        check=True,
        capture_output=True,
        text=True,
    )

    llm = LocalLlmFixture()
    generation = LocalMediaFixture(engine="qwen-image21", output=media, payload={})
    qa = LocalMediaFixture(engine="qa", output=media, payload={"passed": True})
    editor = LocalMediaFixture(engine="editor", output=media, payload={})
    collaborators = build_server_collaborators(
        llm_client=llm,
        engines={"qwen-image21": generation},
        qa_engine=qa,
        editor_engine=editor,
        data_root=tmp_path,
    )

    compiled = []
    original_compile = ProductionGraph.compile_langgraph

    def observe_compile(self):
        result = original_compile(self)
        compiled.append(result)
        return result

    monkeypatch.setattr(ProductionGraph, "compile_langgraph", observe_compile)
    db_path = tmp_path / "production.sqlite3"
    checkpointer = ProductionCheckpointer(db_path)
    service = ProductionService(
        checkpointer=checkpointer,
        collaborators=collaborators,
        resource_vram_budget_mb=96 * 1024,
    )

    planned = service.plan_production("fixture production")
    production_id = planned["production_id"]
    assert planned["phase"] == "awaiting_approval"
    assert planned["plan"]["metadata"]["execution_mode"] == "real"
    binding = planned["plan"]["metadata"]["recipe_binding"]
    assert binding["estimate_only"] is True
    assert binding["bound"] + binding["skipped_has_template"] == 1
    layer = planned["plan"]["sequences"][0]["shots"][0]["layers"][0]
    assert layer["params"]["lease_vram_mb"] > 0

    result = service.approve_plan(production_id)
    assert result["phase"] == "done"
    assert result["status"]["artifact_ready"] is True
    assert generation.calls and qa.calls and editor.calls
    assert compiled and compiled[0] is not None

    # A fresh service/checkpointer instance recovers the terminal snapshot from disk.
    recovered = ProductionService(
        checkpointer=ProductionCheckpointer(db_path),
        collaborators=collaborators,
        resource_vram_budget_mb=96 * 1024,
    )
    status = recovered.production_status(production_id)
    assert status["phase"] == "done"
    assert status["final_output_path"] == str(media)
