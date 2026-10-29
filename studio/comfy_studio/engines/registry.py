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
            note="runner existente (blender.py); adaptador de contrato pendiente",
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
            note="STT real vía worker ASR; TTS requiere integrar síntesis",
        )
    )
    reg.register(
        EngineEntry(
            "voice_clone",
            EngineTier.WARM,
            wired=True,
            note="RVC: clonar/sintetizar voz; modelo pinned en imagen",
        )
    )
    reg.register(
        EngineEntry(
            "music_gen",
            EngineTier.WARM,
            wired=True,
            note="MusicGen/Stable Audio; modelo pinned en imagen",
        )
    )
    reg.register(
        EngineEntry(
            "lipsync",
            EngineTier.WARM,
            wired=True,
            note="Wav2Lip-style; modelo pinned en imagen",
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
            note="Nerfstudio NeRF/Gaussian Splatting; binarios pinned en imagen",
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
    return reg
