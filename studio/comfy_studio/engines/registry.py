"""Registro de motores del estudio (M4-SCR-001).

Un lugar único donde el agente y strict-check descubren los motores de forma
uniforme. Es el punto que convierte los adaptadores en herramientas que el
agente ve "out of the box".

Diseño:
  - El roster CALIENTE es fijo y horneado (ComfyUI, Blender, editor, ...). El
    agente no instala motores aquí; eso es la puerta de instalación (F6).
  - Cada entrada declara su `tier` (resident / warm / installable) para que el
    perfil de VRAM (M4-OPS-001) decida quién está encendido.
  - `describe()` produce un resumen serializable (sin objetos vivos) para el
    API del panel y para una sonda strict-check por motor (F7).
  - El registro NO arranca binarios ni toca la GPU: solo cataloga. La salud
    real la consulta cada motor bajo demanda (verb `verify` / health del motor).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from comfy_studio.engines import EngineTier, is_dynamic_enabled


@dataclass(slots=True)
class EngineEntry:
    """Una entrada del roster: identidad + tier + si está cableada a un backend real."""

    name: str
    tier: EngineTier
    #: True si el motor tiene un backend real conectado; False = solo stub/declarado.
    wired: bool
    #: Nota corta de estado, p.ej. "pendiente decisión Remotion vs OTIO+ffmpeg".
    note: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": str(self.tier),
            "wired": self.wired,
            "note": self.note,
        }


class EngineRegistry:
    """Catálogo de motores calientes. No arranca nada; solo describe."""

    def __init__(self) -> None:
        self._entries: dict[str, EngineEntry] = {}

    def register(self, entry: EngineEntry) -> None:
        if entry.name in self._entries:
            raise ValueError(f"motor duplicado en el registro: {entry.name!r}")
        self._entries[entry.name] = entry

    def get(self, name: str) -> EngineEntry | None:
        return self._entries.get(name)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def resident(self) -> list[EngineEntry]:
        return [e for e in self._entries.values() if e.tier == EngineTier.RESIDENT]

    def describe(self) -> dict[str, Any]:
        """Resumen serializable del roster para el API del panel y strict-check."""
        return {
            "dynamic": is_dynamic_enabled(),
            "count": len(self._entries),
            "engines": [self._entries[n].to_public_dict() for n in self.names()],
        }

    def contract_violations(self) -> dict[str, list[str]]:
        """Violaciones del contrato de 4 verbos por motor (barrido estructural).

        Resuelve cada clase perezosamente y aplica `engine_contract_violations`;
        un roster sano devuelve un dict vacío.
        """
        from comfy_studio.engines.base import engine_contract_violations

        out: dict[str, list[str]] = {}
        for name in self.names():
            entry = self._entries[name]
            try:
                cls = resolve_engine_class(name)
            except KeyError as exc:
                out[name] = [f"{name}: {exc}"]
                continue
            problems = engine_contract_violations(cls, name=name, tier=entry.tier)
            if problems:
                out[name] = problems
        return out


def build_default_registry() -> EngineRegistry:
    """Roster caliente inicial del estudio (M4-SCR-002).

    `wired` refleja el estado real del código M4 hasta ahora: ComfyUI está
    cableado a su cliente; el editor existe en stub a la espera de la decisión
    de backend; los motores de vídeo pesado y audio están declarados pero aún
    sin adaptador (fases F5+).
    """
    reg = EngineRegistry()
    reg.register(EngineEntry("comfyui", EngineTier.RESIDENT, wired=True))
    reg.register(
        EngineEntry(
            "blender",
            EngineTier.WARM,
            wired=True,
            note=(
                "Blender 5.2.4 LTS headless está fijado por SHA-256 en la imagen; "
                "los pesos externos siguen sujetos a su disponibilidad"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "editor",
            EngineTier.WARM,
            wired=True,
            note="Remotion backend cableado (M4); render vía npx remotion",
        )
    )
    reg.register(
        EngineEntry(
            "ffmpeg",
            EngineTier.WARM,
            wired=True,
            note="disponible en el pod; usado por otros motores",
        )
    )
    reg.register(
        EngineEntry(
            "heavy_video",
            EngineTier.INSTALLABLE,
            wired=True,
            note="cliente Minimax cableado (M5); env STUDIO_HEAVY_VIDEO_*",
        )
    )
    reg.register(
        EngineEntry(
            "audio",
            EngineTier.WARM,
            wired=True,
            note="STT real vía worker ASR (pesos faster-whisper); TTS Kokoro horneado",
        )
    )
    reg.register(
        EngineEntry(
            "voice_clone",
            EngineTier.WARM,
            wired=True,
            note=(
                "RVC: conversión de voz; requiere fairseq (NO horneado) — "
                "para clonación usable ver 'indextts'"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "indextts",
            EngineTier.WARM,
            wired=True,
            note=(
                "IndexTTS-2.5: clonación zero-shot texto+referencia->voz; "
                "pesos grupo indextts (5.11 GiB) descargados; venv aislado runtime"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "music_gen",
            EngineTier.WARM,
            wired=True,
            note=(
                "MusicGen/Stable Audio: audiocraft horneado --no-deps; "
                "deps pesadas y pesos en runtime"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "lipsync",
            EngineTier.WARM,
            wired=True,
            note="Wav2Lip-style: repo horneado por SHA; checkpoint wav2lip_gan.pth en runtime",
        )
    )
    reg.register(
        EngineEntry(
            "upscaler",
            EngineTier.WARM,
            wired=True,
            note="Real-ESRGAN super-res; modelo pinned en imagen",
        )
    )
    reg.register(
        EngineEntry(
            "capture3d",
            EngineTier.INSTALLABLE,
            wired=True,
            note=(
                "Nerfstudio horneado; binarios pinned en imagen y "
                "tiny-cuda-nn compilado en el volumen"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "compositor",
            EngineTier.WARM,
            wired=True,
            note="Natron compositing node-based; binario pinned en imagen",
        )
    )
    reg.register(
        EngineEntry(
            "qa",
            EngineTier.WARM,
            wired=True,
            note="QA de vídeo: FFmpeg (nivel 1 horneado) + VBench (nivel 2, venv aislado runtime)",
        )
    )
    reg.register(
        EngineEntry(
            "roto",
            EngineTier.WARM,
            wired=True,
            note="Rotoscopia por texto (SAM 3); paquete horneado, pesos HF-gated en runtime",
        )
    )
    reg.register(
        EngineEntry(
            "ad_studio",
            EngineTier.WARM,
            wired=True,
            note=(
                "Estudio de anuncios: brief->guion->storyboard->variantes 9:16/1:1/16:9 "
                "con ffmpeg + TTS/música/QA existentes (M7)"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "reference-local",
            EngineTier.WARM,
            wired=True,
            note=(
                "Análisis local de referencias: cortes/beats/OCR/cámara -> receta editable "
                "(inspiration.py; CPU)"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "atlas-offline",
            EngineTier.WARM,
            wired=True,
            note=(
                "Bundle del atlas del Aburrá: catálogo + archivos + licencias + OSM "
                "(atlas/packaging.py; CPU)"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "scene-optimizer",
            EngineTier.WARM,
            wired=True,
            note=(
                "Validación de composición y plan de cámara: colisiones, FOV, encuadre "
                "(cinematography.py; CPU)"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "chrono",
            EngineTier.WARM,
            wired=True,
            note=(
                "Proxy cinemático CPU de física/articulaciones: simulación determinista "
                "(physics.py; no es el solver Chrono/Blender real)"
            ),
        )
    )
    reg.register(
        EngineEntry(
            "llm_server",
            EngineTier.WARM,
            wired=True,
            note=(
                "LLM local OpenAI-compatible; llama-server CPU está incluido en la "
                "imagen; el proceso no se supervisa y los pesos .gguf no incluidos "
                "deben configurarse en runtime; se admite STUDIO_LLM_SERVER_BIN "
                "para reemplazar el binario"
            ),
        )
    )
    return reg


#: Roster -> clase de motor (`modulo:Clase`). Todo nombre del roster resuelve
#: a una clase con los 4 verbos; toda clase con `name` está en el roster.
#: La resolución es perezosa (import bajo demanda, sin efectos laterales).
ENGINE_CLASSES: dict[str, str] = {
    "ad_studio": "comfy_studio.engines.ad_studio:AdStudioEngine",
    "atlas-offline": "comfy_studio.engines.atlas_offline:AtlasOfflineEngine",
    "audio": "comfy_studio.engines.audio:AudioEngine",
    "blender": "comfy_studio.engines.blender:BlenderEngine",
    "capture3d": "comfy_studio.engines.capture3d:Capture3dEngine",
    "chrono": "comfy_studio.engines.chrono:ChronoEngine",
    "comfyui": "comfy_studio.engines.comfyui:ComfyUIEngine",
    "compositor": "comfy_studio.engines.compositor:CompositorEngine",
    "editor": "comfy_studio.engines.editor:EditorEngine",
    "ffmpeg": "comfy_studio.engines.ffmpeg:FfmpegEngine",
    "heavy_video": "comfy_studio.engines.heavy_video:HeavyVideoEngine",
    "indextts": "comfy_studio.engines.indextts:IndexttsEngine",
    "lipsync": "comfy_studio.engines.lipsync:LipsyncEngine",
    "llm_server": "comfy_studio.engines.llm_server:LlmServerEngine",
    "music_gen": "comfy_studio.engines.music_gen:MusicGenEngine",
    "qa": "comfy_studio.engines.qa:QaEngine",
    "reference-local": "comfy_studio.engines.reference_local:ReferenceLocalEngine",
    "roto": "comfy_studio.engines.roto:RotoEngine",
    "scene-optimizer": "comfy_studio.engines.scene_optimizer:SceneOptimizerEngine",
    "upscaler": "comfy_studio.engines.upscaler:UpscalerEngine",
    "voice_clone": "comfy_studio.engines.voice_clone:VoiceCloneEngine",
}


def resolve_engine_class(name: str) -> type:
    """Importa y devuelve la clase del motor (sin instanciarla)."""
    import importlib

    try:
        module_name, class_name = ENGINE_CLASSES[name].split(":")
    except KeyError:
        raise KeyError(f"motor sin clase registrada: {name!r}") from None
    module = importlib.import_module(module_name)
    try:
        cls = getattr(module, class_name)
    except AttributeError:
        raise KeyError(f"clase ausente {class_name!r} en {module_name}") from None
    return cls
