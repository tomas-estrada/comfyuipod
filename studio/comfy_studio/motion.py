"""Movimiento humano, rigging y retargeting (plan Ultra, W13 / A15).

W13 pide movimiento humano/coreografías/transferencia de actuación con
esqueletos/mappings/bind/scale declarados, retarget que conserva raíz y tiempos,
contactos de pies/manos y joint limits, y export/import reejecutable. El
invariante (A15): la transferencia distingue identidad/actuación/máscara/cámara,
y la imposibilidad de usar un motor (Isaac Gym/ReMoGen) no se oculta bajo un
comando nominalmente integrado.

Este módulo es el CONTRATO de esqueleto + un retargeting determinista en CPU:

- `Skeleton`: jerarquía de huesos con bind pose y escala declarada.
- `MotionClip`: keyframes por hueso (ángulos) con timebase explícito.
- `retarget`: mapea un clip de un esqueleto fuente a uno destino conservando la
  trayectoria de la RAÍZ y los TIEMPOS (fps); valida que el mapping cubre los
  huesos y que los ángulos respetan los límites del destino.
- `validate_contacts`: comprueba que los contactos declarados (pies/manos) caen
  en frames existentes.

Determinista y reejecutable: export→import→retarget da el mismo resultado. No
ejecuta un motor de síntesis de movimiento real (eso es GPU/build, A29); si un
motor requerido no está, se declara, no se finge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


class MotionError(ValueError):
    """Contrato de movimiento/retarget inválido."""


Vec3 = tuple[float, float, float]
MAX_SKELETON_BONES = 256
MAX_CLIP_FRAMES = 600
MAX_FRAME_BONES = 256
MAX_IDENTIFIER_LENGTH = 128


@dataclass(slots=True)
class ContactEvent:
    """Contacto observado con posición y superficie para validar deslizamiento."""

    effector: str
    frame: int
    position: Vec3
    surface: str


@dataclass(slots=True)
class Bone:
    """Un hueso del esqueleto con su padre y límites de ángulo."""

    name: str
    parent: str | None
    min_deg: float = -180.0
    max_deg: float = 180.0


@dataclass(slots=True)
class Skeleton:
    """Esqueleto: jerarquía de huesos + escala, raíz y bind pose declaradas."""

    name: str
    bones: list[Bone]
    root: str
    scale: float = 1.0
    #: bind pose en grados por hueso; sin declaración la bind efectiva es 0.0.
    bind: dict[str, float] = field(default_factory=dict)
    body_class: str = "humanoid"
    identity_id: str | None = None

    def bone_names(self) -> set[str]:
        return {b.name for b in self.bones}

    def validate(self) -> None:
        names = self.bone_names()
        if len(self.bones) > MAX_SKELETON_BONES:
            raise MotionError(f"skeleton exceeds {MAX_SKELETON_BONES} bones")
        if any(not b.name or len(b.name) > MAX_IDENTIFIER_LENGTH for b in self.bones):
            raise MotionError(f"bone names must be 1..{MAX_IDENTIFIER_LENGTH} characters")
        if len(self.name) > MAX_IDENTIFIER_LENGTH or len(self.body_class) > 64:
            raise MotionError("skeleton name or body_class exceeds its length limit")
        if self.identity_id is not None and len(self.identity_id) > MAX_IDENTIFIER_LENGTH:
            raise MotionError("skeleton identity_id exceeds its length limit")
        if len(names) != len(self.bones):
            raise MotionError("nombre de hueso duplicado")
        if self.root not in names:
            raise MotionError(f"raíz {self.root!r} no está entre los huesos")
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise MotionError("scale del esqueleto debe ser finito y > 0")
        if not self.body_class.strip():
            raise MotionError("body_class del esqueleto no puede estar vacío")
        bones = {bone.name: bone for bone in self.bones}
        for bone, degrees in self.bind.items():
            if bone not in names:
                raise MotionError(
                    f"bind pose: hueso inexistente {bone!r} (debe declarar huesos existentes)"
                )
            if not math.isfinite(degrees):
                raise MotionError(f"bind pose: hueso {bone!r} con grados no finitos")
            declared_bone = bones[bone]
            if not declared_bone.min_deg <= degrees <= declared_bone.max_deg:
                raise MotionError(
                    f"bind pose: hueso {bone!r} con {degrees}° fuera de sus límites "
                    f"[{declared_bone.min_deg}°, {declared_bone.max_deg}°]"
                )
        if bones[self.root].parent is not None:
            raise MotionError("la raíz del esqueleto no puede tener padre")
        for b in self.bones:
            if b.parent is not None and b.parent not in names:
                raise MotionError(f"hueso {b.name}: padre inexistente {b.parent!r}")
            if b.min_deg > b.max_deg:
                raise MotionError(f"hueso {b.name}: min_deg > max_deg")
            if not math.isfinite(b.min_deg) or not math.isfinite(b.max_deg):
                raise MotionError(f"hueso {b.name}: límites no finitos")
            seen: set[str] = set()
            parent = b.parent
            while parent is not None:
                if parent in seen or parent == b.name:
                    raise MotionError(f"ciclo en jerarquía de huesos cerca de {b.name!r}")
                seen.add(parent)
                parent = bones[parent].parent


@dataclass(slots=True)
class MotionClip:
    """Clip de movimiento: ángulos por hueso y frame, con timebase explícito."""

    name: str
    fps: int
    #: frames[i][bone_name] = ángulo en grados.
    frames: list[dict[str, float]] = field(default_factory=list)
    #: trayectoria de la raíz por frame (x,y,z).
    root_track: list[tuple[float, float, float]] = field(default_factory=list)
    #: contactos declarados: {"foot_L": [frame,...], ...}.
    contacts: dict[str, list[int]] = field(default_factory=dict)
    #: Contactos espaciales reabribles; separados del índice legacy `contacts`.
    contact_events: list[ContactEvent] = field(default_factory=list)
    #: Canales distintos: identidad no se infiere del movimiento ni la cámara.
    identity_id: str | None = None
    action_id: str | None = None
    mask_asset_id: str | None = None
    camera_context: dict[str, Any] = field(default_factory=dict)

    def validate(self, skeleton: Skeleton | None = None) -> None:
        """Comprueba timebase, pistas y que cada canal pertenezca al esqueleto."""
        if self.fps <= 0:
            raise MotionError("fps debe ser > 0")
        if not self.name or len(self.name) > MAX_IDENTIFIER_LENGTH:
            raise MotionError(f"clip name must be 1..{MAX_IDENTIFIER_LENGTH} characters")
        if len(self.frames) > MAX_CLIP_FRAMES:
            raise MotionError(f"clip exceeds {MAX_CLIP_FRAMES} frames")
        if any(len(frame) > MAX_FRAME_BONES for frame in self.frames):
            raise MotionError(f"a frame exceeds {MAX_FRAME_BONES} bone keys")
        if self.root_track and len(self.root_track) != len(self.frames):
            raise MotionError("root_track debe tener un vector por frame")
        for index, frame in enumerate(self.frames):
            for bone, angle in frame.items():
                if not bone or not math.isfinite(angle):
                    raise MotionError(f"frame {index}: hueso o ángulo inválido")
        if any(
            len(position) != 3 or any(not math.isfinite(value) for value in position)
            for position in self.root_track
        ):
            raise MotionError("root_track contiene posición 3D inválida")
        if skeleton is not None:
            skeleton.validate()
            unknown = sorted(
                {bone for frame in self.frames for bone in frame} - skeleton.bone_names()
            )
            if unknown:
                raise MotionError(
                    f"clip contiene huesos ausentes del esqueleto: {', '.join(unknown)}"
                )
        validate_contacts(self)

    def duration_s(self) -> float:
        return len(self.frames) / self.fps if self.fps else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fps": self.fps,
            "frame_count": len(self.frames),
            "duration_s": round(self.duration_s(), 4),
            "frames": [dict(f) for f in self.frames],
            "root_track": [list(p) for p in self.root_track],
            "contacts": {k: list(v) for k, v in self.contacts.items()},
            "contact_events": [
                {
                    "effector": item.effector,
                    "frame": item.frame,
                    "position": list(item.position),
                    "surface": item.surface,
                }
                for item in self.contact_events
            ],
            "identity_id": self.identity_id,
            "action_id": self.action_id,
            "mask_asset_id": self.mask_asset_id,
            "camera_context": dict(self.camera_context),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MotionClip:
        clip = cls(
            name=data["name"],
            fps=int(data["fps"]),
            frames=[dict(f) for f in data.get("frames", [])],
            root_track=[tuple(p) for p in data.get("root_track", [])],
            contacts={k: list(v) for k, v in data.get("contacts", {}).items()},
            contact_events=[
                ContactEvent(
                    effector=item["effector"],
                    frame=int(item["frame"]),
                    position=tuple(item["position"]),
                    surface=item["surface"],
                )
                for item in data.get("contact_events", [])
            ],
            identity_id=data.get("identity_id"),
            action_id=data.get("action_id"),
            mask_asset_id=data.get("mask_asset_id"),
            camera_context=dict(data.get("camera_context", {})),
        )
        clip.validate()
        return clip


def validate_contacts(
    clip: MotionClip,
    *,
    surfaces: dict[str, tuple[Vec3, float]] | None = None,
    position_tolerance_m: float = 0.02,
    slide_tolerance_m: float = 0.03,
) -> None:
    """Valida rango temporal, plano de contacto y deslizamiento (A15).

    ``surfaces`` mapea nombre → (normal unitaria, offset) en la convención
    ``dot(normal, point) == offset``. Sin superficies se validan frame y datos
    espaciales; con ellas también se mide penetración/separación y slide.
    """
    if clip.fps <= 0:
        raise MotionError("fps debe ser > 0")
    if (
        not math.isfinite(position_tolerance_m)
        or not math.isfinite(slide_tolerance_m)
        or position_tolerance_m < 0
        or slide_tolerance_m < 0
    ):
        raise MotionError("tolerancias de contacto deben ser finitas y >= 0")
    if surfaces is not None:
        for surface, plane in surfaces.items():
            if not isinstance(plane, tuple) or len(plane) != 2:
                raise MotionError(f"superficie {surface}: se esperaba (normal, offset)")
            normal, offset = plane
            if len(normal) != 3 or any(not math.isfinite(value) for value in (*normal, offset)):
                raise MotionError(f"superficie {surface}: plano 3D inválido")
            if not math.isclose(
                math.sqrt(sum(value * value for value in normal)),
                1.0,
                rel_tol=0,
                abs_tol=1e-6,
            ):
                raise MotionError(f"superficie {surface}: normal debe ser unitaria")
    n = len(clip.frames)
    for part, frames in clip.contacts.items():
        for f in frames:
            if not (0 <= f < n):
                raise MotionError(f"contacto {part}: frame {f} fuera de rango [0,{n})")
    by_effector: dict[str, list[ContactEvent]] = {}
    for event in clip.contact_events:
        if not 0 <= event.frame < n:
            raise MotionError(
                f"contacto {event.effector}: frame {event.frame} fuera de rango [0,{n})"
            )
        if not event.effector or not event.surface:
            raise MotionError("contacto espacial requiere effector y surface")
        if len(event.position) != 3 or any(not math.isfinite(v) for v in event.position):
            raise MotionError(f"contacto {event.effector}: posición 3D inválida")
        by_effector.setdefault(event.effector, []).append(event)
        if surfaces is not None:
            if event.surface not in surfaces:
                raise MotionError(
                    f"contacto {event.effector}: superficie desconocida {event.surface!r}"
                )
            normal, offset = surfaces[event.surface]
            length = math.sqrt(sum(v * v for v in normal))
            if not math.isclose(length, 1.0, rel_tol=0, abs_tol=1e-6):
                raise MotionError(f"superficie {event.surface}: normal debe ser unitaria")
            signed_distance = sum(normal[i] * event.position[i] for i in range(3)) - offset
            if abs(signed_distance) > position_tolerance_m:
                raise MotionError(
                    f"contacto {event.effector}: se separa {signed_distance:.3f}m "
                    f"de {event.surface}"
                )
    for effector, events in by_effector.items():
        ordered = sorted(events, key=lambda item: item.frame)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.frame == left.frame:
                raise MotionError(f"contacto {effector}: frame duplicado")
    if surfaces is not None:
        for effector, events in by_effector.items():
            ordered = sorted(events, key=lambda item: item.frame)
            for left, right in zip(ordered, ordered[1:], strict=False):
                distance = math.sqrt(
                    sum((right.position[i] - left.position[i]) ** 2 for i in range(3))
                )
                if distance > slide_tolerance_m:
                    raise MotionError(
                        f"contacto {effector}: deslizamiento {distance:.3f}m excede "
                        f"{slide_tolerance_m:.3f}m"
                    )


def retarget(
    clip: MotionClip,
    source: Skeleton,
    target: Skeleton,
    mapping: dict[str, str],
) -> MotionClip:
    """Retargetea un clip del esqueleto fuente al destino (A15).

    - `mapping`: {hueso_fuente: hueso_destino}. Debe cubrir los huesos usados.
    - conserva la trayectoria de la RAÍZ (escalada por target.scale/source.scale)
      y los TIEMPOS (mismo fps, mismos frames).
    - los ángulos se interpretan como OFFSET respecto a la bind pose del hueso
      fuente (ángulo − bind), se suman a la bind pose del hueso destino y se
      recortan a los límites del DESTINO — un clip en la bind del fuente
      produce la bind del destino, y sin bind declarada (0.0 por hueso) el
      resultado es el retarget absoluto previo.
    """
    source.validate()
    target.validate()
    if len(mapping) > MAX_SKELETON_BONES:
        raise MotionError(f"mapping exceeds {MAX_SKELETON_BONES} bone pairs")
    if any(
        not isinstance(src, str)
        or not isinstance(dst, str)
        or len(src) > MAX_IDENTIFIER_LENGTH
        or len(dst) > MAX_IDENTIFIER_LENGTH
        for src, dst in mapping.items()
    ):
        raise MotionError(f"mapping identifiers must be <= {MAX_IDENTIFIER_LENGTH} characters")
    if source.body_class != target.body_class:
        raise MotionError(
            f"incompatible body class: {source.body_class!r} to {target.body_class!r}"
        )
    clip.validate(source)
    src_names = source.bone_names()
    tgt_names = target.bone_names()
    # el mapping debe referir huesos reales en ambos lados.
    for s, t in mapping.items():
        if s not in src_names:
            raise MotionError(f"mapping: hueso fuente inexistente {s!r}")
        if t not in tgt_names:
            raise MotionError(f"mapping: hueso destino inexistente {t!r}")

    tgt_limits = {b.name: (b.min_deg, b.max_deg) for b in target.bones}
    src_bind = {bone: value for bone, value in source.bind.items()}
    tgt_bind = {bone: value for bone, value in target.bind.items()}
    unmapped = sorted({bone for frame in clip.frames for bone in frame} - mapping.keys())
    if unmapped:
        raise MotionError(f"mapping incompleto; huesos usados sin destino: {', '.join(unmapped)}")
    scale_ratio = (target.scale / source.scale) if source.scale else 1.0

    out = MotionClip(name=f"{clip.name}@{target.name}", fps=clip.fps)
    for frame in clip.frames:
        new_frame: dict[str, float] = {}
        for src_bone, angle in frame.items():
            tgt_bone = mapping.get(src_bone)
            if tgt_bone is None:  # defensive; completeness is checked above.
                raise MotionError(f"mapping incompleto para hueso {src_bone!r}")
            lo, hi = tgt_limits[tgt_bone]
            # El ángulo es un OFFSET respecto a la bind del fuente: se resta la
            # bind de origen, se suma la bind del destino y se recorta al rango
            # físico del destino. Sin bind declarada (0.0) es el retarget
            # absoluto previo.
            offset = angle - src_bind.get(src_bone, 0.0)
            new_frame[tgt_bone] = max(lo, min(hi, tgt_bind.get(tgt_bone, 0.0) + offset))
        out.frames.append(new_frame)
    # la raíz se conserva, escalada por la razón de escalas de los esqueletos.
    out.root_track = [
        (x * scale_ratio, y * scale_ratio, z * scale_ratio) for (x, y, z) in clip.root_track
    ]
    # los contactos se conservan (mismos frames; el tiempo no cambia).
    out.contacts = {k: list(v) for k, v in clip.contacts.items()}
    out.contact_events = [
        ContactEvent(
            effector=event.effector,
            frame=event.frame,
            position=tuple(value * scale_ratio for value in event.position),
            surface=event.surface,
        )
        for event in clip.contact_events
    ]
    out.identity_id = clip.identity_id
    out.action_id = clip.action_id
    out.mask_asset_id = clip.mask_asset_id
    out.camera_context = dict(clip.camera_context)
    validate_contacts(out)
    return out


def assert_engine_available(engine_name: str, available: bool) -> None:
    """Un motor de movimiento ausente se declara, no se finge (A15).

    «La imposibilidad de usar Isaac Gym o ReMoGen no se oculta bajo un comando
    nominalmente integrado.»
    """
    if not available:
        raise MotionError(
            f"motor de movimiento '{engine_name}' no disponible: no se finge integración"
        )


__all__ = [
    "Bone",
    "ContactEvent",
    "MotionClip",
    "MotionError",
    "Skeleton",
    "assert_engine_available",
    "retarget",
    "validate_contacts",
]
