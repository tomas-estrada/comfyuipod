"""PR engines-health-states: los 7 niveles, probados en los 21 motores.

Pines deterministas sobre un árbol sintético en tmp (sin GPU, sin red):
un árbol completo deja a los 21 en `ready` (prueba que cada spec es
satisfacible y documenta qué necesita cada motor); sin pesos el estado es
`weights_required` y nunca `loaded`/validado; cada hueco reporta su prefijo.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from comfy_studio.engines.preparedness import (
    _expand,
    build_default_prep_specs,
    execution_validated,
    note_engine_run,
    probe_all,
    probe_preparedness,
    reset_run_ledger,
)


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_run_ledger()
    yield
    reset_run_ledger()


def _exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    try:
        path.chmod(0o755)
    except OSError:  # pragma: no cover - Windows
        pass
    # En Windows `shutil.which("nombre")` exige extensión PATHEXT: gemelo .cmd.
    if not path.suffix:
        twin = path.with_suffix(".cmd")
        if not twin.exists():
            twin.write_text("@echo off\n")
    return str(path)


def _full_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Árbol sintético completo: todo lo que los 20 specs piden existe."""
    bin_dir = tmp_path / "bin"
    data = tmp_path / "data"
    app = tmp_path / "ComfyUI"
    app.mkdir(parents=True, exist_ok=True)
    (app / "main.py").write_text("# fake\n")
    env = {
        "COMFY_APP_DIR": str(app),
        "COMFY_DATA_DIR": str(data),
        "STUDIO_REMOTION_PROJECT": str(tmp_path / "remotion"),
        "STUDIO_FFMPEG_BIN": _exe(bin_dir / "ffmpeg"),
        "STUDIO_BLENDER_BIN": _exe(bin_dir / "blender"),
        "STUDIO_VOICE_CLONE_BIN": _exe(bin_dir / "rvc"),
        "STUDIO_VOICE_CLONE_MODEL_DIR": str(data / "models" / "rvc"),
        "STUDIO_INDEXTTS_BIN": _exe(bin_dir / "indextts"),
        "STUDIO_INDEXTTS_MODEL_DIR": str(data / "models" / "indextts"),
        "STUDIO_MUSIC_GEN_BIN": _exe(bin_dir / "musicgen"),
        "STUDIO_LIPSYNC_BIN": _exe(bin_dir / "lipsync"),
        "STUDIO_LIPSYNC_MODEL_DIR": str(data / "models" / "lipsync"),
        "STUDIO_UPSCALER_BIN": _exe(bin_dir / "upscaler"),
        "STUDIO_UPSCALER_MODEL_DIR": str(data / "models" / "upscaler"),
        "STUDIO_COMPOSITOR_BIN": _exe(bin_dir / "natron"),
        "STUDIO_LLM_SERVER_BIN": _exe(bin_dir / "llama-server"),
        "STUDIO_LLM_SERVER_MODEL": str(data / "models" / "llm" / "model.gguf"),
        "STUDIO_ROTO_MODEL_DIR": str(data / "models" / "sam3"),
        "STUDIO_HEAVY_VIDEO_BASE_URL": "http://127.0.0.1:9",
        "STUDIO_HEAVY_VIDEO_MODEL": "fake-model",
        "STUDIO_HEAVY_VIDEO_KEY": "x" * 8,
        "STUDIO_ATLAS_ROOT": str(tmp_path / "atlas"),
    }
    for name in ("blender", "node", "npx", "ns-train", "ffmpeg"):
        _exe(bin_dir / name)
    Path(env["STUDIO_REMOTION_PROJECT"]).mkdir(parents=True, exist_ok=True)
    Path(env["STUDIO_ATLAS_ROOT"]).mkdir(parents=True, exist_ok=True)
    (data / "venvs" / "indextts").mkdir(parents=True, exist_ok=True)
    for wrel in ("models/whisper/faster-whisper-large-v3",
                 "models/rvc", "models/indextts", "models/musicgen",
                 "models/lipsync", "models/upscaler", "models/sam3"):
        wdir = data / wrel
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "weights.bin").write_bytes(b"fake-weights")
    (Path(env["STUDIO_LIPSYNC_MODEL_DIR"]) / "wav2lip_gan.pth").write_bytes(b"fake")
    llm_model = Path(env["STUDIO_LLM_SERVER_MODEL"])
    llm_model.parent.mkdir(parents=True, exist_ok=True)
    llm_model.write_bytes(b"fake-gguf")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return env


def test_arbol_completo_deja_todos_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _full_env(tmp_path, monkeypatch)
    report = probe_all(env=env, module_finder=lambda m: True)
    assert len(report.engines) == 21
    not_ready = {n: p.state for n, p in report.engines.items() if p.state != "ready"}
    assert not_ready == {}, f"motores no listos con árbol completo: {not_ready}"
    assert report.counts() == {"ready": 21, "weights_required": 0,
                               "not_installed": 0, "integration_missing": 0}


def test_sin_pesos_weights_required_nunca_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pesos ausentes => `weights_required`; jamás `loaded`/validado sin GPU."""
    env = _full_env(tmp_path, monkeypatch)
    specs = build_default_prep_specs()
    with_weights = [s for s in specs.values() if s.weights_required]
    assert len(with_weights) >= 5
    for spec in with_weights:
        pruned = dict(env)
        for w in spec.weight_paths:
            target = Path(_expand(w, pruned))
            if target.is_file() or target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        prep = probe_preparedness(spec, env=pruned, module_finder=lambda m: True)
        assert prep.state == "weights_required", spec.name
        assert prep.installed is True
        assert prep.weights_present is False
        assert prep.model_loaded is False
        assert prep.execution_validated is False
        assert prep.hardware == "unchecked"


def test_cada_hueco_reporta_su_prefijo(tmp_path: Path) -> None:
    from comfy_studio.engines.preparedness import EnginePrepSpec

    binary = tmp_path / "bin" / "eng"
    _exe(binary)
    base = {"name": "fake", "binaries": (str(binary),)}
    cases = [
        ({"config_env": ("FALTA_CFG_X",)}, "config:FALTA_CFG_X",
         {"COMFY_DATA_DIR": str(tmp_path)}),
        ({"weight_paths": (str(tmp_path / "pesos" / "m.bin"),),
          "weights_required": True}, "pesos:",
         {"COMFY_DATA_DIR": str(tmp_path)}),
        ({"venvs": (str(tmp_path / "venv_x"),)}, "venv:",
         {"COMFY_DATA_DIR": str(tmp_path)}),
        ({"files": (str(tmp_path / "falta.txt"),)}, "fichero:",
         {"COMFY_DATA_DIR": str(tmp_path)}),
        ({"modules": ("modulo_inexistente_xyz",)}, "modulo:modulo_inexistente_xyz",
         {"COMFY_DATA_DIR": str(tmp_path)}),
    ]
    for kwargs, prefix, env in cases:
        prep = probe_preparedness(EnginePrepSpec(**{**base, **kwargs}), env=env)
        assert any(m.startswith(prefix) for m in prep.missing), (kwargs, prep.missing)


def test_validado_solo_para_quien_ejecuto() -> None:
    note_engine_run("audio", ok=False)
    assert execution_validated("audio") is False
    note_engine_run("audio", ok=True)
    assert execution_validated("audio") is True
    assert execution_validated("qa") is False, "el ledger no contamina motores"
