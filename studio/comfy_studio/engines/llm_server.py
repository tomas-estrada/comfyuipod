"""Motor de LLM local / servidor OpenAI-compatible (loopback).

Sirve completions de chat sin salir del pod: el binario `llama-server`
(llama.cpp) expone `POST /v1/chat/completions` solo en loopback y este
adaptador lo envuelve con los cuatro verbos del contrato.

  - `list`   -> disponibilidad (binario, modelo, endpoint loopback) + stub.
  - `install`-> siempre falla TIPADO: el binario no está incluido ni se instala
                en runtime; el modelo es dependencia de tarea.
  - `verify` -> exige binario ejecutable (+ modelo si se pide uno concreto).
  - `run`    -> `POST {url}/v1/chat/completions` con timeout y endpoint
                validado como loopback; sin binario/modelo falla TIPADO.

tier WARM. Stub-first (`STUDIO_STUB_LLM_SERVER=1`) para CI sin binario.
Subproceso aislado solo para sondear versión; la inferencia va por HTTP
stdlib a loopback. Todo stderr/error pasa por `SecretRedactor`.

El adapter acepta una instalación explícita mediante `STUDIO_LLM_SERVER_BIN`
o `PATH`; esta imagen no empaqueta ni supervisa `llama-server`.
El adaptador tampoco fija pesos: el modelo .gguf llega en runtime.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from comfy_studio.config import SecretRedactor
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

_BIN_ENV = "STUDIO_LLM_SERVER_BIN"
_MODEL_ENV = "STUDIO_LLM_SERVER_MODEL"
_URL_ENV = "STUDIO_LLM_SERVER_URL"
_DEFAULT_URL = "http://127.0.0.1:8080"
_DEFAULT_TIMEOUT_S = 120.0
_MAX_PROMPT_CHARS = 32000


def _find_binary() -> str:
    override = (os.environ.get(_BIN_ENV) or "").strip()
    if override:
        return override
    for cand in ("llama-server", "/usr/local/bin/llama-server"):
        found = shutil.which(cand) if "/" not in cand else (cand if Path(cand).is_file() else "")
        if found:
            return found
    return ""


def _model_path() -> str:
    return (os.environ.get(_MODEL_ENV) or "").strip()


def _model_available() -> bool:
    model = _model_path()
    return bool(model) and Path(model).is_file()


def _endpoint() -> str:
    return (os.environ.get(_URL_ENV) or _DEFAULT_URL).strip() or _DEFAULT_URL


def is_loopback_url(url: str) -> bool:
    """True solo si el endpoint es loopback (127/8, ::1 o localhost)."""
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def binary_version(binary: str, *, timeout_s: float = 15.0) -> str | None:
    """Versión del binario (`--version`, rc 0) o None si no ejecuta."""
    try:
        res = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    first = (res.stdout or res.stderr or "").strip().splitlines()
    return first[0][:120] if first else None


@dataclass(slots=True)
class ChatSpec:
    """Chat completion declarada: messages o prompt + límites."""

    messages: list[dict[str, str]] = field(default_factory=list)
    prompt: str = ""
    model: str = ""
    max_tokens: int = 256
    temperature: float = 0.7
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if not self.messages and not self.prompt.strip():
            raise ValueError("run requiere 'messages' no vacío o 'prompt' no vacío")
        for msg in self.messages:
            if not isinstance(msg, dict) or not str(msg.get("content", "")).strip():
                raise ValueError("cada message necesita 'content' no vacío")
            if str(msg.get("role", "")) not in ("system", "user", "assistant"):
                raise ValueError(f"role inválido: {msg.get('role')!r}")
        if not 1 <= self.max_tokens <= 8192:
            raise ValueError("max_tokens debe estar en 1..8192")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature debe estar en 0.0..2.0")
        if len(self.prompt) > _MAX_PROMPT_CHARS:
            raise ValueError(f"prompt excede {_MAX_PROMPT_CHARS} caracteres")


class LlmServerEngine:
    """Servidor LLM local OpenAI-compatible. Contrato de 4 verbos."""

    name = "llm_server"
    tier = EngineTier.WARM

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def list(self) -> EngineResult:
        binary = _find_binary()
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "available": bool(binary),
                "binary": binary,
                "model_available": _model_available(),
                "endpoint": _endpoint(),
                "loopback": is_loopback_url(_endpoint()),
                "openai_compatible": True,
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "el binario llama-server aún no está incluido ni supervisado en la imagen; "
                f"la preparación para {submotor!r} sigue pendiente"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        if self.is_stub():
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="verify",
                payload={"available": True, "stub": True},
            )
        binary = _find_binary()
        if not binary:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                payload={"available": False, "stub": False},
                error=f"binario llama-server ausente ({_BIN_ENV} o PATH)",
            )
        if submotor:
            model = Path(submotor)
            ready = model.is_file()
            return EngineResult(
                ok=ready,
                engine=self.name,
                verb="verify",
                payload={"model": submotor, "model_available": ready},
                error=None if ready else f"modelo ausente: {submotor}",
            )
        if not is_loopback_url(_endpoint()):
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                payload={"available": True, "endpoint": _endpoint()},
                error=f"endpoint no loopback: {_endpoint()} (solo 127.0.0.1/::1/localhost)",
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="verify",
            payload={
                "available": True,
                "binary": binary,
                "model_available": _model_available(),
                "endpoint": _endpoint(),
            },
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            job = spec.job if isinstance(spec.job, dict) else {}
            parsed = ChatSpec(
                messages=list(job.get("messages", [])),
                prompt=str(job.get("prompt", "")),
                model=str(job.get("model", "")),
                max_tokens=int(job.get("max_tokens", 256)),
                temperature=float(job.get("temperature", 0.7)),
                timeout_s=float(job.get("timeout_s", spec.timeout_s or _DEFAULT_TIMEOUT_S)),
            )
            parsed.validate()
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )
        start = time.monotonic()
        if self.is_stub():
            text = self._stub_text(parsed)
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "text": text,
                    "model": parsed.model or "stub",
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_LLM_SERVER: completion simulada",
            )
        binary = _find_binary()
        if not binary:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"llama-server no disponible ({_BIN_ENV} o PATH); usa STUDIO_STUB_LLM_SERVER=1"
                ),
            )
        if not _model_available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"modelo .gguf no disponible ({_MODEL_ENV}); "
                    "descarga el modelo en runtime o usa STUDIO_STUB_LLM_SERVER=1"
                ),
            )
        endpoint = _endpoint()
        if not is_loopback_url(endpoint):
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"endpoint no loopback: {endpoint} (solo 127.0.0.1/::1/localhost)",
            )
        return self._chat(endpoint, parsed, start)

    @staticmethod
    def _stub_text(parsed: ChatSpec) -> str:
        if parsed.messages:
            last = str(parsed.messages[-1].get("content", ""))[:200]
            return f"[stub] eco: {last}"
        return f"[stub] eco: {parsed.prompt[:200]}"

    def _chat(self, endpoint: str, parsed: ChatSpec, start: float) -> EngineResult:
        redactor = SecretRedactor()
        if parsed.messages:
            messages = [{"role": m["role"], "content": m["content"]} for m in parsed.messages]
        else:
            messages = [{"role": "user", "content": parsed.prompt}]
        body = json.dumps(
            {
                "messages": messages,
                "max_tokens": parsed.max_tokens,
                "temperature": parsed.temperature,
                **({"model": parsed.model} if parsed.model else {}),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=parsed.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except TimeoutError:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"llm_server excedió el timeout ({parsed.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo contactar {endpoint}: {exc}"),
            )
        try:
            data = json.loads(raw)
            text = str(data["choices"][0]["message"]["content"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"respuesta no OpenAI-compatible: {exc}"),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "text": text,
                "model": parsed.model or str(data.get("model", "")),
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
