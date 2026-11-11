"""Música, voz en español y actuación vocal (plan Ultra, W19 / A21).

W19 pide ACE-Step y Qwen3-TTS/IndexTTS con variantes y **lexicon colombiano**,
beat map/estructura, hablantes y pistas separadas, con datasets de pronunciación
y test de sincronía. El invariante clave (A21): «no inferir acento paisa por
idioma español ni stems por generación conjunta»; distinguir una **variante de
pronunciación aprobada** de mero soporte de idioma; lexicon persistente y
corregible.

Este módulo implementa el CONTRATO:

- `PronunciationLexicon`: un lexicon por LOCALE (p. ej. `es-CO-paisa`) con
  entradas texto→fonética APROBADAS explícitamente. El acento NO se infiere del
  idioma: una variante local exige entradas reales en el lexicon.
- `assert_approved_variant`: una variante regional solo es válida si su lexicon
  tiene cobertura; el mero soporte de español NO acredita acento paisa.
- `VoiceLine` / `Utterance`: líneas por HABLANTE (pistas separadas, no una
  generación conjunta).
- `sync_report`: métricas de sincronía/ASR como INDICADORES, no juez perfecto.

Opera sobre texto/fonética simbólica; la síntesis TTS real es engine GPU/build.
Verifica que el acento aprobado viene de datos, no de una suposición.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


class VoiceError(ValueError):
    """Contrato de voz inválido."""


@dataclass(slots=True)
class PronunciationLexicon:
    """Lexicon de pronunciación por locale, persistente y corregible (A21)."""

    locale: str  # p. ej. "es-CO-paisa", "es-ES"
    #: texto -> fonética aprobada (IPA/arpabet simbólico).
    entries: dict[str, str] = field(default_factory=dict)
    #: ¿es una variante regional aprobada, o mero soporte de idioma base?
    approved_variant: bool = False

    def add(self, text: str, phonetic: str) -> None:
        """Corrige/añade una entrada (el lexicon es corregible)."""
        key = text.strip().lower()
        value = phonetic.strip()
        if not key or not value:
            raise VoiceError("entrada de lexicon requiere texto y fonética")
        self.entries[key] = value

    def covers(self, text: str) -> bool:
        return text.strip().lower() in self.entries

    def coverage(self, sample: list[str]) -> float:
        """Fracción de un dataset de muestra cubierta por el lexicon."""
        if not sample:
            return 0.0
        hit = sum(1 for t in sample if self.covers(t))
        return hit / len(sample)

    def validate(self) -> None:
        if not self.locale.strip():
            raise VoiceError("locale vacío")
        if any(not text.strip() or not phonetic.strip() for text, phonetic in self.entries.items()):
            raise VoiceError("entradas de lexicon requieren texto y fonética")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "locale": self.locale,
            "entries": dict(self.entries),
            "approved_variant": self.approved_variant,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PronunciationLexicon:
        try:
            if not isinstance(data, dict):
                raise VoiceError("lexicon debe ser un objeto")
            entries = data.get("entries", {})
            if not isinstance(entries, dict):
                raise VoiceError("entries del lexicon debe ser un objeto")
            lexicon = cls(
                locale=str(data["locale"]),
                entries={
                    str(text).strip().lower(): str(value).strip() for text, value in entries.items()
                },
                approved_variant=bool(data.get("approved_variant", False)),
            )
            lexicon.validate()
            return lexicon
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError(f"lexicon serializado inválido: {exc}") from exc


def assert_approved_variant(
    lex: PronunciationLexicon, sample: list[str], *, min_cov: float = 0.8
) -> None:
    """Una variante regional (acento) exige lexicon real, no el idioma (A21).

    «No inferir acento paisa por idioma español». Solo si el lexicon está marcado
    como variante aprobada Y cubre el dataset de muestra se acredita el acento.
    """
    if not lex.approved_variant:
        raise VoiceError(
            f"locale {lex.locale}: soporte de idioma, NO variante aprobada "
            "(el acento no se infiere del idioma)"
        )
    cov = lex.coverage(sample)
    if cov < min_cov:
        raise VoiceError(
            f"locale {lex.locale}: cobertura de lexicon {cov:.0%} < {min_cov:.0%}; "
            "no se acredita el acento sin datos suficientes"
        )


@dataclass(slots=True)
class Utterance:
    """Una línea hablada de un HABLANTE (pista separada, no conjunta)."""

    speaker: str
    text: str
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def validate(self) -> None:
        if not self.speaker.strip() or not self.text.strip():
            raise VoiceError("utterance requiere speaker y texto")
        if not math.isfinite(self.start_s) or not math.isfinite(self.end_s):
            raise VoiceError("utterance contiene tiempos no finitos")
        if self.start_s < 0 or self.end_s <= self.start_s:
            raise VoiceError("utterance requiere 0 <= start_s < end_s")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "speaker": self.speaker,
            "text": self.text,
            "start_s": self.start_s,
            "end_s": self.end_s,
        }


@dataclass(slots=True)
class VoiceTrack:
    """Pista de voz de un hablante — separada de música/ambiente (W19)."""

    speaker: str
    utterances: list[Utterance] = field(default_factory=list)

    def validate(self) -> None:
        if not self.speaker.strip():
            raise VoiceError("speaker vacío")
        last_end = -1.0
        for u in self.utterances:
            u.validate()
            if u.speaker != self.speaker:
                raise VoiceError(
                    f"utterance de {u.speaker} en pista de {self.speaker} (mezcla de hablantes)"
                )
            if u.start_s < last_end:
                raise VoiceError(f"{self.speaker}: turnos solapados en {u.start_s}")
            last_end = u.end_s

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"speaker": self.speaker, "utterances": [u.to_dict() for u in self.utterances]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VoiceTrack:
        try:
            if not isinstance(data, dict):
                raise VoiceError("voice track debe ser un objeto")
            track = cls(
                speaker=str(data["speaker"]),
                utterances=[
                    Utterance(
                        speaker=str(item["speaker"]),
                        text=str(item["text"]),
                        start_s=float(item["start_s"]),
                        end_s=float(item["end_s"]),
                    )
                    for item in data.get("utterances", [])
                ],
            )
            track.validate()
            return track
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError(f"voice track serializada inválida: {exc}") from exc


def sync_report(utterance: Utterance, asr_text: str) -> dict[str, Any]:
    """Métricas de sincronía/ASR como INDICADORES, no juez perfecto (A21)."""
    expected = utterance.text.strip().lower().split()
    got = asr_text.strip().lower().split()
    matched = sum(1 for w in got if w in expected)
    wer_like = 1.0 - (matched / len(expected)) if expected else 1.0
    return {
        "speaker": utterance.speaker,
        "duration_s": round(utterance.duration_s, 3),
        "word_match_ratio": round(matched / len(expected), 3) if expected else 0.0,
        "wer_like": round(max(0.0, wer_like), 3),
        "note": "indicador ASR; NO es juez perfecto de acento/intención (A21)",
    }


__all__ = [
    "PronunciationLexicon",
    "Utterance",
    "VoiceError",
    "VoiceTrack",
    "assert_approved_variant",
    "sync_report",
]
