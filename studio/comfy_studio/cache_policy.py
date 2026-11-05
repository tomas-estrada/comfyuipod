"""Política de retención de caché de pesos (política de arquitectura).

«Choose cache retention deliberately. Retain reusable weights when doing so is
economical; remove replaceable cached weights under a clear policy. Preserve
user media, project state and outputs.»

Reglas, en orden de prioridad:

1. **Nunca** se toca nada fuera de los manifiestos. Solo se considera un activo
   para expulsión si su `dest_path` está declarado en uno de los manifiestos
   declarantes (`config/models-core.json` y `config/models-studio.json`, o la
   ruta que el despliegue señale con `STUDIO_CORE_MANIFEST`/`STUDIO_MANIFEST`).
   Media del usuario, proyectos, salidas, bases de datos y venvs quedan fuera
   por construcción, no por una lista de exclusiones que haya que mantener al
   día.
2. `bootstrap` y `optional` **se conservan siempre**: lo primero es necesario
   para operar y lo segundo lo pidió el operador explícitamente.
3. `task` se conserva mientras quepa. Solo cuando el volumen supera el
   presupuesto se expulsan activos `task`, por orden de último uso real (LRU).
4. El staging (`.hf_stage*`) es parcial reanudable: se limpia solo si lleva
   inactivo más que `staging_max_age_s`, porque borrarlo antes tira el trabajo
   de una descarga interrumpida.

El planificador es puro: devuelve qué haría, sin borrar nada. Quien aplica es
el CLI (`scripts/prune_model_cache.py`), que por defecto no borra.

El plan declara además qué manifiestos leyó (`manifests`, `manifests_found`) y
cuáles estaban ilegibles (`unreadable_manifests`), para que un plan vacío por
falta de declaración no se confunda con uno vacío porque la declaración no se
pudo leer. Las dos cosas se leen igual en `keep_count` y muy distinto en
`--json`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comfy_studio import model_assets

LOG = logging.getLogger(__name__)

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

#: Nombres de los manifiestos declarantes. Los relativos se resuelven contra las
#: bases compartidas (`model_assets.manifest_search_bases()`): la raíz del
#: paquete primero, y el directorio actual solo como último recurso.
MANIFEST_NAMES = ("models-core.json", "models-studio.json")

#: Variable de entorno que apunta a un manifiesto explícito, **por catálogo**.
#: Manda sobre las rutas conocidas, igual que en `model_assets.manifest_path()`.
#:
#: Cada catálogo tiene la suya y no se comparten: `STUDIO_MANIFEST` es, en todo
#: el sistema, el override del manifiesto de Fase 3 / studio (`model_assets`,
#: `capabilities`, `download-models.sh`, documentación). Usarla también como
#: override del core hacía que fijarla —para lo que está documentado— dejase
#: fuera del planificador a `models-core.json`: los ~68 GB del catálogo de
#: Fases 1/2 desaparecían de `keep_bytes` y ningún activo core era candidato a
#: expulsión, mientras `capabilities` seguía leyendo el core real. Los dos
#: componentes disentían sobre qué catálogo estaba en vigor. De ahí que el core
#: tenga variable propia (`STUDIO_CORE_MANIFEST`), que es la que ya leen
#: `capabilities._CORE_MANIFEST_CANDIDATES` y `download-models.sh`.
MANIFEST_ENV_VARS = {
    "models-core.json": "STUDIO_CORE_MANIFEST",
    "models-studio.json": "STUDIO_MANIFEST",
}

#: Override por defecto para un nombre que no esté en la tabla anterior (p. ej.
#: un manifiesto pasado por ruta absoluta, como hacen los tests): el de Fase 3,
#: que es el histórico de `model_assets.manifest_path()`.
MANIFEST_ENV_VAR = "STUDIO_MANIFEST"

#: Directorios candidatos relativos, en el mismo orden que el descargador: el
#: árbol de fuentes (`config/`), el propio directorio raíz y el horneado en la
#: imagen (`/etc/comfyui/`).
#:
#: Se resuelven contra las bases de `model_assets.manifest_search_bases()` (raíz
#: del paquete primero, CWD después), **no** solo contra el CWD: el comentario
#: anterior decía que la entrada vacía era «el CWD por compatibilidad», pero el
#: código la multiplicaba por la raíz del paquete y nunca miraba el directorio
#: actual.
_RELATIVE_MANIFEST_BASES = ("config", "", "/etc/comfyui")


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
    #: Manifiestos localizados y leídos. Con la lista vacía el plan vacío
    #: significa «nada declarado», no «el volumen está bien».
    manifests: list[str] = field(default_factory=list)
    #: Manifiestos localizados pero ilegibles: se descartan **diciéndolo**. Un
    #: plan vacío por un manifiesto roto no es un plan vacío sano.
    unreadable_manifests: list[str] = field(default_factory=list)
    #: Overrides explícitos (`STUDIO_CORE_MANIFEST`/`STUDIO_MANIFEST`) que no son
    #: ningún fichero: ese catálogo queda **fuera** del plan. Declararlo es la
    #: diferencia entre «el volumen está corto por un catálogo que no se leyó» y
    #: «el volumen está sano» (R42).
    missing_overrides: list[str] = field(default_factory=list)

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
            "manifests": list(self.manifests),
            "manifests_found": len(self.manifests),
            "unreadable_manifests": list(self.unreadable_manifests),
            "missing_overrides": list(self.missing_overrides),
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


def _override_for(name: str, env: dict[str, str]) -> tuple[str, str]:
    """Variable de override de un catálogo y su valor, ya recortado."""
    var = MANIFEST_ENV_VARS.get(Path(name).name, MANIFEST_ENV_VAR)
    return var, (env.get(var) or "").strip()


def _manifest_candidates(name: str, env: dict[str, str]) -> list[Path]:
    """Dónde puede estar un manifiesto, en orden de prioridad."""
    _var, explicit = _override_for(name, env)
    if explicit:
        return [Path(os.path.expanduser(explicit))]
    if Path(name).is_absolute():
        return [Path(name)]
    # Las bases son las compartidas con `model_assets.manifest_path()` y
    # `capabilities.core_manifest_path()` (`manifest_search_bases()`), no una
    # lista propia: con listas distintas, el planificador y el resolvedor podían
    # leer catálogos diferentes sin que ninguno lo dijera (R43).
    candidates: list[Path] = []
    for base in model_assets.manifest_search_bases(__file__):
        for relative in _RELATIVE_MANIFEST_BASES:
            if relative.startswith("/"):
                candidate = Path(relative) / name
            else:
                candidate = base / relative / name
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


@dataclass(slots=True)
class ManifestResolution:
    """Qué manifiestos se leyeron y qué overrides no resolvieron (R42)."""

    #: Manifiestos localizados, en el orden de `MANIFEST_NAMES`.
    paths: list[Path] = field(default_factory=list)
    #: Overrides explícitos que no son ningún fichero. El catálogo correspondiente
    #: queda fuera del plan, y eso tiene que leerse en el resultado.
    missing_overrides: list[str] = field(default_factory=list)
    #: Rutas candidatas examinadas, para el aviso de «nada declarado».
    searched: list[Path] = field(default_factory=list)


def resolve_manifests(env: dict[str, str] | None = None) -> ManifestResolution:
    """Resuelve los manifiestos declarantes, catálogo por catálogo (R42).

    Un override explícito que no es un fichero ya no puede hacer desaparecer un
    catálogo en silencio: si `STUDIO_CORE_MANIFEST` apunta a una ruta que no
    existe, el plan se emitía con ese catálogo simplemente ausente —
    `manifests_found: 1`, `unreadable_manifests: []` y un `keep_bytes` que se lee
    como un volumen sano corto por los ~68 GB del core. Ahorre se registra en
    `missing_overrides` y se avisa por log, que es lo que distingue «falta un
    catálogo» de «el volumen está bien».
    """
    environment = os.environ if env is None else env
    resolution = ManifestResolution()
    for name in MANIFEST_NAMES:
        var, explicit = _override_for(name, environment)
        candidates = _manifest_candidates(name, environment)
        resolution.searched.extend(candidates)
        # Un catálogo está en un solo sitio: la primera candidata que exista. Si
        # se recogieran todas, un mismo catálogo podría aportar dos manifiestos
        # (el de la raíz del paquete y el del CWD) y el plan leería los dos.
        chosen = next((candidate for candidate in candidates if candidate.is_file()), None)
        if chosen is None:
            if explicit:
                resolution.missing_overrides.append(explicit)
                LOG.warning(
                    "%s=%s no es ningún fichero: el catálogo %s queda FUERA del plan "
                    "de retención (su keep_bytes se queda corto por él); no lo leas "
                    "como un volumen sano",
                    var,
                    explicit,
                    name,
                )
            continue
        resolution.paths.append(chosen)
    if resolution.searched and not resolution.paths:
        LOG.warning(
            "ningún manifiesto de activos en %s: la política de retención queda "
            "inoperante (no se conserva ni se expulsa nada)",
            ", ".join(str(path) for path in resolution.searched),
        )
    return resolution


def manifest_paths(env: dict[str, str] | None = None) -> list[Path]:
    """Manifiestos de activos que existen de verdad, sin depender del CWD.

    El planificador solo considera activos declarados en estos manifiestos. Si
    se resolvieran contra el directorio actual, `scripts/prune_model_cache.py
    --json` desde cualquier otro sitio no los encontraría y devolvería un plan
    vacío que se lee como «el volumen está bien»: la política quedaría
    inoperante sin decir nada. Los nombres relativos se resuelven contra la raíz
    del paquete —y después contra el CWD, con el orden compartido con
    `model_assets.manifest_path()` y `capabilities.core_manifest_path()`—, y el
    override explícito de entorno es **por catálogo** (`MANIFEST_ENV_VARS`):
    `STUDIO_CORE_MANIFEST` para el core y `STUDIO_MANIFEST` para el de Fase 3,
    igual que en esos dos resolvedores. Con una sola variable compartida,
    apuntar a un manifiesto hacía desaparecer al otro del plan.

    Un catálogo cuyo override no existe **no** cae a las rutas conocidas: se
    declara ausente (ver `resolve_manifests`), porque si cayera en silencio el
    plan seguiría siendo corto sin decirlo.
    """
    return resolve_manifests(env).paths


def _manifest_entries(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Entradas declaradas por un manifiesto, o el motivo de no poder leerlo.

    Devuelve `(entries, None)` si es un objeto con una lista `models` de objetos;
    si no, `([], motivo)`. Un manifiesto **localizado** pero ilegible se descarta
    diciéndolo (R30): el `continue` mudo dejaba `keep_count: 0` con stderr vacío
    y exit 0, el «plan vacío confidente» que el docstring promete no emitir. El
    camino era inalcanzable cuando los nombres se resolvían contra el CWD; desde
    que `manifest_paths()` los resuelve contra la raíz del paquete sí lo es, así
    que hay que guardarlo aquí y no en el llamador.

    Si la forma no es la esperada se descarta entero en lugar de leerlo a
    medias: un manifiesto medio leído produciría un plan medio confiado, que es
    el mismo problema con otra firma.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return [], "el JSON no es un objeto"
    models = data.get("models", [])
    if not isinstance(models, list) or not all(isinstance(entry, dict) for entry in models):
        return [], "'models' no es una lista de objetos"
    return models, None


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
    """Calcula qué conservar y qué expulsar. No borra nada.

    El plan resultante declara qué manifiestos leyó (`manifests`) y cuáles
    estaban ilegibles (`unreadable_manifests`), para que `keep_count: 0` no se
    lea como «el volumen está bien» cuando lo que hay es «nada declarado» o
    «la declaración no se pudo leer». Declara también los overrides que no
    resolvieron (`missing_overrides`), que es un catálogo ausente por una ruta
    mal escrita y no un volumen sano (R42).
    """
    environment = os.environ if env is None else env
    active = policy or RetentionPolicy()
    moment = time.time() if now is None else now
    usage = load_usage(environment)

    plan = RetentionPlan()
    candidates: list[tuple[float, str, Path, int, str]] = []

    resolution = resolve_manifests(environment)
    plan.missing_overrides = list(resolution.missing_overrides)
    for path in resolution.paths:
        entries, unreadable_reason = _manifest_entries(path)
        if unreadable_reason is not None:
            # Localizado pero ilegible: se descarta **diciéndolo**. Ver
            # `_manifest_entries`: el plan vacío resultante no es un volumen
            # sano, y el consumidor debe poder distinguirlo.
            plan.unreadable_manifests.append(str(path))
            LOG.warning(
                "manifiesto de activos ilegible: %s (%s); se ignora, pero el plan "
                "vacío resultante NO significa que no haya nada declarado",
                path,
                unreadable_reason,
            )
            continue
        plan.manifests.append(str(path))
        for entry in entries:
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
