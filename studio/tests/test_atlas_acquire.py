"""Tests de adquisición del atlas con HTTP simulado (sin red en CI).

Cubre: allowlist/SSRF del HttpClient, rate limit/Retry-After/circuit breaker,
descarga reanudable con hash y tamaño máximo, veredicto de licencias de Commons,
y pipeline completo (commons) con respuestas API falsas: selección por licencia,
rechazo de mapas, deduplicación por sha1, asignación a lugar por GPS, licencia
escrita, cobertura y reanudación (segunda pasada no redescarga).
"""

from __future__ import annotations

import hashlib
import json
import struct
import urllib.error
import zlib
from pathlib import Path
from typing import Any

import pytest

from comfy_studio.atlas.acquire.commons import license_verdict
from comfy_studio.atlas.acquire.http import (
    CircuitOpenError,
    FileTooLargeError,
    ForbiddenError,
    HashMismatchError,
    HttpClient,
    HttpError,
)
from comfy_studio.atlas.acquire.pipeline import AcquisitionPipeline
from comfy_studio.atlas.service import AtlasService


def make_png_bytes(w: int = 64, h: int = 48, color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    # degradado dependiente del color para que el phash varíe entre imágenes
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            row += bytes(((color[0] + x * 3 + y) % 256,
                          (color[1] + y * 5) % 256,
                          (color[2] + x + y * 2) % 256))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_BYTES = make_png_bytes()


def content_for(title: str) -> bytes:
    """Contenido 'descargado' determinista por archivo (color distinto por título)."""
    d = hashlib.sha1(title.encode("utf-8")).digest()
    return make_png_bytes(color=(d[0], d[1], d[2]))


# ---------------------------------------------------------------------------
# HttpClient: allowlist, SSRF, circuit breaker, descarga
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200, headers: dict[str, str] | None = None):
        self._data = data
        self.status = status
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out, self._data = self._data, b""
            return out
        out, self._data = self._data[:n], self._data[n:]
        return out

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeOpener:
    """Sustituye al opener de urllib: programa respuestas por URL o lanza HTTPError."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def open(self, req: Any, timeout: float = 0) -> Any:
        url = req.full_url
        self.requests.append((url, dict(req.header_items())))
        handler = self.routes.get(url)
        if handler is None:
            for prefix, h in self.routes.items():
                if prefix.endswith("*") and url.startswith(prefix[:-1]):
                    handler = h
                    break
        if handler is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(req)
        return FakeResponse(handler)


def make_client(opener: FakeOpener, **kw: Any) -> HttpClient:
    c = HttpClient(
        "test-agent/1.0",
        ["commons.wikimedia.org", "upload.wikimedia.org", "example.org"],
        min_interval_s=0.0,
        max_retries=kw.pop("max_retries", 2),
        backoff_base_s=0.0,
        circuit_failures=kw.pop("circuit_failures", 2),
        circuit_cooldown_s=60.0,
        **kw,
    )
    c._opener = opener  # type: ignore[attr-defined]
    return c


def test_http_rejects_non_https_and_unknown_host() -> None:
    c = make_client(FakeOpener())
    with pytest.raises(HttpError):
        c.request("GET", "http://commons.wikimedia.org/w/api.php")
    with pytest.raises(ForbiddenError):
        c.request("GET", "https://127.0.0.1:8080/x")
    with pytest.raises(ForbiddenError):
        c.request("GET", "https://evil.example.com/x")


def test_http_circuit_breaker_opens_after_failures() -> None:
    op = FakeOpener()
    err = urllib.error.HTTPError(
        "https://example.org/x", 429, "slow down", {"Retry-After": "0"}, None  # type: ignore[arg-type]
    )
    op.routes["https://example.org/x"] = err
    c = make_client(op, max_retries=0, circuit_failures=2)
    with pytest.raises(HttpError):
        c.request("GET", "https://example.org/x")
    with pytest.raises(HttpError):
        c.request("GET", "https://example.org/x")
    with pytest.raises(CircuitOpenError):
        c.request("GET", "https://example.org/x")


def test_download_resume_hash_and_limits(tmp_path: Path) -> None:
    data = PNG_BYTES
    sha1 = hashlib.sha1(data).hexdigest()
    # primera mitad, luego reanudación con Range
    state = {"calls": 0}

    def handler(req: Any) -> FakeResponse:
        state["calls"] += 1
        rng = req.get_header("Range")
        if rng:
            start = int(rng.replace("bytes=", "").split("-")[0])
            return FakeResponse(data[start:], status=206)
        return FakeResponse(data[: len(data) // 2], status=200)

    op = FakeOpener()
    url = "https://upload.wikimedia.org/test/file.png"
    op.routes[url] = handler
    c = make_client(op, max_retries=0)
    dest = tmp_path / "file.png"
    # primer intento parcial: simular corte escribiendo .part a mano
    part = dest.with_suffix(".png.part")
    part.write_bytes(data[: len(data) // 2])
    res = c.download(url, dest, sha1=sha1, max_bytes=10 * 1024 * 1024)
    assert res["resumed"] is True
    assert dest.read_bytes() == data
    assert res["sha256"] == hashlib.sha256(data).hexdigest()

    # hash incorrecto -> error y no promueve
    part2 = tmp_path / "bad.png.part"
    dest2 = tmp_path / "bad.png"
    part2.write_bytes(data)
    with pytest.raises(HashMismatchError):
        c.download(url, dest2, sha1="0" * 40)
    assert not dest2.exists()

    # tamaño máximo -> FileTooLargeError y .part eliminado
    dest3 = tmp_path / "big.png"
    with pytest.raises(FileTooLargeError):
        c.download(url, dest3, sha1=sha1, max_bytes=10)
    assert not dest3.exists()
    assert not dest3.with_suffix(".png.part").exists()


def test_license_verdict() -> None:
    assert license_verdict("CC BY-SA 4.0") == (True, "CC BY-SA 4.0")
    assert license_verdict("CC BY 2.0") == (True, "CC BY 2.0")
    assert license_verdict("CC0 1.0 Universal") == (True, "CC0")
    assert license_verdict("Public domain") == (True, "Public domain")
    assert license_verdict("CC BY-NC 4.0")[0] is False
    assert license_verdict("CC BY-ND 3.0")[0] is False
    assert license_verdict("All rights reserved")[0] is False
    assert license_verdict(None)[0] is False


# ---------------------------------------------------------------------------
# Pipeline con API de Commons falsa
# ---------------------------------------------------------------------------


def commons_api_payload(params: dict[str, Any]) -> bytes:
    """Respuesta falsa de la API de Commons según los parámetros."""
    if params.get("list") == "geosearch":
        return json.dumps({"query": {"geosearch": [
            {"title": "File:Puente 4 Sur foto A.jpg", "lat": 6.2089, "lon": -75.5671},
            {"title": "File:Puente 4 Sur foto B.jpg", "lat": 6.2087, "lon": -75.5669},
            {"title": "File:Mapa del puente 4 Sur.jpg", "lat": 6.2088, "lon": -75.5670},
        ]}}).encode()
    if params.get("list") == "categorymembers":
        return json.dumps({"query": {"categorymembers": [
            {"title": "File:Puente 4 Sur foto A.jpg", "type": "file"},
            {"title": "File:Puente 4 Sur foto C derechos reservados.jpg", "type": "file"},
        ]}}).encode()
    if params.get("list") == "search":
        return json.dumps({"query": {"search": []}}).encode()
    if params.get("prop") == "imageinfo|coordinates" or "imageinfo" in str(params.get("prop")):
        pages = []
        for title in params.get("titles", "").split("|"):
            if "mapa" in title.lower():
                lic = "CC BY-SA 4.0"
            elif "derechos reservados" in title.lower():
                lic = "All rights reserved"
            else:
                lic = "CC BY-SA 4.0"
            content = content_for(title)
            pages.append({
                "title": title,
                "imageinfo": [{
                    "url": "https://upload.wikimedia.org/fake/" + title.replace(" ", "_"),
                    "descriptionurl": (
                        "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")
                    ),
                    "thumburl": (
                        "https://thumb.wikimedia.org/fake/640px-" + title.replace(" ", "_")
                    ),
                    "width": 3000, "height": 2000, "size": len(content),
                    "sha1": hashlib.sha1(content).hexdigest(),
                    "mime": "image/png", "timestamp": "2026-01-01T00:00:00Z",
                    "extmetadata": {
                        "LicenseShortName": {"value": lic},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                        "Artist": {"value": "Autora Fake"},
                        "DateTimeOriginal": {"value": "2025-03-04 10:00:00"},
                        "ImageDescription": {"value": "Vista del puente"},
                        "GPSLatitude": {"value": "6.2088"},
                        "GPSLongitude": {"value": "-75.5670"},
                    },
                }],
                "coordinates": [{"lat": 6.2088, "lon": -75.5670}],
            })
        return json.dumps({"query": {"pages": pages}}).encode()
    return json.dumps({"query": {}}).encode()


@pytest.fixture()
def fake_pipeline(tmp_path: Path) -> AcquisitionPipeline:
    root = tmp_path / "atlas"
    pipe = AcquisitionPipeline(root, max_refs=10, dry_run=False, min_interval_s=0.0)
    op = FakeOpener()
    api_url = "https://commons.wikimedia.org/w/api.php*"

    def api_handler(req: Any) -> FakeResponse:
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(req.full_url).query)
        flat = {k: v[0] for k, v in q.items()}
        return FakeResponse(commons_api_payload(flat))

    def upload_handler(req: Any) -> FakeResponse:
        title = req.full_url.rsplit("/", 1)[-1].replace("_", " ")
        if title.startswith("640px-"):
            title = title[len("640px-"):]
        if not title.startswith("File:"):
            title = "File:" + title
        return FakeResponse(content_for(title))

    op.routes[api_url] = api_handler
    op.routes["https://upload.wikimedia.org/fake/*"] = upload_handler
    op.routes["https://thumb.wikimedia.org/fake/*"] = upload_handler
    pipe.http._opener = op  # type: ignore[attr-defined]
    pipe.ensure_seed_places()
    return pipe


def test_pipeline_commons_end_to_end(fake_pipeline: AcquisitionPipeline) -> None:
    pipe = fake_pipeline
    stats = pipe.run_commons()
    assert stats["ok"], stats
    # 2 fotos válidas (A y B); mapa rechazado por título; derechos reservados por licencia
    assert stats["downloaded"] == 2, stats
    svc = AtlasService(pipe.root)
    store = svc.store
    s = store.stats()
    assert s["references_usable"] == 2
    assert s["places_with_usable_views"] >= 1
    refs = store.all_references(usable_only=True)
    assert all(r.license == "CC BY-SA 4.0" for r in refs)
    assert all(r.date_reliability == "captured" for r in refs)
    assert all((pipe.root / "files" / r.file_path).is_file() for r in refs)
    assert all((pipe.root / "licenses" / f"{r.ref_id}.txt").is_file() for r in refs)
    # asignadas al lugar semilla más cercano (Puente de la 4 Sur)
    place_ids = {r.place_id for r in refs}
    assert place_ids == {"plc_medellin_puente_de_la_4_sur"}
    # cobertura recomputada
    cov = store.get_coverage("plc_medellin_puente_de_la_4_sur")
    assert cov is not None and cov.views_observed == 2
    # manifest y atribución existen
    assert (pipe.root / "manifest.json").is_file()
    assert (pipe.root / "ATTRIBUTION.md").is_file()
    # verificación de integridad OK
    report = svc.verify()
    assert report["ok"], report["errors"]
    # reanudación: segunda pasada no descarga de nuevo
    stats2 = pipe.run_commons()
    assert stats2["downloaded"] == 0
    assert stats2["skipped_resume"] == 2


def test_pipeline_rejects_license_and_content(fake_pipeline: AcquisitionPipeline) -> None:
    pipe = fake_pipeline
    stats = pipe.run_commons()
    store = pipe.svc.store
    titles = {r.title for r in store.all_references(usable_only=True)}
    assert not any("derechos reservados" in t.lower() for t in titles)
    assert not any("mapa" in t.lower() for t in titles)
    # los rechazos quedaron registrados como items de adquisición
    assert stats["rejected"] >= 2
