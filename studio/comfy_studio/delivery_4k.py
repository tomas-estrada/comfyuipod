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

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, ZipFile

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
    source_asset_id: str | None = None
    reason: str = "cut"

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
class AudioStem:
    """Una pista fuente conservada con el proyecto editable."""

    stem_id: str
    source_path: str
    role: str = "unknown"
    license: str = "unknown"
    gain_db: float = 0.0
    channels: int = 2


@dataclass(slots=True)
class TimelineSpec:
    """Proyecto editable: clips + títulos + transiciones + audio."""

    name: str
    aspect: str = "16:9"
    fps: int = 24
    clips: list[Clip] = field(default_factory=list)
    titles: list[Title] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    audio_stems: list[AudioStem] = field(default_factory=list)
    color_space: str = "bt709"
    color_range: str = "limited"
    safe_area_percent: float = 5.0
    audio_loudness_lufs: float = -16.0
    true_peak_dbfs: float = -1.0

    SCHEMA_VERSION = 1

    def total_duration_s(self) -> float:
        return sum(c.duration_s for c in self.clips)

    def validate(self) -> None:
        if not self.name.strip():
            raise DeliveryError("el proyecto requiere nombre")
        if self.aspect not in ASPECT_4K:
            raise DeliveryError(f"aspecto no soportado: {self.aspect}")
        if self.fps <= 0:
            raise DeliveryError("fps debe ser > 0")
        if not self.clips:
            raise DeliveryError("timeline sin clips")
        if self.color_space not in {"bt709", "bt2020", "display-p3"}:
            raise DeliveryError(f"color_space no soportado: {self.color_space}")
        if self.color_range not in {"limited", "full"}:
            raise DeliveryError(f"color_range no soportado: {self.color_range}")
        if not math.isfinite(self.safe_area_percent) or not 0 <= self.safe_area_percent < 50:
            raise DeliveryError("safe_area_percent debe estar entre 0 y 50")
        if not math.isfinite(self.audio_loudness_lufs) or not -70 <= self.audio_loudness_lufs <= 0:
            raise DeliveryError("audio_loudness_lufs debe estar entre -70 y 0")
        if not math.isfinite(self.true_peak_dbfs) or not -20 <= self.true_peak_dbfs <= 0:
            raise DeliveryError("true_peak_dbfs debe estar entre -20 y 0")
        ids = [c.clip_id for c in self.clips]
        if len(ids) != len(set(ids)):
            raise DeliveryError("clip_id duplicado")
        if any(c.in_s < 0 or c.out_s <= c.in_s for c in self.clips):
            raise DeliveryError("rango de clip inválido")
        if any(
            not all(math.isfinite(v) for v in (c.in_s, c.out_s))
            or c.src_width <= 0
            or c.src_height <= 0
            or not c.reason
            for c in self.clips
        ):
            raise DeliveryError("tiempos o dimensiones de clip inválidos")
        title_ids = [t.title_id for t in self.titles]
        if len(title_ids) != len(set(title_ids)):
            raise DeliveryError("title_id duplicado")
        if any(
            not t.title_id
            or not all(math.isfinite(v) for v in (t.start_s, t.end_s))
            or t.start_s < 0
            or t.end_s <= t.start_s
            for t in self.titles
        ):
            raise DeliveryError("rango de título inválido")
        if any(
            not isinstance(transition, str) or not transition
            for transition in self.transitions
        ):
            raise DeliveryError("transición inválida")
        for stem in self.audio_stems:
            if not stem.stem_id or not stem.source_path:
                raise DeliveryError("cada audio stem requiere id y source_path")
            if not math.isfinite(stem.gain_db) or not 1 <= stem.channels <= 8:
                raise DeliveryError("ganancia o número de canales de audio inválido")

    def to_dict(self) -> dict[str, Any]:
        """Serializa una edición reproducible con versión explícita de esquema."""
        self.validate()
        return {
            "schema": "comfyuipod.editable-delivery",
            "schema_version": self.SCHEMA_VERSION,
            "name": self.name,
            "aspect": self.aspect,
            "fps": self.fps,
            "clips": [
                {"clip_id": c.clip_id, "source_path": c.source_path, "in_s": c.in_s,
                 "out_s": c.out_s, "license": c.license, "src_width": c.src_width,
                 "src_height": c.src_height, "source_asset_id": c.source_asset_id,
                 "reason": c.reason}
                for c in self.clips
            ],
            "titles": [
                {"title_id": t.title_id, "text": t.text, "start_s": t.start_s, "end_s": t.end_s}
                for t in self.titles
            ],
            "transitions": list(self.transitions),
            "audio_stems": [
                {"stem_id": a.stem_id, "source_path": a.source_path, "role": a.role,
                 "license": a.license, "gain_db": a.gain_db, "channels": a.channels}
                for a in self.audio_stems
            ],
            "delivery": {
                "color_space": self.color_space,
                "color_range": self.color_range,
                "safe_area_percent": self.safe_area_percent,
                "audio_loudness_lufs": self.audio_loudness_lufs,
                "true_peak_dbfs": self.true_peak_dbfs,
            },
        }

    def to_json(self) -> str:
        """Devuelve JSON estable, listo para guardar como archivo de proyecto."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TimelineSpec:
        """Carga el formato versionado y rechaza esquemas desconocidos."""
        if data.get("schema") != "comfyuipod.editable-delivery":
            raise DeliveryError("schema de proyecto desconocido")
        if data.get("schema_version") != cls.SCHEMA_VERSION:
            raise DeliveryError(f"schema_version no soportado: {data.get('schema_version')}")
        delivery = data.get("delivery") or {}
        try:
            spec = cls(
                name=str(data["name"]), aspect=str(data["aspect"]), fps=int(data["fps"]),
                clips=[Clip(**item) for item in data["clips"]],
                titles=[Title(**item) for item in data.get("titles", [])],
                transitions=list(data.get("transitions", [])),
                audio_stems=[AudioStem(**item) for item in data.get("audio_stems", [])],
                color_space=delivery.get("color_space", "bt709"),
                color_range=delivery.get("color_range", "limited"),
                safe_area_percent=float(delivery.get("safe_area_percent", 5.0)),
                audio_loudness_lufs=float(delivery.get("audio_loudness_lufs", -16.0)),
                true_peak_dbfs=float(delivery.get("true_peak_dbfs", -1.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DeliveryError(f"proyecto editable inválido: {exc}") from exc
        spec.validate()
        return spec

    @classmethod
    def from_json(cls, value: str) -> TimelineSpec:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DeliveryError(f"JSON de proyecto inválido: {exc}") from exc
        if not isinstance(data, dict):
            raise DeliveryError("el proyecto editable debe ser un objeto JSON")
        return cls.from_dict(data)


def write_editable_package(spec: TimelineSpec, destination: str | Path) -> Path:
    """Empaqueta proyecto, medios fuente y manifiesto en un directorio portable.

    No sobrescribe destinos existentes. Cada archivo copiado se verifica por
    SHA-256 y el proyecto apunta a sus copias incluidas, no a las originales.
    """
    spec.validate()
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise DeliveryError(f"el destino ya existe: {target}")
    source_records: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as stage_name:
        stage = Path(stage_name)
        assets = stage / "assets"
        assets.mkdir()
        packaged = TimelineSpec.from_dict(spec.to_dict())
        media = [
            ("clip", c.clip_id, c.source_path, lambda value, c=c: setattr(c, "source_path", value))
            for c in packaged.clips
        ] + [
            ("audio", a.stem_id, a.source_path, lambda value, a=a: setattr(a, "source_path", value))
            for a in packaged.audio_stems
        ]
        seen: dict[Path, str] = {}
        for kind, media_id, raw_path, set_path in media:
            src = Path(raw_path).expanduser().resolve()
            if not src.is_file():
                raise DeliveryError(f"medio fuente inexistente o no es archivo: {raw_path}")
            if src in seen:
                relative = seen[src]
                set_path(relative)
                continue
            digest = hashlib.sha256(src.read_bytes()).hexdigest()
            safe_id = hashlib.sha256(media_id.encode("utf-8")).hexdigest()[:12]
            relative = f"assets/{kind}-{safe_id}-{src.name}"
            out = stage / relative
            shutil.copy2(src, out)
            if hashlib.sha256(out.read_bytes()).hexdigest() != digest:
                raise DeliveryError(f"falló la verificación SHA-256 de {src}")
            seen[src] = relative
            set_path(relative)
            source_records.append({
                "path": relative, "sha256": digest, "kind": kind,
                "media_id": media_id, "original_name": src.name,
            })
        packaged.validate()
        (stage / "project.json").write_text(packaged.to_json(), encoding="utf-8")
        manifest = build_delivery_manifest(packaged).to_dict()
        manifest["schema"] = "comfyuipod.delivery-manifest"
        manifest["schema_version"] = 1
        manifest["sources"] = source_records
        (stage / "delivery-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(stage, target)
    return target


def load_editable_package(directory: str | Path) -> TimelineSpec:
    """Reabre el proyecto de un paquete y valida integridad y confinamiento."""
    root = Path(directory).expanduser().resolve(strict=True)
    project_path = root / "project.json"
    manifest_path = root / "delivery-manifest.json"
    try:
        spec = TimelineSpec.from_json(project_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"paquete editable incompleto o inválido: {exc}") from exc
    if (
        manifest.get("schema") != "comfyuipod.delivery-manifest"
        or manifest.get("schema_version") != 1
    ):
        raise DeliveryError("schema de manifiesto no soportado")
    records = manifest.get("sources")
    if not isinstance(records, list):
        raise DeliveryError("manifiesto sin inventario de fuentes")
    record_paths = {str(record.get("path", "")) for record in records}
    project_paths = {
        media_path
        for media_path in (
            [clip.source_path for clip in spec.clips]
            + [stem.source_path for stem in spec.audio_stems]
        )
    }
    if project_paths != record_paths:
        raise DeliveryError("inventario de medios no coincide con el proyecto")
    for record in records:
        relative = Path(str(record.get("path", "")))
        asset = (root / relative).resolve()
        if relative.is_absolute() or not asset.is_relative_to(root) or not asset.is_file():
            raise DeliveryError(f"ruta de medio insegura o inexistente: {relative}")
        actual = hashlib.sha256(asset.read_bytes()).hexdigest()
        if actual != record.get("sha256"):
            raise DeliveryError(f"hash de medio no coincide: {relative}")
    return spec


def write_editable_package_archive(spec: TimelineSpec, archive_path: str | Path) -> Path:
    """Escribe un ZIP portable con el proyecto, sus fuentes y el manifiesto."""
    archive = Path(archive_path).expanduser().resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="editable-delivery-") as workdir:
        package = write_editable_package(spec, Path(workdir) / "package")
        with ZipFile(archive, "w", compression=ZIP_STORED) as bundle:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(package).as_posix())
    return archive


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
    color_range: str = "limited"
    audio_stems: list[dict[str, Any]] = field(default_factory=list)
    safe_area_percent: float = 5.0
    audio_loudness_lufs: float = -16.0
    true_peak_dbfs: float = -1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "resolution": f"{self.width}x{self.height}",
            "fps": self.fps,
            "duration_s": round(self.duration_s, 3),
            "color_space": self.color_space,
            "color_range": self.color_range,
            "audio_stems": list(self.audio_stems),
            "safe_area_percent": self.safe_area_percent,
            "audio_loudness_lufs": self.audio_loudness_lufs,
            "true_peak_dbfs": self.true_peak_dbfs,
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
        {
            "clip": c.clip_id,
            "source": c.source_path,
            "source_asset_id": c.source_asset_id,
            "in_s": c.in_s,
            "out_s": c.out_s,
            "reason": c.reason,
            "license": c.license,
        }
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
        color_space=spec.color_space,
        color_range=spec.color_range,
        audio_stems=[
            {"stem_id": a.stem_id, "source": a.source_path, "role": a.role,
             "license": a.license, "gain_db": a.gain_db, "channels": a.channels}
            for a in spec.audio_stems
        ],
        safe_area_percent=spec.safe_area_percent,
        audio_loudness_lufs=spec.audio_loudness_lufs,
        true_peak_dbfs=spec.true_peak_dbfs,
    )


__all__ = [
    "ASPECT_4K",
    "AudioStem",
    "Clip",
    "DeliveryError",
    "DeliveryManifest",
    "TimelineSpec",
    "Title",
    "build_delivery_manifest",
    "edit_text",
    "finishing_plan",
    "load_editable_package",
    "target_dimensions",
    "write_editable_package",
    "write_editable_package_archive",
]
