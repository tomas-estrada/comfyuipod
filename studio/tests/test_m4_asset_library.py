"""Tests de la librería de assets / continuidad (M4-SCR-004, F4)."""

from __future__ import annotations

import pytest

from comfy_studio.engines.asset_library import (
    AssetError,
    AssetKind,
    AssetLibrary,
)


def test_register_and_resolve() -> None:
    lib = AssetLibrary()
    lib.register("protagonista", AssetKind.CHARACTER, "/workspace/char.safetensors", "comfyui")
    a = lib.resolve("protagonista")
    assert a.kind is AssetKind.CHARACTER
    assert a.produced_by == "comfyui"
    assert a.version == 1


def test_resolve_missing_fails_loudly() -> None:
    lib = AssetLibrary()
    with pytest.raises(AssetError, match="constrúyelo antes"):
        lib.resolve("coche_rojo")


def test_reregister_bumps_version_not_overwrite() -> None:
    lib = AssetLibrary()
    lib.register("coche", AssetKind.MESH3D, "/workspace/car_v1.glb", "comfyui")
    a2 = lib.register("coche", AssetKind.MESH3D, "/workspace/car_v2.glb", "blender")
    assert a2.version == 2
    assert lib.resolve("coche").path == "/workspace/car_v2.glb"


def test_empty_id_rejected() -> None:
    lib = AssetLibrary()
    with pytest.raises(AssetError, match="vacío"):
        lib.register("  ", AssetKind.VOICE, "/workspace/v.wav", "audio")


def test_by_kind_filters() -> None:
    lib = AssetLibrary()
    lib.register("prota", AssetKind.CHARACTER, "/w/a", "comfyui")
    lib.register("villano", AssetKind.CHARACTER, "/w/b", "comfyui")
    lib.register("coche", AssetKind.MESH3D, "/w/c", "blender")
    chars = lib.by_kind(AssetKind.CHARACTER)
    assert {a.asset_id for a in chars} == {"prota", "villano"}
    assert len(lib.by_kind(AssetKind.MESH3D)) == 1


def test_continuity_same_asset_across_scenes() -> None:
    # El mismo id resuelve al mismo artefacto en escena 1 y escena 7.
    lib = AssetLibrary()
    lib.register("prota", AssetKind.CHARACTER, "/workspace/prota.safetensors", "comfyui")
    scene1 = lib.resolve("prota")
    scene7 = lib.resolve("prota")
    assert scene1.path == scene7.path  # sin deriva de personaje


def test_roundtrip_serialization() -> None:
    lib = AssetLibrary()
    lib.register("prota", AssetKind.CHARACTER, "/w/a", "comfyui", meta={"seed": 42})
    lib.register("coche", AssetKind.MESH3D, "/w/c", "blender")
    restored = AssetLibrary.from_dict(lib.to_dict())
    assert restored.ids() == ["coche", "prota"]
    assert restored.resolve("prota").meta["seed"] == 42
