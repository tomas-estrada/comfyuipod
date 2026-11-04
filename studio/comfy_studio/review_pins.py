"""Dominio de review pins (M3-RVP-001): feedback anclado a versiones de EDL.

Valida ventanas temporales sobre el timeline renderizado, empareja pins con
cortes del EDL (máximo solapamiento) y resume pins abiertos por severidad para
el contexto del agente.
"""

from __future__ import annotations

from typing import Any

PIN_SEVERITIES: tuple[str, ...] = ("note", "issue", "blocker")
PIN_STATUSES: tuple[str, ...] = ("open", "resolved", "discarded")


class ReviewPinError(ValueError):
    """Error de validación de un review pin."""


def validate_pin_window(
    t_start: float,
    t_end: float,
    timeline_duration: float | None = None,
) -> None:
    """Valida la ventana temporal de un pin contra el timeline del EDL.

    `timeline_duration` es opcional: la duración solo se conoce tras el render
    (`update_edl_output`); si es `None`, no se comprueba el techo.
    """
    if t_start < 0:
        raise ReviewPinError(f"t_start no puede ser negativo: {t_start}")
    if t_end < t_start:
        raise ReviewPinError(
            f"t_end ({t_end}) no puede ser menor que t_start ({t_start})"
        )
    if timeline_duration is not None and t_start > timeline_duration:
        raise ReviewPinError(
            f"t_start ({t_start}) excede la duración del timeline "
            f"({timeline_duration})"
        )


def match_pin_to_cut(pin: dict[str, Any], cuts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Devuelve el corte del EDL con máximo solapamiento con el pin.

    `cuts` son cortes tipo `EdlCutItem` (`in_sec`/`out_sec`). Si el pin no
    solapa con ningún corte (hueco entre cortes), devuelve `None`.
    """
    t_start = float(pin.get("t_start", 0.0))
    t_end = float(pin.get("t_end", 0.0))
    best: dict[str, Any] | None = None
    best_overlap = 0.0
    for cut in cuts or []:
        try:
            c_in = float(cut.get("in_sec"))
            c_out = float(cut.get("out_sec"))
        except (TypeError, ValueError):
            continue
        overlap = min(t_end, c_out) - max(t_start, c_in)
        if overlap > best_overlap:
            best_overlap = overlap
            best = cut
    return best


def open_pin_summary(pins: list[dict[str, Any]]) -> dict[str, int]:
    """Cuenta pins por severidad, incluyendo la clave `open` total."""
    summary = {severity: 0 for severity in PIN_SEVERITIES}
    open_count = 0
    for pin in pins or []:
        if pin.get("status") != "open":
            continue
        open_count += 1
        severity = pin.get("severity")
        if severity in summary:
            summary[severity] += 1
    return {**summary, "open": open_count}
