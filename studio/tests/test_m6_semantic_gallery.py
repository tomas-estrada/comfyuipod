"""Tests M6+: aceptación semántica (visión) e integración producción -> galería."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from comfy_studio.engines import EngineResult, EngineRunSpec
from comfy_studio.orchestration.adapters import (
    SemanticQaEvaluator,
    VisionShotEvaluator,
    _parse_semantic_json,
    build_server_collaborators,
)
from comfy_studio.orchestration.state import (
    ProductionState,
    Sequence,
    Shot,
    ShotStatus,
)

FFMPEG = shutil.which("ffmpeg")


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMClient:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(
        self, messages: Any, *, temperature: float = 0.2
    ) -> _FakeLLMResponse:
        self.calls += 1
        return _FakeLLMResponse(self._content)


class _FakeQaEngine:
    def run(self, spec: EngineRunSpec) -> EngineResult:
        return EngineResult(
            ok=True,
            engine="qa",
            verb="run",
            payload={"passed": True, "vbench": {"score": 0.9}},
        )


# --- parse del JSON semántico -------------------------------------------------


def test_parse_semantic_json_valid_and_invalid() -> None:
    assert _parse_semantic_json('{"semantic": 0.85, "reason": "ok"}') == pytest.approx(0.85)
    assert _parse_semantic_json("El plano es genial: {\"semantic\": 0.7}") == pytest.approx(0.7)
    # basura -> 0.0 (fail-closed)
    assert _parse_semantic_json("no hay json aqui") == 0.0
    assert _parse_semantic_json("") == 0.0
    # fuera de rango -> clamp
    assert _parse_semantic_json('{"semantic": 5}') == 1.0
    assert _parse_semantic_json('{"semantic": -2}') == 0.0


# --- VisionShotEvaluator: fail-closed sin output/frames/LLM -------------------


def test_vision_evaluator_fail_closed_paths(tmp_path: Path) -> None:
    shot = Shot(description="un niño salta a un coche")
    ev = VisionShotEvaluator(_FakeLLMClient('{"semantic": 0.9}'))
    # sin output_path -> 0.0
    assert ev.evaluate(shot) == {"semantic": 0.0}
    # output inexistente -> keyframes vacíos -> 0.0
    shot.output_path = str(tmp_path / "no_existe.mp4")
    assert ev.evaluate(shot) == {"semantic": 0.0}


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg requerido")
def test_vision_evaluator_real_video_scores_from_llm(tmp_path: Path) -> None:
    vid = tmp_path / "clip.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=steelblue:s=320x180:d=1:r=16",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(vid),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    shot = Shot(description="plano azul")
    shot.output_path = str(vid)
    ev = VisionShotEvaluator(_FakeLLMClient('{"semantic": 0.88}'))
    assert ev.evaluate(shot) == {"semantic": pytest.approx(0.88)}


def test_semantic_qa_evaluator_combines_technical_and_semantic() -> None:
    ev = SemanticQaEvaluator(_FakeQaEngine(), _FakeLLMClient('{"semantic": 0.7}'))
    shot = Shot(description="d")
    # output sin archivo real: la QA técnica (fake) puntúa alto pero la
    # semántica no puede extraer keyframes -> fail-closed a 0.0.
    shot.output_path = "/tmp/shot_sin_archivo.mp4"
    scores = ev.evaluate(shot)
    assert scores["vbench"] == pytest.approx(0.9)
    assert scores["semantic"] == 0.0


def test_server_collaborators_wire_semantic_evaluator() -> None:
    collab = build_server_collaborators(
        llm_client=_FakeLLMClient('{"semantic": 0.9}'),
        qa_engine=_FakeQaEngine(),
    )
    assert isinstance(collab.evaluator, SemanticQaEvaluator)
    assert collab.evaluator._vision is not None  # noqa: SLF001


# --- Producción -> galería del proyecto ---------------------------------------


def _accepted_state_with_files(tmp_path: Path) -> ProductionState:
    state = ProductionState(idea="reel de prueba")
    state.metadata["project_id"] = "default"
    clip = tmp_path / "shot_a.mp4"
    clip.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 32)
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 32)
    shot = Shot(description="shot a")
    shot.status = ShotStatus.ACCEPTED
    shot.output_path = str(clip)
    seq = Sequence(title="s")
    seq.shots.append(shot)
    state.sequences.append(seq)
    state.final_output_path = str(reel)
    return state


def test_register_outputs_in_gallery(tmp_path: Path) -> None:
    from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
    from comfy_studio.orchestration.service import ProductionService
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(studio_data_dir=tmp_path / "data")
    store.create_project(name="Proyecto", project_id="default")
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "ck.sqlite3"),
        store=store,
    )
    state = _accepted_state_with_files(tmp_path)
    registered = service._register_outputs_in_gallery(state)  # noqa: SLF001

    assert len(registered) == 2  # shot + reel
    assets = store.list_assets("default", category="outputs")
    assert len(assets) == 2
    shot_asset = next(
        a for a in assets if (a.get("metadata") or {}).get("shot_id")
    )
    reel_asset = next(
        a for a in assets if (a.get("metadata") or {}).get("is_final_reel")
    )
    assert shot_asset["metadata"]["production_id"] == state.id
    assert shot_asset["metadata"]["production_render"] is True
    assert reel_asset["metadata"]["production_id"] == state.id
    assert state.metadata["output_asset_ids"] == registered


def test_register_outputs_tolerant_to_missing_files(tmp_path: Path) -> None:
    from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
    from comfy_studio.orchestration.service import ProductionService
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(studio_data_dir=tmp_path / "data")
    store.create_project(name="Proyecto", project_id="default")
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "ck.sqlite3"),
        store=store,
    )
    state = _accepted_state_with_files(tmp_path)
    state.all_shots()[0].output_path = str(tmp_path / "inexistente.mp4")
    state.final_output_path = None
    registered = service._register_outputs_in_gallery(state)  # noqa: SLF001
    assert registered == []
    # el evento de omisión queda registrado en el historial del estado
    kinds = [e.get("kind") for e in state.log]
    assert "gallery_skip" in kinds


def test_plan_production_binds_project_fail_closed(tmp_path: Path) -> None:
    from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
    from comfy_studio.orchestration.service import ProductionService
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(studio_data_dir=tmp_path / "data")
    store.create_project(name="Proyecto", project_id="default")
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "ck.sqlite3"),
        store=store,
    )
    plan = service.plan_production(
        "un niño conduce un coche rápido", project_id="default"
    )
    assert plan["plan"]["metadata"]["project_id"] == "default"
    status = service.production_status(plan["production_id"])
    assert status["project_id"] == "default"
    # proyecto inexistente -> fail-closed al PLANIFICAR, no al aprobar
    with pytest.raises(LookupError, match="no encontrado"):
        service.plan_production("idea", project_id="no-existe")
