"""Tests de la taxonomía de preparación de motores (política de arquitectura).

Regla que se verifica: «instalado» no es «en ejecución», y un chequeo de salud
nunca descarga pesos ni carga modelos.
"""

from __future__ import annotations

import json

import pytest

from comfy_studio.engines.health import (
    EngineNotReadyError,
    assert_production_prepared,
    health_report,
)
from comfy_studio.engines.preparedness import (
    EnginePrepSpec,
    Preparedness,
    build_default_prep_specs,
    execution_validated,
    note_engine_run,
    probe_all,
    probe_preparedness,
    reset_run_ledger,
)
from comfy_studio.engines.registry import build_default_registry


@pytest.fixture(autouse=True)
def _clean_ledger() -> None:
    reset_run_ledger()
    yield
    reset_run_ledger()


def _env(tmp_path, **extra: str) -> dict[str, str]:
    env = {
        "COMFY_APP_DIR": "/opt/ComfyUI",
        "COMFY_DATA_DIR": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    env.update(extra)
    return env


def test_ready_when_binary_and_integration_present(tmp_path) -> None:
    binary = tmp_path / "fake-engine"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    spec = EnginePrepSpec(
        name="fake",
        binaries=(str(binary),),
        config_env=("FAKE_TOKEN",),
    )
    prep = probe_preparedness(spec, env=_env(tmp_path, FAKE_TOKEN="x"))
    assert prep.state == "ready"
    assert prep.installed is True
    assert prep.integration is True
    assert prep.weights_present is None
    assert prep.model_loaded is False
    assert prep.execution_validated is False
    assert prep.hardware == "unchecked"


def test_missing_binary_is_not_installed(tmp_path) -> None:
    spec = EnginePrepSpec(name="fake", binaries=("definitely-not-a-real-binary-xyz",))
    prep = probe_preparedness(spec, env=_env(tmp_path))
    assert prep.state == "not_installed"
    assert prep.installed is False
    assert any(m.startswith("binario:") for m in prep.missing)


def test_missing_config_is_integration_missing(tmp_path) -> None:
    binary = tmp_path / "engine"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    spec = EnginePrepSpec(
        name="fake",
        binaries=(str(binary),),
        config_env=("FAKE_TOKEN",),
    )
    prep = probe_preparedness(spec, env=_env(tmp_path, FAKE_TOKEN="  "))
    assert prep.state == "integration_missing"
    assert prep.integration is False


def test_installed_but_weights_required(tmp_path) -> None:
    """Un motor correctamente instalado puede seguir pidiendo pesos de tarea."""
    binary = tmp_path / "engine"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    weights = tmp_path / "models" / "thing"
    spec = EnginePrepSpec(
        name="fake",
        binaries=(str(binary),),
        weight_paths=(str(weights),),
        weights_required=True,
    )
    missing = probe_preparedness(spec, env=_env(tmp_path))
    assert missing.state == "weights_required"
    assert missing.installed is True
    assert missing.weights_present is False

    weights.mkdir(parents=True)
    (weights / "model.bin").write_bytes(b"x")
    present = probe_preparedness(spec, env=_env(tmp_path))
    assert present.state == "ready"
    assert present.weights_present is True


def test_module_probe_uses_find_spec_without_importing() -> None:
    """`modules` se comprueba con un inyector, nunca importando el módulo."""
    calls: list[str] = []

    def finder(name: str) -> bool:
        calls.append(name)
        return name == "audiocraft"

    spec = EnginePrepSpec(name="fake", modules=("audiocraft", "fairseq"))
    prep = probe_preparedness(spec, env=_env(None), module_finder=finder)
    assert prep.state == "not_installed"
    assert calls == ["audiocraft", "fairseq"]
    assert "modulo:fairseq" in prep.missing


def test_execution_validated_only_after_a_real_run() -> None:
    spec = EnginePrepSpec(name="fake", binaries=("true",))
    assert execution_validated("fake") is False
    note_engine_run("fake", ok=False)
    assert execution_validated("fake") is False
    note_engine_run("fake", ok=True)
    prep = probe_preparedness(spec, env=_env(None))
    assert prep.execution_validated is True


def test_probe_never_touches_the_network_or_gpu() -> None:
    """La sonda completa corre sin red y sin GPU: solo stat/which/find_spec."""
    report = probe_all(env={"PATH": "/usr/bin:/bin", "COMFY_DATA_DIR": "/nonexistent"})
    assert len(report.engines) == 21
    assert sum(report.counts().values()) == 21
    assert set(report.counts()) == {
        "ready",
        "weights_required",
        "not_installed",
        "integration_missing",
    }


def test_default_specs_cover_the_whole_roster() -> None:
    specs = build_default_prep_specs()
    roster = {e["name"] for e in build_default_registry().describe()["engines"]}
    assert set(specs) == roster

def test_blender_declares_that_no_binary_is_baked() -> None:
    """El motor más revelador: el roster lo daba por listo sin binario."""
    specs = build_default_prep_specs()
    assert "STUDIO_BLENDER_BIN" in specs["blender"].binaries[0]
    prep = probe_preparedness(
        specs["blender"], env={"PATH": "/usr/bin:/bin", "STUDIO_BLENDER_BIN": ""}
    )
    assert prep.state == "not_installed"


def test_health_report_exposes_both_views() -> None:
    reg = build_default_registry()
    rep = health_report(reg)
    blob = json.dumps(rep)
    # La vista antigua (cableado) se conserva intacta. El reparto ready/stub
    # depende del entorno (CI activa STUDIO_STUB_*), así que no se fija un
    # literal: lo invariante es que cubre los 21 motores y ninguno queda
    # sin cablear.
    assert rep["counts"]["unwired"] == 0
    assert sum(rep["counts"].values()) == len(rep["engines"]) == 21
    assert rep["counts"]["ready"] + rep["counts"]["stub"] == 21
    # La nueva (preparación) llega además por motor y agregada.
    assert "preparedness" in rep
    assert len(rep["preparedness"]["engines"]) == 21
    assert all("prep" in e for e in rep["engines"])
    assert '"preparedness"' in blob


def test_assert_production_prepared_fails_closed(tmp_path) -> None:
    binary = tmp_path / "engine"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    specs = {
        "ok": EnginePrepSpec(name="ok", binaries=(str(binary),)),
        "broken": EnginePrepSpec(name="broken", binaries=("nope-nope-nope",)),
    }
    report = probe_all(specs, env=_env(tmp_path))
    assert report.engines["ok"].state == "ready"

    # Un motor sin especificación falla; uno no preparado también.
    with pytest.raises(EngineNotReadyError, match="no tiene especificación"):
        assert_production_prepared(build_default_registry(), required=["ok"])
    with pytest.raises(EngineNotReadyError, match="broken"):
        assert_production_prepared(
            build_default_registry(), required=["broken"], preps=report.engines
        )


def test_preparedness_is_serializable() -> None:
    prep = Preparedness(
        installed=True,
        integration=True,
        weights_required=False,
        weights_present=None,
        hardware="unchecked",
        model_loaded=False,
        execution_validated=False,
        missing=(),
        state="ready",
    )
    assert json.loads(json.dumps(prep.to_public_dict()))["state"] == "ready"
