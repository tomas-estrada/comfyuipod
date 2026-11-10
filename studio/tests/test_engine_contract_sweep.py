"""PR engines-ffmpeg-contract: el roster entero cumple el contrato de 4 verbos."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from comfy_studio.engines import EngineResult, EngineRunSpec
from comfy_studio.engines.ffmpeg import (
    FfmpegEngine,
    TranscodeSpec,
    parse_version_line,
)
from comfy_studio.engines.registry import (
    ENGINE_CLASSES,
    build_default_registry,
    resolve_engine_class,
)


def test_roster_y_clases_en_sincronia() -> None:
    roster = set(build_default_registry().names())
    assert roster == set(ENGINE_CLASSES), (
        f"deriva roster<->clases: {roster ^ set(ENGINE_CLASSES)}"
    )
    assert len(roster) == 21


def test_cada_motor_resuelve_y_declara_nombre_y_tier() -> None:
    reg = build_default_registry()
    for name in reg.names():
        cls = resolve_engine_class(name)
        assert cls.name == name, f"{cls} declara name={cls.name!r}, roster={name!r}"
        assert cls.tier == reg.get(name).tier
        assert not inspect.isabstract(cls), f"{name} deja abstractos sin implementar"


def test_los_cuatro_verbos_con_firma_compatible() -> None:
    for name in build_default_registry().names():
        cls = resolve_engine_class(name)
        for verb in ("list", "install", "verify", "run"):
            assert callable(getattr(cls, verb, None)), f"{name}.{verb}"
    sig_list = inspect.signature(resolve_engine_class("ffmpeg").list)
    assert len(sig_list.parameters) == 1  # self
    sig_install = inspect.signature(resolve_engine_class("ffmpeg").install)
    params = sig_install.parameters
    assert list(params) == ["self", "submotor", "ref"]
    assert params["ref"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["ref"].default is None
    sig_verify = inspect.signature(resolve_engine_class("comfyui").verify)
    assert list(sig_verify.parameters) == ["self", "submotor"]
    sig_run = inspect.signature(resolve_engine_class("qa").run)
    assert list(sig_run.parameters) == ["self", "spec"]


def test_barrido_de_contrato_sin_violaciones() -> None:
    """El roster entero pasa el contrato de 4 verbos (firmas y retornos)."""
    violations = build_default_registry().contract_violations()
    assert violations == {}, f"motores que rompen el contrato: {violations}"


def test_el_barrido_detecta_motor_roto() -> None:
    from comfy_studio.engines import EngineTier
    from comfy_studio.engines.base import engine_contract_violations

    class _Roto:
        name = "rota"
        tier = EngineTier.WARM

        def list(self):  # noqa: ANN201
            return None

        def install(self, submotor, ref=None):  # noqa: ANN201, ANN001
            return None

        def verify(self, submotor):  # noqa: ANN201
            return None

        def run(self, spec):  # noqa: ANN201
            return None

    problems = engine_contract_violations(_Roto, name="rota", tier=EngineTier.WARM)
    assert any("install" in p and "firma" in p for p in problems)
    assert any("retorno no es EngineResult" in p for p in problems)


def test_resultado_tipiado_siempre_serializable() -> None:
    res = EngineResult(ok=False, engine="x", verb="run", error="falla honesta")
    assert json.loads(json.dumps(res.to_public_dict()))["error"] == "falla honesta"
    assert set(res.to_public_dict()) == {
        "ok", "engine", "verb", "payload", "error",
        "exit_code", "stderr_tail", "simulated", "reason",
    }


def test_sin_clase_registrada_falla_tipado() -> None:
    with pytest.raises(KeyError, match="sin clase registrada"):
        resolve_engine_class("inexistente")


# ---------------------------------------------------------------------------
# FfmpegEngine: comportamiento real sin GPU ni red
# ---------------------------------------------------------------------------


def test_parse_version_line() -> None:
    assert parse_version_line("ffmpeg version 6.1.2 Copyright (c)") == "6.1.2"
    assert parse_version_line("ffmpeg version n9.0.2-full") == "n9.0.2-full"
    assert parse_version_line("avconv version 3.4") is None
    assert parse_version_line("") is None


def test_list_informa_sin_exigir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_FFMPEG", raising=False)
    eng = FfmpegEngine()
    res = eng.list()
    assert res.ok is True and res.verb == "list"
    assert set(res.payload) == {"available", "binary", "version", "stub"}
    assert res.payload["stub"] is False


def test_verify_sin_binario_es_tipado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_FFMPEG", raising=False)
    monkeypatch.delenv("STUDIO_FFMPEG_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    eng = FfmpegEngine()
    res = eng.verify("")
    assert res.ok is False
    assert "FFmpeg" in (res.error or "")
    res = eng.verify("algun-submotor")
    assert res.ok is False and "submotores" in (res.error or "")


def test_install_siempre_rechazado_tipado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")  # ni con dinámico: nada que instalar
    eng = FfmpegEngine()
    res = eng.install("x264")
    assert res.ok is False and res.verb == "install"


def test_run_rechaza_spec_invalido_sin_binario(tmp_path: Path) -> None:
    eng = FfmpegEngine()
    res = eng.run(EngineRunSpec(job={"inputs": [], "output": str(tmp_path / "o.mp4")}))
    assert res.ok is False and "inputs" in (res.error or "")
    res = eng.run(EngineRunSpec(job={"inputs": [str(tmp_path / "nope.mp4")],
                                     "output": str(tmp_path / "o.mp4")}))
    assert res.ok is False and "inexistente" in (res.error or "")


def test_run_stub_no_toca_disco(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_FFMPEG", "1")
    eng = FfmpegEngine()
    assert eng.is_stub() is True
    out = tmp_path / "out.mp4"
    res = eng.run(EngineRunSpec(job={"inputs": [str(tmp_path / "a.mp4")],
                                     "output": str(out)}))
    assert res.ok is False  # valida primero: la entrada no existe ni en stub
    src = tmp_path / "a.mp4"
    src.write_bytes(b"\x00" * 64)
    res = eng.run(EngineRunSpec(job={"inputs": [str(src)], "output": str(out)}))
    assert res.ok is True and res.simulated is True
    assert not out.exists(), "el stub no debe escribir salidas"


def test_run_real_transcodifica_si_hay_binario(tmp_path: Path) -> None:
    import subprocess

    from comfy_studio.media_bins import find_ffmpeg

    binary = find_ffmpeg()
    if not binary:
        pytest.skip("sin binario ffmpeg en este entorno")
    src = tmp_path / "in.mp4"
    subprocess.run([binary, "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=64x48:rate=5",
                    str(src)], check=True, capture_output=True, timeout=120)
    out = tmp_path / "out.mp4"
    res = FfmpegEngine().run(EngineRunSpec(job={
        "inputs": [str(src)], "output": str(out),
        "args": ["-c:v", "mpeg4"], "timeout_s": 120.0,
    }))
    assert res.ok is True, res.error
    assert out.is_file() and out.stat().st_size > 0


def test_transcode_spec_validate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inputs"):
        TranscodeSpec(inputs=[], output=tmp_path / "o.mp4").validate()
    with pytest.raises(ValueError, match="inexistente"):
        TranscodeSpec(inputs=[tmp_path / "no.mp4"],
                      output=tmp_path / "o.mp4").validate()
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    TranscodeSpec(inputs=[src], output=tmp_path / "o.mp4", args=["-y"]).validate()
    with pytest.raises(ValueError, match="cadenas"):
        TranscodeSpec(inputs=[src], output=tmp_path / "o.mp4",
                      args=["-y", 42]).validate()  # type: ignore[list-item]
