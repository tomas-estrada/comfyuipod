"""Tests para detección de hardware y acotamiento del perfil efectivo.

CPU puro: `effective_profile` acepta `detected=` inyectado, así que no se
invoca nvidia-smi ni se depende de cgroups reales.
"""

from __future__ import annotations

from comfy_studio.hardware_detect import DetectedHardware, effective_profile

GiB = 1024**3


def test_effective_profile_caps_to_detected_hardware() -> None:
    """VRAM/RAM detectadas menores que lo pedido => perfil capado y verificado."""
    detected = DetectedHardware(
        gpu_available=True,
        gpu_name="NVIDIA Test GPU",
        vram_total_mb=48 * 1024,  # 48 GB
        ram_limit_bytes=64 * GiB,  # 64 GB
        source="nvidia-smi+cgroup",
    )
    env = {
        "STUDIO_TARGET_VRAM_GB": "96",
        "STUDIO_TARGET_RAM_GB": "128",
    }

    result = effective_profile(env, detected=detected)

    assert result.requested.vram_gb == 96
    assert result.requested.ram_gb == 128
    assert result.effective.vram_gb == 48
    assert result.effective.ram_gb == 64
    assert result.vram_capped is True
    assert result.ram_capped is True
    assert result.verified is True
    assert result.detected is detected


def test_effective_profile_unknown_source_not_capped_and_unverified() -> None:
    """Sin hardware medible (source=unknown): conserva lo pedido, no verificado."""
    detected = DetectedHardware(
        gpu_available=False,
        gpu_name=None,
        vram_total_mb=None,
        ram_limit_bytes=None,
        source="unknown",
    )
    env = {
        "STUDIO_TARGET_VRAM_GB": "96",
        "STUDIO_TARGET_RAM_GB": "128",
    }

    result = effective_profile(env, detected=detected)

    assert result.requested.vram_gb == 96
    assert result.requested.ram_gb == 128
    assert result.effective.vram_gb == result.requested.vram_gb
    assert result.effective.ram_gb == result.requested.ram_gb
    assert result.vram_capped is False
    assert result.ram_capped is False
    assert result.verified is False


def test_effective_profile_uses_detected_ram_above_preference() -> None:
    """A 200 GB pod uses measured capacity even with a 128 GB preference."""
    detected = DetectedHardware(
        gpu_available=True,
        gpu_name="NVIDIA Test GPU",
        vram_total_mb=96 * 1024,
        ram_limit_bytes=200 * GiB,
        source="nvidia-smi+cgroup",
    )

    result = effective_profile({"STUDIO_TARGET_RAM_GB": "128"}, detected=detected)

    assert result.requested.ram_gb == 128
    assert result.effective.ram_gb == 200
    assert result.effective.ram_usable_gb == 192
    assert result.ram_capped is False
    assert result.detected.ram_limit_bytes == 200 * GiB
