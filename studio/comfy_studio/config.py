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

from comfy_studio import model_assets
from comfy_studio.hardware_profile import (
    DEFAULT_PROFILE,
    RAM_OPTIONS_GB,
    HardwareProfile,
)
from comfy_studio.hardware_profile import (
    from_env as _hardware_profile_from_env,
)

SECRET_PLACEHOLDER = "[REDACTED]"

DEFAULT_STUDIO_LLM_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_STUDIO_LLM_MODEL = "qwen3-vl-plus"
DEFAULT_STUDIO_LLM_FALLBACK_BASE_URL = "https://api.deepseek.com"
DEFAULT_STUDIO_LLM_FALLBACK_MODEL = "deepseek-chat"
DEFAULT_STUDIO_COMFYUI_URL = "http://127.0.0.1:18188"
DEFAULT_STUDIO_SERVER_HOST = "127.0.0.1"
DEFAULT_STUDIO_SERVER_PORT = 18190
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB
DEFAULT_UPLOAD_MAX_TOTAL_MB = 1024  # M1.7: techo del total de la petición multipart
DEFAULT_SSE_HEARTBEAT_SECONDS = 15.0
DEFAULT_STUDIO_RESUME_DELAY_S = 30.0
DEFAULT_STUDIO_RECOVERY_DRAIN_S = 10.0
# M1.5: los timeouts de ffmpeg/ffprobe estaban fijados en código (60 s / 20 s / 10 s),
# lo que hacía fallar cualquier EDL con clips largos. Ahora son configurables.
DEFAULT_STUDIO_FFMPEG_TIMEOUT_S = 60.0
DEFAULT_STUDIO_FFPROBE_TIMEOUT_S = 20.0
DEFAULT_STUDIO_INGEST_MAX_ASSETS = 1000
DEFAULT_STUDIO_INGEST_MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024
# M7-SCR-001: techo de renders en segundo plano ejecutándose a la vez contra ComfyUI
# (ComfyUI serializa la ejecución, pero cada tarea en vuelo retiene su sondeo WS/poll).
DEFAULT_MAX_CONCURRENT_RENDERS = 2

_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]{12,}")
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9._\-]{8,}")
# Asignacion de credencial por NOMBRE de variable. Espejo de rx_studio_env en
# comfyctl/src/redact.rs: misma lista de nombres, mismo separador flexible
# (opcional comilla, luego : o =) y mismo valor (entre comillas con escapes,
# o corrida sin espacios). Antes habia 9 regex sueltas que solo cubrian los
# nombres de Studio: AWS_SECRET_ACCESS_KEY=... salia EN CLARO porque su valor
# contiene / y no casa con ninguna forma de clave. Una sola regex evita que
# las dos implementaciones vuelvan a divergir.
# OJO con la clase del valor sin comillas: excluye comillas, coma y
# delimitadores JSON. Con \S+ (lo que usa el redactor de Rust) el valor se
# come el cierre de la cadena y json.loads revienta con "Expecting ','
# delimiter". Rust puede permitirse \S+ porque solo corre sobre texto de
# log; aqui no, porque se aplica al cuerpo JSON serializado.
_ENV_ASSIGN_RE = re.compile(
    r"(?i)(?:\b)((?:STUDIO_LLM_(?:FALLBACK_)?KEY|STUDIO_API_TOKEN|STUDIO_WEBHOOK_SECRET|VAST_API_KEY|COMFY_PASS|STUDIO_AUTH_PASS|HF_TOKEN|STUDIO_HEAVY_VIDEO_KEY|AWS_SECRET_ACCESS_KEY|AWS_SECRET_KEY|AWS_ACCESS_KEY_ID|GITHUB_TOKEN|SLACK_TOKEN|OPENAI_API_KEY|GOOGLE_API_KEY|ANTHROPIC_API_KEY)"
    r"\s*\"?\s*[:=]\s*)(\"(?:[^\"\\]|\\.)*\"|[^@\s\"'\[\]{},]*)"
)


#: Placeholders que el propio redactor y to_public_dict emiten. No deben
#: enmascararse a si mismos: /status/api publica STUDIO_LLM_KEY: "<definida>" y
#: el contrato exige que ese marcador sobreviva.
_REDACTION_PLACEHOLDERS = frozenset(
    {"<definida>", "<no definida>", "<definido>", "<no definido>"}
)


def _mask_env_assign(match):
    """Re-emite el nombre y enmascara el valor, preservando las comillas.

    La rama entre comillas se re-emite entre comillas para no romper el JSON
    de /status/api (mismo criterio que el redactor de Rust). Los placeholders
    propios se dejan intactos.
    """
    value = match.group(2)
    if value.strip('"') in _REDACTION_PLACEHOLDERS:
        return match.group(0)
    if value.startswith('"'):
        return f'{match.group(1)}"[REDACTED]"'
    return f"{match.group(1)}[REDACTED]"


_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{6,}")
# Paridad con `comfyctl/src/redact.rs`: sin estos, una credencial en una cadena de
# excepción o en un cuerpo de error de proveedor llegaba al cliente. El redactor de
# Rust sí los tiene, así que los dos no deben divergir.
_AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:basic|bearer|token|apikey|key|jwt|pat|digest)\s+)"
    r"[A-Za-z0-9._\-+/=]{8,}"
)
# Esquema genérico (ftp://, postgres://, mongodb://...): anclarse solo a https?
# dejaba la clave en claro en cualquier log que no usara http.
# OJO: la clase de la clave excluye los caracteres estructurales de JSON. El
# redactor de Rust usa `[^\s]*`, válido allí porque solo corre sobre texto de
# log; aquí se aplica al cuerpo JSON serializado, que el cliente vuelve a
# parsear, así que una clase permisiva se come los delimitadores y devuelve
# "Extra data" (mismo fallo que tuvo `_URL_USERINFO_RE`).
_URL_AUTH_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+:)([^@\s\"'\[\]{}]*)(@)")
# Forma sin esquema (`user:pass@host`, proxies y .netrc).
_URL_AUTH_SCHEMELESS_RE = re.compile(
    r"(^|[\s\"'(\[\{=,])([A-Za-z0-9._~%+\-]+):([^@\s\"'\[\]{}/@?#&=]+)@"
)
# Claves de nube/proveedores por su FORMA: no hacen falta literales.
_AWS_KEY_ID_RE = re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")
_CLOUD_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35,}")
_VCS_TOKEN_RE = re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,254}")
_SLACK_TOKEN_RE = re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,248}")
# userinfo de una URL (`https://usuario:clave@host`). El grupo central se limita a
# caracteres que NO puedan ser estructura JSON (comillas, llaves, corchetes): con
# `.*` o `[^@\s]*` la sustitución se pasaba de los delimitadores y devolvía un
# cuerpo JSON inválido ("Extra data") en cuanto el texto posterior contenía una `@`.
_URL_USERINFO_RE = re.compile(r"(https?://[^/\s:@]+:)([^@\s\"'\[\]{}]*)(@)")

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
    # Patrones añadidos para paridad con `comfyctl/src/redact.rs`. Sin su
    # contrapartida en vuelo, un credencial partida entre dos chunks de un
    # stream SSE se emitía en claro: el buffer solo retiene lo que coincide con
    # uno de estos, así que `redact()` y el stream no pueden divergir.
    re.compile(r"(?i)^STUDIO_HEAVY_VIDEO_KEY(?:\s*|\s*[:=]\s*\S*)$"),
    re.compile(r"(?i)^Authorization\s*[:=]\s*(?:basic|bearer|token|apikey|key|jwt|pat|digest)?\s*[A-Za-z0-9._\-+/=]*$"),
    re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]+:[^@\s\"'\[\]{}]*$"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]*$"),
    re.compile(r"AIza[0-9A-Za-z_\-]*$"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]*$"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]*$"),
    re.compile(r"^https?://[^@\s\"'\[\]{}]*$"),
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
                    source.get("STUDIO_HEAVY_VIDEO_KEY"),
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
                    # Clave del motor heavy_video: se declara aquí para que el
                    # valor literal también se enmascare (un proveedor puede
                    # repetirla en su cuerpo de error).
                    os.environ.get("STUDIO_HEAVY_VIDEO_KEY"),
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
        out = _ENV_ASSIGN_RE.sub(_mask_env_assign, out)
        out = _BEARER_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _AUTHORIZATION_RE.sub(rf"\1{SECRET_PLACEHOLDER}", out)
        out = _URL_AUTH_RE.sub(rf"\1{SECRET_PLACEHOLDER}\3", out)
        # La forma sin esquema se descarta si es un esquema tipo mailto:/sip:,
        # igual que hace el redactor de Rust. OJO: hay que mirar `group(2)` (el
        # usuario), NO `group(0)`: group(0) incluye el delimitador inicial
        # obligatorio (espacio/comilla/`(`/`[`/`{`/`=`/`,`), así que
        # `startswith("mailto:")` solo era cierto en la posición 0 y todo
        # `mailto:` incrustado en un valor JSON perdía su parte local.
        _SCHEMELESS_EXEMPT = (
            "mailto", "tel", "sip", "sips", "xmpp", "callto", "ldap", "news", "urn",
        )
        out = _URL_AUTH_SCHEMELESS_RE.sub(
            lambda m: m.group(0)
            if m.group(2).lower() in _SCHEMELESS_EXEMPT
            else f"{m.group(1)}{m.group(2)}:{SECRET_PLACEHOLDER}@",
            out,
        )
        out = _AWS_KEY_ID_RE.sub(SECRET_PLACEHOLDER, out)
        out = _CLOUD_API_KEY_RE.sub(SECRET_PLACEHOLDER, out)
        out = _VCS_TOKEN_RE.sub(SECRET_PLACEHOLDER, out)
        out = _SLACK_TOKEN_RE.sub(SECRET_PLACEHOLDER, out)
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
    """Instala RedactingLogFilter en el logger indicado (o en comfy_studio por defecto).

    También cubre `logging.lastResort`, que es por donde salen realmente los
    registros de `comfy_studio.*`: uvicorn NO configura handlers en el logger
    raíz, así que `Logger.callHandlers` no encuentra ninguno al subir por la
    jerarquía y el registro cae en el `_StderrHandler` de último recurso — sin
    pasar por ningún filtro de logger ancestro (esos solo se consultan en el
    logger de origen). Sin esto, un `getLogger("comfy_studio.x").warning(...)`
    con un secreto llegaba a `/var/log/studio-server.log` en claro.
    """
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
    # `lastResort` es un singleton de proceso: se filtra una sola vez.
    if logging.lastResort is not None:
        logging.lastResort.filters = [
            f for f in logging.lastResort.filters if not isinstance(f, RedactingLogFilter)
        ]
        logging.lastResort.addFilter(RedactingLogFilter(redactor))


@dataclass
class StudioConfig:
    """Configuración inmutable/segura del servidor Comfy Studio."""

    model_profile: str = "studio"
    studio_llm_base_url: str = DEFAULT_STUDIO_LLM_BASE_URL
    studio_llm_key: str | None = field(default=None, repr=False)
    studio_llm_model: str = DEFAULT_STUDIO_LLM_MODEL
    studio_llm_supports_tools: bool = True
    studio_llm_supports_streaming: bool = True
    studio_llm_fallback_base_url: str = DEFAULT_STUDIO_LLM_FALLBACK_BASE_URL
    studio_llm_fallback_key: str | None = field(default=None, repr=False)
    studio_llm_fallback_model: str = DEFAULT_STUDIO_LLM_FALLBACK_MODEL
    studio_llm_fallback_supports_tools: bool = True
    studio_llm_fallback_supports_streaming: bool = True
    studio_comfyui_url: str = DEFAULT_STUDIO_COMFYUI_URL
    studio_data_dir: Path = field(
        default_factory=lambda: _default_studio_data_dir(os.environ)
    )
    studio_server_host: str = DEFAULT_STUDIO_SERVER_HOST
    studio_server_port: int = DEFAULT_STUDIO_SERVER_PORT
    studio_stub_llm: bool = False
    studio_include_animate: bool = False
    studio_include_3d: bool = False
    studio_include_minimax: bool = False
    comfy_user: str = "admin"
    comfy_pass: str | None = field(default=None, repr=False)
    vast_api_key: str | None = field(default=None, repr=False)
    require_auth: bool = False
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    upload_max_total_bytes: int = DEFAULT_UPLOAD_MAX_TOTAL_MB * 1024 * 1024
    sse_heartbeat_seconds: float = DEFAULT_SSE_HEARTBEAT_SECONDS
    studio_webhook_url: str | None = None
    studio_webhook_secret: str | None = field(default=None, repr=False)
    studio_resume_jobs: bool = True
    studio_resume_delay_s: float = DEFAULT_STUDIO_RESUME_DELAY_S
    studio_recovery_drain_s: float = DEFAULT_STUDIO_RECOVERY_DRAIN_S
    # Reconciliación periódica en vivo de jobs vs /queue+/history de ComfyUI (segundos;
    # 0 desactiva). Detecta prompts perdidos tras un reinicio de ComfyUI (OOM 137).
    studio_reconcile_interval_s: float = 30.0
    studio_ffmpeg_timeout_s: float = DEFAULT_STUDIO_FFMPEG_TIMEOUT_S
    studio_ffprobe_timeout_s: float = DEFAULT_STUDIO_FFPROBE_TIMEOUT_S
    studio_ingest_max_assets: int = DEFAULT_STUDIO_INGEST_MAX_ASSETS
    studio_ingest_max_total_bytes: int = DEFAULT_STUDIO_INGEST_MAX_TOTAL_BYTES
    max_concurrent_renders: int = DEFAULT_MAX_CONCURRENT_RENDERS
    # Política (puntos 3 y 5): descargar modelos de VRAM cuando no queda trabajo.
    unload_models_when_idle: bool = True
    # Arquitectura vigente: perfil de hardware (RTX PRO 6000 Blackwell, 96 GB de
    # VRAM; 128/256 GB son preferencias de búsqueda de RAM, no límites de runtime).
    # En orquestación real, app.py sustituye RAM por el límite medido del pod.
    # `target_vram_gb` se mantiene como proyección del perfil por compatibilidad.
    hardware_profile: HardwareProfile = DEFAULT_PROFILE
    target_vram_gb: int = DEFAULT_PROFILE.vram_gb
    target_ram_gb: int = DEFAULT_PROFILE.ram_gb

    @property
    def hardware_profile_name(self) -> str:
        return self.hardware_profile.name

    @property
    def ram_options_gb(self) -> tuple[int, ...]:
        return RAM_OPTIONS_GB
    studio_api_token: str | None = field(default=None, repr=False)
    studio_enable_mcp: bool = True
    # M3.1: protocolo de workers de ML en subprocesso (ComfyUI interpreter).
    studio_ml_python_bin: str = "python3"
    studio_ml_timeout_s: float = 120.0
    # M3.2: backend de percepción: auto (probe al arranque), real (sin fallback), sim.
    studio_perception_backend: str = "auto"

    def __post_init__(self) -> None:
        self.studio_server_host = sanitize_loopback_host(self.studio_server_host)
        if isinstance(self.studio_data_dir, str):
            self.studio_data_dir = Path(self.studio_data_dir)
        if self.studio_llm_key is not None:
            cleaned_key = self.studio_llm_key.strip()
            self.studio_llm_key = cleaned_key or None
        if self.studio_llm_fallback_key is not None:
            cleaned_fb_key = self.studio_llm_fallback_key.strip()
            self.studio_llm_fallback_key = cleaned_fb_key or None
        if self.comfy_pass is not None:
            cleaned_pass = self.comfy_pass.strip()
            self.comfy_pass = cleaned_pass or None
        if self.vast_api_key is not None:
            cleaned_vast = self.vast_api_key.strip()
            self.vast_api_key = cleaned_vast or None
        if self.studio_webhook_url is not None:
            cleaned_wh_url = self.studio_webhook_url.strip()
            self.studio_webhook_url = cleaned_wh_url or None
        if self.studio_webhook_secret is not None:
            cleaned_wh_secret = self.studio_webhook_secret.strip()
            self.studio_webhook_secret = cleaned_wh_secret or None
        if self.studio_api_token is not None:
            cleaned_token = self.studio_api_token.strip()
            self.studio_api_token = cleaned_token or None

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

        def _positive_float(env_key: str, default: float) -> float:
            raw = source.get(env_key, "").strip()
            try:
                value = float(raw) if raw else default
            except ValueError:
                return default
            return value if value > 0 else default

        def _positive_int(env_key: str, default: int) -> int:
            raw = source.get(env_key, "").strip()
            try:
                value = int(raw) if raw else default
            except ValueError:
                return default
            return value if value > 0 else default

        upload_total_mb = _positive_int(
            "STUDIO_UPLOAD_MAX_TOTAL_MB", DEFAULT_UPLOAD_MAX_TOTAL_MB
        )

        raw_reconcile = source.get("STUDIO_RECONCILE_INTERVAL_S", "").strip()
        try:
            reconcile_interval = float(raw_reconcile) if raw_reconcile else 30.0
        except ValueError:
            reconcile_interval = 30.0
        if reconcile_interval < 0:
            reconcile_interval = 30.0

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
            studio_llm_supports_tools=_parse_bool(
                source.get("STUDIO_LLM_SUPPORTS_TOOLS"), default=True
            ),
            studio_llm_supports_streaming=_parse_bool(
                source.get("STUDIO_LLM_SUPPORTS_STREAMING"), default=True
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
            studio_llm_fallback_supports_tools=_parse_bool(
                source.get("STUDIO_LLM_FALLBACK_SUPPORTS_TOOLS"), default=True
            ),
            studio_llm_fallback_supports_streaming=_parse_bool(
                source.get("STUDIO_LLM_FALLBACK_SUPPORTS_STREAMING"), default=True
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
            studio_include_minimax=_parse_bool(
                source.get("STUDIO_INCLUDE_MINIMAX"), default=False
            ),
            comfy_user=(
                source.get("COMFY_USER", "").strip()
                or source.get("STUDIO_AUTH_USER", "").strip()
                or "admin"
            ),
            comfy_pass=comfy_pass,
            require_auth=require_auth,
            max_upload_bytes=max_upload,
            upload_max_total_bytes=upload_total_mb * 1024 * 1024,
            sse_heartbeat_seconds=heartbeat_s,
            studio_webhook_url=source.get("STUDIO_WEBHOOK_URL", "").strip() or None,
            studio_webhook_secret=source.get("STUDIO_WEBHOOK_SECRET", "").strip() or None,
            studio_resume_jobs=_parse_bool(source.get("STUDIO_RESUME_JOBS"), default=True),
            studio_resume_delay_s=resume_delay_s,
            studio_reconcile_interval_s=reconcile_interval,
            studio_recovery_drain_s=_positive_float(
                "STUDIO_RECOVERY_DRAIN_S", DEFAULT_STUDIO_RECOVERY_DRAIN_S
            ),
            studio_ffmpeg_timeout_s=_positive_float(
                "STUDIO_FFMPEG_TIMEOUT_S", DEFAULT_STUDIO_FFMPEG_TIMEOUT_S
            ),
            studio_ffprobe_timeout_s=_positive_float(
                "STUDIO_FFPROBE_TIMEOUT_S", DEFAULT_STUDIO_FFPROBE_TIMEOUT_S
            ),
            studio_ingest_max_assets=_positive_int(
                "STUDIO_INGEST_MAX_ASSETS", DEFAULT_STUDIO_INGEST_MAX_ASSETS
            ),
            studio_ingest_max_total_bytes=_positive_int(
                "STUDIO_INGEST_MAX_TOTAL_BYTES", DEFAULT_STUDIO_INGEST_MAX_TOTAL_BYTES
            ),
            max_concurrent_renders=_positive_int(
                "STUDIO_MAX_CONCURRENT_RENDERS", DEFAULT_MAX_CONCURRENT_RENDERS
            ),
            unload_models_when_idle=_parse_bool(
                source.get("STUDIO_UNLOAD_WHEN_IDLE"), default=True
            ),
            # El perfil es la fuente; los overrides explícitos se aplican dentro
            # de `_hardware_profile_from_env`, así que sus valores ya vienen
            # reflejados y se usan como valor por omisión.
            hardware_profile=_hardware_profile_from_env(source),
            target_vram_gb=_positive_int(
                "STUDIO_TARGET_VRAM_GB", _hardware_profile_from_env(source).vram_gb
            ),
            target_ram_gb=_positive_int(
                "STUDIO_TARGET_RAM_GB", _hardware_profile_from_env(source).ram_gb
            ),
            studio_api_token=source.get("STUDIO_API_TOKEN", "").strip() or None,
            studio_enable_mcp=_parse_bool(source.get("STUDIO_ENABLE_MCP"), default=True),
            # M3.1: binario Python del worker (ComfyUI interpreter por defecto).
            studio_ml_python_bin=(
                source.get("STUDIO_ML_PYTHON_BIN", "").strip() or "python3"
            ),
            # M3.1: timeout por defecto para llamadas al worker ML (segundos).
            studio_ml_timeout_s=_positive_float(
                "STUDIO_ML_TIMEOUT_S", 120.0
            ),
            # M3.2: backend de percepción: auto | real | sim.
            studio_perception_backend=(
                source.get("STUDIO_PERCEPTION_BACKEND", "").strip() or "auto"
            ),
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

    def minimax_weights_expected(self) -> bool:
        """
        ¿Se pidió el grupo MiniMax H3 (Hailuo 3.0)?

        `STUDIO_INCLUDE_MINIMAX=1` hace que `download-models.sh` descargue el
        set local Ref2VA pruned-int8 (+63.00 GB en 6 archivos: DiT ref2va +
        Singularity v2, Qwen3-VL nvfp4, VAEs de vídeo/audio y LoRA turbo); sin
        él la plantilla `minimax_r2v` no se ofrece ni acepta submits.

        Es una señal de **intención**: no afirma que los archivos estén en disco.
        """
        return bool(self.studio_include_minimax)

    def minimax_weights_available(self, env: Mapping[str, str] | None = None) -> bool:
        """¿Están los pesos de MiniMax H3 realmente en disco?

        Política de activos: «disponible» es un hecho del filesystem, no de la
        entorno. Un grupo pedido pero aún descargando NO está disponible.
        """
        return self.minimax_weights_expected() and model_assets.flag_present(
            "STUDIO_INCLUDE_MINIMAX", env
        )

    def animate_weights_expected(self) -> bool:
        """
        ¿Se pidió el grupo Animate (Wan 2.2 Animate 14B)?

        `STUDIO_INCLUDE_ANIMATE=1` hace que `download-models.sh` descargue
        `wan2.2_animate_14B_int8_convrot.safetensors` (+18.41 GB); sin él la
        plantilla `wan_animate` no se ofrece ni acepta submits.
        """
        return bool(self.studio_include_animate)

    def animate_weights_available(self, env: Mapping[str, str] | None = None) -> bool:
        """¿Está `wan2.2_animate_14B_int8_convrot.safetensors` en disco?"""
        return self.animate_weights_expected() and model_assets.flag_present(
            "STUDIO_INCLUDE_ANIMATE", env
        )

    def three_d_weights_expected(self) -> bool:
        """¿Se pidió el grupo 3D (Hunyuan3D) en este pod?

        `STUDIO_INCLUDE_3D=1` o `MODEL_PROFILE=unreal` hacen que
        `download-models.sh` descargue `hunyuan_3d_v2.1.safetensors` (+7.37 GB);
        sin ellos la plantilla `hunyuan3d_image_to_3d` no es ofrecida ni acepta
        submits.
        """
        return bool(self.studio_include_3d)

    def three_d_weights_available(self, env: Mapping[str, str] | None = None) -> bool:
        """¿Está `hunyuan_3d_v2.1.safetensors` en disco?"""
        return self.three_d_weights_expected() and model_assets.flag_present(
            "STUDIO_INCLUDE_3D", env
        )

    def redactor(self) -> SecretRedactor:
        return SecretRedactor.from_config(self)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "MODEL_PROFILE": self.model_profile,
            "STUDIO_LLM_BASE_URL": self.studio_llm_base_url,
            "STUDIO_LLM_KEY": self.masked_llm_key_status(),
            "STUDIO_LLM_MODEL": self.studio_llm_model,
            "STUDIO_LLM_SUPPORTS_TOOLS": "1" if self.studio_llm_supports_tools else "0",
            "STUDIO_LLM_SUPPORTS_STREAMING": (
                "1" if self.studio_llm_supports_streaming else "0"
            ),
            "STUDIO_LLM_FALLBACK_BASE_URL": self.studio_llm_fallback_base_url,
            "STUDIO_LLM_FALLBACK_KEY": self.masked_llm_fallback_key_status(),
            "STUDIO_LLM_FALLBACK_MODEL": self.studio_llm_fallback_model,
            "STUDIO_LLM_FALLBACK_SUPPORTS_TOOLS": (
                "1" if self.studio_llm_fallback_supports_tools else "0"
            ),
            "STUDIO_LLM_FALLBACK_SUPPORTS_STREAMING": (
                "1" if self.studio_llm_fallback_supports_streaming else "0"
            ),
            "STUDIO_COMFYUI_URL": self.studio_comfyui_url,
            "STUDIO_DATA_DIR": str(self.studio_data_dir),
            "STUDIO_SERVER_PORT": str(self.studio_server_port),
            "STUDIO_STUB_LLM": "1" if self.studio_stub_llm else "0",
            "STUDIO_INCLUDE_ANIMATE": "1" if self.studio_include_animate else "0",
            "STUDIO_INCLUDE_3D": "1" if self.studio_include_3d else "0",
            "STUDIO_INCLUDE_MINIMAX": "1" if self.studio_include_minimax else "0",
            "COMFY_USER": self.comfy_user,
            "COMFY_PASS": self.masked_comfy_pass_status(),
            "VAST_API_KEY": "<definida>" if self.vast_api_key else "<no definida>",
            "STUDIO_WEBHOOK_URL": "<definida>" if self.studio_webhook_url else "<no definida>",
            "STUDIO_WEBHOOK_SECRET": (
                "<definida>" if self.studio_webhook_secret else "<no definida>"
            ),
            "STUDIO_RESUME_JOBS": "1" if self.studio_resume_jobs else "0",
            "STUDIO_RESUME_DELAY_S": str(self.studio_resume_delay_s),
            "STUDIO_RECOVERY_DRAIN_S": str(self.studio_recovery_drain_s),
            "STUDIO_FFMPEG_TIMEOUT_S": str(self.studio_ffmpeg_timeout_s),
            "STUDIO_FFPROBE_TIMEOUT_S": str(self.studio_ffprobe_timeout_s),
            "STUDIO_INGEST_MAX_ASSETS": str(self.studio_ingest_max_assets),
            "STUDIO_INGEST_MAX_TOTAL_BYTES": str(self.studio_ingest_max_total_bytes),
            "STUDIO_MAX_CONCURRENT_RENDERS": str(self.max_concurrent_renders),
            "STUDIO_UNLOAD_WHEN_IDLE": "1" if self.unload_models_when_idle else "0",
            "STUDIO_HARDWARE_PROFILE": str(self.hardware_profile_name),
            "STUDIO_TARGET_VRAM_GB": str(self.target_vram_gb),
            "STUDIO_TARGET_RAM_GB": str(self.target_ram_gb),
            "STUDIO_UPLOAD_MAX_TOTAL_MB": str(self.upload_max_total_bytes // (1024 * 1024)),
            "STUDIO_API_TOKEN": "<definido>" if self.studio_api_token else "<no definido>",
            "STUDIO_ENABLE_MCP": "1" if self.studio_enable_mcp else "0",
            "STUDIO_ML_PYTHON_BIN": self.studio_ml_python_bin,
            "STUDIO_ML_TIMEOUT_S": str(self.studio_ml_timeout_s),
            "STUDIO_PERCEPTION_BACKEND": self.studio_perception_backend,
        }
