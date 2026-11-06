"""Tests for template_discovery search (PR 2).

Uses a real ThreadingHTTPServer on 127.0.0.1 port 0 to serve fake index JSON.
No mocking of the search itself: CommunityTemplateSearch consumes the real
parsed index through CommunityIndexDownloader.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from comfy_studio.orchestration.template_discovery import (
    CommunityIndexDownloader,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_server(
    payload: dict,
) -> tuple[ThreadingHTTPServer, int]:
    """Start a real HTTP server on 127.0.0.1 port 0, return (server, port)."""
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
# Fixtures / payloads
# ---------------------------------------------------------------------------

# The real custom-node-list.json shape:
# {"custom_nodes": [{"author", "title", "reference", "files",
#                     "install_type", "description"}, ...]}

INDEX_PAYLOAD = {
    "custom_nodes": [
        {
            "author": "comfyanonymous",
            "title": "ComfyUI-IPAdapter",
            "reference": "https://github.com/cubiq/ComfyUI_IPAdapter_plus",
            "files": [
                "https://github.com/cubiq/ComfyUI_IPAdapter_plus/releases/download/v1.0/ipadapter.zip"
            ],
            "install_type": "git-clone",
            "description": (
                "IPAdapter Plus: plug image and face adapters into ComfyUI. "
                "Use IPAdapterApply, IPAdapterModelLoader, etc."
            ),
        },
        {
            "author": "ArtBot",
            "title": "ComfyUI-ArtBot-Animation",
            "reference": "https://github.com/artbot/ComfyUI-ArtBot",
            "files": [
                "https://github.com/artbot/ComfyUI-ArtBot/releases/download/v1/artbot.zip"
            ],
            "install_type": "git-clone",
            "description": (
                "Animación frame-to-frame para ComfyUI. "
                "Genera animaciones suaves y estables."
            ),
        },
        {
            "author": "SomeDev",
            "title": "RandomPack",
            "reference": "https://github.com/somedev/random",
            "files": [
                "https://github.com/somedev/random/releases/download/v1/random.zip"
            ],
            "install_type": "git-clone",
            "description": "A completely unrelated pack.",
        },
    ]
}


@pytest.fixture()
def server_and_url():
    server, port = _start_server(INDEX_PAYLOAD)
    url = f"http://127.0.0.1:{port}/custom-node-list.json"
    yield server, url
    _stop_server(server)


@pytest.fixture()
def tmp_cache_dir(tmp_path: Path):
    return tmp_path / ".cache" / "template_index"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_orders_by_node_coverage(server_and_url, tmp_path: Path):
    """Pack with matching installed nodes comes before pack with none."""
    from comfy_studio.orchestration.template_discovery import (
        CommunityTemplateSearch,
    )

    server, url = server_and_url
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    downloader = CommunityIndexDownloader(url=url, env=env)

    search = CommunityTemplateSearch(downloader=downloader)

    # Installed nodes that share tokens with 'ComfyUI-IPAdapter':
    # 'ipadapter' appears in 'IPAdapterApply', 'IPAdapterModelLoader', etc.
    installed_nodes = {
        "IPAdapterApply",
        "IPAdapterModelLoader",
        "IPAdapterEncoder",
        "KSampler",
        "EmptyLatentImage",
    }

    results = search.search("ipadapter", installed_nodes=installed_nodes, limit=10)

    assert results, "search must return results"
    # The IPAdapter pack must be first (highest coverage).
    assert results[0].template.name == "ComfyUI-IPAdapter"
    assert results[0].coverage == 3
    assert results[0].installed_pack is True
    # matched_nodes must be exactly the attributed installed nodes, sorted.
    assert results[0].matched_nodes == sorted(
        {"IPAdapterApply", "IPAdapterModelLoader", "IPAdapterEncoder"}
    )
    # RandomPack has no token overlap with any installed node and no relevance
    # to 'ipadapter', so it is filtered out (coverage=0, relevance=0).
    all_names = [r.template.name for r in results]
    assert "RandomPack" not in all_names


def test_matched_nodes_come_from_the_pod_not_from_the_index(server_and_url, tmp_path: Path):
    """Every matched node name must be a member of the installed_nodes set.

    Proves that no node names were invented from the index JSON.
    """
    from comfy_studio.orchestration.template_discovery import (
        CommunityTemplateSearch,
    )

    server, url = server_and_url
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    downloader = CommunityIndexDownloader(url=url, env=env)

    search = CommunityTemplateSearch(downloader=downloader)
    installed_nodes = {"IPAdapterApply", "KSampler", "VAEDecode"}

    results = search.search("ipadapter", installed_nodes=installed_nodes, limit=10)

    for r in results:
        for node_name in r.matched_nodes:
            assert node_name in installed_nodes, (
                f"Node '{node_name}' was not in installed_nodes -- fabricated!"
            )


def test_relevance_orders_when_no_nodes_installed(tmp_path: Path):
    """With installed_nodes=None/empty, ordering falls back to lexical relevance."""
    from comfy_studio.orchestration.template_discovery import (
        CommunityTemplateSearch,
    )

    server, port = _start_server(INDEX_PAYLOAD)
    url = f"http://127.0.0.1:{port}/custom-node-list.json"
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    downloader = CommunityIndexDownloader(url=url, env=env)
    search = CommunityTemplateSearch(downloader=downloader)

    results = search.search("animation", installed_nodes=None, limit=10)

    _stop_server(server)

    assert results, "search must return results"
    # ArtBot-Animation has 'animación' in its description, so it must appear
    # in the results; RandomPack has no 'animation' tokens and no installed
    # nodes, so it is filtered out (coverage=0, relevance=0).
    ranked_names = [r.template.name for r in results]
    assert "ComfyUI-ArtBot-Animation" in ranked_names
    # With no installed nodes, coverage is 0 for everything; ordering is by
    # relevance desc, so the ArtBot pack (highest relevance) comes first.
    assert ranked_names[0] == "ComfyUI-ArtBot-Animation"


def test_query_with_no_matches_returns_honest_results(server_and_url, tmp_path: Path):
    """Query matching nothing returns results with relevance=0 (not fabricated).

    When neither the query nor any installed node matches any pack, the honest
    result is an empty list (nothing has coverage>0 or relevance>0).
    """
    from comfy_studio.orchestration.template_discovery import (
        CommunityTemplateSearch,
    )

    server, url = server_and_url
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    downloader = CommunityIndexDownloader(url=url, env=env)
    search = CommunityTemplateSearch(downloader=downloader)

    # 'zzzz-no-match-zzzz' matches no title/description; 'KSampler' shares no
    # token with any pack title.  Honest result: [].
    results = search.search("zzzz-no-match-zzzz", installed_nodes={"KSampler"}, limit=10)

    assert results == [], (
        "When nothing matches (relevance=0, coverage=0), the honest result is []"
    )


def test_absurd_installed_nodes_does_not_raise(tmp_path: Path):
    """Un `installed_nodes` no iterable degrada a "sin info de pod": nunca lanza.

    El contrato dice que `search()` no lanza nunca; un caller puede pasar
    cualquier cosa y debe obtener una lista, no una excepción.
    """
    from comfy_studio.orchestration.template_discovery import (
        CommunityIndexDownloader,
        CommunityTemplateSearch,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    closed_url = f"http://127.0.0.1:{closed_port}/custom-node-list.json"
    downloader = CommunityIndexDownloader(
        url=closed_url, env={"COMFY_DATA_DIR": str(tmp_path)}
    )
    search = CommunityTemplateSearch(downloader=downloader)
    # Índice no disponible: [] sin lanzar, con un valor absurdo en installed_nodes.
    assert search.search("ipadapter", installed_nodes=42) == []  # type: ignore[arg-type]
    assert search.search("ipadapter", installed_nodes=object()) == []  # type: ignore[arg-type]


def test_unavailable_index_returns_empty(tmp_path: Path):
    """Closed port -> search returns [] without raising."""
    from comfy_studio.orchestration.template_discovery import (
        CommunityTemplateSearch,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    closed_url = f"http://127.0.0.1:{closed_port}/custom-node-list.json"

    downloader = CommunityIndexDownloader(url=closed_url, env={"COMFY_DATA_DIR": str(tmp_path)})
    search = CommunityTemplateSearch(downloader=downloader)

    results = search.search("anything", installed_nodes=None, limit=10)
    assert results == []


def test_satisfies_discovery_provider_protocol(server_and_url, tmp_path: Path):
    """CommunityTemplateSearch must satisfy the DiscoveryProvider Protocol."""
    from comfy_studio.orchestration.template_discovery import (
        CommunityTemplateSearch,
        DiscoveredTemplate,
    )

    server, url = server_and_url
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    downloader = CommunityIndexDownloader(url=url, env=env)
    search = CommunityTemplateSearch(downloader=downloader)

    # Structural check: has a callable `discover` method.
    assert callable(getattr(search, "discover", None))

    found = search.discover("ipadapter", output_kind="", limit=5)
    assert found, "discover() must return results"
    assert all(isinstance(t, DiscoveredTemplate) for t in found)
    assert all(t.source == "community" for t in found)


def test_tokenize_handles_hyphens_and_accents():
    from comfy_studio.orchestration.template_discovery import tokenize

    assert tokenize("ComfyUI-IPAdapter") == {"comfyui", "ipadapter"}
    # animación -> animacion (stripped accent)
    assert "animacion" in tokenize("animación")
    assert "animación" not in tokenize("animación")  # accent stripped


def test_entry_without_title_is_skipped():
    """Entries without 'title' are skipped by the parser; search never sees them."""
    from comfy_studio.orchestration.template_discovery import (
        CommunityIndexDownloader,
    )

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

    server, port = _start_server(payload)
    url = f"http://127.0.0.1:{port}/custom-node-list.json"
    try:
        downloader = CommunityIndexDownloader(url=url)
        result = downloader.fetch()
        assert len(result.entries) == 1
        assert result.entries[0].title == "Has Title"
    finally:
        _stop_server(server)
