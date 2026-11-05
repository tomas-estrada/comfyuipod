"""Tests del motor de captura 3D / Nerfstudio (M5-SCR, fase 9).

Verifica los contratos REALES de nerfstudio 1.1.5 con binarios falsos en
STUDIO_CAPTURE3D_BIN_DIR: ns-process-data, ns-train <método>, ns-render,
ns-export <método>; los "formatos" glb/ply como métodos quedan rechazados, y un
run rc=0 sin artefactos reales falla tipado.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.capture3d import Capture3dEngine

_FAKE_NS = """#!/bin/bash
set -euo pipefail
echo "$0 $@" >> "{log}"
tool="$(basename "$0")"
# último argumento que empieza con / y es dir de salida/entrada según op
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--output-dir" ]; then out="$a"; fi
  prev="$a"
done
case "$tool" in
  ns-process-data)
    mkdir -p "$out/images" "$out/colmap"
    ;;
  ns-train)
    mkdir -p "$out/run/splatfacto/2026-01-01_000000"
    echo "pipeline: {}" > "$out/run/splatfacto/2026-01-01_000000/config.yml"
    ;;
  ns-render)
    mkdir -p "$out/rgb"
    printf 'fake' > "$out/rgb/0001.png"
    ;;
  ns-export)
    mkdir -p "$out"
    printf 'ply\\nformat ascii 1.0\\nelement vertex 1\\nend_header\\n0 0 0\\n' > "$out/splat.ply"
    ;;
esac
"""

_FAKE_NS_EMPTY = """#!/bin/bash
# rc=0 pero SIN artefactos: debe fallar tipado
exit 0
"""


def _make_bins(tmp_path: Path, *, empty: bool = False) -> Path:
    bin_dir = tmp_path / "nsbin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "ns_calls.log"
    script = _FAKE_NS_EMPTY if empty else _FAKE_NS.replace("{log}", str(log))
    for tool in ("ns-process-data", "ns-train", "ns-render", "ns-export"):
        p = bin_dir / tool
        p.write_text(script, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


@pytest.fixture()
def fake_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("STUDIO_STUB_CAPTURE3D", raising=False)
    bin_dir = _make_bins(tmp_path)
    monkeypatch.setenv("STUDIO_CAPTURE3D_BIN_DIR", str(bin_dir))
    return bin_dir


def test_capture3d_is_installable_tier() -> None:
    assert Capture3dEngine().tier == EngineTier.INSTALLABLE


def test_list_reports_ops_and_methods() -> None:
    res = Capture3dEngine().list()
    assert res.ok is True
    assert set(res.payload["ops"]) == {"process", "train", "render", "export"}
    assert set(res.payload["export_methods"]) == {"gaussian-splat", "point-cloud"}


def test_verify_reports_missing_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_CAPTURE3D", raising=False)
    monkeypatch.setenv("STUDIO_CAPTURE3D_BIN_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("PATH", "")
    res = Capture3dEngine().verify("splatfacto")
    assert res.ok is False
    assert res.payload["missing_bins"]


def test_stub_mode_writes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_CAPTURE3D", "1")
    src = tmp_path / "photos"
    src.mkdir()
    job = {"op": "process", "input_path": str(src), "output_path": str(tmp_path / "proc")}
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is True and res.simulated is True
    assert (tmp_path / "proc" / "stub.ply").exists()


def test_process_train_render_export_real_commands(
    tmp_path: Path, fake_bins: Path
) -> None:
    eng = Capture3dEngine()
    assert eng.verify("splatfacto").ok is True

    photos = tmp_path / "photos"
    photos.mkdir()
    proc = tmp_path / "proc"
    res = eng.run(EngineRunSpec(job={
        "op": "process", "input_path": str(photos), "output_path": str(proc),
    }))
    assert res.ok is True, res.error
    assert (proc / "images").is_dir() and (proc / "colmap").is_dir()

    model_out = tmp_path / "train_out"
    res = eng.run(EngineRunSpec(job={
        "op": "train", "input_path": str(proc), "output_path": str(model_out),
    }))
    assert res.ok is True, res.error
    cfg = next(model_out.rglob("config.yml"))
    assert cfg.is_file()

    render_out = tmp_path / "render_out"
    res = eng.run(EngineRunSpec(job={
        "op": "render", "model_path": str(cfg), "output_path": str(render_out),
    }))
    assert res.ok is True, res.error
    assert any(render_out.rglob("*.png"))

    export_out = tmp_path / "export_out"
    res = eng.run(EngineRunSpec(job={
        "op": "export", "model_path": str(cfg),
        "export_method": "gaussian-splat", "output_path": str(export_out),
    }))
    assert res.ok is True, res.error
    ply = next(export_out.rglob("*.ply"))
    assert ply.read_bytes().startswith(b"ply")

    # los comandos reales: método como subcomando, no como flag/extensión
    log = (tmp_path / "ns_calls.log").read_text(encoding="utf-8")
    assert "ns-train splatfacto --data" in log
    assert "ns-export gaussian-splat --load-config" in log
    assert "ns-process-data images --data" in log
    assert "ns-render dataset --load-config" in log


def test_export_rejects_glb_ply_as_method(tmp_path: Path, fake_bins: Path) -> None:
    eng = Capture3dEngine()
    cfg = tmp_path / "config.yml"
    cfg.write_text("x", encoding="utf-8")
    for bad in ("glb", "ply"):
        res = eng.run(EngineRunSpec(job={
            "op": "export", "model_path": str(cfg),
            "export_method": bad, "output_path": str(tmp_path / "o"),
        }))
        assert res.ok is False
        assert "ns-export" in (res.error or "")


def test_rc0_sin_artefactos_falla_tipado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_CAPTURE3D", raising=False)
    bin_dir = _make_bins(tmp_path, empty=True)
    monkeypatch.setenv("STUDIO_CAPTURE3D_BIN_DIR", str(bin_dir))
    photos = tmp_path / "photos"
    photos.mkdir()
    res = Capture3dEngine().run(EngineRunSpec(job={
        "op": "process", "input_path": str(photos), "output_path": str(tmp_path / "proc"),
    }))
    assert res.ok is False
    assert "artefactos" in (res.error or "")


def test_train_missing_input_rejected(tmp_path: Path, fake_bins: Path) -> None:
    job = {"op": "train", "output_path": str(tmp_path / "m")}
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "input_path" in (res.error or "")


def test_no_binary_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_CAPTURE3D", raising=False)
    monkeypatch.setenv("STUDIO_CAPTURE3D_BIN_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("PATH", "")
    src = tmp_path / "photos"
    src.mkdir()
    job = {"op": "train", "input_path": str(src), "output_path": str(tmp_path / "model")}
    res = Capture3dEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "Nerfstudio no disponible" in (res.error or "")
