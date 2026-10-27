"""Doble HTTP y WebSocket determinista de ComfyUI para desarrollo local y tests sin GPU."""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

_SYNTHETIC_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)

_SYNTHETIC_MP4 = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    b"\x00\x00\x00\x08free\x00\x00\x00\x20mdatFAKEVIDEOFRAME"
)

_STUB_OBJECT_INFO: dict[str, Any] = {
    "KSampler": {
        "name": "KSampler",
        "category": "sampling",
        "input": {
            "required": {
                "seed": ["INT", {"default": 0, "min": 0}],
                "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
                "cfg": ["FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}],
                "sampler_name": [["euler", "uni_pc", "dpmpp_2m"]],
                "scheduler": [["normal", "simple", "karras"]],
                "denoise": ["FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}],
            }
        },
        "output": ["LATENT"],
    },
    "LoadImage": {
        "name": "LoadImage",
        "category": "image",
        "input": {"required": {"image": ["STRING", {"default": ""}]}},
        "output": ["IMAGE", "MASK"],
    },
    "SaveImage": {
        "name": "SaveImage",
        "category": "image",
        "input": {
            "required": {
                "images": ["IMAGE"],
                "filename_prefix": ["STRING", {"default": "ComfyUI"}],
            }
        },
        "output": [],
    },
    "VHS_LoadVideo": {
        "name": "VHS_LoadVideo",
        "category": "Video Helper Suite 🎥🅥🅗🅢",
        "input": {
            "required": {
                "video": ["STRING", {"default": ""}],
                "force_rate": ["FLOAT", {"default": 0.0}],
                "custom_width": ["INT", {"default": 0}],
                "custom_height": ["INT", {"default": 0}],
                "frame_load_cap": ["INT", {"default": 0}],
                "skip_first_frames": ["INT", {"default": 0}],
                "select_every_nth": ["INT", {"default": 1}],
            }
        },
        "output": ["IMAGE", "INT", "AUDIO", "VHS_VIDEOINFO"],
    },
    "VHS_VideoCombine": {
        "name": "VHS_VideoCombine",
        "category": "Video Helper Suite 🎥🅥🅗🅢",
        "input": {
            "required": {
                "images": ["IMAGE"],
                "frame_rate": ["FLOAT", {"default": 16.0}],
                "loop_count": ["INT", {"default": 0}],
                "filename_prefix": ["STRING", {"default": "AnimateDiff"}],
                "format": [["video/h264-mp4", "image/gif", "image/webp"]],
                "save_output": ["BOOLEAN", {"default": True}],
            }
        },
        "output": ["VHS_FILENAMES"],
    },
}


@dataclass
class ComfyUIStubState:
    """Estado en memoria y comportamientos programables del doble ComfyUI."""

    healthy: bool = True
    prompts: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploads: dict[str, bytes] = field(default_factory=dict)
    interrupted: bool = False
    freed_count: int = 0
    # Comportamiento programable para tests de sondeo, reintentos y errores
    poll_ticks_before_complete: int = 0
    pending_prompts: dict[str, dict[str, Any]] = field(default_factory=dict)
    transient_failures_remaining: int = 0
    transient_failure_status: int = 503
    fail_next_prompt_error: str | None = None
    fail_next_node_errors: dict[str, Any] | None = None
    fail_execution_error: str | None = None
    response_delay_seconds: float = 0.0
    fail_view_status: int | None = None
    fail_view_error: str | None = None
    fail_view_remaining: int | None = None
    fail_view_timeout: bool = False
    view_delay_seconds: float = 0.0


def _upload_storage_key(storage_type: str, subfolder: str, filename: str) -> str:
    clean_sub = subfolder.strip("/\\")
    if clean_sub:
        return f"{storage_type}/{clean_sub}/{filename}"
    return f"{storage_type}/{filename}"


def create_stub_comfyui_app(state: ComfyUIStubState | None = None) -> FastAPI:
    stub_state = state if state is not None else ComfyUIStubState()
    app = FastAPI(title="ComfyUI Stub", version="0.1.0")
    app.state.comfyui = stub_state

    async def _apply_fault_injection() -> None:
        if stub_state.response_delay_seconds > 0:
            await asyncio.sleep(stub_state.response_delay_seconds)
        if stub_state.transient_failures_remaining > 0:
            stub_state.transient_failures_remaining -= 1
            raise HTTPException(
                status_code=stub_state.transient_failure_status,
                detail="Transient ComfyUI stub failure",
            )
        if not stub_state.healthy:
            raise HTTPException(status_code=503, detail="ComfyUI stub degraded")

    @app.get("/system_stats")
    async def system_stats() -> dict[str, Any]:
        await _apply_fault_injection()
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

    @app.get("/object_info")
    async def get_all_object_info() -> dict[str, Any]:
        await _apply_fault_injection()
        return dict(_STUB_OBJECT_INFO)

    @app.get("/object_info/{node_class}")
    async def get_node_object_info(node_class: str) -> dict[str, Any]:
        await _apply_fault_injection()
        info = _STUB_OBJECT_INFO.get(node_class)
        if info is None:
            return {}
        return {node_class: info}

    @app.post("/prompt")
    async def submit_prompt(payload: dict[str, Any]) -> dict[str, Any]:
        await _apply_fault_injection()

        if stub_state.fail_next_prompt_error or stub_state.fail_next_node_errors:
            err_msg = stub_state.fail_next_prompt_error or "Prompt validation failed"
            node_errs = dict(stub_state.fail_next_node_errors or {})
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "prompt_outputs_failed_validation",
                        "message": err_msg,
                    },
                    "node_errors": node_errs,
                },
            )

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
        preview_filename = f"{prompt_id}_preview.png"
        stub_state.uploads[out_filename] = _SYNTHETIC_MP4
        stub_state.uploads[_upload_storage_key("output", "", out_filename)] = (
            _SYNTHETIC_MP4
        )
        stub_state.uploads[preview_filename] = _SYNTHETIC_PNG
        stub_state.uploads[_upload_storage_key("output", "", preview_filename)] = (
            _SYNTHETIC_PNG
        )

        if stub_state.fail_execution_error:
            history_entry: dict[str, Any] = {
                "prompt": [number, prompt_id, prompt_graph, {}, []],
                "outputs": {},
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [
                        [
                            "execution_error",
                            {
                                "prompt_id": prompt_id,
                                "node_id": "2",
                                "node_type": "KSampler",
                                "exception_message": stub_state.fail_execution_error,
                                "exception_type": "RuntimeError",
                            },
                        ]
                    ],
                },
            }
        else:
            history_entry = {
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
                                "filename": preview_filename,
                                "subfolder": "",
                                "type": "output",
                            }
                        ],
                    }
                },
                "status": {
                    "status_str": "success",
                    "completed": True,
                    "messages": [
                        ["execution_start", {"prompt_id": prompt_id}],
                        ["execution_success", {"prompt_id": prompt_id}],
                    ],
                },
            }

        if stub_state.poll_ticks_before_complete > 0:
            stub_state.pending_prompts[prompt_id] = {
                "remaining": stub_state.poll_ticks_before_complete,
                "total": stub_state.poll_ticks_before_complete,
                "number": number,
                "prompt": prompt_graph,
                "history_entry": history_entry,
            }
        else:
            stub_state.history[prompt_id] = history_entry

        return {
            "prompt_id": prompt_id,
            "number": number,
            "node_errors": {},
        }

    @app.get("/history")
    async def get_all_history() -> dict[str, Any]:
        await _apply_fault_injection()
        return dict(stub_state.history)

    @app.get("/history/{prompt_id}")
    async def get_prompt_history(prompt_id: str) -> dict[str, Any]:
        await _apply_fault_injection()
        pending = stub_state.pending_prompts.get(prompt_id)
        if pending is not None:
            if pending["remaining"] > 0:
                pending["remaining"] -= 1
                return {}
            stub_state.pending_prompts.pop(prompt_id, None)
            stub_state.history[prompt_id] = pending["history_entry"]

        entry = stub_state.history.get(prompt_id)
        if entry is None:
            return {}
        return {prompt_id: entry}

    @app.get("/queue")
    async def get_queue() -> dict[str, Any]:
        await _apply_fault_injection()
        running: list[list[Any]] = []
        for pid, pinfo in stub_state.pending_prompts.items():
            running.append([pinfo["number"], pid, pinfo["prompt"], {}, []])
        return {
            "queue_running": running,
            "queue_pending": [],
        }

    @app.post("/interrupt")
    async def interrupt_execution() -> dict[str, Any]:
        stub_state.interrupted = True
        stub_state.pending_prompts.clear()
        return {"interrupted": True}

    @app.post("/free")
    async def free_memory(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        stub_state.freed_count += 1
        return {
            "freed": True,
            "unload_models": bool((payload or {}).get("unload_models", True)),
            "free_memory": bool((payload or {}).get("free_memory", True)),
        }

    async def _handle_upload(
        upload: UploadFile,
        subfolder: str,
        overwrite: str,
        storage_type: str,
        default_ext: str,
    ) -> dict[str, Any]:
        await _apply_fault_injection()
        data = await upload.read()
        fname = upload.filename or f"upload_{uuid.uuid4().hex[:8]}{default_ext}"
        stub_state.uploads[fname] = data
        stub_state.uploads[_upload_storage_key(storage_type, subfolder, fname)] = data
        return {
            "name": fname,
            "subfolder": subfolder,
            "type": storage_type,
            "overwrite": overwrite.lower() == "true",
        }

    @app.post("/upload/image")
    async def upload_image(
        image: UploadFile = File(...),
        subfolder: str = Form(""),
        overwrite: str = Form("true"),
        type: str = Form("input"),
    ) -> dict[str, Any]:
        return await _handle_upload(image, subfolder, overwrite, type, ".png")

    @app.post("/upload/video")
    @app.post("/vhs/upload")
    async def upload_video(
        image: UploadFile | None = File(None),
        video: UploadFile | None = File(None),
        subfolder: str = Form(""),
        overwrite: str = Form("true"),
        type: str = Form("input"),
    ) -> dict[str, Any]:
        chosen = video or image
        if chosen is None:
            raise HTTPException(status_code=400, detail="Missing uploaded file")
        return await _handle_upload(chosen, subfolder, overwrite, type, ".mp4")

    @app.get("/view")
    @app.get("/vhs/viewvideo")
    @app.get("/viewvideo")
    async def view_file(
        filename: str = Query(...),
        subfolder: str = Query(""),
        type: str = Query("output"),
    ) -> Response:
        await _apply_fault_injection()
        if stub_state.view_delay_seconds > 0:
            await asyncio.sleep(stub_state.view_delay_seconds)
        if stub_state.fail_view_timeout:
            should_timeout = (
                stub_state.fail_view_remaining is None
                or stub_state.fail_view_remaining > 0
            )
            if should_timeout:
                if stub_state.fail_view_remaining is not None:
                    stub_state.fail_view_remaining -= 1
                import httpx

                raise httpx.ReadTimeout(
                    stub_state.fail_view_error
                    or f"Simulated /view read timeout for {filename}"
                )
        if stub_state.fail_view_status is not None:
            should_fail = (
                stub_state.fail_view_remaining is None
                or stub_state.fail_view_remaining > 0
            )
            if should_fail:
                if stub_state.fail_view_remaining is not None:
                    stub_state.fail_view_remaining -= 1
                raise HTTPException(
                    status_code=stub_state.fail_view_status,
                    detail=stub_state.fail_view_error
                    or f"Simulated /view HTTP {stub_state.fail_view_status} failure for {filename}",
                )
        scoped_key = _upload_storage_key(type, subfolder, filename)
        content = stub_state.uploads.get(scoped_key)
        if content is None:
            content = stub_state.uploads.get(filename)
        if content is None:
            content = _SYNTHETIC_MP4 if filename.endswith(".mp4") else _SYNTHETIC_PNG
        media_type = "video/mp4" if filename.endswith(".mp4") else "image/png"
        return Response(content=content, media_type=media_type)

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        await websocket.accept()
        client_id = websocket.query_params.get("clientId", "comfy-studio")
        prompt_id = websocket.query_params.get("promptId")
        if not prompt_id and stub_state.prompts:
            prompt_id = stub_state.prompts[-1]["prompt_id"]
        if not prompt_id:
            prompt_id = f"stub-{uuid.uuid4().hex[:8]}"

        try:
            await websocket.send_json(
                {
                    "type": "status",
                    "data": {
                        "status": {"exec_info": {"queue_remaining": 1}},
                        "sid": client_id,
                    },
                }
            )
            await websocket.send_json(
                {
                    "type": "executing",
                    "data": {"node": "1", "prompt_id": prompt_id},
                }
            )
            for step in range(1, 5):
                await websocket.send_json(
                    {
                        "type": "progress",
                        "data": {
                            "value": step,
                            "max": 4,
                            "prompt_id": prompt_id,
                            "node": "2",
                        },
                    }
                )
            if stub_state.fail_execution_error:
                await websocket.send_json(
                    {
                        "type": "execution_error",
                        "data": {
                            "prompt_id": prompt_id,
                            "node_id": "2",
                            "node_type": "KSampler",
                            "exception_message": stub_state.fail_execution_error,
                        },
                    }
                )
            else:
                await websocket.send_json(
                    {
                        "type": "executed",
                        "data": {
                            "node": "3",
                            "prompt_id": prompt_id,
                            "output": {
                                "gifs": [
                                    {
                                        "filename": f"{prompt_id}_out.mp4",
                                        "subfolder": "",
                                        "type": "output",
                                        "format": "video/h264-mp4",
                                    }
                                ]
                            },
                        },
                    }
                )
                await websocket.send_json(
                    {
                        "type": "status",
                        "data": {
                            "status": {"exec_info": {"queue_remaining": 0}},
                            "sid": client_id,
                        },
                    }
                )
                await websocket.send_json(
                    {
                        "type": "executing",
                        "data": {"node": None, "prompt_id": prompt_id},
                    }
                )
        except WebSocketDisconnect:
            return

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
