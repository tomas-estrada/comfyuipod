"""Perfil de hardware de ejecución (arquitectura vigente).

Perfil principal: **una RTX PRO 6000 Blackwell con 96 GB de VRAM**, con RAM de
host en perfiles separados de **128 y 256 GB**.

Reglas que este módulo hace explícitas:

- **RAM y VRAM se evalúan por separado.** La RAM de host no es intercambiable
  con VRAM: sirve para offloading, a costa de tiempo, y nunca amplía el espacio
  de pesos que cabe en el GPU.
- **Unidades explícitas y reserva operativa.** Los GB del datasheet no son los
  GB utilizables: el sistema operativo, el driver, otros procesos y la
  fragmentación se comen una reserva antes de que un render pueda usarlos.
- **Un solo trabajo GPU pesado a la vez** (la exclusión mutua vive en los
  ejecutores); el perfil solo describe la máquina, no la política de cola.
- Los perfiles menores se mantienen: el de ~32 GB sigue existiendo para ofertas
  antiguas, pero **no limita al principal**.

El perfil se transmite desde la configuración a la API, el resolvedor, las
recetas y el diagnóstico, para que todos juzguen con el mismo número.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Máquina de ejecución objetivo.

    `vram_gb`/`ram_gb` son lo que anuncia la oferta; `*_usable_gb` es lo que
    queda tras la reserva operativa, que es contra lo que se compara una
    medición.
    """

    name: str
    gpu: str
    vram_gb: int
    ram_gb: int
    vram_reserve_gb: float = 2.0
    ram_reserve_gb: float = 8.0
    notes: str = ""

    @property
    def vram_usable_gb(self) -> float:
        return max(self.vram_gb - self.vram_reserve_gb, 0.0)

    @property
    def ram_usable_gb(self) -> float:
        return max(self.ram_gb - self.ram_reserve_gb, 0.0)

    @property
    def vram_usable_mb(self) -> int:
        return int(self.vram_usable_gb * 1024)

    @property
    def ram_usable_mb(self) -> int:
        return int(self.ram_usable_gb * 1024)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "gpu": self.gpu,
            "vram_gb": self.vram_gb,
            "ram_gb": self.ram_gb,
            "vram_reserve_gb": self.vram_reserve_gb,
            "ram_reserve_gb": self.ram_reserve_gb,
            "vram_usable_gb": self.vram_usable_gb,
            "ram_usable_gb": self.ram_usable_gb,
            "notes": self.notes,
        }


#: Perfil principal: RTX PRO 6000 Blackwell, 96 GB de VRAM.
BLACKWELL_96_128 = HardwareProfile(
    name="rtx-pro-6000-blackwell-96-128",
    gpu="NVIDIA RTX PRO 6000 Blackwell (96 GB)",
    vram_gb=96,
    ram_gb=128,
    notes="Perfil principal. Generador completo en VRAM cuando sea viable.",
)

#: Mismo GPU con la RAM alta: para cargas con offload grande o lotes anchos.
BLACKWELL_96_256 = HardwareProfile(
    name="rtx-pro-6000-blackwell-96-256",
    gpu="NVIDIA RTX PRO 6000 Blackwell (96 GB)",
    vram_gb=96,
    ram_gb=256,
    notes="RAM alta para offload de componentes completos.",
)

#: Perfil menor conservado: ofertas de consumo antiguas. No limita al principal.
CONSUMER_32 = HardwareProfile(
    name="consumer-gpu-32",
    gpu="GPU de consumo (~32 GB)",
    vram_gb=32,
    ram_gb=64,
    vram_reserve_gb=2.0,
    ram_reserve_gb=6.0,
    notes="Perfil menor heredado; se mantiene por compatibilidad.",
)

#: Perfil por defecto de la arquitectura vigente.
DEFAULT_PROFILE = BLACKWELL_96_128

#: Perfiles de RAM reconocidos para el GPU principal (objetivo, punto 3).
RAM_OPTIONS_GB: tuple[int, ...] = (128, 256)

_PROFILES: dict[str, HardwareProfile] = {
    p.name: p
    for p in (BLACKWELL_96_128, BLACKWELL_96_256, CONSUMER_32)
}

#: Alias cortos para seleccionar sin escribir el nombre completo.
_ALIASES: dict[str, str] = {
    "96": BLACKWELL_96_128.name,
    "96gb": BLACKWELL_96_128.name,
    "blackwell": BLACKWELL_96_128.name,
    "blackwell-96": BLACKWELL_96_128.name,
    "rtx-pro-6000": BLACKWELL_96_128.name,
    "96-256": BLACKWELL_96_256.name,
    "blackwell-96-256": BLACKWELL_96_256.name,
    "32": CONSUMER_32.name,
    "consumer": CONSUMER_32.name,
}


def known_profiles() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def resolve_profile(name: str | None) -> HardwareProfile:
    """Resuelve un perfil por nombre o alias; desconocido cae al principal."""
    if not name:
        return DEFAULT_PROFILE
    key = str(name).strip().lower()
    resolved = _ALIASES.get(key, key)
    return _PROFILES.get(resolved, DEFAULT_PROFILE)


def with_ram(profile: HardwareProfile, ram_gb: int) -> HardwareProfile:
    """Mismo GPU con otro perfil de RAM (128 o 256 GB)."""
    return replace(profile, ram_gb=int(ram_gb))


def from_env(env: dict[str, str] | None = None) -> HardwareProfile:
    """Perfil según el entorno.

    `STUDIO_HARDWARE_PROFILE` selecciona; `STUDIO_TARGET_VRAM_GB` y
    `STUDIO_TARGET_RAM_GB` siguen funcionando como override explícito, para no
    romper despliegues que ya los usaban.
    """
    environment = os.environ if env is None else env
    profile = resolve_profile(environment.get("STUDIO_HARDWARE_PROFILE"))

    vram_override = (environment.get("STUDIO_TARGET_VRAM_GB") or "").strip()
    if vram_override:
        try:
            profile = replace(profile, vram_gb=int(float(vram_override)))
        except ValueError:
            pass

    ram_override = (environment.get("STUDIO_TARGET_RAM_GB") or "").strip()
    if ram_override:
        try:
            profile = replace(profile, ram_gb=int(float(ram_override)))
        except ValueError:
            pass

    return profile
