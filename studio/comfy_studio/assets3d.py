"""Contrato de activos 3D con procedencia explícita (plan Ultra, W10 / A12).

W10 pide activos 3D (Trellis/PartCrafter/MapAnything) con «part IDs/PBR/cámaras y
zonas inferidas persistentes» y el invariante: «fijar transforms/color/alpha; no
simular geometría con una imagen plana sin declararlo». A12 añade: units y
transforms explícitos, asset ID persistente, convenciones de ejes/poses, y que
una **cara oculta inferida NO se presente como observación**.

Este módulo es ese contrato de datos — el que los engines de reconstrucción
escriben y los de render/composición leen. Es puro y serializable; no reconstruye
geometría (eso es GPU, A29). Su valor: hacer imposible que una región sintetizada
pase por evidencia factual, y que un activo quede sin transform/units declarados.

Convenciones fijadas (deben ser explícitas, no asumidas):
- `up_axis` y `forward_axis` (p. ej. Y-up, -Z-forward estilo OpenGL/USD).
- `units` del espacio del activo (metros por defecto).
- `transform` 4x4 fila-mayor respecto al mundo del proyecto.
Cada parte y cada superficie lleva una `provenance`:
- `observed`: derivada de evidencia real (fotos/geometría medida).
- `inferred`: sintetizada/propuesta — NUNCA es evidencia factual nueva.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Provenance(StrEnum):
    """De dónde viene una parte/superficie del activo."""

    OBSERVED = "observed"
    INFERRED = "inferred"


class UpAxis(StrEnum):
    Y = "y_up"
    Z = "z_up"


#: Transform identidad 4x4 (fila-mayor).
IDENTITY_4X4: tuple[tuple[float, ...], ...] = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


class Asset3DError(ValueError):
    """Contrato de activo 3D inválido."""


@dataclass(slots=True)
class Material:
    """Material PBR con su color base y alpha declarados explícitamente."""

    name: str
    #: color base RGBA en [0,1]; alpha explícito (no se asume opaco).
    base_color_rgba: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)
    roughness: float = 0.5
    metallic: float = 0.0
    provenance: Provenance = Provenance.OBSERVED
    texture_license: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_color_rgba": list(self.base_color_rgba),
            "roughness": self.roughness,
            "metallic": self.metallic,
            "provenance": str(self.provenance),
            "texture_license": self.texture_license,
        }


@dataclass(slots=True)
class Part:
    """Una parte reabrible del activo (PartCrafter) con su procedencia."""

    part_id: str
    name: str
    material: str  # nombre de un Material del activo
    provenance: Provenance
    #: bbox local [minx,miny,minz, maxx,maxy,maxz] para trazabilidad.
    bbox: tuple[float, float, float, float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "name": self.name,
            "material": self.material,
            "provenance": str(self.provenance),
            "bbox": list(self.bbox) if self.bbox else None,
        }


@dataclass(slots=True)
class CameraPose:
    """Una cámara reabrible asociada al activo."""

    name: str
    #: posición y target en el espacio del activo.
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    fov_deg: float = 50.0
    provenance: Provenance = Provenance.OBSERVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "position": list(self.position),
            "target": list(self.target),
            "fov_deg": self.fov_deg,
            "provenance": str(self.provenance),
        }


@dataclass(slots=True)
class Asset3D:
    """Activo 3D reabrible con transforms/units/procedencia explícitos."""

    asset_id: str
    name: str
    up_axis: UpAxis = UpAxis.Y
    #: eje "hacia adelante" como vector unitario; -Z por defecto (OpenGL/USD).
    forward_axis: tuple[float, float, float] = (0.0, 0.0, -1.0)
    units: str = "meters"
    transform: tuple[tuple[float, ...], ...] = IDENTITY_4X4
    materials: list[Material] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    cameras: list[CameraPose] = field(default_factory=list)
    #: engine que lo produjo (trellis/partcrafter/mapanything), trazabilidad.
    source_engine: str | None = None

    def validate(self) -> None:
        """Comprueba el contrato; lanza `Asset3DError` si algo falta.

        No valida la geometría (es GPU); valida que el activo esté COMPLETO y
        HONESTO: transform/units presentes, cada parte apunta a un material real,
        y la procedencia es explícita.
        """
        if not self.asset_id:
            raise Asset3DError("asset_id vacío: el id debe persistir")
        if self.units not in ("meters", "centimeters", "millimeters"):
            raise Asset3DError(f"units no reconocidas: {self.units!r}")
        if len(self.transform) != 4 or any(len(r) != 4 for r in self.transform):
            raise Asset3DError("transform debe ser 4x4")
        mat_names = {m.name for m in self.materials}
        for part in self.parts:
            if part.material not in mat_names:
                raise Asset3DError(
                    f"parte {part.part_id} referencia material inexistente {part.material!r}"
                )
        # Cada material inferido debe decir que lo es (no colar un alpha/color
        # sintetizado como observado).
        for mat in self.materials:
            if not isinstance(mat.provenance, Provenance):
                raise Asset3DError(f"material {mat.name} sin procedencia explícita")

    def observed_parts(self) -> list[Part]:
        return [p for p in self.parts if p.provenance is Provenance.OBSERVED]

    def inferred_parts(self) -> list[Part]:
        return [p for p in self.parts if p.provenance is Provenance.INFERRED]

    def has_inferred_geometry(self) -> bool:
        """True si alguna parte/cámara es inferida (no evidencia factual)."""
        return any(p.provenance is Provenance.INFERRED for p in self.parts) or any(
            c.provenance is Provenance.INFERRED for c in self.cameras
        )

    def coverage_report(self) -> dict[str, Any]:
        """Qué parte del activo es observada vs. inferida — para no engañar.

        Un consumidor (render, atlas strict_exact) usa esto para rechazar
        presentar una cara oculta inferida como observación factual.
        """
        total = len(self.parts)
        observed = len(self.observed_parts())
        return {
            "asset_id": self.asset_id,
            "parts_total": total,
            "parts_observed": observed,
            "parts_inferred": total - observed,
            "has_inferred_geometry": self.has_inferred_geometry(),
            "fully_observed": total > 0 and observed == total,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "up_axis": str(self.up_axis),
            "forward_axis": list(self.forward_axis),
            "units": self.units,
            "transform": [list(r) for r in self.transform],
            "source_engine": self.source_engine,
            "materials": [m.to_dict() for m in self.materials],
            "parts": [p.to_dict() for p in self.parts],
            "cameras": [c.to_dict() for c in self.cameras],
            "coverage": self.coverage_report(),
        }


def assert_not_presented_as_observed(asset: Asset3D, part_id: str) -> None:
    """Falla si se intenta tratar una parte INFERIDA como observación factual.

    Es la guarda que A12 exige: «no presentar visión de cara oculta como
    observación». Un render/export en modo factual llama a esto antes de usar
    una parte como evidencia del mundo real.
    """
    part = next((p for p in asset.parts if p.part_id == part_id), None)
    if part is None:
        raise Asset3DError(f"parte inexistente: {part_id}")
    if part.provenance is Provenance.INFERRED:
        raise Asset3DError(
            f"parte {part_id} es INFERIDA: no puede presentarse como observación factual"
        )


__all__ = [
    "Asset3D",
    "Asset3DError",
    "CameraPose",
    "Material",
    "Part",
    "Provenance",
    "UpAxis",
    "assert_not_presented_as_observed",
]
