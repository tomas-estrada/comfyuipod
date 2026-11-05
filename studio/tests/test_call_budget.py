"""Test puro-CPU para la clase CallBudget de comfy_studio.agent.loop.

Cubre el consumo compartido de llamadas LLM por turno y el saneo de límites:
- `consume()` devuelve True exactamente `max_calls` veces y False después.
- `max_calls` no positivo (0, negativo) se sanean a 1.
- `max_calls` flotante se trunca con `int()` antes del saneo.
"""

from __future__ import annotations

from comfy_studio.agent.loop import CallBudget


def test_consume_returns_true_exactly_max_calls_times() -> None:
    """Tres llamadas autorizadas: las tres primeras son True, la cuarta False."""
    budget = CallBudget(3)
    assert budget.max_calls == 3
    assert budget.used == 0

    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False
    assert budget.consume() is False  # se mantiene agotado

    assert budget.used == 3


def test_consume_zero_allows_exactly_one_call() -> None:
    """Con `max_calls=0` el saneo eleva el límite a 1."""
    budget = CallBudget(0)
    assert budget.max_calls == 1
    assert budget.consume() is True
    assert budget.consume() is False
    assert budget.used == 1


def test_consume_negative_is_clamped_to_one() -> None:
    """Un límite negativo (p. ej. -5) se sanea a 1, nunca a 0 ni negativo."""
    budget = CallBudget(-5)
    assert budget.max_calls == 1
    assert budget.consume() is True
    assert budget.consume() is False
    assert budget.used == 1


def test_float_max_calls_is_truncated_before_clamp() -> None:
    """`max_calls=2.9` se trunca a 2 (int), no se redondea a 3."""
    budget = CallBudget(2.9)
    assert budget.max_calls == 2
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False
    assert budget.used == 2


def test_consume_starts_fresh_and_used_is_public() -> None:
    """Un nuevo presupuesto empieza con `used=0` y los campos son los esperados."""
    budget = CallBudget(2)
    assert budget.used == 0
    assert budget.max_calls == 2