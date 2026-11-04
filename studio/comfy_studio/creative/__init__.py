"""Cerebro creativo del estudio (M7, Worker 7): comprensión multimodal con
evidence, brief creativo persistente/editable, dirección editorial, revisión
sobre el render y paquete de entrega.

Todo dato derivado del material conserva su traza: `asset_id`, intervalo o
fotograma, método de extracción, estado real/simulado y la evidencia disponible.
"""

from __future__ import annotations

from .brief import (
    BRIEF_SCHEMA,
    PIECE_TYPES,
    build_brief,
    new_brief,
    parse_prompt,
    validate_brief,
)
from .delivery import build_delivery_package
from .direction import build_direction
from .review import review_output
from .understanding import UNDERSTANDING_SCHEMA, build_understanding

__all__ = [
    "BRIEF_SCHEMA",
    "PIECE_TYPES",
    "UNDERSTANDING_SCHEMA",
    "build_brief",
    "build_delivery_package",
    "build_direction",
    "build_understanding",
    "new_brief",
    "parse_prompt",
    "review_output",
    "validate_brief",
]
