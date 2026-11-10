"""Clase base abstracta de un motor del estudio (M4-SCR-001).

Cada motor caliente (ComfyUI, Blender, editor, vídeo pesado, audio) implementa
estos cuatro verbos. El agente los ve como herramientas uniformes; el panel y
`/studio/api/chat` no cambian.

Contrato de implementación:
  - `name` es el identificador corto del motor (minúsculas), usado también para
    `STUDIO_STUB_<NAME>` y para la sonda strict-check.
  - `tier` declara residencia en GPU (M4-OPS-001).
  - Ningún método lanza por fallo del motor: devuelven `EngineResult(ok=False, ...)`.
  - `install` SOLO procede si `is_dynamic_enabled()` o el submotor ya está
    horneado; en pod determinista `install` devuelve ok=False con motivo.
"""

from __future__ import annotations

import abc
import inspect
from typing import Any

from comfy_studio.engines import (
    ENGINE_VERBS,
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_dynamic_enabled,
    is_stub_mode,
)


class Engine(abc.ABC):
    """Interfaz uniforme de un motor del estudio."""

    #: identificador corto, p.ej. "comfyui", "blender", "editor".
    name: str = "engine"
    #: nivel de residencia en GPU.
    tier: EngineTier = EngineTier.WARM

    # -- helpers comunes -----------------------------------------------------

    def is_stub(self) -> bool:
        """True si este motor corre en modo stub (tests sin binario/GPU)."""
        return is_stub_mode(self.name)

    def _dynamic_blocked(self, verb: str) -> EngineResult:
        """Resultado tipado cuando un verbo dinámico está deshabilitado."""
        return EngineResult(
            ok=False,
            engine=self.name,
            verb=verb,
            error=(
                f"'{verb}' requiere STUDIO_DYNAMIC=1 (pod determinista); "
                "el submotor no está horneado en la imagen."
            ),
        )

    # -- los cuatro verbos ---------------------------------------------------

    @abc.abstractmethod
    def list(self) -> EngineResult:
        """Qué submotores/capacidades ofrece el motor ahora mismo.

        ComfyUI: GET /object_info. Blender: add-ons activos.
        """

    @abc.abstractmethod
    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        """Traer un submotor que falta (nodo ComfyUI, add-on Blender).

        Rápido porque el motor host ya está caliente. Fijado bajo
        `PIP_CONSTRAINT`/`nodes.lock`. En pod determinista devuelve ok=False
        (ver `_dynamic_blocked`).
        """

    @abc.abstractmethod
    def verify(self, submotor: str) -> EngineResult:
        """Confirmar que un submotor cargó y es usable antes de `run`."""

    @abc.abstractmethod
    def run(self, spec: EngineRunSpec) -> EngineResult:
        """Ejecutar el trabajo con timeout y tope de coste.

        Toda falla se tipifica en `EngineResult`; stderr llega redactado.
        """

    # -- guardas por defecto para verbos dinámicos ---------------------------

    def _guard_install(self, submotor: str, *, baked: bool) -> EngineResult | None:
        """Devuelve un resultado bloqueante si install no procede, o None si sí.

        `baked=True` si el submotor ya viene horneado (install siempre permitido).
        """
        if baked or is_dynamic_enabled():
            return None
        return self._dynamic_blocked("install")

    # -- helpers de verificación y degradación honesta ------------------------

    def verify_result(
        self,
        *,
        installed: bool = True,
        weights: str = "not_required",
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        detail: str | None = None,
    ) -> EngineResult:
        """Resultado tipado de `verify`.

        `weights` es uno de: `not_required|present|missing`. Distingue
        instalado-pero-sin-pesos (`weights_missing`) de listo, sin red ni GPU.
        """
        from comfy_studio.engines import EngineReason

        data = dict(payload or {})
        if weights == "missing":
            ready = False
            reason = EngineReason.WEIGHTS_MISSING
            msg = error or (
                f"{self.name}: instalado pero pesos ausentes"
                + (f" ({detail})" if detail else "")
            )
        elif not installed:
            ready = False
            reason = EngineReason.NOT_INSTALLED
            msg = error or (
                f"{self.name}: no instalado" + (f" ({detail})" if detail else "")
            )
        else:
            ready = True
            reason = None
            msg = None
        data.setdefault("weights", weights)
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload=data,
            error=msg,
            reason=str(reason) if reason is not None else None,
        )


# ---------------------------------------------------------------------------
# Contrato de 4 verbos: verificación estructural del roster
# ---------------------------------------------------------------------------

#: Firma esperada de cada verbo (nombre y orden de parámetros, incluido `self`).
_EXPECTED_PARAMS: dict[str, tuple[tuple[str, Any], ...]] = {
    "list": (("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),),
    "install": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("submotor", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("ref", inspect.Parameter.KEYWORD_ONLY),
    ),
    "verify": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("submotor", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "run": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("spec", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
}


def _returns_engine_result(fn: Any) -> bool:
    """True si la anotación de retorno resuelve a `EngineResult`."""
    annotation = inspect.signature(fn).return_annotation
    if annotation is inspect.Signature.empty:
        return False
    if isinstance(annotation, str):
        return annotation.strip() in ("EngineResult", "'EngineResult'", '"EngineResult"')
    return annotation is EngineResult


def engine_contract_violations(cls: type, *, name: str, tier: EngineTier) -> list[str]:
    """Violaciones del contrato de 4 verbos de un adaptador de motor.

    Chequeo estructural barato (sin instanciar): los cuatro verbos existen, con
    la firma y el tipo de retorno esperados, y `name`/`tier` coinciden con el
    roster. Es el barrido que falla si un motor rompe el contrato.
    """
    problems: list[str] = []
    if not isinstance(cls, type):
        return [f"{name}: no es una clase ({cls!r})"]
    if getattr(cls, "name", None) != name:
        problems.append(f"{name}: name={getattr(cls, 'name', None)!r}")
    if getattr(cls, "tier", None) != tier:
        problems.append(f"{name}: tier={getattr(cls, 'tier', None)!r} (roster={tier!r})")

    for verb in sorted(ENGINE_VERBS):
        fn = getattr(cls, verb, None)
        if fn is None or not callable(fn):
            problems.append(f"{name}: falta el verbo {verb!r}")
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            problems.append(f"{name}.{verb}: firma no inspeccionable")
            continue
        actual = tuple((p.name, p.kind) for p in sig.parameters.values())
        expected = _EXPECTED_PARAMS[verb]
        if actual != expected:
            problems.append(f"{name}.{verb}: firma {actual} != esperada {expected}")
        if verb == "install":
            ref = sig.parameters.get("ref")
            if ref is None or ref.default is not None:
                problems.append(f"{name}.install: 'ref' debe ser keyword-only sin valor")
        if not _returns_engine_result(fn):
            problems.append(f"{name}.{verb}: retorno no es EngineResult")
    return problems
