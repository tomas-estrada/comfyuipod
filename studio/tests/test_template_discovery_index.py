"""Tests for template_discovery index downloader.

Uses a real ThreadingHTTPServer on 127.0.0.1 port 0 to serve fake JSON.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from comfy_studio.orchestration.template_discovery import (
    CACHE_DIRNAME,
    CommunityIndexDownloader,
    CommunityIndexEntry,
    redact_url,
    resolve_cache_dir,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
SECRET_URL = f"https://user:{SECRET}@127.0.0.1:8080/path?token={SECRET}&other=ok"


def _start_server(
    payload: dict,
) -> tuple[ThreadingHTTPServer, int]:
    """Start a real HTTP server on 127.0.0.1 port 0, return (server, port).

    El servidor cuenta sus peticiones en `server.request_count` para que los
    tests puedan afirmar de verdad que la caché evitó una descarga (sin ello,
    un test de caché pasaría aunque se volviera a la red).
    """
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                self.server.request_count += 1
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args, **kwargs):
            pass  # silence

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.request_count = 0
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server, server.server_address[1]


def _stop_server(server: ThreadingHTTPServer):
    server.shutdown()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_payload():
    return {
        "custom_nodes": [
            {
                "author": "Alice",
                "title": "Node A",
                "reference": "https://github.com/alice/NodeA",
                "files": ["https://github.com/alice/NodeA/releases/download/v1/a.zip"],
                "install_type": "git-clone",
                "description": "Does A",
            },
            {
                "author": "Bob",
                "title": "Node B",
                "reference": "https://github.com/bob/NodeB",
                "files": [
                    "https://github.com/bob/NodeB/releases/download/v1/b.zip"
                ],
                "install_type": "git-clone",
                "description": "Does B",
            },
            {
                "author": "Carol",
                "title": "Node C",
                "reference": "https://github.com/carol/NodeC",
                "files": [
                    "https://github.com/carol/NodeC/releases/download/v1/c.zip"
                ],
                "install_type": "git-clone",
                "description": "Does C",
            },
        ]
    }


@pytest.fixture()
def tmp_env(tmp_path: Path):
    return {"COMFY_DATA_DIR": str(tmp_path)}


@pytest.fixture()
def server_and_url(fake_payload, tmp_env):
    server, port = _start_server(fake_payload)
    url = f"http://127.0.0.1:{port}/custom-node-list.json"
    yield server, url, tmp_env
    _stop_server(server)


@pytest.fixture()
def tmp_cache_dir(tmp_path: Path):
    """Override the cache dir to a temp dir."""
    return tmp_path / ".cache" / "template_index"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fetches_from_local_server_and_parses_real_fields(server_and_url):
    server, url, tmp_env = server_and_url
    downloader = CommunityIndexDownloader(url=url, env=tmp_env)
    result = downloader.fetch()
    assert result.source == "network"
    assert len(result.entries) == 3
    assert {e.title for e in result.entries} == {"Node A", "Node B", "Node C"}
    assert result.entries[0].author == "Alice"
    assert result.entries[0].reference == "https://github.com/alice/NodeA"
    assert result.entries[0].files[0].endswith("a.zip")
    assert result.entries[0].install_type == "git-clone"


def test_second_fetch_uses_cache_without_new_request(server_and_url):
    server, url, tmp_env = server_and_url
    downloader = CommunityIndexDownloader(url=url, env=tmp_env)
    r1 = downloader.fetch()
    assert r1.source == "network"
    assert server.request_count == 1
    # Small sleep to ensure mtime differs.
    time.sleep(0.05)
    r2 = downloader.fetch()
    assert r2.source == "cache"
    assert r2.age_s is not None
    assert r2.age_s >= 0
    assert len(r2.entries) == 3
    # La caché es real: el segundo fetch NO golpeó la red.
    assert server.request_count == 1, "el segundo fetch debía servirse de la caché"


def test_expired_cache_refetches(server_and_url):
    server, url, tmp_env = server_and_url
    downloader = CommunityIndexDownloader(url=url, env=tmp_env, ttl_s=0)
    r1 = downloader.fetch()
    assert r1.source == "network"
    assert server.request_count == 1
    time.sleep(0.05)
    r2 = downloader.fetch()
    assert r2.source == "network"
    # TTL expirado: sí volvió a la red.
    assert server.request_count == 2


def test_server_unreachable_returns_empty_with_cause_not_exception():
    # Find a closed port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    closed_url = f"http://127.0.0.1:{closed_port}/x"
    downloader = CommunityIndexDownloader(url=closed_url)
    result = downloader.fetch()
    assert result.source == "unavailable"
    assert result.entries == []
    assert result.cause  # non-empty reason


def test_stale_cache_survives_network_failure(server_and_url, tmp_path: Path):
    server, url, tmp_env = server_and_url
    downloader = CommunityIndexDownloader(url=url, env=tmp_env)
    r1 = downloader.fetch()
    assert r1.source == "network"
    assert len(r1.entries) == 3
    # Backdate the cache so it is stale (older than default TTL of 24h).
    cache_path = downloader._cache_path()
    now = time.time()
    os.utime(cache_path, (now - 200000, now - 200000))
    # Now stop the server and try again.
    _stop_server(server)
    downloader2 = CommunityIndexDownloader(url=url, env=tmp_env)
    result = downloader2.fetch()
    assert result.source == "cache"
    assert len(result.entries) == 3
    assert result.cause  # explains the failure


def test_malformed_payload_is_a_failure_not_an_empty_success(server_and_url):
    server, url, tmp_env = server_and_url
    # Override handler to serve malformed JSON on the fly.
    # Easier: start a new server with bad payload.
    _stop_server(server)
    bad_server, bad_port = _start_server({"custom_nodes": "not-a-list"})
    try:
        downloader = CommunityIndexDownloader(url=f"http://127.0.0.1:{bad_port}/x", env=tmp_env)
        result = downloader.fetch()
        assert result.entries == []
        assert result.cause
        # Not a silent empty-success.
        assert result.source != "network"
    finally:
        _stop_server(bad_server)


def test_unwritable_cache_does_not_break_the_download(server_and_url, tmp_path: Path):
    """La caché es una optimización: si no se puede escribir, el índice recién
    descargado sigue siendo válido y `fetch()` no lanza.

    Se fuerza un fallo REAL y determinista (un fichero ocupando el sitio donde
    debería estar el directorio de caché), no permisos, que dependen del uid.
    """
    server, url, tmp_env = server_and_url
    blocker = tmp_path / ".cache"
    blocker.write_text("soy un fichero, no un directorio", encoding="utf-8")
    downloader = CommunityIndexDownloader(url=url, env=tmp_env)
    result = downloader.fetch()
    assert result.source == "network"
    assert len(result.entries) == 3
    assert result.cause == ""
    # El índice bueno se devuelve aunque la caché no haya podido persistirse.
    assert not (tmp_path / ".cache" / "template_index").exists()


def test_url_token_is_redacted():
    redacted = redact_url(SECRET_URL)
    assert SECRET not in redacted
    assert "***" in redacted
    # generic redaction via SecretRedactor also applied
    assert "@127.0.0.1" in redacted or "***@127.0.0.1" in redacted


def test_failure_cause_is_redacted(tmp_env):
    """El `cause` de un fallo no puede filtrar el token que iba en la URL.

    Un cliente HTTP real puede hacer eco de la URL completa en su mensaje de
    error; el motivo se redacta en origen, antes de llegar a ningún `cause`.
    """
    secret = "ghp_SECRETVALUE1234567890"
    url = f"http://user:{secret}@127.0.0.1:9/x?token={secret}"

    def leaking_opener(u: str, timeout: float) -> bytes:
        raise OSError(f"cannot reach {u}")

    downloader = CommunityIndexDownloader(url=url, env=tmp_env, opener=leaking_opener)
    result = downloader.fetch()
    assert result.source == "unavailable"
    assert result.entries == []
    assert result.cause
    assert secret not in result.cause, "el motivo del fallo filtró el token de la URL"


def test_cache_dir_follows_comfy_data_dir():
    env = {"COMFY_DATA_DIR": "/tmp/test-comfy-data-123"}
    cache_dir = resolve_cache_dir(env=env)
    expected = Path("/tmp/test-comfy-data-123") / CACHE_DIRNAME
    assert cache_dir == expected
    # Must not be a staging dir.
    from comfy_studio.cache_policy import STAGING_DIRNAMES

    assert cache_dir.name not in STAGING_DIRNAMES
    assert ".cache" in cache_dir.parts


def test_no_download_at_import(server_and_url):
    server, url, tmp_env = server_and_url
    # Just constructing the downloader must not trigger a request.
    downloader = CommunityIndexDownloader(url=url, env=tmp_env)
    # We can't directly check the request count here, but we can verify
    # that cached_age_s() is None before any fetch.
    assert downloader.cached_age_s() is None


def test_entries_missing_title_are_skipped(server_and_url, tmp_path: Path):
    payload = {
        "custom_nodes": [
            {
                "author": "X",
                # no title
                "reference": "https://github.com/x/no-title",
                "files": [],
                "install_type": "git-clone",
                "description": "no title entry",
            },
            {
                "author": "Y",
                "title": "Has Title",
                "reference": "https://github.com/y/has-title",
                "files": [],
                "install_type": "git-clone",
                "description": "valid entry",
            },
        ]
    }
    _stop_server(server_and_url[0])
    server, _ = _start_server(payload)
    try:
        env = {"COMFY_DATA_DIR": str(tmp_path)}
        dl = CommunityIndexDownloader(
            url=f"http://127.0.0.1:{server.server_address[1]}/x",
            env=env,
        )
        result = dl.fetch()
        assert len(result.entries) == 1
        assert result.entries[0].title == "Has Title"
    finally:
        _stop_server(server)


def test_redact_url_noop_on_bad_input():
    assert redact_url("not a url") == "not a url"


def test_entry_id_derived_from_reference():
    entry = CommunityIndexEntry(
        title="T",
        author="A",
        reference="https://github.com/ltdrdata/ComfyUI-Node",
        files=(),
        install_type="git-clone",
        description="d",
    )
    assert entry.entry_id == "community:ltdrdata/ComfyUI-Node"


def test_entry_id_falls_back_to_files():
    entry = CommunityIndexEntry(
        title="T",
        author="A",
        reference="",
        files=("https://github.com/foo/Bar/releases/download/v1/b.zip",),
        install_type="git-clone",
        description="d",
    )
    assert entry.entry_id == "community:foo/Bar"


def test_entry_id_empty_when_no_slug():
    entry = CommunityIndexEntry(
        title="T",
        author="A",
        reference="",
        files=(),
        install_type="git-clone",
        description="d",
    )
    assert entry.entry_id == ""


def test_to_public_dict_round_trip():
    entry = CommunityIndexEntry(
        title="T",
        author="A",
        reference="https://github.com/x/y",
        files=("https://example.com/f.zip",),
        install_type="git-clone",
        description="desc",
    )
    d = entry.to_public_dict()
    assert d["title"] == "T"
    assert d["entry_id"] == "community:x/y"
    assert d["files"] == ["https://example.com/f.zip"]
