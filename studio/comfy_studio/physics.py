"""Física y articulación verificable (plan Ultra, W12 / A14).

W12 pide física/vehículos/productos articulados (C10/C15/C18/C19): una maniobra
ejecutada headless en CPU que exporte poses/velocidades/contactos a la escena,
con joints/limits/unidades/timebase y estados vinculados por hash/seed. El
invariante (A14): la fidelidad tras la traducción neural NO se deduce del proxy.

Este módulo implementa el CONTRATO y un **solver cinemático determinista en CPU**
para el ensayo (proxy): integra una trayectoria bajo gravedad y restricciones de
joint con un timebase explícito, exporta poses/velocidades/contactos, y liga el
estado al render por un `state_hash` (seed + parámetros). No es el motor Chrono/
Blender real (eso es build/GPU, A29) — es el proxy verificable que la política
exige antes del stage caro, y que marca honestamente que es un proxy.

Determinista: mismos (seed, params) → mismo `state_hash` y mismas poses.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

GRAVITY = 9.81


class PhysicsError(ValueError):
    """Contrato de simulación inválido."""


@dataclass(slots=True)
class Joint:
    """Un joint articulado con sus límites y unidades explícitas."""

    name: str
    axis: str  # "x" | "y" | "z"
    min_deg: float
    max_deg: float

    def clamp(self, angle_deg: float) -> float:
        """Aplica los límites del joint (no se excede el rango físico)."""
        return max(self.min_deg, min(self.max_deg, angle_deg))

    def within_limits(self, angle_deg: float) -> bool:
        return self.min_deg <= angle_deg <= self.max_deg


@dataclass(slots=True)
class SimSpec:
    """Especificación de una maniobra: timebase, seed y parámetros explícitos."""

    maneuver: str
    seed: int
    fps: int = 24
    duration_s: float = 2.0
    #: altura inicial (m) para una caída; o amplitud de una articulación.
    initial_height_m: float = 10.0
    joints: list[Joint] = field(default_factory=list)

    def validate(self) -> None:
        if self.maneuver not in {
            "free_fall",
            "caida_libre",
            "joint_sweep",
            "joint_cycle",
        }:
            raise PhysicsError(f"maniobra no soportada: {self.maneuver!r}")
        if (
            self.fps <= 0
            or not math.isfinite(self.duration_s)
            or self.duration_s <= 0
            or not math.isfinite(self.initial_height_m)
        ):
            raise PhysicsError("fps y duration_s deben ser > 0")
        if not self.joints and self.maneuver not in {"free_fall", "caida_libre"}:
            raise PhysicsError(f"{self.maneuver} requiere al menos un joint")
        if self.initial_height_m < 0:
            raise PhysicsError("initial_height_m debe ser >= 0")
        names: set[str] = set()
        for joint in self.joints:
            if not joint.name or joint.axis not in {"x", "y", "z"}:
                raise PhysicsError(f"joint inválido: {joint.name!r}/{joint.axis!r}")
            if joint.name in names:
                raise PhysicsError(f"joint duplicado: {joint.name!r}")
            names.add(joint.name)
            if joint.min_deg > joint.max_deg:
                raise PhysicsError(f"joint {joint.name}: min_deg > max_deg")

    def frame_count(self) -> int:
        return max(1, int(round(self.fps * self.duration_s)))

    def state_hash(self) -> str:
        """Hash determinista del estado: liga simulación y render (A14)."""
        payload = {
            "maneuver": self.maneuver,
            "seed": self.seed,
            "fps": self.fps,
            "duration_s": self.duration_s,
            "initial_height_m": self.initial_height_m,
            "joints": [
                {"name": j.name, "axis": j.axis, "min": j.min_deg, "max": j.max_deg}
                for j in self.joints
            ],
        }
        blob = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass(slots=True)
class FrameState:
    """Estado físico en un frame: posición, velocidad, contacto."""

    frame: int
    t: float
    height_m: float
    velocity_mps: float
    in_contact: bool
    joint_angles_deg: dict[str, float] = field(default_factory=dict)
    joint_velocity_deg_s: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class SimResult:
    """Resultado de la simulación proxy: poses/velocidades/contactos."""

    spec_hash: str
    frames: list[FrameState] = field(default_factory=list)
    is_proxy: bool = True  # SIEMPRE proxy; la fidelidad neural no se deduce aquí

    def contact_frame(self) -> int | None:
        """Primer frame en contacto (el objeto tocó el suelo), o None."""
        for f in self.frames:
            if f.in_contact:
                return f.frame
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_hash": self.spec_hash,
            "frame_count": len(self.frames),
            "is_proxy": self.is_proxy,
            "proxy_note": (
                "proxy cinemático CPU; la fidelidad tras render neural NO se deduce de esto (A14)"
            ),
            "contact_frame": self.contact_frame(),
            "frames": [
                {
                    "frame": f.frame,
                    "t": round(f.t, 4),
                    "height_m": round(f.height_m, 4),
                    "velocity_mps": round(f.velocity_mps, 4),
                    "in_contact": f.in_contact,
                    "joint_angles_deg": {k: round(v, 3) for k, v in f.joint_angles_deg.items()},
                    "joint_velocity_deg_s": {
                        k: round(v, 3) for k, v in f.joint_velocity_deg_s.items()
                    },
                }
                for f in self.frames
            ],
        }


def simulate(spec: SimSpec) -> SimResult:
    """Solver cinemático determinista CPU de la maniobra (proxy).

    - caída libre bajo gravedad con contacto en el suelo (altura 0), velocidad
      que se anula al tocar;
    - los joints se mueven dentro de sus límites a lo largo de la duración;
    - timebase explícito (fps); mismo seed/params → mismo resultado.
    """
    spec.validate()
    if spec.maneuver == "joint_cycle" and spec.frame_count() < 2:
        raise PhysicsError("joint_cycle requiere al menos dos frames")

    result = SimResult(spec_hash=spec.state_hash())
    dt = 1.0 / spec.fps
    height = spec.initial_height_m
    velocity = 0.0
    n = spec.frame_count()
    for i in range(n):
        t = i * dt
        if spec.maneuver in {"free_fall", "caida_libre"} and height > 0.0:
            velocity += GRAVITY * dt
            height -= velocity * dt
        contact = spec.maneuver in {"free_fall", "caida_libre"} and height <= 0.0
        if contact:
            height = 0.0
            velocity = 0.0
        # joints: interpolación lineal determinista dentro de límites.
        angles: dict[str, float] = {}
        angular_velocity: dict[str, float] = {}
        frac = i / max(1, n - 1)
        for j in spec.joints:
            span = j.max_deg - j.min_deg
            if spec.maneuver == "joint_cycle":
                phase = math.tau * frac
                raw = (j.min_deg + j.max_deg) / 2 - span / 2 * math.cos(phase)
                angular_velocity[j.name] = span * math.pi / spec.duration_s * math.sin(phase)
            else:
                raw = j.min_deg + frac * span
                angular_velocity[j.name] = (
                    span / spec.duration_s if spec.maneuver == "joint_sweep" else 0.0
                )
            angles[j.name] = j.clamp(raw)
        result.frames.append(
            FrameState(
                frame=i,
                t=t,
                height_m=height,
                velocity_mps=velocity,
                in_contact=contact,
                joint_angles_deg=angles,
                joint_velocity_deg_s=angular_velocity,
            )
        )
    return result


def assert_returns_to_initial(result_a: SimResult, result_b: SimResult | None = None) -> None:
    """Compara corridas o prueba un ciclo real entre primer y último frame (A14)."""
    if result_b is not None:
        if result_a.spec_hash != result_b.spec_hash:
            raise PhysicsError("simulaciones de specs distintas: no comparables")
        if not result_a.frames or not result_b.frames:
            raise PhysicsError("simulación vacía")
        if result_a.to_dict() != result_b.to_dict():
            raise PhysicsError("corridas no deterministas")
        return
    if len(result_a.frames) < 2:
        raise PhysicsError("un ciclo requiere al menos dos frames")
    first, last = result_a.frames[0], result_a.frames[-1]
    if (
        abs(first.height_m - last.height_m) > 1e-6
        or abs(first.velocity_mps - last.velocity_mps) > 1e-6
        or first.in_contact != last.in_contact
        or first.joint_angles_deg.keys() != last.joint_angles_deg.keys()
        or any(
            abs(first.joint_angles_deg[name] - last.joint_angles_deg[name]) > 1e-6
            for name in first.joint_angles_deg
        )
        or any(abs(value) > 1e-6 for value in last.joint_velocity_deg_s.values())
        or any(abs(value) > 1e-6 for value in first.joint_velocity_deg_s.values())
    ):
        raise PhysicsError("estado final no vuelve al estado inicial")


__all__ = [
    "FrameState",
    "Joint",
    "PhysicsError",
    "SimResult",
    "SimSpec",
    "assert_returns_to_initial",
    "simulate",
]
