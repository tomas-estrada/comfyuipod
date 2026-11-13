"""Memoria espacial, multishot y reconstrucción 4D (plan Ultra, W17 / A19).

W17 pide memoria espacial entre planos y reconstrucción 4D con objetos/poses/
cámaras temporales editables. El invariante (A19): una secuencia A→B→A conserva
las anclas y registra el **drift**; ninguna imagen generada se promociona a
evidencia del atlas; se respetan límites de escena/duración/contexto.

Este módulo implementa el CONTRATO:

- `Anchor`: un ancla espacial persistente (id estable, posición) con procedencia
  observed/inferred (reutiliza la distinción de W09/W10).
- `SpatialMemory`: el registro de anclas entre planos; `visit` registra la
  posición observada de un ancla en un plano, y `drift` mide cuánto se movió un
  ancla entre dos visitas (A→B→A debe conservar el ancla con drift medido).
- `assert_not_atlas_evidence`: una posición de un ancla INFERIDA (generada) no se
  promociona a evidencia factual del atlas (A19).

Geometría vectorial simple; la reconstrucción 4D neural es GPU/A29. Verifica la
conservación de anclas, el drift y la honestidad de procedencia.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Vec3 = tuple[float, float, float]

# Cota dura de la escena temporal: acotada por diseño (A19), no ilimitada.
MAX_SCENE_DURATION_S = 120.0
MAX_SCENE_FPS = 240
MAX_SCENE_ENTITIES = 16
MAX_SCENE_KEYS_PER_ENTITY = 256


class Provenance(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    SYNTHETIC = "synthetic"


class SpatialError(ValueError):
    """Contrato de memoria espacial inválido."""


def _dist(a: Vec3, b: Vec3) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


@dataclass(slots=True)
class Anchor:
    """Un ancla espacial persistente entre planos."""

    anchor_id: str
    provenance: Provenance = Provenance.OBSERVED


@dataclass(slots=True)
class Visit:
    """La posición observada de un ancla en un plano concreto."""

    shot_id: str
    position: Vec3


@dataclass(slots=True)
class SpatialMemory:
    """Memoria de anclas entre planos; mide drift y conserva anclas (A19)."""

    anchors: dict[str, Anchor] = field(default_factory=dict)
    #: visitas por ancla, en orden temporal.
    visits: dict[str, list[Visit]] = field(default_factory=dict)
    #: límite de contexto por receta (nº de planos recordados).
    max_shots: int = 32

    def register(self, anchor: Anchor) -> None:
        if not anchor.anchor_id.strip():
            raise SpatialError("anchor_id vacío")
        if not isinstance(anchor.provenance, Provenance):
            raise SpatialError(f"procedencia inválida para ancla {anchor.anchor_id}")
        existing = self.anchors.get(anchor.anchor_id)
        if existing is not None and existing.provenance is not anchor.provenance:
            raise SpatialError(f"procedencia inmutable para ancla {anchor.anchor_id}")
        self.anchors[anchor.anchor_id] = anchor
        self.visits.setdefault(anchor.anchor_id, [])

    def visit(self, anchor_id: str, shot_id: str, position: Vec3) -> None:
        if anchor_id not in self.anchors:
            raise SpatialError(f"ancla no registrada: {anchor_id}")
        if not shot_id.strip() or len(position) != 3 or any(
            not math.isfinite(value) for value in position
        ):
            raise SpatialError("visita requiere shot_id y posición 3D finita")
        visits = self.visits[anchor_id]
        previous = next((visit for visit in visits if visit.shot_id == shot_id), None)
        if previous is not None:
            if previous.position == position:
                return  # Retry of a persisted request is idempotent.
            raise SpatialError(f"visita {shot_id!r} ya existe con otra posición")
        if len({v.shot_id for v in visits} | {shot_id}) > self.max_shots:
            raise SpatialError(
                f"límite de contexto excedido ({self.max_shots} planos) para {anchor_id}"
            )
        visits.append(Visit(shot_id=shot_id, position=position))

    def drift(self, anchor_id: str) -> float:
        """Distancia entre la primera y la última visita del ancla (A→B→A)."""
        visits = self.visits.get(anchor_id, [])
        if len(visits) < 2:
            return 0.0
        return _dist(visits[0].position, visits[-1].position)

    def round_trip_drift(self, anchor_id: str) -> dict[str, Any]:
        """Para una secuencia A→B→A: ¿el ancla volvió a su sitio? Drift medido."""
        visits = self.visits.get(anchor_id, [])
        if len(visits) < 3:
            raise SpatialError(
                f"round-trip necesita >=3 visitas (A->B->A) para {anchor_id}"
            )
        return {
            "anchor_id": anchor_id,
            "shots": [v.shot_id for v in visits],
            "drift": round(self.drift(anchor_id), 6),
            "returned": self.drift(anchor_id) < 1e-6,
        }

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "spatial-memory/1",
            "max_shots": self.max_shots,
            "anchors": {
                aid: {"provenance": str(a.provenance)} for aid, a in self.anchors.items()
            },
            "visits": {
                aid: [
                    {"shot_id": visit.shot_id, "position": list(visit.position)}
                    for visit in visits
                ]
                for aid, visits in self.visits.items()
            },
            "drift": {aid: round(self.drift(aid), 6) for aid in self.anchors},
        }

    def validate(self) -> None:
        if self.max_shots <= 0:
            raise SpatialError("max_shots debe ser > 0")
        if set(self.anchors) != set(self.visits):
            raise SpatialError("cada ancla debe tener su lista de visitas")
        for anchor_id, anchor in self.anchors.items():
            if not anchor_id or anchor.anchor_id != anchor_id:
                raise SpatialError("anchor_id inconsistente")
            if len({visit.shot_id for visit in self.visits[anchor_id]}) > self.max_shots:
                raise SpatialError(f"límite de contexto excedido para {anchor_id}")
            for visit in self.visits[anchor_id]:
                if not visit.shot_id or len(visit.position) != 3 or any(
                    not math.isfinite(value) for value in visit.position
                ):
                    raise SpatialError(f"visita inválida para ancla {anchor_id}")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpatialMemory:
        if data.get("schema") not in (None, "spatial-memory/1"):
            raise SpatialError(f"schema desconocido: {data.get('schema')!r}")
        try:
            memory = cls(max_shots=int(data.get("max_shots", 32)))
            for anchor_id, value in data.get("anchors", {}).items():
                memory.register(
                    Anchor(anchor_id=anchor_id, provenance=Provenance(value["provenance"]))
                )
            for anchor_id, visits in data.get("visits", {}).items():
                if anchor_id not in memory.anchors:
                    raise SpatialError(f"visitas para ancla no registrada: {anchor_id}")
                for visit in visits:
                    memory.visit(
                        anchor_id,
                        str(visit["shot_id"]),
                        tuple(float(value) for value in visit["position"]),
                    )
            memory.validate()
            return memory
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SpatialError):
                raise
            raise SpatialError(f"memoria espacial serializada inválida: {exc}") from exc


def assert_not_atlas_evidence(memory: SpatialMemory, anchor_id: str) -> None:
    """Solo anclas observadas pueden promocionarse a evidencia factual (A19)."""
    anchor = memory.anchors.get(anchor_id)
    if anchor is None:
        raise SpatialError(f"ancla inexistente: {anchor_id}")
    if anchor.provenance is not Provenance.OBSERVED:
        raise SpatialError(
            f"ancla {anchor_id} {anchor.provenance.value.upper()} (no observada): "
            "no es evidencia del atlas"
        )


__all__ = [
    "Anchor",
    "CameraEntity",
    "CameraKey",
    "MAX_SCENE_DURATION_S",
    "MAX_SCENE_FPS",
    "ObjectEntity",
    "ObjectKey",
    "PoseEntity",
    "PoseKey",
    "Provenance",
    "SourceRef",
    "SpatialError",
    "SpatialMemory",
    "TemporalScene",
    "Timebase",
    "Visit",
    "assert_not_atlas_evidence",
    "synthetic_round_trip_fixture",
]


# ---------------------------------------------------------------------------
# Escena temporal editable (Task 1, C06/C17 / A19)
#
# Representación EDITABLE indexada por tiempo: objetos (posición), poses
# (ángulos por hueso) y cámaras (posición/orientación/fov), con timebase
# estricta, serialización determinista, reframe/retime y procedencia de fuente.
# NO es reconstrucción 4D neuronal: es una representación de keyframes CPU,
# acotada (duración, nº de entidades, nº de claves) y persistida por proyecto.
# ---------------------------------------------------------------------------


def _r(x: float) -> float:
    """Redondeo determinista a 6 decimales para serializar (+0.0 normaliza -0.0)."""
    return round(float(x), 6) + 0.0


def _same(a: float, b: float) -> bool:
    """Igualdad idempotente tolerante al redondeo de serialización."""
    return _r(a) == _r(b)


def _rotated(v: Vec3, deg: Vec3) -> Vec3:
    """Rota (x,y,z) por ángulos XYZ en grados (orden intrínseco Z·Y·X aplicado Rx→Ry→Rz)."""
    rx, ry, rz = (math.radians(a) for a in deg)
    x, y, z = v
    # Rx
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    # Ry
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    # Rz
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return (x, y, z)


@dataclass(slots=True)
class Timebase:
    """Timebase estricta: fps entero acotado (1..240), sin bools ni floats."""

    fps: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if isinstance(self.fps, bool) or not isinstance(self.fps, int):
            raise SpatialError("fps debe ser un entero")
        if not 1 <= self.fps <= MAX_SCENE_FPS:
            raise SpatialError(f"fps debe estar entre 1 y {MAX_SCENE_FPS}")


@dataclass(slots=True)
class SourceRef:
    """Procedencia de una fuente observada/sintética del proyecto."""

    kind: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise SpatialError("kind de fuente requerido")
        if self.sha256 is not None:
            value = self.sha256.lower()
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise SpatialError("sha256 de fuente debe ser hexadecimal de 64 caracteres")
            self.sha256 = value


@dataclass(slots=True)
class ObjectKey:
    t: float
    position: Vec3


@dataclass(slots=True)
class PoseKey:
    t: float
    angles: dict[str, float]


@dataclass(slots=True)
class CameraKey:
    t: float
    position: Vec3
    orientation_deg: Vec3
    fov_deg: float = 50.0


@dataclass(slots=True)
class ObjectEntity:
    entity_id: str
    provenance: Provenance
    source_id: str | None
    keys: list[ObjectKey]


@dataclass(slots=True)
class PoseEntity:
    entity_id: str
    provenance: Provenance
    source_id: str | None
    keys: list[PoseKey]


@dataclass(slots=True)
class CameraEntity:
    entity_id: str
    provenance: Provenance
    source_id: str | None
    keys: list[CameraKey]


def _checked_time(t: float, duration_s: float) -> float:
    if isinstance(t, bool):
        raise SpatialError("tiempo de clave debe ser finito")
    try:
        value = float(t)
    except (TypeError, ValueError) as exc:
        raise SpatialError("tiempo de clave debe ser finito") from exc
    if not math.isfinite(value):
        raise SpatialError("tiempo de clave debe ser finito")
    if value < 0.0 or value > duration_s:
        raise SpatialError(
            f"tiempo de clave fuera de rango: {value} fuera de [0, {duration_s}]"
        )
    return value


def _checked_query_time(t: float) -> float:
    if isinstance(t, bool):
        raise SpatialError("tiempo de consulta debe ser finito")
    try:
        value = float(t)
    except (TypeError, ValueError) as exc:
        raise SpatialError("tiempo de consulta debe ser finito") from exc
    if not math.isfinite(value):
        raise SpatialError("tiempo de consulta debe ser finito")
    return value


def _checked_vec3(v: Vec3, label: str = "posición") -> Vec3:
    try:
        values = tuple(float(x) for x in v)
    except (TypeError, ValueError) as exc:
        raise SpatialError(f"{label} debe ser un vector 3D finito") from exc
    if len(values) != 3 or any(not math.isfinite(x) for x in values):
        raise SpatialError(f"{label} debe ser un vector 3D finito")
    return values  # type: ignore[return-value]


def _interpolate(t: float, times: list[float]) -> tuple[int, float]:
    """Índice del segmento y factor de mezcla para consulta held-out."""
    if t <= times[0]:
        return 0, 0.0
    if t >= times[-1]:
        return len(times) - 1, 0.0
    for index in range(len(times) - 1):
        if times[index] <= t <= times[index + 1]:
            span = times[index + 1] - times[index]
            return index, (t - times[index]) / span if span > 0 else 0.0
    return len(times) - 1, 0.0


@dataclass(slots=True)
class TemporalScene:
    """Escena temporal editable y acotada por proyecto (objetos/poses/cámaras)."""

    timebase: Timebase
    duration_s: float
    objects: dict[str, ObjectEntity] = field(default_factory=dict)
    poses: dict[str, PoseEntity] = field(default_factory=dict)
    cameras: dict[str, CameraEntity] = field(default_factory=dict)
    sources: dict[str, SourceRef] = field(default_factory=dict)
    max_entities: int = 16
    max_keys_per_entity: int = 256

    # --- construcción / edición -------------------------------------------

    def add_source(self, source_id: str, ref: SourceRef) -> None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise SpatialError("source_id de fuente requerido")
        if not isinstance(ref, SourceRef):
            raise SpatialError("fuente inválida")
        existing = self.sources.get(source_id)
        if existing is not None and (existing.kind, existing.sha256) != (
            ref.kind,
            ref.sha256,
        ):
            raise SpatialError(
                f"conflicto: fuente {source_id!r} ya registrada con otro contenido"
            )
        self.sources[source_id] = ref

    def _entity_ids(self) -> set[str]:
        return set(self.objects) | set(self.poses) | set(self.cameras)

    def _check_new_entity(self, entity_id: str, provenance: Provenance) -> None:
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise SpatialError("entity_id requerido")
        if entity_id in self._entity_ids():
            raise SpatialError(f"entity_id duplicado entre kinds: {entity_id!r}")
        if not isinstance(provenance, Provenance):
            raise SpatialError(f"procedencia inválida para {entity_id!r}")
        if len(self._entity_ids()) + 1 > self.max_entities:
            raise SpatialError(
                f"límite de entidades excedido ({self.max_entities})"
            )

    def _check_source(self, source_id: str | None) -> None:
        if source_id is None:
            return
        if source_id not in self.sources:
            raise SpatialError(f"fuente no registrada: {source_id!r}")

    def _check_source_provenance(
        self, source_id: str | None, provenance: Provenance
    ) -> None:
        if not isinstance(provenance, Provenance):
            raise SpatialError("procedencia inválida para entidad")
        self._check_source(source_id)
        if (
            source_id is not None
            and self.sources[source_id].kind == "synthetic"
            and provenance is not Provenance.SYNTHETIC
        ):
            raise SpatialError(
                f"procedencia {provenance.value!r} no coincide con fuente sintética "
                f"{source_id!r}"
            )

    @staticmethod
    def _insert_key(keys: list, key: Any, max_keys: int, entity_id: str) -> None:
        """Inserción idempotente; conflicto en la misma marca temporal falla."""
        existing = next((k for k in keys if _same(k.t, key.t)), None)
        if existing is not None:
            if existing == key:
                return  # Reintento de una petición persistida es idempotente.
            raise SpatialError(
                f"conflicto: clave en t={_r(key.t)} ya existe para {entity_id!r}"
            )
        if len(keys) + 1 > max_keys:
            raise SpatialError(
                f"límite de claves excedido ({max_keys}) para {entity_id!r}"
            )
        keys.append(key)
        keys.sort(key=lambda k: k.t)

    def set_object_key(
        self,
        entity_id: str,
        t: float,
        position: Vec3,
        *,
        provenance: Provenance = Provenance.OBSERVED,
        source_id: str | None = None,
    ) -> None:
        t = _checked_time(t, self.duration_s)
        position = _checked_vec3(position)
        self._check_source_provenance(source_id, provenance)
        entity = self.objects.get(entity_id)
        if entity is None:
            self._check_new_entity(entity_id, provenance)
            entity = ObjectEntity(
                entity_id=entity_id, provenance=provenance, source_id=source_id, keys=[]
            )
            self.objects[entity_id] = entity
        elif entity.provenance is not provenance or entity.source_id != source_id:
            raise SpatialError(f"procedencia inmutable para entidad {entity_id!r}")
        self._insert_key(
            entity.keys,
            ObjectKey(t=t, position=position),
            self.max_keys_per_entity,
            entity_id,
        )

    def set_pose_key(
        self,
        entity_id: str,
        t: float,
        angles: dict[str, float],
        *,
        provenance: Provenance = Provenance.OBSERVED,
        source_id: str | None = None,
    ) -> None:
        t = _checked_time(t, self.duration_s)
        if not isinstance(angles, dict) or not angles:
            raise SpatialError("pose requiere ángulos por hueso")
        clean = {}
        for bone, angle in angles.items():
            try:
                value = float(angle)
            except (TypeError, ValueError) as exc:
                raise SpatialError(f"ángulo no finito para hueso {bone!r}") from exc
            if not isinstance(bone, str) or not bone.strip() or isinstance(
                angle, bool
            ) or not math.isfinite(value):
                raise SpatialError(f"ángulo no finito para hueso {bone!r}")
            clean[bone] = value
        self._check_source_provenance(source_id, provenance)
        entity = self.poses.get(entity_id)
        if entity is None:
            self._check_new_entity(entity_id, provenance)
            entity = PoseEntity(
                entity_id=entity_id, provenance=provenance, source_id=source_id, keys=[]
            )
            self.poses[entity_id] = entity
        elif entity.provenance is not provenance or entity.source_id != source_id:
            raise SpatialError(f"procedencia inmutable para entidad {entity_id!r}")
        self._insert_key(
            entity.keys,
            PoseKey(t=t, angles=clean),
            self.max_keys_per_entity,
            entity_id,
        )

    def set_camera_key(
        self,
        entity_id: str,
        t: float,
        position: Vec3,
        orientation_deg: Vec3,
        fov_deg: float = 50.0,
        *,
        provenance: Provenance = Provenance.OBSERVED,
        source_id: str | None = None,
    ) -> None:
        t = _checked_time(t, self.duration_s)
        position = _checked_vec3(position)
        orientation_deg = _checked_vec3(orientation_deg, "orientación")
        try:
            fov_value = float(fov_deg)
        except (TypeError, ValueError) as exc:
            raise SpatialError("fov_deg debe estar entre 1 y 180") from exc
        invalid_fov = (
            isinstance(fov_deg, bool)
            or not math.isfinite(fov_value)
            or not 1.0 <= fov_value <= 180.0
        )
        if invalid_fov:
            raise SpatialError("fov_deg debe estar entre 1 y 180")
        self._check_source_provenance(source_id, provenance)
        entity = self.cameras.get(entity_id)
        if entity is None:
            self._check_new_entity(entity_id, provenance)
            entity = CameraEntity(
                entity_id=entity_id, provenance=provenance, source_id=source_id, keys=[]
            )
            self.cameras[entity_id] = entity
        elif entity.provenance is not provenance or entity.source_id != source_id:
            raise SpatialError(f"procedencia inmutable para entidad {entity_id!r}")
        self._insert_key(
            entity.keys,
            CameraKey(
                t=t,
                position=position,
                orientation_deg=orientation_deg,
                fov_deg=fov_value,
            ),
            self.max_keys_per_entity,
            entity_id,
        )

    # --- consultas held-out -------------------------------------------------

    def _require(self, mapping: dict, entity_id: str) -> Any:
        entity = mapping.get(entity_id)
        if entity is None:
            raise SpatialError(f"entidad no registrada: {entity_id!r}")
        if not entity.keys:
            raise SpatialError(f"entidad sin claves: {entity_id!r}")
        return entity

    def sample_object(self, entity_id: str, t: float) -> Vec3:
        entity = self._require(self.objects, entity_id)
        t = _checked_query_time(t)
        times = [k.t for k in entity.keys]
        index, mix = _interpolate(t, times)
        a = entity.keys[index].position
        b = entity.keys[min(index + 1, len(entity.keys) - 1)].position
        return tuple(a[i] + (b[i] - a[i]) * mix for i in range(3))  # type: ignore[return-value]

    def sample_pose(self, entity_id: str, t: float) -> dict[str, float]:
        entity = self._require(self.poses, entity_id)
        t = _checked_query_time(t)
        times = [k.t for k in entity.keys]
        index, mix = _interpolate(t, times)
        a, b = entity.keys[index].angles, entity.keys[min(index + 1, len(entity.keys) - 1)].angles
        blended: dict[str, float] = {}
        for bone in sorted(set(a) | set(b)):
            if bone in a and bone in b:
                blended[bone] = a[bone] + (b[bone] - a[bone]) * mix
            else:
                blended[bone] = a.get(bone, b.get(bone, 0.0))
        return blended

    def sample_camera(self, entity_id: str, t: float) -> CameraKey:
        entity = self._require(self.cameras, entity_id)
        t = _checked_query_time(t)
        times = [k.t for k in entity.keys]
        index, mix = _interpolate(t, times)
        a, b = entity.keys[index], entity.keys[min(index + 1, len(entity.keys) - 1)]
        position = tuple(a.position[i] + (b.position[i] - a.position[i]) * mix for i in range(3))
        orientation = tuple(
            a.orientation_deg[i] + (b.orientation_deg[i] - a.orientation_deg[i]) * mix
            for i in range(3)
        )
        fov = a.fov_deg + (b.fov_deg - a.fov_deg) * mix
        return CameraKey(
            t=float(t),
            position=position,  # type: ignore[arg-type]
            orientation_deg=orientation,  # type: ignore[arg-type]
            fov_deg=fov,
        )

    def object_round_trip(self, entity_id: str) -> dict[str, Any]:
        """A19: A→B→A sobre la trayectoria del objeto; drift medido."""
        entity = self._require(self.objects, entity_id)
        if len(entity.keys) < 3:
            raise SpatialError(f"round-trip necesita >=3 claves (A->B->A) para {entity_id!r}")
        first, last = entity.keys[0].position, entity.keys[-1].position
        drift = math.sqrt(sum((first[i] - last[i]) ** 2 for i in range(3)))
        return {
            "entity_id": entity_id,
            "times": [_r(k.t) for k in entity.keys],
            "drift": round(drift, 6),
            "returned": drift < 1e-6,
        }

    # --- reframe / retime ---------------------------------------------------

    def reframe(
        self, camera_id: str, offset: Vec3, rotate_deg: Vec3 = (0.0, 0.0, 0.0)
    ) -> dict[str, Any]:
        """Reencuadre determinista: rota y traslada la cámara; objetos intactos."""
        entity = self._require(self.cameras, camera_id)
        offset = _checked_vec3(offset, "offset")
        rotate_deg = _checked_vec3(rotate_deg, "rotación")
        for key in entity.keys:
            rotated = _rotated(key.position, rotate_deg)
            key.position = tuple(rotated[i] + offset[i] for i in range(3))  # type: ignore[assignment]
            key.orientation_deg = tuple(
                key.orientation_deg[i] + rotate_deg[i] for i in range(3)
            )  # type: ignore[assignment]
        self.validate()
        return {
            "camera_id": camera_id,
            "keys_reframed": len(entity.keys),
        }

    def retime(self, scale: float, *, new_fps: int | None = None) -> dict[str, Any]:
        """Retemporización determinista: escala todos los tiempos por `scale` > 0."""
        if isinstance(scale, bool) or not math.isfinite(float(scale)) or float(scale) <= 0:
            raise SpatialError("escala de retime debe ser finita y > 0")
        scale = float(scale)
        if self.duration_s * scale > MAX_SCENE_DURATION_S:
            raise SpatialError("retime excede la duración máxima de la escena")
        for group in (self.objects, self.poses, self.cameras):
            for entity in group.values():
                for key in entity.keys:
                    key.t = _r(key.t * scale)
        self.duration_s = _r(self.duration_s * scale)
        if new_fps is not None:
            self.timebase = Timebase(fps=new_fps)
        self.validate()
        return {"scale": scale, "timebase_fps": self.timebase.fps}

    def set_timebase(self, fps: int) -> None:
        self.timebase = Timebase(fps=fps)

    # --- validación / serialización determinista ----------------------------

    def validate(self) -> None:
        self.timebase.validate()
        if isinstance(self.duration_s, bool) or not math.isfinite(
            float(self.duration_s)
        ) or not 0.0 < float(self.duration_s) <= MAX_SCENE_DURATION_S:
            raise SpatialError(
                f"duration_s debe estar en (0, {MAX_SCENE_DURATION_S}]"
            )
        if not 1 <= self.max_entities <= MAX_SCENE_ENTITIES:
            raise SpatialError(
                f"max_entities debe estar entre 1 y {MAX_SCENE_ENTITIES}"
            )
        if not 1 <= self.max_keys_per_entity <= MAX_SCENE_KEYS_PER_ENTITY:
            raise SpatialError(
                "max_keys_per_entity debe estar entre 1 y "
                f"{MAX_SCENE_KEYS_PER_ENTITY}"
            )
        if len(self._entity_ids()) > self.max_entities:
            raise SpatialError(f"límite de entidades excedido ({self.max_entities})")
        if len(set(self.sources)) != len(self.sources):
            raise SpatialError("source_id duplicado")
        for source_id, ref in self.sources.items():
            if not source_id.strip():
                raise SpatialError("source_id de fuente vacío")
            ref.validate()
        for mapping in (self.objects, self.poses, self.cameras):
            for entity_id, entity in mapping.items():
                if not isinstance(entity.provenance, Provenance):
                    raise SpatialError(f"procedencia inválida para {entity_id!r}")
                self._check_source_provenance(entity.source_id, entity.provenance)
                if not 0 < len(entity.keys) <= self.max_keys_per_entity:
                    raise SpatialError(
                        f"nº de claves inválido para {entity_id!r}"
                    )
                times = [_checked_time(k.t, self.duration_s) for k in entity.keys]
                if times != sorted(times) or len(set(times)) != len(times):
                    raise SpatialError(
                        f"claves de {entity_id!r} deben tener tiempos únicos y ordenados"
                    )

    def to_dict(self) -> dict[str, Any]:
        self.validate()

        def entity(entity: Any) -> dict[str, Any]:
            return {
                "provenance": str(entity.provenance),
                "source_id": entity.source_id,
                "keys": [
                    {
                        "t": _r(k.t),
                        **(
                            {"position": [_r(v) for v in k.position]}
                            if hasattr(k, "position")
                            else {}
                        ),
                        **(
                            {"angles": {b: _r(a) for b, a in sorted(k.angles.items())}}
                            if hasattr(k, "angles")
                            else {}
                        ),
                        **(
                            {"orientation_deg": [_r(v) for v in k.orientation_deg]}
                            if hasattr(k, "orientation_deg")
                            else {}
                        ),
                        **(
                            {"fov_deg": _r(k.fov_deg)}
                            if hasattr(k, "fov_deg")
                            else {}
                        ),
                    }
                    for k in entity.keys
                ],
            }

        return {
            "schema": "temporal-scene/1",
            "timebase": {"fps": self.timebase.fps},
            "duration_s": _r(self.duration_s),
            "limits": {
                "max_entities": self.max_entities,
                "max_keys_per_entity": self.max_keys_per_entity,
            },
            "sources": {
                sid: {"kind": ref.kind, "sha256": ref.sha256}
                for sid, ref in sorted(self.sources.items())
            },
            "objects": {eid: entity(e) for eid, e in sorted(self.objects.items())},
            "poses": {eid: entity(e) for eid, e in sorted(self.poses.items())},
            "cameras": {eid: entity(e) for eid, e in sorted(self.cameras.items())},
        }

    def dumps(self) -> str:
        """Serialización determinista: claves ordenadas, sin espacios ni NaN."""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemporalScene:
        if not isinstance(data, dict) or data.get("schema") not in (None, "temporal-scene/1"):
            schema = data.get("schema") if isinstance(data, dict) else data
            raise SpatialError(f"schema desconocido: {schema!r}")
        try:
            limits = data.get("limits", {})
            scene = cls(
                timebase=Timebase(fps=int(data["timebase"]["fps"])),
                duration_s=float(data["duration_s"]),
                max_entities=int(limits.get("max_entities", 16)),
                max_keys_per_entity=int(limits.get("max_keys_per_entity", 256)),
            )
            for source_id, ref in data.get("sources", {}).items():
                scene.add_source(
                    str(source_id),
                    SourceRef(kind=str(ref["kind"]), sha256=ref.get("sha256")),
                )
            for kind, setter, keys_field in (
                ("objects", scene.set_object_key, "objects"),
                ("poses", scene.set_pose_key, "poses"),
                ("cameras", scene.set_camera_key, "cameras"),
            ):
                for entity_id, payload in data.get(keys_field, {}).items():
                    entity_provenance = Provenance(payload["provenance"])
                    entity_source = payload.get("source_id")
                    for key in payload["keys"]:
                        t = float(key["t"])
                        if kind == "objects":
                            setter(
                                str(entity_id),
                                t,
                                tuple(float(v) for v in key["position"]),
                                provenance=entity_provenance,
                                source_id=entity_source,
                            )
                        elif kind == "poses":
                            setter(
                                str(entity_id),
                                t,
                                {b: float(a) for b, a in key["angles"].items()},
                                provenance=entity_provenance,
                                source_id=entity_source,
                            )
                        else:
                            setter(
                                str(entity_id),
                                t,
                                tuple(float(v) for v in key["position"]),
                                tuple(float(v) for v in key["orientation_deg"]),
                                float(key.get("fov_deg", 50.0)),
                                provenance=entity_provenance,
                                source_id=entity_source,
                            )
            scene.validate()
            return scene
        except SpatialError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SpatialError(f"escena temporal serializada inválida: {exc}") from exc


def synthetic_round_trip_fixture() -> TemporalScene:
    """Fixture sintético CPU A→B→A con procedencia de fuente determinista.

    Objeto "mesa": A(0,0,0) en t=0 → B(2,0,0) en t=1 → A en t=2 → mantiene A
    hasta t=2.5. Pose y cámara con muestreo equivalente. Fuente sintética con
    sha256 determinista del contenido del fixture.
    """
    source_digest = hashlib.sha256(b"synthetic:a-to-b-to-a:fixture").hexdigest()
    scene = TemporalScene(timebase=Timebase(fps=24), duration_s=3.0)
    scene.add_source("cap-synthetic", SourceRef(kind="synthetic", sha256=source_digest))
    scene.set_object_key(
        "mesa", 0.0, (0.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_object_key(
        "mesa", 1.0, (2.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_object_key(
        "mesa", 2.0, (0.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_object_key(
        "mesa", 2.5, (0.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_pose_key(
        "actor_1", 0.0, {"hip": 0.0, "knee": 0.0},
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_pose_key(
        "actor_1", 1.0, {"hip": 5.0, "knee": 10.0},
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_pose_key(
        "actor_1", 2.0, {"hip": 0.0, "knee": 0.0},
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_camera_key(
        "cam", 0.0, (1.0, 0.0, 2.0), (0.0, 0.0, 0.0), 50.0,
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    scene.set_camera_key(
        "cam", 1.0, (1.0, 2.0, 2.0), (0.0, 0.0, 0.0), 50.0,
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    return scene
