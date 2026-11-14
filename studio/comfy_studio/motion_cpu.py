"""Deterministic CPU motion recovery, beat composition, and acting transfer.

Recovery consumes explicit 3D observations; it does not infer pose from pixels.
"""

from __future__ import annotations

import math

from .motion import MAX_CLIP_FRAMES, MAX_FRAME_BONES, MotionClip, MotionError, Skeleton, retarget


def recover_motion(
    *,
    name: str,
    fps: int,
    frames: list[dict[str, tuple[float, float, float] | float]],
    camera_track: list[tuple[float, float, float]],
    subject_scale: float,
    camera_yaw_radians: list[float] | None = None,
    foot_contact_tolerance_m: float = 0.025,
) -> MotionClip:
    """Normalize declared world-space joint observations into subject space.

    Camera translation and yaw are removed before subject scale; stationary
    feet are marked as observed contacts.
    """
    if not frames or len(camera_track) != len(frames):
        raise MotionError("recovery requires one camera sample per observation frame")
    if len(frames) > MAX_CLIP_FRAMES:
        raise MotionError(f"recovery exceeds {MAX_CLIP_FRAMES} frames")
    if any(not isinstance(frame, dict) for frame in frames):
        raise MotionError("each recovery observation must be a JSON object")
    if any(len(frame) > MAX_FRAME_BONES + 1 for frame in frames):
        raise MotionError(f"a recovery frame exceeds {MAX_FRAME_BONES} bone keys")
    yaw_track = [0.0] * len(frames) if camera_yaw_radians is None else camera_yaw_radians
    if len(yaw_track) != len(frames) or any(not math.isfinite(yaw) for yaw in yaw_track):
        raise MotionError("recovery requires one finite camera yaw per observation frame")
    if not math.isfinite(subject_scale) or subject_scale <= 0:
        raise MotionError("subject_scale must be finite and > 0")
    if foot_contact_tolerance_m < 0 or not math.isfinite(foot_contact_tolerance_m):
        raise MotionError("foot contact tolerance must be finite and >= 0")
    root_track: list[tuple[float, float, float]] = []
    rotations: list[dict[str, float]] = []
    effectors: dict[str, list[tuple[int, tuple[float, float, float]]]] = {}
    for index, (sample, camera, yaw) in enumerate(
        zip(frames, camera_track, yaw_track, strict=True)
    ):
        if len(camera) != 3 or any(not math.isfinite(v) for v in camera):
            raise MotionError(f"camera frame {index} must be a finite 3D vector")
        root = sample.get("root")
        if not isinstance(root, (tuple, list)) or len(root) != 3:
            raise MotionError(f"observation frame {index} requires a root 3D position")
        root_track.append(_camera_compensated(root, camera, yaw, subject_scale))
        rotations.append(
            {
                key: float(value)
                for key, value in sample.items()
                if key not in {"root", "foot_L", "foot_R", "hand_L", "hand_R"}
            }
        )
        for effector in ("foot_L", "foot_R"):
            point = sample.get(effector)
            if isinstance(point, (tuple, list)) and len(point) == 3:
                normalized = _camera_compensated(point, camera, yaw, subject_scale)
                effectors.setdefault(effector, []).append((index, normalized))
    contacts: dict[str, list[int]] = {}
    for effector, points in effectors.items():
        for (left_frame, left), (right_frame, right) in zip(points, points[1:], strict=False):
            distance = math.dist(left, right)
            if right_frame == left_frame + 1 and distance <= foot_contact_tolerance_m:
                contacts.setdefault(effector, []).extend((left_frame, right_frame))
    contacts = {name: sorted(set(indices)) for name, indices in contacts.items()}
    clip = MotionClip(
        name=name,
        fps=fps,
        frames=rotations,
        root_track=root_track,
        contacts=contacts,
        camera_context={
            "compensation": "translation_rotation_and_scale",
            "subject_scale": subject_scale,
            "camera_track_samples": len(camera_track),
            "origin": "declared_3d_observations",
        },
    )
    clip.validate()
    return clip


def _camera_compensated(
    point: tuple[float, float, float],
    camera: tuple[float, float, float],
    yaw: float,
    subject_scale: float,
) -> tuple[float, float, float]:
    dx, dy, dz = (point[i] - camera[i] for i in range(3))
    cosine, sine = math.cos(yaw), math.sin(yaw)
    return (
        (cosine * dx + sine * dy) / subject_scale,
        (-sine * dx + cosine * dy) / subject_scale,
        dz / subject_scale,
    )


def compose_actions(actions: list[MotionClip], beat_frames: list[int]) -> MotionClip:
    """Concatenate exactly three clips at declared beat frames, shifting contacts."""
    if len(actions) != 3 or len(beat_frames) != 3:
        raise MotionError("choreography requires exactly three actions and three beat frames")
    fps = actions[0].fps
    if any(action.fps != fps or not action.frames for action in actions):
        raise MotionError("choreography actions require matching fps and non-empty frames")
    if beat_frames[0] != 0 or any(
        b < a for a, b in zip(beat_frames, beat_frames[1:], strict=False)
    ):
        raise MotionError("beat frames must be ordered and begin at frame 0")
    output = MotionClip(name="beat_choreography", fps=fps)
    for action, beat in zip(actions, beat_frames, strict=True):
        if beat != len(output.frames):
            raise MotionError("each action beat must align with the preceding action boundary")
        output.frames.extend(dict(frame) for frame in action.frames)
        output.root_track.extend(action.root_track or [(0.0, 0.0, 0.0)] * len(action.frames))
        for effector, frames in action.contacts.items():
            output.contacts.setdefault(effector, []).extend(beat + frame for frame in frames)
    output.action_id = "+".join(action.name for action in actions)
    output.validate()
    return output


def transfer_acting(
    clip: MotionClip, source: Skeleton, target: Skeleton, mapping: dict[str, str]
) -> MotionClip:
    """Transfer only across compatible body classes and matching identity/mask channels."""
    if source.body_class != target.body_class:
        raise MotionError(
            f"incompatible body class: {source.body_class!r} to {target.body_class!r}"
        )
    if clip.identity_id and target.identity_id and clip.identity_id != target.identity_id:
        raise MotionError("acting transfer identity does not match target rig")
    if not clip.mask_asset_id:
        raise MotionError("acting transfer requires an explicit mask_asset_id")
    output = retarget(clip, source, target, mapping)
    output.identity_id = clip.identity_id or target.identity_id
    output.mask_asset_id = clip.mask_asset_id
    output.camera_context = {
        **clip.camera_context,
        "transfer": "identity_mask_compatible_cpu_retarget",
    }
    return output


__all__ = ["compose_actions", "recover_motion", "transfer_acting"]
