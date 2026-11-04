"""Tests del estudio de anuncios (motor ad_studio, M7): schema, pipeline, rutas y tools.

Cubre el paquete brief -> guion -> storyboard -> variantes de formato:
  - composición determinista del guion y validación del brief;
  - persistencia del anuncio en project_meta y puerta de storyboard;
  - variantes simuladas (stub) con artefactos reales registrados como assets;
  - render real con ffmpeg cuando está disponible (skip si no hay ffmpeg);
  - fallos tipados (sin storyboard, aspect inválido, asset ausente, sin ffmpeg);
  - cancelación sticky del job entre variantes;
  - rutas HTTP (script/list/get/storyboard/variants 202 + wait=1) y tool-belt del agente.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.ads.pipeline import (
    AdCancelledError,
    AdPipelineError,
    attach_storyboard,
    create_ad_doc,
    get_ad_doc,
    list_ad_docs,
    run_ad_variants,
)
from comfy_studio.ads.schema import (
    AdBrief,
    AdStoryboard,
    build_storyboard,
    compose_script,
)
from comfy_studio.agent.tools import ToolContext, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.engines import EngineRunSpec, EngineTier, is_stub_mode
from comfy_studio.engines.ad_studio import AdStudioEngine
from comfy_studio.engines.registry import build_default_registry
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)

_BRIEF = {
    "product_name": "Cafetera Nova",
    "key_message": "El café perfecto cada mañana en 30 segundos.",
    "audience": "profesionales ocupados",
    "tone": "cercano",
    "target_duration_s": 12.0,
    "cta": "Pruébala hoy con un 20% de descuento.",
    "language": "es",
    "product_asset_ids": [],
}


def _brief(**overrides) -> AdBrief:
    return AdBrief.model_validate({**_BRIEF, **overrides})


def _store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(studio_data_dir=tmp_path / "studio_data")


def _project_with_image(store: ProjectStore) -> tuple[str, str]:
    project = store.create_project(name="Proyecto Anuncios")
    asset = store.save_upload(
        project["id"],
        filename="producto.png",
        content_type="image/png",
        data=_PNG_1PX,
        max_upload_bytes=2 * 1024 * 1024,
        category="uploads",
    )
    return project["id"], asset["id"]


# ---------------------------------------------------------------------------
# Schema y composición determinista
# ---------------------------------------------------------------------------


def test_compose_script_is_deterministic() -> None:
    s1 = compose_script(_brief())
    s2 = compose_script(_brief())
    assert s1.to_dict() == s2.to_dict()
    assert s1.generator == "template"
    assert [sc.role for sc in s1.scenes] == ["hook", "problema", "solucion", "cta"]
    assert abs(s1.total_duration_s - sum(sc.duration_s for sc in s1.scenes)) < 0.01
    assert all(sc.duration_s >= 2.0 for sc in s1.scenes)
    assert s1.cta == _BRIEF["cta"]


def test_brief_validation_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        AdBrief.model_validate({**_BRIEF, "product_name": "  "})
    with pytest.raises(ValueError):
        AdBrief.model_validate({**_BRIEF, "tone": "epico"})
    with pytest.raises(ValueError):
        AdBrief.model_validate({**_BRIEF, "target_duration_s": 999.0})


def test_build_storyboard_assigns_product_assets_round_robin() -> None:
    script = compose_script(_brief(product_asset_ids=["a1", "a2"]))
    sb = build_storyboard(script, ad_id="ad123")
    assert isinstance(sb, AdStoryboard)
    assert len(sb.shots) == len(script.scenes)
    assigned = [s.asset_id for s in sb.shots if s.asset_id]
    assert assigned == ["a1", "a2", "a1", "a2"]  # round-robin sobre los 4 planos
    assert sb.total_duration_s == script.total_duration_s
    # El storyboard sobrevive a serializar/validar (persistencia en project_meta).
    assert AdStoryboard.model_validate(sb.to_dict()).to_dict() == sb.to_dict()


# ---------------------------------------------------------------------------
# Pipeline: persistencia y variantes (stub)
# ---------------------------------------------------------------------------


def test_ad_doc_lifecycle_and_storyboard_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_id, asset_id = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=[asset_id]))
    assert doc["id"] and doc["script"]["schema"] == "comfy_studio.ad_script.v1"
    fetched = get_ad_doc(store, project_id, doc["id"])
    assert fetched is not None and fetched["id"] == doc["id"]
    assert [d["id"] for d in list_ad_docs(store, project_id)] == [doc["id"]]

    doc2 = attach_storyboard(store, project_id, doc["id"])
    sb = doc2["storyboard"]
    assert sb["schema"] == "comfy_studio.ad_storyboard.v1"
    assert len(sb["shots"]) == 4
    assert doc2["gate"]["state"] == "awaiting_approval"
    # Los planos con asset asignado apuntan al asset de producto real.
    assert all(
        store.get_asset(project_id, s["asset_id"]) is not None
        for s in sb["shots"]
        if s["asset_id"]
    )


def test_attach_storyboard_requires_existing_ad(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    with pytest.raises(AdPipelineError):
        attach_storyboard(store, project["id"], "noexiste")


def test_run_variants_stub_registers_assets_and_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_AD_STUDIO", "1")
    assert is_stub_mode("ad_studio") is True
    store = _store(tmp_path)
    broker = ProgressBroker()
    project_id, asset_id = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=[asset_id]))
    attach_storyboard(store, project_id, doc["id"])
    job = store.create_job(kind="ad_variants", project_id=project_id, params={"ad_id": doc["id"]})

    summary = run_ad_variants(
        store, project_id, doc["id"],
        broker=broker, job_id=job["id"],
        aspect_ratios=["9:16", "1:1", "16:9"],
        voiceover=True, music=True,
        simulated=True,
    )
    assert summary["variant_count"] == 3
    assert summary["simulated"] is True
    assert summary["all_qa_passed"] is True
    for variant in summary["variants"]:
        assert variant["simulated"] is True
        asset = store.get_asset(project_id, variant["asset_id"])
        assert asset is not None and asset["kind"] == "video"
        assert asset["category"] == "outputs"
        meta = asset["metadata"]
        assert meta["engine"] == "ad_studio" and meta["ad_id"] == doc["id"]
        assert meta["aspect_ratio"] == variant["aspect_ratio"]
        assert meta["simulated"] is True
    # Job terminal done y anuncio persistido con variantes.
    assert store.get_job(job["id"])["status"] == "done"
    doc_after = get_ad_doc(store, project_id, doc["id"])
    assert len(doc_after["variants"]) == 3
    # Eventos SSE publicados.
    events = [t for t, _ in broker.recent_events() if t == "ad"]
    assert events
    assert any(d.get("event") == "variant" for t, d in broker.recent_events() if t == "ad")


def test_run_variants_requires_storyboard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_id, _ = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief())
    with pytest.raises(AdPipelineError, match="storyboard"):
        run_ad_variants(store, project_id, doc["id"], simulated=True)


def test_run_variants_rejects_bad_params(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_id, asset_id = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=[asset_id]))
    attach_storyboard(store, project_id, doc["id"])
    with pytest.raises(AdPipelineError, match="[Aa]spect"):
        run_ad_variants(store, project_id, doc["id"], aspect_ratios=["3:2"], simulated=True)
    with pytest.raises(AdPipelineError, match="fps"):
        run_ad_variants(store, project_id, doc["id"], fps=999, simulated=True)
    # Sin assets en el brief, el fallback source_asset_id debe existir.
    doc_no_assets = create_ad_doc(store, project_id, _brief())
    attach_storyboard(store, project_id, doc_no_assets["id"])
    with pytest.raises(AdPipelineError, match="Asset no encontrado"):
        run_ad_variants(
            store, project_id, doc_no_assets["id"],
            source_asset_id="noexiste", simulated=True,
        )
    with pytest.raises(AdPipelineError, match="no tiene asset"):
        run_ad_variants(store, project_id, doc_no_assets["id"], simulated=True)


def test_run_variants_honours_sticky_cancel(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project_id, asset_id = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=[asset_id]))
    attach_storyboard(store, project_id, doc["id"])
    job = store.create_job(kind="ad_variants", project_id=project_id, params={"ad_id": doc["id"]})
    store.update_job(job["id"], status="cancelled", error="cancelado por el usuario")
    with pytest.raises(AdCancelledError):
        run_ad_variants(store, project_id, doc["id"], job_id=job["id"], simulated=True)
    # El job sigue cancelado (sticky): el pipeline no lo resucitó.
    assert store.get_job(job["id"])["status"] == "cancelled"


def test_run_variants_without_ffmpeg_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_AD_STUDIO", raising=False)
    monkeypatch.setenv("STUDIO_FFMPEG_BIN", "definitely-not-a-real-ffmpeg-xyz")
    monkeypatch.setenv("STUDIO_FFPROBE_BIN", "definitely-not-a-real-ffprobe-xyz")
    store = _store(tmp_path)
    project_id, asset_id = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=[asset_id]))
    attach_storyboard(store, project_id, doc["id"])
    with pytest.raises(AdPipelineError, match="ffmpeg"):
        run_ad_variants(store, project_id, doc["id"], simulated=False)


@pytest.mark.skipif(FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe requeridos")
def test_run_variants_real_ffmpeg_renders_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integración real (CPU): 2 planos de vídeo -> variante 16:9 con duración verificada."""
    monkeypatch.delenv("STUDIO_STUB_AD_STUDIO", raising=False)
    store = _store(tmp_path)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Ad Real")
    project_id = project["id"]
    clip_ids = []
    for i, color in enumerate(("red", "blue")):
        clip = tmp_path / f"clip{i}.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"color=c={color}:s=320x180:d=3:r=12", "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-preset", "ultrafast", str(clip)],
            check=True, capture_output=True, timeout=60,
        )
        asset = store.save_upload(
            project_id, filename=f"clip{i}.mp4", content_type="video/mp4",
            data=clip.read_bytes(), max_upload_bytes=64 * 1024 * 1024, category="uploads",
        )
        clip_ids.append(asset["id"])
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=clip_ids))
    doc = attach_storyboard(store, project_id, doc["id"])
    # Forzar un chunk por plano para montar los dos clips reales.
    sb = doc["storyboard"]
    for shot, cid in zip(sb["shots"], clip_ids * 2, strict=True):
        shot["asset_id"] = cid
    from comfy_studio.ads.pipeline import save_ad_doc
    save_ad_doc(store, project_id, doc)

    job = store.create_job(kind="ad_variants", project_id=project_id, params={"ad_id": doc["id"]})
    summary = run_ad_variants(
        store, project_id, doc["id"], broker=broker, job_id=job["id"],
        aspect_ratios=["16:9"], simulated=False,
    )
    assert summary["simulated"] is False
    assert summary["variant_count"] == 1
    variant = summary["variants"][0]
    assert variant["qa_passed"] is True
    out = store.get_asset_path(project_id, variant["asset_id"])
    assert out is not None and out.is_file() and out.stat().st_size > 0
    from comfy_studio.perception.keyframes import probe_media_info
    info = probe_media_info(out)
    assert info["probe_ok"] and info["has_video"]
    assert info["width"] == 1280 and info["height"] == 720
    assert abs(float(info["duration_sec"]) - summary["total_duration_s"]) < 2.0
    assert store.get_job(job["id"])["status"] == "done"


# ---------------------------------------------------------------------------
# Motor (contrato de 4 verbos)
# ---------------------------------------------------------------------------


def test_engine_contract_and_tier() -> None:
    engine = AdStudioEngine()
    assert engine.name == "ad_studio"
    assert engine.tier == EngineTier.WARM
    listed = engine.list()
    assert listed.ok and set(listed.payload["ops"]) == {"script", "storyboard", "variants"}
    assert engine.verify("script").ok is True
    assert engine.verify("nope").ok is False
    installed = engine.install("anything")
    assert installed.ok is False and "manifiesto" in (installed.error or "")
    bad = engine.run(EngineRunSpec(job={"op": "nope"}))
    assert bad.ok is False and "op" in (bad.error or "")


def test_engine_script_op_pure_and_persisted(tmp_path: Path) -> None:
    engine = AdStudioEngine()
    res = engine.run(EngineRunSpec(job={
        "op": "script", "brief": _BRIEF, "output_path": str(tmp_path / "script.json"),
    }))
    assert res.ok and res.payload["script"]["generator"] == "template"
    assert Path(res.payload["output"]).is_file()

    store = _store(tmp_path)
    project = store.create_project(name="P")
    engine2 = AdStudioEngine(store=store, broker=ProgressBroker(), config=StudioConfig.from_env())
    res2 = engine2.run(EngineRunSpec(job={
        "op": "script", "brief": _BRIEF, "project_id": project["id"],
    }))
    assert res2.ok and res2.payload["ad_id"]
    assert get_ad_doc(store, project["id"], res2.payload["ad_id"]) is not None

    sb = engine2.run(EngineRunSpec(job={
        "op": "storyboard", "project_id": project["id"], "ad_id": res2.payload["ad_id"],
    }))
    assert sb.ok and len(sb.payload["storyboard"]["shots"]) == 4

    no_store = engine.run(EngineRunSpec(job={
        "op": "variants", "project_id": "x", "ad_id": "y",
    }))
    assert no_store.ok is False and "store" in (no_store.error or "")


def test_engine_variants_op_via_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_AD_STUDIO", "1")
    store = _store(tmp_path)
    broker = ProgressBroker()
    project_id, asset_id = _project_with_image(store)
    doc = create_ad_doc(store, project_id, _brief(product_asset_ids=[asset_id]))
    attach_storyboard(store, project_id, doc["id"])
    engine = AdStudioEngine(store=store, broker=broker)
    res = engine.run(EngineRunSpec(job={
        "op": "variants", "project_id": project_id, "ad_id": doc["id"],
        "aspect_ratios": ["9:16"],
    }))
    assert res.ok and res.simulated is True
    assert res.payload["variant_count"] == 1


def test_registry_includes_ad_studio() -> None:
    reg = build_default_registry()
    entry = reg.get("ad_studio")
    assert entry is not None and entry.wired is True
    assert entry.tier == EngineTier.WARM


# ---------------------------------------------------------------------------
# Rutas HTTP
# ---------------------------------------------------------------------------


def _new_project(client: TestClient, name: str = "Proyecto Ads") -> str:
    resp = client.post("/studio/api/projects", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload_png(client: TestClient, project_id: str) -> str:
    resp = client.post(
        f"/studio/api/projects/{project_id}/media",
        files={"file": ("producto.png", _PNG_1PX, "image/png")},
        data={"category": "uploads"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_routes_script_list_get_storyboard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_AD_STUDIO", "1")
    project_id = _new_project(client)
    asset_id = _upload_png(client, project_id)

    resp = client.post(
        f"/studio/api/projects/{project_id}/ads/script",
        json={**_BRIEF, "product_asset_ids": [asset_id]},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    ad_id = doc["id"]
    assert doc["script"]["scenes"] and doc["brief"]["product_name"] == "Cafetera Nova"

    listing = client.get(f"/studio/api/projects/{project_id}/ads").json()
    assert listing["count"] == 1 and listing["ads"][0]["id"] == ad_id
    got = client.get(f"/studio/api/projects/{project_id}/ads/{ad_id}")
    assert got.status_code == 200 and got.json()["id"] == ad_id
    assert client.get(f"/studio/api/projects/{project_id}/ads/nope").status_code == 404

    sb = client.post(f"/studio/api/projects/{project_id}/ads/{ad_id}/storyboard")
    assert sb.status_code == 200, sb.text
    assert len(sb.json()["storyboard"]["shots"]) == 4
    assert sb.json()["gate"]["state"] == "awaiting_approval"

    # Validación de entrada: brief inválido -> 422; proyecto inexistente -> 404.
    bad = client.post(
        f"/studio/api/projects/{project_id}/ads/script",
        json={**_BRIEF, "product_name": ""},
    )
    assert bad.status_code == 422
    assert client.post(
        "/studio/api/projects/nope/ads/script", json=_BRIEF
    ).status_code == 404


def test_routes_variants_wait_and_async(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_AD_STUDIO", "1")
    project_id = _new_project(client, "Proyecto Ads Variants")
    asset_id = _upload_png(client, project_id)
    doc = client.post(
        f"/studio/api/projects/{project_id}/ads/script",
        json={**_BRIEF, "product_asset_ids": [asset_id]},
    ).json()
    ad_id = doc["id"]
    client.post(f"/studio/api/projects/{project_id}/ads/{ad_id}/storyboard")

    # wait=1: respuesta síncrona con el resumen.
    resp = client.post(
        f"/studio/api/projects/{project_id}/ads/{ad_id}/variants?wait=1",
        json={"aspect_ratios": ["9:16", "16:9"], "voiceover": True},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["ad_id"] == ad_id
    assert summary["variant_count"] == 2
    assert summary["simulated"] is True
    assert all(v["qa_passed"] for v in summary["variants"])

    # async: 202 + job_id, y el job termina en done en segundo plano.
    resp2 = client.post(
        f"/studio/api/projects/{project_id}/ads/{ad_id}/variants",
        json={"aspect_ratios": ["1:1"]},
    )
    assert resp2.status_code == 202, resp2.text
    body = resp2.json()
    assert body["async"] is True and body["ad_id"] == ad_id
    deadline = time.monotonic() + 15.0
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/studio/api/jobs/{body['job_id']}").json()
        if job["status"] in {"done", "failed", "cancelled", "error"}:
            break
        time.sleep(0.1)
    assert job is not None and job["status"] == "done", job
    assert job["kind"] == "ad_variants"

    # Variantes pedidas sin storyboard -> 400 tipado; anuncio inexistente -> 404.
    doc2 = client.post(
        f"/studio/api/projects/{project_id}/ads/script", json=_BRIEF
    ).json()
    resp3 = client.post(
        f"/studio/api/projects/{project_id}/ads/{doc2['id']}/variants?wait=1",
        json={"aspect_ratios": ["16:9"]},
    )
    assert resp3.status_code == 400 and "storyboard" in resp3.json()["detail"]
    resp4 = client.post(
        f"/studio/api/projects/{project_id}/ads/nope/variants?wait=1", json={}
    )
    assert resp4.status_code == 404
    # aspect ratio inválido -> 422 antes de crear job.
    resp5 = client.post(
        f"/studio/api/projects/{project_id}/ads/{ad_id}/variants?wait=1",
        json={"aspect_ratios": ["3:2"]},
    )
    assert resp5.status_code == 422


# ---------------------------------------------------------------------------
# Tool-belt del agente
# ---------------------------------------------------------------------------


def _tool_context(tmp_path: Path) -> ToolContext:
    cfg = StudioConfig.from_env()
    store = _store(tmp_path)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Tools Ads")
    comfy_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url, transport=None, store=store,
        broker=broker, redactor=cfg.redactor(),
    )
    return ToolContext(
        project_id=project["id"], store=store, comfyui_client=comfy_client,
        broker=broker, config=cfg,
    )


@pytest.mark.anyio
async def test_agent_tools_ad_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_AD_STUDIO", "1")
    ctx = _tool_context(tmp_path)
    asset = ctx.store.save_upload(
        ctx.project_id, filename="producto.png", content_type="image/png",
        data=_PNG_1PX, max_upload_bytes=2 * 1024 * 1024, category="uploads",
    )
    belt = create_default_tool_belt()
    assert {"ad_script", "ad_storyboard", "build_ad_variants"}.issubset(set(belt.list_names()))

    created = await belt.execute("ad_script", {**_BRIEF, "product_asset_ids": [asset["id"]]}, ctx)
    assert created["status"] == "scripted"
    ad_id = created["ad_id"]

    sb = await belt.execute("ad_storyboard", {"ad_id": ad_id}, ctx)
    assert sb["status"] == "storyboarded" and len(sb["storyboard"]["shots"]) == 4

    variants = await belt.execute(
        "build_ad_variants",
        {"ad_id": ad_id, "aspect_ratios": ["9:16", "1:1"], "voiceover": True},
        ctx,
    )
    assert variants["status"] == "variants_built"
    assert variants["variant_count"] == 2
    assert variants["simulated"] is True
    job = ctx.store.get_job(variants["job_id"])
    assert job is not None and job["status"] == "done"

    # Error tipado: anuncio inexistente.
    from comfy_studio.agent.tools import ToolExecutionError
    with pytest.raises(ToolExecutionError):
        await belt.execute("build_ad_variants", {"ad_id": "nope"}, ctx)
