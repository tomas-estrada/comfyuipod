"""Sonda de backend ML: ausente degrada explícito, presente habilita (sin GPU)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from comfy_studio.ml import probe as probe_mod
from comfy_studio.ml.probe import probe_capabilities

ALL_BACKENDS = ("clip", "insightface", "asr", "diarize")


def test_interprete_ausente_shape_completo_con_backends_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUDIO_ML_PYTHON_BIN", raising=False)
    caps = probe_capabilities(
        interpreter="/ruta/inexistente/python3-ml-probe", data_dir="/tmp/inexistente"
    )
    assert caps.interpreter_ok is False
    assert caps.probe_ok is False
    assert caps.available_backends == []
    for backend in ALL_BACKENDS:
        assert caps.backends[backend] is False
        assert caps.imports_ok[backend] is False
        assert caps.weights_ok[backend] is False
    assert "intérprete no encontrado" in caps.reason
    public = caps.to_public_dict()
    assert set(public["backends"]) == set(ALL_BACKENDS)


def test_comfy_data_dir_vacio_es_no_definido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMFY_DATA_DIR", "")
    caps = probe_capabilities(data_dir="")
    assert caps.interpreter_ok is True
    assert caps.probe_ok is False
    for backend in ALL_BACKENDS:
        assert caps.backends[backend] is False
        assert caps.weights_ok[backend] is False
    assert "COMFY_DATA_DIR no definido" in caps.reason


def test_pesos_ausentes_degrada_con_limitacion_explicita(
    tmp_path: Path,
) -> None:
    caps = probe_capabilities(data_dir=tmp_path)
    assert caps.interpreter_ok is True
    assert caps.probe_ok is False
    assert caps.available_backends == []
    assert "pesos ausentes" in caps.reason
    assert "weights" in caps.reason


def _hacer_arbol_pesos(root: Path) -> None:
    for rel in probe_mod.ML_WEIGHTS.values():
        for item in rel:
            # clip/asr son dirs, insightface/diarize son ficheros .onnx/.bin
            target = root / item
            if item.endswith((".onnx", ".bin")):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"0")
            else:
                target.mkdir(parents=True, exist_ok=True)


def test_backend_presente_habilita_con_imports_y_pesos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hacer_arbol_pesos(tmp_path)

    def _ok(*args, **kwargs):  # noqa: ANN001, ANN002
        stdout = "1" if "cuda" in kwargs.get("input", "") else ""
        # _check_imports pasa el script como lista [interp, "-c", code]
        code = args[0][2] if len(args[0]) > 2 else kwargs.get("input", "")
        if "torch.cuda.is_available" in str(code):
            stdout = "1"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(probe_mod.subprocess, "run", _ok)
    caps = probe_capabilities(data_dir=tmp_path)
    assert caps.interpreter_ok is True
    assert caps.cuda_available is True
    for backend in ALL_BACKENDS:
        assert caps.imports_ok[backend] is True
        assert caps.weights_ok[backend] is True
        assert caps.backends[backend] is True
    assert caps.probe_ok is True
    assert sorted(caps.available_backends) == sorted(ALL_BACKENDS)


def test_import_fallido_degrada_aun_con_pesos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hacer_arbol_pesos(tmp_path)

    def _fail_import(*args, **kwargs):  # noqa: ANN001, ANN002
        code = args[0][2] if len(args[0]) > 2 else ""
        if "torch.cuda.is_available" in str(code):
            return SimpleNamespace(returncode=0, stdout="0", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="ModuleNotFoundError: x")

    monkeypatch.setattr(probe_mod.subprocess, "run", _fail_import)
    caps = probe_capabilities(data_dir=tmp_path)
    assert caps.probe_ok is False
    for backend in ALL_BACKENDS:
        assert caps.weights_ok[backend] is True
        assert caps.backends[backend] is False
    assert "imports" in caps.reason


def test_limitacion_gpu_tipificada_no_es_exito(
    tmp_path: Path,
) -> None:
    """Sin GPU/pesos reales el probe nunca finge éxito."""
    caps = probe_capabilities(data_dir=tmp_path)
    assert caps.probe_ok is False
    assert caps.cuda_available is False
    assert caps.reason
