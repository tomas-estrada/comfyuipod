"""Configuración del servidor Comfy Studio y redacción estricta de secretos."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SECRET_PLACEHOLDER = "[REDACTED]"

DEFAULT_STUDIO_LLM_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_STUDIO_LLM_MODEL = "qwen3-vl-plus"
DEFAULT_STUDIO_LLM_FALLBACK_BASE_URL = "https://api.deepseek.com"
DEFAULT_STUDIO_LLM_FALLBACK_MODEL = "deepseek-chat"
DEFAULT_STUDIO_COMFYUI_URL = "http://127.0.0.1:18188"
DEFAULT_STUDIO_SERVER_HOST = "127.0.0.1"
DEFAULT_STUDIO_SERVER_PORT = 18190
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB
DEFAULT_SSE_HEARTBEAT_SECONDS = 15.0

_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{12,}")
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9._\-]{8,}")
_STUDIO_LLM_KEY_ASSIGN_RE = re.compile(r"(?i)(STUDIO_LLM_KEY\s*[:=]\s*)(\S+)")
_COMFY_PASS_ASSIGN_RE = re.compile(r"(?i)(COMFY_PASS\s*[:=]\s*)(\S+)")
_HF_TOKEN_ASSIGN_RE = re.compile(r"(?i)(HF_TOKEN\s*[:=]\s*)(\S+)")
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{6,}")
_URL_USERINFO_RE = re.compile(r"(https?://[^/\s:@]+:)(.*)(@)")


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    return cleaned in {"1", "true", "yes", "on"}


def _default_studio_data_dir(env: Mapping[str, str]) -> Path:
    explicit = env.get("STUDIO_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit)
    comfy_data = env.get("COMFY_DATA_DIR", "").strip()
    if comfy_data:
        return Path(comfy_data) / "studio"
    linux_pod_dir = Path("/workspace/comfyui-data")
    if os.name != "nt" and linux_pod_dir.exists():
        return linux_pod_dir / "studio"
    return Path(tempfile.gettempdir()) / "comfy_studio_data"


class SecretRedactor:
    """Redactor determinista de secretos para logs, diagnósticos y respuestas HTTP."""

    def __init__(
        self,
        *,
        studio_llm_key: str | None = None,
        comfy_pass: str | None = None,
        hf_token: str | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        candidates = [studio_llm_key, comfy_pass, hf_token, *extra_secrets]
        cleaned = sorted(
            {s.strip() for s in candidates if s and len(s.strip()) >= 2},
            key=len,
            reverse=True,
        )
        self._literal_secrets: tuple[str, ...] = tuple(cleaned)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SecretRedactor:
        source = env if env is not None else os.environ
        return cls(
            studio_llm_key=source.get("STUDIO_LLM_KEY"),
            comfy_pass=source.get("COMFY_PASS") or source.get("STUDIO_AUTH_PASS"),
            hf_token=source.get("HF_TOKEN"),
        )

    @classmethod
    def from_config(cls, config: StudioConfig) -> SecretRedactor:
        return cls(
            studio_llm_key=config.get_llm_key(),
            comfy_pass=config.get_comfy_pass(),
        )

    def redact(self, text: str) -> str:
        if not text:
            return text
        out = text
        for secret in self._literal_secrets:
            out = out.replace(secret, SECRET_PLACEHOLDER)
        out = _HF_TOKEN_RE.sub(SECRET_PLACEHOLDER, out)
        out = _SK_TOKEN_RE.sub(SECRET_PLACEHOLDER, out)
        out = _STUDIO_LLM_KEY_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _COMFY_PASS_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _HF_TOKEN_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _BEARER_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _URL_USERINFO_RE.sub(rf"\1{SECRET_PLACEHOLDER}\3", out)
        return out

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(v) for v in value)
        return value


class RedactingLogFilter(logging.Filter):
    """Filtro de logging que redacta mensajes y argumentos antes de emitir."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redactor.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redactor.redact_value(v) for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redactor.redact_value(a) for a in record.args)
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter que aplica SecretRedactor sobre la línea final formateada."""

    def __init__(
        self,
        redactor: SecretRedactor,
        fmt: str | None = None,
        datefmt: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return self._redactor.redact(rendered)


def install_log_redaction(
    redactor: SecretRedactor,
    logger: logging.Logger | None = None,
) -> None:
    """Instala RedactingLogFilter en el logger indicado (o en comfy_studio por defecto)."""
    target = logger if logger is not None else logging.getLogger("comfy_studio")
    # Reemplazar filtros previos de redacción para mantener idempotencia
    target.filters = [
        f for f in target.filters if not isinstance(f, RedactingLogFilter)
    ]
    target.addFilter(RedactingLogFilter(redactor))
    for handler in target.handlers:
        handler.filters = [
            f for f in handler.filters if not isinstance(f, RedactingLogFilter)
        ]
        handler.addFilter(RedactingLogFilter(redactor))


@dataclass
class StudioConfig:
    """Configuración inmutable/segura del servidor Comfy Studio."""

    model_profile: str = "studio"
    studio_llm_base_url: str = DEFAULT_STUDIO_LLM_BASE_URL
    studio_llm_key: str | None = field(default=None, repr=False)
    studio_llm_model: str = DEFAULT_STUDIO_LLM_MODEL
    studio_llm_fallback_base_url: str = DEFAULT_STUDIO_LLM_FALLBACK_BASE_URL
    studio_llm_fallback_model: str = DEFAULT_STUDIO_LLM_FALLBACK_MODEL
    studio_comfyui_url: str = DEFAULT_STUDIO_COMFYUI_URL
    studio_data_dir: Path = field(
        default_factory=lambda: _default_studio_data_dir(os.environ)
    )
    studio_server_host: str = DEFAULT_STUDIO_SERVER_HOST
    studio_server_port: int = DEFAULT_STUDIO_SERVER_PORT
    studio_stub_llm: bool = False
    studio_include_animate: bool = False
    comfy_user: str = "admin"
    comfy_pass: str | None = field(default=None, repr=False)
    require_auth: bool = False
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    sse_heartbeat_seconds: float = DEFAULT_SSE_HEARTBEAT_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.studio_data_dir, str):
            self.studio_data_dir = Path(self.studio_data_dir)
        if self.studio_llm_key is not None:
            cleaned_key = self.studio_llm_key.strip()
            self.studio_llm_key = cleaned_key if cleaned_key else None
        if self.comfy_pass is not None:
            cleaned_pass = self.comfy_pass.strip()
            self.comfy_pass = cleaned_pass if cleaned_pass else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StudioConfig:
        source = env if env is not None else os.environ

        raw_port = source.get("STUDIO_SERVER_PORT", "").strip()
        try:
            port = int(raw_port) if raw_port else DEFAULT_STUDIO_SERVER_PORT
        except ValueError:
            port = DEFAULT_STUDIO_SERVER_PORT

        raw_max_upload = source.get("STUDIO_MAX_UPLOAD_BYTES", "").strip()
        try:
            max_upload = int(raw_max_upload) if raw_max_upload else DEFAULT_MAX_UPLOAD_BYTES
        except ValueError:
            max_upload = DEFAULT_MAX_UPLOAD_BYTES

        raw_hb = source.get("STUDIO_SSE_HEARTBEAT_SECONDS", "").strip()
        try:
            heartbeat_s = float(raw_hb) if raw_hb else DEFAULT_SSE_HEARTBEAT_SECONDS
        except ValueError:
            heartbeat_s = DEFAULT_SSE_HEARTBEAT_SECONDS

        comfy_pass = (
            source.get("COMFY_PASS", "").strip()
            or source.get("STUDIO_AUTH_PASS", "").strip()
            or None
        )
        require_auth_env = _parse_bool(source.get("STUDIO_REQUIRE_AUTH"), default=False)
        require_auth = bool(comfy_pass) or require_auth_env

        return cls(
            model_profile=source.get("MODEL_PROFILE", "studio").strip() or "studio",
            studio_llm_base_url=(
                source.get("STUDIO_LLM_BASE_URL", "").strip()
                or DEFAULT_STUDIO_LLM_BASE_URL
            ),
            studio_llm_key=source.get("STUDIO_LLM_KEY", "").strip() or None,
            studio_llm_model=(
                source.get("STUDIO_LLM_MODEL", "").strip() or DEFAULT_STUDIO_LLM_MODEL
            ),
            studio_llm_fallback_base_url=(
                source.get("STUDIO_LLM_FALLBACK_BASE_URL", "").strip()
                or DEFAULT_STUDIO_LLM_FALLBACK_BASE_URL
            ),
            studio_llm_fallback_model=(
                source.get("STUDIO_LLM_FALLBACK_MODEL", "").strip()
                or DEFAULT_STUDIO_LLM_FALLBACK_MODEL
            ),
            studio_comfyui_url=(
                source.get("STUDIO_COMFYUI_URL", "").strip()
                or DEFAULT_STUDIO_COMFYUI_URL
            ),
            studio_data_dir=_default_studio_data_dir(source),
            studio_server_host=(
                source.get("STUDIO_SERVER_HOST", "").strip()
                or DEFAULT_STUDIO_SERVER_HOST
            ),
            studio_server_port=port,
            studio_stub_llm=_parse_bool(source.get("STUDIO_STUB_LLM"), default=False),
            studio_include_animate=_parse_bool(
                source.get("STUDIO_INCLUDE_ANIMATE"), default=False
            ),
            comfy_user=(
                source.get("COMFY_USER", "").strip()
                or source.get("STUDIO_AUTH_USER", "").strip()
                or "admin"
            ),
            comfy_pass=comfy_pass,
            require_auth=require_auth,
            max_upload_bytes=max_upload,
            sse_heartbeat_seconds=heartbeat_s,
        )

    def get_llm_key(self) -> str | None:
        return self.studio_llm_key

    def get_comfy_pass(self) -> str | None:
        return self.comfy_pass

    def masked_llm_key_status(self) -> str:
        return "<definida>" if self.studio_llm_key else "<no definida>"

    def masked_comfy_pass_status(self) -> str:
        return "<definida>" if self.comfy_pass else "<no definida>"

    def redactor(self) -> SecretRedactor:
        return SecretRedactor.from_config(self)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "MODEL_PROFILE": self.model_profile,
            "STUDIO_LLM_BASE_URL": self.studio_llm_base_url,
            "STUDIO_LLM_KEY": self.masked_llm_key_status(),
            "STUDIO_LLM_MODEL": self.studio_llm_model,
            "STUDIO_LLM_FALLBACK_BASE_URL": self.studio_llm_fallback_base_url,
            "STUDIO_LLM_FALLBACK_MODEL": self.studio_llm_fallback_model,
            "STUDIO_COMFYUI_URL": self.studio_comfyui_url,
            "STUDIO_DATA_DIR": str(self.studio_data_dir),
            "STUDIO_SERVER_PORT": str(self.studio_server_port),
            "STUDIO_STUB_LLM": "1" if self.studio_stub_llm else "0",
            "STUDIO_INCLUDE_ANIMATE": "1" if self.studio_include_animate else "0",
            "COMFY_USER": self.comfy_user,
            "COMFY_PASS": self.masked_comfy_pass_status(),
        }
