"""Production-flow coverage for project image selection and ComfyUI staging.

ComfyUI inference is replaced by a fake client. This proves selection, approval,
staging, template binding and output registration, not generated image quality.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from comfy_studio.comfyui_client import ComfyUIOutputFile, PromptResult
from comfy_studio.engines import EngineResult
from comfy_studio.engines.comfyui import ComfyUIEngine
from comfy_studio.orchestration.adapters import build_server_collaborators
from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.graph import langgraph_available
from comfy_studio.orchestration.service import ProductionService
from comfy_studio.projects import ProjectStore
from comfy_studio.templates.registry import TemplateRegistry


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def _png(color: tuple[int, int, int]) -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    scanline = b"\x00" + bytes(color)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanline))
        + _png_chunk(b"IEND", b"")
    )


class _TwoShotPlanner:
    async def complete(self, messages: list[dict[str, str]], *, temperature: float) -> Any:
        if "director técnico" in messages[0]["content"]:
            content = {
                "sequences": [
                    {
                        "title": "two reference shots",
                        "shots": [
                            {
                                "description": "waterfall landscape",
                                "duration_s": 1.0,
                                "layers": [
                                    {
                                        "kind": "gen",
                                        "engine": "comfyui",
                                        "params": {
                                            "capability": "video.image_to_video",
                                            "prompt": "wide cinematic waterfall landscape",
                                            "width": 832,
                                            "height": 480,
                                            "frames": 81,
                                        },
                                    }
                                ],
                            },
                            {
                                "description": "tram interior",
                                "duration_s": 1.0,
                                "layers": [
                                    {
                                        "kind": "gen",
                                        "engine": "comfyui",
                                        "params": {
                                            "capability": "video.image_to_video",
                                            "prompt": "cinematic tram interior",
                                            "width": 832,
                                            "height": 480,
                                            "frames": 81,
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
            return SimpleNamespace(content=json.dumps(content))
        return SimpleNamespace(content='{"semantic": 1.0, "reason": "fixture response"}')


class _MediaFixture:
    def __init__(self, engine: str, output: Path, payload: dict[str, Any]) -> None:
        self.engine = engine
        self.output = output
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def run(self, spec: Any) -> EngineResult:
        self.calls.append(spec.job)
        return EngineResult(
            ok=True,
            engine=self.engine,
            verb="run",
            payload={**self.payload, "output_path": str(self.output)},
        )


class _FakeComfyUIClient:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.submissions: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def execute_prompt_job(self, graph: dict[str, Any], **kwargs: Any) -> PromptResult:
        self.submissions.append((graph, kwargs))
        return PromptResult(
            prompt_id=f"fake-prompt-{len(self.submissions)}",
            outputs=[
                ComfyUIOutputFile(node_id="save_video", kind="video", filename=str(self.output))
            ],
        )


def _fixture_video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "C23 production acceptance requires ffmpeg"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=teal:s=320x180:r=24:d=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_project_references_flow_through_approval_staging_and_wan_submit(
    tmp_path: Path,
) -> None:
    assert langgraph_available(), "production acceptance requires LangGraph"
    output = tmp_path / "fixture.mp4"
    _fixture_video(output)

    store = ProjectStore(tmp_path / "studio-data")
    project = store.create_project("C23 reference selection")
    project_id = project["id"]
    expected_by_term: dict[str, str] = {}
    for index in range(100):
        tags: list[str] = []
        if index == 0:
            tags = ["waterfall"]
        elif index == 1:
            tags = ["tram"]
        asset = store.save_upload(
            project_id,
            filename=f"project_image_{index:03}.png",
            content_type="image/png",
            data=_png((index % 255, (index * 3) % 255, (index * 7) % 255)),
            max_upload_bytes=1024,
            metadata={"tags": tags},
        )
        if tags:
            expected_by_term[tags[0]] = asset["id"]

    assert len(store.list_assets(project_id, kind="image")) == 100
    input_dir = tmp_path / "comfy-input"
    comfy_client = _FakeComfyUIClient(output)
    collaborators = build_server_collaborators(
        llm_client=_TwoShotPlanner(),
        engines={"comfyui": ComfyUIEngine(comfy_client)},
        qa_engine=_MediaFixture("qa", output, {"passed": True}),
        editor_engine=_MediaFixture("editor", output, {}),
        template_registry=TemplateRegistry(),
        project_store=store,
        comfyui_input_dir=str(input_dir),
        data_root=tmp_path,
    )
    db_path = tmp_path / "production.sqlite3"
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path),
        collaborators=collaborators,
        store=store,
        resource_vram_budget_mb=96 * 1024,
    )

    planned = service.plan_production(
        "Use the matching project reference for each shot", project_id=project_id
    )
    assert planned["phase"] == "awaiting_approval"
    assert comfy_client.submissions == []
    summary = planned["plan"]["metadata"]["project_reference_selection"]
    assert summary == {
        "shots_compiled": 2,
        "candidate_assets": 100,
        "selected": 2,
        "excluded": 198,
        "unresolved": 0,
    }

    shots = planned["plan"]["sequences"][0]["shots"]
    assert len(shots) == 2
    expected_selection = [expected_by_term["waterfall"], expected_by_term["tram"]]
    for shot, expected_asset_id in zip(shots, expected_selection, strict=True):
        layer = shot["layers"][0]
        report = layer["params"]["reference_selection"]
        assert report["candidate_count"] == 100
        assert report["capacity"] == 1
        assert [item["asset_id"] for item in report["selected"]] == [expected_asset_id]
        assert len(report["excluded"]) == 99
        assert layer["params"]["image_asset_id"] == expected_asset_id

    production_id = planned["production_id"]
    result = service.approve_plan(production_id)
    assert result["phase"] == "done"
    assert result["status"]["artifact_ready"] is True
    assert len(comfy_client.submissions) == 2
    assert len(result["output_asset_ids"]) >= 1

    submitted_assets: list[str] = []
    for graph, submit in comfy_client.submissions:
        filename = graph["8"]["inputs"]["image"]
        assert filename.startswith("studio-ref-") and filename.endswith(".png")
        assert "{{" not in filename
        reference_asset = next(
            asset
            for asset in store.list_assets(project_id, kind="image")
            if filename
            == ("studio-ref-" + hashlib.sha256(asset["id"].encode()).hexdigest()[:24] + ".png")
        )
        source_path = store.get_asset_path(project_id, reference_asset["id"])
        staged_path = input_dir / filename
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert hashlib.sha256(staged_path.read_bytes()).hexdigest() == source_sha
        assert submit["project_id"] == project_id
        submitted_assets.append(reference_asset["id"])

    assert set(submitted_assets) == set(expected_selection)
    for asset_id in result["output_asset_ids"]:
        registered = store.get_asset(project_id, asset_id)
        assert registered is not None
