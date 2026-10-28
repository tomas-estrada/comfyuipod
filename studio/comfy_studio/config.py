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
DEFAULT_STUDIO_RESUME_DELAY_S = 30.0

_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{12,}")
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9._\-]{8,}")
_STUDIO_LLM_KEY_ASSIGN_RE = re.compile(r"(?i)(STUDIO_LLM_(?:FALLBACK_)?KEY\s*[:=]\s*)(\S+)")
_COMFY_PASS_ASSIGN_RE = re.compile(r"(?i)(COMFY_PASS\s*[:=]\s*)(\S+)")
_STUDIO_AUTH_PASS_ASSIGN_RE = re.compile(r"(?i)(STUDIO_AUTH_PASS\s*[:=]\s*)(\S+)")
_HF_TOKEN_ASSIGN_RE = re.compile(r"(?i)(HF_TOKEN\s*[:=]\s*)(\S+)")
_STUDIO_API_TOKEN_ASSIGN_RE = re.compile(r"(?i)(STUDIO_API_TOKEN\s*[:=]\s*)(\S+)")
_STUDIO_WEBHOOK_SECRET_ASSIGN_RE = re.compile(r"(?i)(STUDIO_WEBHOOK_SECRET\s*[:=]\s*)(\S+)")
_VAST_API_KEY_ASSIGN_RE = re.compile(r"(?i)(VAST_API_KEY\s*[:=]\s*)(\S+)")
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{6,}")
_URL_USERINFO_RE = re.compile(r"(https?://[^/\s:@]+:)(.*)(@)")

_CASE_SENSITIVE_STREAM_PREFIXES: tuple[str, ...] = (
    "hf_",
    "sk-",
    "http://",
    "https://",
)
_CASE_INSENSITIVE_STREAM_PREFIXES: tuple[str, ...] = (
    "bearer ",
    "studio_llm_key",
    "studio_llm_fallback_key",
    "comfy_pass",
    "studio_auth_pass",
    "hf_token",
    "studio_api_token",
    "studio_webhook_secret",
    "vast_api_key",
)
_INFLIGHT_STREAM_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^hf_[A-Za-z0-9]*$"),
    re.compile(r"^sk-[A-Za-z0-9._\-]*$"),
    re.compile(r"(?i)^Bearer\s+[A-Za-z0-9._\-]*$"),
    re.compile(r"(?i)^STUDIO_LLM_(?:FALLBACK_)?KEY(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^COMFY_PASS(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^STUDIO_AUTH_PASS(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^HF_TOKEN(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^STUDIO_API_TOKEN(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^STUDIO_WEBHOOK_SECRET(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^VAST_API_KEY(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"^https?://[^/\s@]*$"),
)



def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    return cleaned in {"1", "true", "yes", "on"}


def sanitize_loopback_host(host: str | None) -> str:
    """Fuerza binding exclusivo a loopback 127.0.0.1 (Regla 2 de AGENTS.md)."""
    if not host:
        return DEFAULT_STUDIO_SERVER_HOST
    cleaned = host.strip()
    if cleaned in {"127.0.0.1", "localhost"} or cleaned.startswith("127."):
        return cleaned
    return DEFAULT_STUDIO_SERVER_HOST


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
        studio_llm_fallback_key: str | None = None,
        comfy_pass: str | None = None,
        hf_token: str | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> None:
        candidates = [
            studio_llm_key,
            studio_llm_fallback_key,
            comfy_pass,
            hf_token,
            *extra_secrets,
        ]
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
            studio_llm_fallback_key=source.get("STUDIO_LLM_FALLBACK_KEY"),
            comfy_pass=source.get("COMFY_PASS") or source.get("STUDIO_AUTH_PASS"),
            hf_token=source.get("HF_TOKEN"),
            extra_secrets=[
                s
                for s in (
                    source.get("STUDIO_WEBHOOK_SECRET"),
                    source.get("STUDIO_API_TOKEN"),
                    source.get("VAST_API_KEY"),
                )
                if s
            ],
        )

    @classmethod
    def from_config(cls, config: StudioConfig) -> SecretRedactor:
        return cls(
            studio_llm_key=config.get_llm_key(),
            studio_llm_fallback_key=config.get_llm_fallback_key(),
            comfy_pass=config.get_comfy_pass(),
            hf_token=os.environ.get("HF_TOKEN"),
            extra_secrets=[
                s
                for s in (
                    config.get_webhook_secret(),
                    config.get_api_token(),
                    config.get_vast_api_key(),
                )
                if s
            ],
        )

    @property
    def literal_secrets(self) -> tuple[str, ...]:
        return self._literal_secrets

    def create_stream(self) -> SecretStreamRedactor:
        """Crea un redactor incremental con retención en fronteras de chunk."""
        return SecretStreamRedactor(self)

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
        out = _STUDIO_AUTH_PASS_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _HF_TOKEN_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _STUDIO_API_TOKEN_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _STUDIO_WEBHOOK_SECRET_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _VAST_API_KEY_ASSIGN_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
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


class SecretStreamRedactor:
    """Redactor con estado para streams de tokens (SSE y stub guionizado).

    Retiene únicamente el sufijo en vuelo que coincida con un prefijo parcial
    de algún secreto literal o patrón sensible y emite de inmediato el prefijo
    seguro, garantizando que secretos partidos entre 2+ chunks nunca se filtren
    sin sacrificar la entrega incremental del stream.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._buffer = ""

    def _is_proper_literal_prefix(self, candidate: str) -> bool:
        for secret in self._redactor.literal_secrets:
            if len(secret) > len(candidate) and secret.startswith(candidate):
                return True
        return False

    @staticmethod
    def _is_inflight_regex_suffix(candidate: str) -> bool:
        if candidate:
            for kw in _CASE_SENSITIVE_STREAM_PREFIXES:
                if len(kw) > len(candidate) and kw.startswith(candidate):
                    return True
            lower_cand = candidate.lower()
            for kw in _CASE_INSENSITIVE_STREAM_PREFIXES:
                if len(kw) > len(lower_cand) and kw.startswith(lower_cand):
                    return True
        for pattern in _INFLIGHT_STREAM_REGEXES:
            if pattern.match(candidate):
                return True
        return False

    def _redact_completed_literals_in_buffer(self) -> None:
        if not self._buffer:
            return
        for secret in self._redactor.literal_secrets:
            search_pos = 0
            while True:
                idx = self._buffer.find(secret, search_pos)
                if idx < 0:
                    break
                tail = self._buffer[idx:]
                if self._is_proper_literal_prefix(tail) or self._is_inflight_regex_suffix(tail):
                    search_pos = idx + 1
                    continue
                self._buffer = (
                    self._buffer[:idx]
                    + SECRET_PLACEHOLDER
                    + self._buffer[idx + len(secret) :]
                )
                search_pos = idx + len(SECRET_PLACEHOLDER)

    def _find_holdback_index(self) -> int:
        buf = self._buffer
        n = len(buf)
        for k in range(n):
            suffix = buf[k:]
            if self._is_proper_literal_prefix(suffix) or self._is_inflight_regex_suffix(suffix):
                return k
        return n

    def push(self, piece: str) -> str:
        """Procesa un nuevo fragmento del stream y devuelve la parte segura lista para emitir."""
        if not piece:
            return ""
        self._buffer += piece
        self._redact_completed_literals_in_buffer()
        hold_from = self._find_holdback_index()
        if hold_from <= 0:
            return ""
        ready = self._buffer[:hold_from]
        self._buffer = self._buffer[hold_from:]
        return self._redactor.redact(ready)

    def flush(self) -> str:
        """Vacía y redacta cualquier remanente retenido al finalizar el stream."""
        if not self._buffer:
            return ""
        remaining = self._buffer
        self._buffer = ""
        return self._redactor.redact(remaining)



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
    studio_llm_fallback_key: str | None = field(default=None, repr=False)
    studio_llm_fallback_model: str = DEFAULT_STUDIO_LLM_FALLBACK_MODEL
    studio_comfyui_url: str = DEFAULT_STUDIO_COMFYUI_URL
    studio_data_dir: Path = field(
        default_factory=lambda: _default_studio_data_dir(os.environ)
    )
    studio_server_host: str = DEFAULT_STUDIO_SERVER_HOST
    studio_server_port: int = DEFAULT_STUDIO_SERVER_PORT
    studio_stub_llm: bool = False
    studio_include_animate: bool = False
    studio_include_3d: bool = False
    comfy_user: str = "admin"
    comfy_pass: str | None = field(default=None, repr=False)
    vast_api_key: str | None = field(default=None, repr=False)
    require_auth: bool = False
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    sse_heartbeat_seconds: float = DEFAULT_SSE_HEARTBEAT_SECONDS
    studio_webhook_url: str | None = None
    studio_webhook_secret: str | None = field(default=None, repr=False)
    studio_resume_jobs: bool = True
    studio_resume_delay_s: float = DEFAULT_STUDIO_RESUME_DELAY_S
    studio_api_token: str | None = field(default=None, repr=False)
    studio_enable_mcp: bool = True

    def __post_init__(self) -> None:
        self.studio_server_host = sanitize_loopback_host(self.studio_server_host)
        if isinstance(self.studio_data_dir, str):
            self.studio_data_dir = Path(self.studio_data_dir)
        if self.studio_llm_key is not None:
            cleaned_key = self.studio_llm_key.strip()
            self.studio_llm_key = cleaned_key if cleaned_key else None
        if self.studio_llm_fallback_key is not None:
            cleaned_fb_key = self.studio_llm_fallback_key.strip()
            self.studio_llm_fallback_key = cleaned_fb_key if cleaned_fb_key else None
        if self.comfy_pass is not None:
            cleaned_pass = self.comfy_pass.strip()
            self.comfy_pass = cleaned_pass if cleaned_pass else None
        if self.vast_api_key is not None:
            cleaned_vast = self.vast_api_key.strip()
            self.vast_api_key = cleaned_vast if cleaned_vast else None
        if self.studio_webhook_url is not None:
            cleaned_wh_url = self.studio_webhook_url.strip()
            self.studio_webhook_url = cleaned_wh_url if cleaned_wh_url else None
        if self.studio_webhook_secret is not None:
            cleaned_wh_secret = self.studio_webhook_secret.strip()
            self.studio_webhook_secret = cleaned_wh_secret if cleaned_wh_secret else None
        if self.studio_api_token is not None:
            cleaned_token = self.studio_api_token.strip()
            self.studio_api_token = cleaned_token if cleaned_token else None

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

        raw_resume_delay = source.get("STUDIO_RESUME_DELAY_S", "").strip()
        try:
            resume_delay_s = (
                float(raw_resume_delay) if raw_resume_delay else DEFAULT_STUDIO_RESUME_DELAY_S
            )
        except ValueError:
            resume_delay_s = DEFAULT_STUDIO_RESUME_DELAY_S

        comfy_pass = (
            source.get("COMFY_PASS", "").strip()
            or source.get("STUDIO_AUTH_PASS", "").strip()
            or None
        )
        require_auth_env = _parse_bool(source.get("STUDIO_REQUIRE_AUTH"), default=False)
        require_auth = bool(comfy_pass) or require_auth_env

        # `MODEL_PROFILE=unreal` descarga el grupo 3D por defecto; un
        # STUDIO_INCLUDE_3D=0 explícito puede renunciar a él.
        profile = source.get("MODEL_PROFILE", "studio").strip() or "studio"
        include_3d_default = profile.strip().lower() == "unreal"

        return cls(
            model_profile=profile,
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
            studio_llm_fallback_key=(
                source.get("STUDIO_LLM_FALLBACK_KEY", "").strip() or None
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
            studio_server_host=sanitize_loopback_host(source.get("STUDIO_SERVER_HOST")),
            studio_server_port=port,
            studio_stub_llm=_parse_bool(source.get("STUDIO_STUB_LLM"), default=False),
            studio_include_animate=_parse_bool(
                source.get("STUDIO_INCLUDE_ANIMATE"), default=False
            ),
            studio_include_3d=_parse_bool(
                source.get("STUDIO_INCLUDE_3D"), default=include_3d_default
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
            studio_webhook_url=source.get("STUDIO_WEBHOOK_URL", "").strip() or None,
            studio_webhook_secret=source.get("STUDIO_WEBHOOK_SECRET", "").strip() or None,
            studio_resume_jobs=_parse_bool(source.get("STUDIO_RESUME_JOBS"), default=True),
            studio_resume_delay_s=resume_delay_s,
            studio_api_token=source.get("STUDIO_API_TOKEN", "").strip() or None,
            studio_enable_mcp=_parse_bool(source.get("STUDIO_ENABLE_MCP"), default=True),
            vast_api_key=source.get("VAST_API_KEY", "").strip() or None,
        )

    def get_llm_key(self) -> str | None:
        return self.studio_llm_key

    def get_llm_fallback_key(self) -> str | None:
        return self.studio_llm_fallback_key

    def get_comfy_pass(self) -> str | None:
        return self.comfy_pass

    def get_webhook_secret(self) -> str | None:
        return self.studio_webhook_secret

    def get_api_token(self) -> str | None:
        return self.studio_api_token

    def get_vast_api_key(self) -> str | None:
        return self.vast_api_key

    def masked_llm_key_status(self) -> str:
        return "<definida>" if self.studio_llm_key else "<no definida>"

    def masked_llm_fallback_key_status(self) -> str:
        return "<definida>" if self.studio_llm_fallback_key else "<no definida>"

    def masked_comfy_pass_status(self) -> str:
        return "<definida>" if self.comfy_pass else "<no definida>"

    def three_d_weights_available(self) -> bool:
        """¿Están previstos los pesos del grupo 3D (Hunyuan3D) en este pod?

        `STUDIO_INCLUDE_3D=1` o `MODEL_PROFILE=unreal` hacen que
        `download-models.sh` descargue `hunyuan_3d_v2.1.safetensors`; sin ellos la
        plantilla `hunyuan3d_image_to_3d` no es ofrecida ni acepta submits.
        """
        return bool(self.studio_include_3d)

    def redactor(self) -> SecretRedactor:
        return SecretRedactor.from_config(self)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "MODEL_PROFILE": self.model_profile,
            "STUDIO_LLM_BASE_URL": self.studio_llm_base_url,
            "STUDIO_LLM_KEY": self.masked_llm_key_status(),
            "STUDIO_LLM_MODEL": self.studio_llm_model,
            "STUDIO_LLM_FALLBACK_BASE_URL": self.studio_llm_fallback_base_url,
            "STUDIO_LLM_FALLBACK_KEY": self.masked_llm_fallback_key_status(),
            "STUDIO_LLM_FALLBACK_MODEL": self.studio_llm_fallback_model,
            "STUDIO_COMFYUI_URL": self.studio_comfyui_url,
            "STUDIO_DATA_DIR": str(self.studio_data_dir),
            "STUDIO_SERVER_PORT": str(self.studio_server_port),
            "STUDIO_STUB_LLM": "1" if self.studio_stub_llm else "0",
            "STUDIO_INCLUDE_ANIMATE": "1" if self.studio_include_animate else "0",
            "STUDIO_INCLUDE_3D": "1" if self.studio_include_3d else "0",
            "COMFY_USER": self.comfy_user,
            "COMFY_PASS": self.masked_comfy_pass_status(),
            "VAST_API_KEY": "<definida>" if self.vast_api_key else "<no definida>",
            "STUDIO_WEBHOOK_URL": "<definida>" if self.studio_webhook_url else "<no definida>",
            "STUDIO_WEBHOOK_SECRET": (
                "<definida>" if self.studio_webhook_secret else "<no definida>"
            ),
            "STUDIO_RESUME_JOBS": "1" if self.studio_resume_jobs else "0",
            "STUDIO_RESUME_DELAY_S": str(self.studio_resume_delay_s),
            "STUDIO_API_TOKEN": "<definido>" if self.studio_api_token else "<no definido>",
            "STUDIO_ENABLE_MCP": "1" if self.studio_enable_mcp else "0",
        }
