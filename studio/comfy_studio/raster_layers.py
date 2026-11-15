"""CPU compositor for project-owned, lossless RGBA raster layers."""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from comfy_studio.projects import ProjectStore


class RasterLayerError(ValueError):
    """A raster layer set cannot be safely composed."""


class RasterLayerStaleError(RasterLayerError):
    """A project asset no longer matches the source bytes pinned by its layer."""


class RasterLayerIntegrityUnknownError(RasterLayerError):
    """A legacy layer has no pinned source hash, so its integrity is unknown."""


MAX_RASTER_LAYERS = 32

# Round-trip exactness boundary: only 8-bit PNGs whose Pillow mode converts
# losslessly to RGBA (unassociated straight alpha) are supported. Anything
# else (16-bit depth, palette, CMYK, float modes) is rejected fail-closed.
EXACT_RGBA_SUPPORTED_MODES = frozenset({"RGBA", "RGB", "LA", "L"})
RGBA_ROUNDTRIP_SEMANTICS = "unassociated-straight-alpha-8bit"


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


def _working_memory_budget() -> tuple[int, int]:
    """Reserve at most five percent of available memory for CPU raster work.

    Four RGBA surfaces per pixel conservatively cover source, destination and
    Pillow's alpha-composite temporaries.
    """
    available_memory = _available_memory_bytes()
    if available_memory is None or available_memory <= 0:
        raise RasterLayerError("memoria disponible desconocida; composición rechazada")
    working_memory_budget = available_memory // 20
    max_pixels = working_memory_budget // 16
    if max_pixels < 1:
        raise RasterLayerError("memoria disponible insuficiente para composición RGBA")
    return working_memory_budget, max_pixels


@dataclass(frozen=True, slots=True)
class RasterLayer:
    layer_id: str
    asset_id: str
    caused_by: str | None = None
    region: str = "full"
    order: int = 0
    source_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "asset_id": self.asset_id,
            "caused_by": self.caused_by,
            "region": self.region,
            "order": self.order,
            "source_sha256": self.source_sha256,
        }


def validate_layers(layers: list[RasterLayer]) -> list[RasterLayer]:
    if len(layers) > MAX_RASTER_LAYERS:
        raise RasterLayerError(f"máximo {MAX_RASTER_LAYERS} capas por conjunto")
    for layer in layers:
        if not isinstance(layer.layer_id, str) or not 1 <= len(layer.layer_id) <= 64:
            raise RasterLayerError("layer_id debe tener entre 1 y 64 caracteres")
        if not isinstance(layer.asset_id, str) or not 1 <= len(layer.asset_id) <= 64:
            raise RasterLayerError("asset_id debe tener entre 1 y 64 caracteres")
        if not isinstance(layer.region, str) or not 1 <= len(layer.region) <= 128:
            raise RasterLayerError("region debe tener entre 1 y 128 caracteres")
        if layer.caused_by is not None and (
            not isinstance(layer.caused_by, str) or len(layer.caused_by) > 64
        ):
            raise RasterLayerError("caused_by inválido")
        if (
            isinstance(layer.order, bool)
            or not isinstance(layer.order, int)
            or not 0 <= layer.order < MAX_RASTER_LAYERS
        ):
            raise RasterLayerError(f"order debe estar entre 0 y {MAX_RASTER_LAYERS - 1}")
        if layer.source_sha256 is not None and (
            not isinstance(layer.source_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", layer.source_sha256)
        ):
            raise RasterLayerError("source_sha256 inválido")
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


def affected_by(layers: list[RasterLayer], changed_id: str) -> set[str]:
    """Return the changed layer and its transitive causal descendants."""
    ordered = validate_layers(layers)
    if changed_id not in {layer.layer_id for layer in ordered}:
        raise RasterLayerError(f"capa inexistente: {changed_id}")
    affected = {changed_id}
    while True:
        descendants = {
            layer.layer_id
            for layer in ordered
            if layer.caused_by in affected and layer.layer_id not in affected
        }
        if not descendants:
            return affected
        affected.update(descendants)


def _load_project_png(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    *,
    max_pixels: int,
    expected_sha256: str | None = None,
) -> tuple[Image.Image, str, Path]:
    """Open a project-owned PNG and return it loaded, unconverted, plus its hash."""
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
            digest = _sha256_file(path)
            if expected_sha256 is not None and digest != expected_sha256:
                raise RasterLayerStaleError(
                    f"{asset_id}: la fuente cambió desde que se guardó la capa"
                )
            return source.copy(), digest, path
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise RasterLayerError(f"{asset_id}: PNG ilegible") from exc


def _open_rgba(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    *,
    max_pixels: int,
    expected_sha256: str | None = None,
) -> tuple[Image.Image, str]:
    source, digest, _ = _load_project_png(
        store,
        project_id,
        asset_id,
        max_pixels=max_pixels,
        expected_sha256=expected_sha256,
    )
    try:
        return source.convert("RGBA"), digest
    finally:
        source.close()


def validate_asset_references(
    store: ProjectStore, project_id: str, layers: list[RasterLayer]
) -> None:
    """Require every layer reference to resolve to a project-owned PNG asset."""
    for layer in validate_layers(layers):
        asset = store.get_asset(project_id, layer.asset_id)
        path = store.get_asset_path(project_id, layer.asset_id)
        if asset is None or path is None:
            raise RasterLayerError(
                f"asset inexistente en este proyecto: {layer.asset_id}"
            )
        if asset.get("kind") != "image" or path.suffix.lower() != ".png":
            raise RasterLayerError(f"{layer.asset_id}: se requiere un PNG del proyecto")


def pin_asset_sources(
    store: ProjectStore, project_id: str, layers: list[RasterLayer]
) -> list[RasterLayer]:
    """Return layers pinned to the current bytes of their project-owned PNGs."""
    ordered = validate_layers(layers)
    validate_asset_references(store, project_id, ordered)
    pinned: list[RasterLayer] = []
    for layer in ordered:
        path = store.get_asset_path(project_id, layer.asset_id)
        if path is None:
            raise RasterLayerError(f"asset inexistente en este proyecto: {layer.asset_id}")
        pinned.append(replace(layer, source_sha256=_sha256_file(path)))
    return pinned


def stale_layer_ids(
    store: ProjectStore, project_id: str, layers: list[RasterLayer]
) -> list[str]:
    """List layers whose project asset no longer matches its pinned source hash."""
    stale: list[str] = []
    for layer in validate_layers(layers):
        if layer.source_sha256 is None:
            continue  # Legacy manifests predate source-byte pinning.
        path = store.get_asset_path(project_id, layer.asset_id)
        if path is None or _sha256_file(path) != layer.source_sha256:
            stale.append(layer.layer_id)
    return stale


def integrity_unknown_layer_ids(layers: list[RasterLayer]) -> list[str]:
    """List legacy layers with no source hash; their current bytes are unverified."""
    return [layer.layer_id for layer in validate_layers(layers) if layer.source_sha256 is None]


def composite_layers(
    store: ProjectStore, project_id: str, layers: list[RasterLayer]
) -> tuple[bytes, dict[str, Any]]:
    """Compose same-size PNG assets in order and emit a lossless PNG artifact."""
    ordered = validate_layers(layers)
    if not ordered:
        raise RasterLayerError("se requiere al menos una capa")
    unknown = integrity_unknown_layer_ids(ordered)
    if unknown:
        raise RasterLayerIntegrityUnknownError(
            "integrity unknown for layers without a pinned source hash: " + ", ".join(unknown)
        )
    working_memory_budget, max_pixels = _working_memory_budget()
    canvas: Image.Image | None = None
    sources: list[dict[str, str]] = []
    try:
        for layer in ordered:
            image, digest = _open_rgba(
                store,
                project_id,
                layer.asset_id,
                max_pixels=max_pixels,
                expected_sha256=layer.source_sha256,
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
        data = _encode_png(canvas)
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


def _encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _png_bit_depth(path: Path) -> int | None:
    """Parse the IHDR bit-depth byte; None when the header is not readable."""
    header = path.read_bytes()[:33]
    if len(header) < 33 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if header[12:16] != b"IHDR":
        return None
    return header[24]


def rgba_roundtrip(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    *,
    semantics: str,
    source_sha256: str | None = None,
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Decompose a project PNG into color and alpha planes and reassemble it.

    Semantics are explicit and fail-closed: only 8-bit PNGs with unassociated
    (straight) alpha are supported. The color plane carries the original RGB
    with a fully opaque alpha channel, so RGB under alpha=0 survives the
    decompose step verbatim; the alpha plane is the isolated alpha channel.
    Exactness is proven by decoding the emitted plane PNGs, reconstructing the
    RGBA image from those decoded planes and comparing bytes against the
    loaded source; any mismatch is an error, never a relaxed claim.
    """
    if semantics != RGBA_ROUNDTRIP_SEMANTICS:
        raise RasterLayerError(
            f"semántica {semantics!r} no soportada; se requiere "
            f"{RGBA_ROUNDTRIP_SEMANTICS!r} (alpha recto no premultiplicado)"
        )
    working_memory_budget, max_pixels = _working_memory_budget()
    source, digest, path = _load_project_png(
        store,
        project_id,
        asset_id,
        max_pixels=max_pixels,
        expected_sha256=source_sha256,
    )
    # Inicializado antes del try exterior: si el modo o la profundidad se
    # rechazan, rgba aún no existe y el finally no debe desreferenciarlo.
    rgba: Image.Image | None = None
    try:
        if source.mode not in EXACT_RGBA_SUPPORTED_MODES:
            raise RasterLayerError(
                f"{asset_id}: modo {source.mode!r} no soportado para roundtrip "
                "RGBA exacto; se requiere 8-bit RGBA/RGB/LA/L"
            )
        bit_depth = _png_bit_depth(path)
        if bit_depth != 8:
            raise RasterLayerError(
                f"{asset_id}: profundidad {bit_depth!r} no soportada para "
                "roundtrip RGBA exacto; se requiere PNG de 8 bits"
            )
        rgba = source.convert("RGBA")
        # Every image below is initialized before its try/finally so the
        # cleanup closes exactly the resources that were actually created.
        red: Image.Image | None = None
        green: Image.Image | None = None
        blue: Image.Image | None = None
        alpha: Image.Image | None = None
        opaque_alpha: Image.Image | None = None
        color_plane: Image.Image | None = None
        decoded_color: Image.Image | None = None
        decoded_alpha: Image.Image | None = None
        reconstructed: Image.Image | None = None
        try:
            red, green, blue, alpha = rgba.split()
            opaque_alpha = Image.new("L", rgba.size, 255)
            color_plane = Image.merge("RGBA", (red, green, blue, opaque_alpha))
            # Encode both planes as the PNGs that will be delivered, then
            # decode exactly those bytes: the proof covers the delivered
            # artifacts, not in-memory intermediates.
            color_png = _encode_png(color_plane)
            alpha_png = _encode_png(alpha)
            try:
                decoded_color = Image.open(io.BytesIO(color_png))
                decoded_alpha = Image.open(io.BytesIO(alpha_png))
                if decoded_color.mode != "RGBA" or decoded_alpha.mode != "L":
                    raise RasterLayerError(
                        f"{asset_id}: los planos emitidos no decodifican en "
                        "RGBA/L; roundtrip rechazado"
                    )
                color_bands = decoded_color.split()
                reconstructed = Image.merge(
                    "RGBA", (color_bands[0], color_bands[1], color_bands[2], decoded_alpha)
                )
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
                raise RasterLayerError(
                    f"{asset_id}: los planos emitidos no son PNG legibles"
                ) from exc
            try:
                source_rgba_bytes = rgba.tobytes()
                reconstructed_bytes = reconstructed.tobytes()
                if source_rgba_bytes != reconstructed_bytes:
                    raise RasterLayerError(
                        f"{asset_id}: la reconstrucción RGBA no es "
                        "idéntica a la fuente; roundtrip rechazado"
                    )
                source_rgba_sha256 = hashlib.sha256(source_rgba_bytes).hexdigest()
                reconstructed_sha256 = hashlib.sha256(reconstructed_bytes).hexdigest()
                evidence: dict[str, Any] = {
                    "exact_rgba_roundtrip": True,
                    "semantics": {
                        "declared": semantics,
                        "alpha_encoding": "unassociated (straight) alpha",
                        "premultiplied": False,
                        "bit_depth": 8,
                        "modes_supported": sorted(EXACT_RGBA_SUPPORTED_MODES),
                    },
                    # La igualdad exacta byte a byte (medida sobre los planos
                    # PNG decodificados) demuestra que el RGB bajo alpha=0 no
                    # fue premultiplicado ni descartado.
                    "rgb_under_alpha_zero": "preserved",
                    "source": {
                        "asset_id": asset_id,
                        "mode": source.mode,
                        "width": rgba.width,
                        "height": rgba.height,
                        "sha256": digest,
                    },
                    "source_rgba_sha256": source_rgba_sha256,
                    "reconstructed_sha256": reconstructed_sha256,
                    "color_plane_sha256": hashlib.sha256(color_png).hexdigest(),
                    "alpha_plane_sha256": hashlib.sha256(alpha_png).hexdigest(),
                    "working_memory_budget_bytes": working_memory_budget,
                    "renderer": "pillow-cpu",
                    "matting": "not_run",
                }
                return color_png, alpha_png, evidence
            finally:
                reconstructed.close()
        finally:
            for plane in (red, green, blue, alpha, opaque_alpha, color_plane):
                if plane is not None:
                    plane.close()
            if decoded_color is not None:
                decoded_color.close()
            if decoded_alpha is not None:
                decoded_alpha.close()
    finally:
        if rgba is not None:
            rgba.close()
        source.close()


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
    "RasterLayerIntegrityUnknownError",
    "affected_by",
    "canonical_manifest",
    "composite_layers",
    "integrity_unknown_layer_ids",
    "parse_manifest",
    "rgba_roundtrip",
    "validate_asset_references",
    "validate_layers",
]
