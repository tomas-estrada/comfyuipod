"""Descubrimiento y descarga del extracto fechado de Geofabrik (sin red).

Cubre `discover_dated_extract` (regex + max(dates) sobre el HTML del índice)
y `fetch_extract` (descarga con chequeo md5) con un stub de HttpClient que
tiene lógica real: payload dictado por URL y escritura real del fichero.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from comfy_studio.atlas.acquire.geofabrik import (
    GEOFABRIK_INDEX,
    discover_dated_extract,
    fetch_extract,
)


class StubHttp:
    """Doble mínimo de HttpClient: request devuelve bytes dictados por URL y
    download escribe el payload correspondiente en disco (con la misma
    firma keyword que usa fetch_extract)."""

    def __init__(
        self,
        responses: dict[str, bytes],
        downloads: dict[str, bytes] | None = None,
    ) -> None:
        self.responses = responses
        self.downloads = downloads if downloads is not None else {}
        self.requested: list[tuple[str, str]] = []
        self.downloaded: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> bytes:
        self.requested.append((method, url))
        if url not in self.responses:
            raise AssertionError(f"URL no dictada en el stub: {url}")
        return self.responses[url]

    def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int | None = None,
        resume: bool = True,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.downloaded.append(
            {"url": url, "dest": Path(dest), "max_bytes": max_bytes,
             "resume": resume, "timeout_s": timeout_s}
        )
        if url not in self.downloads:
            raise AssertionError(f"download no dictado en el stub: {url}")
        data = self.downloads[url]
        if max_bytes is not None and len(data) > max_bytes:
            raise AssertionError("payload supera max_bytes")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {"bytes": len(data), "url": url, "resumed": False,
                "from_cache": False, "sha256": hashlib.sha256(data).hexdigest()}


# ---------------------------------------------------------------------------
# discover_dated_extract
# ---------------------------------------------------------------------------


def test_discover_elige_la_fecha_mas_reciente() -> None:
    html = b"""
    <ul>
      <li><a href="colombia-240101.osm.pbf">enero</a></li>
      <li><a href="colombia-251111.osm.pbf">noviembre</a></li>
      <li><a href="colombia-250999.osm.pbf">septiembre</a></li>
    </ul>
    """
    http = StubHttp({GEOFABRIK_INDEX: html})
    info = discover_dated_extract(http)
    assert info["date"] == "251111"
    base = "https://download.geofabrik.de/south-america/colombia-251111"
    assert info["pbf_url"] == f"{base}.osm.pbf"
    assert info["md5_url"] == f"{base}.osm.pbf.md5"
    assert http.requested == [("GET", GEOFABRIK_INDEX)]


def test_discover_html_vacio_lanza_runtime_error() -> None:
    http = StubHttp({GEOFABRIK_INDEX: b""})
    with pytest.raises(RuntimeError, match="no se encontraron extractos fechados"):
        discover_dated_extract(http)


def test_discover_html_sin_fechas_lanza_runtime_error() -> None:
    html = b"<html><body><h1>Colombia</h1><p>colombia-latest.osm.pbf</p></body></html>"
    http = StubHttp({GEOFABRIK_INDEX: html})
    with pytest.raises(RuntimeError, match="no se encontraron extractos fechados"):
        discover_dated_extract(http)


def test_discover_solo_acepta_seis_digitos_con_extension_exacta() -> None:
    # Candidatos que el regex debe ignorar: latest, 5 y 7 digitos, otra extension.
    html = (
        b"colombia-latest.osm.pbf "
        b"colombia-99999.osm.pbf "
        b"colombia-2511111.osm.pbf "
        b"colombia-250101.osm.zip "
        b"colombia-250630.osm.pbf"
    )
    http = StubHttp({GEOFABRIK_INDEX: html})
    info = discover_dated_extract(http)
    assert info["date"] == "250630"
    assert info["pbf_url"].endswith("colombia-250630.osm.pbf")


def test_discover_ignora_sufijos_tras_la_fecha() -> None:
    # "colombia-251111.osm.pbf.bak" sí contiene el patron completo: se acepta.
    # El maximo es lexicografico: 999999 gana aunque no sea una fecha valida.
    html = b"colombia-251111.osm.pbf.bak colombia-999999.osm.pbf"
    http = StubHttp({GEOFABRIK_INDEX: html})
    info = discover_dated_extract(http)
    assert info["date"] == "999999"


# ---------------------------------------------------------------------------
# fetch_extract
# ---------------------------------------------------------------------------


def _info() -> dict[str, str]:
    date = "251111"
    base = f"https://download.geofabrik.de/south-america/colombia-{date}"
    return {"date": date, "pbf_url": f"{base}.osm.pbf", "md5_url": f"{base}.osm.pbf.md5"}


def test_fetch_md5_correcto_devuelve_metadatos(tmp_path: Path) -> None:
    pbf = b"contenido-determinista-del-extracto-osm"
    md5 = hashlib.md5(pbf).hexdigest()
    info = _info()
    http = StubHttp(
        responses={info["md5_url"]: f"{md5}  colombia-251111.osm.pbf\n".encode()},
        downloads={info["pbf_url"]: pbf},
    )
    workdir = tmp_path / "extractos"
    meta = fetch_extract(http, workdir, info)
    assert meta["date"] == "251111"
    assert meta["md5"] == md5
    assert meta["url"] == info["pbf_url"]
    assert meta["bytes"] == len(pbf)
    assert meta["sha256"] == hashlib.sha256(pbf).hexdigest()
    assert Path(meta["path"]) == workdir / "colombia-251111.osm.pbf"
    assert Path(meta["path"]).is_file()
    # la descarga se pidio reanudable con el limite por defecto
    assert http.downloaded[0]["resume"] is True
    assert http.downloaded[0]["max_bytes"] == 4 * 1024**3


def test_fetch_md5_en_mayusculas_y_con_texto_adelante_pasa(tmp_path: Path) -> None:
    # formato tipico de un fichero .md5: hash en mayusculas + nombre del fichero
    pbf = b"otro-contenido-del-extracto"
    md5 = hashlib.md5(pbf).hexdigest().upper()
    info = _info()
    http = StubHttp(
        responses={info["md5_url"]: f"{md5}  colombia-251111.osm.pbf\n".encode()},
        downloads={info["pbf_url"]: pbf},
    )
    meta = fetch_extract(http, tmp_path, info)
    assert meta["md5"] == md5.lower()
    assert meta["sha256"] == hashlib.sha256(pbf).hexdigest()


def test_fetch_md5_incorrecto_lanza_runtime_error(tmp_path: Path) -> None:
    pbf = b"contenido-del-extracto"
    info = _info()
    http = StubHttp(
        responses={info["md5_url"]: b"0" * 32 + b"  colombia-251111.osm.pbf\n"},
        downloads={info["pbf_url"]: pbf},
    )
    actual = hashlib.md5(pbf).hexdigest()
    with pytest.raises(RuntimeError) as excinfo:
        fetch_extract(http, tmp_path, info)
    msg = str(excinfo.value)
    assert "md5 del extracto OSM no coincide" in msg
    assert actual in msg
    assert "0" * 32 in msg


def test_fetch_crea_el_workdir_si_no_existe(tmp_path: Path) -> None:
    pbf = b"x" * 100
    info = _info()
    http = StubHttp(
        responses={info["md5_url"]: hashlib.md5(pbf).hexdigest().encode()},
        downloads={info["pbf_url"]: pbf},
    )
    workdir = tmp_path / "anidado" / "profundo" / "extractos"
    meta = fetch_extract(http, workdir, info)
    assert workdir.is_dir()
    assert Path(meta["path"]).parent == workdir
