"""Tests de los adaptadores de servidor (integración de colaboradores reales).

Verifica SIN GPU/LLM/red que cada adaptador envuelve bien su motor:
  - `LLMPlannerAdapter`   parsea el JSON del cliente LLM y cae a plan mínimo;
  - `EngineRunnerAdapter` despacha al motor (síncrono y async), tipifica fallos;
  - `QaShotEvaluator`     mapea el resultado del motor qa a un score vbench;
  - `EngineAssembler`     envuelve el editor y normaliza output_path;
  - `build_server_collaborators` arma un Collaborators que corre el grafo real.

Los motores aquí son FAKES que cumplen el contrato (`run(spec)->EngineResult|dict`),
igual que los stubs reales harían sin backend. El grafo sigue siendo síncrono.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.engines import EngineResult, EngineRunSpec
from comfy_studio.orchestration import (
    EngineAssembler,
    EngineRunnerAdapter,
    LLMPlannerAdapter,
    ProductionCheckpointer,
    ProductionState,
    QaShotEvaluator,
    Shot,
    ShotStatus,
    build_production_graph,
    build_server_collaborators,
    plan_production,
)

# --------------------------------------------------------------------------
# Fakes que cumplen el contrato de cada pieza (sin GPU/red)
# --------------------------------------------------------------------------


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMClient:
    """Cliente LLM async falso: devuelve un JSON de plan fijo."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, messages: Any, *, temperature: float = 0.2) -> _FakeLLMResponse:
        self.calls += 1
        return _FakeLLMResponse(self._content)


class _SyncEngine:
    """Motor síncrono que devuelve un EngineResult ok con output_path."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.runs = 0

    def run(self, spec: EngineRunSpec) -> EngineResult:
        self.runs += 1
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={"output_path": f"/out/{self.name}.mp4"},
        )


class _AsyncEngine:
    """Motor con `run` async (como ComfyUI/Blender)."""

    def __init__(self, name: str = "comfyui") -> None:
        self.name = name
        self.runs = 0

    async def run(self, spec: EngineRunSpec) -> EngineResult:
        self.runs += 1
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={"output_path": f"/out/{self.name}_async.mp4"},
        )


class _FailingEngine:
    def run(self, spec: EngineRunSpec) -> EngineResult:
        return EngineResult(ok=False, engine="bad", verb="run", error="boom")


class _FakeQaEngine:
    """Motor qa falso: 'passed' según acceptance; opcional VBench."""

    def __init__(self, *, passed: bool = True, vbench_score: float | None = None) -> None:
        self._passed = passed
        self._vbench_score = vbench_score

    def run(self, spec: EngineRunSpec) -> EngineResult:
        vbench = None
        if self._vbench_score is not None:
            vbench = {"score": self._vbench_score}
        return EngineResult(
            ok=True,
            engine="qa",
            verb="run",
            payload={"passed": self._passed, "vbench": vbench},
        )


# --------------------------------------------------------------------------
# 1) LLMPlannerAdapter
# --------------------------------------------------------------------------


def test_llm_planner_parses_json_plan():
    content = (
        '{"sequences":[{"title":"s1","shots":['
        '{"description":"abrir","layers":[{"kind":"gen","engine":"comfyui","params":{}}]}'
        "]}]}"
    )
    adapter = LLMPlannerAdapter(_FakeLLMClient(content))
    plan = adapter.plan("un gato en la ciudad")
    assert plan["sequences"][0]["title"] == "s1"
    assert plan["sequences"][0]["shots"][0]["description"] == "abrir"


def test_llm_planner_tolerates_code_fence():
    content = '```json\n{"sequences":[{"title":"x","shots":[]}]}\n```'
    adapter = LLMPlannerAdapter(_FakeLLMClient(content))
    plan = adapter.plan("idea")
    assert plan["sequences"][0]["title"] == "x"


def test_llm_planner_falls_back_on_garbage():
    adapter = LLMPlannerAdapter(_FakeLLMClient("lo siento, no puedo"))
    plan = adapter.plan("idea de reel")
    # plan mínimo: una secuencia, un shot con la idea.
    assert plan["sequences"]
    assert plan["sequences"][0]["shots"][0]["description"] == "idea de reel"


# --------------------------------------------------------------------------
# 2) EngineRunnerAdapter
# --------------------------------------------------------------------------


def test_engine_runner_dispatches_sync_engine():
    eng = _SyncEngine("upscaler")
    runner = EngineRunnerAdapter({"upscaler": eng})
    res = runner.run_layer("upscaler", {"kind": "upscale"}, cost_cap=1.0)
    assert res["ok"] is True
    assert res["output_path"] == "/out/upscaler.mp4"
    assert eng.runs == 1


def test_engine_runner_dispatches_async_engine():
    eng = _AsyncEngine("comfyui")
    runner = EngineRunnerAdapter({"comfyui": eng})
    res = runner.run_layer("comfyui", {"kind": "gen"})
    assert res["ok"] is True
    assert res["output_path"] == "/out/comfyui_async.mp4"
    assert eng.runs == 1


def test_engine_runner_unknown_engine_is_typed_failure():
    runner = EngineRunnerAdapter({"comfyui": _SyncEngine()})
    res = runner.run_layer("nope", {"kind": "gen"})
    assert res["ok"] is False
    assert "no disponible" in res["error"]


def test_engine_runner_engine_failure_is_typed():
    runner = EngineRunnerAdapter({"bad": _FailingEngine()})
    res = runner.run_layer("bad", {"kind": "gen"})
    assert res["ok"] is False
    assert res["error"] == "boom"


# --------------------------------------------------------------------------
# 3) QaShotEvaluator
# --------------------------------------------------------------------------


def test_qa_evaluator_uses_vbench_score_when_present():
    ev = QaShotEvaluator(_FakeQaEngine(passed=True, vbench_score=0.73))
    shot = Shot(description="x", output_path="/out/x.mp4", acceptance={"vbench_min": 0.55})
    assert ev.evaluate(shot) == {"vbench": 0.73}


def test_qa_evaluator_derives_score_from_ffmpeg_verdict():
    ev = QaShotEvaluator(_FakeQaEngine(passed=True))
    shot = Shot(description="x", output_path="/out/x.mp4", acceptance={"vbench_min": 0.55})
    assert ev.evaluate(shot)["vbench"] >= 0.55  # pass -> score alto
    ev_fail = QaShotEvaluator(_FakeQaEngine(passed=False))
    assert ev_fail.evaluate(shot)["vbench"] < 0.55  # fallo -> score bajo


def test_qa_evaluator_no_output_scores_zero():
    ev = QaShotEvaluator(_FakeQaEngine(passed=True))
    shot = Shot(description="x", acceptance={"vbench_min": 0.55})
    assert ev.evaluate(shot) == {"vbench": 0.0}


# --------------------------------------------------------------------------
# 5) EngineAssembler
# --------------------------------------------------------------------------


def test_assembler_wraps_editor_and_returns_output():
    asm = EngineAssembler(_SyncEngine("editor"))
    res = asm.assemble("prod_1", [{"path": "/out/a.mp4", "duration_s": 5.0}])
    assert res["ok"] is True
    assert res["output_path"] == "/out/editor.mp4"
    assert res["clips"] == 1


def test_assembler_editor_failure_is_typed():
    asm = EngineAssembler(_FailingEngine())
    res = asm.assemble("prod_1", [{"path": "/out/a.mp4"}])
    assert res["ok"] is False
    assert res["error"] == "boom"


# --------------------------------------------------------------------------
# build_server_collaborators: el grafo corre con colaboradores reales
# --------------------------------------------------------------------------


def test_server_collaborators_run_full_graph_to_done(tmp_path):
    content = (
        '{"sequences":[{"title":"s","shots":['
        '{"description":"shot a","layers":[{"kind":"gen","engine":"comfyui","params":{}}]}'
        "]}]}"
    )
    engines = {"comfyui": _AsyncEngine("comfyui")}
    collab = build_server_collaborators(
        llm_client=_FakeLLMClient(content),
        engines=engines,
        qa_engine=_FakeQaEngine(passed=True, vbench_score=0.9),
        editor_engine=_SyncEngine("editor"),
    )
    cp = ProductionCheckpointer(db_path=tmp_path / "ck.sqlite3")
    state = ProductionState(idea="prueba de integración", cost_cap_usd=1.0)
    plan_production(state, collab.planner)
    graph = build_production_graph(checkpointer=cp, collaborators=collab)
    final = graph.run(state)
    assert final.phase == "done"
    assert final.final_output_path == "/out/editor.mp4"
    shots = final.all_shots()
    assert all(s.status == ShotStatus.ACCEPTED for s in shots)
