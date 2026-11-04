"""Montaje asistido y verificable desde brief en lenguaje natural (M7).

Recupera momentos relevantes del proyecto cruzando imagen (CLIP), diálogo
(transcripciones) y persona (identidades), propone cortes con asset y timecodes
de origen, y deja cada decisión trazable a evidencia real del proyecto.
"""

from __future__ import annotations

from .brief import (
    MontageBriefRequest,
    MontagePlanResult,
    parse_brief,
    plan_montage_from_brief,
    tokenize_terms,
)

__all__ = [
    "MontageBriefRequest",
    "MontagePlanResult",
    "parse_brief",
    "plan_montage_from_brief",
    "tokenize_terms",
]
