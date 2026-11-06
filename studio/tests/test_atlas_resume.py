"""PR-1 atlas-resume-duro: 429/403, circuito y reanudación por caché sin red."""

from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest
from test_atlas_acquire import (
    FakeOpener,
    FakeResponse,
    commons_api_payload,
    content_for,
)

from comfy_studio.atlas.acquire.http import (
    CircuitOpenError,
    ForbiddenError,
    HttpClient,
    RateLimitedError,
)
from comfy_studio.atlas.acquire.pipeline import AcquisitionPipeline
from comfy_studio.atlas.service import AtlasService


def _client(op: FakeOpener, **kw) -> HttpClient:
    args = {"min_interval_s": 0.0, "backoff_base_s": 0.0, "timeout_s": 5.0}
    args.update(kw)
    c = HttpClient("test-agent/1.0", ["example.org"], **args)  # type: ignore[arg-type]
    c._opener = op  # type: ignore[attr-defined]
    return c


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "slow down", {"Retry-After": "0"}, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HttpClient: 429 con Retry-After, 403 sin reintento, circuito
# ---------------------------------------------------------------------------


def test_429_con_retry_after_cero_se_recupera() -> None:
    op = FakeOpener()
    calls = {"n": 0}

    def flaky(req) -> FakeResponse:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(req.full_url, 429)
        return FakeResponse(b'{"query":{}}')

    op.routes["https://example.org/x"] = flaky
    c = _client(op, max_retries=2, circuit_failures=10)
    assert c.request("GET", "https://example.org/x") == b'{"query":{}}'
    assert calls["n"] == 2


def test_circuito_se_abre_y_no_golpea_red() -> None:
    op = FakeOpener()
    op.routes["https://example.org/x"] = _http_error("https://example.org/x", 429)
    c = _client(op, max_retries=0, circuit_failures=2, circuit_cooldown_s=3600.0)
    with pytest.raises(RateLimitedError):
        c.request("GET", "https://example.org/x")
    with pytest.raises(RateLimitedError):
        c.request("GET", "https://example.org/x")
    with pytest.raises(CircuitOpenError):
        c.request("GET", "https://example.org/x")
    assert len(op.requests) == 2, "abierto el circuito no debe salir a red"


def test_403_no_reintenta_y_abre_circuito() -> None:
    op = FakeOpener()
    op.routes["https://example.org/x"] = _http_error("https://example.org/x", 403)
    c = _client(op, max_retries=4, circuit_failures=1, circuit_cooldown_s=3600.0)
    with pytest.raises(ForbiddenError):
        c.request("GET", "https://example.org/x")
    assert len(op.requests) == 1, "un 403 no se reintenta"
    with pytest.raises(CircuitOpenError):
        c.request("GET", "https://example.org/x")


# ---------------------------------------------------------------------------
# Pipeline: un outage 429 no borra inventario y lo pendiente se reanuda
# ---------------------------------------------------------------------------

NEW_TITLE = "File:Puente 4 Sur foto D.jpg"


def _working_pipe(root: Path) -> AcquisitionPipeline:
    pipe = AcquisitionPipeline(root, max_refs=10, dry_run=False, min_interval_s=0.0)
    op = FakeOpener()

    def api_handler(req) -> FakeResponse:  # noqa: ANN001
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(req.full_url).query)
        flat = {k: v[0] for k, v in q.items()}
        return FakeResponse(commons_api_payload(flat))

    def upload_handler(req) -> FakeResponse:  # noqa: ANN001
        title = req.full_url.rsplit("/", 1)[-1].replace("_", " ")
        if title.startswith("640px-"):
            title = title[len("640px-"):]
        if not title.startswith("File:"):
            title = "File:" + title
        return FakeResponse(content_for(title))

    op.routes["https://commons.wikimedia.org/w/api.php*"] = api_handler
    op.routes["https://upload.wikimedia.org/fake/*"] = upload_handler
    op.routes["https://thumb.wikimedia.org/fake/*"] = upload_handler
    pipe.http._opener = op  # type: ignore[attr-defined]
    pipe.ensure_seed_places()
    return pipe


def _inject_pending_candidate(root: Path, pipe: AcquisitionPipeline) -> bytes:
    """Añade un tercer candidato SOLO a las cachés (sin tocar la API)."""
    candidates_path = root / ".cache" / "candidates.json"
    infos_path = root / ".cache" / "infos.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    infos = json.loads(infos_path.read_text(encoding="utf-8"))
    base = next(v for v in infos.values() if "foto A" in v["title"])
    content = content_for(NEW_TITLE)
    candidates.append({"title": NEW_TITLE, "lat": 6.2090, "lon": -75.5672})
    infos[NEW_TITLE] = {
        **base,
        "title": NEW_TITLE,
        "url": "https://upload.wikimedia.org/fake/" + NEW_TITLE.replace(" ", "_"),
        "size": len(content),
        "sha1": hashlib.sha1(content).hexdigest(),
    }
    candidates_path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    infos_path.write_text(json.dumps(infos, ensure_ascii=False), encoding="utf-8")
    return content


def test_outage_429_conserva_inventario_y_reanuda(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    pipe = _working_pipe(root)
    phase1 = pipe.run_commons()
    assert phase1["ok"] and phase1["downloaded"] == 2, phase1
    svc = AtlasService(root)
    refs_before = {r.ref_id: r.sha256 for r in svc.store.all_references(usable_only=True)}
    assert len(refs_before) == 2
    files_before = {
        r.file_path: (root / "files" / r.file_path).read_bytes()
        for r in svc.store.all_references(usable_only=True)
    }
    svc.store.close()

    # fase 2: outage total (toda la red devuelve 429) + un candidato pendiente
    _inject_pending_candidate(root, pipe)
    outage = FakeOpener()
    outage.routes["https://commons.wikimedia.org/*"] = _http_error("api", 429)
    outage.routes["https://upload.wikimedia.org/*"] = _http_error("up", 429)
    outage.routes["https://thumb.wikimedia.org/*"] = _http_error("th", 429)
    pipe.http._opener = outage  # type: ignore[attr-defined]
    pipe.http.max_retries = 0
    pipe.http.backoff_base_s = 0.0
    pipe.http.min_interval_s = 0.0
    phase2 = pipe.run_commons()
    assert phase2["ok"] is True, phase2
    assert phase2["downloaded"] == 0
    assert phase2["skipped_resume"] == 2
    assert pipe.svc.store.acq_item_status("main", "commons", NEW_TITLE) == "failed"
    assert not any(u.startswith("https://commons.wikimedia.org/w/api.php")
                   for u, _ in outage.requests), "con cachés no debe llamar a la API"
    assert any(u.startswith("https://upload.wikimedia.org/") for u, _ in outage.requests)
    # inventario intacto: mismas refs, mismos bytes, cobertura honesta intacta
    svc2 = AtlasService(root)
    refs_after = {r.ref_id: r.sha256 for r in svc2.store.all_references(usable_only=True)}
    assert refs_after == refs_before
    for rel, data in files_before.items():
        assert (root / "files" / rel).read_bytes() == data
    cov = svc2.store.get_coverage("plc_medellin_puente_de_la_4_sur")
    assert cov is not None and cov.views_observed == 2
    svc2.store.close()

    # fase 3: vuelve la red solo para el pendiente -> se descarga y colapsa nada
    pipe.http._opener = _working_pipe_urls_only_upload()  # type: ignore[attr-defined]
    phase3 = pipe.run_commons()
    assert phase3["ok"] is True, phase3
    assert phase3["downloaded"] == 1
    assert phase3["skipped_resume"] == 2
    svc3 = AtlasService(root)
    assert len(svc3.store.all_references(usable_only=True)) == 3
    assert svc3.store.get_coverage("plc_medellin_puente_de_la_4_sur").views_observed == 3  # type: ignore[union-attr]
    svc3.store.close()


def _working_pipe_urls_only_upload() -> FakeOpener:
    op = FakeOpener()

    def upload_handler(req) -> FakeResponse:  # noqa: ANN001
        title = req.full_url.rsplit("/", 1)[-1].replace("_", " ")
        if not title.startswith("File:"):
            title = "File:" + title
        return FakeResponse(content_for(title))

    op.routes["https://upload.wikimedia.org/fake/*"] = upload_handler
    op.routes["https://thumb.wikimedia.org/fake/*"] = upload_handler
    return op
