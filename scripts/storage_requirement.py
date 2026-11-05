#!/usr/bin/env python3
"""Requisito de almacenamiento derivado (política de arquitectura y recursos).

180 GB era una estimación, no un requisito. Este script **calcula** el mínimo de
disco a partir de componentes reales y versionados, en lugar de repetir un
literal:

    R(perfil, opt-ins) = imagen
                       + arranque (lo que baja `--bootstrap`: hoy 0 bytes)
                       + modelos de tarea (Fase 3 base + opt-ins pedidos)
                       + pico de staging (archivo más grande × factor)
                       + reserva de descarga (HF_DISK_MARGIN_BYTES)
                       + reserva de trabajo (media, proxies, salidas, cachés)

La suma se redondea al alza a GB y se compara con el umbral que hoy está
*codificado a mano* en `comfyctl/src/config.rs` (`default_min_disk_gb`) y en
`tools/mac/comfy` (`comfy rent`). Con `--check` el script falla si un umbral
queda por debajo del requisito derivado, para que CI o un operador lo detecten
sin tener que acordarse de los números.

No descarga nada: lee `config/models-studio.json` (Fase 3), `config/models-core.json`
(arranque y adquisición a demanda) y los totales que ambos declaran. La ruta del
catálogo core la puede mover `STUDIO_CORE_MANIFEST`, la misma variable que leen
`scripts/download-models.sh`, `capabilities.core_manifest_path()` y el
planificador de retención (`studio/comfy_studio/cache_policy.py`): los cuatro
consumidores tienen que derivar del mismo manifiesto.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

GiB = 1024**3
GB = 1000**3

#: Raíz del árbol de fuentes. En la imagen el script se copia a
#: `/usr/local/bin`, así que apunta a un directorio sin `config/`: solo se usa
#: para resolver un override **relativo**, nunca como ruta por omisión.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Tamaño medido de la imagen publicada (README §B2 / CI). Se declara, no se
#: calcula: es un artefacto medido, no una constante del código.
IMAGE_BYTES_DEFAULT = 15_600_000_000

#: Sobrecarga de filesystem (ext4/overlay: metadatos, journal, inodes).
FILESYSTEM_OVERHEAD_BYTES = 2 * GiB

#: Reserva de descarga declarada en scripts/download-models.sh (HF_DISK_MARGIN_BYTES).
DOWNLOAD_MARGIN_BYTES = 10 * GiB

#: Reserva de trabajo: media fuente, proxies, salidas y cachés de Studio.
WORKING_RESERVE_BYTES = 20 * GiB

#: Durante una descarga pueden coexistir el `.incomplete` y la copia final.
STAGING_COEXIST_FACTOR = 2

#: Perfiles que reconoce `download-models.sh`. El arranque ya no es por perfil:
#: `--bootstrap` baja solo lo que el manifiesto core declara clase `bootstrap`.
PROFILES = ("all", "general", "realistic", "anime", "minimal", "studio", "unreal")

#: Perfiles que descargan la Fase 3 (el manifiesto). El resto no la toca.
PHASE3_PROFILES = frozenset({"studio", "unreal"})

#: Manifiesto que lee `--bootstrap` (scripts/download-models.sh). Se busca en el
#: árbol de trabajo y en la ruta horneada en la imagen.
CORE_MANIFEST_CANDIDATES = (
    "config/models-core.json",
    "/etc/comfyui/models-core.json",
)

#: Avisos ya emitidos (R99). El script es un proceso corto, así que un conjunto
#: de módulo basta para no repetir el mismo aviso por cada entrada y perfil.
_WARNED: set[str] = set()

#: Variable de entorno que señala otro catálogo core. La leen ya
#: `scripts/download-models.sh` (`${STUDIO_CORE_MANIFEST:-...}`) y
#: `capabilities.core_manifest_path()`; este script era el tercer consumidor que
#: la ignoraba, así que un despliegue que la fijaba derivaba el requisito de
#: disco —y el disco que `tools/mac/comfy` pide al proveedor— de un catálogo
#: distinto del que el resolvedor y el arranque acaban usando.
CORE_MANIFEST_ENV = "STUDIO_CORE_MANIFEST"

#: Marcadores de posición y defaults, idénticos a `model_assets._DEFAULT_ENV`.
#: Están duplicados a propósito: en la imagen este script vive solo en
#: `/usr/local/bin/storage_requirement.py`, sin el paquete `comfy_studio` al
#: lado. `_expand_placeholders` delega en `model_assets.expand_placeholders`
#: cuando puede importarlo, para que no acabe habiendo dos reglas distintas.
PLACEHOLDER_DEFAULTS = {
    "COMFY_APP_DIR": "/opt/ComfyUI",
    "COMFY_DATA_DIR": "/workspace/comfyui-data",
}

#: Clases del manifiesto core que la adquisición a demanda puede pedir.
ON_DEMAND_CLASSES = frozenset({"bootstrap", "task", "optional"})

#: Umbrales codificados a mano que este script debe justificar o destapar.
DECLARED_THRESHOLDS_GB: dict[str, int] = {
    "all": 75,
    "general": 45,
    "realistic": 45,
    "anime": 45,
    "minimal": 10,
    "studio": 180,
    "unreal": 210,
}

#: Mínimo que pide `tools/mac/comfy` al alquilar (GB). `studio`/`unreal` son None
#: porque el helper los DERIVA de este mismo script: no hay literal que verificar.
RENT_MIN_DISK_GB: dict[str, int | None] = {
    "all": 120,
    "general": 120,
    "realistic": 120,
    "anime": 120,
    "minimal": 120,
    "studio": None,
    "unreal": None,
}

OPT_IN_FLAGS = {
    "STUDIO_INCLUDE_ANIMATE": "animate",
    "STUDIO_INCLUDE_3D": "three_d",
    "STUDIO_INCLUDE_MINIMAX": "minimax",
}


def load_manifest(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _load_core_manifest(path: Path) -> dict[str, Any]:
    """Raíz del catálogo core, degradada por `_manifest_root` (T2).

    `load_manifest` devuelve lo que haya en el fichero, y una raíz `null` es
    indistinguible —para `requirement_bytes`— del `core_manifest=None` que
    significa «no hay catálogo que leer». Se degrada aquí, donde la ruta existe y
    el valor es el fichero: una raíz que no es objeto avisa, y quien deriva
    recibe `{}`. Es el mismo helper que lee las demás raíces.
    """
    return _manifest_root(load_manifest(path))


def _entry_field(entry: object, key: str) -> object:
    """Campo `key` de una entrada de `models`; `None` si no se puede leer.

    D4/R94: `models[]` también lo escribe el operario y `json.load` acepta
    cualquier literal, así que una entrada que no es un objeto (`[]`, `"x"`,
    `5`, `null`) es posible. Los cuatro lectores hacían `entry.get(...)` **fuera**
    de todo `try` y respondían `AttributeError` —que no es ni `TypeError` ni
    `ValueError`—, con el script saliendo por traza y sin requisito que
    publicar. Una entrada sin mapa no tiene campos: degrada como ausente, que es
    lo que ya hacía `_entry_bytes` con un tamaño ilegible.

    R99 acotó el alcance de esa regla tal y como queda dicha aquí: cubre las
    **entradas** que no son mapas, y nada más. Las demás formas que un manifiesto
    de operario puede tener —una raíz que no es un objeto, un `models` que no es
    una lista, un `opt_in` o una `class` sin clave hashable y un `profiles` que
    no es iterable— se degradan donde se leen (`_manifest_root`,
    `_manifest_models`, `_opt_in_bytes`, `_is_on_demand`, `_matches_profile`),
    porque ninguna de ellas es un campo de una entrada.
    """
    try:
        return entry.get(key)  # type: ignore[union-attr]
    except AttributeError:
        return None


def _warn_once(message: str) -> None:
    """Avisa por stderr, una sola vez por mensaje (R99).

    El requisito se publica aunque el manifiesto esté mal: el operario tiene que
    enterarse de qué parte de su catálogo se está ignorando, pero quedarse sin
    informe —rc=1 y traza— es exactamente lo que D4 quitó de en medio. Una sola
    vez por mensaje porque los cuatro lectores preguntan lo mismo por entrada y
    por perfil, y un aviso repetido por cada una es ruido.
    """
    if message not in _WARNED:
        _WARNED.add(message)
        print(f"AVISO: {message}", file=sys.stderr)


def _manifest_root(manifest: object) -> dict[str, Any]:
    """El manifiesto si su raíz es un objeto; `{}` si no lo es (R99).

    `json.load` devuelve lo que haya en el fichero, y la raíz de un manifiesto
    de operario puede ser una lista, un número o `null`. `manifest.get(...)`
    sobre eso responde `AttributeError`, que ningún `except TypeError/ValueError`
    del módulo ve: el script salía por traza, sin requisito que publicar y sin
    decir por qué. Una raíz sin objeto no tiene nada que derivar: degrada a
    manifiesto vacío y avisa, que es el desenlace de D4 para el resto de formas.

    Degrada también cuando la raíz es falsy (`[]`, `null`, `false`): el
    `core_manifest or {}` de `requirement_bytes` silenciaba justo esas, y el
    operario no se enteraba de que su catálogo core no se estaba leyendo. El
    `None` que significa «no hay catálogo» lo resuelve `requirement_bytes` sin
    pasar por aquí; el `null` de un fichero sí llega, por `_load_core_manifest`.
    """
    if isinstance(manifest, dict):
        return manifest
    _warn_once(f"manifiesto sin objeto en la raíz (se ignora): {type(manifest).__name__}")
    return {}


def _manifest_models(manifest: object) -> list[Any]:
    """`models` del manifiesto si es una lista; `[]` si no lo es (R99).

    `manifest.get("models", [])` devuelve lo que haya, y un `"models": 5` o un
    `"models": null` llegaban al `for entry in ...` como `TypeError: 'int' object
    is not iterable` —el `None` además sin pasar por el default, porque la clave
    existe—. Los cuatro lectores hacían ese `for` fuera de todo `try`. Un
    `models` que no es una lista no tiene entradas: degrada a vacío y avisa.
    """
    models = _manifest_root(manifest).get("models", [])
    if isinstance(models, list):
        return models
    _warn_once(f"'models' no es una lista (se ignora): {type(models).__name__}")
    return []


def _entry_bytes(entry: object) -> int:
    """Bytes que declara una entrada; 0 si no declara ninguno utilizable.

    D4/R94: `json.load` acepta los literales no estándar `Infinity`,
    `-Infinity` y `1e400`, y `int()` sobre ellos responde `OverflowError` —un
    `ArithmeticError`, fuera de `TypeError`/`ValueError`—. Un manifiesto de
    operario con `"bytes": Infinity` hacía salir el script con rc=1 y sin
    requisito que publicar, cuando lo correcto es lo que ya hacen
    `capabilities._declared_bytes` y `asset_acquire._declared_bytes`: un tamaño
    ilegible es «no declarado», cuenta 0.

    `AttributeError` está en la lista por la misma razón: una entrada que no es
    un mapa (`models: [[]]`) responde eso y no `TypeError`, y es «no declarado».
    `_entry_field` ya lo degrada, así que queda como segunda red. Misma regla que
    `_meta_bytes`, mismo desenlace (0).

    R99: esa regla, dicha así, cubre las entradas que no son mapas y nada más. Lo
    demás se degrada donde se lee —`_manifest_root`, `_manifest_models`,
    `_opt_in_bytes`, `_is_on_demand`, `_matches_profile`—, porque ninguna de esas
    formas es un campo de una entrada.
    """
    try:
        return int(_entry_field(entry, "bytes") or 0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


def _meta_bytes(manifest: dict[str, Any]) -> int:
    """Bytes base que declara `meta.default_bytes`; 0 si no declara ninguno.

    D4/R94 (misma regla que `_entry_bytes`): `meta.default_bytes` es dato de
    manifiesto, lo escribe un operario, y `int()` sobre `Infinity`/`-Infinity`/
    `1e400` responde `OverflowError` —un `ArithmeticError`, fuera de
    `TypeError`/`ValueError`—. Un `meta` que no es un mapa responde además
    `AttributeError` en el `.get`, y `_entry_field` lo degrada igual. El `int()`
    pelado hacía salir el script con rc=1 y sin requisito que publicar, mientras
    `check_models_manifest.py` informaba del mismo manifiesto como simplemente
    inválido: los dos lectores no se ponían de acuerdo. Un tamaño ilegible es
    «no declarado», cuenta 0.

    R99: la regla, dicha como queda aquí, vale para `meta` —un `meta` que no es un
    mapa responde `AttributeError` en el `.get` y `_entry_field` lo degrada— y para
    el manifiesto que lo contiene, que `_manifest_root` degrada a vacío si su raíz
    no es un objeto. Con eso los cinco caminos que quedaban fuera (raíz no mapa,
    `models` no lista, `opt_in` y `class` sin clave hashable, `profiles` no
    iterable) acaban todos en «no declarado» o «sin entradas», y el requisito se
    publica.
    """
    try:
        return int(
            _entry_field(_manifest_root(manifest).get("meta", {}), "default_bytes") or 0
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


def _opt_in_bytes(manifest: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in _manifest_models(manifest):
        flag = _entry_field(entry, "opt_in")
        if flag:
            try:
                hash(flag)
            except TypeError:
                # R99: `out[flag]` necesita una clave hashable, y `opt_in` es
                # dato de manifiesto: un `"opt_in": ["a"]` respondía
                # `TypeError: unhashable type: 'list'` y el script salía por
                # traza. Un grupo que no se puede indexar se ignora; el resto
                # del catálogo se deriva igual.
                _warn_once(f"'opt_in' sin clave hashable (se ignora): {type(flag).__name__}")
                continue
            out[flag] = out.get(flag, 0) + _entry_bytes(entry)
    return out


def _largest_default_bytes(manifest: dict[str, Any]) -> int:
    sizes = [
        _entry_bytes(e)
        for e in _manifest_models(manifest)
        if not _entry_field(e, "opt_in")
    ]
    return max(sizes) if sizes else 0


def _expand_placeholders(value: str, env: dict[str, str] | None = None) -> str:
    """Expande `{COMFY_APP_DIR}`/`{COMFY_DATA_DIR}` y `~`, como el resolvedor.

    Delega en `model_assets.expand_placeholders` (la regla compartida con
    `capabilities.core_manifest_path()`) cuando el paquete está importable. Si no
    lo está —caso de la imagen, donde el script se copia a
    `/usr/local/bin/storage_requirement.py`— se aplica la misma regla en local.
    Los tests fijan que ambas den el mismo resultado.
    """
    environment = os.environ if env is None else env
    try:
        from comfy_studio import model_assets
    except ImportError:
        out = value
        for key, default in PLACEHOLDER_DEFAULTS.items():
            out = out.replace("{" + key + "}", environment.get(key, default))
        return os.path.expanduser(out)
    return model_assets.expand_placeholders(value, environment)


def _existing_path(raw: str) -> Path | None:
    """Ruta absoluta si existe; si es relativa, contra el cwd y la raíz del repo.

    Misma resolución que `capabilities.core_manifest_path()`: un override
    relativo se busca donde estaría el del árbol de fuentes.
    """
    path = Path(_expand_placeholders(raw))
    if path.is_absolute():
        return path if path.is_file() else None
    for base in (Path.cwd(), REPO_ROOT):
        candidate = base / path
        if candidate.is_file():
            return candidate
    return None


def resolve_core_manifest(explicit: str | None) -> Path | None:
    """Manifiesto core: el que lee `--bootstrap` y la adquisición a demanda.

    Orden: `--core-manifest`, luego `STUDIO_CORE_MANIFEST`, luego las rutas
    conocidas. La variable de entorno va **antes** de las rutas horneadas porque
    es el mismo nombre que leen `download-models.sh` y
    `capabilities.core_manifest_path()`: si este script la ignorara, el requisito
    de disco se derivaría de un catálogo distinto del que se acaba descargando.

    Un override que no existe no aborta: se pasa a las rutas conocidas, que es lo
    que hace también `scripts/download-models.sh` (si `${STUDIO_CORE_MANIFEST}`
    no es un fichero, cae en `config/models-core.json`). **No** es lo que hace el
    cuarto consumidor, el planificador de retención (`cache_policy`): allí un
    override ausente deja ese catálogo fuera del plan y se declara en
    `missing_overrides`, porque si cayera en silencio el plan se leería como un
    volumen sano corto por el catálogo entero.
    """
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    override = (os.environ.get(CORE_MANIFEST_ENV) or "").strip()
    if override:
        found = _existing_path(override)
        if found is not None:
            return found
    for candidate in CORE_MANIFEST_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def bootstrap_download_bytes(core_manifest: dict[str, Any]) -> int:
    """Bytes que baja `--bootstrap`: las entradas de clase `bootstrap` del core.

    Hoy es **0 a propósito**: abrir la API y descubrir capacidades no arrastra
    ningún modelo (política, punto 4). Se deriva del manifiesto en lugar de
    repetir un literal para que, si alguien vuelve a declarar una entrada
    `bootstrap`, la puerta de arranque la recoja sin tocar este script.
    """
    return sum(
        _entry_bytes(entry)
        for entry in _manifest_models(core_manifest)
        if _entry_field(entry, "class") == "bootstrap"
    )


def _is_on_demand(entry: object) -> bool:
    """True si la clase de la entrada es adquisible a demanda (R99).

    `in` sobre un `frozenset` exige un operando hashable, y `class` es dato de
    manifiesto: un `"class": []` respondía `TypeError: unhashable type: 'list'`
    con el script saliendo por traza. Una clase que no se puede comparar no está
    en el conjunto, igual que una que no coincide con ninguno de sus miembros.
    """
    asset_class = _entry_field(entry, "class")
    try:
        return asset_class in ON_DEMAND_CLASSES
    except TypeError:
        _warn_once(f"'class' sin clave hashable (se ignora): {type(asset_class).__name__}")
        return False


def _matches_profile(entry: object, profile: str) -> bool:
    """True si la entrada no declara perfiles o declara `profile` (R99).

    Sale del campo `profiles` del manifiesto (la misma fuente que valida
    `check_profile_parity.py`). Un `profiles` que no es iterable
    (`"profiles": 5`) hacía reventar la comprehensión con
    `TypeError: 'int' object is not iterable`; se trata como «sin perfiles
    declarados», que es lo que ya significa el `not _entry_field(...)` de la
    condición original: la entrada cuenta para todos los perfiles.
    """
    profiles = _entry_field(entry, "profiles")
    if not profiles:
        return True
    try:
        names = {str(p) for p in profiles}  # type: ignore[union-attr]
    except TypeError:
        _warn_once(f"'profiles' no es iterable (se ignora): {type(profiles).__name__}")
        return True
    return profile in names


def on_demand_largest_bytes(core_manifest: dict[str, Any], *, profile: str) -> int:
    """Archivo más grande que el perfil puede pedir del catálogo core.

    Es el pico de staging real de la adquisición a demanda: durante una descarga
    coexisten el `.incomplete` y la copia final. Sale del campo `profiles` del
    manifiesto (la misma fuente que valida `check_profile_parity.py`), no de un
    número escrito a mano por fase.
    """
    sizes = [
        _entry_bytes(entry)
        for entry in _manifest_models(core_manifest)
        if _is_on_demand(entry) and _matches_profile(entry, profile)
    ]
    return max(sizes) if sizes else 0


def requirement_bytes(
    manifest: dict[str, Any],
    *,
    profile: str,
    opt_ins: set[str] | None = None,
    image_bytes: int = IMAGE_BYTES_DEFAULT,
    staging_factor: int = STAGING_COEXIST_FACTOR,
    image_on_volume: bool = False,
    core_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Requisito derivado en bytes, con el desglose por componente."""
    requested = set(opt_ins or ())
    if profile == "unreal":
        # `unreal` implica Hunyuan3D (el helper no tiene perfil unreal).
        requested.add("STUDIO_INCLUDE_3D")

    # `core_manifest or {}` degradaba en silencio una raíz falsy que no era un
    # objeto (`[]`, `null`): el operario se quedaba sin aviso de que su catálogo
    # core no se estaba leyendo, mientras que un `5` o un `"x"` —las mismas
    # formas, solo que verdaderas— sí avisaban por `_manifest_root`. Se lee la
    # raíz por el mismo helper que el resto. `None` es «no hay catálogo core», no
    # una raíz ilegible, y se resuelve aquí sin avisar.
    core = _manifest_root(core_manifest) if core_manifest is not None else {}
    opt_bytes = _opt_in_bytes(manifest)
    base_bytes = _meta_bytes(manifest)
    # Lo que el arranque descarga de verdad. No depende del perfil: `--bootstrap`
    # ejecuta el mismo plan para todos.
    startup = bootstrap_download_bytes(core)
    uses_phase3 = profile in PHASE3_PROFILES
    models = (base_bytes + sum(opt_bytes.get(flag, 0) for flag in requested)) if uses_phase3 else 0
    if uses_phase3:
        staging = _largest_default_bytes(manifest) * staging_factor
    else:
        staging = on_demand_largest_bytes(core, profile=profile) * staging_factor

    components = {
        "image": image_bytes if image_on_volume else 0,
        "filesystem_overhead": FILESYSTEM_OVERHEAD_BYTES if image_on_volume else 0,
        # Nombre deliberadamente distinto de la clase `bootstrap` del manifiesto:
        # aquella significa «imprescindible para abrir la API» y está vacía; esta
        # es «lo que el arranque acaba bajando», que hoy también es 0.
        "startup_download": startup,
        "staging_peak": staging,
        "download_margin": DOWNLOAD_MARGIN_BYTES,
    }
    # Puerta de arranque: lo que debe estar LIBRE en el volumen de datos antes de
    # servir la primera petición (arranque + pico de staging + margen de descarga).
    start_gate = startup + staging + DOWNLOAD_MARGIN_BYTES
    components["task_models"] = models
    components["working_reserve"] = WORKING_RESERVE_BYTES
    total = sum(components.values())
    threshold = DECLARED_THRESHOLDS_GB.get(profile)
    rent = RENT_MIN_DISK_GB.get(profile)
    return {
        "profile": profile,
        "opt_ins": sorted(requested),
        "image_on_volume": image_on_volume,
        # Tamaño real de la imagen: se transfiere en every arranque en frío,
        # aunque no ocupe el volumen de datos.
        "image_bytes": image_bytes,
        "components": components,
        "start_gate_bytes": start_gate,
        # Entero ya redondeado al alza: es lo que consume comfyctl (S5) y evita
        # casts en punto flotante en Rust.
        "start_gate_gb": -(-int(start_gate) // GB),
        "total_bytes": total,
        "total_gb": total / GB,
        "total_gib": total / GiB,
        "min_volume_gb": -(-int(total) // GB),  # techo en GB
        "declared_threshold_gb": threshold,
        "rent_min_disk_gb": rent,
        # Positivo = el umbral declarado se queda CORTO respecto a lo derivado.
        "start_gate_shortfall_gb": max(0.0, (start_gate - (threshold or 0) * GB) / GB),
        "volume_shortfall_gb": max(0.0, (total - (rent or 0) * GB) / GB),
    }

def _gb(value: int) -> str:
    return f"{value / GB:.2f} GB ({value / GiB:.2f} GiB)"


#: Precios por defecto (orientativos, sobreescritibles por CLI/entorno).
DEFAULT_GPU_DPH = 0.55
DEFAULT_STORAGE_COST_PER_GB_MONTH = 0.15
DEFAULT_TRANSFER_PER_GB = 0.01
DEFAULT_INET_DOWN_MBPS = 500.0


def session_cost(
    report: dict[str, Any],
    *,
    hours: float = 2.0,
    gpu_dph: float = DEFAULT_GPU_DPH,
    disk_gb: int | None = None,
    storage_cost_per_gb_month: float = DEFAULT_STORAGE_COST_PER_GB_MONTH,
    transfer_per_gb: float = DEFAULT_TRANSFER_PER_GB,
    inet_down_mbps: float = DEFAULT_INET_DOWN_MBPS,
) -> dict[str, Any]:
    """Coste completo de una sesión (política, punto 6).

    «Optimize the cost of producing an acceptable deliverable, including startup,
    transfers, downloads, inference, evaluation, storage and API usage.»

    Lo que suele ignorarse y aquí se hace explícito: la GPU se paga **mientras
    se descarga**. Con 180 GB de modelos a 500 Mbps son ~50 min de GPU cobrando
    sin producir nada, y eso cambia qué hardware compensa.
    """
    components = report["components"]
    image_bytes = report.get("image_bytes") or 0
    model_bytes = (
        components.get("startup_download", 0)
        + components.get("task_models", 0)
    )
    transfer_bytes = image_bytes + model_bytes

    bits = transfer_bytes * 8
    seconds = bits / (max(inet_down_mbps, 1.0) * 1e6)
    transfer_hours = seconds / 3600.0

    image_bits = image_bytes * 8
    cold_start_hours = (image_bits / (max(inet_down_mbps, 1.0) * 1e6)) / 3600.0

    disk = disk_gb if disk_gb is not None else report["min_volume_gb"]
    disk_cost = (disk * storage_cost_per_gb_month / 730.0) * hours
    gpu_cost = gpu_dph * hours
    # La GPU factura durante el arranque en frío y la descarga de modelos.
    idle_gpu_cost = gpu_dph * min(transfer_hours, hours) if hours else 0.0
    transfer_cost = (transfer_bytes / 1e9) * transfer_per_gb

    return {
        "hours": hours,
        "gpu_dph": gpu_dph,
        "disk_gb": disk,
        "inet_down_mbps": inet_down_mbps,
        "image_bytes": image_bytes,
        "model_bytes": model_bytes,
        "transfer_bytes": transfer_bytes,
        "transfer_cost": round(transfer_cost, 4),
        "disk_cost": round(disk_cost, 4),
        "gpu_cost": round(gpu_cost, 4),
        "gpu_idle_cost": round(idle_gpu_cost, 4),
        "cold_start_hours": round(cold_start_hours, 3),
        "download_hours": round(transfer_hours, 3),
        "productive_hours": round(max(0.0, hours - min(transfer_hours, hours)), 3),
        "total_cost": round(gpu_cost + disk_cost + transfer_cost, 4),
        # Coste por hora productiva: la métrica que de verdad importa.
        "cost_per_productive_hour": round(
            (gpu_cost + disk_cost + transfer_cost)
            / max(0.001, hours - min(transfer_hours, hours)),
            4,
        ),
    }


def render_cost(report: dict[str, Any], cost: dict[str, Any]) -> str:
    lines = [
        f"Coste de sesión ({cost['hours']}h, GPU ${cost['gpu_dph']}/h, "
        f"disco {cost['disk_gb']} GB, {cost['inet_down_mbps']:.0f} Mbps)",
        "",
        f"  imagen                    {cost['image_bytes'] / 1e9:>8.2f} GB",
        f"  modelos                   {cost['model_bytes'] / 1e9:>8.2f} GB",
        f"  transferencia total       {cost['transfer_bytes'] / 1e9:>8.2f} GB "
        f"(${cost['transfer_cost']:.2f})",
        f"  arranque en frío          {cost['cold_start_hours']:>8.2f} h",
        f"  descarga de modelos       {cost['download_hours']:>8.2f} h",
        f"  GPU facturada sin producir{cost['download_hours']:>7.2f} h "
        f"(${cost['gpu_idle_cost']:.2f})",
        f"  horas productivas         {cost['productive_hours']:>8.2f} h",
        "",
        f"  coste GPU                 ${cost['gpu_cost']:>8.2f}",
        f"  coste disco               ${cost['disk_cost']:>8.2f}",
        f"  coste transferencia       ${cost['transfer_cost']:>8.2f}",
        f"  TOTAL                     ${cost['total_cost']:>8.2f}",
        f"  por hora productiva       ${cost['cost_per_productive_hour']:>8.2f}",
    ]
    return "\n".join(lines)


def render(report: dict[str, Any]) -> str:
    lines = [
        f"Perfil `{report['profile']}`"
        + (f" + opt-ins {', '.join(report['opt_ins'])}" if report["opt_ins"] else ""),
        "",
        "  componente                bytes",
    ]
    for name, value in report["components"].items():
        lines.append(f"  {name:<24} {_gb(value):>28}")
    lines.append(f"  {'TOTAL':<24} {_gb(report['total_bytes']):>28}")
    lines.append("")
    lines.append(f"  puerta de arranque (S5)   {_gb(report['start_gate_bytes']):>28}")
    declared = report["declared_threshold_gb"]
    rent = report["rent_min_disk_gb"]
    if declared is not None:
        verdict = (
            "OK"
            if report["start_gate_shortfall_gb"] <= 0
            else f"INSUFICIENTE por {report['start_gate_shortfall_gb']:.1f} GB"
        )
        lines.append(f"  umbral declarado (comfyctl S5): {declared} GB -> {verdict}")
    if rent is not None:
        verdict = (
            "OK"
            if report["volume_shortfall_gb"] <= 0
            else f"INSUFICIENTE por {report['volume_shortfall_gb']:.1f} GB"
        )
        lines.append(f"  volumen mínimo (comfy rent): {rent} GB -> {verdict}")
    else:
        lines.append("  volumen mínimo (comfy rent): derivado por el helper")
    lines.append(f"  volumen derivado recomendado: {report['min_volume_gb']} GB")
    return "\n".join(lines)


def _write_all_profiles(
    manifest_path: Path,
    core_manifest_path: Path,
    *,
    image_bytes: int,
    out: str | None,
) -> int:
    """Deriva todos los perfiles y escribe el JSON que consume `comfyctl`.

    Es el contrato entre el script (Python, fuente de verdad del cálculo) y el
    binario Rust: la imagen lo hornea en `/etc/comfyui/storage-requirement.json`
    y S5 lee de ahí en lugar de una tabla codificada a mano.
    """
    manifest = load_manifest(manifest_path)
    core_manifest = _load_core_manifest(core_manifest_path)
    profiles: dict[str, Any] = {}
    for name in PROFILES:
        report = requirement_bytes(
            manifest, profile=name, image_bytes=image_bytes, core_manifest=core_manifest
        )
        profiles[name] = {
            "start_gate_gb": report["start_gate_gb"],
            "start_gate_bytes": report["start_gate_bytes"],
            "min_volume_gb": report["min_volume_gb"],
            "total_bytes": report["total_bytes"],
        }
    payload = {
        "version": 1,
        "source": "scripts/storage_requirement.py",
        "image_bytes": image_bytes,
        "profiles": profiles,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"escrito {out}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="config/models-studio.json",
        help="Ruta del manifiesto de Fase 3.",
    )
    parser.add_argument(
        "--core-manifest",
        default=None,
        help="Ruta del manifiesto core (el que lee `--bootstrap`). "
        "Por omisión manda STUDIO_CORE_MANIFEST y, si no está, se busca "
        "en el árbol y en /etc/comfyui.",
    )
    parser.add_argument("--profile", default="studio", choices=sorted(PROFILES))
    parser.add_argument(
        "--opt-in",
        action="append",
        default=[],
        choices=sorted(OPT_IN_FLAGS),
        help="Grupo opt-in pedido (repetible).",
    )
    parser.add_argument(
        "--image-bytes",
        type=int,
        default=IMAGE_BYTES_DEFAULT,
        help="Tamaño medido de la imagen publicada.",
    )
    parser.add_argument(
        "--image-on-volume",
        action="store_true",
        help="La imagen vive en el mismo volumen que los datos (RunPod/proveedores "
        "sin overlay separado). En Vast.ai la imagen está en el host: no se carga.",
    )
    parser.add_argument("--cost", action="store_true", help="Añade el coste de sesión.")
    parser.add_argument(
        "--hours", type=float, default=2.0, help="Duración de la sesión en horas."
    )
    parser.add_argument(
        "--gpu-dph", type=float, default=DEFAULT_GPU_DPH, help="Precio de la GPU por hora."
    )
    parser.add_argument(
        "--disk-gb", type=int, default=None, help="GB de disco (por defecto el derivado)."
    )
    parser.add_argument(
        "--transfer-per-gb",
        type=float,
        default=DEFAULT_TRANSFER_PER_GB,
        help="Coste de transferencia por GB.",
    )
    parser.add_argument(
        "--inet-down-mbps",
        type=float,
        default=DEFAULT_INET_DOWN_MBPS,
        help="Ancho de bajada del host.",
    )
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="Deriva todos los perfiles y escribe el JSON que hornea la imagen.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Ruta de salida para --all-profiles (por defecto stdout).",
    )
    parser.add_argument("--json", action="store_true", help="Salida JSON.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Salir con código 1 si algún umbral declarado queda por debajo.",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"ERROR: no existe el manifiesto {manifest_path}", file=sys.stderr)
        return 2

    core_manifest_path = resolve_core_manifest(args.core_manifest)
    if core_manifest_path is None:
        searched = " / ".join(
            part
            for part in (
                args.core_manifest,
                f"{CORE_MANIFEST_ENV}={os.environ.get(CORE_MANIFEST_ENV) or '-'}",
                *CORE_MANIFEST_CANDIDATES,
            )
            if part
        )
        print(
            f"ERROR: no existe el manifiesto core ({searched}): "
            "sin él no se puede derivar el arranque ni el pico de staging",
            file=sys.stderr,
        )
        return 2

    if args.all_profiles:
        return _write_all_profiles(
            manifest_path,
            core_manifest_path,
            image_bytes=args.image_bytes,
            out=args.out,
        )

    report = requirement_bytes(
        load_manifest(manifest_path),
        profile=args.profile,
        opt_ins=set(args.opt_in),
        image_bytes=args.image_bytes,
        image_on_volume=args.image_on_volume,
        core_manifest=_load_core_manifest(core_manifest_path),
    )

    cost = None
    if args.cost:
        cost = session_cost(
            report,
            hours=args.hours,
            gpu_dph=args.gpu_dph,
            disk_gb=args.disk_gb,
            transfer_per_gb=args.transfer_per_gb,
            inet_down_mbps=args.inet_down_mbps,
        )

    if args.json:
        payload = dict(report)
        if cost is not None:
            payload["cost"] = cost
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(report))
        if cost is not None:
            print()
            print(render_cost(report, cost))

    if args.check:
        problems = []
        if report["declared_threshold_gb"] is not None and report["start_gate_shortfall_gb"] > 0:
            problems.append(
                f"S5/{report['profile']}={report['declared_threshold_gb']} GB "
                f"< puerta de arranque {report['start_gate_gb']:.1f} GB"
            )
        if (
            report["rent_min_disk_gb"] is not None
            and report["volume_shortfall_gb"] > 0
        ):
            problems.append(
                f"comfy rent/{report['profile']}={report['rent_min_disk_gb']} GB "
                f"< volumen derivado {report['min_volume_gb']} GB"
            )
        if problems:
            for problem in problems:
                print(f"FALLO: {problem}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
