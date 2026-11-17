"""D05 full project-flow CPU contract: normal APIs, graph and editable package.

All media are synthetic. The ComfyUI client is the only inference boundary
replaced; this test does not establish neural quality or GPU evidence.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import time
import wave
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from comfy_studio.app import create_app
from comfy_studio.comfyui_client import ComfyUIOutputFile
from comfy_studio.config import StudioConfig
from comfy_studio.engines.comfyui import ComfyUIEngine
from comfy_studio.engines.editor import EditorEngine
from comfy_studio.engines.qa import QaEngine
from comfy_studio.orchestration import ProductionCheckpointer, ProductionService
from comfy_studio.orchestration.adapters import build_server_collaborators
from comfy_studio.orchestration.graph import langgraph_available
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app
from comfy_studio.templates.registry import TemplateRegistry

_SUPPORTED_BROWSER_NAMES = (
    "chromium",
    "google-chrome",
    "google-chrome-stable",
    "google-chrome-beta",
    "chrome",
    "msedge",
    "microsoft-edge",
    "microsoft-edge-stable",
)

_SUPPORTED_BROWSER_PATHS = (
    # Windows
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    # macOS (Chrome and Edge use the same app-bundle layout)
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    # Linux
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
)


def _find_supported_browser() -> Path | None:
    """Locate a Chromium-family executable on the current platform.

    Honors STUDIO_REMOTION_BROWSER when the operator set it explicitly;
    otherwise resolves via PATH and well-known per-OS install locations.
    """
    explicit = shutil.which(os.environ.get("STUDIO_REMOTION_BROWSER", ""))
    if explicit:
        return Path(explicit)
    for name in _SUPPORTED_BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in _SUPPORTED_BROWSER_PATHS:
        path = Path(candidate)
        if path.is_file():
            return path
    project = Path(__file__).resolve().parents[1] / "remotion"
    for root in (
        project / "node_modules" / ".remotion",
        Path.home() / ".cache" / "remotion",
        Path.home() / ".cache" / "puppeteer",
    ):
        if not root.is_dir():
            continue
        for binary_name in ("chrome-headless-shell", "chrome"):
            for path in root.rglob(binary_name):
                if path.is_file() and os.access(path, os.X_OK):
                    return path
    return None


def _safe_extract_package(archive: Path, destination: Path) -> None:
    """Extract only regular files whose normalized paths stay inside destination."""
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            name = entry.filename
            relative = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (
                not name
                or "\\" in name
                or "\x00" in name
                or relative.is_absolute()
                or PureWindowsPath(name).is_absolute()
                or ".." in relative.parts
                or stat.S_ISLNK(mode)
            ):
                raise ValueError(f"unsafe package member: {name!r}")
            target = (root / Path(*relative.parts)).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"package member escapes destination: {name!r}")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _require_d05_evidence_or_skip(reason: str) -> None:
    if os.environ.get("COMFYUIPOD_REQUIRE_D05_EVIDENCE") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def _mp4(ffmpeg: str, path: Path, *, duration: float = 1.0, hue: int = 0) -> bytes:
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
            f"testsrc2=size=64x48:rate=8:duration={duration}",
            "-vf",
            f"hue=h={hue}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return path.read_bytes()


def _png_bytes(seed: int) -> bytes:
    image = Image.new("RGB", (32, 24), ((seed * 37) % 255, (seed * 67) % 255, (seed * 97) % 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _wav_bytes(seed: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes((int(seed).to_bytes(2, "little", signed=True)) * 800)
    return output.getvalue()


class _FixtureComfyUIClient:
    def __init__(self, ffmpeg: str, root: Path) -> None:
        self.ffmpeg = ffmpeg
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.submissions: list[dict] = []

    async def execute_prompt_job(self, graph: dict, **kwargs):
        self.submissions.append({"graph": graph, **kwargs})
        path = self.root / f"shot-{len(self.submissions):02d}.mp4"
        subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=teal:s=96x64:r=8:d=5",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return SimpleNamespace(
            prompt_id=f"cpu-fixture-{len(self.submissions)}",
            outputs=[ComfyUIOutputFile(node_id="save_video", kind="video", filename=str(path))],
            simulated=True,
        )


class _LocalLlmFixture:
    async def complete(self, messages, *, temperature):
        if "director técnico" in messages[0]["content"]:
            content = json.dumps(
                {
                    "sequences": [
                        {
                            "title": "fixture",
                            "shots": [
                                {
                                    "description": "fixture",
                                    "layers": [
                                        {
                                            "kind": "gen",
                                            "engine": "comfyui",
                                            "params": {"prompt": "fixture"},
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )
        else:
            content = '{"semantic": 1.0, "reason": "fixture"}'
        return SimpleNamespace(content=content)


def test_d05_100_asset_approved_brief_to_editable_package_cpu(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        _require_d05_evidence_or_skip("D05 acceptance requires ffmpeg and ffprobe")
    if not langgraph_available():
        _require_d05_evidence_or_skip("D05 acceptance requires LangGraph")

    monkeypatch.setenv("STUDIO_ORCH_REAL_ENGINES", "0")
    monkeypatch.setenv("MODEL_PROFILE", "all")
    monkeypatch.setenv(
        "STUDIO_REMOTION_PROJECT",
        str(Path(__file__).resolve().parents[1] / "remotion"),
    )
    browser = _find_supported_browser()
    if not browser:
        _require_d05_evidence_or_skip("D05 acceptance requires a Remotion Chromium browser")
    monkeypatch.setenv("STUDIO_REMOTION_BROWSER", str(browser))
    data_root = tmp_path / "studio-data"
    config = StudioConfig(
        model_profile="all",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://fixture-comfyui.local:39172",
        studio_data_dir=data_root,
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
        project_response = client.post(
            "/studio/api/projects", json={"name": "D05 100 asset fixture"}
        )
        assert project_response.status_code == 201, project_response.text
        project_id = project_response.json()["id"]

        valid: list[dict] = []
        reference_ids: list[str] = []
        for index in range(40):
            filename = (
                f"{['apertura', 'desarrollo', 'cierre'][index]}-perro-parque-{index:02d}.png"
                if index < 3
                else f"perro-parque-{index:02d}.png"
            )
            response = client.post(
                f"/studio/api/projects/{project_id}/media",
                files={"file": (filename, _png_bytes(index + 1), "image/png")},
            )
            assert response.status_code == 201, response.text
            valid.append(response.json())
            reference_ids.append(response.json()["id"])

        for index in range(30):
            response = client.post(
                f"/studio/api/projects/{project_id}/media",
                files={"file": (f"audio-{index:02d}.wav", _wav_bytes(index + 1), "audio/wav")},
            )
            assert response.status_code == 201, response.text
            valid.append(response.json())

        source_video = tmp_path / "source.mp4"
        video_bytes = _mp4(ffmpeg, source_video)
        for index in range(30):
            response = client.post(
                f"/studio/api/projects/{project_id}/media",
                files={"file": (f"clip-{index:02d}.mp4", video_bytes, "video/mp4")},
            )
            assert response.status_code == 201, response.text
            valid.append(response.json())

        unsupported = client.post(
            f"/studio/api/projects/{project_id}/media",
            files={"file": ("unsupported.xyz", b"not media", "application/octet-stream")},
        )
        corrupt = client.post(
            f"/studio/api/projects/{project_id}/media",
            files={"file": ("corrupt.mp4", b"", "video/mp4")},
        )
        assert unsupported.status_code == 415
        assert corrupt.status_code == 400
        assert len(client.get(f"/studio/api/projects/{project_id}/media").json()["assets"]) == 100

        source_hashes = {
            item["id"]: hashlib.sha256(
                app.state.store.get_asset_path(project_id, item["id"]).read_bytes()
            ).hexdigest()
            for item in valid
        }
        understanding = client.post(
            f"/studio/api/projects/{project_id}/brief/understand",
            json={"prompt": "reel de perro en parque"},
        )
        assert understanding.status_code == 200, understanding.text
        assert understanding.json()["understanding"]["counts"]["assets"] == 100
        assert len(understanding.json()["understanding"]["assets"]) == 100

        brief_prompt = (
            "Anuncio de 15 segundos de un perro en el parque. "
            "Asegura que cuesta $99."
        )
        brief_response = client.post(
            f"/studio/api/projects/{project_id}/brief",
            json={
                "prompt": brief_prompt,
                "asset_ids": reference_ids,
            },
        )
        assert brief_response.status_code == 201, brief_response.text
        draft = brief_response.json()
        claims = {item["kind"]: item for item in draft["brief"]["claim_assessments"]}
        assert claims["price"]["status"] == "unverified"
        assert "99" not in draft["brief"]["main_message"]
        approved = client.post(
            f"/studio/api/projects/{project_id}/brief/{draft['version']}/approve"
        )
        assert approved.status_code == 200, approved.text
        approved_brief = approved.json()["brief"]
        assert approved_brief["status"] == "approved"
        assert len(approved_brief["asset_facts"]) <= 64

        fixture_comfy = _FixtureComfyUIClient(ffmpeg, data_root / "comfy-output")
        registry = app.state.template_registry or TemplateRegistry()
        collaborators = build_server_collaborators(
            llm_client=_LocalLlmFixture(),
            engines={"comfyui": ComfyUIEngine(fixture_comfy)},
            qa_engine=QaEngine(),
            editor_engine=EditorEngine(backend="remotion"),
            template_registry=registry,
            project_store=app.state.store,
            comfyui_input_dir=str(tmp_path / "comfy-input"),
            data_root=data_root,
        )
        app.state.production_service = ProductionService(
            checkpointer=ProductionCheckpointer(db_path=data_root / "d05-checkpoints.sqlite3"),
            collaborators=collaborators,
            store=app.state.store,
            resource_vram_budget_mb=96 * 1024,
        )

        planned = client.post(
            "/studio/api/productions",
            json={
                "idea": "Anuncio de 15 segundos de perro en parque",
                "project_id": project_id,
                "brief": approved_brief,
            },
        )
        assert planned.status_code == 201, planned.text
        plan = planned.json()
        assert plan["phase"] == "awaiting_approval"
        shots = plan["plan"]["sequences"][0]["shots"]
        assert len(shots) == 3
        assert fixture_comfy.submissions == []
        assert all("99" not in shot["description"] for shot in shots)
        assert all("cuesta" not in shot["layers"][0]["params"]["prompt"] for shot in shots)
        selected = [shot["layers"][0]["params"]["image_asset_id"] for shot in shots]
        assert all(shot["layers"][0]["params"]["reference_selection"]["selected"] for shot in shots)
        assert all(asset_id in reference_ids for asset_id in selected)
        assert all(shot["layers"][0]["params"]["recipe_id"] == "wan22_i2v_720p" for shot in shots)

        result = client.post(f"/studio/api/productions/{plan['production_id']}/approve")
        assert result.status_code == 202, result.text
        # Rendering a three shot Remotion composition can take several minutes
        # on CPU only; the approval endpoint itself remains an immediate 202.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status = client.get(
                f"/studio/api/productions/{plan['production_id']}/status"
            ).json()
            if status["phase"] in {"done", "simulated", "failed", "cancelled", "halted"}:
                break
            time.sleep(0.02)
        after = client.get(f"/studio/api/productions/{plan['production_id']}").json()["plan"]
        assert status["phase"] == "simulated", json.dumps(after["log"][-8:])
        assert status["simulated"] is True
        assert status["execution_mode"] == "simulated"
        assert len(fixture_comfy.submissions) == 3
        assert all("LoadImage" in json.dumps(item["graph"]) for item in fixture_comfy.submissions)

        persisted = client.get(f"/studio/api/productions/{plan['production_id']}").json()["plan"]
        final_shots = persisted["sequences"][0]["shots"]
        assert all(Path(shot["output_path"]).is_file() for shot in final_shots)
        for asset_id, expected_sha in source_hashes.items():
            path = app.state.store.get_asset_path(project_id, asset_id)
            assert path and hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha

        from comfy_studio.delivery_4k import (
            Clip,
            TimelineSpec,
            load_editable_package,
            write_editable_package_archive,
        )

        clips = [
            Clip(
                clip_id=f"d05-{index + 1}",
                source_path=shot["output_path"],
                in_s=0,
                out_s=5,
                source_asset_id=shot["id"],
            )
            for index, shot in enumerate(final_shots)
        ]
        lineage = tmp_path / "d05-lineage.json"
        lineage_payload = {
            "scope": "synthetic_cpu_fixture_only",
            "project_id": project_id,
            "production_id": plan["production_id"],
            "selected_references": [
                {"asset_id": asset_id, "sha256": source_hashes[asset_id]} for asset_id in selected
            ],
            "recipes": [shot["layers"][0]["params"]["recipe_id"] for shot in final_shots],
            "outputs": [
                {
                    "shot_id": shot["id"],
                    "sha256": hashlib.sha256(Path(shot["output_path"]).read_bytes()).hexdigest(),
                }
                for shot in final_shots
            ],
        }
        lineage.write_text(json.dumps(lineage_payload, sort_keys=True, indent=2) + "\n")
        archive = write_editable_package_archive(
            TimelineSpec(name="D05 synthetic CPU fixture", clips=clips),
            tmp_path / "d05-editable.zip",
            additional_artifacts=[
                {
                    "source_path": str(lineage),
                    "filename": "d05-lineage.json",
                    "kind": "provenance",
                    "media_type": "application/json",
                },
                *[
                    {
                        "source_path": str(app.state.store.get_asset_path(project_id, asset_id)),
                        "filename": f"reference-{index + 1}.png",
                        "kind": "reference",
                        "media_type": "image/png",
                        "metadata": {"asset_id": asset_id, "sha256": source_hashes[asset_id]},
                    }
                    for index, asset_id in enumerate(selected)
                ],
            ],
        )
        assert archive.is_file()
        extract = tmp_path / "reopened"
        _safe_extract_package(archive, extract)
        reopened = load_editable_package(extract)
        assert len(reopened.clips) == 3
        manifest = json.loads((extract / "delivery-manifest.json").read_text())
        assert len(manifest["sources"]) == 3
        assert len(manifest["delivery_artifacts"]) == 4
        assert all(
            hashlib.sha256((extract / record["path"]).read_bytes()).hexdigest()
            == record["sha256"]
            for record in manifest["sources"] + manifest["delivery_artifacts"]
        )
        assert (
            json.loads((extract / "masters/d05-lineage.json").read_text())["scope"]
            == "synthetic_cpu_fixture_only"
        )

        from comfy_studio.delivery_4k import DeliveryError

        bad_source = tmp_path / "tampered-source"
        shutil.copytree(extract, bad_source)
        source_path = bad_source / manifest["sources"][0]["path"]
        source_path.write_bytes(source_path.read_bytes() + b"tampered")
        with pytest.raises(DeliveryError, match="hash de medio no coincide"):
            load_editable_package(bad_source)

        bad_artifact = tmp_path / "tampered-artifact"
        shutil.copytree(extract, bad_artifact)
        artifact_path = bad_artifact / manifest["delivery_artifacts"][0]["path"]
        artifact_path.write_bytes(artifact_path.read_bytes() + b"tampered")
        with pytest.raises(DeliveryError, match="hash de artefacto no coincide"):
            load_editable_package(bad_artifact)

        unsafe_manifest = tmp_path / "unsafe-manifest"
        shutil.copytree(extract, unsafe_manifest)
        unsafe_data = json.loads((unsafe_manifest / "delivery-manifest.json").read_text())
        unsafe_data["delivery_artifacts"][0]["path"] = "../../escape.json"
        (unsafe_manifest / "delivery-manifest.json").write_text(
            json.dumps(unsafe_data, sort_keys=True)
        )
        with pytest.raises(DeliveryError, match="ruta de artefacto insegura"):
            load_editable_package(unsafe_manifest)

        for index, member in enumerate(("../escape.json", "C:/escape.json")):
            hostile_archive = tmp_path / f"hostile-{index}.zip"
            with zipfile.ZipFile(hostile_archive, "w") as bundle:
                bundle.writestr(member, "not allowed")
            with pytest.raises(ValueError, match="unsafe package member"):
                _safe_extract_package(
                    hostile_archive, tmp_path / f"hostile-extracted-{index}"
                )
        assert not (tmp_path / "escape.json").exists()
