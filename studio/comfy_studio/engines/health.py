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

Además, desde la política de arquitectura y recursos, cada motor expone su
**taxonomía de preparación** (`engines/preparedness.py`): software instalado,
integración configurada, pesos requeridos/ausentes, modelo en disco, hardware,
modelo cargado y ejecución validada. `wired` dice «hay backend»; la taxonomía
dice «este motor puede ejecutar aquí y ahora». Son preguntas distintas y un
motor puede estar correctamente instalado y seguir pidiendo pesos de tarea.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from comfy_studio.engines import is_stub_mode
from comfy_studio.engines.preparedness import probe_all
from comfy_studio.engines.registry import EngineRegistry


class EngineNotReadyError(RuntimeError):
    """Un motor requerido no está listo para producción (fail-closed)."""


@dataclass(slots=True)
class EngineHealth:
    name: str
    tier: str
    state: str  # "ready" | "stub" | "unwired"
    note: str = ""
    #: Taxonomía de preparación (None si no se sondeó).
    prep: dict[str, Any] | None = None

    def to_public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "tier": self.tier,
            "state": self.state,
            "note": self.note,
        }
        if self.prep is not None:
            out["prep"] = self.prep
        return out


def _state_for(name: str, wired: bool) -> str:
    if is_stub_mode(name):
        return "stub"
    return "ready" if wired else "unwired"


def probe_engines(registry: EngineRegistry, *, with_prep: bool = True) -> list[EngineHealth]:
    """Estado de cada motor del roster (sin arrancar binarios).

    Con `with_prep=True` (por defecto) adjunta la taxonomía de preparación: la
    sonda solo hace `stat`/`which`/`find_spec`, nunca descarga ni carga modelos.
    """
    prep_report = probe_all() if with_prep else None
    report: list[EngineHealth] = []
    for name in registry.names():
        entry = registry.get(name)
        if entry is None:  # pragma: no cover - names() garantiza presencia
            continue
        prep = prep_report.engines.get(name) if prep_report else None
        report.append(
            EngineHealth(
                name=entry.name,
                tier=str(entry.tier),
                state=_state_for(entry.name, entry.wired),
                note=entry.note,
                prep=prep.to_public_dict() if prep is not None else None,
            )
        )
    return report


def health_report(registry: EngineRegistry) -> dict[str, Any]:
    """Reporte serializable para el panel / strict-check."""
    engines = probe_engines(registry)
    counts = {"ready": 0, "stub": 0, "unwired": 0}
    for h in engines:
        counts[h.state] = counts.get(h.state, 0) + 1
    prep_report = probe_all()
    return {
        "counts": counts,
        "preparedness": prep_report.to_public_dict(),
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


def assert_production_prepared(
    registry: EngineRegistry,
    required: list[str],
    *,
    preps: Mapping[str, Any] | None = None,
) -> None:
    """Falla si un motor requerido no está *preparado* para ejecutar aquí.

    A diferencia de `assert_production_ready` (que solo mira cableado), esta
    variante usa la taxonomía de preparación: un motor sin binario, sin
    integración o sin los pesos de su tarea no puede ejecutar, aunque el roster
    lo dé por cableado. No carga modelos ni descarga nada para decidir.

    `preps` permite inyectar una taxonomía ya sondeada (tests); por defecto se
    sondea el roster real.
    """
    table = probe_all().engines if preps is None else preps
    problems: list[str] = []
    for name in required:
        prep = table.get(name)
        if prep is None:
            problems.append(f"{name}: no tiene especificación de preparación")
        elif prep.state != "ready":
            detail = ", ".join(prep.missing) or prep.state
            problems.append(f"{name}: {prep.state} ({detail})")
    if problems:
        raise EngineNotReadyError(
            "motores requeridos no preparados para ejecutar: " + "; ".join(problems)
        )
