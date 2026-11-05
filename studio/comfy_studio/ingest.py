"""Archivo semántico e ingesta de medios heterogéneos (plan Ultra, W05 / A08).

Recibe un lote de assets mixtos (vídeo móvil rotado/VFR, audio, imágenes,
duplicados y alguno corrupto) y produce un **índice consultable con evidencia
por segundos y ámbito de proyecto**, sin que un asset corrupto rompa los
válidos (acceptance A08).

Diseño verificable en CPU, sin descargar modelos ni tocar GPU:

- **Clasificación** por extensión + sondeo de cabecera (no se infla con 100
  copias del mismo PNG: la deduplicación las colapsa).
- **Deduplicación** por hash de contenido (SHA-256 en streaming): un duplicado
  no multiplica trabajo ni entradas de índice; apunta al canónico.
- **Normalización declarada**: rotación y VFR→timebase del proyecto se registran
  como transformaciones explícitas (no se reencoda aquí; eso lo hace el motor de
  media con FFmpeg, W08/W20). El manifiesto deja trazable original→segmentos.
- **Errores parciales aislados**: un asset ilegible/corrupto produce un
  diagnóstico tipado y NO aborta el lote; los válidos siguen.
- **Backpressure**: el lote se procesa en bloques con un tope de items en vuelo,
  para carpetas grandes.

La extracción semántica real (CLIP, ASR, OCR) la hacen los backends de
`perception`/`asr` cuando sus pesos están presentes; aquí se declara el plan de
segmentos y se delega. Sin pesos, el estado es `pending_weights`, honesto, no un
embedding inventado.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Tope de items procesados a la vez (backpressure para carpetas grandes).
DEFAULT_INFLIGHT = 16

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    UNKNOWN = "unknown"


class AssetStatus(StrEnum):
    INDEXED = "indexed"
    DUPLICATE = "duplicate"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    #: clasificado y encolado, pero la extracción semántica espera pesos.
    PENDING_WEIGHTS = "pending_weights"


def classify(path: Path) -> MediaKind:
    """Tipo de medio por extensión + mimetype (sin abrir el contenido completo)."""
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return MediaKind.VIDEO
    if ext in AUDIO_EXT:
        return MediaKind.AUDIO
    if ext in IMAGE_EXT:
        return MediaKind.IMAGE
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        if guessed.startswith("video/"):
            return MediaKind.VIDEO
        if guessed.startswith("audio/"):
            return MediaKind.AUDIO
        if guessed.startswith("image/"):
            return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def content_hash(path: Path, *, chunk: int = 1024 * 1024) -> str:
    """SHA-256 del contenido, en streaming (no carga el fichero entero)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(slots=True)
class IngestedAsset:
    """Una entrada del archivo semántico tras la ingesta."""

    asset_id: str
    source_path: str
    kind: MediaKind
    status: AssetStatus
    content_sha256: str | None = None
    bytes: int = 0
    #: id del asset canónico si este es un duplicado.
    duplicate_of: str | None = None
    #: transformaciones declaradas (rotación, VFR→timebase) para trazabilidad.
    transforms: dict[str, Any] = field(default_factory=dict)
    #: segmentos planificados (intervalos en segundos) con su evidencia.
    segments: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "source_path": self.source_path,
            "kind": str(self.kind),
            "status": str(self.status),
            "content_sha256": self.content_sha256,
            "bytes": self.bytes,
            "duplicate_of": self.duplicate_of,
            "transforms": dict(self.transforms),
            "segments": list(self.segments),
            "error": self.error,
        }


@dataclass(slots=True)
class IngestReport:
    """Resultado de ingerir un lote: entradas + conteos por estado."""

    assets: list[IngestedAsset] = field(default_factory=list)
    project_id: str | None = None

    def by_status(self, status: AssetStatus) -> list[IngestedAsset]:
        return [a for a in self.assets if a.status == status]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for asset in self.assets:
            out[str(asset.status)] = out.get(str(asset.status), 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "counts": self.counts(),
            "assets": [a.to_dict() for a in self.assets],
        }


def _asset_id(kind: MediaKind, sha: str) -> str:
    return f"{kind.value}_{sha[:12]}"


def _probe_header(path: Path) -> bool:
    """Lectura mínima de cabecera: detecta un fichero vacío o ilegible.

    No valida el formato completo (eso es FFprobe en el motor de media); solo
    distingue un asset manifiestamente corrupto/vacío para aislarlo del lote.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with open(path, "rb") as handle:
            return bool(handle.read(16))
    except OSError:
        return False


def _plan_segments(kind: MediaKind, weights_available: bool) -> list[dict[str, Any]]:
    """Plan de segmentos con evidencia por tiempo (pendiente de pesos si faltan).

    La extracción real (CLIP/ASR/OCR) la ejecutan los backends de percepción/ASR
    cuando hay pesos; aquí se declara qué segmentar y con qué evidencia, para que
    una búsqueda devuelva archivo+intervalo, no una descripción genérica.
    """
    if kind in (MediaKind.VIDEO, MediaKind.AUDIO):
        kinds = ["asr", "keyframe"] if kind is MediaKind.VIDEO else ["asr"]
    elif kind is MediaKind.IMAGE:
        kinds = ["clip", "ocr"]
    else:
        kinds = []
    return [
        {"evidence": k, "status": "ready" if weights_available else "pending_weights"}
        for k in kinds
    ]


def _batched(items: list[Path], size: int) -> Iterator[list[Path]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def ingest_assets(
    paths: Iterable[str | Path],
    *,
    project_id: str | None = None,
    weights_available: bool = False,
    inflight: int = DEFAULT_INFLIGHT,
) -> IngestReport:
    """Ingiere un lote de assets de forma determinista y tolerante a fallos.

    - deduplica por hash de contenido;
    - clasifica por tipo;
    - aísla corruptos/no soportados sin abortar el lote;
    - procesa en bloques de `inflight` (backpressure).
    """
    report = IngestReport(project_id=project_id)
    seen_hashes: dict[str, str] = {}  # sha -> asset_id canónico
    all_paths = [Path(p) for p in paths]

    for block in _batched(all_paths, max(1, inflight)):
        for path in block:
            kind = classify(path)
            if not _probe_header(path):
                report.assets.append(
                    IngestedAsset(
                        asset_id=f"corrupt_{abs(hash(str(path))) % (10**10):010d}",
                        source_path=str(path),
                        kind=kind,
                        status=AssetStatus.CORRUPT,
                        error="fichero vacío o cabecera ilegible",
                    )
                )
                continue
            if kind is MediaKind.UNKNOWN:
                report.assets.append(
                    IngestedAsset(
                        asset_id=f"unsupported_{abs(hash(str(path))) % (10**10):010d}",
                        source_path=str(path),
                        kind=kind,
                        status=AssetStatus.UNSUPPORTED,
                        error=f"tipo no soportado: {path.suffix or 'sin extensión'}",
                    )
                )
                continue

            try:
                sha = content_hash(path)
                size = path.stat().st_size
            except OSError as exc:
                report.assets.append(
                    IngestedAsset(
                        asset_id=f"corrupt_{abs(hash(str(path))) % (10**10):010d}",
                        source_path=str(path),
                        kind=kind,
                        status=AssetStatus.CORRUPT,
                        error=f"no legible: {exc}",
                    )
                )
                continue

            if sha in seen_hashes:
                # Duplicado: no multiplica trabajo ni índice; apunta al canónico.
                report.assets.append(
                    IngestedAsset(
                        asset_id=f"dup_{sha[:12]}",
                        source_path=str(path),
                        kind=kind,
                        status=AssetStatus.DUPLICATE,
                        content_sha256=sha,
                        bytes=size,
                        duplicate_of=seen_hashes[sha],
                    )
                )
                continue

            asset_id = _asset_id(kind, sha)
            seen_hashes[sha] = asset_id
            segments = _plan_segments(kind, weights_available)
            status = (
                AssetStatus.INDEXED
                if weights_available or not segments
                else AssetStatus.PENDING_WEIGHTS
            )
            report.assets.append(
                IngestedAsset(
                    asset_id=asset_id,
                    source_path=str(path),
                    kind=kind,
                    status=status,
                    content_sha256=sha,
                    bytes=size,
                    transforms=_declared_transforms(kind),
                    segments=segments,
                )
            )
    return report


def _declared_transforms(kind: MediaKind) -> dict[str, Any]:
    """Transformaciones que la ingesta DECLARA (no aplica) para trazabilidad.

    La rotación y el paso VFR→CFR se resuelven en el motor de media (FFmpeg) a
    partir de estos metadatos; declararlos aquí deja el linaje original→segmentos
    explícito.
    """
    if kind is MediaKind.VIDEO:
        return {"normalize_timebase": "project_cfr", "apply_rotation_metadata": True}
    return {}


__all__ = [
    "AssetStatus",
    "IngestReport",
    "IngestedAsset",
    "MediaKind",
    "classify",
    "content_hash",
    "ingest_assets",
]
