"""Dirección cinematográfica por restricciones (plan Ultra, W11 / A13).

W11 pide cámaras/reconstrucción/ensayo de rodaje (C05/C07/C08/C20): trayectoria,
focal, duración, legibilidad, identidad, oclusiones y continuidad, con animatics
y un optimizador de focal/path. El invariante (A13): «datos insuficientes
impiden cámara factual; no rellenar fachadas como observadas», y «colisiones/
visibilidad/composición verificadas antes de stage caro».

Este módulo implementa el CONTRATO de cámara y las comprobaciones geométricas
baratas que deben pasar ANTES de gastar GPU en el render:

- `CameraPlan`: trayectoria de cámara (keyframes de posición/target), focal y
  lente, duración.
- `check_composition`: colisión de la cámara con la geometría, visibilidad del
  sujeto (no ocluido), y encuadre dentro de límites. Fail-closed: si algo no se
  puede comprobar por falta de datos, no se da por bueno.
- `assert_factual_camera`: una cámara marcada factual exige que el activo que
  encuadra sea OBSERVADO (no una fachada inferida). Reutiliza la procedencia de
  `assets3d` (W10): A13 «una foto 2.5D no acredita exactitud de fachada oculta».

Puro CPU y determinista (geometría vectorial simple). El animatic visible real y
la traducción neural son render (GPU/A29); aquí se valida el plan antes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from comfy_studio.assets3d import Asset3D, Provenance

Vec3 = tuple[float, float, float]


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(a: Vec3) -> float:
    return math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)


def _dist(a: Vec3, b: Vec3) -> float:
    return _norm(_sub(a, b))


class CameraError(ValueError):
    """Plan de cámara inválido o no verificable."""


@dataclass(slots=True)
class CameraKeyframe:
    """Un keyframe de la trayectoria de cámara."""

    t: float  # tiempo normalizado 0..1
    position: Vec3
    target: Vec3


@dataclass(slots=True)
class CameraPlan:
    """Plan de cámara: trayectoria + lente, verificable antes del render."""

    shot_id: str
    keyframes: list[CameraKeyframe] = field(default_factory=list)
    focal_mm: float = 35.0
    sensor_mm: float = 36.0  # full-frame por defecto
    duration_s: float = 5.0
    #: ¿se afirma que esta cámara es factual (lugar real exacto)?
    factual: bool = False

    def fov_deg(self) -> float:
        """Campo de visión horizontal según focal y sensor."""
        return math.degrees(2 * math.atan(self.sensor_mm / (2 * self.focal_mm)))

    def validate(self) -> None:
        if len(self.keyframes) < 1:
            raise CameraError(f"{self.shot_id}: la trayectoria necesita >=1 keyframe")
        ts = [k.t for k in self.keyframes]
        if ts != sorted(ts):
            raise CameraError(f"{self.shot_id}: keyframes fuera de orden temporal")
        if self.focal_mm <= 0 or self.sensor_mm <= 0:
            raise CameraError(f"{self.shot_id}: focal/sensor deben ser > 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "focal_mm": self.focal_mm,
            "fov_deg": round(self.fov_deg(), 2),
            "duration_s": self.duration_s,
            "factual": self.factual,
            "keyframes": [
                {"t": k.t, "position": list(k.position), "target": list(k.target)}
                for k in self.keyframes
            ],
        }


@dataclass(slots=True)
class CompositionReport:
    """Resultado de las comprobaciones baratas previas al stage caro."""

    ok: bool
    collisions: list[str] = field(default_factory=list)
    occlusions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "collisions": list(self.collisions),
            "occlusions": list(self.occlusions),
            "warnings": list(self.warnings),
        }


def check_composition(
    plan: CameraPlan,
    *,
    subject_position: Vec3,
    subject_radius: float,
    min_distance: float = 0.5,
    obstacles: list[tuple[Vec3, float]] | None = None,
) -> CompositionReport:
    """Comprueba colisiones, visibilidad y encuadre ANTES del render (A13).

    - colisión: ningún keyframe de cámara entra dentro del sujeto ni de un
      obstáculo (esfera posición+radio).
    - visibilidad: el sujeto está dentro del FOV desde cada keyframe.
    - fail-closed: si no hay keyframes, no se da por bueno.
    """
    plan.validate()
    report = CompositionReport(ok=True)
    obstacles = obstacles or []
    half_fov = math.radians(plan.fov_deg() / 2)

    for kf in plan.keyframes:
        # colisión con el sujeto.
        if _dist(kf.position, subject_position) < (subject_radius + min_distance):
            report.collisions.append(f"t={kf.t}: cámara dentro de la zona del sujeto")
        # colisión con obstáculos.
        for i, (obs_pos, obs_r) in enumerate(obstacles):
            if _dist(kf.position, obs_pos) < (obs_r + min_distance):
                report.collisions.append(f"t={kf.t}: cámara colisiona con obstáculo {i}")
        # visibilidad: el sujeto debe caer dentro del FOV (ángulo entre el eje de
        # vista y la dirección al sujeto <= medio FOV).
        view_dir = _sub(kf.target, kf.position)
        to_subj = _sub(subject_position, kf.position)
        vn, sn = _norm(view_dir), _norm(to_subj)
        if vn == 0 or sn == 0:
            report.warnings.append(f"t={kf.t}: dirección de vista degenerada")
            continue
        cos_ang = (
            view_dir[0] * to_subj[0] + view_dir[1] * to_subj[1] + view_dir[2] * to_subj[2]
        ) / (vn * sn)
        cos_ang = max(-1.0, min(1.0, cos_ang))
        if math.acos(cos_ang) > half_fov:
            report.occlusions.append(f"t={kf.t}: sujeto fuera del encuadre (FOV)")

        # An obstacle sphere between the camera and the subject blocks the
        # sightline even when the camera itself does not collide with it.
        segment_sq = sum(value * value for value in to_subj)
        for i, (obs_pos, obs_r) in enumerate(obstacles):
            if obs_r <= 0:
                continue
            to_obstacle = _sub(obs_pos, kf.position)
            fraction = sum(to_obstacle[axis] * to_subj[axis] for axis in range(3)) / segment_sq
            if not 0.0 < fraction < 1.0:
                continue
            closest = tuple(kf.position[axis] + fraction * to_subj[axis] for axis in range(3))
            if _dist(obs_pos, closest) < obs_r:
                report.occlusions.append(f"t={kf.t}: sujeto ocluido por obstáculo {i}")

    report.ok = not report.collisions and not report.occlusions
    return report


def assert_factual_camera(plan: CameraPlan, asset: Asset3D, part_id: str) -> None:
    """Una cámara factual exige que lo que encuadra sea OBSERVADO (A13).

    Datos insuficientes (una parte inferida, una fachada no observada) impiden
    afirmar una cámara factual del lugar real: se lanza en vez de rellenar.
    """
    if not plan.factual:
        return  # cámara no factual (mundo ficticio): sin restricción
    part = next((p for p in asset.parts if p.part_id == part_id), None)
    if part is None:
        raise CameraError(f"cámara factual sobre parte inexistente {part_id}")
    if part.provenance is not Provenance.OBSERVED:
        raise CameraError(
            f"cámara factual sobre parte {part_id} INFERIDA: datos insuficientes "
            "para una vista factual (no se rellena fachada como observada)"
        )


__all__ = [
    "CameraError",
    "CameraKeyframe",
    "CameraPlan",
    "CompositionReport",
    "assert_factual_camera",
    "check_composition",
    "interpolate_camera",
    "interpolate_camera_at",
    "interpolate_camera_keys",
    "sample_camera_path",
    "check_animatic_preflight",
    "render_animatic",
    "render_animatic_preview",
]


# --- Animatic (C05/C08/A13): delegación lazy para evitar import circular ----


def interpolate_camera(*args, **kwargs):
    from .animatic import interpolate_camera as _fn

    return _fn(*args, **kwargs)


def interpolate_camera_at(*args, **kwargs):
    from .animatic import interpolate_camera_at as _fn

    return _fn(*args, **kwargs)


def interpolate_camera_keys(*args, **kwargs):
    from .animatic import interpolate_camera_keys as _fn

    return _fn(*args, **kwargs)


def sample_camera_path(*args, **kwargs):
    from .animatic import sample_camera_path as _fn

    return _fn(*args, **kwargs)


def check_animatic_preflight(*args, **kwargs):
    from .animatic import check_animatic_preflight as _fn

    return _fn(*args, **kwargs)


def render_animatic(*args, **kwargs):
    from .animatic import render_animatic as _fn

    return _fn(*args, **kwargs)


def render_animatic_preview(*args, **kwargs):
    from .animatic import render_animatic_preview as _fn

    return _fn(*args, **kwargs)
