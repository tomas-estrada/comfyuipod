"""Taxonomía tipada de fallos de capa del executor (M6).

Hoy cualquier `ok=False` del runner escala la escalera de replan
local→plantilla→global por igual. Eso trata igual un timeout transitorio (que un
reintento in-place arregla) que un parámetro inválido (que ningún reintento
idéntico arregla) o una denegación de permiso (que ni el replan arregla).

Contrato:
- El runner PUEDE declarar `failure_kind` en el resultado (o el adaptador puede
  traducir su `reason`/`lease`). Sin declaración el resultado es **legacy**:
  se comporta exactamente como antes (escalera de replan), sin reintento
  in-place — compatibilidad hacia atrás garantizada.
- `TRANSIENT`: reintento in-place ACOTADO (`MAX_TRANSIENT_RETRIES`) con la MISMA
  identidad de operación (la op nunca completó, el idempotency_key no cambia);
  agotado el presupuesto, escala la escalera como hoy.
- `INVALID_PARAMETERS`: cero reintentos idénticos; directo a la escalera
  (cambiar plantilla/plan es la única cura).
- `PERMISSION_DENIED`: fail-fast tipado — el shot FAILED sin consumir escalón.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class LayerFailureKind(StrEnum):
    """Clase de fallo de una capa, declarada o inferida."""

    #: fallo pasajero (timeout, GPU ocupada, rate limit): reintento in-place acotado.
    TRANSIENT = "transient"
    #: parámetros/spec inválidos: reintentar idéntico no ayuda; escala la escalera.
    INVALID_PARAMETERS = "invalid_parameters"
    #: permiso denegado por política: fail-fast, sin escalera ni reintentos.
    PERMISSION_DENIED = "permission_denied"
    #: fallo genérico de motor o resultado legacy sin clase declarada.
    ENGINE = "engine"


#: Presupuesto de reintentos in-place para fallos TRANSIENT (no incluye el
#: intento inicial: 2 reintentos → hasta 3 llamadas por capa y ejecución).
MAX_TRANSIENT_RETRIES = 2

#: Alias que los runners/adaptadores ya emiten hoy, normalizados por clase.
_FAILURE_ALIASES: dict[LayerFailureKind, frozenset[str]] = {
    LayerFailureKind.TRANSIENT: frozenset(
        {"transient", "timeout", "rate_limited", "gpu_busy", "resource_busy"}
    ),
    LayerFailureKind.INVALID_PARAMETERS: frozenset(
        {"invalid_parameters", "invalid", "spec_invalid", "bad_request", "unknown_engine"}
    ),
    LayerFailureKind.PERMISSION_DENIED: frozenset(
        {"permission_denied", "forbidden", "unauthorized"}
    ),
    LayerFailureKind.ENGINE: frozenset(
        {"engine", "engine_error", "execution_failed", "crash", "oom"}
    ),
}


def normalize_failure_kind(value: Any) -> LayerFailureKind | None:
    """Normaliza una clase declarada; None si no se declaró o no se reconoce."""
    if value is None:
        return None
    name = str(value).strip().lower()
    if not name:
        return None
    for kind, aliases in _FAILURE_ALIASES.items():
        if name in aliases:
            return kind
    return None


def classify_runner_failure(result: Mapping[str, Any]) -> LayerFailureKind:
    """Clasifica un resultado de `run_layer` fallido.

    Precedencia: `failure_kind` explícito del runner > `lease` del
    LeasedEngineRunner > `reason` tipado de `EngineResult` > ENGINE (legacy).
    Un valor explícito no reconocido cae a ENGINE (fail-safe: comportamiento
    idéntico al legacy, nunca peor).
    """
    declared = normalize_failure_kind(result.get("failure_kind"))
    if declared is not None:
        return declared
    lease = str(result.get("lease") or "").strip().lower()
    if lease == "unavailable":
        # GPU ocupada por otro owner: pasajero por definición.
        return LayerFailureKind.TRANSIENT
    reason = normalize_failure_kind(result.get("reason"))
    if reason is not None:
        return reason
    return LayerFailureKind.ENGINE


__all__ = [
    "MAX_TRANSIENT_RETRIES",
    "LayerFailureKind",
    "classify_runner_failure",
    "normalize_failure_kind",
]
