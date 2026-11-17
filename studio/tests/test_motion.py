"""Movimiento humano, rigging y retargeting (plan Ultra, W13 / A15).

Esqueletos/mappings/bind/scale; retarget conserva raíz y tiempos; contactos y
joint limits; export/import reejecutable. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.motion import (
    Bone,
    ContactEvent,
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


def test_skeleton_rejects_cycles_and_invalid_scale() -> None:
    cyclic = Skeleton(
        name="cycle",
        bones=[
            Bone(name="root", parent=None),
            Bone(name="arm", parent="leg"),
            Bone(name="leg", parent="arm"),
        ],
        root="root",
    )
    with pytest.raises(MotionError, match="ciclo"):
        cyclic.validate()
    with pytest.raises(MotionError, match="scale"):
        Skeleton(name="bad", bones=[Bone("root", None)], root="root", scale=0).validate()


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


def test_retarget_rejects_unmapped_bones_used_in_clip() -> None:
    src, tgt = _skeleton("src"), _skeleton("tgt")
    with pytest.raises(MotionError, match="mapping incompleto.*arm_L"):
        retarget(_clip(), src, tgt, {"spine": "spine"})


def test_retarget_rejects_human_rig_to_nonhuman_rig() -> None:
    human = _skeleton("human")
    vehicle = _skeleton("car")
    vehicle.body_class = "vehicle"
    with pytest.raises(MotionError, match="body class"):
        retarget(_clip(), human, vehicle, {"spine": "spine", "arm_L": "arm_L"})


def test_contacts_must_fall_in_existing_frames() -> None:
    clip = _clip()
    clip.contacts = {"foot_L": [99]}  # frame inexistente
    with pytest.raises(MotionError, match="fuera de rango"):
        validate_contacts(clip)


def test_clip_rejects_unmapped_or_nonfinite_frame_channels() -> None:
    clip = _clip()
    clip.frames[0]["unknown_bone"] = 1.0
    with pytest.raises(MotionError, match="huesos ausentes"):
        clip.validate(_skeleton("source"))
    clip = _clip()
    clip.frames[0]["spine"] = float("nan")
    with pytest.raises(MotionError, match="ángulo inválido"):
        clip.validate(_skeleton("source"))


def test_spatial_contacts_validate_surface_and_sliding() -> None:
    clip = _clip()
    clip.contact_events = [
        ContactEvent("foot_L", 0, (0.0, 0.0, 0.0), "floor"),
        ContactEvent("foot_L", 1, (0.01, 0.0, 0.0), "floor"),
    ]
    validate_contacts(clip, surfaces={"floor": ((0.0, 1.0, 0.0), 0.0)})

    clip.contact_events[1].position = (0.1, 0.0, 0.0)
    with pytest.raises(MotionError, match="deslizamiento"):
        validate_contacts(clip, surfaces={"floor": ((0.0, 1.0, 0.0), 0.0)})


def test_spatial_contact_rejects_penetration_or_unknown_surface() -> None:
    clip = _clip()
    clip.contact_events = [ContactEvent("foot_L", 0, (0.0, -0.1, 0.0), "floor")]
    with pytest.raises(MotionError, match="separa"):
        validate_contacts(clip, surfaces={"floor": ((0.0, 1.0, 0.0), 0.0)})
    clip.contact_events[0].surface = "unknown"
    with pytest.raises(MotionError, match="desconocida"):
        validate_contacts(clip, surfaces={"floor": ((0.0, 1.0, 0.0), 0.0)})


def test_spatial_contact_rejects_malformed_surface_before_distance_math() -> None:
    clip = _clip()
    clip.contact_events = [ContactEvent("foot_L", 0, (0.0, 0.0, 0.0), "floor")]
    with pytest.raises(MotionError, match="plano 3D inválido"):
        validate_contacts(clip, surfaces={"floor": ((0.0, 1.0), 0.0)})


def test_export_import_reexecutable() -> None:
    """export/import reejecutable: roundtrip conserva el clip (A15)."""
    clip = _clip()
    clip.contact_events = [ContactEvent("foot_L", 0, (0.0, 0.0, 0.0), "floor")]
    clip.identity_id = "actor-1"
    clip.action_id = "walk-cycle-1"
    clip.mask_asset_id = "mask-1"
    clip.camera_context = {"camera_id": "cam-1", "view": "wide"}
    restored = MotionClip.from_dict(clip.to_dict())
    assert restored.fps == clip.fps
    assert len(restored.frames) == len(clip.frames)
    assert restored.contacts == clip.contacts
    assert restored.contact_events == clip.contact_events
    assert restored.identity_id == clip.identity_id
    assert restored.action_id == clip.action_id
    assert restored.mask_asset_id == clip.mask_asset_id
    assert restored.camera_context == clip.camera_context
    # Y retargetea igual.
    src, tgt = _skeleton("src"), _skeleton("tgt")
    m = {"spine": "spine", "arm_L": "arm_L"}
    assert retarget(clip, src, tgt, m).to_dict() == retarget(restored, src, tgt, m).to_dict()


def test_retarget_preserves_separate_identity_action_mask_and_camera_context() -> None:
    clip = _clip()
    clip.contact_events = [ContactEvent("foot_L", 0, (0.1, 0.0, 0.0), "floor")]
    clip.identity_id = "actor-1"
    clip.action_id = "jump"
    clip.mask_asset_id = "mask-1"
    clip.camera_context = {"camera_id": "camera-2"}
    out = retarget(
        clip,
        _skeleton("src"),
        _skeleton("target", scale=2.0),
        {"spine": "spine", "arm_L": "arm_L"},
    )
    assert (out.identity_id, out.action_id, out.mask_asset_id) == (
        "actor-1",
        "jump",
        "mask-1",
    )
    assert out.camera_context == {"camera_id": "camera-2"}
    assert out.contact_events[0].position == pytest.approx((0.2, 0.0, 0.0))


def test_missing_engine_is_declared_not_faked() -> None:
    """A15: la imposibilidad de usar un motor no se oculta bajo un comando."""
    assert_engine_available("gvhmr", available=True)  # disponible: no lanza
    with pytest.raises(MotionError, match="no se finge"):
        assert_engine_available("isaac_gym", available=False)
