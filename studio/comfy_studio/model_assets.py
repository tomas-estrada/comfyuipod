"""Activos de modelo como dependencias de tarea (política de arquitectura).

La política distingue tres clases de activo y prohíbe tratar una colección
grande de modelos como «bootstrap» solo porque un motor la soporte:

  - `bootstrap`: genuinamente necesarios para la operación básica del pod;
  - `task`: se descartan a demanda para la tarea seleccionada;
  - `optional`: variantes y mejoras opcionales.

Este módulo es la **fuente de verdad en Python** de `config/models-studio.json`
(el manifiesto versionado de Fase 3): resuelve su ruta igual que
`scripts/download-models.sh`, expone las entradas por clase y por grupo opt-in,
y comprueba la presencia real en disco. No descarga nada: solo lee y hace `stat`.

Con ello, «disponible» deja de ser una variable de entorno y pasa a ser un
hecho del filesystem: `STUDIO_INCLUDE_ANIMATE=1` significa *se pidió*, y los
pesos están disponibles solo cuando los archivos existen.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Clases de activo admitidas por la política.
CLASS_BOOTSTRAP = "bootstrap"
CLASS_TASK = "task"
CLASS_OPTIONAL = "optional"
VALID_CLASSES = (CLASS_BOOTSTRAP, CLASS_TASK, CLASS_OPTIONAL)

#: Variables opt-in del manifiesto -> nombre de grupo lógico.
OPT_IN_FLAGS = {
    "STUDIO_INCLUDE_ANIMATE": "animate",
    "STUDIO_INCLUDE_3D": "three_d",
    "STUDIO_INCLUDE_MINIMAX": "minimax",
}

#: Defaults de rutas cuando el entorno no las define (coinciden con la imagen).
_DEFAULT_ENV = {
    "COMFY_APP_DIR": "/opt/ComfyUI",
    "COMFY_DATA_DIR": "/workspace/comfyui-data",
}

#: Rutas candidatas del manifiesto, en el mismo orden que download-models.sh.
_MANIFEST_CANDIDATES = (
    "{STUDIO_MANIFEST}",
    "config/models-studio.json",
    "/etc/comfyui/models-studio.json",
)


def manifest_search_bases(module_file: str | None = None) -> tuple[Path, Path]:
    """Bases ordenadas contra las que se resuelve un manifiesto **relativo**.

    Una sola regla, compartida por `model_assets.manifest_path()`,
    `capabilities.core_manifest_path()` y `cache_policy._manifest_candidates()`:
    cada uno tenía la suya y podían acabar leyendo catálogos distintos. El
    planificador de retención resolvía solo contra la raíz del paquete; los otros
    dos probaban antes el directorio actual, así que desde un CWD con su propio
    `config/models-core.json` el resolvedor leía un manifiesto y el planificador
    otro.

    El orden es **raíz del paquete primero, CWD después**. La raíz del paquete es
    la fuente versionada (`config/models-*.json` viaja con el repo), mientras que
    el CWD es solo el sitio desde el que se invocó el proceso: si el CWD mandara,
    `scripts/prune_model_cache.py --json` lanzado desde un directorio con su
    propio `config/` planificaría contra un catálogo que nadie versionó, que es
    justo el modo de fallo que la resolución contra la raíz del paquete vino a
    corregir. El CWD se conserva como **último** recurso para la imagen, donde la
    raíz del paquete es `site-packages` y no hay `config/` sobre ella.

    `module_file` deja pasar el `__file__` del llamador (los tests simulan
    layouts de instalación); por omisión es el de este módulo, que vive en el
    mismo paquete que los otros dos.
    """
    root = Path(module_file or __file__).resolve().parents[2]
    try:
        cwd = Path.cwd()
    except OSError:  # pragma: no cover - CWD borrado en caliente
        cwd = root
    return (root, cwd)


def expand_placeholders(value: str, env: dict[str, str]) -> str:
    """Expande `{COMFY_APP_DIR}`/`{COMFY_DATA_DIR}` y `~` en una ruta.

    Pública porque la usa también `capabilities.core_manifest_path()`: mientras
    era `_expand`, dos módulos quedaban acoplados por un nombre privado que
    ningún linter protege, y renombrarlo rompía en silencio la resolución del
    manifiesto core.
    """
    out = value
    for key, default in _DEFAULT_ENV.items():
        out = out.replace("{" + key + "}", env.get(key, default))
    return os.path.expanduser(out)


#: Alias histórico del helper, ya público. Se mantiene para no romper a nadie
#: que lo importara por su nombre anterior.
_expand = expand_placeholders


def resolve_dest(dest: str, env: dict[str, str] | None = None) -> Path:
    """Resuelve un `dest_path` del manifiesto a ruta absoluta.

    Mismas reglas que `scripts/download-models.sh` (`case "${target_dir}"`):
    `models/*` vive bajo `${COMFY_DATA_DIR}/models`, `custom_nodes/*` bajo
    `${COMFY_APP_DIR}`, `${COMFY_DATA_DIR}/*` se expande, lo absoluto se respeta
    y cualquier otra cosa cae bajo `${COMFY_DATA_DIR}`.
    """
    environment = os.environ if env is None else env
    raw = _expand(str(dest), environment)
    # Un COMFY_DATA_DIR vacío produciría rutas relativas (y por tanto escrituras
    # dentro del repo): cae siempre al default de la imagen.
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip() or _DEFAULT_ENV["COMFY_DATA_DIR"]
    comfy_app = (environment.get("COMFY_APP_DIR") or "").strip() or _DEFAULT_ENV["COMFY_APP_DIR"]
    if raw.startswith("/"):
        return Path(raw)
    if raw == "models" or raw.startswith("models/"):
        if raw == "models":
            return Path(comfy_data) / "models"
        return Path(comfy_data) / "models" / raw[len("models/") :]
    if raw.startswith("custom_nodes/"):
        return Path(comfy_app) / raw
    return Path(comfy_data) / raw


def manifest_path(env: dict[str, str] | None = None) -> Path | None:
    """Resuelve la ruta del manifiesto (primero la explícita, luego las conocidas).

    Las relativas se resuelven contra las bases compartidas
    (`manifest_search_bases()`): la raíz del paquete manda sobre el directorio
    actual, igual que en `capabilities.core_manifest_path()` y en
    `cache_policy._manifest_candidates()`. Antes el CWD iba primero, así que un
    proceso lanzado desde un directorio con su propio `config/` leía un catálogo
    distinto del que leen los otros dos consumidores.
    """
    environment = os.environ if env is None else env
    for candidate in _MANIFEST_CANDIDATES:
        raw = environment.get("STUDIO_MANIFEST") if candidate == "{STUDIO_MANIFEST}" else candidate
        if not raw:
            continue
        path = Path(expand_placeholders(raw, environment))
        if path.is_absolute():
            if path.is_file():
                return path
            continue
        for base in manifest_search_bases(__file__):
            resolved = base / path
            if resolved.is_file():
                return resolved
    return None


@lru_cache(maxsize=1)
def _load(path_str: str) -> dict[str, Any]:
    with open(path_str, encoding="utf-8") as handle:
        return json.load(handle)


def load_manifest(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Carga el manifiesto, o None si no existe (modo dev sin config/)."""
    path = manifest_path(env)
    if path is None:
        return None
    try:
        return _load(str(path))
    except (OSError, ValueError):  # pragma: no cover - manifiesto corrupto
        return None


def entries(env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    manifest = load_manifest(env)
    if not manifest:
        return []
    return [e for e in manifest.get("models", []) if isinstance(e, dict)]


def entry_class(entry: dict[str, Any]) -> str:
    """Clase de un activo. Por defecto `task`: nada es bootstrap sin declararlo."""
    declared = str(entry.get("class") or "").strip().lower()
    return declared if declared in VALID_CLASSES else CLASS_TASK


def entries_for_flag(flag: str, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Entradas de un grupo opt-in (p. ej. `STUDIO_INCLUDE_ANIMATE`)."""
    return [e for e in entries(env) if e.get("opt_in") == flag]


def paths_for_flag(flag: str, env: dict[str, str] | None = None) -> list[Path]:
    """Rutas absolutas en disco de un grupo opt-in."""
    environment = os.environ if env is None else env
    out: list[Path] = []
    for entry in entries_for_flag(flag, env):
        dest = entry.get("dest_path")
        if not dest:
            continue
        out.append(resolve_dest(str(dest), environment))
    return out


def flag_present(flag: str, env: dict[str, str] | None = None) -> bool:
    """True si todos los archivos del grupo existen y no están vacíos.

    Un grupo vacío (manifiesto ausente) devuelve False: sin manifiesto no se
    puede afirmar que los pesos están.
    """
    paths = paths_for_flag(flag, env)
    if not paths:
        return False
    return all(p.is_file() and p.stat().st_size > 0 for p in paths)


def missing_for_flag(flag: str, env: dict[str, str] | None = None) -> list[str]:
    """Nombres de archivo que faltan o están vacíos en un grupo opt-in."""
    out: list[str] = []
    for path in paths_for_flag(flag, env):
        if not (path.is_file() and path.stat().st_size > 0):
            out.append(path.name)
    return out


def class_counts(env: dict[str, str] | None = None) -> dict[str, int]:
    counts = {CLASS_BOOTSTRAP: 0, CLASS_TASK: 0, CLASS_OPTIONAL: 0}
    for entry in entries(env):
        counts[entry_class(entry)] += 1
    return counts


def class_bytes(env: dict[str, str] | None = None) -> dict[str, int]:
    """Bytes por clase. Un `bytes` ilegible cuenta 0, nunca aborta.

    R94/D4: `int()` sobre `Infinity`/`-Infinity`/`1e400` responde
    `OverflowError` —un `ArithmeticError`, que no es ni `TypeError` ni
    `ValueError`—, y `json.load` acepta esos literales no estándar, así que un
    manifiesto de operario con `"bytes": Infinity` hacía reventar `class_bytes`
    y con ella `summary()`, que está cableada sin protección en
    `routers/core.py` (`/studio/api/status`). Misma regla que
    `capabilities._declared_bytes` y `asset_acquire._declared_bytes`: degradar a
    0 y seguir.
    """
    totals = {CLASS_BOOTSTRAP: 0, CLASS_TASK: 0, CLASS_OPTIONAL: 0}
    for entry in entries(env):
        try:
            size = int(entry.get("bytes") or 0)
        except (TypeError, ValueError, OverflowError):
            size = 0
        totals[entry_class(entry)] += size
    return totals


def summary(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Resumen serializable para `/studio/api/status` y documentación."""
    manifest = load_manifest(env)
    counts = class_counts(env)
    totals = class_bytes(env)
    meta = (manifest or {}).get("meta", {})
    return {
        "manifest": str(manifest_path(env)) if manifest else None,
        "version": (manifest or {}).get("version"),
        "entries": len(entries(env)),
        "default_entries": meta.get("default_entries"),
        "default_bytes": meta.get("default_bytes"),
        "opt_in_entries": meta.get("opt_in_entries"),
        "opt_in_bytes": meta.get("opt_in_bytes"),
        "classes": {
            name: {"entries": counts[name], "bytes": totals[name]}
            for name in VALID_CLASSES
        },
        "opt_in_groups": {
            group: {
                "flag": flag,
                "present": flag_present(flag, env),
                "missing": missing_for_flag(flag, env),
            }
            for flag, group in OPT_IN_FLAGS.items()
        },
    }
