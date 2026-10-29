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

from comfy_studio.engines import (
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
