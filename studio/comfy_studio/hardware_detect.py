"""Detección del hardware REAL y acotamiento del perfil solicitado.

Defecto P0 #7 del plan Ultra (W02 / acceptance A02): `hardware_profile.from_env`
dejaba que una variable de entorno declarara 96 GB de VRAM y 256 GB de RAM sin
comprobar que la máquina los tuviera. «Una variable de entorno no puede inventar
VRAM/RAM» (requirements R16).

Este módulo:

- lee la VRAM real por GPU con `nvidia-smi` (sin dependencias nuevas; el pod ya
  lo tiene). CPU-only / driver ausente => `available=False`, nunca una GPU
  emulada compatible;
- lee el límite de RAM real: cgroup v2 (`memory.max`), cgroup v1
  (`memory.limit_in_bytes`) y `/proc/meminfo` como techo, tomando el **menor**
  (un cgroup puede limitar por debajo de la RAM física);
- intersecta el perfil solicitado con esos límites: el perfil efectivo nunca
  excede el hardware real. Si no se puede medir, se reporta `unknown` y se
  conserva el perfil nominal marcándolo como no verificado, en lugar de
  afirmar una capacidad que no se comprobó.

Todo es best-effort y portable: en Windows/macOS de desarrollo la detección
devuelve `unknown` sin reventar; la autoridad del host sigue siendo
`comfyctl strict-check` en el pod Linux.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from comfy_studio.hardware_profile import DEFAULT_PROFILE, HardwareProfile, from_env

MiB = 1024 * 1024
GiB = 1024**3


@dataclass(frozen=True, slots=True)
class DetectedHardware:
    """Lo que de verdad hay en la máquina (no lo que pide una variable)."""

    gpu_available: bool
    gpu_name: str | None
    #: VRAM total de la GPU objetivo, en MiB (None = no medible).
    vram_total_mb: int | None
    #: Límite de RAM efectivo (menor de físico y cgroup), en bytes (None = ?).
    ram_limit_bytes: int | None
    source: str  # "nvidia-smi+cgroup", "unknown", ...

    def to_public_dict(self) -> dict[str, object]:
        return {
            "gpu_available": self.gpu_available,
            "gpu_name": self.gpu_name,
            "vram_total_mb": self.vram_total_mb,
            "vram_total_gb": (
                round(self.vram_total_mb / 1024, 1) if self.vram_total_mb else None
            ),
            "ram_limit_bytes": self.ram_limit_bytes,
            "ram_limit_gb": (
                round(self.ram_limit_bytes / GiB, 1) if self.ram_limit_bytes else None
            ),
            "source": self.source,
        }


def _nvidia_smi_bin(env: dict[str, str] | None) -> str:
    environment = os.environ if env is None else env
    explicit = (environment.get("COMFY_NVIDIA_SMI_BIN") or "").strip()
    return explicit or "nvidia-smi"


def detect_gpu(env: dict[str, str] | None = None) -> tuple[bool, str | None, int | None]:
    """(disponible, nombre, vram_total_mb) leído de `nvidia-smi`.

    Sin binario o sin GPU accesible: `(False, None, None)` — modo CPU honesto,
    no una GPU emulada.
    """
    bin_name = _nvidia_smi_bin(env)
    if shutil.which(bin_name) is None and not Path(bin_name).exists():
        return (False, None, None)
    try:
        proc = subprocess.run(  # noqa: S603
            [
                bin_name,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return (False, None, None)
    if proc.returncode != 0:
        return (False, None, None)
    first = next((ln for ln in proc.stdout.splitlines() if ln.strip()), "")
    if not first:
        return (False, None, None)
    parts = [p.strip() for p in first.split(",")]
    name = parts[0] if parts and parts[0] else None
    vram_mb: int | None = None
    if len(parts) >= 2:
        try:
            vram_mb = int(float(parts[1]))  # nvidia-smi reporta MiB con nounits
        except ValueError:
            vram_mb = None
    return (True, name, vram_mb)


def detect_ram_limit_bytes() -> int | None:
    """Límite de RAM efectivo: el MENOR de RAM física y límite de cgroup.

    Un cgroup puede recortar por debajo de la RAM física del host; usar la
    física a secas sobreestimaría lo disponible para el proceso.
    """
    candidates: list[int] = []

    # RAM física (/proc/meminfo: MemTotal en kB).
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    candidates.append(kb * 1024)
                    break
        except (OSError, ValueError, IndexError):
            pass

    # cgroup v2.
    v2 = Path("/sys/fs/cgroup/memory.max")
    if v2.is_file():
        try:
            raw = v2.read_text().strip()
            if raw and raw != "max":
                candidates.append(int(raw))
        except (OSError, ValueError):
            pass

    # cgroup v1.
    v1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v1.is_file():
        try:
            value = int(v1.read_text().strip())
            # cgroup v1 usa un valor gigantesco como "sin límite"; se descarta.
            if 0 < value < (1 << 62):
                candidates.append(value)
        except (OSError, ValueError):
            pass

    if not candidates:
        return None
    return min(candidates)


def detect_hardware(env: dict[str, str] | None = None) -> DetectedHardware:
    available, name, vram_mb = detect_gpu(env)
    ram_limit = detect_ram_limit_bytes()
    if not available and vram_mb is None and ram_limit is None:
        source = "unknown"
    else:
        source = "nvidia-smi+cgroup"
    return DetectedHardware(
        gpu_available=available,
        gpu_name=name,
        vram_total_mb=vram_mb,
        ram_limit_bytes=ram_limit,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class EffectiveProfile:
    """Perfil solicitado acotado por el hardware real."""

    requested: HardwareProfile
    effective: HardwareProfile
    detected: DetectedHardware
    vram_capped: bool
    ram_capped: bool
    verified: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested.to_dict(),
            "effective": self.effective.to_dict(),
            "detected": self.detected.to_public_dict(),
            "vram_capped": self.vram_capped,
            "ram_capped": self.ram_capped,
            "verified": self.verified,
        }


def effective_profile(
    env: dict[str, str] | None = None,
    *,
    detected: DetectedHardware | None = None,
) -> EffectiveProfile:
    """Intersecta el perfil pedido (env) con el hardware real detectado.

    - La VRAM/RAM efectiva NUNCA supera lo detectado: una variable no inventa
      memoria (A02). Si lo detectado es mayor o no medible, se conserva lo
      pedido.
    - `verified` es True solo si se pudo medir el hardware; si no, el perfil se
      devuelve tal cual pero marcado como no verificado — honesto en vez de
      afirmar una capacidad no comprobada.
    """
    requested = from_env(env)
    hw = detected if detected is not None else detect_hardware(env)

    eff = requested
    vram_capped = False
    ram_capped = False

    if hw.vram_total_mb is not None:
        detected_vram_gb = hw.vram_total_mb // 1024
        if detected_vram_gb < requested.vram_gb:
            eff = replace(eff, vram_gb=detected_vram_gb)
            vram_capped = True

    if hw.ram_limit_bytes is not None:
        detected_ram_gb = int(hw.ram_limit_bytes // GiB)
        if detected_ram_gb < requested.ram_gb:
            eff = replace(eff, ram_gb=detected_ram_gb)
            ram_capped = True

    verified = hw.source != "unknown"
    return EffectiveProfile(
        requested=requested,
        effective=eff,
        detected=hw,
        vram_capped=vram_capped,
        ram_capped=ram_capped,
        verified=verified,
    )


__all__ = [
    "DEFAULT_PROFILE",
    "DetectedHardware",
    "EffectiveProfile",
    "detect_gpu",
    "detect_hardware",
    "detect_ram_limit_bytes",
    "effective_profile",
]
