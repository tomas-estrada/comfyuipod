"""Bind pose y retarget de grados relativo al bind (motion-bind-retarget).

El retarget debe transferir el OFFSET de grados respecto a la bind pose del
esqueleto fuente (ángulo − bind), sumarlo a la bind pose del hueso destino y
recortar el resultado a los límites del destino. Sin bind declarado (0.0 por
hueso) el comportamiento absoluto actual no cambia.

Estas pruebas cubren la validación, el retarget relativo, la persistencia y la
compatibilidad de esqueletos sin bind declarada.
"""

from __future__ import annotations

import pytest

from comfy_studio.motion import Bone, MotionClip, MotionError, Skeleton, retarget


def _skeleton(
    name: str,
    bind: dict[str, float] | None = None,
    scale: float = 1.0,
) -> Skeleton:
    bones = [
        Bone(name="root", parent=None),
        Bone(name="spine", parent="root", min_deg=-45.0, max_deg=45.0),
        Bone(name="arm_L", parent="spine", min_deg=0.0, max_deg=160.0),
    ]
    return Skeleton(
        name=name, bones=bones, root="root", scale=scale, bind=dict(bind or {})
    )


def _clip(frames: list[dict[str, float]]) -> MotionClip:
    return MotionClip(
        name="clip",
        fps=24,
        frames=frames,
        root_track=[(0.0, 0.0, 0.0) for _ in frames],
    )


_FULL_MAPPING = {"spine": "spine", "arm_L": "arm_L"}


def test_skeleton_accepts_and_keeps_declared_bind_pose() -> None:
    """El esqueleto declara su bind pose en grados por hueso y la conserva."""
    skeleton = _skeleton("src", bind={"spine": 10.0, "arm_L": 5.0})
    skeleton.validate()  # no lanza
    assert skeleton.bind == {"spine": 10.0, "arm_L": 5.0}


def test_skeleton_defaults_bind_to_empty() -> None:
    """Sin bind declarado, la bind pose efectiva es 0.0 por hueso."""
    skeleton = _skeleton("src")
    skeleton.validate()  # no lanza
    assert skeleton.bind == {}


def test_skeleton_rejects_bind_for_unknown_bone() -> None:
    """La bind pose solo puede declarar huesos existentes."""
    skeleton = _skeleton("src", bind={"no_existe": 5.0})
    with pytest.raises(MotionError, match="bind"):
        skeleton.validate()


def test_skeleton_rejects_nonfinite_bind_degree() -> None:
    """Los grados de bind deben ser finitos."""
    skeleton = _skeleton("src", bind={"spine": float("nan")})
    with pytest.raises(MotionError, match="bind"):
        skeleton.validate()


def test_skeleton_rejects_bind_outside_joint_limits() -> None:
    """La bind pose debe estar dentro del rango permitido de cada articulación."""
    skeleton = _skeleton("src", bind={"spine": 60.0})
    with pytest.raises(MotionError, match="fuera de sus límites"):
        skeleton.validate()


def test_retarget_transfers_offset_relative_to_source_bind() -> None:
    """El ángulo se interpreta como offset respecto a la bind del fuente.

    Fuente con bind spine=10° y clip spine=20°: offset +10°; destino con
    bind spine=0° ⇒ el hueso destino queda en 10°, no en 20°.
    """
    src = _skeleton("src", bind={"spine": 10.0})
    tgt = _skeleton("tgt", bind={"spine": 0.0})
    out = retarget(_clip([{"spine": 20.0, "arm_L": 0.0}]), src, tgt, _FULL_MAPPING)
    assert out.frames[0]["spine"] == pytest.approx(10.0)


def test_retarget_adds_offset_to_target_bind() -> None:
    """El mismo offset +10° sobre un destino con bind spine=30° ⇒ 40°."""
    src = _skeleton("src", bind={"spine": 0.0})
    tgt = _skeleton("tgt", bind={"spine": 30.0})
    out = retarget(_clip([{"spine": 10.0, "arm_L": 0.0}]), src, tgt, _FULL_MAPPING)
    assert out.frames[0]["spine"] == pytest.approx(40.0)


def test_retarget_at_bind_pose_maps_bind_to_bind() -> None:
    """Un clip en la bind pose del fuente produce la bind pose del destino."""
    src = _skeleton("src", bind={"spine": -10.0, "arm_L": 20.0})
    tgt = _skeleton("tgt", bind={"spine": 5.0, "arm_L": 45.0})
    out = retarget(
        _clip([{"spine": -10.0, "arm_L": 20.0}]), src, tgt, _FULL_MAPPING
    )
    assert out.frames[0]["spine"] == pytest.approx(5.0)
    assert out.frames[0]["arm_L"] == pytest.approx(45.0)


def test_retarget_clamps_above_max_after_bind_offset() -> None:
    """Tras aplicar el offset, se respeta el límite máximo del destino."""
    src = _skeleton("src", bind={"arm_L": 0.0})
    tgt = _skeleton("tgt", bind={"arm_L": 20.0})
    # offset +155° ⇒ 175° sobre la bind del destino; el máximo del destino
    # es 160°, se recorta.
    out = retarget(_clip([{"spine": 0.0, "arm_L": 155.0}]), src, tgt, _FULL_MAPPING)
    assert out.frames[0]["arm_L"] == pytest.approx(160.0)


def test_retarget_clamps_below_min_after_bind_offset() -> None:
    """Tras aplicar el offset, se respeta el límite mínimo del destino."""
    src = _skeleton("src", bind={"arm_L": 10.0})
    tgt = _skeleton("tgt", bind={"arm_L": 10.0})
    # offset −30° ⇒ −20° sobre la bind del destino; el mínimo del destino
    # es 0°, se recorta.
    out = retarget(_clip([{"spine": 0.0, "arm_L": -20.0}]), src, tgt, _FULL_MAPPING)
    assert out.frames[0]["arm_L"] == pytest.approx(0.0)


def test_retarget_without_declared_bind_keeps_absolute_degrees() -> None:
    """Regresión: sin bind declarado el retarget absoluto actual no cambia."""
    src, tgt = _skeleton("src"), _skeleton("tgt")
    out = retarget(_clip([{"spine": 20.0, "arm_L": 200.0}]), src, tgt, _FULL_MAPPING)
    assert out.frames[0]["spine"] == pytest.approx(20.0)
    assert out.frames[0]["arm_L"] == pytest.approx(160.0)  # clamp al límite destino


def test_retarget_validates_bind_of_both_skeletons() -> None:
    """El retarget valida la bind pose declarada de fuente y destino."""
    bad_target = _skeleton("tgt", bind={"hueso_fantasma": 1.0})
    with pytest.raises(MotionError, match="bind"):
        retarget(
            _clip([{"spine": 0.0, "arm_L": 0.0}]),
            _skeleton("src"),
            bad_target,
            _FULL_MAPPING,
        )
