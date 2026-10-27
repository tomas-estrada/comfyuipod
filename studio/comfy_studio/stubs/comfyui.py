"""Doble HTTP determinista de ComfyUI para desarrollo local y tests sin GPU."""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

_SYNTHETIC_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass
class ComfyUIStubState:
    """Estado en memoria del doble ComfyUI."""

    healthy: bool = True
    prompts: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploads: dict[str, bytes] = field(default_factory=dict)
    interrupted: bool = False
    freed_count: int = 0


def create_stub_comfyui_app(state: ComfyUIStubState | None = None) -> FastAPI:
    stub_state = state if state is not None else ComfyUIStubState()
    app = FastAPI(title="ComfyUI Stub", version="0.1.0")
    app.state.comfyui = stub_state

    @app.get("/system_stats")
    async def system_stats() -> dict[str, Any]:
        if not stub_state.healthy:
            raise HTTPException(status_code=503, detail="ComfyUI stub degraded")
        return {
            "system": {
                "os": "posix",
                "comfyui_version": "0.3.0-stub",
                "python_version": "3.11.9",
                "embedded_python": False,
            },
            "devices": [
                {
                    "name": "cuda:0 NVIDIA GeForce RTX 5090 (stub)",
                    "type": "cuda",
                    "index": 0,
                    "vram_total": 34359738368,
                    "vram_free": 30064771072,
                    "torch_vram_total": 34359738368,
                    "torch_vram_free": 30064771072,
                }
            ],
        }

    @app.post("/prompt")
    async def submit_prompt(payload: dict[str, Any]) -> dict[str, Any]:
        if not stub_state.healthy:
            raise HTTPException(status_code=503, detail="ComfyUI stub degraded")
        prompt_graph = payload.get("prompt")
        if not isinstance(prompt_graph, dict) or not prompt_graph:
            raise HTTPException(
                status_code=400,
                detail={"error": "Invalid prompt graph", "node_errors": {}},
            )
        prompt_id = f"stub-{uuid.uuid4().hex[:12]}"
        number = len(stub_state.prompts) + 1
        record = {
            "prompt_id": prompt_id,
            "number": number,
            "prompt": prompt_graph,
            "client_id": payload.get("client_id", "comfy-studio"),
        }
        stub_state.prompts.append(record)
        out_filename = f"{prompt_id}_out.mp4"
        stub_state.uploads[out_filename] = (
            b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        )
        stub_state.history[prompt_id] = {
            "prompt": [number, prompt_id, prompt_graph, {}, []],
            "outputs": {
                "save_node": {
                    "gifs": [
                        {
                            "filename": out_filename,
                            "subfolder": "",
                            "type": "output",
                            "format": "video/h264-mp4",
                        }
                    ],
                    "images": [
                        {
                            "filename": f"{prompt_id}_preview.png",
                            "subfolder": "",
                            "type": "output",
                        }
                    ],
                }
            },
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [],
            },
        }
        return {
            "prompt_id": prompt_id,
            "number": number,
            "node_errors": {},
        }

    @app.get("/history")
    async def get_all_history() -> dict[str, Any]:
        return dict(stub_state.history)

    @app.get("/history/{prompt_id}")
    async def get_prompt_history(prompt_id: str) -> dict[str, Any]:
        entry = stub_state.history.get(prompt_id)
        if entry is None:
            return {}
        return {prompt_id: entry}

    @app.get("/queue")
    async def get_queue() -> dict[str, Any]:
        return {
            "queue_running": [],
            "queue_pending": [],
        }

    @app.post("/interrupt")
    async def interrupt_execution() -> dict[str, Any]:
        stub_state.interrupted = True
        return {"interrupted": True}

    @app.post("/free")
    async def free_memory(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        stub_state.freed_count += 1
        return {
            "freed": True,
            "unload_models": bool((payload or {}).get("unload_models", True)),
            "free_memory": bool((payload or {}).get("free_memory", True)),
        }

    @app.post("/upload/image")
    async def upload_image(
        image: UploadFile = File(...),
        subfolder: str = Form(""),
        overwrite: str = Form("true"),
    ) -> dict[str, Any]:
        data = await image.read()
        fname = image.filename or f"upload_{uuid.uuid4().hex[:8]}.png"
        stub_state.uploads[fname] = data
        return {
            "name": fname,
            "subfolder": subfolder,
            "type": "input",
            "overwrite": overwrite.lower() == "true",
        }

    @app.get("/view")
    async def view_file(
        filename: str = Query(...),
        subfolder: str = Query(""),
        type: str = Query("output"),
    ) -> Response:
        _ = (subfolder, type)
        content = stub_state.uploads.get(filename, _SYNTHETIC_PNG)
        media_type = "video/mp4" if filename.endswith(".mp4") else "image/png"
        return Response(content=content, media_type=media_type)

    return app


app = create_stub_comfyui_app()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ComfyUI HTTP Stub Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host de escucha")
    parser.add_argument("--port", type=int, default=39172, help="Puerto de escucha")
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
