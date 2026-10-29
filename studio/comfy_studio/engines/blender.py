"""Adaptador del motor Blender al contrato de motores (M4-SCR-002).

Blender ya existe como runner headless aislado (`comfy_studio/blender.py`): este
adaptador NO reimplementa nada, solo lo expone con los cuatro verbos del
contrato para que el agente lo vea como herramienta uniforme.

  - `list`    → modos y formatos que soporta (render/export) + add-ons activos
                (los add-ons son el submotor dinámico de Blender).
  - `install` → habilitar un add-on; guarda STUDIO_DYNAMIC igual que ComfyUI.
                Cableado real del enable/pip del add-on en F6.
  - `verify`  → confirmar que un add-on/capacidad está disponible.
  - `run`     → `run_blender_spec` con un BlenderRenderSpec o BlenderExportSpec.

Blender es tier WARM: carga al primer uso, no reside en GPU como ComfyUI.
Mantiene su modo stub existente (`STUDIO_STUB_BLENDER=1`).
"""

from __future__ import annotations

from pathlib import Path

from comfy_studio.blender import (
    BlenderExportSpec,
    BlenderRenderSpec,
    classify_blender_failure,
    run_blender_spec,
)
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_dynamic_enabled,
)
from comfy_studio.engines.base import Engine

_MODES = ("render", "export")


class BlenderEngine(Engine):
    """Motor Blender (warm). Envuelve el runner existente."""

    name = "blender"
    tier = EngineTier.WARM

    def __init__(self, log_dir: Path, blender_bin: str | None = None) -> None:
        self._log_dir = Path(log_dir)
        self._blender_bin = blender_bin

    # -- contrato: 4 verbos --------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "modes": list(_MODES),
                "export_formats": ["usd", "gltf", "fbx"],
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        """Habilitar un add-on de Blender (submotor). Guarda STUDIO_DYNAMIC."""
        if not is_dynamic_enabled():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="install",
                error=(
                    f"habilitar add-on '{submotor}' requiere STUDIO_DYNAMIC=1; "
                    "en pod determinista los add-ons vienen horneados"
                ),
            )
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                f"cableado de enable/pip de add-ons pendiente (F6) para '{submotor}'"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        """Capacidad base siempre presente; add-ons concretos se verifican en F6."""
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="verify",
            payload={"capability": submotor, "stub": self.is_stub()},
        )

    async def run(self, spec: EngineRunSpec) -> EngineResult:
        """Ejecuta un render o export. `spec.job['mode']` elige el spec.

        Async porque `run_blender_spec` es una corrutina (igual que el motor
        ComfyUI). El contrato es la forma de `EngineResult`, no el color del
        método.
        """
        try:
            blender_spec = self._build_spec(spec.job)
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        result = await run_blender_spec(
            blender_spec, log_dir=self._log_dir, blender_bin=self._blender_bin
        )
        kind = classify_blender_failure(result.exit_code, result.log_tail)
        return EngineResult(
            ok=result.ok,
            engine=self.name,
            verb="run",
            payload={
                "outputs": [str(p) for p in result.outputs],
                "duration_s": round(result.duration_s, 3),
                "failure_kind": kind,
            },
            exit_code=result.exit_code,
            stderr_tail=result.log_tail or None,
            simulated=result.stubbed,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _build_spec(job: dict) -> BlenderRenderSpec | BlenderExportSpec:
        mode = str(job.get("mode", "render")).lower()
        if mode not in _MODES:
            raise ValueError(f"mode debe ser uno de {_MODES}, no {mode!r}")
        if mode == "export":
            return BlenderExportSpec(
                scene_path=Path(job["scene_path"]),
                output_dir=Path(job["output_dir"]),
                formats=tuple(job.get("formats", ("usd", "gltf", "fbx"))),
            )
        return BlenderRenderSpec(
            scene_path=Path(job["scene_path"]),
            output_dir=Path(job["output_dir"]),
            engine=str(job.get("engine", "cycles")),
            samples=int(job.get("samples", 64)),
            width=int(job.get("width", 1280)),
            height=int(job.get("height", 720)),
            frames=int(job.get("frames", 1)),
        )
