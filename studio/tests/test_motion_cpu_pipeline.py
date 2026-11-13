from __future__ import annotations

import asyncio
import shutil

import pytest

from comfy_studio.blender_motion import BlenderRigError, BlenderRigSpec, run_blender_rig
from comfy_studio.motion import Bone, MotionClip, MotionError, Skeleton
from comfy_studio.motion_cpu import compose_actions, recover_motion, transfer_acting


def _rig(kind: str = "humanoid", identity: str = "actor-a") -> Skeleton:
    rig = Skeleton(
        name=identity,
        root="root",
        bones=[Bone("root", None), Bone("foot_L", "root")],
        body_class=kind,
    )
    rig.identity_id = identity
    return rig


def test_recovery_removes_camera_translation_and_scale_and_measures_foot_contacts():
    clip = recover_motion(
        name="tracked",
        fps=24,
        frames=[
            {"root": (5.0, 0.0, 0.0), "foot_L": (5.0, 0.0, 0.0)},
            {"root": (7.0, 0.0, 0.0), "foot_L": (6.02, 0.0, 0.0)},
        ],
        camera_track=[(4.0, 0.0, 0.0), (5.0, 0.0, 0.0)],
        subject_scale=2.0,
    )
    assert clip.root_track == [(0.5, 0.0, 0.0), (1.0, 0.0, 0.0)]
    assert clip.contacts["foot_L"] == [0, 1]
    assert clip.camera_context["compensation"] == "translation_rotation_and_scale"


def test_recovery_compensates_camera_yaw_before_subject_scale():
    clip = recover_motion(
        name="turning_camera",
        fps=24,
        frames=[{"root": (0.0, 1.0, 0.0)}],
        camera_track=[(0.0, 0.0, 0.0)],
        camera_yaw_radians=[1.5707963267948966],
        subject_scale=1.0,
    )
    assert clip.root_track[0][0] == pytest.approx(1.0)
    assert clip.root_track[0][1] == pytest.approx(0.0)


def test_choreography_requires_three_beat_aligned_actions_and_contacts():
    actions = [
        MotionClip(name=name, fps=24, frames=[{"root": 0.0}] * 24, contacts={"foot_L": [12]})
        for name in ("step", "turn", "pose")
    ]
    result = compose_actions(actions, beat_frames=[0, 24, 48])
    assert result.action_id == "step+turn+pose"
    assert len(result.frames) == 72
    assert result.contacts["foot_L"] == [12, 36, 60]
    with pytest.raises(MotionError, match="three"):
        compose_actions(actions[:2], beat_frames=[0, 24])


def test_acting_transfer_preserves_identity_and_rejects_human_to_nonhuman():
    source = _rig()
    target = _rig(identity="actor-a")
    clip = MotionClip(
        name="dance", fps=24, frames=[{"foot_L": 12}], identity_id="actor-a", mask_asset_id="mask-1"
    )
    transferred = transfer_acting(clip, source, target, {"foot_L": "foot_L"})
    assert transferred.identity_id == "actor-a"
    assert transferred.mask_asset_id == "mask-1"
    with pytest.raises(MotionError, match="body class"):
        transfer_acting(clip, source, _rig("vehicle", "car-1"), {"foot_L": "foot_L"})


def test_blender_cpu_exports_and_reimports_editable_skin_and_animation(tmp_path):
    if not shutil.which("blender"):
        pytest.skip("headless Blender unavailable")
    rig = _rig()
    rig.bind = {"foot_L": 5.0}
    clip = MotionClip(
        name="extreme",
        fps=12,
        frames=[{"foot_L": 0.0}, {"foot_L": 90.0}],
        root_track=[(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)],
    )
    result = asyncio.run(run_blender_rig(BlenderRigSpec(rig, clip, tmp_path)))
    assert result.ok, result.error
    assert result.glb_path and result.glb_path.read_bytes()[:4] == b"glTF"
    assert result.report["reimport_bones"] == 2
    assert result.report["reimport_skinned_meshes"] == 1
    assert result.report["extreme_pose_deformation_max_m"] > 0
    assert isinstance(result.report["self_intersection_triangle_pairs"], int)
    assert result.report["bind_pose_applied"] is True


def test_blender_rig_rejects_pose_outside_declared_joint_limits(tmp_path):
    rig = _rig()
    clip = MotionClip(name="invalid", fps=12, frames=[{"foot_L": 181.0}])
    with pytest.raises(BlenderRigError, match="joint limit"):
        BlenderRigSpec(rig, clip, tmp_path).validate()
