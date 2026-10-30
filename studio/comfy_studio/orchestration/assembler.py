"""Ensamblaje del reel final (M6 Bloque 4, fase 16).

Combina los shots ACEPTADOS en el reel final descargable con los motores de
ensamblaje (editor = Remotion, compositor = Natron cuando un shot necesita
composición de capas). Es el nodo terminal del grafo: tras él la producción
queda en `done` con `final_output_path`.

Criterio (consistente con el bloque): el ensamblador real envuelve
`EditorEngine`/`CompositorEngine`; por defecto un **stub determinista** que
produce una ruta de reel sin tocar binarios. Solo ensambla shots aceptados; si no
hay ninguno, deja el estado sin reel y lo registra (no inventa salida).
"""

from __future__ import annotations

from typing import Any, Protocol

from comfy_studio.orchestration.state import ProductionState, ShotStatus


class Assembler(Protocol):
    """Contrato del ensamblador (síncrono).

    La implementación real llama al editor (y al compositor si hay capas a
    componer) y devuelve la ruta del reel. El stub lo cumple sin binarios.
    """

    def assemble(
        self, production_id: str, clips: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Une los clips en un reel; devuelve {'ok': bool, 'output_path'?}."""
        ...


class StubAssembler:
    """Ensamblador determinista para CI/tests."""

    def assemble(
        self, production_id: str, clips: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "output_path": f"/tmp/stub/{production_id}_reel.mp4",
            "clips": len(clips),
        }


def assemble_reel(
    state: ProductionState, assembler: Assembler | None = None
) -> ProductionState:
    """Ensambla los shots aceptados en el reel final (nodo `assemble`).

    Recoge los shots ACEPTADOS en orden de plan, los pasa al ensamblador y fija
    `final_output_path`. Deja la producción en `done`.
    """
    asm = assembler or StubAssembler()
    clips = [
        {"shot": s.id, "path": s.output_path, "duration_s": s.duration_s}
        for s in state.all_shots()
        if s.status == ShotStatus.ACCEPTED and s.output_path
    ]

    if not clips:
        state.phase = "done"
        state.record("assemble", clips=0, output=None, note="sin shots aceptados")
        return state

    result = asm.assemble(state.id, clips)
    if not result.get("ok", False):
        state.record(
            "assemble_failed",
            clips=len(clips),
            error=str(result.get("error", "fallo de ensamblaje"))[:500],
        )
        # no se marca done: el grafo puede reintentar o el humano interviene.
        return state

    state.final_output_path = result.get("output_path")
    state.phase = "done"
    state.record("assemble", clips=len(clips), output=state.final_output_path)
    return state
