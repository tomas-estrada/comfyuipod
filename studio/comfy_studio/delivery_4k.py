"""Composición y entregables 4K (plan Ultra, W20 / A22).

W20 es el cierre de la ruta esencial: ensamblar clips (W08), capas (W15) y audio
(W18/W19) en un timeline y producir un **entregable 4K editable** con manifest
de reproducción y créditos. El invariante (A22): no CDN ni publish/cloud ocultos;
créditos y licencias viajan con el proyecto; un proyecto editable reabre y cambia
texto/transición SIN regenerar el contenido aprobado; cambios de aspecto no
estiran personas; una resolución 3840×2176 de un motor no se disfraza de 4K real
(crop explícito).

Este módulo implementa el CONTRATO de entrega (complementa el paquete `montage/`
existente, que cubre el montaje asistido desde brief del M7):

- `Clip` / `Title` / `TimelineSpec`: el proyecto editable.
- `DeliveryManifest`: receta de entrega 4K + créditos/licencias que viajan con
  el proyecto.
- `edit_text`: cambia un título SIN tocar los clips aprobados.
- `finishing_plan`: lleva un clip a 4K exacto con crop EXPLÍCITO si la fuente no
  es 2160 (no se disfraza).

El encode real lo hace FFmpeg en el engine; aquí se verifica el contrato A22.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: dimensiones 4K exactas por aspecto de entrega.
ASPECT_4K = {
    "16:9": (3840, 2160),
    "9:16": (2160, 3840),
    "1:1": (2160, 2160),
}


class DeliveryError(ValueError):
    """Contrato de entrega inválido."""


@dataclass(slots=True)
class Clip:
    """Un clip del timeline con su fuente, tiempos y licencia."""

    clip_id: str
    source_path: str
    in_s: float
    out_s: float
    license: str = "unknown"   # viaja con el proyecto (A22)
    src_width: int = 1920
    src_height: int = 1080

    @property
    def duration_s(self) -> float:
        return max(0.0, self.out_s - self.in_s)


@dataclass(slots=True)
class Title:
    """Un título/caption editable sin regenerar contenido."""

    title_id: str
    text: str
    start_s: float
    end_s: float


@dataclass(slots=True)
class TimelineSpec:
    """Proyecto editable: clips + títulos + transiciones + audio."""

    name: str
    aspect: str = "16:9"
    fps: int = 24
    clips: list[Clip] = field(default_factory=list)
    titles: list[Title] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    audio_stems: list[str] = field(default_factory=list)

    def total_duration_s(self) -> float:
        return sum(c.duration_s for c in self.clips)

    def validate(self) -> None:
        if self.aspect not in ASPECT_4K:
            raise DeliveryError(f"aspecto no soportado: {self.aspect}")
        if self.fps <= 0:
            raise DeliveryError("fps debe ser > 0")
        if not self.clips:
            raise DeliveryError("timeline sin clips")


def edit_text(spec: TimelineSpec, title_id: str, new_text: str) -> set[str]:
    """Cambia el texto de un título SIN regenerar los clips (A22)."""
    title = next((t for t in spec.titles if t.title_id == title_id), None)
    if title is None:
        raise DeliveryError(f"título inexistente: {title_id}")
    title.text = new_text
    return {title_id}  # solo el título; los clips NO se tocan


def target_dimensions(aspect: str) -> tuple[int, int]:
    """Dimensiones 4K exactas para el aspecto de entrega (A22)."""
    if aspect not in ASPECT_4K:
        raise DeliveryError(f"aspecto no soportado: {aspect}")
    return ASPECT_4K[aspect]


def finishing_plan(clip: Clip, aspect: str) -> dict[str, Any]:
    """Cómo llevar un clip a 4K exacto: none / crop explícito / upscale (A22)."""
    tw, th = target_dimensions(aspect)
    plan: dict[str, Any] = {"target": [tw, th], "source": [clip.src_width, clip.src_height]}
    if clip.src_width == tw and clip.src_height == th:
        plan["op"] = "none"
    elif clip.src_width >= tw and clip.src_height >= th:
        plan["op"] = "crop"
        plan["crop_px"] = [clip.src_width - tw, clip.src_height - th]
        plan["note"] = "crop explícito; no se disfraza otra resolución de 4K (A22)"
    else:
        plan["op"] = "upscale"
        plan["note"] = "upscale a 4K; mas pixeles no recuperan detalle perdido"
    return plan


@dataclass(slots=True)
class DeliveryManifest:
    """Manifest de reproducción y créditos que viaja con el proyecto (A22)."""

    project: str
    width: int
    height: int
    fps: int
    duration_s: float
    credits: list[dict[str, str]] = field(default_factory=list)
    captions: bool = False
    color_space: str = "bt709"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "resolution": f"{self.width}x{self.height}",
            "fps": self.fps,
            "duration_s": round(self.duration_s, 3),
            "color_space": self.color_space,
            "captions": self.captions,
            "credits": list(self.credits),
            "delivery_note": (
                "assets y creditos viajan con el proyecto; sin CDN ni cloud oculto (A22)"
            ),
        }


def build_delivery_manifest(spec: TimelineSpec, *, captions: bool = True) -> DeliveryManifest:
    """Construye el manifest de entrega 4K con créditos de todas las fuentes."""
    spec.validate()
    w, h = target_dimensions(spec.aspect)
    credits = [
        {"clip": c.clip_id, "source": c.source_path, "license": c.license}
        for c in spec.clips
    ]
    return DeliveryManifest(
        project=spec.name,
        width=w,
        height=h,
        fps=spec.fps,
        duration_s=spec.total_duration_s(),
        credits=credits,
        captions=captions,
    )


__all__ = [
    "ASPECT_4K",
    "Clip",
    "DeliveryError",
    "DeliveryManifest",
    "TimelineSpec",
    "Title",
    "build_delivery_manifest",
    "edit_text",
    "finishing_plan",
    "target_dimensions",
]
