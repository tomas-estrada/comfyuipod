"""Tests de aceptación M7 (Worker 7): brief creativo persistente + comprensión
multimodal con evidencia + dirección editorial + revisión sobre el render +
paquete de entrega, integrados en API y tool-belt del agente.

Cubren los escenarios del spec: prompt ambiguo con un video, brief específico con
restricciones, evento con datos exactos que deben conservarse, pieza sin diálogo,
cien assets mixtos con duplicados e inválidos, interrupción/reanudación, ausencia
de datos de tendencias y edición localizada que solo toca lo pedido.

Son deterministas y NO exigen ffmpeg (el pod de CI sí lo tiene; el test de
render verificado se salta si no hay binario).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.agent.tools import ToolContext, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.creative.brief import _assess_commercial_claims
from comfy_studio.perception.store import PerceptionVectorStore
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512
FAKE_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02\x08\x02\x00\x00\x00"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_project(client: TestClient, name: str = "Proyecto M7") -> str:
    resp = client.post("/studio/api/projects", json={"name": name})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _upload(client: TestClient, pid: str, filename: str, data: bytes, mime: str) -> dict:
    resp = client.post(
        f"/studio/api/projects/{pid}/assets",
        files={"file": (filename, data, mime)},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _create_brief(client: TestClient, pid: str, prompt: str, **extra) -> dict:
    resp = client.post(f"/studio/api/projects/{pid}/brief", json={"prompt": prompt, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Un video + prompt ambiguo
# ---------------------------------------------------------------------------


def test_ambiguous_prompt_single_video_builds_brief_with_visible_assumptions(
    client: TestClient,
) -> None:
    pid = _make_project(client, "Reel del carro")
    asset = _upload(client, pid, "carro.mp4", FAKE_MP4, "video/mp4")

    row = _create_brief(client, pid, "hazme un reel brutal del carro")
    assert row["version"] == 1
    assert row["status"] == "draft"
    brief = row["brief"]
    assert brief["schema"] == "comfy_studio.brief.v1"
    assert brief["piece_type"] == "reel"
    assert brief["tone"] == "impactante"
    assert brief["assumptions"], "los supuestos deben quedar visibles ante prompt ambiguo"
    assert brief["asset_facts"], "debe haber hechos del material con evidencia"
    assert all("asset_id" in f and "method" in f and "simulated" in f for f in brief["asset_facts"])
    assert any(f["asset_id"] == asset["id"] for f in brief["asset_facts"])
    editorial = brief["editorial"]
    assert editorial["promise"]
    assert editorial["hook"]["description"]
    assert isinstance(editorial["structure"], list)
    assert editorial["material"]["use"], "selecciona material por intención, no por cantidad"
    assert any(
        ac["id"] == "ac-no-invented-facts" for ac in brief["acceptance_criteria"]
    )
    # GET del brief activo devuelve lo mismo + understanding persistido
    got = client.get(f"/studio/api/projects/{pid}/brief")
    assert got.status_code == 200
    payload = got.json()
    assert payload["brief"]["version"] == 1
    assert payload["understanding"]["schema"] == "comfy_studio.understanding.v1"
    assert payload["understanding"]["counts"]["assets"] == 1


# ---------------------------------------------------------------------------
# 2. Brief específico con restricciones (duración, tono, música, texto, formato)
# ---------------------------------------------------------------------------


def test_specific_brief_parses_constraints_into_user_constraints_and_acceptance(
    client: TestClient,
) -> None:
    pid = _make_project(client, "Brief específico")
    _upload(client, pid, "entrevista.mp4", FAKE_MP4, "video/mp4")

    prompt = (
        "quiero un video de 15 segundos, 9:16, sin música, con subtítulos, "
        "en inglés, para TikTok, tono épico"
    )
    row = _create_brief(client, pid, prompt)
    brief = row["brief"]

    by_key = {c["key"]: c["value"] for c in brief["user_constraints"]}
    assert by_key["duration_sec"] == 15.0
    assert by_key["aspect_ratio"] == "9:16"
    assert by_key["music"] is False
    assert by_key["subtitles"] is True
    assert by_key["language"] == "en"
    assert by_key["platform"] == "tiktok"
    assert all(c["source"] == "user" for c in brief["user_constraints"])

    assert brief["duration"]["target_sec"] == 15.0
    assert brief["aspect_ratio"] == "9:16"
    assert brief["audience"]["platform"] == "tiktok"
    assert brief["audience"]["language"] == "en"
    assert brief["audience"]["is_hypothesis"] is True
    assert brief["tone"] == "épico"

    ac_ids = {ac["id"] for ac in brief["acceptance_criteria"]}
    assert {"ac-duration", "ac-aspect", "ac-subtitles", "ac-no-music", "ac-language"} <= ac_ids

    audio_plan = brief["editorial"]["audio_plan"]
    assert "sin música" in audio_plan["music"]
    assert audio_plan["music_policy"] == "prohibited"
    assert audio_plan["subtitles_policy"] == "required"
    assert audio_plan["subtitles"] == "subtítulos obligatorios"


# ---------------------------------------------------------------------------
# 3. Evento: los datos exactos del material se conservan verbatim (no se inventan)
# ---------------------------------------------------------------------------


def test_event_facts_preserved_verbatim_with_evidence_and_simulated_flag(
    client: TestClient,
) -> None:
    pid = _make_project(client, "Evento lanzamiento")
    asset = _upload(client, pid, "evento.mp4", FAKE_MP4, "video/mp4")

    exact = "El evento es el 12 de octubre de 2026 en Madrid, entrada 20:00"
    tr_resp = client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={
            "asset_id": asset["id"],
            "language": "es",
            "script_segments": [
                {"start": 0.0, "end": 2.0, "text": exact, "speaker": "SPEAKER_00"},
                {"start": 2.0, "end": 4.0, "text": "Habrá show en vivo", "speaker": "SPEAKER_01"},
            ],
        },
    )
    assert tr_resp.status_code == 200, tr_resp.text
    transcription_id = tr_resp.json()["transcription"]["id"]

    row = _create_brief(client, pid, "anuncia el evento con los datos exactos")
    brief = row["brief"]
    assert brief["piece_type"] == "event"

    claims = [f["claim"] for f in brief["asset_facts"] if f["kind"] == "transcript_segment"]
    assert any(exact in c for c in claims), "la frase exacta debe conservarse verbatim"
    fact = next(
        f for f in brief["asset_facts"]
        if f["kind"] == "transcript_segment" and exact in f["claim"]
    )
    assert fact["evidence"]["transcription_id"] == transcription_id
    assert fact["evidence"]["speaker"] == "SPEAKER_00"
    assert fact["interval_sec"] == [0.0, 2.0]
    assert fact["simulated"] is True, "whisperx simulado debe marcarse como tal"
    assert fact["method"] == "whisperx"

    # Nada inventado: la única fecha del brief proviene de la transcripción real
    import re

    dates = re.findall(r"\d{1,2} de [a-z]+ de \d{4}", str(brief["asset_facts"]))
    assert dates == ["12 de octubre de 2026"]


def test_false_commercial_claims_remain_unverified_and_out_of_plan(
    client: TestClient,
) -> None:
    pid = _make_project(client, "Evento con datos por verificar")
    asset = _upload(client, pid, "evento.mp4", FAKE_MP4, "video/mp4")
    transcript = "El evento será el 12 de octubre de 2026 y la entrada cuesta $25.000 COP"
    tr_resp = client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={
            "asset_id": asset["id"],
            "language": "es",
            "script_segments": [
                {"start": 0.0, "end": 3.0, "text": transcript, "speaker": "S1"}
            ],
        },
    )
    assert tr_resp.status_code == 200, tr_resp.text

    row = _create_brief(
        client,
        pid,
        "Anuncia el evento el 10 de octubre de 2025 con entrada a $20.000 COP",
    )
    brief = row["brief"]
    claims = {claim["kind"]: claim for claim in brief["claim_assessments"]}
    assert claims["date"]["claim"] == "10 de octubre de 2025"
    assert claims["price"]["claim"] == "$20.000"
    # La transcripción de fixture está marcada como simulada y no puede
    # confirmar ni refutar una fecha o un precio como hechos.
    assert {claim["status"] for claim in claims.values()} == {"unverified"}
    assert all(not claim["evidence_refs"] for claim in claims.values())
    assert len(brief["pending"]) == 2
    assert all("no usar como hecho" in item["question"] for item in brief["pending"])
    assert "2025" not in brief["main_message"]
    assert "20.000" not in brief["main_message"]
    assert "2025" not in " ".join(brief["keywords"])
    assert "20.000" not in " ".join(brief["keywords"])
    assert "2025" not in str(brief["editorial"])
    assert "20.000" not in str(brief["editorial"])


def test_real_transcript_claims_are_referenced_or_marked_conflicting() -> None:
    facts = [
        {
            "id": "fact-0007",
            "kind": "transcript_segment",
            "simulated": False,
            "claim": "Dice: «El evento será el 12 de octubre de 2026; entrada $25.000 COP»",
        }
    ]
    supported = _assess_commercial_claims(
        "El evento será el 12 de octubre de 2026 y cuesta $25.000 COP", facts
    )
    assert [item["status"] for item in supported] == ["supported", "supported"]
    assert all(item["evidence_refs"] == ["fact-0007"] for item in supported)

    conflicting = _assess_commercial_claims(
        "El evento será el 10 de octubre de 2025 y cuesta $20.000 COP", facts
    )
    assert [item["status"] for item in conflicting] == ["conflicting", "conflicting"]
    assert all(item["evidence_refs"] == ["fact-0007"] for item in conflicting)


# ---------------------------------------------------------------------------
# 4. Pieza sin diálogo (solo imágenes)
# ---------------------------------------------------------------------------


def test_no_dialogue_piece_builds_from_visual_evidence_only(client: TestClient) -> None:
    pid = _make_project(client, "Ambiente sin diálogo")
    for i in range(3):
        _upload(client, pid, f"foto{i}.png", FAKE_PNG + bytes([i]), "image/png")

    row = _create_brief(client, pid, "crea una pieza de ambiente con estas fotos")
    brief = row["brief"]
    assert brief["piece_type"] == "ambient"
    audio_plan = brief["editorial"]["audio_plan"]
    assert "sin diálogo" in audio_plan["dialogue"]
    assert audio_plan["subtitles"] == "sin subtítulos"
    kinds = {f["kind"] for f in brief["asset_facts"]}
    assert "media_metadata" in kinds
    assert "transcript_segment" not in kinds
    # la estructura cae al fallback por assets en orden
    assert brief["editorial"]["structure"]


# ---------------------------------------------------------------------------
# 5. Cien assets mixtos con duplicados y alguno inválido
# ---------------------------------------------------------------------------


def test_hundred_mixed_assets_with_duplicates_and_invalid_file(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    pid = _make_project(client, "Cien assets")
    store = ProjectStore(studio_data_dir)

    uploaded = []
    for i in range(98):
        data = FAKE_PNG if i % 3 else FAKE_PNG + b"dup"  # duplicados por contenido
        a = store.save_upload(
            pid,
            filename=f"foto{i}.png",
            content_type="image/png",
            data=data,
            max_upload_bytes=2 * 1024 * 1024,
        )
        uploaded.append(a)
    video = store.save_upload(
        pid, filename="entrevista.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024,
    )
    uploaded.append(video)
    # 100º asset: otro mp4
    video2 = store.save_upload(
        pid, filename="broll.mp4", content_type="video/mp4",
        data=FAKE_MP4 + b"x", max_upload_bytes=2 * 1024 * 1024,
    )
    uploaded.append(video2)
    assert len(store.list_assets(pid)) == 100

    # archivo inválido vía API: MIME no soportado → 415, el proyecto sigue vivo
    bad = client.post(
        f"/studio/api/projects/{pid}/assets",
        files={"file": ("notes.txt", b"hola", "text/plain")},
    )
    assert bad.status_code == 415

    # un asset queda con archivo físico ausente → invalid, no rompe nada
    victim = uploaded[5]
    path = store.get_asset_path(pid, victim["id"])
    assert path is not None and path.is_file()
    path.unlink()

    und_resp = client.post(
        f"/studio/api/projects/{pid}/brief/understand", json={"prompt": "reel del evento"}
    )
    assert und_resp.status_code == 200, und_resp.text
    und = und_resp.json()["understanding"]
    assert und["counts"]["assets"] == 100
    assert und["counts"]["duplicates"] >= 1, "los duplicados por hash deben detectarse"
    assert any(d["asset_id"] == victim["id"] for d in und["invalid"])
    assert und["limitations"], "las limitaciones deben declararse"

    row = _create_brief(client, pid, "hazme un reel del evento con todo este material")
    brief = row["brief"]
    # la cantidad de archivos no determina la cantidad usada: hay skip con motivo
    skipped = brief["editorial"]["material"]["skip"]
    assert skipped and all("reason" in s for s in skipped)
    dup_skip = [s for s in skipped if "duplicado" in s["reason"]]
    assert dup_skip, "los duplicados exactos deben excluirse con motivo"


# ---------------------------------------------------------------------------
# 6. Interrupción y reanudación (persistencia tras reinicio del pod)
# ---------------------------------------------------------------------------


def test_brief_survives_store_reinstantiation_and_versions_append(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    pid = _make_project(client, "Persistencia")
    _upload(client, pid, "a.mp4", FAKE_MP4, "video/mp4")
    row1 = _create_brief(client, pid, "haz un anuncio para este producto")
    assert row1["version"] == 1

    # "reinicio": nueva instancia del store sobre el mismo data dir
    store2 = ProjectStore(studio_data_dir)
    latest = store2.get_latest_creative_brief(pid)
    assert latest is not None and latest["version"] == 1
    assert latest["brief"]["piece_type"] == "product"

    # edición del usuario → nueva versión con traza
    patch = client.patch(
        f"/studio/api/projects/{pid}/brief",
        json={"changes": {"tone": "más íntimo", "status": "proposed"}},
    )
    assert patch.status_code == 200, patch.text
    row2 = patch.json()
    assert row2["version"] == 2
    assert row2["status"] == "proposed"
    assert row2["brief"]["tone"] == "más íntimo"
    assert row2["brief"]["piece_type"] == "product", "lo no editado se conserva"
    assert row2["changes"][-1]["field"] == "status"
    assert all(c["source"] == "user" for c in row2["changes"])

    # aprobación humana de la v2 → v3 aprobada
    appr = client.post(f"/studio/api/projects/{pid}/brief/2/approve")
    assert appr.status_code == 200, appr.text
    assert appr.json()["version"] == 3
    assert appr.json()["status"] == "approved"
    assert appr.json()["brief"]["status"] == "approved"
    assert appr.json()["brief"]["tone"] == "más íntimo"

    versions = client.get(f"/studio/api/projects/{pid}/briefs").json()
    assert [v["version"] for v in versions["briefs"]] == [3, 2, 1]

    # campo no editable → 400
    bad = client.patch(
        f"/studio/api/projects/{pid}/brief", json={"changes": {"asset_facts": []}}
    )
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# 7. Edición localizada: solo cambia lo pedido
# ---------------------------------------------------------------------------


def test_localized_edit_only_touches_requested_field(client: TestClient) -> None:
    pid = _make_project(client, "Edición localizada")
    _upload(client, pid, "clip.mp4", FAKE_MP4, "video/mp4")
    row = _create_brief(
        client, pid, "video de 10 segundos 9:16 con subtítulos para instagram, tono épico"
    )
    before = row["brief"]

    resp = client.patch(
        f"/studio/api/projects/{pid}/brief",
        json={"changes": {"editorial": {
            **before["editorial"], "promise": "menos publicidad, más emoción",
        }}},
    )
    assert resp.status_code == 200, resp.text
    after = resp.json()["brief"]
    assert after["editorial"]["promise"] == "menos publicidad, más emoción"
    assert after["user_constraints"] == before["user_constraints"]
    assert after["duration"] == before["duration"]
    assert after["aspect_ratio"] == before["aspect_ratio"]
    assert len(resp.json()["changes"]) == 1
    assert resp.json()["changes"][0]["field"] == "editorial"


# ---------------------------------------------------------------------------
# 8. Revisión del render real (técnica vs editorial separadas)
# ---------------------------------------------------------------------------


def test_review_output_separates_technical_from_editorial_and_localizes_corrections(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    pid = _make_project(client, "Revisión")
    store = ProjectStore(studio_data_dir)
    src = _upload(client, pid, "fuente.mp4", FAKE_MP4, "video/mp4")
    unrelated = _upload(client, pid, "otro.mp4", FAKE_MP4, "video/mp4")
    out = store.save_upload(
        pid, filename="render.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024, category="outputs",
    )
    store.save_edl_version(
        pid,
        edl={"schema": "comfy_studio.edl.v1", "cuts": [{"asset_id": src["id"]}]},
        output_asset_id=out["id"],
    )
    _create_brief(client, pid, "video de 15 segundos con subtítulos del material")

    resp = client.post(f"/studio/api/projects/{pid}/brief/review", json={"asset_id": out["id"]})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["schema"] == "comfy_studio.review.v1"
    assert report["asset_id"] == out["id"]
    # mediciones técnicas presentes y honestas
    assert "probe_ok" in report["technical"]
    assert report["summary"]["measured"] == report["technical"]["probe_ok"]
    # la evaluación editorial NO se inventa: queda pendiente de humano/VLM
    pending = [c for c in report["editorial_checks"] if c["status"] == "pending"]
    assert pending and all(c["requires"] == "human_or_vlm" for c in pending)
    claims_check = next(c for c in pending if c["check"] == "afirmaciones inventadas")
    assert "no compara" in claims_check["note"]
    # cada issue trae corrección localizada
    assert all(i.get("correction") for i in report["issues"])
    # sin ffprobe en este entorno el archivo no es verificable → error de integridad
    if shutil.which("ffprobe") is None:
        assert report["summary"]["passed"] is False
        assert any(i["check"] == "integridad" for i in report["issues"])
    # el informe queda persistido (reanudable)
    saved = store.get_meta(pid, f"creative_review:{out['id']}")
    assert isinstance(saved, dict) and saved["asset_id"] == out["id"]

    # asset inexistente → 404
    missing = client.post(
        f"/studio/api/projects/{pid}/brief/review", json={"asset_id": "noexiste123"}
    )
    assert missing.status_code == 404

    # Una transcripción ajena al EDL no se atribuye a este render.
    client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={"asset_id": unrelated["id"], "language": "es",
              "script_segments": [{"start": 0.0, "end": 1.0, "text": "hola mundo"}]},
    )
    resp2 = client.post(f"/studio/api/projects/{pid}/brief/review", json={"asset_id": out["id"]})
    rep2 = resp2.json()
    assert rep2["subtitles"]["required"] is True
    assert rep2["subtitles"]["available"] is False
    assert any(issue["check"] == "subtítulos" for issue in rep2["issues"])

    # Una transcripción exportable vinculada al source del EDL sí es elegible.
    client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={"asset_id": src["id"], "language": "es",
              "script_segments": [{"start": 0.0, "end": 1.0, "text": "hola mundo"}]},
    )
    resp3 = client.post(f"/studio/api/projects/{pid}/brief/review", json={"asset_id": out["id"]})
    rep3 = resp3.json()
    assert rep3["subtitles"]["available"] is True
    assert rep3["subtitles"]["transcription_id"]
    assert rep3["subtitles"]["srt_url"].endswith("fmt=srt")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requiere ffmpeg/ffprobe reales")
def test_review_real_render_passes_when_constraints_met(
    client: TestClient,
    studio_data_dir: Path,
    tmp_path: Path,
) -> None:
    pid = _make_project(client, "Render real")
    store = ProjectStore(studio_data_dir)
    video_path = tmp_path / "real.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=#3b82f6:s=320x180:d=6:r=24",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        timeout=60.0,
    )
    out = store.save_upload(
        pid, filename="real.mp4", content_type="video/mp4",
        data=video_path.read_bytes(), max_upload_bytes=64 * 1024 * 1024,
        category="outputs",
    )
    _create_brief(client, pid, "video de 6 segundos 16:9 sin música")
    resp = client.post(f"/studio/api/projects/{pid}/brief/review", json={"asset_id": out["id"]})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["technical"]["probe_ok"] is True
    assert report["technical"]["duration_sec"] == pytest.approx(6.0, abs=0.5)
    assert report["technical"]["aspect"] == pytest.approx(16 / 9, abs=0.02)
    assert report["summary"]["passed"] is True
    assert report["summary"]["errors"] == 0


# ---------------------------------------------------------------------------
# 9. Paquete de entrega: sin datos de tendencias no se inventa nada
# ---------------------------------------------------------------------------


def test_delivery_package_marks_trend_data_unavailable_and_never_publishes(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    pid = _make_project(client, "Entrega")
    store = ProjectStore(studio_data_dir)
    src = _upload(client, pid, "entrevista.mp4", FAKE_MP4, "video/mp4")
    unrelated = _upload(client, pid, "material-ajeno.mp4", FAKE_MP4, "video/mp4")
    out = store.save_upload(
        pid, filename="reel_final.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024, category="outputs",
        metadata={"production_id": "prod-1", "is_final_reel": True},
    )
    client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={"asset_id": src["id"], "language": "es",
              "script_segments": [{"start": 0.0, "end": 2.0, "text": "gran noticia del producto"}]},
    )
    client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={"asset_id": unrelated["id"], "language": "es",
              "script_segments": [{"start": 0.0, "end": 2.0, "text": "otro proyecto"}]},
    )
    store.save_edl_version(
        pid,
        edl={
            "schema": "comfy_studio.edl.v1",
            "title": "reel",
            "aspect_ratio": "16:9",
            "total_duration_sec": 2.0,
            "cuts": [{"asset_id": src["id"], "in_sec": 0.0, "out_sec": 2.0, "reason": "apertura"}],
        },
        output_asset_id=out["id"],
    )
    _create_brief(client, pid, "reel del producto para instagram")

    resp = client.get(f"/studio/api/projects/{pid}/brief/delivery")
    assert resp.status_code == 200, resp.text
    pkg = resp.json()
    assert pkg["schema"] == "comfy_studio.delivery.v1"
    assert pkg["video"]["asset_id"] == out["id"]
    assert pkg["video"]["file_url"].endswith(f"/assets/{out['id']}/file")
    assert pkg["variants"] and pkg["variants"][0]["asset_id"] == out["id"]
    assert pkg["subtitles"]["srt_url"].endswith("fmt=srt")
    assert pkg["subtitles"]["source_asset_id"] == src["id"]
    assert pkg["edl"]["cuts_count"] == 1
    assert pkg["edl"]["render_url"].endswith("/render")
    assert pkg["generated_assets"][0]["provenance"]["is_final_reel"] is True
    texts = pkg["texts"]
    assert texts["title"] and texts["caption"]
    assert texts["trend_data"]["available"] is False
    assert "no se afirma popularidad" in texts["trend_data"]["limitation"]
    assert texts["evidence_refs"], "título/caption respaldados por evidencia"
    assert pkg["publishing"]["published"] is False
    assert pkg["decisions"]["user_constraints"]
    assert pkg["limitations"], "las limitaciones (p. ej. simulado) deben declararse"


# ---------------------------------------------------------------------------
# 10. Feedback: aprendizaje real (preferencias persistidas y reutilizadas)
# ---------------------------------------------------------------------------


def test_feedback_is_learned_and_carried_into_next_brief(client: TestClient) -> None:
    pid = _make_project(client, "Feedback")
    _upload(client, pid, "a.mp4", FAKE_MP4, "video/mp4")
    _create_brief(client, pid, "un reel")

    fb = client.post(
        f"/studio/api/projects/{pid}/brief/feedback",
        json={"text": "Prefiero aperturas lentas, sin música de stock", "applies_to": "tone"},
    )
    assert fb.status_code == 200, fb.text
    assert fb.json()["stored"] is True
    assert fb.json()["brief"]["version"] == 2
    assert fb.json()["brief"]["brief"]["feedback"]

    got = client.get(f"/studio/api/projects/{pid}/brief")
    assert got.json()["learned_preferences"], "las preferencias aprobadas se conservan"

    row = _create_brief(client, pid, "otro reel del mismo material")
    assert row["version"] == 3
    assert row["brief"]["learned_preferences"], "el nuevo brief hereda las preferencias"


# ---------------------------------------------------------------------------
# 11. Integración con el tool-belt del agente (modo agente)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_agent_tool_belt_brief_flow(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto agente M7")
    pid = project["id"]
    store.save_upload(
        pid, filename="entrevista.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024,
    )
    client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    ctx = ToolContext(
        project_id=pid, store=store, comfyui_client=client, broker=broker, config=studio_config
    )
    belt = create_default_tool_belt()
    for name in (
        "understand_project",
        "create_creative_brief",
        "update_creative_brief",
        "review_output",
    ):
        assert name in belt.list_names()

    und = await belt.execute(
        "understand_project", {"prompt": "entrevista larga, encuentra la historia"}, ctx
    )
    assert und["status"] == "understood"
    assert und["understanding"]["counts"]["assets"] == 1

    created = await belt.execute(
        "create_creative_brief",
        {"prompt": "encuentra la historia que vale la pena contar de esta entrevista"},
        ctx,
    )
    assert created["status"] == "created"
    assert created["brief"]["version"] == 1
    assert created["brief"]["brief"]["piece_type"] == "interview"

    updated = await belt.execute(
        "update_creative_brief", {"changes": {"tone": "íntimo"}}, ctx
    )
    assert updated["status"] == "updated"
    assert updated["brief"]["version"] == 2
    assert updated["brief"]["brief"]["tone"] == "íntimo"

    out = store.save_upload(
        pid, filename="corte.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024, category="outputs",
    )
    review = await belt.execute("review_output", {"asset_id": out["id"]}, ctx)
    assert review["status"] == "reviewed"
    assert review["report"]["asset_id"] == out["id"]
    assert store.get_meta(pid, f"creative_review:{out['id']}") is not None

    # herramienta de solo lectura sin brief → error controlado
    store2_dir = studio_data_dir / "otro"
    store2 = ProjectStore(store2_dir)
    p2 = store2.create_project(name="vacío")
    ctx2 = ToolContext(
        project_id=p2["id"], store=store2, comfyui_client=client, broker=broker,
        config=studio_config,
    )
    from comfy_studio.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        await belt.execute("update_creative_brief", {"changes": {"tone": "x"}}, ctx2)


# ---------------------------------------------------------------------------
# 12. Percepción real (índice vectorial) alimenta la evidencia cuando existe
# ---------------------------------------------------------------------------


def test_understanding_includes_clip_shots_when_vector_store_available(
    studio_data_dir: Path,
) -> None:
    from comfy_studio.creative import build_understanding
    from comfy_studio.perception.store import CLIP_EMBED_DIM

    store = ProjectStore(studio_data_dir)
    pid = store.create_project(name="Con índice")["id"]
    asset = store.save_upload(
        pid, filename="escena.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024,
    )
    vec = PerceptionVectorStore(studio_data_dir / "index.db")
    vec.upsert_clip_shot(
        project_id=pid,
        source_asset_id=asset["id"],
        keyframe_asset_id=f"kf-{asset['id']}-1",
        timestamp_sec=1.0,
        shot_start_sec=0.0,
        shot_end_sec=2.0,
        embedding=[0.1] * CLIP_EMBED_DIM,
        caption="plano del producto",
    )
    und = build_understanding(
        store, pid, prompt="reel del producto", vector_store=vec, ml_simulated=True
    )
    shots = [f for f in und["facts"] if f["kind"] == "clip_shot"]
    assert shots, "los shots del índice deben aparecer como evidencia"
    assert shots[0]["evidence"]["keyframe_asset_id"] == f"kf-{asset['id']}-1"
    assert shots[0]["interval_sec"] == [0.0, 2.0]
    assert shots[0]["simulated"] is True
    assert und["perception_available"] is True
    assert und["counts"]["shots"] == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requiere ffmpeg/ffprobe reales")
def test_music_policy_distinguishes_prohibited_from_required_audio(
    client: TestClient,
    studio_data_dir: Path,
    tmp_path: Path,
) -> None:
    pid = _make_project(client, "Política de audio")
    store = ProjectStore(studio_data_dir)
    video_path = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=320x180:d=3:r=24",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        timeout=60.0,
    )
    output = store.save_upload(
        pid, filename="silent.mp4", content_type="video/mp4",
        data=video_path.read_bytes(), max_upload_bytes=64 * 1024 * 1024,
        category="outputs",
    )

    _create_brief(client, pid, "video de 3 segundos sin música")
    prohibited = client.post(
        f"/studio/api/projects/{pid}/brief/review", json={"asset_id": output["id"]}
    ).json()
    assert prohibited["summary"]["passed"] is True
    assert not any(issue["check"] == "audio" for issue in prohibited["issues"])

    _create_brief(client, pid, "video de 3 segundos con música")
    required = client.post(
        f"/studio/api/projects/{pid}/brief/review", json={"asset_id": output["id"]}
    ).json()
    assert required["summary"]["passed"] is False
    assert any(issue["check"] == "audio" for issue in required["issues"])


def test_explicit_no_subtitles_suppresses_review_and_delivery_links(
    client: TestClient,
    studio_data_dir: Path,
) -> None:
    pid = _make_project(client, "Sin subtítulos")
    store = ProjectStore(studio_data_dir)
    source = _upload(client, pid, "source.mp4", FAKE_MP4, "video/mp4")
    output = store.save_upload(
        pid, filename="output.mp4", content_type="video/mp4",
        data=FAKE_MP4, max_upload_bytes=2 * 1024 * 1024, category="outputs",
    )
    store.save_edl_version(
        pid,
        edl={"schema": "comfy_studio.edl.v1", "cuts": [{"asset_id": source["id"]}]},
        output_asset_id=output["id"],
    )
    client.post(
        f"/studio/api/projects/{pid}/transcribe",
        json={"asset_id": source["id"], "language": "es",
              "script_segments": [{"start": 0.0, "end": 1.0, "text": "texto disponible"}]},
    )
    _create_brief(client, pid, "video sin subtítulos")

    review = client.post(
        f"/studio/api/projects/{pid}/brief/review", json={"asset_id": output["id"]}
    ).json()
    delivery = client.get(f"/studio/api/projects/{pid}/brief/delivery").json()
    assert review["subtitles"]["required"] is False
    assert review["subtitles"]["available"] is False
    assert review["subtitles"]["srt_url"] is None
    assert delivery["subtitles"] is None
