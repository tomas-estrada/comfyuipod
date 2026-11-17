"""Normalización de texto hablado es-CO en CPU pura (C27, slice acotado).

Este módulo NO sintetiza audio y NO afirma acento. Convierte un subconjunto
determinista y autoral de patrones (fechas `DD/MM/YYYY` y cifras con punto
separador de miles es-CO) a palabras en español, e informa coincidencias con
un lexicon fonético AUTORAL (Sabaneta/Itagüí/Envigado) cuya fonética NO está
verificada por audio. Ante patrón ambiguo o fecha malformada/fuera de rango
rechaza en lugar de adivinar.

Invariantes:
- La fonética del lexicon es autoral: no hay verificación por audio.
- No se afirma acento paisa ni calidad de modelo (A21): el idioma no acredita
  acento.
- Los nombres propios se conservan con ortografía NFC (incluye «Itagüí»).
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from comfy_studio.voice import VoiceError

LOCALE_ES_CO = "es-CO"

# Lexicon fonético AUTORAL (sin verificación por audio; ver provenance).
AUTHORED_LEXICON: dict[str, str] = {
    "Sabaneta": "sa-ba-NE-ta",
    "Itagüí": "i-ta-GU-i",
    "Envigado": "en-vi-GA-do",
}

_DATE_RE = re.compile(r"^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$")
_PLAIN_INT_RE = re.compile(r"^[0-9]+$")
_THOUSAND_GROUPED_RE = re.compile(r"^([0-9]{1,3}(\.[0-9]{3})+)$")

_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

_UNITS = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve")
_TEENS = (
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
)
_TWENTIES = (
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
)
_TENS = ("", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa")
_HUNDREDS = (
    "",
    "ciento",
    "doscientos",
    "trescientos",
    "cuatrocientos",
    "quinientos",
    "seiscientos",
    "setecientos",
    "ochocientos",
    "novecientos",
)

_MAX_NUMBER = 10**12 - 1  # hasta 999.999.999.999

_PAUSE_NAMES = {
    ".": "full_stop",
    ",": "comma",
    ";": "semicolon",
    ":": "colon",
    "!": "exclamation",
    "?": "question",
}
_TRAILING_PUNCT = frozenset(_PAUSE_NAMES)
_TOKEN_STRIP_CHARS = ".,;:!?¿¡\"'«»“”()"


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _key(text: str) -> str:
    return _nfc(text).casefold()


def _hundreds_to_words(n: int, *, apocope: bool = False) -> list[str]:
    """0..999 → palabras en español. `apocope` da «un/veintiún» antes de «mil/millones»."""
    if n == 0:
        return []
    if n == 100:
        return ["cien"]
    words: list[str] = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if rest == 0:
        return words
    if rest < 10:
        unit = _UNITS[rest]
    elif rest < 20:
        unit = _TEENS[rest - 10]
    elif rest < 30:
        unit = _TWENTIES[rest - 20]
    else:
        tens_word, unit_digit = divmod(rest, 10)
        unit = _UNITS[unit_digit]
        if unit_digit:
            unit = f"{_TENS[tens_word]} y {unit}"
        else:
            unit = _TENS[tens_word]
    if apocope:
        if unit == "uno":
            unit = "un"
        elif unit == "veintiuno":
            unit = "veintiún"
        elif unit.endswith(" y uno"):
            unit = f"{unit[:-6]} y un"
    words.append(unit)
    return words


def _thousands_group_to_words(n: int, *, apocope: bool = False) -> str:
    """0..999.999 → grupo de miles con apócope ante «mil»."""
    thousands, remainder = divmod(n, 1000)
    parts: list[str] = []
    if thousands:
        if thousands == 1:
            parts.append("mil")
        else:
            parts.append(" ".join(_hundreds_to_words(thousands, apocope=True)) + " mil")
    if remainder:
        parts.append(" ".join(_hundreds_to_words(remainder, apocope=apocope)))
    return " ".join(parts)


def number_to_words(n: int) -> str:
    """Entero 0..999.999.999.999 → palabras en español (determinista)."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise VoiceError("número debe ser entero")
    if n < 0 or n > _MAX_NUMBER:
        raise VoiceError(f"número {n} fuera de rango soportado (0..{_MAX_NUMBER})")
    if n == 0:
        return "cero"
    parts: list[str] = []
    millions, remainder = divmod(n, 1_000_000)
    if millions:
        if millions == 1:
            parts.append("un millón")
        else:
            parts.append(f"{_thousands_group_to_words(millions, apocope=True)} millones")
    thousands, last = divmod(remainder, 1000)
    if thousands:
        if thousands == 1:
            parts.append("mil")
        else:
            parts.append(" ".join(_hundreds_to_words(thousands, apocope=True)) + " mil")
    if last:
        parts.append(" ".join(_hundreds_to_words(last)))
    return " ".join(parts)


def _parse_date(token: str) -> tuple[int, int, int]:
    """`DD/MM/YYYY` estricto; rechaza formato ambiguo y fechas inválidas."""
    if not _DATE_RE.match(token):
        raise VoiceError(
            f"fecha «{token}» en formato no soportado; se exige DD/MM/YYYY "
            "con año de 4 dígitos (p. ej. 12/10/2026)"
        )
    day_s, month_s, year_s = token.split("/")
    day, month, year = int(day_s), int(month_s), int(year_s)
    if year == 0:
        raise VoiceError(f"fecha «{token}» inválida: año debe ser mayor que cero")
    if not 1 <= month <= 12:
        raise VoiceError(f"fecha «{token}» inválida: mes {month} fuera de rango (1..12)")
    last_day = calendar.monthrange(year, month)[1]
    if not 1 <= day <= last_day:
        raise VoiceError(
            f"fecha «{token}» inválida: día {day} fuera de rango para "
            f"{month:02d}/{year} (1..{last_day})"
        )
    return day, month, year


def _date_to_words(token: str) -> str:
    day, month, year = _parse_date(token)
    return f"{number_to_words(day)} de {_MONTHS[month - 1]} de {number_to_words(year)}"


def _parse_number(token: str) -> int:
    """Cifras es-CO: entero o grupos de miles con punto; ambiguo → rechazo."""
    if _PLAIN_INT_RE.match(token):
        if len(token) > 1 and token.startswith("0"):
            raise VoiceError(f"cifra «{token}» ambigua (cero inicial)")
        if len(token) > 12:
            raise VoiceError("cifra fuera de rango soportado (máximo 12 dígitos)")
        n = int(token)
    elif _THOUSAND_GROUPED_RE.match(token):
        groups = token.split(".")
        if groups[0].startswith("0"):
            raise VoiceError(f"cifra «{token}» ambigua (cero inicial en grupo principal)")
        if len("".join(groups)) > 12:
            raise VoiceError("cifra fuera de rango soportado (máximo 12 dígitos)")
        n = int("".join(groups))
    else:
        raise VoiceError(
            f"cifra «{token}» ambigua o en formato no soportado "
            "(se admite entero o grupos de miles con punto, p. ej. 125.000)"
        )
    if n > _MAX_NUMBER:
        raise VoiceError(f"cifra «{token}» fuera de rango soportado (<= {_MAX_NUMBER})")
    return n


def _number_token_to_words(token: str) -> str:
    return number_to_words(_parse_number(token))


def _token_core(token: str) -> tuple[str, str, str]:
    """Separa puntuación periférica de un token: (lead, core, tail)."""
    lead = ""
    while token and token[0] in _TOKEN_STRIP_CHARS:
        # A leading decimal-like dot belongs to the numeric core and must fail
        # closed instead of becoming sentence punctuation.
        if token[0] == "." and len(token) > 1 and token[1] in "0123456789":
            break
        lead += token[0]
        token = token[1:]
    tail = ""
    while token and token[-1] in _TOKEN_STRIP_CHARS:
        tail = token[-1] + tail
        token = token[:-1]
    return lead, token, tail


@dataclass(slots=True)
class LexiconMatch:
    """Coincidencia con el lexicon autoral (fonética NO verificada por audio)."""

    name: str
    phonetic: str
    source: str = "authored_lexicon"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "phonetic": self.phonetic, "source": self.source}


@dataclass(slots=True)
class PauseMarker:
    """Marca de pausa derivada de puntuación (determinista)."""

    marker: str
    type: str
    token_index: int

    def to_dict(self) -> dict[str, Any]:
        return {"marker": self.marker, "type": self.type, "token_index": self.token_index}


@dataclass(slots=True)
class PatternConversion:
    """Conversión respaldada por patrón (fecha o cifra) a palabras."""

    raw: str
    spoken: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "spoken": self.spoken, "kind": self.kind}


@dataclass(slots=True)
class Provenance:
    """Procedencia: fonética autoral sin verificación por audio; sin acento."""

    phonetics_verified_by_audio: bool = False
    accent_claimed: bool = False
    note: str = (
        "lexicon fonético autoral NO verificado por audio; no se afirma acento "
        "paisa ni calidad de modelo (A21); sin ejecución ASR/TTS"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phonetics_verified_by_audio": self.phonetics_verified_by_audio,
            "accent_claimed": self.accent_claimed,
            "note": self.note,
        }


@dataclass(slots=True)
class NormalizationReport:
    """Informe tipado de normalización es-CO (CPU pura)."""

    raw_text: str
    spoken_text: str
    locale: str = LOCALE_ES_CO
    lexicon_matches: list[LexiconMatch] = field(default_factory=list)
    pause_markers: list[PauseMarker] = field(default_factory=list)
    conversions: list[PatternConversion] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "spoken_text": self.spoken_text,
            "locale": self.locale,
            "lexicon_matches": [m.to_dict() for m in self.lexicon_matches],
            "pause_markers": [p.to_dict() for p in self.pause_markers],
            "conversions": [c.to_dict() for c in self.conversions],
            "provenance": self.provenance.to_dict(),
        }


def normalize_es_co_text(raw_text: str) -> NormalizationReport:
    """Normaliza texto hablado es-CO con reglas autoral y acotadas (C27).

    Soporta fechas `DD/MM/YYYY` y cifras enteras / agrupadas por miles con
    punto (`125.000`). Ante fecha malformada/fuera de rango o cifra ambigua
    lanza `VoiceError` en lugar de adivinar. Los nombres del lexicon autoral
    se reportan con su fonética NO verificada por audio.
    """
    text = _nfc(raw_text)
    tokens = text.split()
    if not tokens:
        raise VoiceError("texto vacío: nada que normalizar")

    lexicon_keys = {_key(name): name for name in AUTHORED_LEXICON}
    matched_keys: set[str] = set()
    lexicon_matches: list[LexiconMatch] = []
    pause_markers: list[PauseMarker] = []
    conversions: list[PatternConversion] = []
    spoken_parts: list[str] = []

    for index, token in enumerate(tokens):
        lead, core, tail = _token_core(token)
        for marker in tail:
            marker_type = _PAUSE_NAMES.get(marker)
            if marker_type:
                pause_markers.append(PauseMarker(marker, marker_type, index))
        if core:
            folded = _key(core)
            if folded in lexicon_keys and folded not in matched_keys:
                matched_keys.add(folded)
                name = lexicon_keys[folded]
                lexicon_matches.append(LexiconMatch(name=name, phonetic=AUTHORED_LEXICON[name]))
            if _DATE_RE.match(core):
                spoken = _date_to_words(core)
                conversions.append(PatternConversion(core, spoken, "date"))
                spoken_parts.append(f"{lead}{spoken}{tail}")
                continue
            if _PLAIN_INT_RE.match(core) or _THOUSAND_GROUPED_RE.match(core):
                spoken = _number_token_to_words(core)
                conversions.append(PatternConversion(core, spoken, "number"))
                spoken_parts.append(f"{lead}{spoken}{tail}")
                continue
            if any(ch.isdigit() for ch in core):
                # dígitos presentes pero patrón no soportado → no adivinar
                if "/" in core:
                    raise VoiceError(
                        f"fecha «{core}» en formato ambiguo o no soportado; "
                        "se exige DD/MM/YYYY con año de 4 dígitos"
                    )
                _parse_number(core)
        spoken_parts.append(token)

    return NormalizationReport(
        raw_text=text,
        spoken_text=" ".join(spoken_parts),
        lexicon_matches=lexicon_matches,
        pause_markers=pause_markers,
        conversions=conversions,
    )


__all__ = [
    "AUTHORED_LEXICON",
    "LOCALE_ES_CO",
    "LexiconMatch",
    "NormalizationReport",
    "PauseMarker",
    "PatternConversion",
    "Provenance",
    "normalize_es_co_text",
    "number_to_words",
]
