"""Sonda de salud de motores para strict-check (M4-SCR, F7 seed).

La mitad "estable" del objetivo: un motor roto o no cableado debe fallar el pod
RUIDOSAMENTE al arrancar, no en silencio a mitad de un reel pagado.

Produce un reporte serializable (consumible por el panel y por una sonda del
supervisor Rust `comfyctl/src/strict.rs`), y una aserción estricta que distingue
tres estados por motor:

  - `ready`     : cableado a un backend real y utilizable.
  - `stub`      : corre en modo stub (`STUDIO_STUB_<NAME>=1`) — OK para tests/CI,
                  NO para producción.
  - `unwired`   : declarado en el roster pero sin backend real (p.ej. editor a la
                  espera de decisión Remotion vs OTIO+ffmpeg).

`assert_production_ready` falla si un motor REQUERIDO no está `ready`. El
conjunto requerido depende del perfil (M4-OPS-001): no todos los motores están
calientes a la vez, así que solo se exige lo que el perfil activa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from comfy_studio.engines import is_stub_mode
from comfy_studio.engines.registry import EngineRegistry


class EngineNotReadyError(RuntimeError):
    """Un motor requerido no está listo para producción (fail-closed)."""


@dataclass(slots=True)
class EngineHealth:
    name: str
    tier: str
    state: str  # "ready" | "stub" | "unwired"
    note: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tier": self.tier, "state": self.state, "note": self.note}


def _state_for(name: str, wired: bool) -> str:
    if is_stub_mode(name):
        return "stub"
    return "ready" if wired else "unwired"


def probe_engines(registry: EngineRegistry) -> list[EngineHealth]:
    """Estado de cada motor del roster (sin arrancar binarios)."""
    report: list[EngineHealth] = []
    for name in registry.names():
        entry = registry.get(name)
        if entry is None:  # pragma: no cover - names() garantiza presencia
            continue
        report.append(
            EngineHealth(
                name=entry.name,
                tier=str(entry.tier),
                state=_state_for(entry.name, entry.wired),
                note=entry.note,
            )
        )
    return report


def health_report(registry: EngineRegistry) -> dict[str, Any]:
    """Reporte serializable para el panel / strict-check."""
    engines = probe_engines(registry)
    counts = {"ready": 0, "stub": 0, "unwired": 0}
    for h in engines:
        counts[h.state] = counts.get(h.state, 0) + 1
    return {
        "counts": counts,
        "engines": [h.to_public_dict() for h in engines],
    }


def assert_production_ready(registry: EngineRegistry, required: list[str]) -> None:
    """Falla ruidosamente si algún motor REQUERIDO no está `ready`.

    `stub` y `unwired` NO cuentan como listos en producción: un motor en stub
    finge trabajo y uno sin cablear no puede hacerlo. En CI/tests se usa un
    `required` vacío o se aceptan stubs explícitamente arriba.
    """
    by_name = {h.name: h for h in probe_engines(registry)}
    problems: list[str] = []
    for name in required:
        h = by_name.get(name)
        if h is None:
            problems.append(f"{name}: no está en el roster")
        elif h.state != "ready":
            problems.append(f"{name}: estado '{h.state}' (se requiere 'ready')")
    if problems:
        raise EngineNotReadyError(
            "motores requeridos no listos para producción: " + "; ".join(problems)
        )
