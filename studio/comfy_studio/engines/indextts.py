"""Motor de clonación de voz zero-shot IndexTTS-2.5 (ULA bilibili).

Sintetiza texto con el timbre de un audio de referencia (zero-shot, sin
entrenamiento por hablante). Diferencias clave con RVC (motor
`voice_clone`):

- RVC es CONVERSIÓN de voz (audio→audio) y requiere fairseq, que no está
  horneado en el pod: falla tipado.
- IndexTTS-2.5 es SÍNTESIS desde texto (texto+referencia→voz) y sus pesos
  (grupo `indextts`, 22 archivos, 5.11 GiB) ya están descargados.

El paquete vive en un venv aislado en runtime (`STUDIO_INDEXTTS_VENV`,
persistente bajo COMFY_DATA_DIR) porque sus pines de dependencias
(torch==2.8, numpy==2.2.6, transformers==4.52.1) clavarían el entorno del
pod; el adaptador CLI `indextts25` (engine_bin) gestiona ese venv.

Licencia: IndexTTS-2.5 es ULA de bilibili (uso no comercial permitido por
su licencia; ver docs/decisiones.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from comfy_studio.config import SecretRedactor
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

#: directorio de pesos (grupo indextts descargado por download-models.sh).
_MODEL_DIR_ENV = "STUDIO_INDEXTTS_MODEL_DIR"
#: adaptador CLI horneado en /usr/local/bin/indextts25.
_BIN_ENV = "STUDIO_INDEXTTS_BIN"
_DEFAULT_TIMEOUT_S = 900.0

_STUB_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16


@dataclass(slots=True)
class IndexttsSpec:
    """Trabajo de síntesis: texto + audio de referencia -> WAV con su timbre."""

    text: str
    output_path: Path
    reference_audio: Path  # muestra del timbre a clonar (zero-shot)
    lang: str = "es"
    emotion_text: str = ""  # descripción de emoción opcional (QwenEmotion)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not self.text.strip():
            raise ValueError("indextts requiere 'text'")
        if not self.reference_audio.is_file():
            raise ValueError(f"audio de referencia inexistente: {self.reference_audio}")
        if self.lang not in ("es", "en", "zh", "ja", "yue"):
            raise ValueError(f"lang no soportado: {self.lang!r}")


class IndexttsEngine:
    """Clonación de voz zero-shot IndexTTS-2.5. Contrato de 4 verbos."""

    name = "indextts"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _model_available(self) -> bool:
        model_dir = (os.environ.get(_MODEL_DIR_ENV) or "").strip()
        if model_dir:
            p = Path(model_dir)
            return (p / "config.yaml").is_file() and (p / "gpt.pth").is_file()
        from comfy_studio.engines.preparedness import weights_status

        return bool(weights_status(self.name).present)

    def list(self) -> EngineResult:
        stub = self.is_stub()
        ready = stub or self._model_available()
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "model_dir": os.environ.get(_MODEL_DIR_ENV) or "",
                "weights_ok": ready and not stub,
                "stub": stub,
            },
            error=None if ready else f"pesos ausentes ({_MODEL_DIR_ENV})",
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        # Los pesos vienen horneados por download-models.sh; nada que instalar.
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "los pesos del grupo indextts se descargan con download-models.sh "
                "(fase indextts); este motor no instala submotores"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        available = self._model_available()
        present = self.is_stub() or available
        weights = "present" if available else "missing"
        return EngineResult(
            ok=present,
            engine=self.name,
            verb="verify",
            payload={"weights_ok": present, "stub": self.is_stub(), "weights": weights},
            error=None if present else f"pesos ausentes ({_MODEL_DIR_ENV})",
            reason=None if present else "weights_missing",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            job = spec.job
            m = IndexttsSpec(
                text=str(job.get("text", "")),
                output_path=Path(job["output_path"]),
                reference_audio=Path(job["reference_audio"]),
                lang=str(job.get("lang", "es")),
                emotion_text=str(job.get("emotion_text", "")),
                timeout_s=float(job.get("timeout_s", _DEFAULT_TIMEOUT_S)),
            )
            m.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"spec inválido: {exc}",
                reason="spec_invalid",
            )

        start = time.monotonic()
        if self.is_stub():
            m.output_path.parent.mkdir(parents=True, exist_ok=True)
            m.output_path.write_bytes(_STUB_WAV)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "output": str(m.output_path),
                    "lang": m.lang,
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_INDEXTTS: clonación zero-shot simulada",
            )

        if not self._model_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"pesos IndexTTS-2.5 ausentes ({_MODEL_DIR_ENV}); "
                    "descárgalos con la fase indextts de download-models.sh "
                    "o usa STUDIO_STUB_INDEXTTS=1"
                ),
                reason="weights_missing",
            )
        return self._run_model(m, start)

    def _run_model(self, m: IndexttsSpec, start: float) -> EngineResult:
        """Inferencia real vía el adaptador horneado, subproceso aislado."""
        redactor = SecretRedactor()
        indextts_bin = (
            os.environ.get(_BIN_ENV) or shutil.which("indextts25") or ""
        ).strip()
        if not indextts_bin:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"binario indextts25 no encontrado ({_BIN_ENV} o PATH)",
            )
        model_dir = (os.environ.get(_MODEL_DIR_ENV) or "").strip()
        cmd = [
            indextts_bin,
            "--model-dir",
            model_dir,
            "--reference",
            str(m.reference_audio),
            "--text",
            m.text,
            "--output",
            str(m.output_path),
            "--lang",
            m.lang,
            "--timeout",
            f"{m.timeout_s:.1f}",
        ]
        if m.emotion_text:
            cmd += ["--emotion-text", m.emotion_text]

        m.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=m.timeout_s + 120.0,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"IndexTTS-2.5 excedió el timeout ({m.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar indextts25: {exc}"),
            )
        if res.returncode != 0 or not m.output_path.exists():
            err = redactor.redact((res.stderr or "").strip()[-800:])
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"IndexTTS-2.5 falló (rc={res.returncode}): {err}",
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "output": str(m.output_path),
                "lang": m.lang,
                "emotion_text": m.emotion_text or None,
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
