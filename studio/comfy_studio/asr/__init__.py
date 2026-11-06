"""Módulos ASR (`whisperx` + aligners wav2vec2 ES/EN) y diarización (`FunASR cam++`)."""

from __future__ import annotations

from .diarize import (
    CAMPPLUS_MODEL_REL,
    UNKNOWN_SPEAKER,
    CamPlusDiarizer,
    diarize_audio_file,
    diarize_project_asset,
    merge_diarization_with_transcription,
    parse_diarization_payload,
)
from .whisperx import (
    ALIGNER_EN_REL,
    ALIGNER_ES_REL,
    FASTER_WHISPER_MODEL_REL,
    SPANISH_FILLER_WORDS,
    ASRComputeConfig,
    WhisperXTranscriber,
    analyze_wav_acoustic_windows,
    extract_16k_mono_wav,
    parse_asr_payload,
    resolve_asr_backend_plan,
    resolve_asr_compute_config,
    run_with_blackwell_fallback,
    transcribe_project_asset,
)

__all__ = [
    "ALIGNER_EN_REL",
    "ALIGNER_ES_REL",
    "CAMPPLUS_MODEL_REL",
    "FASTER_WHISPER_MODEL_REL",
    "SPANISH_FILLER_WORDS",
    "UNKNOWN_SPEAKER",
    "ASRComputeConfig",
    "CamPlusDiarizer",
    "WhisperXTranscriber",
    "analyze_wav_acoustic_windows",
    "diarize_audio_file",
    "diarize_project_asset",
    "extract_16k_mono_wav",
    "merge_diarization_with_transcription",
    "parse_asr_payload",
    "parse_diarization_payload",
    "resolve_asr_backend_plan",
    "resolve_asr_compute_config",
    "run_with_blackwell_fallback",
    "transcribe_project_asset",
]
