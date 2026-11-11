"""CPU compositor for project-owned, lossless RGBA raster layers."""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from comfy_studio.projects import ProjectStore


class RasterLayerError(ValueError):
    """A raster layer set cannot be safely composed."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _available_memory_bytes() -> int | None:
    """Read current available memory, accounting for cgroup limits when present."""
    available: list[int] = []
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    available.append(int(line.split()[1]) * 1024)
                    break
        except (OSError, ValueError, IndexError):
            pass
    current_paths = (
        (Path("/sys/fs/cgroup/memory.current"), Path("/sys/fs/cgroup/memory.max")),
        (
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        ),
    )
    for current_path, limit_path in current_paths:
        if current_path.is_file() and limit_path.is_file():
            try:
                current = int(current_path.read_text().strip())
                raw_limit = limit_path.read_text().strip()
                if raw_limit != "max":
                    limit = int(raw_limit)
                    if 0 < limit < (1 << 62):
                        available.append(max(0, limit - current))
                        break
            except (OSError, ValueError):
                pass
    if not available and hasattr(os, "sysconf"):
        try:
            available.append(
                int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
            )
        except (OSError, ValueError):
            pass
    if not available:
        try:
            proc = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=3, check=True
            )
            page_match = re.search(r"page size of (\d+) bytes", proc.stdout)
            page_size = int(page_match.group(1)) if page_match else 0
            page_counts = {
                name: int(count.replace(".", ""))
                for name, count in re.findall(
                    r"Pages (free|inactive|speculative|purgeable):\s+([\d.]+)",
                    proc.stdout,
                )
            }
            mac_available = sum(
                page_counts.get(name, 0)
                for name in ("free", "inactive", "speculative", "purgeable")
            ) * page_size
            if page_size > 0 and mac_available > 0:
                available.append(mac_available)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return min(available) if available else None


@dataclass(frozen=True, slots=True)
class RasterLayer:
    layer_id: str
    asset_id: str
    caused_by: str | None = None
    region: str = "full"
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "asset_id": self.asset_id,
            "caused_by": self.caused_by,
            "region": self.region,
            "order": self.order,
        }


def validate_layers(layers: list[RasterLayer]) -> list[RasterLayer]:
    ids = [layer.layer_id for layer in layers]
    if len(ids) != len(set(ids)):
        raise RasterLayerError("layer_id duplicado")
    if any(not item.strip() for item in ids):
        raise RasterLayerError("layer_id vacío")
    if any(not layer.asset_id.strip() or not layer.region.strip() for layer in layers):
        raise RasterLayerError("asset_id y region son obligatorios")
    if any(layer.caused_by == layer.layer_id for layer in layers):
        raise RasterLayerError("una capa no puede depender de sí misma")
    by_id = {layer.layer_id: layer for layer in layers}
    for layer in layers:
        if layer.caused_by is not None and layer.caused_by not in by_id:
            raise RasterLayerError(
                f"{layer.layer_id}: capa causal inexistente {layer.caused_by!r}"
            )
        seen: set[str] = set()
        parent = layer.caused_by
        while parent is not None:
            if parent in seen or parent == layer.layer_id:
                raise RasterLayerError(f"ciclo causal cerca de {layer.layer_id!r}")
            seen.add(parent)
            parent = by_id[parent].caused_by
    if len({layer.order for layer in layers}) != len(layers):
        raise RasterLayerError("order debe ser único por capa")
    return sorted(layers, key=lambda layer: layer.order)


def _open_rgba(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    *,
    max_pixels: int,
) -> tuple[Image.Image, str]:
    asset = store.get_asset(project_id, asset_id)
    path = store.get_asset_path(project_id, asset_id)
    if asset is None or path is None:
        raise RasterLayerError(f"asset inexistente en este proyecto: {asset_id}")
    if asset.get("kind") != "image" or path.suffix.lower() != ".png":
        raise RasterLayerError(f"{asset_id}: se requiere un PNG del proyecto")
    try:
        with Image.open(path) as source:
            if source.format != "PNG":
                raise RasterLayerError(f"{asset_id}: el archivo no es un PNG válido")
            if source.width * source.height > max_pixels:
                raise RasterLayerError(
                    f"{asset_id}: imagen excede el presupuesto dinámico de memoria"
                )
            source.load()
            return source.convert("RGBA"), _sha256_file(path)
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise RasterLayerError(f"{asset_id}: PNG ilegible") from exc


def composite_layers(
    store: ProjectStore, project_id: str, layers: list[RasterLayer]
) -> tuple[bytes, dict[str, Any]]:
    """Compose same-size PNG assets in order and emit a lossless PNG artifact."""
    ordered = validate_layers(layers)
    if not ordered:
        raise RasterLayerError("se requiere al menos una capa")
    available_memory = _available_memory_bytes()
    if available_memory is None or available_memory <= 0:
        raise RasterLayerError("memoria disponible desconocida; composición rechazada")
    # At most five percent of currently available memory is assigned to the
    # compositor. Four RGBA surfaces per pixel conservatively cover source,
    # destination and Pillow's alpha-composite temporaries.
    working_memory_budget = available_memory // 20
    max_pixels = working_memory_budget // 16
    if max_pixels < 1:
        raise RasterLayerError("memoria disponible insuficiente para composición RGBA")
    canvas: Image.Image | None = None
    sources: list[dict[str, str]] = []
    try:
        for layer in ordered:
            image, digest = _open_rgba(
                store, project_id, layer.asset_id, max_pixels=max_pixels
            )
            try:
                if canvas is None:
                    canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
                if image.size != canvas.size:
                    raise RasterLayerError(
                        "todas las capas deben tener las mismas dimensiones"
                    )
                previous = canvas
                canvas = Image.alpha_composite(previous, image)
                previous.close()
                sources.append({"layer_id": layer.layer_id, "sha256": digest})
            finally:
                image.close()
        assert canvas is not None
        encoded = io.BytesIO()
        canvas.save(encoded, format="PNG", optimize=False)
        data = encoded.getvalue()
        return data, {
            "width": canvas.width,
            "height": canvas.height,
            "sha256": hashlib.sha256(data).hexdigest(),
            "sources": sources,
            "working_memory_budget_bytes": working_memory_budget,
            "renderer": "pillow-alpha-composite-cpu",
            "matting": "not_run",
            "temporal_layering": "not_run",
        }
    finally:
        if canvas is not None:
            canvas.close()


def canonical_manifest(layers: list[RasterLayer]) -> dict[str, Any]:
    return {
        "schema": "raster-layer-set/1",
        "layers": [layer.to_dict() for layer in validate_layers(layers)],
    }


def parse_manifest(raw: Any) -> list[RasterLayer]:
    if not isinstance(raw, dict) or raw.get("schema") != "raster-layer-set/1":
        raise RasterLayerError("manifiesto de capas raster inválido")
    entries = raw.get("layers")
    if not isinstance(entries, list):
        raise RasterLayerError("layers debe ser una lista")
    try:
        layers = [RasterLayer(**item) for item in entries if isinstance(item, dict)]
    except TypeError as exc:
        raise RasterLayerError("entrada de capa raster inválida") from exc
    if len(layers) != len(entries):
        raise RasterLayerError("cada capa debe ser un objeto")
    return validate_layers(layers)


__all__ = [
    "RasterLayer",
    "RasterLayerError",
    "canonical_manifest",
    "composite_layers",
    "parse_manifest",
    "validate_layers",
]
