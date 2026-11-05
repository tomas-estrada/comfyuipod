"""Política de retención de caché de pesos (política de arquitectura).

«Choose cache retention deliberately. Retain reusable weights when doing so is
economical; remove replaceable cached weights under a clear policy. Preserve
user media, project state and outputs.»

Reglas, en orden de prioridad:

1. **Nunca** se toca nada fuera de los manifiestos. Solo se considera un activo
   para expulsión si su `dest_path` está declarado en `config/models-core.json`
   o `config/models-studio.json`. Media del usuario, proyectos, salidas,
   bases de datos y venvs quedan fuera por construcción, no por una lista de
   exclusiones que haya que mantener al día.
2. `bootstrap` y `optional` **se conservan siempre**: lo primero es necesario
   para operar y lo segundo lo pidió el operador explícitamente.
3. `task` se conserva mientras quepa. Solo cuando el volumen supera el
   presupuesto se expulsan activos `task`, por orden de último uso real (LRU).
4. El staging (`.hf_stage*`) es parcial reanudable: se limpia solo si lleva
   inactivo más que `staging_max_age_s`, porque borrarlo antes tira el trabajo
   de una descarga interrumpida.

El planificador es puro: devuelve qué haría, sin borrar nada. Quien aplica es
el CLI (`scripts/prune_model_cache.py`), que por defecto no borra.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Rutas que nunca se tocan aunque un manifiesto las declarara por error.
#: Defensa en profundidad: la regla 1 ya las excluye, esto lo hace explícito.
PROTECTED_PREFIXES = (
    "input/",
    "output/",
    "user/",
    "studio/",
    "venvs/",
    "temp/",
)

#: Directorios de staging: parciales reanudables, no activos.
STAGING_DIRNAMES = (".hf_stage", ".hf_stage_phase3", ".hf_stage_capability")

RETENTION_POLICY_VERSION = 1

MANIFEST_NAMES = ("models-core.json", "models-studio.json")

@dataclass(slots=True)
class RetentionPolicy:
    """Política vigente. Versionada para que sea auditable."""

    version: int = RETENTION_POLICY_VERSION
    #: Presupuesto en bytes. None = sin límite (no se expulsa nada).
    budget_bytes: int | None = None
    #: Edad máxima de un parcial de staging antes de limpiarlo.
    staging_max_age_s: int = 7 * 24 * 3600
    #: Clases que nunca se expulsan.
    pinned_classes: tuple[str, ...] = ("bootstrap", "optional")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "budget_bytes": self.budget_bytes,
            "staging_max_age_s": self.staging_max_age_s,
            "pinned_classes": list(self.pinned_classes),
        }


@dataclass(slots=True)
class Eviction:
    """Un activo que la política propone expulsar."""

    filename: str
    dest_path: str
    bytes: int
    asset_class: str
    last_used_ts: float | None
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "dest_path": self.dest_path,
            "bytes": self.bytes,
            "class": self.asset_class,
            "last_used_ts": self.last_used_ts,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RetentionPlan:
    keep_bytes: int = 0
    keep_count: int = 0
    evictions: list[Eviction] = field(default_factory=list)
    staging_cleanups: list[dict[str, Any]] = field(default_factory=list)
    protected_seen: list[str] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(e.bytes for e in self.evictions)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "keep_count": self.keep_count,
            "keep_bytes": self.keep_bytes,
            "freed_bytes": self.freed_bytes,
            "evictions": [e.to_public_dict() for e in self.evictions],
            "staging_cleanups": list(self.staging_cleanups),
            "protected_seen": list(self.protected_seen),
        }


# --------------------------------------------------------------------------
# Ledger de uso real
# --------------------------------------------------------------------------


def usage_ledger_path(env: dict[str, str] | None = None) -> Path:
    """Dónde se guarda el último uso real de cada activo."""
    environment = os.environ if env is None else env
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip()
    if not comfy_data:
        comfy_data = "/workspace/comfyui-data"
    return Path(comfy_data) / ".model-usage.json"


def load_usage(env: dict[str, str] | None = None) -> dict[str, float]:
    path = usage_ledger_path(env)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {str(k): float(v) for k, v in usage.items() if isinstance(v, (int, float))}


def record_use(filenames: list[str], *, env: dict[str, str] | None = None) -> None:
    """Marca activos como usados ahora. Falla en silencio: nunca rompe un render.

    Se llama cuando un activo se descarga y cuando se renderiza la plantilla que
    lo declara, que es la señal de uso real que alimenta el LRU.
    """
    if not filenames:
        return
    path = usage_ledger_path(env)
    usage = load_usage(env)
    now = time.time()
    for name in filenames:
        usage[name] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": RETENTION_POLICY_VERSION, "usage": usage}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        return


# --------------------------------------------------------------------------
# Planificador
# --------------------------------------------------------------------------


def _is_protected(dest: str) -> bool:
    return any(dest.startswith(prefix) or f"/{prefix}" in dest for prefix in PROTECTED_PREFIXES)


def _resolve(dest: str, env: dict[str, str]) -> Path:
    from comfy_studio import model_assets

    return model_assets.resolve_dest(dest, env)


def plan_retention(
    *,
    env: dict[str, str] | None = None,
    policy: RetentionPolicy | None = None,
    now: float | None = None,
) -> RetentionPlan:
    """Calcula qué conservar y qué expulsar. No borra nada."""
    environment = os.environ if env is None else env
    active = policy or RetentionPolicy()
    moment = time.time() if now is None else now
    usage = load_usage(environment)

    plan = RetentionPlan()
    candidates: list[tuple[float, str, Path, int, str]] = []

    for manifest_name in MANIFEST_NAMES:
        path = Path(manifest_name)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in data.get("models", []):
            dest = str(entry.get("dest_path") or "")
            if not dest:
                continue
            if _is_protected(dest):
                plan.protected_seen.append(dest)
                continue
            resolved = _resolve(dest, environment)
            if not (resolved.is_file() and resolved.stat().st_size > 0):
                continue
            size = resolved.stat().st_size
            filename = str(entry.get("filename") or resolved.name)
            asset_class = str(entry.get("class") or "task")
            plan.keep_bytes += size
            plan.keep_count += 1
            if asset_class in active.pinned_classes:
                continue
            last_used = usage.get(filename)
            never_used = -1.0 if last_used is None else float(last_used)
            candidates.append((never_used, filename, resolved, size, asset_class))

    # Sobrepresupuesto: expulsar `task` por LRU (los no usados primero).
    if active.budget_bytes is not None and plan.keep_bytes > active.budget_bytes:
        candidates.sort(key=lambda item: (item[0], item[1]))
        over = plan.keep_bytes - active.budget_bytes
        for last_used, filename, resolved, size, asset_class in candidates:
            if over <= 0:
                break
            plan.evictions.append(
                Eviction(
                    filename=filename,
                    dest_path=str(resolved),
                    bytes=size,
                    asset_class=asset_class,
                    last_used_ts=None if last_used < 0 else last_used,
                    reason="sobre presupuesto, último uso más antiguo",
                )
            )
            plan.keep_bytes -= size
            plan.keep_count -= 1
            over -= size

    # Staging inactivo: parcial reanudable, pero no para siempre.
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip() or "/workspace/comfyui-data"
    models_dir = Path(comfy_data) / "models"
    for name in STAGING_DIRNAMES:
        staging = models_dir / name
        if not staging.is_dir():
            continue
        try:
            age = moment - staging.stat().st_mtime
            size = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())
        except OSError:
            continue
        if age > active.staging_max_age_s:
            plan.staging_cleanups.append(
                {"path": str(staging), "bytes": size, "age_s": age, "reason": "staging inactivo"}
            )

    return plan


def apply_plan(plan: RetentionPlan, *, dry_run: bool = True) -> dict[str, Any]:
    """Ejecuta el plan. Por defecto no borra nada."""
    if dry_run:
        return {"applied": False, "would_free_bytes": plan.freed_bytes}
    removed = 0
    for eviction in plan.evictions:
        try:
            Path(eviction.dest_path).unlink()
            removed += 1
        except OSError:
            continue
    for staging in plan.staging_cleanups:
        import shutil

        try:
            shutil.rmtree(staging["path"], ignore_errors=True)
        except OSError:  # pragma: no cover
            continue
    return {"applied": True, "removed": removed, "freed_bytes": plan.freed_bytes}
