"""Validación de artefactos de Blender a nivel de run (CPU-only, sin GPU).

Contrato: `run_blender_spec` NUNCA devuelve ok con artefactos inválidos.
- modo render: antes no tenía NINGÚN check de salida (el export sí); ahora un
  render que acaba exit-0 sin frames, o con PNG/EXR podridos, falla tipado
  (`blender_invalid_output`).
- export glTF reclamado: el `.glb` producido debe llevar magic bytes `glTF`; un
  `.glb` truncado/corrupto se rechaza.
- el modo stub (STUDIO_STUB_BLENDER=1) pasa por la misma validación.

Los tests del camino subprocess real usan un binario `blender` FALSO (bash),
igual que `test_m5_capture3d.py` usa `ns-*` falsos.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from comfy_studio.blender import (
    _STUB_EXR,
    _STUB_GLB_HEADER,
    _STUB_PNG,
    BlenderExportSpec,
    BlenderRenderSpec,
    run_blender_spec,
)
from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.blender import BlenderEngine


def _fake_blender(tmp_path: Path, body: str) -> str:
    """Crea un binario `blender` falso (bash) que corre su `body` y sale 0."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "blender"
    fake.write_text(f"#!/bin/bash\n{body}\nexit 0\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return str(fake)


def _render_spec(tmp_path: Path, output_dir: str | None = None) -> BlenderRenderSpec:
    scene = tmp_path / "car.glb"
    scene.write_bytes(b"glTF\x02\x00\x00\x00")
    return BlenderRenderSpec(
        scene_path=scene,
        output_dir=Path(output_dir or (tmp_path / "out")),
        frames=1,
        timeout_s=30.0,
    )


def _export_spec(tmp_path: Path, output_dir: str | None = None) -> BlenderExportSpec:
    scene = tmp_path / "car.glb"
    scene.write_bytes(b"glTF\x02\x00\x00\x00")
    return BlenderExportSpec(
        scene_path=scene,
        output_dir=Path(output_dir or (tmp_path / "exp")),
        formats=("usd", "gltf", "fbx"),
        timeout_s=30.0,
    )


# --- modo render: el check de salida que no existía -------------------------


def test_render_with_no_outputs_fails_typed(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, 'echo "fake render wrote nothing"')
    result = asyncio.run(
        run_blender_spec(_render_spec(tmp_path), log_dir=tmp_path / "logs", blender_bin=fake)
    )
    assert result.ok is False
    assert result.validation_error is not None
    assert "render sin artefactos" in result.validation_error
    assert result.exit_code == 0  # el proceso acabó bien; el artefacto no existe


def test_render_with_corrupt_png_fails_typed(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, 'printf "not a png" > "$PWD/render_0001.png"')
    result = asyncio.run(
        run_blender_spec(_render_spec(tmp_path), log_dir=tmp_path / "logs", blender_bin=fake)
    )
    assert result.ok is False
    assert result.validation_error is not None
    assert ".png" in result.validation_error


def test_render_with_valid_png_passes(tmp_path: Path) -> None:
    fake = _fake_blender(
        tmp_path,
        "printf '\\x89PNG\\r\\n\\x1a\\n' > \"$PWD/render_0001.png\"",
    )
    result = asyncio.run(
        run_blender_spec(_render_spec(tmp_path), log_dir=tmp_path / "logs", blender_bin=fake)
    )
    assert result.ok is True
    assert result.validation_error is None
    assert len(result.outputs) == 1


# --- export glTF: magic bytes obligatorios ----------------------------------


def test_export_claimed_glb_with_bad_magic_fails_typed(tmp_path: Path) -> None:
    fake = _fake_blender(
        tmp_path, 'printf "GLTF-fake-no-magic" > "$PWD/car.glb"'
    )
    result = asyncio.run(
        run_blender_spec(_export_spec(tmp_path), log_dir=tmp_path / "logs", blender_bin=fake)
    )
    assert result.ok is False
    assert result.validation_error is not None
    assert ".glb" in result.validation_error


def test_export_claimed_gltf_without_glb_file_fails_typed(tmp_path: Path) -> None:
    fake = _fake_blender(
        tmp_path, 'printf "usda" > "$PWD/car.usdc"; printf "fbx" > "$PWD/car.fbx"'
    )
    result = asyncio.run(
        run_blender_spec(_export_spec(tmp_path), log_dir=tmp_path / "logs", blender_bin=fake)
    )
    assert result.ok is False
    assert "no se produjo ningún .glb" in (result.validation_error or "")


def test_export_with_valid_glb_magic_passes(tmp_path: Path) -> None:
    fake = _fake_blender(
        tmp_path,
        'printf "glTF\\x02\\x00\\x00\\x00" > "$PWD/car.glb"; '
        'printf "usda" > "$PWD/car.usdc"; printf "fbx" > "$PWD/car.fbx"',
    )
    result = asyncio.run(
        run_blender_spec(_export_spec(tmp_path), log_dir=tmp_path / "logs", blender_bin=fake)
    )
    assert result.ok is True
    assert result.validation_error is None


# --- modo stub: la misma garantía -------------------------------------------


def test_stub_render_outputs_pass_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    result = asyncio.run(
        run_blender_spec(_render_spec(tmp_path), log_dir=tmp_path / "logs")
    )
    assert result.stubbed is True
    assert result.ok is True
    assert result.validation_error is None
    assert result.outputs[0].read_bytes()[:8] == _STUB_PNG[:8]


def test_stub_exr_render_uses_exr_magic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    spec = _render_spec(tmp_path)
    spec.output_format = "exr"
    result = asyncio.run(run_blender_spec(spec, log_dir=tmp_path / "logs"))
    assert result.ok is True
    assert result.outputs[0].suffix == ".exr"
    assert result.outputs[0].read_bytes()[:4] == _STUB_EXR[:4]


def test_stub_export_glb_has_gltf_magic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    result = asyncio.run(
        run_blender_spec(_export_spec(tmp_path), log_dir=tmp_path / "logs")
    )
    assert result.ok is True
    glb = next(p for p in result.outputs if p.suffix == ".glb")
    assert glb.read_bytes()[:4] == _STUB_GLB_HEADER[:4]


# --- adaptador del motor: rechazo tipado visible en EngineResult -------------


def test_engine_run_reports_invalid_output_kind(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, 'echo "fake wrote nothing"')
    eng = BlenderEngine(log_dir=tmp_path / "logs", blender_bin=fake)
    spec = _render_spec(tmp_path)
    res = asyncio.run(
        eng.run(EngineRunSpec(job={
            "mode": "render",
            "scene_path": str(spec.scene_path),
            "output_dir": str(spec.output_dir),
            "frames": 1,
        }))
    )
    assert res.ok is False
    assert res.exit_code == 0  # blender acabó bien; el artefacto no existe
    assert res.simulated is False
    assert res.payload["failure_kind"] == "blender_invalid_output"
    assert res.payload["validation_error"] is not None
    assert "[VALIDACION]" in (res.stderr_tail or "")


def test_engine_run_accepts_valid_outputs(tmp_path: Path) -> None:
    fake = _fake_blender(tmp_path, 'printf "\\x89PNG\\r\\n\\x1a\\n" > "$PWD/render_0001.png"')
    eng = BlenderEngine(log_dir=tmp_path / "logs", blender_bin=fake)
    spec = _render_spec(tmp_path)
    res = asyncio.run(
        eng.run(EngineRunSpec(job={
            "mode": "render",
            "scene_path": str(spec.scene_path),
            "output_dir": str(spec.output_dir),
            "frames": 1,
        }))
    )
    assert res.ok is True
    assert res.payload["failure_kind"] == "ok"
    assert res.payload["validation_error"] is None


# --- especificación: la validación no depende del entorno del test -----------


def test_validate_accepts_exr_and_rejects_empty(tmp_path: Path) -> None:
    from comfy_studio.blender import validate_blender_outputs

    out = tmp_path / "out"
    out.mkdir()
    exr = out / "render_0001.exr"
    exr.write_bytes(_STUB_EXR)
    spec = _render_spec(tmp_path, output_dir=str(out))
    assert validate_blender_outputs(spec, [exr]) is None
    os.remove(exr)
    assert validate_blender_outputs(spec, []) is not None
