"""Adquisición de activos de modelo bajo demanda (política de arquitectura).

El resolvedor de capacidades dice **qué falta**; este módulo lo trae usando la
**herramienta de descarga existente** (el CLI `hf`/`huggingface-cli`), con las
mismas reglas que `scripts/download-models.sh`:

  - reutiliza lo que ya está en disco (skip si el destino existe y no está vacío);
  - comprueba espacio libre por intento y falla cerrado si no se puede leer;
  - staging en el mismo filesystem que el destino, con `rename()` atómico;
  - 3 intentos con backoff 2s → 5s → 11s y timeout por intento;
  - distingue timeout (rc 124) de fallo real;
  - admite progreso y cancelación entre intentos.

No selecciona modelos ni decide políticas: ejecuta un plan que le dan.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GiB = 1024**3

DEFAULT_HF_FETCH_TIMEOUT_S = 3600
DEFAULT_DISK_MARGIN_BYTES = 10 * GiB
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 5, 11)

#: Directorio de staging aislado: los parciales deben sobrevivir a la limpieza
#: de las fases 1/2/3 (`.hf_stage`, `.hf_stage_phase3`).
STAGE_DIRNAME = ".hf_stage_capability"


def resolve_hf_cli(env: dict[str, str] | None = None) -> str | None:
    """`HF_CLI` explícito, si no `hf` y finalmente `huggingface-cli`."""
    environment = os.environ if env is None else env
    explicit = (environment.get("HF_CLI") or "").strip()
    if explicit:
        return explicit
    for candidate in ("hf", "huggingface-cli"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def default_stage_dir(env: dict[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip()
    if not comfy_data:
        comfy_data = "/workspace/comfyui-data"
    return Path(comfy_data) / "models" / STAGE_DIRNAME


@dataclass(slots=True)
class AcquiredEntry:
    filename: str
    dest_path: str
    status: str  # "skipped" | "downloaded" | "failed"
    bytes: int = 0
    attempts: int = 0
    error: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "dest_path": self.dest_path,
            "status": self.status,
            "bytes": self.bytes,
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass(slots=True)
class AcquisitionReport:
    entries: list[AcquiredEntry] = field(default_factory=list)
    hf_cli: str | None = None
    stage_dir: str = ""
    cancelled: bool = False

    @property
    def downloaded_bytes(self) -> int:
        return sum(e.bytes for e in self.entries if e.status == "downloaded")

    @property
    def failed(self) -> list[AcquiredEntry]:
        return [e for e in self.entries if e.status == "failed"]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "hf_cli": self.hf_cli,
            "stage_dir": self.stage_dir,
            "cancelled": self.cancelled,
            "downloaded_bytes": self.downloaded_bytes,
            "failed": [e.filename for e in self.failed],
            "entries": [e.to_public_dict() for e in self.entries],
        }


def _free_bytes(path: Path) -> int | None:
    """Espacio libre real del filesystem que contiene `path` (None si ilegible)."""
    try:
        target = path if path.exists() else path.parent
        if not target.exists():
            target = Path("/")
        return shutil.disk_usage(target).free
    except OSError:
        return None


def acquire(
    needs: list[Any],
    *,
    env: dict[str, str] | None = None,
    hf_cli: str | None = None,
    stage_dir: Path | None = None,
    timeout_s: int | None = None,
    margin_bytes: int | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    progress: Callable[[str, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> AcquisitionReport:
    """Descarga los activos que faltan. `needs` son `AssetNeed` del resolvedor."""
    environment = os.environ if env is None else env
    cli = hf_cli or resolve_hf_cli(env)
    stage = stage_dir or default_stage_dir(env)
    timeout = timeout_s or int(
        float(environment.get("HF_FETCH_TIMEOUT") or DEFAULT_HF_FETCH_TIMEOUT_S)
    )
    margin = margin_bytes or int(
        float(environment.get("HF_DISK_MARGIN_BYTES") or DEFAULT_DISK_MARGIN_BYTES)
    )
    guard_enabled = (environment.get("HF_DISK_GUARD") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )

    report = AcquisitionReport(hf_cli=cli, stage_dir=str(stage))
    if cli is None:
        for need in needs:
            report.entries.append(
                AcquiredEntry(
                    filename=need.filename,
                    dest_path=need.dest_path,
                    status="failed",
                    error="sin cliente de HuggingFace (hf/huggingface-cli) disponible",
                )
            )
        return report

    for need in needs:
        if should_cancel is not None and should_cancel():
            report.cancelled = True
            break
        report.entries.append(
            _acquire_one(
                need,
                cli=cli,
                stage=stage,
                timeout=timeout,
                margin=margin,
                max_attempts=max_attempts,
                guard_enabled=guard_enabled,
                progress=progress,
            )
        )
    return report


def _acquire_one(
    need: Any,
    *,
    cli: str,
    stage: Path,
    timeout: int,
    margin: int,
    max_attempts: int,
    guard_enabled: bool,
    progress: Callable[[str, str], None] | None,
) -> AcquiredEntry:
    dest = Path(need.dest_path) if need.dest_path else None
    if dest is not None and dest.is_file() and dest.stat().st_size > 0:
        if progress:
            progress(need.filename, "skipped")
        return AcquiredEntry(
            filename=need.filename,
            dest_path=str(dest),
            status="skipped",
            bytes=dest.stat().st_size,
        )

    if not need.repo_id or not need.remote_path or dest is None:
        return AcquiredEntry(
            filename=need.filename,
            dest_path=need.dest_path,
            status="failed",
            error="activo sin origen declarado en el manifiesto",
        )

    expected = int(need.bytes or 0)
    staged = stage / need.remote_path
    # Un destino relativo se resolvería contra el CWD y podría escribir dentro
    # del repositorio: solo se aceptan rutas absolutas.
    if not dest.is_absolute():
        return AcquiredEntry(
            filename=need.filename,
            dest_path=str(dest),
            status="failed",
            error=f"destino no absoluto (se rechaza por seguridad): {dest}",
        )
    backoff_index = 0

    for attempt in range(1, max_attempts + 1):
        if guard_enabled:
            free = _free_bytes(stage)
            if free is None:
                return AcquiredEntry(
                    filename=need.filename,
                    dest_path=str(dest),
                    status="failed",
                    attempts=attempt,
                    error="espacio libre ilegible (fail-closed)",
                )
            if free < expected + margin:
                return AcquiredEntry(
                    filename=need.filename,
                    dest_path=str(dest),
                    status="failed",
                    attempts=attempt,
                    error=(
                        f"espacio insuficiente: {free / GiB:.1f} GiB libres, "
                        f"se necesitan {(expected + margin) / GiB:.1f} GiB"
                    ),
                )

        if progress:
            progress(need.filename, f"downloading:{attempt}")
        try:
            completed = subprocess.run(  # noqa: S603
                [
                    cli,
                    "download",
                    str(need.repo_id),
                    str(need.remote_path),
                    "--repo-type",
                    str(need.repo_type or "model"),
                    "--local-dir",
                    str(stage),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
        except OSError as exc:
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="failed",
                attempts=attempt,
                error=f"no se pudo ejecutar {cli}: {exc}",
            )

        if returncode == 0 and staged.is_file() and staged.stat().st_size > 0:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, dest)
            if progress:
                progress(need.filename, "downloaded")
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="downloaded",
                bytes=dest.stat().st_size,
                attempts=attempt,
            )

        staged.unlink(missing_ok=True)
        if attempt < max_attempts:
            delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
            backoff_index += 1
            time.sleep(delay)

    return AcquiredEntry(
        filename=need.filename,
        dest_path=str(dest),
        status="failed",
        attempts=max_attempts,
        error="timeout" if returncode == 124 else f"fallo del cliente (rc={returncode})",
    )
