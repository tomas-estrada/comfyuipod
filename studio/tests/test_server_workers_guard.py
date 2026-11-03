"""M7-OPS-004: el studio-server es de un solo worker (ProgressBroker in-memory).

`STUDIO_SERVER_WORKERS != 1` debe abortar con error claro en vez de arrancar en
un modo donde los eventos SSE no llegan a todos los clientes.
"""

from __future__ import annotations

import pytest

from comfy_studio import server


def test_resolve_workers_defaults_to_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_SERVER_WORKERS", raising=False)
    assert server._resolve_workers() == 1
    monkeypatch.setenv("STUDIO_SERVER_WORKERS", "1")
    assert server._resolve_workers() == 1
    monkeypatch.setenv("STUDIO_SERVER_WORKERS", "")
    assert server._resolve_workers() == 1


def test_resolve_workers_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_SERVER_WORKERS", "abc")
    with pytest.raises(SystemExit) as exc:
        server._resolve_workers()
    assert exc.value.code == 2
    monkeypatch.setenv("STUDIO_SERVER_WORKERS", "0")
    with pytest.raises(SystemExit):
        server._resolve_workers()
    monkeypatch.setenv("STUDIO_SERVER_WORKERS", "-2")
    with pytest.raises(SystemExit):
        server._resolve_workers()


def test_main_refuses_multiple_workers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("STUDIO_SERVER_WORKERS", "4")
    with pytest.raises(SystemExit) as exc:
        server.main([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "STUDIO_SERVER_WORKERS=4" in err
    assert "ProgressBroker" in err
