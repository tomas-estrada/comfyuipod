"""Tests de la puerta de aprobación del storyboard (M4-SCR, F3)."""

from __future__ import annotations

import pytest

from comfy_studio.engines.storyboard_gate import (
    GateTransitionError,
    StoryboardGate,
    StoryboardState,
)


def test_happy_path_draft_to_approved() -> None:
    g = StoryboardGate()
    assert g.can_enter_build() is False
    g.submit_draft("draft_v1.mp4")
    assert g.state is StoryboardState.AWAITING_APPROVAL
    assert g.can_enter_build() is False  # aún no aprobado: BUILD bloqueado
    g.approve()
    assert g.state is StoryboardState.APPROVED
    assert g.can_enter_build() is True
    assert g.rounds == 1


def test_request_changes_loops_back_to_drafting() -> None:
    g = StoryboardGate()
    g.submit_draft("draft_v1.mp4")
    g.request_changes("más contraste en escena 2")
    assert g.state is StoryboardState.DRAFTING
    assert g.can_enter_build() is False
    g.submit_draft("draft_v2.mp4")
    g.approve()
    assert g.can_enter_build() is True
    assert g.rounds == 2  # dos borradores enviados


def test_reject_is_terminal() -> None:
    g = StoryboardGate()
    g.submit_draft("draft_v1.mp4")
    g.reject("idea no funciona")
    assert g.state is StoryboardState.REJECTED
    assert g.is_terminal() is True
    assert g.can_enter_build() is False


def test_cannot_approve_before_draft() -> None:
    g = StoryboardGate()
    with pytest.raises(GateTransitionError, match="AWAITING_APPROVAL"):
        g.approve()


def test_cannot_build_from_drafting_or_awaiting() -> None:
    g = StoryboardGate()
    assert g.can_enter_build() is False
    g.submit_draft("d.mp4")
    assert g.can_enter_build() is False  # nunca cae a BUILD sin aprobación


def test_double_submit_rejected() -> None:
    g = StoryboardGate()
    g.submit_draft("d1.mp4")
    with pytest.raises(GateTransitionError, match="DRAFTING"):
        g.submit_draft("d2.mp4")  # ya está esperando aprobación


def test_roundtrip_serialization() -> None:
    g = StoryboardGate()
    g.submit_draft("draft_v1.mp4")
    g.request_changes("cambio")
    g.submit_draft("draft_v2.mp4")
    g.approve()
    data = g.to_dict()
    restored = StoryboardGate.from_dict(data)
    assert restored.state is StoryboardState.APPROVED
    assert restored.rounds == 2
    assert restored.can_enter_build() is True
    assert len(restored.history) == len(g.history)
