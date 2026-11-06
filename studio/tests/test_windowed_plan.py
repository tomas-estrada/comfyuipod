"""Tests del planificador de ventanas (M4): `plan_generation_windows`.

`plan_generation_windows` es aritmética pura (no toca ffmpeg, ni el
ProjectStore, ni disco), así que se cubre de forma determinista en CPU.

Estos tests fijan el comportamiento REAL medido sobre el código, incluidas
tres desviaciones frente a lo que prometía el docstring original:

1. `estimated_assembled_sec` NO está acotado por `total_duration_sec`.
   La última ventana tiene un suelo de 5 fotogramas (`max(5, ...)`), así que
   para duraciones cortas el plan pide generar más vídeo del que se pidió
   (p. ej. total=0.1 s a 16 fps => 5 frames = 0.312 s, un 212 % de exceso).
2. El solapamiento se resta redondeado a milisegundos (`0.5625` -> `0.562`),
   lo que deja un residuo positivo sistemático de ~0.5 ms por costura
   cuando hay varias ventanas.
3. `max_windows` rechaza el plan justo cuando se genera la ventana número
   `max_windows` (el guard se evalúa DESPUÉS de añadir), así que el máximo
   de ventanas que se puede planificar es `max_windows - 1`.

Solo una corrección observable cambia con el tipo: se comprueba que
`WindowedAssemblyError` es un `RuntimeError`.
"""

from __future__ import annotations

import pytest

from comfy_studio.edl.windowed import (
    MAX_WINDOW_CHUNKS,
    WindowedAssemblyError,
    plan_generation_windows,
)

# Avance neto por ventana con los valores por defecto: (81 - 9) / 16 = 4.5 s.
DEFAULT_EFFECTIVE_SEC = 4.5
DEFAULT_CHUNK_SEC = 81 / 16.0  # 5.0625
DEFAULT_OVERLAP_SEC = 9 / 16.0  # 0.5625


# ---------------------------------------------------------------------------
# Guardias: tabla de los cinco controles, probando el borde exacto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total",
    [0.0, -1.0, -0.001, -1000.0],
)
def test_rejects_total_duration_not_positive(total: float) -> None:
    """`total_sec <= 0` se rechaza (estricto: 0 NO es válido)."""
    with pytest.raises(WindowedAssemblyError, match="total_duration_sec"):
        plan_generation_windows(total)


def test_total_duration_guard_is_exclusive_at_zero() -> None:
    """0.0 rechazado, pero el menor positivo representable se acepta.

    La guardia aplica `round(x, 3)` ANTES de comparar, así que 0.0001
    (que redondea a 0.0) se rechaza igual que 0.0.
    """
    with pytest.raises(WindowedAssemblyError):
        plan_generation_windows(0.0)
    # round(0.0001, 3) == 0.0 -> cae en la misma guardia.
    with pytest.raises(WindowedAssemblyError):
        plan_generation_windows(0.0001)
    # round(0.0005, 3) == 0.001 > 0 -> ya entra.
    assert plan_generation_windows(0.0005)["total_duration_sec"] == 0.001


def test_windowed_assembly_error_is_runtime_error() -> None:
    assert issubclass(WindowedAssemblyError, RuntimeError)


@pytest.mark.parametrize(
    ("chunk_frames", "rejected"),
    [
        (4, True),
        (5, False),  # borde inferior inclusivo
        (81, False),  # defecto
        (241, False),  # borde superior inclusivo
        (242, True),
    ],
)
def test_chunk_frames_bounds_are_inclusive(chunk_frames: int, rejected: bool) -> None:
    """`chunk_frames` admite [5, 241]; 5 y 241 incluidos."""
    if rejected:
        with pytest.raises(WindowedAssemblyError, match="chunk_frames"):
            plan_generation_windows(1.0, chunk_frames=chunk_frames)
    else:
        # El solape por defecto (9) solo cabe si chunk_frames > 9.
        overlap = min(9, chunk_frames - 1)
        plan = plan_generation_windows(
            1.0, chunk_frames=chunk_frames, overlap_frames=overlap
        )
        assert plan["chunk_frames"] == chunk_frames


@pytest.mark.parametrize(
    ("fps", "rejected"),
    [
        (0, True),
        (1, False),  # borde inferior inclusivo
        (16, False),  # defecto
        (60, False),  # borde superior inclusivo
        (61, True),
    ],
)
def test_fps_bounds_are_inclusive(fps: int, rejected: bool) -> None:
    """`fps` admite [1, 60]; ambos extremos incluidos."""
    if rejected:
        with pytest.raises(WindowedAssemblyError, match="fps"):
            plan_generation_windows(1.0, fps=fps)
    else:
        plan = plan_generation_windows(1.0, fps=fps)
        assert plan["fps"] == fps


@pytest.mark.parametrize(
    ("overlap_frames", "rejected"),
    [
        (-1, True),
        (0, False),  # borde inferior inclusivo
        (80, False),  # chunk_frames - 1 con el defecto: válido
        (81, True),  # == chunk_frames: EXCLUIDO
    ],
)
def test_overlap_frames_upper_bound_is_exclusive(
    overlap_frames: int, rejected: bool
) -> None:
    """`overlap_frames` admite [0, chunk_frames): 81 con chunk=81 se rechaza."""
    if rejected:
        with pytest.raises(WindowedAssemblyError, match="overlap_frames"):
            plan_generation_windows(1.0, chunk_frames=81, overlap_frames=overlap_frames)
    else:
        plan = plan_generation_windows(
            1.0, chunk_frames=81, overlap_frames=overlap_frames
        )
        assert plan["overlap_frames"] == overlap_frames


def test_overlap_bound_scales_with_chunk_frames() -> None:
    """El límite superior de solape sigue a `chunk_frames` (relativo, no fijo)."""
    # Con chunk_frames=241 el 240 sigue siendo válido y 241 no.
    # Total corto: con chunk_frames=241 y solape 240 el avance neto es de un
    # solo fotograma, así que totales largos superarían el cupo de ventanas.
    assert plan_generation_windows(1.0, chunk_frames=241, overlap_frames=240) is not None
    with pytest.raises(WindowedAssemblyError, match="overlap_frames"):
        plan_generation_windows(2.0, chunk_frames=241, overlap_frames=241)

    # Con chunk_frames=5 (mínimo) el máximo solape es 4. Se usan totales
    # cortos: con solape máximo el avance neto es de 1 fotograma y un total
    # de 2 s exigiría 32 ventanas, por encima del cupo por defecto.
    assert plan_generation_windows(1.0, chunk_frames=5, overlap_frames=4) is not None
    with pytest.raises(WindowedAssemblyError, match="overlap_frames"):
        plan_generation_windows(1.0, chunk_frames=5, overlap_frames=5)
    # Recuerda: el solape por defecto (9) es inválido con chunk_frames=5.
    with pytest.raises(WindowedAssemblyError, match="overlap_frames"):
        plan_generation_windows(2.0, chunk_frames=5)


# ---------------------------------------------------------------------------
# `max_windows`
# ---------------------------------------------------------------------------


def test_max_windows_boundary_rejects_at_max_not_above() -> None:
    """El plan se rechaza al generar la ventana `max_windows`, una antes de lo que

    sugiere el mensaje: el guard se evalúa DESPUÉS de añadir, así que el máximo
    de ventanas que se llega a planificar es `max_windows - 1`.

    Con 4.5 s de avance neto hacen falta total > 4.5 s para una 2ª ventana,
    y eso se rechaza con max_windows=2 pese a necesitar solo 2.
    """
    # Una sola ventana: OK con max_windows=2.
    assert plan_generation_windows(4.5, max_windows=2)["windows_count"] == 1
    # En cuanto se requiere la 2ª ventana, se rechaza (aunque 2 == max_windows).
    with pytest.raises(WindowedAssemblyError, match="ventanas"):
        plan_generation_windows(4.51, max_windows=2)
    # El mismo total con cupo suficiente da exactamente 2 ventanas.
    assert plan_generation_windows(4.51, max_windows=64)["windows_count"] == 2


def test_max_windows_one_is_unreachable() -> None:
    """Con `max_windows=1` ninguna duración produce plan: siempre lanza."""
    for total in (0.001, 0.01, 1.0, 10.0, 1000.0):
        with pytest.raises(WindowedAssemblyError):
            plan_generation_windows(total, max_windows=1)


def test_max_windows_default_rejects_before_max_window_chunks() -> None:
    """Con el cupo por defecto se llega a 31 ventanas, no a 32.

    0.5 s extra sobre el salto número 31 (30 * 4.5) ya dispara el guard.
    """
    last_ok = 30 * DEFAULT_EFFECTIVE_SEC + 0.5  # 135.5 s -> 31 ventanas
    assert plan_generation_windows(last_ok)["windows_count"] == 31
    # +1 salto más excede el cupo por defecto.
    with pytest.raises(WindowedAssemblyError, match="ventanas"):
        plan_generation_windows(31 * DEFAULT_EFFECTIVE_SEC + 0.01)
    # `MAX_WINDOW_CHUNKS` es el cupo de chunks del ensamblador, no de ventanas.
    assert MAX_WINDOW_CHUNKS == 32


def test_max_windows_guard_message_names_the_limit() -> None:
    with pytest.raises(WindowedAssemblyError, match="más de 3 ventanas"):
        plan_generation_windows(100.0, max_windows=3)


# ---------------------------------------------------------------------------
# Aritmética normal: ventanas, solape, chunking
# ---------------------------------------------------------------------------


def test_plan_shape_and_types() -> None:
    plan = plan_generation_windows(10.0)
    assert set(plan) == {
        "total_duration_sec",
        "fps",
        "chunk_frames",
        "overlap_frames",
        "windows_count",
        "estimated_assembled_sec",
        "seamless_hint",
        "windows",
    }
    assert plan["total_duration_sec"] == 10.0
    assert plan["fps"] == 16
    assert plan["chunk_frames"] == 81
    assert plan["overlap_frames"] == 9
    assert isinstance(plan["windows"], list)
    assert isinstance(plan["seamless_hint"], str) and plan["seamless_hint"]
    assert isinstance(plan["estimated_assembled_sec"], float)


def test_windows_count_matches_list_length() -> None:
    plan = plan_generation_windows(30.0)
    assert plan["windows_count"] == len(plan["windows"]) == 7


def test_every_window_has_expected_keys_and_monotonic_index() -> None:
    plan = plan_generation_windows(30.0)
    for position, window in enumerate(plan["windows"]):
        assert set(window) == {
            "index",
            "start_sec",
            "end_sec",
            "frames",
            "skip_first_frames",
            "video_frame_offset",
            "crossfade_sec",
            "continue_motion_frames",
        }
        assert window["index"] == position
        assert isinstance(window["frames"], int)
        assert isinstance(window["skip_first_frames"], int)
        assert isinstance(window["video_frame_offset"], int)


def test_starts_advance_by_net_step_chunk_minus_overlap() -> None:
    """El inicio avanza `chunk_frames - overlap_frames` fotogramas, no `chunk_frames`."""
    plan = plan_generation_windows(30.0)
    starts = [w["start_sec"] for w in plan["windows"]]
    assert starts[0] == 0.0
    # 72 fotogramas / 16 fps = 4.5 s
    assert [round(s, 3) for s in starts] == [0.0, 4.5, 9.0, 13.5, 18.0, 22.5, 27.0]
    for previous, current in zip(starts, starts[1:], strict=False):
        assert current - previous == pytest.approx(DEFAULT_EFFECTIVE_SEC)


def test_skip_first_frames_and_video_frame_offset_track_start() -> None:
    """Ambos offsets son `round(start_sec * fps)`; sirven a `VHS_LoadVideo` y a

    `wan_animate_cont` respectivamente.
    """
    plan = plan_generation_windows(30.0)
    for window in plan["windows"]:
        expected = int(round(window["start_sec"] * 16))
        assert window["skip_first_frames"] == expected
        assert window["video_frame_offset"] == window["skip_first_frames"]


def test_no_window_is_empty_and_none_exceeds_chunk_frames() -> None:
    """Nunca hay ventanas vacías: `frames >= 1` siempre, y <= `chunk_frames`."""
    for total in (0.001, 0.1, 1.0, 10.0, 30.0, 100.0):
        plan = plan_generation_windows(total)
        assert plan["windows"], f"sin ventanas para total={total}"
        for window in plan["windows"]:
            assert window["frames"] >= 1
            assert window["frames"] <= 81
            assert window["end_sec"] > window["start_sec"] or total <= 5 / 16.0


def test_window_end_sec_is_clamped_to_total() -> None:
    """`end_sec` usa `min(total_sec, start + frames/fps)`: ningún fin pasa del total.

    Esto es lo que SÍ cumple el docstring: la marca de fin de cada ventana
    está recortada, aunque `frames` no lo esté (ver tests de exceso).
    """
    for total in (0.01, 0.1, 1.0, 5.0, 10.0, 30.0, 120.0):
        plan = plan_generation_windows(total)
        for window in plan["windows"]:
            assert window["end_sec"] <= plan["total_duration_sec"] + 1e-9
        assert plan["windows"][-1]["end_sec"] == pytest.approx(total, abs=1e-3)


def test_first_window_has_no_continuation_seams() -> None:
    """La ventana 0 no funde con nada; de la 1 en adelante llevan el solape."""
    plan = plan_generation_windows(30.0)
    first, second = plan["windows"][0], plan["windows"][1]
    assert first["crossfade_sec"] == 0.0
    assert first["continue_motion_frames"] == 0
    assert second["crossfade_sec"] == pytest.approx(0.562)
    assert second["continue_motion_frames"] == 9


def test_overlap_zero_disables_all_seams() -> None:
    plan = plan_generation_windows(30.0, overlap_frames=0)
    assert [w["crossfade_sec"] for w in plan["windows"]] == [0.0] * len(plan["windows"])
    assert all(w["continue_motion_frames"] == 0 for w in plan["windows"])
    # Sin solape el avance es el chunk completo: 81/16 = 5.0625 s (el
    # `start_sec` publicado va redondeado a milisegundos, de ahí 5.062).
    starts = [w["start_sec"] for w in plan["windows"]]
    assert starts[1] == 5.062
    assert starts[1] - starts[0] == pytest.approx(DEFAULT_CHUNK_SEC, abs=1e-3)


def test_single_window_when_total_fits_in_one_chunk() -> None:
    """Un clip más corto que un chunk produce una sola ventana recortada."""
    plan = plan_generation_windows(1.0)  # 16 fotogramas < 81
    assert plan["windows_count"] == 1
    assert plan["windows"][0]["frames"] == 16
    assert plan["windows"][0]["start_sec"] == 0.0
    assert plan["windows"][0]["end_sec"] == 1.0


def test_last_window_frames_is_the_remainder() -> None:
    """La última ventana se recorta a los fotogramas restantes del total."""
    plan = plan_generation_windows(30.0)
    # 30 s * 16 fps = 480 fotogramas; 5 ventanas de 81 -> 75 fotogramas finales.
    last = plan["windows"][-1]
    assert last["start_sec"] == 27.0
    assert (30.0 - 27.0) * 16 == 48
    assert last["frames"] == 48
    assert last["skip_first_frames"] == 432


def test_full_chunk_windows_use_exactly_chunk_frames() -> None:
    """Las ventanas intermedias (no la última) piden `chunk_frames` completos."""
    plan = plan_generation_windows(30.0)
    for window in plan["windows"][:-1]:
        assert window["frames"] == 81


def test_plan_is_deterministic() -> None:
    assert plan_generation_windows(17.3) == plan_generation_windows(17.3)


def test_duration_is_rounded_to_milliseconds() -> None:
    plan = plan_generation_windows(10.0009)
    assert plan["total_duration_sec"] == 10.001


# ---------------------------------------------------------------------------
# El exceso: preguntas planteadas sobre las cotas del plan
# ---------------------------------------------------------------------------


def test_windows_never_overshoot_total_duration() -> None:
    """Respuesta a "¿las ventanas pueden pasarse de `total_sec`?": NO.

    Probado por barrido sobre combinaciones de duración, fps, chunk y solape:
    ningún `start_sec` alcanza el total y ningún `end_sec` lo supera, porque
    `end_sec` está recortado con `min(total_sec, ...)` y el bucle para con
    `start_sec < total_sec - 1e-6`.
    """
    exceeded: list[tuple[float, int, int, int, dict]] = []
    for total in (0.01, 0.1, 0.3125, 1.0, 4.5, 5.0, 10.0, 30.0, 100.0, 135.5):
        for chunk_frames in (5, 9, 81, 241):
            for fps in (1, 16, 60):
                for overlap in (0, chunk_frames - 1):
                    try:
                        plan = plan_generation_windows(
                            total,
                            chunk_frames=chunk_frames,
                            fps=fps,
                            overlap_frames=overlap,
                        )
                    except WindowedAssemblyError:
                        continue
                    for window in plan["windows"]:
                        if window["end_sec"] > plan["total_duration_sec"] + 1e-9:
                            exceeded.append(
                                (total, chunk_frames, fps, overlap, window)
                            )
    assert exceeded == []


def test_five_frame_floor_makes_frames_overshoot_the_remaining_video() -> None:
    """Respuesta a "¿`frames` puede exceder lo que queda de vídeo?": SÍ.

    `frames = min(chunk_frames, max(5, round(remaining * fps)))` tiene suelo de
    5 fotogramas, así que para un total corto el plan pide fotogramas que el
    vídeo fuente no tiene:

        total = 0.1 s, fps = 16  ->  quedan 0.1 * 16 = 1.6 fotogramas
        round(1.6) = 2  ->  max(5, 2) = 5  ->  min(81, 5) = 5 fotogramas

    Es decir, 5 / 16 = 0.312 s pedidos para un clip de 0.1 s: 3.1x el total.
    `end_sec` sigue valiendo 0.1 (recortado), pero `frames` = 5 describe una
    ventana 0.212 s más larga que la fuente.
    """
    plan = plan_generation_windows(0.1)
    window = plan["windows"][0]
    assert window["frames"] == 5
    assert (0.1 - window["start_sec"]) * 16 == pytest.approx(1.6)
    assert window["frames"] > 1.6
    assert window["end_sec"] == 0.1  # recortado, no 0.312


@pytest.mark.parametrize(
    ("total", "fps", "frames", "generated_sec"),
    [
        (0.1, 16, 5, 0.312),  # 3.1x el total
        (0.01, 16, 5, 0.312),  # 31x el total
        (1.0, 1, 5, 5.0),  # 5x el total
        (0.01, 1, 5, 5.0),  # 500x el total
    ],
)
def test_short_duration_floor_requests_more_than_source(
    total: float, fps: int, frames: int, generated_sec: float
) -> None:
    """El suelo de 5 fotogramas pide siempre 5 frames cuando el total es corto."""
    plan = plan_generation_windows(total, fps=fps, overlap_frames=0)
    window = plan["windows"][0]
    assert window["frames"] == frames
    assert frames / fps == pytest.approx(generated_sec, abs=1e-3)
    assert generated_sec > total


def test_estimated_assembled_sec_is_not_bounded_by_total_duration() -> None:
    """Respuesta a "¿está `estimated_assembled_sec` acotado por el total?": NO.

    El docstring daba a entender que sí. Medido:

        plan(10.0)  ->  ventanas de 81, 81 y 16 fotogramas a 16 fps
                        bruto   = (81 + 81 + 16) / 16 = 11.125 s
                        costuras = 2 * 0.562          = 1.124 s
                        estimated_assembled_sec = 11.125 - 1.124 = 10.001 s

    10.001 > 10.0: el plan promete 1 ms más que la duración pedida. No es un
    redondeo inocuo, es estructural (ver los dos tests siguientes).
    """
    plan = plan_generation_windows(10.0)
    assert plan["estimated_assembled_sec"] == 10.001
    assert plan["estimated_assembled_sec"] > plan["total_duration_sec"]


def test_estimated_assembled_sec_overshoot_is_amplified_by_the_frame_floor() -> None:
    """El grueso del exceso viene del suelo de 5 fotogramas en la última ventana.

        total = 0.1 s, 16 fps, sin solape:
            única ventana -> frames = 5 -> 5 / 16 = 0.3125 s
            costuras = 0                -> estimated = 0.312 s
            0.312 - 0.1 = +0.212 s (212 % por encima del pedido)
    """
    plan = plan_generation_windows(0.1, overlap_frames=0)
    assert plan["estimated_assembled_sec"] == 0.312
    assert plan["estimated_assembled_sec"] - 0.1 == pytest.approx(0.212)


def test_crossfade_rounding_leaves_a_positive_residue_per_seam() -> None:
    """Segunda causa del exceso: el solape se resta redondeado a milisegundos.

        overlap_sec  = 9 / 16 = 0.5625 s (exacto)
        redondeado   = 0.562 s           (lo que se guarda y se resta)
        residuo      = 0.0005 s ACUMULADO por cada costura

    Con 7 ventanas (30 s) hay 6 costuras: 6 * 0.0005 = 0.003 s de exceso.
    Con solape 0 el residuo desaparece y el total cuadra exacto.
    """
    plan = plan_generation_windows(30.0)
    seams = sum(1 for w in plan["windows"] if w["index"] > 0)
    assert seams == 6
    assert plan["estimated_assembled_sec"] == 30.003
    assert plan["estimated_assembled_sec"] - 30.0 == pytest.approx(0.003)

    no_overlap = plan_generation_windows(30.0, overlap_frames=0)
    assert no_overlap["estimated_assembled_sec"] == pytest.approx(30.0, abs=1e-6)


def test_floor_and_overlap_residue_compound_for_many_windows() -> None:
    """Con cupo amplio el exceso crece con el número de ventanas.

        total = 130 s, fps = 1, chunk = 9, solape 0:
            avance de 9 s/ventana -> 15 ventanas
            última ventana: round(4 * 1) = 4 -> max(5, 4) = 5 fotogramas
            bruto = 14 * 9 + 5 = 131 s -> +1 s sobre los 130 pedidos
    """
    plan = plan_generation_windows(130.0, chunk_frames=9, fps=1, overlap_frames=0)
    assert plan["windows_count"] == 15
    assert plan["windows"][-1]["frames"] == 5
    assert plan["estimated_assembled_sec"] == 131.0
    assert plan["estimated_assembled_sec"] > plan["total_duration_sec"]
