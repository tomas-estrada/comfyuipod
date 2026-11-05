"""Tests del pinner de manifiestos (`scripts/pin_manifest_integrity.py`).

Viven aquí, y no dentro del script, porque la puerta documentada en AGENTS.md es
`ruff check studio panel scripts config/probes` + `pytest studio/tests -q`: ocho
pruebas al final del módulo de producción solo se ejecutaban si alguien
recordaba el conjuro `pytest scripts/pin_manifest_integrity.py`, que ni CI ni el
Makefile lanzan.

Ninguna abre un socket: `_OPENER` y `head_metadata` se sustituyen por dobles.
Cubren lo que decide el manifiesto final (qué se escribe y qué se rechaza).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pin_manifest_integrity as pinner  # noqa: E402

_REV = "0" * 40
_SHA256 = "a" * 64
_SHA1 = "b" * 40
_URL = "https://huggingface.co/Owner/Repo/resolve/main/model.safetensors"
_RENAMED_URL = "https://huggingface.co/Owner/Renamed/resolve/main/model.safetensors"


class _Headers:
    """Cabeceras suficientes para `head_metadata`: `get` sin distinguir caso."""

    def __init__(self, **values: str) -> None:
        self._values = {key.lower(): value for key, value in values.items()}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name.lower(), default)


class _Response:
    """Respuesta `HEAD` utilizable como context manager."""

    def __init__(self, **headers: str) -> None:
        self.headers = _Headers(**headers)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _redirect(location: str, **headers: str) -> urllib.error.HTTPError:
    """302 tal como lo emite huggingface.co (el opener no lo sigue)."""
    return urllib.error.HTTPError(
        _URL, 302, "Found", _Headers(Location=location, **headers), None
    )


class _FakeOpener:
    """Sustituto de `_OPENER`: respuestas guionizadas, sin red.

    Agotados los pasos se repite el último: así un 200 sin ETag se devuelve en
    cada reintento sin tener que encadenarlos a mano.
    """

    def __init__(self, *steps: object) -> None:
        self._steps = list(steps)
        self._index = 0
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout=None):  # noqa: ANN001, ANN201
        self.requests.append(request)
        step = self._steps[min(self._index, len(self._steps) - 1)]
        self._index += 1
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]


def _use(monkeypatch, opener: _FakeOpener) -> None:
    monkeypatch.setattr(pinner, "_OPENER", opener)


def _use_head(
    monkeypatch, result: tuple[str | None, str | None, str | None]
) -> None:
    monkeypatch.setattr(pinner, "head_metadata", lambda url, attempts=3: result)


def _manifest(tmp_path: Path) -> Path:
    """Manifiesto mínimo válido, escrito en un directorio temporal."""
    path = tmp_path / "models-test.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "profile": "studio",
                "models": [
                    {
                        "url": _URL,
                        "filename": "model.safetensors",
                        "repo_id": "Owner/Repo",
                        "dest": "models/model.safetensors",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_head_metadata_200(monkeypatch) -> None:
    """Un 200 con commit y ETag fija revisión y dígest (ETag entre comillas)."""
    opener = _FakeOpener(
        _Response(**{"X-Repo-Commit": _REV, "X-Linked-Etag": f'"{_SHA256.upper()}"'})
    )
    _use(monkeypatch, opener)
    assert pinner.head_metadata(_URL) == (_REV, _SHA256, _URL)
    assert [request.method for request in opener.requests] == ["HEAD"]


def test_head_metadata_302(monkeypatch) -> None:
    """El 302 al CDN de xet no se sigue: es él quien publica la integridad."""
    opener = _FakeOpener(
        _redirect(
            "https://cdn-lfs.example/xet/abc",
            **{"X-Repo-Commit": _REV, "X-Linked-Etag": _SHA256},
        )
    )
    _use(monkeypatch, opener)
    assert pinner.head_metadata(_URL) == (_REV, _SHA256, _URL)
    assert len(opener.requests) == 1


def test_head_metadata_same_host_rename_hop(monkeypatch) -> None:
    """Un 302 con ruta relativa es un renombrado de repo: se sigue el salto."""
    opener = _FakeOpener(
        _redirect("/Owner/Renamed/resolve/main/model.safetensors"),
        _Response(**{"X-Repo-Commit": _REV, "X-Linked-Etag": _SHA256}),
    )
    _use(monkeypatch, opener)
    assert pinner.head_metadata(_URL) == (_REV, _SHA256, _RENAMED_URL)
    assert opener.urls == [_URL, _RENAMED_URL]


def test_head_metadata_missing_etag(monkeypatch) -> None:
    """Sin `X-Linked-Etag` no hay integridad que fijar: nada se devuelve."""
    opener = _FakeOpener(_Response(**{"X-Repo-Commit": _REV}))
    _use(monkeypatch, opener)
    assert pinner.head_metadata(_URL, attempts=1) == (None, None, None)


def test_head_metadata_hops_are_bounded(monkeypatch) -> None:
    """Renombrados encadenados: se para en `_MAX_HOPS`, no en bucle infinito."""
    opener = _FakeOpener(
        *(_redirect(f"/hop{index}") for index in range(pinner._MAX_HOPS + 2))
    )
    _use(monkeypatch, opener)
    assert pinner.head_metadata(_URL, attempts=1) == (None, None, None)
    assert len(opener.requests) == pinner._MAX_HOPS


def test_algorithm_for_digest() -> None:
    """El algoritmo sale de la forma del dígest, no solo de su longitud."""
    assert pinner._algorithm_for(_SHA256) == "sha256"
    assert pinner._algorithm_for(_SHA1) == "sha1"
    assert pinner._algorithm_for("z" * 64) is None
    assert pinner._algorithm_for("g" * 40) is None
    assert pinner._algorithm_for(_SHA256[:-1]) is None
    assert pinner._algorithm_for("") is None
    assert pinner._algorithm_for(None) is None


def test_pin_rejects_non_hex_digest(tmp_path, monkeypatch, capsys) -> None:
    """Un ETag de 64 caracteres no hexadecimal no se escribe como `sha256`."""
    path = _manifest(tmp_path)
    before = path.read_text(encoding="utf-8")
    _use_head(monkeypatch, (_REV, "z" * 64, _URL))

    assert pinner.pin(path) == 1
    assert path.read_text(encoding="utf-8") == before
    assert "no reconocido" in capsys.readouterr().err


def test_pin_records_sha256(tmp_path, monkeypatch) -> None:
    """El caso bueno sigue fijando revisión, hash y URL canónica."""
    path = _manifest(tmp_path)
    _use_head(monkeypatch, (_REV, _SHA256, _URL))

    assert pinner.pin(path) == 0
    entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert entry["revision"] == _REV
    assert entry["sha256"] == _SHA256
    assert "sha1" not in entry
    assert entry["url"] == _URL.replace("/resolve/main/", f"/resolve/{_REV}/")
