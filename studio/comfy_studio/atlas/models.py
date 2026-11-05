"""Contratos de datos del atlas (modelos tipados y serializables).

Contratos mínimos del encargo, adaptados al estilo del repo (dataclasses stdlib,
serialización JSON de ida y vuelta, nada lanza por sí mismo):

- Lugar: id estable, municipio, nombres/alias, geometría/bounds, sectores y
  referencias geográficas.
- Referencia: id, hash, archivo local, fuente, autor, licencia, revisión, fechas
  con fiabilidad de fecha, dimensiones nativas, calidad y usabilidad.
- Cámara/vista: posición y sistema de coordenadas, orientación, intrinsecos/FOV,
  método de estimación y estado de calibración. Desconocido es desconocido, no 0.
- Escenario: id+versión, referencias usadas, geometría disponible, transformaciones
  y capacidades verificadas.
- Cobertura: vistas observadas, región respaldada, superficies no observadas y
  antigüedad de la evidencia, con base de confianza medible.
- Producción: versión del atlas/escenario, cámara, referencias exactas,
  parámetros y resultado; serializable y recuperable tras reiniciar.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Environment(StrEnum):
    """Tipo de entorno urbano del lugar."""

    BRIDGE = "bridge"
    ROAD_CORRIDOR = "road_corridor"
    INTERSECTION = "intersection"
    PLAZA = "plaza"
    MIRADOR = "mirador"
    PARK = "park"
    RIVER = "river"
    STREET = "street"
    HUB = "hub"
    OTHER = "other"


#: municipios prioritarios del Valle de Aburrá (los 5 primeros son los objetivo).
MUNICIPALITIES: tuple[str, ...] = (
    "Medellín",
    "Envigado",
    "Sabaneta",
    "Itagüí",
    "Bello",
    "La Estrella",
    "Caldas",
    "Copacabana",
    "Girardota",
    "Barbosa",
)


class DateReliability(StrEnum):
    """Fiabilidad de la fecha de una referencia (nunca confundir subida con captura)."""

    CAPTURED = "captured"  # fecha de captura real (DateTimeOriginal)
    UPLOADED_ONLY = "uploaded_only"  # solo se conoce la subida
    HISTORICAL = "historical"  # material antiguo usado SOLO como referencia histórica
    UNKNOWN = "unknown"


class ResolutionClass(StrEnum):
    """Si el archivo local es el original o una copia reducida (nunca se simula detalle)."""

    ORIGINAL = "original"
    DERIVED_DOWNSCALE = "derived_downscale"


class CalibState(StrEnum):
    """Estado de calibración de una cámara/vista."""

    UNCALIBRATED = "uncalibrated"
    ESTIMATED = "estimated"
    VERIFIED = "verified"


class ScenarioKind(StrEnum):
    """Naturaleza del escenario: foto real, panorama, multivista o reconstrucción."""

    PHOTO_BG = "photo_bg"
    PANORAMA = "panorama"
    MULTIVIEW = "multiview"
    RECONSTRUCTION = "reconstruction"
    PROXY_3D = "proxy_3d"


class ScenarioStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"


@dataclass(slots=True)
class Place:
    """Un lugar georreferenciado del Valle de Aburrá (id estable, inmutable)."""

    place_id: str
    municipality: str
    name: str
    kind: Environment
    aliases: list[str] = field(default_factory=list)
    sector: str | None = None
    lat: float | None = None
    lon: float | None = None
    #: bounds (min_lat, max_lat, min_lon, max_lon) si el lugar tiene extensión.
    bounds: tuple[float, float, float, float] | None = None
    osm_refs: list[str] = field(default_factory=list)
    #: procedencia de la geometría/coordenadas (fuente + método), nunca inventada.
    provenance: dict[str, Any] = field(default_factory=dict)
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "place_id": self.place_id,
            "municipality": self.municipality,
            "name": self.name,
            "kind": str(self.kind),
            "aliases": list(self.aliases),
            "sector": self.sector,
            "lat": self.lat,
            "lon": self.lon,
            "bounds": list(self.bounds) if self.bounds else None,
            "osm_refs": list(self.osm_refs),
            "provenance": dict(self.provenance),
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Place:
        bounds = d.get("bounds")
        return cls(
            place_id=str(d["place_id"]),
            municipality=str(d["municipality"]),
            name=str(d["name"]),
            kind=Environment(d.get("kind", "other")),
            aliases=list(d.get("aliases", [])),
            sector=d.get("sector"),
            lat=d.get("lat"),
            lon=d.get("lon"),
            bounds=tuple(bounds) if bounds else None,
            osm_refs=list(d.get("osm_refs", [])),
            provenance=dict(d.get("provenance", {})),
            created_ts=float(d.get("created_ts", time.time())),
            updated_ts=float(d.get("updated_ts", time.time())),
        )


@dataclass(slots=True)
class Reference:
    """Una referencia fotográfica original (archivo local + metadatos completos)."""

    ref_id: str
    title: str
    file_path: str  # relativo a files/
    sha256: str
    source: str  # p. ej. "wikimedia_commons"
    place_id: str | None = None
    source_page: str | None = None
    source_file_url: str | None = None
    sha1_source: str | None = None  # hash del original en la fuente
    author: str | None = None
    license: str = "UNKNOWN"
    license_url: str | None = None
    attribution: str | None = None
    capture_date: str | None = None
    upload_date: str | None = None
    date_reliability: DateReliability = DateReliability.UNKNOWN
    fetched_ts: float = field(default_factory=time.time)
    width: int | None = None
    height: int | None = None
    #: dimensiones nativas del original en la fuente (si difieren de las del archivo local)
    native_width: int | None = None
    native_height: int | None = None
    bytes: int | None = None
    mime: str | None = None
    resolution_class: ResolutionClass = ResolutionClass.ORIGINAL
    derived_from_url: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    heading_deg: float | None = None  # None = desconocido (no cero ficticio)
    intrinsics: dict[str, Any] | None = None
    environment: Environment | None = None
    description: str | None = None
    quality: float = 0.0
    usable: bool = True
    reject_reason: str | None = None
    phash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "title": self.title,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "source": self.source,
            "place_id": self.place_id,
            "source_page": self.source_page,
            "source_file_url": self.source_file_url,
            "sha1_source": self.sha1_source,
            "author": self.author,
            "license": self.license,
            "license_url": self.license_url,
            "attribution": self.attribution,
            "capture_date": self.capture_date,
            "upload_date": self.upload_date,
            "date_reliability": str(self.date_reliability),
            "fetched_ts": self.fetched_ts,
            "width": self.width,
            "height": self.height,
            "native_width": self.native_width,
            "native_height": self.native_height,
            "bytes": self.bytes,
            "mime": self.mime,
            "resolution_class": str(self.resolution_class),
            "derived_from_url": self.derived_from_url,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "heading_deg": self.heading_deg,
            "intrinsics": self.intrinsics,
            "environment": str(self.environment) if self.environment else None,
            "description": self.description,
            "quality": self.quality,
            "usable": self.usable,
            "reject_reason": self.reject_reason,
            "phash": self.phash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Reference:
        return cls(
            ref_id=str(d["ref_id"]),
            title=str(d["title"]),
            file_path=str(d["file_path"]),
            sha256=str(d["sha256"]),
            source=str(d["source"]),
            place_id=d.get("place_id"),
            source_page=d.get("source_page"),
            source_file_url=d.get("source_file_url"),
            sha1_source=d.get("sha1_source"),
            author=d.get("author"),
            license=str(d.get("license", "UNKNOWN")),
            license_url=d.get("license_url"),
            attribution=d.get("attribution"),
            capture_date=d.get("capture_date"),
            upload_date=d.get("upload_date"),
            date_reliability=DateReliability(d.get("date_reliability", "unknown")),
            fetched_ts=float(d.get("fetched_ts", time.time())),
            width=d.get("width"),
            height=d.get("height"),
            native_width=d.get("native_width"),
            native_height=d.get("native_height"),
            bytes=d.get("bytes"),
            mime=d.get("mime"),
            resolution_class=ResolutionClass(d.get("resolution_class", "original")),
            derived_from_url=d.get("derived_from_url"),
            gps_lat=d.get("gps_lat"),
            gps_lon=d.get("gps_lon"),
            heading_deg=d.get("heading_deg"),
            intrinsics=d.get("intrinsics"),
            environment=Environment(d["environment"]) if d.get("environment") else None,
            description=d.get("description"),
            quality=float(d.get("quality", 0.0)),
            usable=bool(d.get("usable", True)),
            reject_reason=d.get("reject_reason"),
            phash=d.get("phash"),
        )


@dataclass(slots=True)
class View:
    """Una vista utilizable de un lugar (posición de captura + cámara estimada)."""

    view_id: str
    place_id: str
    ref_id: str
    label: str = ""
    lat: float | None = None
    lon: float | None = None
    heading_deg: float | None = None
    fov_deg: float | None = None
    calib_state: CalibState = CalibState.UNCALIBRATED
    method: str = "gps_metadata"  # cómo se estimó la cámara
    usable: bool = True
    notes: str | None = None
    created_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_id": self.view_id,
            "place_id": self.place_id,
            "ref_id": self.ref_id,
            "label": self.label,
            "lat": self.lat,
            "lon": self.lon,
            "heading_deg": self.heading_deg,
            "fov_deg": self.fov_deg,
            "calib_state": str(self.calib_state),
            "method": self.method,
            "usable": self.usable,
            "notes": self.notes,
            "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> View:
        return cls(
            view_id=str(d["view_id"]),
            place_id=str(d["place_id"]),
            ref_id=str(d["ref_id"]),
            label=str(d.get("label", "")),
            lat=d.get("lat"),
            lon=d.get("lon"),
            heading_deg=d.get("heading_deg"),
            fov_deg=d.get("fov_deg"),
            calib_state=CalibState(d.get("calib_state", "uncalibrated")),
            method=str(d.get("method", "gps_metadata")),
            usable=bool(d.get("usable", True)),
            notes=d.get("notes"),
            created_ts=float(d.get("created_ts", time.time())),
        )


@dataclass(slots=True)
class Scenario:
    """Un escenario versionado de un lugar (fondo fotográfico, panorama, reconstrucción)."""

    scenario_id: str
    place_id: str
    kind: ScenarioKind
    version: int = 1
    refs: list[str] = field(default_factory=list)
    geometry_path: str | None = None
    transforms: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    status: ScenarioStatus = ScenarioStatus.DRAFT
    created_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "place_id": self.place_id,
            "kind": str(self.kind),
            "version": self.version,
            "refs": list(self.refs),
            "geometry_path": self.geometry_path,
            "transforms": dict(self.transforms),
            "capabilities": dict(self.capabilities),
            "status": str(self.status),
            "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Scenario:
        return cls(
            scenario_id=str(d["scenario_id"]),
            place_id=str(d["place_id"]),
            kind=ScenarioKind(d["kind"]),
            version=int(d.get("version", 1)),
            refs=list(d.get("refs", [])),
            geometry_path=d.get("geometry_path"),
            transforms=dict(d.get("transforms", {})),
            capabilities=dict(d.get("capabilities", {})),
            status=ScenarioStatus(d.get("status", "draft")),
            created_ts=float(d.get("created_ts", time.time())),
        )


@dataclass(slots=True)
class Coverage:
    """Cobertura medida de un lugar (qué se observó y con qué antigüedad)."""

    place_id: str
    views_observed: int = 0
    #: sectores/tramos respaldados por referencias (geometría aproximada en texto/JSON).
    backed: dict[str, Any] = field(default_factory=dict)
    #: superficies/perspectivas no observadas.
    unobserved: dict[str, Any] = field(default_factory=dict)
    evidence_newest: str | None = None
    evidence_oldest: str | None = None
    #: base medible de la confianza (conteos reales), no una cifra inventada.
    confidence_basis: str = ""
    updated_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "place_id": self.place_id,
            "views_observed": self.views_observed,
            "backed": dict(self.backed),
            "unobserved": dict(self.unobserved),
            "evidence_newest": self.evidence_newest,
            "evidence_oldest": self.evidence_oldest,
            "confidence_basis": self.confidence_basis,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Coverage:
        return cls(
            place_id=str(d["place_id"]),
            views_observed=int(d.get("views_observed", 0)),
            backed=dict(d.get("backed", {})),
            unobserved=dict(d.get("unobserved", {})),
            evidence_newest=d.get("evidence_newest"),
            evidence_oldest=d.get("evidence_oldest"),
            confidence_basis=str(d.get("confidence_basis", "")),
            updated_ts=float(d.get("updated_ts", time.time())),
        )


@dataclass(slots=True)
class ProductionRecord:
    """Registro serializable de una producción hecha con el atlas (recuperable)."""

    prod_id: str
    atlas_version: str
    place_id: str | None = None
    view_id: str | None = None
    scenario_id: str | None = None
    camera: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    created_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prod_id": self.prod_id,
            "atlas_version": self.atlas_version,
            "place_id": self.place_id,
            "view_id": self.view_id,
            "scenario_id": self.scenario_id,
            "camera": dict(self.camera),
            "params": dict(self.params),
            "result": dict(self.result),
            "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProductionRecord:
        return cls(
            prod_id=str(d["prod_id"]),
            atlas_version=str(d.get("atlas_version", "")),
            place_id=d.get("place_id"),
            view_id=d.get("view_id"),
            scenario_id=d.get("scenario_id"),
            camera=dict(d.get("camera", {})),
            params=dict(d.get("params", {})),
            result=dict(d.get("result", {})),
            created_ts=float(d.get("created_ts", time.time())),
        )


@dataclass(slots=True)
class SourceRecord:
    """Procedencia y licencia de una fuente de datos del atlas."""

    source_id: str
    name: str
    kind: str  # osm_extract | wms_layer | media_api | ...
    url: str
    license: str
    license_url: str | None = None
    terms: str | None = None
    extract_date: str | None = None
    sha256: str | None = None
    fetched_ts: float = field(default_factory=time.time)
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "license": self.license,
            "license_url": self.license_url,
            "terms": self.terms,
            "extract_date": self.extract_date,
            "sha256": self.sha256,
            "fetched_ts": self.fetched_ts,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SourceRecord:
        return cls(
            source_id=str(d["source_id"]),
            name=str(d["name"]),
            kind=str(d["kind"]),
            url=str(d["url"]),
            license=str(d.get("license", "UNKNOWN")),
            license_url=d.get("license_url"),
            terms=d.get("terms"),
            extract_date=d.get("extract_date"),
            sha256=d.get("sha256"),
            fetched_ts=float(d.get("fetched_ts", time.time())),
            notes=d.get("notes"),
        )
