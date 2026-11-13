"""Contrato de arreglos musicales (C26): grid inmutable, hit/loop, variantes.

Metadata-only: los stems referenciados son los ya almacenados en el mix causal
del proyecto; nada aquí genera audio ni reclama haberlo generado.
"""

from __future__ import annotations

import dataclasses

import pytest

from comfy_studio.music import (
    MAX_REEL_DURATION_S,
    Arrangement,
    ArrangementSection,
    MusicError,
    TempoGrid,
    delete_stem_sections,
)


def _section(**overrides) -> ArrangementSection:
    base = {
        "section_id": "hit-1",
        "kind": "hit",
        "start_s": 0.0,
        "duration_s": 2.0,
        "stem": "drums",
    }
    base.update(overrides)
    return ArrangementSection(**base)


def _arrangement(sections, duration_s=20.0, bpm=120.0) -> Arrangement:
    return Arrangement(
        grid=TempoGrid(bpm=bpm),
        sections=tuple(sections),
        duration_s=duration_s,
    )


# --- TempoGrid: BPM y timebase inválidos -------------------------------------


def test_invalid_bpm_rejected() -> None:
    for bad in (0.0, -90.0, float("nan"), float("inf")):
        with pytest.raises(MusicError):
            TempoGrid(bpm=bad)


def test_invalid_timebase_rejected() -> None:
    for bad in (0, -480):
        with pytest.raises(MusicError):
            TempoGrid(bpm=120.0, timebase_ppq=bad)


# --- Secciones y arreglo inválidos --------------------------------------------


def test_invalid_sections_rejected() -> None:
    with pytest.raises(MusicError):
        _section(kind="solo")  # solo hit | loop
    with pytest.raises(MusicError):
        _section(stem="  ")  # stem vacío
    with pytest.raises(MusicError):
        _section(start_s=-0.5)
    with pytest.raises(MusicError):
        _section(duration_s=0.0)
    with pytest.raises(MusicError):
        _section(section_id=" ")  # id vacío


def test_reel_capped_at_20_seconds() -> None:
    assert MAX_REEL_DURATION_S == 20.0
    with pytest.raises(MusicError):
        _arrangement([_section()], duration_s=20.5)
    with pytest.raises(MusicError):
        _arrangement([_section()], duration_s=0.0)
    with pytest.raises(MusicError):
        _arrangement([_section(start_s=19.0, duration_s=2.0)])


def test_duplicate_section_ids_rejected() -> None:
    with pytest.raises(MusicError):
        _arrangement([_section(), _section(section_id="hit-1", start_s=2.0)])


# --- Beat map determinista -----------------------------------------------------


def test_beat_map_is_deterministic_and_round_trips() -> None:
    grid = TempoGrid(bpm=120.0)
    beats = grid.beat_map(2.0)
    assert beats == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert grid.beat_map(2.0) == beats  # misma entrada, misma salida
    restored = TempoGrid.from_dict(grid.to_dict())
    assert restored == grid
    assert restored.beat_map(2.0) == beats
    # bpm 140: intervalo 60/140 s; redondeo determinista a 6 decimales.
    fast = TempoGrid(bpm=140.0)
    assert fast.beat_map(1.0) == [round(i * 60.0 / 140.0, 6) for i in range(3)]


# --- Serialización hit + loop + variante ---------------------------------------


def test_hit_loop_variant_serialization_round_trip() -> None:
    arrangement = _arrangement(
        [
            _section(),
            _section(
                section_id="loop-1",
                kind="loop",
                start_s=2.0,
                duration_s=6.0,
                stem="bass",
                variant="chorus",
            ),
        ]
    )
    data = arrangement.to_dict()
    assert data["schema"] == "music-arrangement/1"
    assert Arrangement.from_dict(data) == arrangement
    # Inmutabilidad real: no se puede mutar ni el grid ni una sección.
    with pytest.raises(dataclasses.FrozenInstanceError):
        arrangement.duration_s = 30.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        arrangement.sections[0].stem = "other"


def test_corrupt_serialized_arrangement_fails_closed() -> None:
    with pytest.raises(MusicError):
        Arrangement.from_dict({"schema": "unknown-music-schema"})
    with pytest.raises(MusicError):
        Arrangement.from_dict({"schema": "music-arrangement/1", "bpm": "x"})


# --- Borrado causal de stems ----------------------------------------------------


def test_delete_stem_is_selective_and_immutable() -> None:
    arrangement = _arrangement(
        [
            _section(section_id="hit-drums", stem="drums"),
            _section(section_id="loop-bass", kind="loop", start_s=2.0, stem="bass"),
            _section(section_id="hit-drums-2", start_s=9.0, duration_s=1.0, stem="drums"),
        ]
    )
    updated, removed = delete_stem_sections(arrangement, "drums")
    assert removed == ["hit-drums", "hit-drums-2"]
    assert [s.section_id for s in updated.sections] == ["loop-bass"]
    # El arreglo original queda intacto (inmutabilidad).
    assert len(arrangement.sections) == 3
    # Stem inexistente: no cambia nada, no elimina nada.
    unchanged, removed_none = delete_stem_sections(arrangement, "vocals")
    assert removed_none == []
    assert unchanged == arrangement
