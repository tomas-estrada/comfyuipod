"""Composición y entregables 4K (plan Ultra, W20 / A22).

Timeline editable; entrega 4K por aspecto; créditos/licencias viajan con el
proyecto; editar texto no regenera clips; crop explícito, no disfrazado. CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.delivery_4k import (
    Clip,
    DeliveryError,
    TimelineSpec,
    Title,
    build_delivery_manifest,
    edit_text,
    finishing_plan,
    target_dimensions,
)


def _spec() -> TimelineSpec:
    return TimelineSpec(
        name="reel_dron",
        aspect="16:9",
        fps=24,
        clips=[
            Clip("c1", "/out/wan_t2v_1.mp4", 0.0, 3.0, license="Apache-2.0"),
            Clip("c2", "/out/wan_t2v_2.mp4", 3.0, 6.0, license="Apache-2.0"),
        ],
        titles=[Title("t1", "ATARDECER", 0.0, 2.0)],
        audio_stems=["foley", "music"],
    )


def test_target_dimensions_are_exact_4k_per_aspect() -> None:
    assert target_dimensions("16:9") == (3840, 2160)
    assert target_dimensions("9:16") == (2160, 3840)


def test_unsupported_aspect_rejected() -> None:
    with pytest.raises(DeliveryError, match="aspecto"):
        target_dimensions("21:9")


def test_delivery_manifest_carries_credits_and_licenses() -> None:
    """A22: créditos y licencias viajan con el proyecto (no en un CDN)."""
    d = build_delivery_manifest(_spec()).to_dict()
    assert d["resolution"] == "3840x2160"
    assert len(d["credits"]) == 2
    assert all(c["license"] == "Apache-2.0" for c in d["credits"])
    assert "sin CDN" in d["delivery_note"]


def test_edit_text_does_not_regenerate_clips() -> None:
    """A22: cambiar texto reabre el proyecto sin regenerar contenido aprobado."""
    spec = _spec()
    invalidated = edit_text(spec, "t1", "AMANECER")
    assert invalidated == {"t1"}
    assert spec.clips[0].clip_id == "c1"
    assert spec.titles[0].text == "AMANECER"


def test_finishing_crop_is_explicit_not_disguised() -> None:
    """A22: 3840x2176 no se disfraza de 4K; crop explícito."""
    clip = Clip("c", "/out/x.mp4", 0.0, 3.0, src_width=3840, src_height=2176)
    plan = finishing_plan(clip, "16:9")
    assert plan["op"] == "crop"
    assert plan["crop_px"] == [0, 16]
    assert "no se disfraza" in plan["note"]


def test_finishing_upscale_marked_when_source_smaller() -> None:
    clip = Clip("c", "/out/x.mp4", 0.0, 3.0, src_width=1920, src_height=1080)
    plan = finishing_plan(clip, "16:9")
    assert plan["op"] == "upscale"
    assert "no recuperan detalle" in plan["note"]


def test_finishing_none_when_already_4k() -> None:
    clip = Clip("c", "/out/x.mp4", 0.0, 3.0, src_width=3840, src_height=2160)
    assert finishing_plan(clip, "16:9")["op"] == "none"


def test_empty_timeline_rejected() -> None:
    spec = TimelineSpec(name="vacio", clips=[])
    with pytest.raises(DeliveryError, match="sin clips"):
        spec.validate()


def test_duration_sums_clips() -> None:
    assert _spec().total_duration_s() == pytest.approx(6.0)


def test_finishing_upscale_when_dimensions_mixed() -> None:
    """Caso mixto (una dim >=, otra <): cae en else -> upscale, no crop."""
    clip = Clip("c", "/out/x.mp4", 0.0, 3.0, src_width=3840, src_height=1080)
    plan = finishing_plan(clip, "16:9")
    assert plan["op"] == "upscale"
    assert plan["target"] == [3840, 2160]
    assert plan["source"] == [3840, 1080]


def test_clip_duration_s_never_negative() -> None:
    """duracion negativa se clamp a 0.0."""
    clip = Clip("c", "/out/x.mp4", in_s=5.0, out_s=3.0)
    assert clip.duration_s == 0.0
