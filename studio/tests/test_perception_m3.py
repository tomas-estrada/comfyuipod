"""Tests de integración y unitarios para el Milestone M3: Percepción + EDL (`m3-percepcion-edl`).

Cubre las aserciones del contrato de validación:
- `VAL-PERC-001`: Extracción de keyframes con `ffmpeg`, timestamps precisos y miniaturas.
- `VAL-PERC-002`: Indexado visual CLIP en `sqlite-vec` (`index.db`) y `find_shots_text`.
- `VAL-PERC-003`: Indexado facial InsightFace (`buffalo_l`), identidades con nombre en `studio.db` +
  `index.db`, `find_shots_face` y búsqueda combinada (`texto ∩ persona`).
- `VAL-PERC-004`: Transcripción `whisperx` (`faster-whisper-large-v3` + aligners wav2vec2 ES/EN) con
  timestamps por palabra y cadena de fallbacks `sm_120` (`float16` -> `float32` -> `openai-whisper`
  -> `funasr` -> `cpu:int8`).
- `VAL-PERC-005`: Diarización de hablantes con FunASR `cam++` sin `pyannote` ni `HF_TOKEN`,
  fusionada con la transcripción por palabra/segmento (`SPEAKER_00`, `SPEAKER_01`).
- `VAL-PERC-006`: Generación de highlights (eliminación de muletillas y pausas) y ejecución
  determinista de EDL sobre `ffmpeg` en `16:9` y `9:16` verificada con `ffprobe`.
"""

from __future__ import annotations

import array
import math
import shutil
import struct
import subprocess
import wave
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.agent.tools import ToolContext, ToolExecutionError, create_default_tool_belt
from comfy_studio.asr import (
    CAMPPLUS_MODEL_REL,
    diarize_audio_file,
    resolve_asr_compute_config,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.perception import probe_media_info
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


def _make_rgb_png(
    width: int,
    height: int,
    rgb: tuple[int, int, int],
    *,
    face_box: tuple[int, int, int, int] | None = None,
    face_rgb: tuple[int, int, int] = (230, 180, 140),
    eye_rgb: tuple[int, int, int] = (20, 30, 60),
) -> bytes:
    """Construye un PNG RGB válido sin dependencias externas (solo stdlib `zlib`/`struct`)."""
    r_bg, g_bg, b_bg = rgb
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray([0])  # filtro PNG None
        for x in range(width):
            pr, pg, pb = r_bg, g_bg, b_bg
            if face_box is not None:
                x0, y0, x1, y1 = face_box
                if x0 <= x < x1 and y0 <= y < y1:
                    pr, pg, pb = face_rgb
                    # Añadir ojos/rasgos deterministas dentro de la caja facial
                    rel_x = (x - x0) / max(1, (x1 - x0))
                    rel_y = (y - y0) / max(1, (y1 - y0))
                    if 0.25 <= rel_y <= 0.42 and (0.22 <= rel_x <= 0.38 or 0.62 <= rel_x <= 0.78):
                        pr, pg, pb = eye_rgb
                    elif 0.68 <= rel_y <= 0.78 and 0.30 <= rel_x <= 0.70:
                        pr, pg, pb = (180, 60, 70)
            row.extend((pr & 0xFF, pg & 0xFF, pb & 0xFF))
        rows.append(bytes(row))

    raw = b"".join(rows)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, level=6)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def _synthesize_scene_video_mp4(out_path: Path) -> None:
    """Genera un MP4 real de 6.0s con 3 escenas de 2.0s (verde, rojo, azul) usando `ffmpeg`."""
    ffmpeg_bin = shutil.which("ffmpeg")
    assert ffmpeg_bin is not None, "ffmpeg debe estar instalado para los tests de M3"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=#10b981:s=320x180:d=2:r=24",
        "-f",
        "lavfi",
        "-i",
        "color=c=#ef4444:s=320x180:d=2:r=24",
        "-f",
        "lavfi",
        "-i",
        "color=c=#3b82f6:s=320x180:d=2:r=24",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]",
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(out_path),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, timeout=30.0)
    assert proc.returncode == 0, f"Fallo al sintetizar MP4 con ffmpeg: {proc.stderr.decode()}"


def _synthesize_two_speaker_wav(out_path: Path) -> None:
    """Genera un WAV PCM 16 kHz mono con 2 hablantes diferenciados por frecuencia fundamental.

    - 0.0s a 1.8s: Hablante 0 (185 Hz)
    - 1.8s a 2.4s: Silencio / pausa (0.6s)
    - 2.4s a 3.6s: Hablante 0 (185 Hz, segmento de muletillas)
    - 3.6s a 4.2s: Silencio / pausa (0.6s)
    - 4.2s a 6.0s: Hablante 1 (410 Hz)
    """
    sample_rate = 16000
    duration_sec = 6.0
    total_samples = int(sample_rate * duration_sec)
    samples = array.array("h", [0] * total_samples)

    spans = [
        (0.0, 1.8, 185.0, 0.45),
        (2.4, 3.6, 185.0, 0.40),
        (4.2, 6.0, 410.0, 0.50),
    ]
    for start_s, end_s, freq_hz, amp in spans:
        i0 = int(start_s * sample_rate)
        i1 = min(total_samples, int(end_s * sample_rate))
        for idx in range(i0, i1):
            t = idx / float(sample_rate)
            val = int(amp * 32767.0 * math.sin(2.0 * math.pi * freq_hz * t))
            samples[idx] = max(-32768, min(32767, val))

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


# ---------------------------------------------------------------------------
# VAL-PERC-001: Extracción de keyframes con timestamps precisos y panel
# ---------------------------------------------------------------------------


def test_val_perc_001_extract_keyframes_with_timestamps_and_panel_thumbnails(
    client: TestClient,
    tmp_path: Path,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Keyframes M3", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    video_file = tmp_path / "escenas_3colores.mp4"
    _synthesize_scene_video_mp4(video_file)

    up_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("escenas_3colores.mp4", video_file.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    )
    assert up_resp.status_code == 201
    video_asset = up_resp.json()

    ext_resp = client.post(
        f"/studio/api/projects/{project_id}/keyframes/extract",
        json={
            "asset_id": video_asset["id"],
            "interval_sec": 2.0,
            "scene_threshold": 0.25,
            "max_frames": 12,
        },
    )
    assert ext_resp.status_code == 200, ext_resp.text
    ext_data = ext_resp.json()
    assert ext_data["status"] == "extracted"
    assert ext_data["count"] >= 3
    keyframes = ext_data["keyframes"]

    # Verificar que existen físicamente y tienen timestamps precisos (±0.5 s de 0.0, 2.0, 4.0)
    timestamps = [float(kf["timestamp_sec"]) for kf in keyframes]
    for expected_ts in (0.0, 2.0, 4.0):
        assert any(abs(ts - expected_ts) <= 0.5 for ts in timestamps), (
            f"No se encontró keyframe cercano a {expected_ts}s en {timestamps}"
        )

    # Verificar listado de keyframes en la API y acceso a cada miniatura PNG
    list_resp = client.get(f"/studio/api/projects/{project_id}/keyframes")
    assert list_resp.status_code == 200
    listed_kfs = list_resp.json()["keyframes"]
    assert len(listed_kfs) == len(keyframes)
    for kf in listed_kfs:
        assert kf["category"] == "keyframes"
        assert kf["kind"] == "image"
        view_r = client.get(kf["view_url"])
        assert view_r.status_code == 200
        assert view_r.content.startswith(b"\x89PNG\r\n\x1a\n")

    # Verificar que panel/app.js renderiza miniaturas con clase keyframe-thumb y timestamp_sec
    repo_root = Path(__file__).resolve().parents[2]
    app_js = (repo_root / "panel" / "app.js").read_text(encoding="utf-8")
    assert "keyframe-thumb" in app_js
    assert "timestamp_sec" in app_js


# ---------------------------------------------------------------------------
# VAL-PERC-002: Indexado visual CLIP en sqlite-vec y búsqueda texto -> planos
# ---------------------------------------------------------------------------


def test_val_perc_002_clip_sqlite_vec_semantic_search_ranks_target_shot_top(
    client: TestClient,
    studio_data_dir: Path,
    tmp_path: Path,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto CLIP Search", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    video_file = tmp_path / "clip_multicolor.mp4"
    _synthesize_scene_video_mp4(video_file)
    up_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("clip_multicolor.mp4", video_file.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    )
    assert up_resp.status_code == 201
    vid_id = up_resp.json()["id"]

    idx_resp = client.post(
        f"/studio/api/projects/{project_id}/perception/index",
        json={"asset_id": vid_id, "interval_sec": 2.0, "detect_faces": False},
    )
    assert idx_resp.status_code == 200, idx_resp.text
    idx_body = idx_resp.json()
    assert idx_body["shots_indexed"] >= 3
    assert (studio_data_dir / "index.db").is_file()

    # 1. Consulta por escena verde ("plano con gorra verde")
    green_search = client.get(
        f"/studio/api/projects/{project_id}/perception/search",
        params={"query": "plano con gorra verde en bosque", "limit": 5},
    )
    assert green_search.status_code == 200
    green_shots = green_search.json()["shots"]
    assert len(green_shots) >= 3
    top_green = green_shots[0]
    assert abs(float(top_green["timestamp_sec"]) - 0.0) <= 0.6
    assert "verde" in (top_green["metadata"].get("tags") or [])

    # 2. Consulta por escena roja ("fuego rojo intenso")
    red_search = client.get(
        f"/studio/api/projects/{project_id}/perception/search",
        params={"query": "escena calida de fuego rojo", "limit": 5},
    )
    assert red_search.status_code == 200
    red_shots = red_search.json()["shots"]
    top_red = red_shots[0]
    assert abs(float(top_red["timestamp_sec"]) - 2.0) <= 0.6
    assert "rojo" in (top_red["metadata"].get("tags") or [])

    # 3. Consulta por escena azul ("mar azul nocturno")
    blue_search = client.get(
        f"/studio/api/projects/{project_id}/perception/search",
        params={"query": "plano de cielo y mar azul", "limit": 5},
    )
    assert blue_search.status_code == 200
    blue_shots = blue_search.json()["shots"]
    top_blue = blue_shots[0]
    assert abs(float(top_blue["timestamp_sec"]) - 4.0) <= 0.6
    assert "azul" in (top_blue["metadata"].get("tags") or [])


# ---------------------------------------------------------------------------
# VAL-PERC-003: Identidades faciales (InsightFace + sqlite-vec) y texto ∩ persona
# ---------------------------------------------------------------------------


def test_val_perc_003_face_identity_crud_search_and_combined_text_person_query(
    client: TestClient,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Identidades InsightFace", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    # Crear foto de referencia de Ana y de Carlos con geometría/rasgos distintos
    ana_ref_png = _make_rgb_png(
        96,
        96,
        (40, 180, 90),
        face_box=(24, 18, 72, 78),
        face_rgb=(235, 190, 150),
        eye_rgb=(20, 90, 40),
    )
    carlos_ref_png = _make_rgb_png(
        96,
        96,
        (190, 50, 50),
        face_box=(20, 20, 76, 76),
        face_rgb=(150, 105, 80),
        eye_rgb=(90, 20, 20),
    )
    # Plano 1: Ana en escenario verde ("gorra verde")
    shot_ana_green = _make_rgb_png(
        96,
        96,
        (16, 185, 129),
        face_box=(24, 18, 72, 78),
        face_rgb=(235, 190, 150),
        eye_rgb=(20, 90, 40),
    )
    # Plano 2: Ana en escenario azul ("cielo azul")
    shot_ana_blue = _make_rgb_png(
        96,
        96,
        (59, 130, 246),
        face_box=(24, 18, 72, 78),
        face_rgb=(235, 190, 150),
        eye_rgb=(20, 90, 40),
    )
    # Plano 3: Carlos en escenario rojo ("fuego rojo")
    shot_carlos_red = _make_rgb_png(
        96,
        96,
        (239, 68, 68),
        face_box=(20, 20, 76, 76),
        face_rgb=(150, 105, 80),
        eye_rgb=(90, 20, 20),
    )

    ana_ref_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("ana_referencia.png", ana_ref_png, "image/png")},
        data={"category": "uploads"},
    ).json()
    carlos_ref_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("carlos_referencia.png", carlos_ref_png, "image/png")},
        data={"category": "uploads"},
    ).json()
    shot1_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("plano_ana_gorra_verde.png", shot_ana_green, "image/png")},
        data={"category": "keyframes"},
    ).json()
    shot2_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("plano_ana_estudio_azul.png", shot_ana_blue, "image/png")},
        data={"category": "keyframes"},
    ).json()
    shot3_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("plano_carlos_fuego_rojo.png", shot_carlos_red, "image/png")},
        data={"category": "keyframes"},
    ).json()

    # Indexar todos los medios del proyecto (CLIP + InsightFace)
    idx_resp = client.post(
        f"/studio/api/projects/{project_id}/perception/index",
        json={"detect_faces": True},
    )
    assert idx_resp.status_code == 200
    assert idx_resp.json()["faces_detected"] >= 3

    # Registrar identidad inicialmente como "Persona_01" y renombrarla a "Ana"
    reg_resp = client.post(
        f"/studio/api/projects/{project_id}/identities",
        json={"name": "Persona_01", "reference_asset_id": ana_ref_asset["id"]},
    )
    assert reg_resp.status_code == 201
    identity = reg_resp.json()
    identity_id = identity["id"]
    assert identity["name"] == "Persona_01"

    ren_resp = client.patch(
        f"/studio/api/projects/{project_id}/identities/{identity_id}",
        json={"name": "Ana"},
    )
    assert ren_resp.status_code == 200
    assert ren_resp.json()["name"] == "Ana"

    # Registrar también a "Carlos"
    reg_carlos = client.post(
        f"/studio/api/projects/{project_id}/identities",
        json={"name": "Carlos", "reference_asset_id": carlos_ref_asset["id"]},
    )
    assert reg_carlos.status_code == 201

    # Listar identidades persistidas
    id_list = client.get(f"/studio/api/projects/{project_id}/identities").json()
    assert {item["name"] for item in id_list["identities"]} == {"Ana", "Carlos"}

    # Búsqueda facial por nombre "Ana" -> devuelve los planos de Ana por encima de Carlos
    face_search = client.post(
        f"/studio/api/projects/{project_id}/perception/search",
        json={"identity_name": "Ana", "limit": 5},
    )
    assert face_search.status_code == 200
    ana_shots = face_search.json()["shots"]
    ana_asset_ids = {s["keyframe_asset_id"] for s in ana_shots}
    assert shot1_asset["id"] in ana_asset_ids
    assert shot2_asset["id"] in ana_asset_ids
    # El plano de Carlos no debe ser el top match de Ana
    assert ana_shots[0]["keyframe_asset_id"] in {
        shot1_asset["id"],
        shot2_asset["id"],
        ana_ref_asset["id"],
    }

    # Búsqueda combinada (texto ∩ persona): "Ana" + "gorra verde" -> plano_ana_gorra_verde en #1
    combined_search = client.post(
        f"/studio/api/projects/{project_id}/perception/search",
        json={
            "identity_name": "Ana",
            "query": "plano con gorra verde en bosque",
            "limit": 5,
        },
    )
    assert combined_search.status_code == 200
    comb_data = combined_search.json()
    assert comb_data["mode"] == "combined"
    comb_shots = comb_data["shots"]
    assert len(comb_shots) >= 1
    assert comb_shots[0]["keyframe_asset_id"] in {shot1_asset["id"], ana_ref_asset["id"]}
    assert comb_shots[0]["keyframe_asset_id"] != shot3_asset["id"]


# ---------------------------------------------------------------------------
# VAL-PERC-004: Transcripción whisperx con timestamps por palabra y fallbacks sm_120
# ---------------------------------------------------------------------------


def test_val_perc_004_whisperx_word_timestamps_and_sm120_fallback_chain(
    client: TestClient,
    tmp_path: Path,
) -> None:
    # 1. Unit tests exhaustivos de la cadena de fallbacks en sm_120 (RTX 5090 Blackwell)
    cfg_primary = resolve_asr_compute_config(
        cuda_available=True,
        sm_arch="sm_120",
        ct2_float16_ok=True,
        ct2_cuda_ok=True,
        language="es",
    )
    assert cfg_primary.backend == "whisperx"
    assert cfg_primary.device == "cuda"
    assert cfg_primary.compute_type == "float16"
    assert cfg_primary.fallback_used is False
    assert cfg_primary.requires_hf_token is False
    assert "spanish" in cfg_primary.aligner_model_path

    cfg_fp32 = resolve_asr_compute_config(
        cuda_available=True,
        sm_arch="sm_120",
        ct2_float16_ok=False,
        ct2_cuda_ok=True,
        language="en",
    )
    assert cfg_fp32.backend == "whisperx"
    assert cfg_fp32.device == "cuda"
    assert cfg_fp32.compute_type == "float32"
    assert cfg_fp32.fallback_used is True
    assert "float32" in (cfg_fp32.fallback_reason or "")
    assert "english" in cfg_fp32.aligner_model_path

    cfg_oai = resolve_asr_compute_config(
        cuda_available=True,
        sm_arch="sm_120",
        ct2_float16_ok=False,
        ct2_cuda_ok=False,
        openai_whisper_ok=True,
    )
    assert cfg_oai.backend == "openai-whisper"
    assert cfg_oai.device == "cuda"
    assert cfg_oai.compute_type == "float16"
    assert cfg_oai.fallback_used is True

    cfg_funasr = resolve_asr_compute_config(
        cuda_available=True,
        sm_arch="sm_120",
        ct2_float16_ok=False,
        ct2_cuda_ok=False,
        openai_whisper_ok=False,
        funasr_ok=True,
    )
    assert cfg_funasr.backend == "funasr"
    assert cfg_funasr.device == "cuda"
    assert cfg_funasr.fallback_used is True

    cfg_cpu = resolve_asr_compute_config(
        cuda_available=False,
        sm_arch="sm_120",
    )
    assert cfg_cpu.backend == "whisperx"
    assert cfg_cpu.device == "cpu"
    assert cfg_cpu.compute_type == "int8"
    assert cfg_cpu.fallback_used is True

    # 2. Integración sobre audio WAV real con tramos conocidos (0.0-1.8s, 2.4-3.6s, 4.2-6.0s)
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto WhisperX M3", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    wav_path = tmp_path / "locucion_2hablantes.wav"
    _synthesize_two_speaker_wav(wav_path)
    up_wav = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("locucion_2hablantes.wav", wav_path.read_bytes(), "audio/wav")},
        data={"category": "voice"},
    )
    assert up_wav.status_code == 201
    wav_asset = up_wav.json()

    tr_resp = client.post(
        f"/studio/api/projects/{project_id}/transcribe",
        json={"asset_id": wav_asset["id"], "language": "es"},
    )
    assert tr_resp.status_code == 200, tr_resp.text
    tr_data = tr_resp.json()["transcription"]
    segments = tr_data["segments"]
    words = tr_data["words"]

    assert len(segments) == 3
    assert len(words) >= 10
    # Verificar precisión de inicio/fin de cada tramo (±0.2 s)
    expected_spans = [(0.0, 1.8), (2.4, 3.6), (4.2, 6.0)]
    for seg, (exp_start, exp_end) in zip(segments, expected_spans, strict=True):
        assert abs(float(seg["start"]) - exp_start) <= 0.2
        assert abs(float(seg["end"]) - exp_end) <= 0.2
        assert len(seg["words"]) >= 3
        for w in seg["words"]:
            assert "word" in w
            assert float(w["start"]) >= float(seg["start"]) - 0.05
            assert float(w["end"]) <= float(seg["end"]) + 0.05


# ---------------------------------------------------------------------------
# VAL-PERC-005: Diarización FunASR cam++ sin pyannote ni HF_TOKEN
# ---------------------------------------------------------------------------


def test_val_perc_005_funasr_campplus_diarization_without_hf_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Asegurar que HF_TOKEN y HUGGING_FACE_HUB_TOKEN NO existen en el entorno
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)

    wav_path = tmp_path / "dialogo_dos_hablantes.wav"
    _synthesize_two_speaker_wav(wav_path)

    # Prueba directa de diarize_audio_file
    raw_diar = diarize_audio_file(wav_path)
    assert raw_diar["backend"] == "funasr_campplus"
    assert raw_diar["model_path"] == CAMPPLUS_MODEL_REL
    assert raw_diar["requires_hf_token"] is False
    assert raw_diar["uses_pyannote"] is False
    assert raw_diar["speakers"] == ["SPEAKER_00", "SPEAKER_01"]

    # Prueba vía API del proyecto fusionando con la transcripción de whisperx
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Diarización cam++", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    up_wav = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("dialogo_dos_hablantes.wav", wav_path.read_bytes(), "audio/wav")},
        data={"category": "voice"},
    ).json()

    diar_resp = client.post(
        f"/studio/api/projects/{project_id}/diarize",
        json={"asset_id": up_wav["id"], "expected_speakers": 2},
    )
    assert diar_resp.status_code == 200, diar_resp.text
    diar_body = diar_resp.json()
    assert diar_body["status"] == "diarized"
    assert diar_body["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert diar_body["diarization"]["requires_hf_token"] is False
    assert diar_body["diarization"]["uses_pyannote"] is False

    by_speaker = diar_body["by_speaker"]
    assert "SPEAKER_00" in by_speaker
    assert "SPEAKER_01" in by_speaker
    assert len(by_speaker["SPEAKER_00"]["ranges"]) >= 1
    assert len(by_speaker["SPEAKER_01"]["ranges"]) >= 1

    # Cada palabra de la transcripción actualizada tiene asignado su speaker
    updated_tr = diar_body["transcription"]
    assert updated_tr["diarized"] is True
    assert all(w.get("speaker") in ("SPEAKER_00", "SPEAKER_01") for w in updated_tr["words"])


# ---------------------------------------------------------------------------
# VAL-PERC-006: Highlights LLM stub -> EDL -> ffmpeg (16:9 y 9:16) con ffprobe
# ---------------------------------------------------------------------------


def test_val_perc_006_highlights_edl_and_deterministic_ffmpeg_render_16x9_and_9x16(
    client: TestClient,
    tmp_path: Path,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto EDL Highlights M3", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    # Subir vídeo de 6.0s y audio de 6.0s
    video_file = tmp_path / "bruto_entrevista_6s.mp4"
    _synthesize_scene_video_mp4(video_file)
    vid_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("bruto_entrevista_6s.mp4", video_file.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    ).json()

    # Transcribir con segmentos explícitos que incluyen muletillas y pausas
    tr_resp = client.post(
        f"/studio/api/projects/{project_id}/transcribe",
        json={
            "asset_id": vid_asset["id"],
            "language": "es",
            "script_segments": [
                {
                    "start": 0.0,
                    "end": 1.8,
                    "speaker": "SPEAKER_00",
                    "text": (
                        "Bienvenidos al estudio de producción audiovisual "
                        "con inteligencia artificial."
                    ),
                },
                {
                    "start": 2.4,
                    "end": 3.5,
                    "speaker": "SPEAKER_00",
                    "text": "eh mmm este bueno digamos",
                },
                {
                    "start": 4.0,
                    "end": 5.6,
                    "speaker": "SPEAKER_01",
                    "text": "Aquí presentamos el resultado final sin pausas ni muletillas.",
                },
            ],
        },
    )
    assert tr_resp.status_code == 200
    tr_id = tr_resp.json()["transcription"]["id"]

    # Generar EDL de highlights eliminando muletillas (2.4-3.5s) y pausas (1.8-2.4s, 3.5-4.0s)
    hl_resp = client.post(
        f"/studio/api/projects/{project_id}/edl/highlights",
        json={
            "asset_id": vid_asset["id"],
            "transcription_id": tr_id,
            "title": "Highlights Limpios M3",
            "aspect_ratio": "16:9",
            "remove_fillers": True,
            "min_pause_sec": 0.35,
        },
    )
    assert hl_resp.status_code == 200, hl_resp.text
    edl_record = hl_resp.json()
    edl_doc = edl_record["edl"]
    assert edl_doc["schema"] == "comfy_studio.edl.v1"
    cuts = edl_doc["cuts"]
    assert len(cuts) == 2
    assert cuts[0]["in_sec"] == pytest.approx(0.0, abs=0.05)
    assert cuts[0]["out_sec"] == pytest.approx(1.8, abs=0.05)
    assert cuts[1]["in_sec"] == pytest.approx(4.0, abs=0.05)
    assert cuts[1]["out_sec"] == pytest.approx(5.6, abs=0.05)
    assert edl_doc["total_duration_sec"] == pytest.approx(3.4, abs=0.1)
    assert len(edl_doc["editorial_report"]["removed_fillers"]) >= 1
    assert len(edl_doc["editorial_report"]["removed_pauses"]) >= 2

    # Ejecutar EDL en formato 16:9 horizontal con ffmpeg y verificar con ffprobe
    build_16x9 = client.post(
        f"/studio/api/projects/{project_id}/edl/build",
        json={
            "edl_id": edl_record["id"],
            "aspect_ratio": "16:9",
            "source_asset_id": vid_asset["id"],
        },
    )
    assert build_16x9.status_code == 200, build_16x9.text
    res_16x9 = build_16x9.json()
    assert res_16x9["status"] == "built"
    assert res_16x9["aspect_ratio"] == "16:9"
    ver_16x9 = res_16x9["verification"]
    assert ver_16x9["width"] == 1280
    assert ver_16x9["height"] == 720
    assert abs(ver_16x9["actual_duration_sec"] - 3.4) <= 0.35

    # Ejecutar el mismo EDL en formato 9:16 vertical (Reels/Shorts) y verificar con ffprobe
    build_9x16 = client.post(
        f"/studio/api/projects/{project_id}/edl/build",
        json={
            "edl_id": edl_record["id"],
            "aspect_ratio": "9:16",
            "source_asset_id": vid_asset["id"],
        },
    )
    assert build_9x16.status_code == 200, build_9x16.text
    res_9x16 = build_9x16.json()
    assert res_9x16["aspect_ratio"] == "9:16"
    ver_9x16 = res_9x16["verification"]
    assert ver_9x16["width"] == 720
    assert ver_9x16["height"] == 1280
    assert abs(ver_9x16["actual_duration_sec"] - 3.4) <= 0.35

    # Verificar que ambos MP4 están en la galería de outputs y son inspeccionables por ffprobe
    out_mp4_path = tmp_path / "downloaded_9x16.mp4"
    dl_9x16 = client.get(res_9x16["output_asset"]["download_url"])
    assert dl_9x16.status_code == 200
    out_mp4_path.write_bytes(dl_9x16.content)
    probed = probe_media_info(out_mp4_path)
    assert probed["width"] == 720
    assert probed["height"] == 1280
    assert abs(probed["duration_sec"] - 3.4) <= 0.35


# ---------------------------------------------------------------------------
# Tool-belt M3: Ejecución directa de las 7 herramientas M3 y casos borde
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_m3_agent_tools_end_to_end_and_edge_cases(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
    tmp_path: Path,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Agent Tools M3", aspect_ratio="16:9")
    pid = project["id"]
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    ctx = ToolContext(
        project_id=pid,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        config=studio_config,
    )
    belt = create_default_tool_belt()
    names = set(belt.list_names())
    assert {
        "extract_keyframes",
        "index_media",
        "find_shots_text",
        "find_shots_face",
        "transcribe",
        "diarize",
        "propose_edl",
        "build_edl",
    }.issubset(names)

    # Caso borde: extraer keyframes sin vídeos en el proyecto -> ToolExecutionError
    with pytest.raises(ToolExecutionError, match="vídeo"):
        await belt.execute("extract_keyframes", {}, ctx)

    # Subir vídeo, audio y foto de rostro
    vid_file = tmp_path / "escenas_tools.mp4"
    _synthesize_scene_video_mp4(vid_file)
    wav_file = tmp_path / "voces_tools.wav"
    _synthesize_two_speaker_wav(wav_file)
    face_png = _make_rgb_png(
        96,
        96,
        (16, 185, 129),
        face_box=(24, 18, 72, 78),
    )

    vid_asset = store.save_upload(
        pid,
        filename="escenas_tools.mp4",
        content_type="video/mp4",
        data=vid_file.read_bytes(),
        max_upload_bytes=10 * 1024 * 1024,
        category="uploads",
    )
    wav_asset = store.save_upload(
        pid,
        filename="voces_tools.wav",
        content_type="audio/wav",
        data=wav_file.read_bytes(),
        max_upload_bytes=10 * 1024 * 1024,
        category="voice",
    )
    face_asset = store.save_upload(
        pid,
        filename="elena_ref.png",
        content_type="image/png",
        data=face_png,
        max_upload_bytes=10 * 1024 * 1024,
        category="uploads",
    )

    # 1. extract_keyframes
    kf_res = await belt.execute(
        "extract_keyframes", {"asset_id": vid_asset["id"], "interval_sec": 2.0}, ctx
    )
    assert kf_res["status"] == "extracted"
    assert kf_res["count"] >= 3

    # 2. index_media
    idx_res = await belt.execute("index_media", {"interval_sec": 2.0, "detect_faces": True}, ctx)
    assert idx_res["status"] == "indexed"
    assert idx_res["shots_indexed"] >= 3

    # 3. find_shots_text
    txt_res = await belt.execute(
        "find_shots_text", {"query": "plano con gorra verde", "limit": 3}, ctx
    )
    assert txt_res["count"] >= 1

    # 4. find_shots_face (por reference_asset_id y caso borde sin argumentos)
    with pytest.raises(ToolExecutionError):
        await belt.execute("find_shots_face", {"identity_name": "NoExiste"}, ctx)

    face_res = await belt.execute(
        "find_shots_face",
        {"reference_asset_id": face_asset["id"], "text_query": "verde", "limit": 3},
        ctx,
    )
    assert face_res["count"] >= 1

    # 5. transcribe + 6. diarize
    tr_res = await belt.execute("transcribe", {"asset_id": wav_asset["id"], "language": "es"}, ctx)
    assert tr_res["status"] == "transcribed"
    assert tr_res["words_count"] >= 5

    diar_res = await belt.execute(
        "diarize",
        {
            "asset_id": wav_asset["id"],
            "transcription_id": tr_res["transcription_id"],
            "expected_speakers": 2,
        },
        ctx,
    )
    assert diar_res["status"] == "diarized"
    assert diar_res["speakers"] == ["SPEAKER_00", "SPEAKER_01"]

    # 7. build_edl generando highlights automáticamente desde la transcripción
    build_res = await belt.execute(
        "build_edl",
        {
            "aspect_ratio": "9:16",
            "source_asset_id": vid_asset["id"],
            "from_transcription": True,
        },
        ctx,
    )
    assert build_res["status"] == "built"
    assert build_res["aspect_ratio"] == "9:16"
    assert build_res["verification"]["width"] == 720
    assert build_res["verification"]["height"] == 1280


def test_m3_stub_llm_chat_turns_trigger_perception_asr_and_edl_tools(
    client: TestClient,
    tmp_path: Path,
) -> None:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Chat M3", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    vid_file = tmp_path / "chat_video.mp4"
    _synthesize_scene_video_mp4(vid_file)
    wav_file = tmp_path / "chat_audio.wav"
    _synthesize_two_speaker_wav(wav_file)
    ana_png = _make_rgb_png(96, 96, (16, 185, 129), face_box=(24, 18, 72, 78))

    client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("chat_video.mp4", vid_file.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    )
    client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("chat_audio.wav", wav_file.read_bytes(), "audio/wav")},
        data={"category": "voice"},
    )
    ana_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("ana_chat.png", ana_png, "image/png")},
        data={"category": "uploads"},
    ).json()
    client.post(
        f"/studio/api/projects/{project_id}/identities",
        json={"name": "Ana", "reference_asset_id": ana_asset["id"]},
    )

    prompts_and_tools = [
        ("Por favor extrae keyframes del vídeo", "extract_keyframes"),
        ("Ahora indexa los medios del proyecto", "index_media"),
        ("Busca planos con gorra verde", "find_shots_text"),
        ("Busca planos donde aparece Ana", "find_shots_face"),
        ("Transcribe el audio en español", "transcribe"),
        ("Diariza los hablantes de la locución", "diarize"),
        ("Construye el EDL en vertical 9:16", "build_edl"),
    ]
    for prompt_text, expected_tool in prompts_and_tools:
        resp = client.post(
            f"/studio/api/projects/{project_id}/chat",
            json={"message": prompt_text},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        tool_names = [tc["name"] for tc in data["tool_calls"]]
        assert expected_tool in tool_names, (
            f"No se ejecutó {expected_tool} para {prompt_text!r}: {tool_names}"
        )
        for tr in data["tool_results"]:
            assert tr["is_error"] is False, f"Error en tool {tr['name']}: {tr['result']}"


def test_m3_runtime_blackwell_fallback_route_aliases_and_edl_audio_preservation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    from comfy_studio.agent.loop import SYSTEM_PROMPT_ES
    from comfy_studio.asr import (
        CamPlusDiarizer,
        WhisperXTranscriber,
        resolve_asr_backend_plan,
        run_with_blackwell_fallback,
    )
    from comfy_studio.edl import build_highlights_cuts_from_transcription, probe_media_file
    from comfy_studio.perception import (
        INSIGHTFACE_NC_LICENSE_NOTICE,
        FaceIndex,
        combined_search,
    )

    # 1. Verificar que SYSTEM_PROMPT_ES incluye las 10 herramientas M2 + M3
    for tool_name in (
        "search_assets",
        "enqueue_render",
        "propose_edl",
        "extract_keyframes",
        "index_media",
        "find_shots_text",
        "find_shots_face",
        "transcribe",
        "diarize",
        "build_edl",
    ):
        assert tool_name in SYSTEM_PROMPT_ES

    # 2. Verificar ejecución dinámica de `run_with_blackwell_fallback` ante excepciones sm_120
    plan = resolve_asr_backend_plan(cuda_available=True, sm_arch="sm_120", language="es")
    assert len(plan) == 5
    assert [f"{c.backend}:{c.device}:{c.compute_type}" for c in plan] == [
        "whisperx:cuda:float16",
        "whisperx:cuda:float32",
        "openai-whisper:cuda:float16",
        "funasr:cuda:float16",
        "whisperx:cpu:int8",
    ]

    def _failing_ct2_runner(cfg):
        if cfg.backend == "whisperx" and cfg.device == "cuda":
            raise RuntimeError(
                f"CUDA error: no kernel image for {cfg.sm_arch} ({cfg.compute_type})"
            )
        return {"ok": True, "used": f"{cfg.backend}:{cfg.device}:{cfg.compute_type}"}

    out, resolved_cfg = run_with_blackwell_fallback(
        _failing_ct2_runner,
        cuda_available=True,
        sm_arch="sm_120",
    )
    assert out["used"] == "openai-whisper:cuda:float16"
    assert resolved_cfg.fallback_used is True
    assert resolved_cfg.attempted_chain == [
        "whisperx:cuda:float16",
        "whisperx:cuda:float32",
        "openai-whisper:cuda:float16",
    ]
    assert "no kernel image" in (resolved_cfg.fallback_reason or "")

    # 3. Verificar rutas documentadas (/faces/index, /search/faces, /search/combined,
    # /edl/{v}/render) y aviso no comercial de buffalo_l en API + panel/index.html
    panel_html = client.get("/studio/").text
    assert "insightface-license-notice" in panel_html
    assert "buffalo_l" in panel_html

    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Aliases y Audio EDL", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    # Sintetizar vídeo CON pista de audio AAC real para verificar preservación de audio en EDL
    ffmpeg_bin = shutil.which("ffmpeg")
    assert ffmpeg_bin is not None
    av_mp4 = tmp_path / "clip_con_audio.mp4"
    wav_for_mux = tmp_path / "audio_mux.wav"
    _synthesize_two_speaker_wav(wav_for_mux)
    silent_mp4 = tmp_path / "video_mudo.mp4"
    _synthesize_scene_video_mp4(silent_mp4)

    cmd_mux = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(silent_mp4),
        "-i",
        str(wav_for_mux),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(av_mp4),
    ]
    subprocess.run(cmd_mux, check=True, capture_output=True, timeout=30.0)
    assert probe_media_file(av_mp4)["has_audio"] is True

    # Caso borde: transcribir y diarizar un vídeo SIN pista de audio (silent_mp4)
    silent_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("video_mudo.mp4", silent_mp4.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    ).json()
    tr_silent = client.post(
        f"/studio/api/projects/{project_id}/transcribe",
        json={"asset_id": silent_asset["id"], "language": "es"},
    )
    assert tr_silent.status_code == 200
    diar_silent = client.post(
        f"/studio/api/projects/{project_id}/diarize",
        json={"asset_id": silent_asset["id"]},
    )
    assert diar_silent.status_code == 200

    # Subir el vídeo CON audio y una imagen de rostro
    av_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("clip_con_audio.mp4", av_mp4.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    ).json()
    ana_png = _make_rgb_png(96, 96, (16, 185, 129), face_box=(24, 18, 72, 78))
    ana_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("ana_ref.png", ana_png, "image/png")},
        data={"category": "uploads"},
    ).json()

    # Extraer keyframes e invocar POST /faces/index con registro de identidad
    client.post(
        f"/studio/api/projects/{project_id}/keyframes/extract",
        json={"asset_id": av_asset["id"], "interval_sec": 2.0},
    )
    f_idx = client.post(
        f"/studio/api/projects/{project_id}/faces/index",
        json={"name": "Ana", "reference_asset_id": ana_asset["id"]},
    )
    assert f_idx.status_code == 200, f_idx.text
    f_idx_body = f_idx.json()
    assert f_idx_body["license_notice"] == INSIGHTFACE_NC_LICENSE_NOTICE
    assert f_idx_body["identity"]["name"] == "Ana"

    # Invocar POST /search/faces y POST /search/combined
    sf_resp = client.post(
        f"/studio/api/projects/{project_id}/search/faces",
        json={"person_name": "Ana", "limit": 5},
    )
    assert sf_resp.status_code == 200, sf_resp.text
    assert sf_resp.json()["license_notice"] == INSIGHTFACE_NC_LICENSE_NOTICE
    assert sf_resp.json()["count"] >= 1

    sc_resp = client.post(
        f"/studio/api/projects/{project_id}/search/combined",
        json={"person_name": "Ana", "text_query": "verde", "limit": 5},
    )
    assert sc_resp.status_code == 200, sc_resp.text
    assert sc_resp.json()["license_notice"] == INSIGHTFACE_NC_LICENSE_NOTICE
    assert sc_resp.json()["count"] >= 1

    # Verificar clases fachada exportadas
    assert FaceIndex is not None
    assert callable(combined_search)
    assert callable(build_highlights_cuts_from_transcription)
    assert WhisperXTranscriber().resolve_compute_config().backend == "whisperx"
    assert CamPlusDiarizer().diarize_file(wav_for_mux)["backend"] == "funasr_campplus"

    # Crear EDL sobre el clip CON audio y renderizar con POST /edl/{version}/render
    edl_created = client.post(
        f"/studio/api/projects/{project_id}/edl",
        json={
            "title": "EDL Con Audio",
            "aspect_ratio": "16:9",
            "source_asset_id": av_asset["id"],
            "cuts": [
                {"in_sec": 0.0, "out_sec": 1.5, "reason": "apertura con voz"},
                {"in_sec": 2.0, "out_sec": 3.5, "reason": "cierre con voz"},
            ],
        },
    ).json()

    render_resp = client.post(
        f"/studio/api/projects/{project_id}/edl/{edl_created['version']}/render",
        json={"aspect_ratio": "16:9", "source_asset_id": av_asset["id"]},
    )
    assert render_resp.status_code == 200, render_resp.text
    render_data = render_resp.json()
    assert render_data["verification"]["width"] == 1280
    assert render_data["verification"]["height"] == 720
    assert render_data["verification"]["has_audio"] is True
    assert render_data["verification"]["audio_codec_name"] == "aac"
