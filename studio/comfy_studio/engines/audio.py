"""Motor de audio del estudio (M4-SCR-002, F5): voz, transcripción, música, lipsync.

Agrupa el acabado de audio por escena bajo un solo motor con cuatro verbos. Las
operaciones concretas van en `spec.job['op']`:

    tts      → texto → voz (IndexTTS ya existe en el pod).
    stt      → audio → texto (faster-whisper; ver ml/worker.py asr_transcribe).
    music    → prompt → pista musical (MusicGen/Stable Audio, submotor on the run).
    lipsync  → vídeo + audio → vídeo con labios sincronizados.

tier WARM. Stub-first: cada op escribe un artefacto mínimo en modo
`STUDIO_STUB_AUDIO=1`. Las ops que hoy NO tienen backend real en el pod (music,
lipsync) fallan tipado fuera de stub, en vez de fingir — el agente sabe que debe
traer el submotor (F6) antes de usarlas.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

_OPS = ("tts", "stt", "music", "lipsync")

# Ops con backend real ya presente en el pod (M3): TTS y STT. Las demás son
# submotores a traer on the run (F6); fuera de stub fallan tipado.
_WIRED_OPS = frozenset({"tts", "stt"})

_STUB_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
_STUB_TXT = "[STUB] transcripción simulada"


@dataclass(slots=True)
class AudioSpec:
    op: str
    output_path: Path
    text: str = ""
    input_audio: Path | None = None
    input_video: Path | None = None
    prompt: str = ""
    voice_id: str = "default"
    max_seconds: float = 30.0

    def validate(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"op debe ser uno de {_OPS}, no {self.op!r}")
        if self.op == "tts" and not self.text.strip():
            raise ValueError("tts requiere 'text'")
        if self.op == "stt" and self.input_audio is None:
            raise ValueError("stt requiere 'input_audio'")
        if self.op == "music" and not self.prompt.strip():
            raise ValueError("music requiere 'prompt'")
        if self.op == "lipsync" and (self.input_video is None or self.input_audio is None):
            raise ValueError("lipsync requiere 'input_video' y 'input_audio'")
        if not (0.1 <= self.max_seconds <= 300.0):
            raise ValueError("max_seconds fuera de rango (0.1..300)")


class AudioEngine:
    """Motor de audio. Contrato de 4 verbos; `run` despacha por op."""

    name = "audio"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "wired_ops": sorted(_WIRED_OPS),
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        """music/lipsync son submotores a traer on the run (cableado F6)."""
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=f"cableado de submotor de audio '{submotor}' pendiente (F6)",
        )

    def verify(self, submotor: str) -> EngineResult:
        present = submotor in _WIRED_OPS or self.is_stub()
        return EngineResult(
            ok=present,
            engine=self.name,
            verb="verify",
            payload={"op": submotor, "wired": submotor in _WIRED_OPS, "stub": self.is_stub()},
            error=None if present else f"op '{submotor}' aún sin backend (F6)",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            audio = self._parse(spec.job)
            audio.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            return self._stub_run(audio, start)

        if audio.op not in _WIRED_OPS:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"op '{audio.op}' aún no tiene backend real en el pod; "
                    "traer el submotor (F6) o usar STUDIO_STUB_AUDIO=1"
                ),
            )

        # STT real: delegar al worker de ML existente (faster-whisper,
        # asr_transcribe). Escribe la transcripción en output_path.
        if audio.op == "stt":
            from comfy_studio.ml.runner import run_worker

            resp = run_worker(
                "asr_transcribe",
                {"audio_path": str(audio.input_audio)},
            )
            if not resp.ok:
                return EngineResult(
                    ok=False,
                    engine=self.name,
                    verb="run",
                    error=f"stt falló: {resp.error or 'error del worker'}",
                    stderr_tail=resp.stderr_tail,
                )
            text = str((resp.payload or {}).get("text", ""))
            simulated = bool((resp.payload or {}).get("simulated", False))
            audio.output_path.parent.mkdir(parents=True, exist_ok=True)
            audio.output_path.write_text(text, encoding="utf-8")
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "op": "stt",
                    "output": str(audio.output_path),
                    "language": (resp.payload or {}).get("language"),
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=simulated,
            )

        # TTS: backend real Kokoro (lang_code='e' = español, Apache, CPU). Horneado
        # en la imagen bajo constraint torch-safe. Clonación de voz (Chatterbox/
        # CosyVoice3) queda DIFERIDA a un venv aislado en runtime: sus pines
        # torch==2.6.0 clavarían el torch 2.9.1 del pod. Sin kokoro, falla tipado.
        if audio.op == "tts":
            return self._run_kokoro_tts(audio, start)

        return EngineResult(
            ok=False,
            engine=self.name,
            verb="run",
            error=(
                f"op '{audio.op}' sin backend real integrado todavía; "
                "STT usa el worker ASR, TTS usa Kokoro"
            ),
        )

    def _run_kokoro_tts(self, audio: AudioSpec, start: float) -> EngineResult:
        try:
            import numpy as np
            import soundfile as sf
            from kokoro import KPipeline
        except ImportError:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    "backend TTS (Kokoro) no disponible; "
                    "hornear `kokoro` en la imagen o usar STUDIO_STUB_AUDIO=1"
                ),
            )
        # voice_id puede nombrar una voz española (p.ej. 'ef_dora'); default a una es.
        voice = audio.voice_id if audio.voice_id != "default" else "ef_dora"
        try:
            pipeline = KPipeline(lang_code="e")  # español
            chunks = [
                audio_chunk
                for _gs, _ps, audio_chunk in pipeline(audio.text, voice=voice)
            ]
        except (RuntimeError, ValueError, OSError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"Kokoro TTS falló: {type(exc).__name__}: {exc}",
            )
        if not chunks:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error="Kokoro no produjo audio"
            )
        audio.output_path.parent.mkdir(parents=True, exist_ok=True)
        wav = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        sf.write(str(audio.output_path), wav, 24000)
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "op": "tts",
                "output": str(audio.output_path),
                "voice": voice,
                "lang": "es",
                "duration_s": round(time.monotonic() - start, 3),
            },
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _parse(job: dict) -> AudioSpec:
        return AudioSpec(
            op=str(job.get("op", "")),
            output_path=Path(job["output_path"]),
            text=str(job.get("text", "")),
            input_audio=Path(job["input_audio"]) if job.get("input_audio") else None,
            input_video=Path(job["input_video"]) if job.get("input_video") else None,
            prompt=str(job.get("prompt", "")),
            voice_id=str(job.get("voice_id", "default")),
            max_seconds=float(job.get("max_seconds", 30.0)),
        )

    def _stub_run(self, audio: AudioSpec, start: float) -> EngineResult:
        out = audio.output_path
        out.parent.mkdir(parents=True, exist_ok=True)
        if audio.op == "stt":
            out.write_text(_STUB_TXT, encoding="utf-8")
        elif audio.op == "lipsync":
            out.write_bytes(_STUB_MP4)
        else:  # tts, music
            out.write_bytes(_STUB_WAV)
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "op": audio.op,
                "output": str(out),
                "duration_s": round(time.monotonic() - start, 3),
            },
            simulated=True,
            stderr_tail=f"STUDIO_STUB_AUDIO: {audio.op} simulado",
        )
