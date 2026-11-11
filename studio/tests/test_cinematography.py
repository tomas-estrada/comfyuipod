"""Dirección cinematográfica por restricciones (plan Ultra, W11 / A13).

Comprobaciones geométricas baratas antes del stage caro; cámara factual exige
datos observados. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.assets3d import Asset3D, Material, Part, Provenance
from comfy_studio.cinematography import (
    CameraError,
    CameraKeyframe,
    CameraPlan,
    assert_factual_camera,
    check_composition,
)


def _plan(**kw) -> CameraPlan:
    defaults = {
        "shot_id": "s1",
        "keyframes": [
            CameraKeyframe(t=0.0, position=(0.0, 1.0, 5.0), target=(0.0, 1.0, 0.0)),
            CameraKeyframe(t=1.0, position=(2.0, 1.0, 5.0), target=(0.0, 1.0, 0.0)),
        ],
        "focal_mm": 35.0,
    }
    defaults.update(kw)
    return CameraPlan(**defaults)


def test_fov_from_focal() -> None:
    wide = _plan(focal_mm=18.0).fov_deg()
    tele = _plan(focal_mm=85.0).fov_deg()
    assert wide > tele  # gran angular tiene mayor FOV que teleobjetivo


def test_validate_rejects_out_of_order_keyframes() -> None:
    p = _plan(
        keyframes=[
            CameraKeyframe(t=1.0, position=(0, 0, 5), target=(0, 0, 0)),
            CameraKeyframe(t=0.0, position=(0, 0, 5), target=(0, 0, 0)),
        ]
    )
    with pytest.raises(CameraError, match="fuera de orden"):
        p.validate()


def test_composition_ok_when_subject_in_frame_and_no_collision() -> None:
    p = _plan()
    rep = check_composition(p, subject_position=(0.0, 1.0, 0.0), subject_radius=0.5)
    assert rep.ok is True
    assert not rep.collisions and not rep.occlusions


def test_composition_detects_camera_collision_with_subject() -> None:
    p = _plan(keyframes=[CameraKeyframe(t=0.0, position=(0.0, 1.0, 0.1), target=(0.0, 1.0, 0.0))])
    rep = check_composition(p, subject_position=(0.0, 1.0, 0.0), subject_radius=0.5)
    assert rep.ok is False
    assert rep.collisions


def test_composition_detects_subject_out_of_frame() -> None:
    # Cámara mirando lejos del sujeto (target opuesto): sujeto fuera del FOV.
    p = _plan(
        keyframes=[CameraKeyframe(t=0.0, position=(0.0, 1.0, 5.0), target=(0.0, 1.0, 10.0))],
        focal_mm=85.0,  # FOV estrecho
    )
    rep = check_composition(p, subject_position=(0.0, 1.0, 0.0), subject_radius=0.5)
    assert rep.ok is False
    assert rep.occlusions


def test_composition_detects_obstacle_collision() -> None:
    p = _plan(keyframes=[CameraKeyframe(t=0.0, position=(0.0, 1.0, 5.0), target=(0.0, 1.0, 0.0))])
    rep = check_composition(
        p,
        subject_position=(0.0, 1.0, 0.0),
        subject_radius=0.5,
        obstacles=[((0.0, 1.0, 5.0), 0.3)],  # obstáculo justo en la cámara
    )
    assert rep.ok is False
    assert any("obstáculo" in c for c in rep.collisions)


def test_composition_detects_obstacle_occluding_subject() -> None:
    p = _plan(keyframes=[CameraKeyframe(t=0.0, position=(0.0, 1.0, 5.0), target=(0.0, 1.0, 0.0))])
    rep = check_composition(
        p,
        subject_position=(0.0, 1.0, 0.0),
        subject_radius=0.5,
        obstacles=[((0.0, 1.0, 2.5), 0.4)],
    )
    assert rep.ok is False
    assert any("sujeto ocluido" in item for item in rep.occlusions)


def test_obstacle_behind_subject_does_not_occlude_subject() -> None:
    p = _plan(keyframes=[CameraKeyframe(t=0.0, position=(0.0, 1.0, 5.0), target=(0.0, 1.0, 0.0))])
    rep = check_composition(
        p,
        subject_position=(0.0, 1.0, 0.0),
        subject_radius=0.5,
        obstacles=[((0.0, 1.0, -2.0), 0.4)],
    )
    assert rep.ok is True
    assert not rep.occlusions


def _asset_with(front_prov: Provenance, back_prov: Provenance) -> Asset3D:
    mats = [Material(name="m", provenance=Provenance.OBSERVED)]
    parts = [
        Part(part_id="front", name="fachada", material="m", provenance=front_prov),
        Part(part_id="back", name="cara_oculta", material="m", provenance=back_prov),
    ]
    return Asset3D(asset_id="edificio", name="e", materials=mats, parts=parts)


def test_factual_camera_allows_observed_part() -> None:
    p = _plan(factual=True)
    asset = _asset_with(Provenance.OBSERVED, Provenance.INFERRED)
    assert_factual_camera(p, asset, "front")  # no lanza


def test_factual_camera_rejects_inferred_part() -> None:
    """A13: datos insuficientes impiden una cámara factual."""
    p = _plan(factual=True)
    asset = _asset_with(Provenance.OBSERVED, Provenance.INFERRED)
    with pytest.raises(CameraError, match="INFERIDA"):
        assert_factual_camera(p, asset, "back")


def test_non_factual_camera_has_no_provenance_restriction() -> None:
    """Un mundo ficticio (no factual) puede usar geometría inferida."""
    p = _plan(factual=False)
    asset = _asset_with(Provenance.OBSERVED, Provenance.INFERRED)
    assert_factual_camera(p, asset, "back")  # no lanza: no es factual
