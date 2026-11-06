"""Protocolo del worker ML: caídas tipadas, timeouts, stdout inválido (sin GPU)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfy_studio.ml.runner import (
    _build_env,
    run_worker,
    run_worker_with_probe_fallback,
)

WORKER = (
    Path(__file__).resolve().parent.parent / "comfy_studio" / "ml" / "worker.py"
)


def _fake_run(monkeypatch: pytest.MonkeyPatch, side_effect):
    calls: list[dict] = []

    def fake(*args, **kwargs):  # noqa: ANN001, ANN002
        calls.append({"args": args, "kwargs": kwargs})
        return side_effect(*args, **kwargs)

    monkeypatch.setattr("comfy_studio.ml.runner.subprocess.run", fake)
    return calls


def test_build_env_es_dict_explicito_sin_secretos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "STUDIO_LLM_KEY",
        "STUDIO_LLM_API_KEY",
        "COMFY_PASS",
        "STUDIO_API_TOKEN",
        "STUDIO_WEBHOOK_SECRET",
        "VAST_API_KEY",
    ):
        monkeypatch.setenv(key, "secreto-de-test-no-debe-salir")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _build_env()
    assert isinstance(env, dict)
    assert env.get("PATH") == "/usr/bin:/bin"
    for key in (
        "STUDIO_LLM_KEY",
        "STUDIO_LLM_API_KEY",
        "COMFY_PASS",
        "STUDIO_API_TOKEN",
        "STUDIO_WEBHOOK_SECRET",
        "VAST_API_KEY",
    ):
        assert key not in env
    assert "secreto-de-test-no-debe-salir" not in str(env.values())


def test_run_worker_timeout_devuelve_error_tipado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002
        raise subprocess.TimeoutExpired(
            cmd=args[0][0] if args else "worker", timeout=1, output=b"traza"
        )

    calls = _fake_run(monkeypatch, _boom)
    resp = run_worker("probe", {}, timeout_s=1.0)
    assert resp.ok is False
    assert resp.subcommand == "probe"
    assert "timeout" in resp.error
    assert resp.exit_code is None
    assert calls[0]["kwargs"]["timeout"] == 1.0
    assert calls[0]["kwargs"].get("shell") is not True


def test_run_worker_interprete_ausente_error_tipado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_ML_PYTHON_BIN", "/ruta/inexistente/python3-inexistente")

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002
        raise FileNotFoundError("Intérprete no encontrado")

    _fake_run(monkeypatch, _boom)
    resp = run_worker("probe", {})
    assert resp.ok is False
    assert "intérprete no encontrado" in resp.error
    assert resp.exit_code is None


def test_run_worker_rc_distinto_cero_tipificado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*args, **kwargs):  # noqa: ANN001, ANN002
        return SimpleNamespace(
            returncode=2,
            stdout=json.dumps({"ok": False, "subcommand": "probe", "error": "fallo interno"}),
            stderr="traza interna",
        )

    _fake_run(monkeypatch, _fake)
    resp = run_worker("probe", {})
    assert resp.ok is False
    assert resp.exit_code == 2
    assert resp.error == "fallo interno"
    assert resp.stderr_tail == "traza interna"


def test_run_worker_stdout_no_json_tipificado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*args, **kwargs):  # noqa: ANN001, ANN002
        return SimpleNamespace(returncode=0, stdout="no-es-json", stderr="")

    _fake_run(monkeypatch, _fake)
    resp = run_worker("probe", {})
    assert resp.ok is False
    assert "no es JSON válido" in resp.error
    assert resp.exit_code == 0


def test_run_worker_stdout_vacio_con_rc0_no_es_exito(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*args, **kwargs):  # noqa: ANN001, ANN002
        return SimpleNamespace(returncode=0, stdout="   ", stderr="")

    _fake_run(monkeypatch, _fake)
    resp = run_worker("probe", {})
    assert resp.ok is False
    assert "vacía" in resp.error
    assert resp.payload == {}


def test_run_worker_ok_con_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*args, **kwargs):  # noqa: ANN001, ANN002
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "subcommand": "probe",
                    "payload": {"probe_ok": True, "backends": {}},
                    "error": None,
                    "stderr_tail": None,
                }
            ),
            stderr="",
        )

    _fake_run(monkeypatch, _fake)
    resp = run_worker("probe", {})
    assert resp.ok is True
    assert resp.payload == {"probe_ok": True, "backends": {}}
    assert resp.exit_code == 0


def test_run_worker_with_probe_fallback_simulado_y_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*args, **kwargs):  # noqa: ANN001, ANN002
        return SimpleNamespace(
            returncode=2,
            stdout=json.dumps({"ok": False, "error": "sin pesos"}),
            stderr="sin pesos",
        )

    _fake_run(monkeypatch, _fake)
    sim = run_worker_with_probe_fallback("probe", {}, require_real=False)
    assert sim.ok is True
    assert sim.payload["simulated"] is True
    assert sim.payload["worker_error"] == "sin pesos"
    real = run_worker_with_probe_fallback("probe", {}, require_real=True)
    assert real.ok is False
    assert real.error == "sin pesos"


def _run_worker_direct(stdin_text: str, subcommand: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(WORKER), subcommand],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_worker_stdin_no_json_devuelve_rc2_tipado() -> None:
    rc, body = _run_worker_direct("no-json{{{", "probe")
    assert rc == 2
    assert body["ok"] is False
    assert body["subcommand"] == "probe"
    assert "no es JSON válido" in body["error"]


def test_worker_subcomando_desconocido_rc2_tipado() -> None:
    rc, body = _run_worker_direct("{}", "subcomando_inexistente")
    assert rc == 2
    assert body["ok"] is False
    assert "desconocido" in body["error"]
