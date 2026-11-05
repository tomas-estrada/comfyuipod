"""Movimiento humano, rigging y retargeting (plan Ultra, W13 / A15).

Esqueletos/mappings/bind/scale; retarget conserva raíz y tiempos; contactos y
joint limits; export/import reejecutable. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.motion import (
    Bone,
    MotionClip,
    MotionError,
    Skeleton,
    assert_engine_available,
    retarget,
    validate_contacts,
)


def _skeleton(name: str, scale: float = 1.0) -> Skeleton:
    bones = [
        Bone(name="root", parent=None),
        Bone(name="spine", parent="root", min_deg=-45.0, max_deg=45.0),
        Bone(name="arm_L", parent="spine", min_deg=0.0, max_deg=160.0),
    ]
    return Skeleton(name=name, bones=bones, root="root", scale=scale)


def _clip() -> MotionClip:
    return MotionClip(
        name="walk",
        fps=24,
        frames=[{"spine": 10.0, "arm_L": 30.0}, {"spine": 20.0, "arm_L": 200.0}],
        root_track=[(0.0, 0.0, 0.0), (0.1, 0.0, 0.0)],
        contacts={"foot_L": [0]},
    )


def test_skeleton_validates_hierarchy() -> None:
    _skeleton("src").validate()  # no lanza


def test_skeleton_rejects_missing_root() -> None:
    sk = Skeleton(name="x", bones=[Bone(name="a", parent=None)], root="no_existe")
    with pytest.raises(MotionError, match="raíz"):
        sk.validate()


def test_retarget_preserves_timebase_and_frame_count() -> None:
    src, tgt = _skeleton("src"), _skeleton("tgt")
    mapping = {"root": "root", "spine": "spine", "arm_L": "arm_L"}
    out = retarget(_clip(), src, tgt, mapping)
    assert out.fps == 24  # mismo timebase
    assert len(out.frames) == 2  # mismos frames (tiempos conservados)
    assert out.duration_s() == _clip().duration_s()


def test_retarget_clamps_to_target_joint_limits() -> None:
    """El ángulo 200° de arm_L se recorta al máximo 160° del destino (A15)."""
    src, tgt = _skeleton("src"), _skeleton("tgt")
    out = retarget(_clip(), src, tgt, {"spine": "spine", "arm_L": "arm_L"})
    assert out.frames[1]["arm_L"] == 160.0  # clamped


def test_retarget_scales_root_track_by_skeleton_scale() -> None:
    """La raíz se conserva, escalada por la razón de escalas (A15)."""
    src = _skeleton("src", scale=1.0)
    tgt = _skeleton("tgt", scale=2.0)
    out = retarget(_clip(), src, tgt, {"spine": "spine", "arm_L": "arm_L"})
    # root_track escalado x2.
    assert out.root_track[1][0] == pytest.approx(0.2)


def test_retarget_rejects_unknown_mapping_bone() -> None:
    src, tgt = _skeleton("src"), _skeleton("tgt")
    with pytest.raises(MotionError, match="inexistente"):
        retarget(_clip(), src, tgt, {"no_existe": "spine"})


def test_contacts_must_fall_in_existing_frames() -> None:
    clip = _clip()
    clip.contacts = {"foot_L": [99]}  # frame inexistente
    with pytest.raises(MotionError, match="fuera de rango"):
        validate_contacts(clip)


def test_export_import_reexecutable() -> None:
    """export/import reejecutable: roundtrip conserva el clip (A15)."""
    clip = _clip()
    restored = MotionClip.from_dict(clip.to_dict())
    assert restored.fps == clip.fps
    assert len(restored.frames) == len(clip.frames)
    assert restored.contacts == clip.contacts
    # Y retargetea igual.
    src, tgt = _skeleton("src"), _skeleton("tgt")
    m = {"spine": "spine", "arm_L": "arm_L"}
    assert retarget(clip, src, tgt, m).to_dict() == retarget(restored, src, tgt, m).to_dict()


def test_missing_engine_is_declared_not_faked() -> None:
    """A15: la imposibilidad de usar un motor no se oculta bajo un comando."""
    assert_engine_available("gvhmr", available=True)  # disponible: no lanza
    with pytest.raises(MotionError, match="no se finge"):
        assert_engine_available("isaac_gym", available=False)
