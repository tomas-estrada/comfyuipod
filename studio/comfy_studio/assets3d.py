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

import math
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
        if not self.name:
            raise Asset3DError("name vacío: el activo debe tener nombre")
        if not isinstance(self.up_axis, UpAxis):
            raise Asset3DError(f"up_axis no reconocido: {self.up_axis!r}")
        if self.units not in ("meters", "centimeters", "millimeters"):
            raise Asset3DError(f"units no reconocidas: {self.units!r}")
        if len(self.transform) != 4 or any(len(r) != 4 for r in self.transform):
            raise Asset3DError("transform debe ser 4x4")
        if any(not math.isfinite(value) for row in self.transform for value in row):
            raise Asset3DError("transform contiene valores no finitos")
        if any(
            abs(actual - expected) > 1e-9
            for actual, expected in zip(self.transform[3], (0, 0, 0, 1), strict=True)
        ):
            raise Asset3DError("transform debe tener una última fila homogénea [0, 0, 0, 1]")
        if len(self.forward_axis) != 3 or any(
            not math.isfinite(value) for value in self.forward_axis
        ):
            raise Asset3DError("forward_axis debe ser un vector 3D finito")
        forward_length = math.sqrt(sum(value * value for value in self.forward_axis))
        if not math.isclose(forward_length, 1.0, rel_tol=0, abs_tol=1e-6):
            raise Asset3DError("forward_axis debe ser unitario")
        up_index = 1 if self.up_axis is UpAxis.Y else 2
        if abs(self.forward_axis[up_index]) > 1e-6:
            raise Asset3DError("forward_axis no puede ser paralelo al up_axis")
        mat_names = {m.name for m in self.materials}
        if len(mat_names) != len(self.materials) or any(not name for name in mat_names):
            raise Asset3DError("nombres de material vacíos o duplicados")
        part_ids = [part.part_id for part in self.parts]
        if len(part_ids) != len(set(part_ids)):
            raise Asset3DError("part_id duplicado")
        for part in self.parts:
            if not part.part_id or not part.name:
                raise Asset3DError("parte sin part_id o nombre")
            if part.material not in mat_names:
                raise Asset3DError(
                    f"parte {part.part_id} referencia material inexistente {part.material!r}"
                )
            if not isinstance(part.provenance, Provenance):
                raise Asset3DError(f"parte {part.part_id} sin procedencia explícita")
            if part.bbox is not None:
                if len(part.bbox) != 6 or any(not math.isfinite(value) for value in part.bbox):
                    raise Asset3DError(f"parte {part.part_id} tiene bbox inválido")
                if any(part.bbox[i] > part.bbox[i + 3] for i in range(3)):
                    raise Asset3DError(f"parte {part.part_id} tiene bbox invertido")
        # Cada material inferido debe decir que lo es (no colar un alpha/color
        # sintetizado como observado).
        for mat in self.materials:
            if not isinstance(mat.provenance, Provenance):
                raise Asset3DError(f"material {mat.name} sin procedencia explícita")
            if len(mat.base_color_rgba) != 4 or any(
                not math.isfinite(value) or not 0 <= value <= 1 for value in mat.base_color_rgba
            ):
                raise Asset3DError(f"material {mat.name} tiene base_color_rgba fuera de [0,1]")
            if not math.isfinite(mat.roughness) or not 0 <= mat.roughness <= 1:
                raise Asset3DError(f"material {mat.name} tiene roughness fuera de [0,1]")
            if not math.isfinite(mat.metallic) or not 0 <= mat.metallic <= 1:
                raise Asset3DError(f"material {mat.name} tiene metallic fuera de [0,1]")
        camera_names = [camera.name for camera in self.cameras]
        if len(camera_names) != len(set(camera_names)):
            raise Asset3DError("nombre de cámara duplicado")
        for camera in self.cameras:
            if not camera.name or len(camera.position) != 3 or len(camera.target) != 3:
                raise Asset3DError(f"cámara {camera.name!r} requiere nombre y vectores 3D")
            if not isinstance(camera.provenance, Provenance):
                raise Asset3DError(f"cámara {camera.name} sin procedencia explícita")
            if any(
                not math.isfinite(value)
                for value in (*camera.position, *camera.target, camera.fov_deg)
            ):
                raise Asset3DError(f"cámara {camera.name} contiene valores no finitos")
            if camera.fov_deg <= 0 or camera.fov_deg >= 180:
                raise Asset3DError(f"cámara {camera.name} tiene un FOV fuera de (0, 180)")

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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Asset3D:
        """Reabre un activo serializado y vuelve a comprobar sus invariantes."""
        try:
            asset = cls(
                asset_id=data["asset_id"],
                name=data["name"],
                up_axis=UpAxis(data["up_axis"]),
                forward_axis=tuple(data["forward_axis"]),
                units=data["units"],
                transform=tuple(tuple(row) for row in data["transform"]),
                materials=[
                    Material(
                        name=item["name"],
                        base_color_rgba=tuple(item["base_color_rgba"]),
                        roughness=item["roughness"],
                        metallic=item["metallic"],
                        provenance=Provenance(item["provenance"]),
                        texture_license=item.get("texture_license"),
                    )
                    for item in data.get("materials", [])
                ],
                parts=[
                    Part(
                        part_id=item["part_id"],
                        name=item["name"],
                        material=item["material"],
                        provenance=Provenance(item["provenance"]),
                        bbox=tuple(item["bbox"]) if item.get("bbox") is not None else None,
                    )
                    for item in data.get("parts", [])
                ],
                cameras=[
                    CameraPose(
                        name=item["name"],
                        position=tuple(item["position"]),
                        target=tuple(item["target"]),
                        fov_deg=item["fov_deg"],
                        provenance=Provenance(item["provenance"]),
                    )
                    for item in data.get("cameras", [])
                ],
                source_engine=data.get("source_engine"),
            )
            asset.validate()
            return asset
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, Asset3DError):
                raise
            raise Asset3DError(f"serialized asset is invalid: {exc}") from exc


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


def assert_camera_not_presented_as_observed(asset: Asset3D, camera_name: str) -> None:
    """Rechaza usar una cámara inferida como si fuera una vista observada."""
    camera = next((camera for camera in asset.cameras if camera.name == camera_name), None)
    if camera is None:
        raise Asset3DError(f"cámara inexistente: {camera_name}")
    if camera.provenance is Provenance.INFERRED:
        raise Asset3DError(
            f"cámara {camera_name} es INFERIDA: no puede presentarse como observación factual"
        )


__all__ = [
    "Asset3D",
    "Asset3DError",
    "assert_camera_not_presented_as_observed",
    "CameraPose",
    "Material",
    "Part",
    "Provenance",
    "UpAxis",
    "assert_not_presented_as_observed",
]
