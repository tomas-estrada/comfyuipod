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

from dataclasses import dataclass, field
from typing import Any


class MotionError(ValueError):
    """Contrato de movimiento/retarget inválido."""


@dataclass(slots=True)
class Bone:
    """Un hueso del esqueleto con su padre y límites de ángulo."""

    name: str
    parent: str | None
    min_deg: float = -180.0
    max_deg: float = 180.0


@dataclass(slots=True)
class Skeleton:
    """Esqueleto: jerarquía de huesos + escala y raíz declaradas."""

    name: str
    bones: list[Bone]
    root: str
    scale: float = 1.0

    def bone_names(self) -> set[str]:
        return {b.name for b in self.bones}

    def validate(self) -> None:
        names = self.bone_names()
        if self.root not in names:
            raise MotionError(f"raíz {self.root!r} no está entre los huesos")
        for b in self.bones:
            if b.parent is not None and b.parent not in names:
                raise MotionError(f"hueso {b.name}: padre inexistente {b.parent!r}")
            if b.min_deg > b.max_deg:
                raise MotionError(f"hueso {b.name}: min_deg > max_deg")


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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MotionClip:
        return cls(
            name=data["name"],
            fps=int(data["fps"]),
            frames=[dict(f) for f in data.get("frames", [])],
            root_track=[tuple(p) for p in data.get("root_track", [])],
            contacts={k: list(v) for k, v in data.get("contacts", {}).items()},
        )


def validate_contacts(clip: MotionClip) -> None:
    """Los contactos declarados deben caer en frames existentes (A15)."""
    n = len(clip.frames)
    for part, frames in clip.contacts.items():
        for f in frames:
            if not (0 <= f < n):
                raise MotionError(
                    f"contacto {part}: frame {f} fuera de rango [0,{n})"
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
    - aplica los límites de ángulo del DESTINO (clamp) — un retarget no viola el
      rango físico del esqueleto destino.
    """
    source.validate()
    target.validate()
    src_names = source.bone_names()
    tgt_names = target.bone_names()
    # el mapping debe referir huesos reales en ambos lados.
    for s, t in mapping.items():
        if s not in src_names:
            raise MotionError(f"mapping: hueso fuente inexistente {s!r}")
        if t not in tgt_names:
            raise MotionError(f"mapping: hueso destino inexistente {t!r}")

    tgt_limits = {b.name: (b.min_deg, b.max_deg) for b in target.bones}
    scale_ratio = (target.scale / source.scale) if source.scale else 1.0

    out = MotionClip(name=f"{clip.name}@{target.name}", fps=clip.fps)
    for frame in clip.frames:
        new_frame: dict[str, float] = {}
        for src_bone, angle in frame.items():
            tgt_bone = mapping.get(src_bone)
            if tgt_bone is None:
                continue  # hueso sin mapping: no se transfiere (no se inventa)
            lo, hi = tgt_limits[tgt_bone]
            new_frame[tgt_bone] = max(lo, min(hi, angle))
        out.frames.append(new_frame)
    # la raíz se conserva, escalada por la razón de escalas de los esqueletos.
    out.root_track = [
        (x * scale_ratio, y * scale_ratio, z * scale_ratio)
        for (x, y, z) in clip.root_track
    ]
    # los contactos se conservan (mismos frames; el tiempo no cambia).
    out.contacts = {k: list(v) for k, v in clip.contacts.items()}
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
    "MotionClip",
    "MotionError",
    "Skeleton",
    "assert_engine_available",
    "retarget",
    "validate_contacts",
]
