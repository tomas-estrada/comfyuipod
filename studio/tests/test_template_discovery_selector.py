"""Integration tests for PR 3: threshold-gated dynamic template discovery.

Uses the real CommunityTemplateSearch backed by a real ThreadingHTTPServer
serving a shaped custom-node-list.json (same approach as
test_template_discovery_search.py).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from comfy_studio.orchestration.template_discovery import (
    CommunityIndexDownloader,
    CommunityTemplateSearch,
)
from comfy_studio.orchestration.template_selector import (
    ShotIntent,
    TemplateSelector,
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


# The real custom-node-list.json shape expected by CommunityIndexDownloader.
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
    ]
}


@pytest.fixture()
def server_and_url(tmp_path: Path):
    server, port = _start_server(INDEX_PAYLOAD)
    url = f"http://127.0.0.1:{port}/custom-node-list.json"
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    yield server, url, env
    _stop_server(server)


def _make_search(url: str, env: dict) -> CommunityTemplateSearch:
    downloader = CommunityIndexDownloader(url=url, env=env)
    return CommunityTemplateSearch(downloader=downloader)


def _all_required_nodes() -> set[str]:
    """Collect every required_node declared by the curated registry."""
    sel = TemplateSelector()
    nodes: set[str] = set()
    for tpl in sel._registry.list_templates(include_workflow=False):
        nodes.update(tpl.get("required_nodes") or [])
    return nodes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dynamic_off_returns_only_curated(monkeypatch: pytest.MonkeyPatch, server_and_url):
    """STUDIO_DYNAMIC unset/0; assert every candidate has provenance=='curated'
    and that NO community candidate appears.

    THIS IS THE TEST THE OBJECTIVE NAMES: it must fail if STUDIO_DYNAMIC=0
    ever returns community results.
    """
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    server, url, env = server_and_url
    search = _make_search(url, env)

    sel = TemplateSelector(discovery_provider=search)
    cands = sel.select(
        ShotIntent(intent="ipadapter image"),
        available_nodes=set(),
        top_k=20,
    )
    assert cands, "select() must return candidates"
    # Every returned candidate must be curated.
    assert all(c.provenance == "curated" for c in cands), (
        "STUDIO_DYNAMIC=0 returned a non-curated candidate"
    )
    # No community template_id should appear.
    community_ids = [c.template_id for c in cands if c.provenance != "curated"]
    assert community_ids == [], (
        f"STUDIO_DYNAMIC=0 returned community candidates: {community_ids}"
    )


def test_dynamic_on_augments_when_curated_is_thin(
    monkeypatch: pytest.MonkeyPatch, server_and_url
):
    """An intent that matches few/no viable curated templates, with
    STUDIO_DYNAMIC=1 and the real discoverer; assert community candidates
    appear and carry provenance 'community:cache' or 'community:network'.
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    server, url, env = server_and_url
    search = _make_search(url, env)

    # "animation" matches the ArtBot-Animation community pack; with empty
    # available_nodes, no curated template is viable.
    sel = TemplateSelector(discovery_provider=search)
    cands = sel.select(
        ShotIntent(intent="animation"),
        available_nodes=set(),
        top_k=20,
    )
    # At least one community candidate must appear.
    community = [c for c in cands if c.provenance.startswith("community:")]
    assert community, (
        "STUDIO_DYNAMIC=1 with thin curated results should return community candidates"
    )
    valid_provenances = {"community:cache", "community:network"}
    assert all(c.provenance in valid_provenances for c in community), (
        f"Unexpected provenance values: {[c.provenance for c in community]}"
    )


def test_dynamic_on_does_not_call_discoverer_when_curated_is_sufficient(
    monkeypatch: pytest.MonkeyPatch, server_and_url
):
    """An intent that matches several viable curated templates; assert the
    discoverer was NOT consulted (count requests on the test HTTP server) and
    that all results are curated.
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    server, url, env = server_and_url
    search = _make_search(url, env)

    sel = TemplateSelector(discovery_provider=search)
    # Reset the server request count after constructing the search (which
    # does NOT hit the network; fetch() is lazy).
    server.request_count = 0

    all_nodes = _all_required_nodes()
    cands = sel.select(
        ShotIntent(intent="texto a video wan", output_kind="video"),
        available_nodes=all_nodes,
        top_k=20,
    )
    # The discoverer must NOT have been called: no HTTP requests.
    assert server.request_count == 0, (
        f"Discoverer was consulted ({server.request_count} HTTP requests) "
        "even though curated results were sufficient"
    )
    # All results must be curated.
    assert all(c.provenance == "curated" for c in cands)


def test_min_curated_threshold_is_configurable(
    monkeypatch: pytest.MonkeyPatch, server_and_url
):
    """Construct with min_curated=10 and prove the discoverer IS consulted
    even when several curated templates match.
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    server, url, env = server_and_url
    search = _make_search(url, env)

    # "animation" matches the ArtBot-Animation community pack; with empty
    # available_nodes no curated template is viable, but even if some were,
    # min_curated=10 forces the discoverer to be consulted.
    sel = TemplateSelector(discovery_provider=search, min_curated=10)
    server.request_count = 0

    cands = sel.select(
        ShotIntent(intent="animation"),
        available_nodes=set(),
        top_k=20,
    )
    # The discoverer MUST have been called because viable curated < 10.
    assert server.request_count >= 1, (
        "Discoverer was not consulted with min_curated=10 and <10 viable curated"
    )
    # There must be at least one community candidate in the results.
    community = [c for c in cands if c.provenance.startswith("community:")]
    assert community, "Expected community candidates when discoverer was called"


def test_unified_ranking_puts_viable_first(server_and_url):
    """Mixed viable/non-viable from both sources; assert viable ones come
    first and that a curated template wins a tie against a community one.
    """
    server, url, env = server_and_url
    search = _make_search(url, env)

    # Provide nodes that make community IPAdapter viable, but leave curated
    # templates non-viable by not providing their required nodes.
    available = {"IPAdapterApply", "IPAdapterModelLoader", "IPAdapterEncoder"}
    sel = TemplateSelector(discovery_provider=search)
    cands = sel.select(
        ShotIntent(intent="ipadapter"),
        available_nodes=available,
        top_k=20,
    )
    assert cands, "select() must return candidates"
    # Viable must come before non-viable.
    viable_indices = [i for i, c in enumerate(cands) if c.viable]
    nonviable_indices = [i for i, c in enumerate(cands) if not c.viable]
    if viable_indices and nonviable_indices:
        assert max(viable_indices) < min(nonviable_indices), (
            "Viable candidates must precede non-viable ones"
        )
    # In a tie on (viable, score), curated wins over community.
    viable_curated = [c for c in cands if c.viable and c.provenance == "curated"]
    viable_community = [c for c in cands if c.viable and c.provenance.startswith("community:")]
    if viable_curated and viable_community:
        for i, c in enumerate(cands):
            for j in range(i + 1, len(cands)):
                if (
                    c.viable == cands[j].viable
                    and c.score == cands[j].score
                ):
                    if c.provenance.startswith("community:") and cands[j].provenance == "curated":
                        pytest.fail(
                            "Curated should win ties over community, "
                            f"but {cands[j].template_id} (curated) comes after "
                            f"{c.template_id} (community)"
                        )


def test_provenance_distinguishes_curated_from_community(
    monkeypatch: pytest.MonkeyPatch, server_and_url
):
    """Assert the two provenance values are different and both appear in
    to_public_dict().
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    server, url, env = server_and_url
    search = _make_search(url, env)

    sel = TemplateSelector(discovery_provider=search)
    # Use available_nodes that match community templates so both sources appear.
    available = {"IPAdapterApply", "IPAdapterModelLoader", "IPAdapterEncoder"}
    cands = sel.select(
        ShotIntent(intent="ipadapter"),
        available_nodes=available,
        top_k=20,
    )
    provenances = {c.provenance for c in cands}
    # Curated must always be present (the registry is non-empty).
    assert "curated" in provenances, "No curated provenance found"
    # At least one community provenance must appear (the discoverer is called
    # because with the available_nodes only a few curated templates will be
    # viable).
    community_provs = {p for p in provenances if p.startswith("community:")}
    assert community_provs, "No community provenance found"
    # The two provenance values must be different strings.
    assert "curated" not in community_provs
    # Y la procedencia tiene que salir en la forma pública, no solo en el objeto:
    # un consumidor del API no debe perder el estado real/caché del índice.
    for cand in cands:
        public = cand.to_public_dict()
        assert "provenance" in public, "TemplateCandidate.to_public_dict() perdió provenance"
        assert public["provenance"] == cand.provenance


def test_public_dicts_of_the_discovery_module_expose_provenance(monkeypatch):
    """`DiscoveredTemplate` y `SearchResult` deben exponer su procedencia.

    Sin esto, un consumidor del API ve `source='community'` pero no puede
    distinguir un índice real y fresco de uno servido de caché.
    """
    from comfy_studio.orchestration.template_discovery import (
        DiscoveredTemplate,
        SearchResult,
    )

    tpl = DiscoveredTemplate(
        template_id="community:acme/ComfyUI-Thing",
        name="ComfyUI-Thing",
        category="",
        text="una cosa",
        required_nodes=["ThingApply"],
        source="community",
        provenance="community:cache",
    )
    public = tpl.to_public_dict()
    assert public["provenance"] == "community:cache"
    assert public["source"] == "community"

    result = SearchResult(template=tpl, relevance=0.5, coverage=1, matched_nodes=["ThingApply"])
    rpublic = result.to_public_dict()
    assert rpublic["provenance"] == "community:cache"
    # También por dentro, para quien ya recorría `template`.
    assert rpublic["template"]["provenance"] == "community:cache"


def test_public_dicts_of_the_discovery_module_expose_provenance_default(monkeypatch):
    """El valor por defecto sigue siendo estable: '' cuando no se sabe."""
    from comfy_studio.orchestration.template_discovery import DiscoveredTemplate

    tpl = DiscoveredTemplate(
        template_id="community:x/y", name="y", category="", text="t"
    )
    assert tpl.to_public_dict()["provenance"] == ""


def test_community_candidates_get_the_same_capability_cross_check(
    monkeypatch: pytest.MonkeyPatch, server_and_url
):
    """A community pack whose attributed nodes are NOT in available_nodes;
    assert it comes back non-viable with a reason naming the missing nodes
    (same treatment as curated).
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    server, url, env = server_and_url
    search = _make_search(url, env)

    # The real discoverer gets installed_nodes so it can attribute nodes;
    # select() passes an empty available_nodes so those attributed nodes
    # appear as missing, making the community candidate non-viable.
    class _RealSearchWithNodes:
        def __init__(self, search, installed_nodes):
            self._search = search
            self._installed_nodes = installed_nodes

        def discover(self, query, *, output_kind, limit, installed_nodes=None):
            # Always use the pre-configured installed_nodes for attribution.
            return self._search.search_templates(
                query, installed_nodes=self._installed_nodes, limit=limit
            )

    installed = {"IPAdapterApply", "IPAdapterModelLoader", "IPAdapterEncoder"}
    prov = _RealSearchWithNodes(search, installed)

    sel = TemplateSelector(discovery_provider=prov)
    # Empty available_nodes: every required node is missing.
    cands = sel.select(
        ShotIntent(intent="ipadapter"),
        available_nodes=set(),
        top_k=20,
    )
    community = [c for c in cands if c.provenance.startswith("community:")]
    assert community, "Expected community candidates"
    # Every community candidate must be non-viable with a missing-nodes reason.
    for c in community:
        assert c.viable is False, f"Community candidate {c.template_id} should be non-viable"
        assert c.missing_nodes, f"Missing nodes not recorded for {c.template_id}"
        assert "faltan nodos" in c.reason, (
            f"Reason should mention missing nodes for {c.template_id}: {c.reason!r}"
        )


def test_select_never_raises_when_index_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """STUDIO_DYNAMIC=1, discoverer pointed at a closed port; assert select()
    returns the curated results and does not raise.
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    closed_url = f"http://127.0.0.1:{closed_port}/custom-node-list.json"
    env = {"COMFY_DATA_DIR": str(tmp_path)}
    downloader = CommunityIndexDownloader(url=closed_url, env=env)
    search = CommunityTemplateSearch(downloader=downloader)

    sel = TemplateSelector(discovery_provider=search)
    # Should not raise even though the index is unreachable.
    cands = sel.select(
        ShotIntent(intent="video"),
        available_nodes=set(),
        top_k=10,
    )
    assert cands, "select() must return curated candidates when index is unavailable"
    assert all(c.provenance == "curated" for c in cands)


def test_second_call_reports_cached_provenance(
    monkeypatch: pytest.MonkeyPatch, server_and_url
):
    """Two select() calls; assert the second one's community candidates report
    'community:cache' (proving the cache state is visible).
    """
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    server, url, env = server_and_url
    search = _make_search(url, env)

    sel = TemplateSelector(discovery_provider=search)
    # First call: fetches from network (or cache if already present from other
    # tests). We only care that the second call shows cached provenance.
    sel.select(
        ShotIntent(intent="animation obscure query xyz"),
        available_nodes=set(),
        top_k=20,
    )
    # Small sleep to ensure mtime advances.
    time.sleep(0.05)
    # Second call: the downloader should use the cache.
    cands2 = sel.select(
        ShotIntent(intent="animation obscure query xyz"),
        available_nodes=set(),
        top_k=20,
    )
    community = [c for c in cands2 if c.provenance.startswith("community:")]
    assert community, "Expected community candidates on second call"
    assert all(c.provenance == "community:cache" for c in community), (
        f"Second call should report 'community:cache', got: "
        f"{[c.provenance for c in community]}"
    )
