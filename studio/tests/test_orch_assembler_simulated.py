"""El ensamblaje propaga el estado real/simulado del editor (T3).

`EngineAssembler.assemble` reconstruia `{"ok", "output_path", "clips"}` y
tiraba el `simulated` del `EngineResult`: un reel simulado (stub del editor
o backend sin decidir) llegaba al grafo como un `ok:True` indistinguible de
un render real.
"""

from __future__ import annotations

from comfy_studio.engines import EngineResult, EngineRunSpec
from comfy_studio.orchestration.adapters import EngineAssembler


class _StubEditor:
    """Editor simulado: exito marcado como simulado, como el stub real."""

    name = "editor"

    def run(self, spec: EngineRunSpec) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={"output": spec.job["output_path"], "clips": 1},
            simulated=True,
        )


class _RealEditor:
    """Editor real: exito no simulado."""

    name = "editor"

    def run(self, spec: EngineRunSpec) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={"output": spec.job["output_path"], "clips": 1},
            simulated=False,
        )


def _clips() -> list[dict]:
    return [{"path": "/out/a.mp4", "duration_s": 5.0}]


def test_assemble_propagates_simulated_from_stub_editor() -> None:
    res = EngineAssembler(_StubEditor()).assemble("prod_1", _clips())
    assert res["ok"] is True
    assert res.get("simulated") is True, "un reel simulado no puede verse como exito real"


def test_assemble_marks_real_editor_as_not_simulated() -> None:
    res = EngineAssembler(_RealEditor()).assemble("prod_1", _clips())
    assert res["ok"] is True
    assert res.get("simulated") is False
    assert res["output_path"].endswith("prod_1_reel.mp4")
    assert res["clips"] == 1
