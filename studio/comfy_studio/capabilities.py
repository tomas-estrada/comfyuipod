"""Resolvedor de capacidades (política de arquitectura y recursos).

Convierir «necesito esta capacidad» en un plan determinista:

    capacidad -> motor preparado + receta (plantilla) + activos requeridos
              -> qué falta en disco -> adquirirlo con la herramienta existente

La declaración de activos por tubería ya existe: cada plantilla en
`studio/comfy_studio/templates/data/*.json` declara `models`. Este módulo une
esa declaración con los manifiestos (`config/models-core.json` para las Fases
1/2 y `config/models-studio.json` para la Fase 3) y responde, sin red y sin GPU:

  - `ready`            : motor preparado y todos los activos en disco;
  - `engine_unprepared`: el motor no puede ejecutar aquí (ver preparedness);
  - `assets_missing`   : motor preparado, faltan activos (con la lista exacta);
  - `unknown_capability`: la capacidad no está registrada.

El registro de capacidades es **versionado y declarativo**: añadir una
capacidad es añadir una entrada, no razonar de nuevo en cada despliegue.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comfy_studio import hardware_profile, model_assets
from comfy_studio.engines.preparedness import (
    build_default_prep_specs,
    probe_preparedness,
)
from comfy_studio.templates import get_default_registry

CAPABILITY_REGISTRY_VERSION = 1

LOG = logging.getLogger(__name__)

#: Capacidades registradas. `templates` es la receta candidata (la primera es
#: la preferida); `alternatives` son capacidades equivalentes si la preferida
#: no puede ejecutar (política: «considerar una alternativa adecuada»).
CAPABILITIES: dict[str, dict[str, Any]] = {
    "video.text_to_video": {
        "engine": "comfyui",
        "templates": ["wan_t2v", "wan_t2v_5b"],
        "summary": "Vídeo desde texto (Wan 2.2 14B MoE o 5B turbo)",
    },
    "video.image_to_video": {
        "engine": "comfyui",
        "templates": ["wan_i2v", "wan_i2v_5b"],
        "summary": "Vídeo desde imagen (Wan 2.2 14B MoE o 5B turbo)",
    },
    "video.first_last_to_video": {
        "engine": "comfyui",
        "templates": ["wan_flf2v"],
        "summary": "Vídeo desde primer y último fotograma",
    },
    "video.speech_to_video": {
        "engine": "comfyui",
        "templates": ["wan_s2v"],
        "summary": "Vídeo locutado (Wan 2.2 S2V con alineador wav2vec2 ES)",
    },
    "video.animate": {
        "engine": "comfyui",
        "templates": ["wan_animate", "wan_animate_cont"],
        "summary": "Animación de personaje (Wan 2.2 Animate 14B, pesos opt-in)",
    },
    "video.reference_to_video": {
        "engine": "comfyui",
        "templates": ["minimax_r2v"],
        "summary": "Vídeo por referencia (MiniMax H3 Ref2VA, pesos opt-in)",
    },
    "video.inpaint": {
        "engine": "comfyui",
        "templates": ["vace_inpaint"],
        "summary": "Inpaint guiado por máscara (Wan 2.2 VACE 14B)",
    },
    "video.restyle": {
        "engine": "comfyui",
        "templates": ["vace_restyle"],
        "summary": "Restilo de vídeo (Wan 2.2 VACE 14B)",
    },
    "video.face_swap": {
        "engine": "comfyui",
        "templates": ["vace_swap"],
        "summary": "Intercambio de rostro en vídeo (Wan 2.2 VACE 14B)",
    },
    "video.lipsync": {
        "engine": "comfyui",
        "templates": ["latentsync"],
        "summary": "Lipsync de vídeo con audio (LatentSync 1.6)",
    },
    "video.face_restore": {
        "engine": "comfyui",
        "templates": ["face_fix"],
        "summary": "Restauración facial sobre vídeo (CodeFormer/GFPGAN)",
    },
    "video.upscale": {
        "engine": "upscaler",
        "templates": [],
        "summary": "Super-resolución 2x/4x (Real-ESRGAN)",
    },
    "image.generate": {
        "engine": "comfyui",
        "templates": ["sdxl_image"],
        "summary": "Imagen desde texto (SDXL Juggernaut XI)",
    },
    "image.ipadapter": {
        "engine": "comfyui",
        "templates": ["sdxl_ipadapter"],
        "summary": "Imagen condicionada por referencia (IP-Adapter SDXL)",
    },
    "model3d.from_image": {
        "engine": "comfyui",
        "templates": ["hunyuan3d_image_to_3d"],
        "summary": "Malla 3D texturizada desde imagen (Hunyuan3D 2.1, pesos opt-in)",
    },
    "audio.speech": {
        "engine": "audio",
        "templates": [],
        "summary": "Síntesis de voz (Kokoro horneado; clonación vía IndexTTS-2.5)",
    },
    "audio.transcribe": {
        "engine": "audio",
        "templates": [],
        "summary": "Transcripción con timestamps (faster-whisper large-v3)",
    },
    "music.generate": {
        "engine": "music_gen",
        "templates": [],
        "summary": "Música incidental (MusicGen/audiocraft)",
    },
}


@dataclass(slots=True)
class AssetNeed:
    """Un activo requerido por una receta y su estado en disco."""

    filename: str
    #: Ruta absoluta resuelta (lista para escribir/leer).
    dest_path: str
    bytes: int
    asset_class: str
    present: bool
    #: Manifiesto del que sale (`core`, `studio` o `unknown`).
    source: str
    repo_id: str | None = None
    remote_path: str | None = None
    repo_type: str | None = None
    license: str | None = None
    url: str | None = None
    #: Ruta tal y como la declara el manifiesto (relativa), para trazabilidad.
    manifest_dest: str = ""
    #: Revisión fijada (commit) e hash de integridad, si el manifiesto los publica.
    revision: str | None = None
    sha256: str | None = None
    sha1: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "dest_path": self.dest_path,
            "bytes": self.bytes,
            "class": self.asset_class,
            "present": self.present,
            "source": self.source,
            "license": self.license,
        }


@dataclass(slots=True)
class Resolution:
    """Resultado determinista de resolver una capacidad."""

    capability: str
    state: str
    engine: str
    engine_prep: dict[str, Any]
    templates: list[str]
    selected_template: str | None
    assets: list[AssetNeed] = field(default_factory=list)
    missing: list[AssetNeed] = field(default_factory=list)
    missing_bytes: int = 0
    summary: str = ""
    registry_version: int = CAPABILITY_REGISTRY_VERSION
    #: Receta medida de la plantilla seleccionada, si existe (política, punto 5).
    recipe: dict[str, Any] | None = None
    #: Perfil de hardware con el que se juzgó la **receta medida** (ajuste de
    #: VRAM/RAM), no el veredicto de disponibilidad. Se propaga siempre: sin
    #: él, la receta se calcularía contra el objetivo por omisión.
    profile: hardware_profile.HardwareProfile | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "state": self.state,
            "engine": self.engine,
            "engine_prep": self.engine_prep,
            "templates": self.templates,
            "selected_template": self.selected_template,
            "summary": self.summary,
            "registry_version": self.registry_version,
            "assets": [a.to_public_dict() for a in self.assets],
            "missing": [a.to_public_dict() for a in self.missing],
            "missing_bytes": self.missing_bytes,
            "recipe": self.recipe,
            # El perfil se publica aquí y no solo dentro de `recipe` porque es
            # la máquina contra la que se juzgó la receta medida (su ajuste de
            # VRAM/RAM), y puede no haber receta que lo lleve. **No** es la base
            # del veredicto: `state` sale solo de la preparación del motor y de
            # la presencia de los activos en disco. Publicarlo siempre evita que
            # el agente tenga que hurgar en la receta —que quizá no existe—
            # para saber con qué máquina se midió.
            "profile": self.profile.to_dict() if self.profile is not None else None,
        }


#: Rutas candidatas del manifiesto de Fases 1/2, en el mismo orden que
#: `model_assets.manifest_path()` y `scripts/download-models.sh`: la explícita
#: del entorno, la del árbol de fuentes y la horneada en la imagen.
#:
#: Tres restricciones en tensión, reconciliadas así:
#:
#:   1. **`STUDIO_MANIFEST` no es de este catálogo.** En todo el sistema esa
#:      variable es el override del manifiesto de Fase 3 / studio
#:      (`model_assets`, `cache_policy`, `download-models.sh`, documentación).
#:      Los dos catálogos son disjuntos (26 entradas core frente a 87 de
#:      estudio, cero solape), así que leer el core con ella convierte cada
#:      activo de Fase 1/2 en `source='unknown'`, `bytes=0` y sin destino: se
#:      reintroduce por la puerta de atrás el modo de fallo —catálogo core
#:      ilegible— que esta lista de candidatos vino a corregir. De ahí que el
#:      override explícito tenga variable **propia** (`STUDIO_CORE_MANIFEST`),
#:      que ningún operario confundirá con la de Fase 3 —y que es, además, el
#:      nombre que el script ya usaba para el core (`download-models.sh`,
#:      `${STUDIO_CORE_MANIFEST:-/etc/comfyui/models-core.json}`).
#:   2. **El core debe seguir resolviéndose en la imagen.** `pip install
#:      /opt/studio` deja el módulo en `site-packages`, sin `config/` sobre él:
#:      se conservan la ruta del árbol de fuentes y la horneada en
#:      `/etc/comfyui/models-core.json`.
#:   3. **Un despliegue debe poder señalar otro manifiesto core** sin
#:      reconstruir la imagen: por eso el candidato de entorno sigue el primero
#:      y manda sobre las rutas horneadas, con la misma mecánica que en Fase 3.
#:
#: Es decir: misma capacidad de override, mismos destinos alternativos, y
#: fijar `STUDIO_MANIFEST` para lo que está documentado ya no puede dejar ciego
#: al catálogo core.
_CORE_MANIFEST_CANDIDATES = (
    "{STUDIO_CORE_MANIFEST}",
    "config/models-core.json",
    "/etc/comfyui/models-core.json",
)


def core_manifest_path(env: dict[str, str] | None = None) -> Path | None:
    """Ruta del manifiesto de Fases 1/2 allí donde de verdad esté.

    El paquete se instala con `pip install /opt/studio`, así que
    `Path(__file__).resolve().parents[2]` ya no es la raíz del repositorio sino
    el directorio de `site-packages` del venv: buscarlo solo ahí deja el
    catálogo core ilegible en la imagen. El manifiesto viaja horneado en
    `/etc/comfyui/models-core.json`, así que se resuelve con la misma lista de
    candidatos que el manifiesto de Fase 3 —pero con su propia variable de
    entorno, porque la de Fase 3 apunta a un catálogo disjunto (ver
    `_CORE_MANIFEST_CANDIDATES`).

    La expansión es la de `model_assets.manifest_path()`
    (`model_assets.expand_placeholders`, público): `{COMFY_APP_DIR}`/
    `{COMFY_DATA_DIR}` más `~`. Resolverla con `os.path.expanduser` a secas
    divergía del resolvedor de Fase 3 para un override que usara esos marcadores
    de posición.

    Las candidatas relativas se resuelven contra las bases **compartidas**
    (`model_assets.manifest_search_bases()`): la raíz del paquete primero y el
    directorio actual después. Antes el CWD iba primero aquí, así que lanzado
    desde un directorio con su propio `config/models-core.json` el resolvedor
    leía ese manifiesto mientras `cache_policy.manifest_paths()` leía el de la
    raíz del paquete: los dos componentes disentían sobre qué catálogo estaba en
    vigor sin que ninguno lo dijera.
    """
    environment = os.environ if env is None else env
    for candidate in _CORE_MANIFEST_CANDIDATES:
        raw = (
            environment.get("STUDIO_CORE_MANIFEST")
            if candidate == "{STUDIO_CORE_MANIFEST}"
            else candidate
        )
        if not raw:
            continue
        path = Path(model_assets.expand_placeholders(raw, environment))
        if path.is_absolute():
            if path.is_file():
                return path
            continue
        for base in model_assets.manifest_search_bases(__file__):
            resolved = base / path
            if resolved.is_file():
                return resolved
    return None


def _load_core_manifest(
    env: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Activos de Fases 1/2 indexados por nombre de archivo.

    Un manifiesto core ilegible o mal formado degrada a catálogo vacío —los
    activos salen honestamente `source='unknown'`, `bytes=0`— y **nunca**
    levanta: el manifiesto puede venir de `STUDIO_CORE_MANIFEST`, que escribe el
    operario, y una sola entrada inválida no puede tumbar `resolve()`, y con
    ella `list_capabilities` y `request_capability`.
    """
    path = core_manifest_path(env)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        entries = data.get("models", [])
        if not isinstance(entries, list):
            return {}
        indexed: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            indexed[filename] = entry
        return indexed
    except (OSError, ValueError):
        return {}


def _declared_bytes(value: Any) -> int:
    """Bytes declarados por una entrada de manifiesto, que es dato no confiable.

    `STUDIO_CORE_MANIFEST`/`STUDIO_MANIFEST` los escribe el operario, así que una
    entrada con `bytes` no numérica existe y no puede tumbar `resolve()` —y con
    ella `list_capabilities` y `request_capability`—. La regla es la misma que
    `model_assets.class_bytes()` y que `asset_acquire._declared_bytes`: degradar
    a 0 en lugar de propagar.

    D4: la regla es **compartida**, no heredada: `class_bytes()` capturaba solo
    `TypeError`/`ValueError`, así que degradaba los no numéricos pero propagaba
    los no finitos, y este docstring afirmaba una coincidencia que no existía.
    Los tres sitios tratan ahora `OverflowError` igual —`json.load` acepta
    `Infinity`, e `int(inf)` es un `ArithmeticError` que ninguno de los otros
    dos excepciones ve—.

    R94: `json.load` acepta el literal no estándar `Infinity`, así que un
    `"bytes": Infinity` en el manifiesto llega aquí como `float('inf`) y
    `int(inf)` responde `OverflowError` —un `ArithmeticError`, que no es ni
    `TypeError` ni `ValueError`—. Salía de `_asset_need` y de
    `needs_for_group`, y `_handle_download_model` las llama sin protección: el
    plan entero abortaba antes de que `acquire()` pudiera informar de nada. Es
    el mismo R85 que `asset_acquire._declared_bytes` ya cubre, un nivel más
    arriba de donde se consume: un tamaño no finito es ilegible, así que se
    trata como no declarado.
    """
    try:
        number = value or 0
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError(value)
        return int(number)
    except (TypeError, ValueError, OverflowError):
        LOG.warning(
            "bytes declarados no numéricos (%r): se tratan como no declarados", value
        )
        return 0


def _studio_entries(env: dict[str, str] | None) -> list[dict[str, Any]]:
    """Entradas del manifiesto de Fase 3, tolerando una forma inválida.

    `model_assets.entries()` da por hecho que `load_manifest()` devolvió un
    objeto y hace `manifest.get("models", [])` sobre él: un manifiesto de Fase 3
    que sea JSON válido pero no un objeto —alcanzable por `STUDIO_MANIFEST`, que
    escribe el operario— revienta con `AttributeError` dentro de `resolve()`.
    `cache_policy._manifest_entries()` ya descarta esa forma diciéndolo; el
    resolvedor no puede quedarse atrás, así que degrada a «sin entradas».
    """
    try:
        entries = model_assets.entries(env)
    except (AttributeError, TypeError):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _studio_entries_for_flag(flag: str, env: dict[str, str] | None) -> list[dict[str, Any]]:
    """Entradas de un grupo opt-in, con la misma tolerancia a forma inválida."""
    try:
        return list(model_assets.entries_for_flag(flag, env))
    except (AttributeError, TypeError):
        return []


def _asset_need(
    filename: str,
    core: dict[str, dict[str, Any]],
    env: dict[str, str] | None,
) -> AssetNeed:
    """Determina el origen y la presencia en disco de un activo declarado."""
    entry = core.get(filename)
    source = "core"
    if entry is None:
        # No está en el manifiesto core: se busca en el de Fase 3.
        for candidate in _studio_entries(env):
            if candidate.get("filename") == filename:
                entry = candidate
                source = "studio"
                break
    if entry is None:
        # Sin manifiesto: no se puede afirmar origen ni tamaño. Se declara
        # honestamente como desconocido en lugar de inventar una URL.
        return AssetNeed(
            filename=filename,
            dest_path="",
            bytes=0,
            asset_class="unknown",
            present=False,
            source="unknown",
        )

    dest = entry.get("dest_path") or ""
    path = model_assets.resolve_dest(dest, env) if dest else None
    present = bool(path and path.is_file() and path.stat().st_size > 0)
    return AssetNeed(
        filename=filename,
        dest_path=str(path) if path is not None else "",
        bytes=_declared_bytes(entry.get("bytes")),
        asset_class=str(entry.get("class") or "task"),
        present=present,
        source=source,
        repo_id=entry.get("repo_id"),
        remote_path=entry.get("remote_path"),
        repo_type=entry.get("repo_type") or "model",
        license=entry.get("license"),
        url=entry.get("url"),
        manifest_dest=dest,
        revision=entry.get("revision"),
        sha256=entry.get("sha256"),
        sha1=entry.get("sha1"),
    )


def needs_for_group(flag: str, *, env: dict[str, str] | None = None) -> list[AssetNeed]:
    """Activos de un grupo opt-in del manifiesto de Fase 3 (`STUDIO_INCLUDE_*`)."""
    core = _load_core_manifest(env)
    return [
        _asset_need(entry["filename"], core, env)
        for entry in _studio_entries_for_flag(flag, env)
    ]


def _default_store(env: dict[str, str] | None) -> Any | None:
    """Store por defecto para consultar recetas medidas (None si no hay datos)."""
    try:
        from comfy_studio.projects import ProjectStore

        return ProjectStore(_studio_data_dir(env))
    except Exception:  # noqa: BLE001, S110 - sin store no hay receta medida
        return None


def _studio_data_dir(env: dict[str, str] | None) -> Path:
    environment = os.environ if env is None else env
    explicit = (environment.get("STUDIO_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip()
    if comfy_data:
        return Path(comfy_data) / "studio"
    return Path("/workspace/comfyui-data/studio")


def list_capabilities() -> list[dict[str, Any]]:
    """Catálogo serializable de capacidades registradas."""
    return [
        {
            "id": capability_id,
            "engine": spec["engine"],
            "templates": list(spec["templates"]),
            "summary": spec["summary"],
        }
        for capability_id, spec in sorted(CAPABILITIES.items())
    ]


def resolve(
    capability_id: str,
    *,
    env: dict[str, str] | None = None,
    probe_engines: bool = True,
    store: Any | None = None,
    profile: hardware_profile.HardwareProfile | None = None,
) -> Resolution:
    """Resuelve una capacidad a motor + receta + activos, sin red ni GPU.

    `profile` es el perfil de hardware con el que se juzga la **receta medida**
    (su ajuste de VRAM/RAM), no el veredicto de disponibilidad, que sale de la
    preparación del motor y de los activos en disco. Si no se pasa, se resuelve
    del entorno (`STUDIO_HARDWARE_PROFILE`), que es lo que hace la
    configuración: la receta no puede juzgarse contra el objetivo por omisión
    cuando el pod tiene otro perfil.
    """
    active_profile = (
        profile if profile is not None else hardware_profile.from_env(env)
    )
    spec = CAPABILITIES.get(capability_id)
    if spec is None:
        return Resolution(
            capability=capability_id,
            state="unknown_capability",
            engine="",
            engine_prep={},
            templates=[],
            selected_template=None,
            summary=f"Capabilidad no registrada: {capability_id!r}",
            profile=active_profile,
        )

    engine = spec["engine"]
    prep: dict[str, Any] = {}
    if probe_engines:
        specs = build_default_prep_specs()
        engine_spec = specs.get(engine)
        if engine_spec is not None:
            prep = probe_preparedness(engine_spec, env=env).to_public_dict()

    registry = get_default_registry()
    templates = [t for t in spec["templates"] if registry.get_raw_definition(t) is not None]
    selected = templates[0] if templates else None

    assets: list[AssetNeed] = []
    if selected is not None:
        definition = registry.get_raw_definition(selected)
        declared = list(definition.get("models", []) or [])
        core = _load_core_manifest(env)
        for filename in declared:
            assets.append(_asset_need(filename, core, env))

    missing = [a for a in assets if not a.present]
    missing_bytes = sum(a.bytes for a in missing)

    # Receta medida: si este pod ya renderó esta plantilla, se reutiliza el
    # resultado en lugar de estimar desde cero. Es opcional: si no hay store o
    # no hay mediciones, se resuelve igual. El perfil se propaga siempre: una
    # receta de 40 GB no puede juzgarse contra el objetivo por omisión cuando
    # el perfil real tiene 96 GB (o al revés).
    recipe: dict[str, Any] | None = None
    if selected is not None:
        active_store = store if store is not None else _default_store(env)
        if active_store is not None:
            try:
                recipe = active_store.recipe_for_template(selected, profile=active_profile)
            except Exception:  # noqa: BLE001 - la receta medida nunca rompe la resolución
                recipe = None

    if prep.get("state") not in (None, "", "ready"):
        state = "engine_unprepared"
    elif missing:
        state = "assets_missing"
    else:
        state = "ready"

    return Resolution(
        capability=capability_id,
        state=state,
        engine=engine,
        engine_prep=prep,
        templates=templates,
        selected_template=selected,
        assets=assets,
        missing=missing,
        missing_bytes=missing_bytes,
        summary=spec["summary"],
        recipe=recipe,
        profile=active_profile,
    )
