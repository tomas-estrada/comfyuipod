"""Foley, separación y sonido causal (plan Ultra, W18 / A20).

Eventos con onset/loudness/causa; stems separados; timebase compartido; borrar
una causa elimina solo eventos vinculados. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.audio import (
    AudioError,
    Mix,
    SoundEvent,
    Stem,
    assert_separate_stems,
    delete_cause,
)


def _mix() -> Mix:
    foley = Stem(
        name="foley",
        events=[
            SoundEvent("step1", onset_s=0.5, caused_by="actor_walk"),
            SoundEvent("step2", onset_s=1.0, caused_by="actor_walk"),
            SoundEvent("door", onset_s=2.0, caused_by="door_open"),
        ],
    )
    ambience = Stem(name="ambience", events=[SoundEvent("wind", onset_s=0.0)])
    return Mix(fps=24, stems=[foley, ambience], duration_s=3.0)


def test_mix_validates_shared_timebase_and_separate_stems() -> None:
    m = _mix()
    m.validate()  # no lanza
    assert m.frame_of(1.0) == 24  # onset 1s -> frame 24 @ 24fps


def test_duplicate_stem_names_rejected() -> None:
    m = Mix(fps=24, stems=[Stem(name="foley"), Stem(name="foley")])
    with pytest.raises(AudioError, match="duplicado"):
        m.validate()


def test_event_outside_timebase_rejected() -> None:
    stem = Stem(name="foley", events=[SoundEvent("x", onset_s=99.0)])
    m = Mix(fps=24, stems=[stem], duration_s=3.0)
    with pytest.raises(AudioError, match="fuera del timebase"):
        m.validate()


def test_delete_cause_removes_only_linked_events() -> None:
    """A20: borrar la causa elimina solo los eventos vinculados."""
    m = _mix()
    removed = delete_cause(m, "actor_walk")
    assert removed == {"step1", "step2"}  # los pasos del actor
    # El golpe de puerta y el viento se conservan (otras causas / sin causa).
    remaining = {e.event_id for e in m.all_events()}
    assert "door" in remaining
    assert "wind" in remaining
    assert "step1" not in remaining


def test_delete_cause_without_links_removes_nothing() -> None:
    m = _mix()
    removed = delete_cause(m, "causa_inexistente")
    assert removed == set()
    assert len(m.all_events()) == 4  # nada se tocó


def test_single_mixed_stem_does_not_count_as_separated() -> None:
    """W19: no inferir stems por generación conjunta."""
    m = Mix(fps=24, stems=[Stem(name="mixed", events=[SoundEvent("all", 0.0)])])
    with pytest.raises(AudioError, match="no acredita separación"):
        assert_separate_stems(m)


def test_real_separation_passes() -> None:
    assert_separate_stems(_mix())  # dialogue/foley/ambience separados: no lanza


def test_mix_serialization_groups_by_stem() -> None:
    d = _mix().to_dict()
    assert "foley" in d["stems"] and "ambience" in d["stems"]
    assert len(d["stems"]["foley"]) == 3
    assert Mix.from_dict(d).to_dict() == d


def test_mix_rejects_non_finite_timing_and_duplicate_event_ids() -> None:
    from math import inf

    invalid_time = Mix(fps=24, stems=[Stem(name="foley", events=[SoundEvent("x", inf)])])
    with pytest.raises(AudioError, match="onset inválido"):
        invalid_time.validate()

    duplicate = Mix(
        fps=24,
        stems=[
            Stem(name="foley", events=[SoundEvent("same", 0.0)]),
            Stem(name="music", events=[SoundEvent("same", 0.5)]),
        ],
    )
    with pytest.raises(AudioError, match="event_id duplicado"):
        duplicate.validate()
