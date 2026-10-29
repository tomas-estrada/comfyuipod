"""Puerta de decisión de instalación de motor (M4-SCR-003, F6).

La escotilla de escape del plan §6: el agente PUEDE instalar un motor nuevo a
mitad de ejecución cuando el set caliente no logra la tarea — porque la meta es
terminar. Pero instalar un MOTOR deja la GPU ociosa minutos, así que es una
decisión DELIBERADA y razonada, no un accidente.

Esta puerta no instala nada: DECIDE si procede, de forma pura y testeable, y
deja un registro para la promoción al set caliente (un motor instalado que
repite valor se hornea en el siguiente build).

Las cuatro preguntas del plan §6, en orden:
  1. ¿El set caliente realmente falló esta tarea?  (no instalar lo que ya tienes)
  2. ¿El coste de GPU ociosa vale frente al valor de la tarea?
  3. ¿Entra en el sandbox? (loopback, redacción, tope de coste)
  4. (si se permite y repite valor) → candidato a promoción.

Requiere STUDIO_DYNAMIC=1: en pod determinista, instalar un motor se rechaza.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from comfy_studio.engines import is_dynamic_enabled
from comfy_studio.engines.registry import EngineRegistry


@dataclass(slots=True)
class InstallRequest:
    """Petición del agente para instalar un motor nuevo."""

    engine_name: str
    warm_attempted: bool  # ¿ya intentó el set caliente y falló?
    task_value: float  # valor estimado de la tarea (misma unidad que idle_cost)
    est_idle_cost: float  # coste estimado de GPU ociosa durante la instalación
    sandbox_safe: bool  # ¿loopback-only, sin telemetría, bajo tope de coste?
    source: str = ""  # de dónde se instalaría (repo/paquete)


@dataclass(slots=True)
class InstallDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    promote_candidate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "promote_candidate": self.promote_candidate,
        }


def decide_install(req: InstallRequest, registry: EngineRegistry) -> InstallDecision:
    """Aplica las cuatro preguntas del §6. Fail-closed: duda ⇒ no instalar."""
    reasons: list[str] = []

    if not is_dynamic_enabled():
        return InstallDecision(False, ["STUDIO_DYNAMIC off: pod determinista no instala motores"])

    # 0. ¿Ya existe en el roster caliente? Entonces no se instala, se usa.
    if registry.get(req.engine_name) is not None:
        return InstallDecision(
            False, [f"'{req.engine_name}' ya está en el roster caliente: úsalo, no lo instales"]
        )

    # 1. ¿El set caliente realmente falló?
    if not req.warm_attempted:
        return InstallDecision(
            False, ["el set caliente no se ha intentado todavía: pruébalo antes de instalar"]
        )
    reasons.append("set caliente intentado y insuficiente")

    # 2. ¿El valor supera el coste de GPU ociosa?
    if req.task_value <= req.est_idle_cost:
        return InstallDecision(
            False,
            reasons
            + [
                f"valor de tarea ({req.task_value}) no supera el coste de "
                f"GPU ociosa ({req.est_idle_cost}): no vale la pena"
            ],
        )
    reasons.append(f"valor ({req.task_value}) > coste ocioso ({req.est_idle_cost})")

    # 3. ¿Entra en el sandbox?
    if not req.sandbox_safe:
        return InstallDecision(
            False, reasons + ["no cumple el sandbox (loopback/redacción/tope): rechazado"]
        )
    reasons.append("cumple el sandbox")

    # Permitido. Candidato a promoción si el valor supera holgadamente el coste.
    promote = req.task_value >= (2.0 * req.est_idle_cost)
    if promote:
        reasons.append("valor >= 2x coste: candidato a hornear caliente en el próximo build")
    return InstallDecision(True, reasons, promote_candidate=promote)


@dataclass(slots=True)
class PromotionRecord:
    """Un motor instalado dinámicamente que demostró valor y debería hornearse."""

    engine_name: str
    source: str
    times_used: int = 1
    first_seen_ts: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_name": self.engine_name,
            "source": self.source,
            "times_used": self.times_used,
            "first_seen_ts": self.first_seen_ts,
        }


class PromotionLedger:
    """Acumula el uso de motores instalados para decidir qué promover."""

    def __init__(self, promote_threshold: int = 3) -> None:
        self._records: dict[str, PromotionRecord] = {}
        self._threshold = promote_threshold

    def record_use(self, engine_name: str, source: str = "") -> PromotionRecord:
        rec = self._records.get(engine_name)
        if rec is None:
            rec = PromotionRecord(engine_name=engine_name, source=source)
            self._records[engine_name] = rec
        else:
            rec.times_used += 1
        return rec

    def ready_to_promote(self) -> list[str]:
        """Motores cuyo uso alcanza el umbral: hornear en el próximo build."""
        return sorted(
            name for name, rec in self._records.items() if rec.times_used >= self._threshold
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self._threshold,
            "records": [self._records[n].to_dict() for n in sorted(self._records)],
        }
