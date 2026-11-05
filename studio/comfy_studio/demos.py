"""Banco de evaluación y demostraciones D01–D05 (plan Ultra, W24 / A25).

W24 pide un harness D01–D05 con manifest de inputs/esperados/splits, fixtures
CPU reales, métricas y jobs GPU marcados pendientes, con presupuesto por
escenario. El invariante (A25): puede construirse temprano, pero NO marcar
renders futuros como resultados existentes.

Este módulo registra las cinco demostraciones como escenarios REPRODUCIBLES con
su manifest, y separa honestamente lo que está listo en CPU de lo que espera GPU:

- `DemoStage`: un paso de una demo, con `level` de evidencia (L0–L4) y estado.
  Un paso de render neural es `pending_gpu` (L3), NUNCA `done` sin medición.
- `Demo`: la definición D0x con inputs, esperados, splits y budget declarados.
- `demo_catalog`: las cinco demos del plan.
- `assert_no_fake_gpu_result`: falla si un paso neural se marca como completado
  sin evidencia GPU — impide disfrazar un render futuro de resultado existente.

Determinista; no ejecuta GPU. Deja el harness seleccionable y los pasos GPU
explícitamente pendientes para la campaña W26.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvidenceLevel(StrEnum):
    L0 = "L0"  # propuesta/fuente
    L1 = "L1"  # código/contrato + unit tests
    L2 = "L2"  # engine preparado + adapter + prueba CPU
    L3 = "L3"  # ejecución GPU real
    L4 = "L4"  # reproducido con holdouts


class StageStatus(StrEnum):
    READY_CPU = "ready_cpu"       # fixture/contrato CPU listo
    PENDING_GPU = "pending_gpu"   # requiere la campaña GPU (no done)
    DONE = "done"                 # solo tras evidencia real del nivel declarado


class DemoError(ValueError):
    """Contrato de demo inválido."""


@dataclass(slots=True)
class DemoStage:
    """Un paso de una demo con su nivel de evidencia y estado honesto."""

    name: str
    level: EvidenceLevel
    status: StageStatus
    #: ¿este paso ejecuta un render neural (requiere GPU)?
    neural: bool = False

    def validate(self) -> None:
        # Un paso neural no puede estar DONE sin ser al menos L3 (evidencia GPU).
        if self.neural and self.status is StageStatus.DONE and self.level not in (
            EvidenceLevel.L3,
            EvidenceLevel.L4,
        ):
            raise DemoError(
                f"paso neural {self.name} marcado done sin evidencia GPU (L3/L4)"
            )


@dataclass(slots=True)
class Demo:
    """Una demostración D0x reproducible con su manifest."""

    demo_id: str
    title: str
    inputs: list[str]
    expected: list[str]
    splits: list[str]
    budget_runs: int
    stages: list[DemoStage] = field(default_factory=list)

    def validate(self) -> None:
        if not self.inputs or not self.expected:
            raise DemoError(f"{self.demo_id}: manifest sin inputs/esperados")
        for s in self.stages:
            s.validate()

    def cpu_ready(self) -> list[DemoStage]:
        return [s for s in self.stages if s.status is StageStatus.READY_CPU]

    def gpu_pending(self) -> list[DemoStage]:
        return [s for s in self.stages if s.status is StageStatus.PENDING_GPU]

    def to_dict(self) -> dict[str, Any]:
        return {
            "demo_id": self.demo_id,
            "title": self.title,
            "inputs": list(self.inputs),
            "expected": list(self.expected),
            "splits": list(self.splits),
            "budget_runs": self.budget_runs,
            "stages": [
                {"name": s.name, "level": str(s.level), "status": str(s.status), "neural": s.neural}
                for s in self.stages
            ],
            "cpu_ready": len(self.cpu_ready()),
            "gpu_pending": len(self.gpu_pending()),
        }


def _stage(name: str, neural: bool) -> DemoStage:
    """Paso por defecto: contrato CPU listo; si es neural, pendiente de GPU."""
    if neural:
        return DemoStage(
            name=name, level=EvidenceLevel.L2, status=StageStatus.PENDING_GPU, neural=True
        )
    return DemoStage(
        name=name, level=EvidenceLevel.L1, status=StageStatus.READY_CPU, neural=False
    )


def demo_catalog() -> list[Demo]:
    """Las cinco demostraciones del plan, con fixtures CPU y pasos GPU pendientes."""
    return [
        Demo(
            demo_id="D01",
            title="Coche en lugar real del Aburrá, varias cámaras/luces/sonido",
            inputs=["atlas:puente_4sur", "asset:coche_cad", "brief:car_trend"],
            expected=["reel_4k", "reproyeccion<thr", "logos_preservados"],
            splits=["heldout_views"],
            budget_runs=3,
            stages=[
                _stage("reconstruccion_guias", neural=False),
                _stage("generacion", neural=True),
                _stage("relight", neural=True),
                _stage("foley", neural=False),
                _stage("finishing_4k", neural=True),
            ],
        ),
        Demo(
            demo_id="D02",
            title="Eliminar una causa y sus consecuencias (omnimatte)",
            inputs=["render:actor_on", "render:actor_off"],
            expected=["partes_intactas", "error_residual_revelado"],
            splits=["actor_on_off"],
            budget_runs=2,
            stages=[_stage("matting", neural=True), _stage("causal_delete", neural=False)],
        ),
        Demo(
            demo_id="D03",
            title="Producto articulado con cambio de material y varias cámaras",
            inputs=["cad:partes", "joints:conocidos"],
            expected=["consistencia_multivista", "material_transfer"],
            splits=["heldout_multivista"],
            budget_runs=3,
            stages=[
                _stage("reconstruccion_partes", neural=True),
                _stage("mitsuba_ajuste", neural=False),
                _stage("transferencia_neural", neural=True),
            ],
        ),
        Demo(
            demo_id="D04",
            title="Actor hablante con identidad persistente y voz español",
            inputs=["identity:ana", "audio:voz_paisa"],
            expected=["identidad_persistente", "lipsync_sincronia"],
            splits=["shots_multiples"],
            budget_runs=3,
            stages=[
                _stage("voz_tts", neural=True),
                _stage("lipsync", neural=True),
                _stage("identidad_check", neural=False),
            ],
        ),
        Demo(
            demo_id="D05",
            title="Montaje desde brief a reel 4K editable con créditos",
            inputs=["brief:dron_costero", "clips:generados"],
            expected=["reel_4k_editable", "creditos_manifest"],
            splits=["aspectos_16_9_9_16"],
            budget_runs=2,
            stages=[
                _stage("montaje", neural=False),
                _stage("generacion_clips", neural=True),
                _stage("entrega_4k", neural=False),
            ],
        ),
    ]


def assert_no_fake_gpu_result(demo: Demo) -> None:
    """Falla si un paso neural se disfraza de resultado existente (A25)."""
    for s in demo.stages:
        if s.neural and s.status is StageStatus.DONE and s.level not in (
            EvidenceLevel.L3,
            EvidenceLevel.L4,
        ):
            raise DemoError(
                f"{demo.demo_id}/{s.name}: render futuro marcado como resultado existente"
            )


__all__ = [
    "Demo",
    "DemoError",
    "DemoStage",
    "EvidenceLevel",
    "StageStatus",
    "assert_no_fake_gpu_result",
    "demo_catalog",
]
