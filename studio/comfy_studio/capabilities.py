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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from comfy_studio import model_assets
from comfy_studio.engines.preparedness import (
    build_default_prep_specs,
    probe_preparedness,
)
from comfy_studio.templates import get_default_registry

CAPABILITY_REGISTRY_VERSION = 1

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
        }


def _load_core_manifest() -> dict[str, dict[str, Any]]:
    """Activos de Fases 1/2 indexados por nombre de archivo."""
    path = Path(__file__).resolve().parents[2] / "config" / "models-core.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {entry["filename"]: entry for entry in data.get("models", [])}


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
        for candidate in model_assets.entries(env):
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
        bytes=int(entry.get("bytes") or 0),
        asset_class=str(entry.get("class") or "task"),
        present=present,
        source=source,
        repo_id=entry.get("repo_id"),
        remote_path=entry.get("remote_path"),
        repo_type=entry.get("repo_type") or "model",
        license=entry.get("license"),
        url=entry.get("url"),
        manifest_dest=dest,
    )


def needs_for_group(flag: str, *, env: dict[str, str] | None = None) -> list[AssetNeed]:
    """Activos de un grupo opt-in del manifiesto de Fase 3 (`STUDIO_INCLUDE_*`)."""
    core = _load_core_manifest()
    return [
        _asset_need(entry["filename"], core, env)
        for entry in model_assets.entries_for_flag(flag, env)
    ]


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
) -> Resolution:
    """Resuelve una capacidad a motor + receta + activos, sin red ni GPU."""
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
        core = _load_core_manifest()
        for filename in declared:
            assets.append(_asset_need(filename, core, env))

    missing = [a for a in assets if not a.present]
    missing_bytes = sum(a.bytes for a in missing)

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
    )
