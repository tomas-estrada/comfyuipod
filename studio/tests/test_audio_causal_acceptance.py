"""Aceptación causal end-to-end del audio (A20/C25): ruta + store + FFmpeg reales.

TDD: la prueba debe demostrar con audio REAL (WAV del proyecto ensamblados con
FFmpeg vía la ruta HTTP) el invariante causal completo:

1. Stems separados (actor / speech / ambience) renderizados desde muestras
   propias del proyecto, con sample rate, clipping, loudness y true peak
   MEDIDOS de nuevo de forma independiente (ffprobe + loudnorm) sobre los bytes
   descargados — no valores declarados de adorno.
2. Borrar una causa elimina SOLO su evento: los stems ajenos conservan sus
   bytes (hash) y métricas intactos; el stem afectado se reporta obsoleto.
3. Rerender excluye únicamente la causa borrada, verificado por contenido:
   la ventana temporal del evento eliminado queda en silencio digital y la del
   evento retenido sigue sonando. El stem vaciado por la segunda causa se
   elimina (asset 404) mientras los ajenos siguen byte a byte idénticos.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.audio import TARGET_SAMPLE_RATE, TRUE_PEAK_CEILING_DBFS

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

FF_REQUIRED = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe requeridos para la aceptación causal de audio",
)

#: Ventanas (s) que aíslan cada evento del stem 'actor' en un mix de 3.0 s.
DELETED_EVENT_WINDOW = (0.45, 0.95)  # step-1 (causa 'actor-step') onsets 0.5 s
KEPT_EVENT_WINDOW = (1.45, 1.95)  # step-2 (causa 'actor-clap') onset 1.5 s
SILENT_RMS_MAX = 50.0  # escala s16: apad digital es exactamente 0
SOUND_RMS_MIN = 5000.0  # seno audible proyecta miles en escala s16


def _make_sample(path: Path, *, freq: int, seconds: float = 0.8) -> None:
    """Muestra WAV propia del proyecto: seno sintético PCM s16 estéreo 48 kHz."""
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-af",
            "volume=4.0,aformat=sample_rates=48000:channel_layouts=stereo",
            "-c:a",
            "pcm_s16le",
            "-bitexact",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset_file(client: TestClient, project_id: str, asset_id: str) -> bytes:
    response = client.get(f"/studio/api/projects/{project_id}/assets/{asset_id}/file")
    assert response.status_code == 200, response.text
    return response.content


def _probe(path: Path) -> dict:
    proc = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,channels:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(proc.stdout)
    stream = data["streams"][0]
    return {
        "codec_name": stream["codec_name"],
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "duration_s": float(data["format"]["duration"]),
    }


def _measure_loudness(path: Path) -> tuple[float, float]:
    """Medición INDEPENDIENTE con loudnorm (EBU R128) del WAV descargado."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-v",
            "info",
            "-i",
            str(path),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    blocks = [b for b in _json_blocks(proc.stderr) if '"input_i"' in b]
    assert blocks, f"loudnorm no devolvió medición para {path}"
    data = json.loads(blocks[-1])
    return float(data["input_i"]), float(data["input_tp"])


def _json_blocks(text: str) -> list[str]:
    import re

    return re.findall(r"\{[^{}]*\}", text)


def _window_rms_s16(path: Path, start_s: float, end_s: float) -> float:
    """RMS s16 mono de una ventana temporal decodificando el WAV real."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    samples = struct.unpack(f"<{len(proc.stdout) // 2}h", proc.stdout)
    sample_rate = 48000
    lo = int(start_s * sample_rate)
    hi = min(int(end_s * sample_rate), len(samples))
    window = samples[lo:hi]
    assert window, f"ventana vacía {start_s}-{end_s}s en {path}"
    return math.sqrt(sum(float(v) ** 2 for v in window) / len(window))


def _assert_measured_fields(
    client: TestClient, project_id: str, stem_entry: dict
) -> tuple[str, dict]:
    """Los campos de medición de la ruta coinciden con ffprobe/loudnorm reales."""
    stem_path = Path(_download_to_temp(client, project_id, stem_entry["asset_id"]))
    probe = _probe(stem_path)
    assert probe["codec_name"] == "pcm_s16le"
    assert probe["channels"] == 2
    # El sample rate declarado es el medido en el fichero, no una constante.
    assert stem_entry["sample_rate"] == probe["sample_rate"] == TARGET_SAMPLE_RATE
    integrated, true_peak = _measure_loudness(stem_path)
    # Loudness/true peak reportados = medición real del WAV (tolerancia por
    # redondeo), no placeholders declarados.
    assert math.isfinite(stem_entry["integrated_loudness_lufs"])
    assert math.isfinite(stem_entry["true_peak_dbfs"])
    assert abs(stem_entry["integrated_loudness_lufs"] - integrated) < 0.6
    assert abs(stem_entry["true_peak_dbfs"] - true_peak) < 0.6
    # Clipping coherente con el techo medido: sin clip y debajo del techo.
    assert stem_entry["clipped"] is False
    assert stem_entry["true_peak_dbfs"] < TRUE_PEAK_CEILING_DBFS
    return stem_path, {
        "sha256": _sha256(stem_path.read_bytes()),
        "integrated_loudness_lufs": stem_entry["integrated_loudness_lufs"],
        "true_peak_dbfs": stem_entry["true_peak_dbfs"],
        "sample_rate": stem_entry["sample_rate"],
        "clipped": stem_entry["clipped"],
    }


def _download_to_temp(client: TestClient, project_id: str, asset_id: str) -> str:
    import tempfile

    data = _asset_file(client, project_id, asset_id)
    fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    fd.write(data)
    fd.close()
    return fd.name


@FF_REQUIRED
def test_causal_delete_preserves_unrelated_stems_and_rerender_excludes_only_deleted_cause(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Aceptación causal audio"}
    ).json()["id"]

    # Muestras sintéticas propias del proyecto, cada una un asset de audio real.
    samples: dict[str, Path] = {}
    asset_ids: dict[str, str] = {}
    for name, freq in (
        ("step", 440),
        ("clap", 880),
        ("line", 660),
        ("wind", 220),
    ):
        path = tmp_path / f"{name}.wav"
        _make_sample(path, freq=freq)
        samples[name] = path
        uploaded = client.post(
            f"/studio/api/projects/{project_id}/assets",
            files={"file": (path.name, path.read_bytes(), "audio/wav")},
            data={"category": "uploads"},
        )
        assert uploaded.status_code == 201, uploaded.text
        asset_ids[name] = uploaded.json()["id"]

    rights = {"sample_license": "user-owned", "sample_attribution": "Project owner upload"}

    def event(event_id: str, onset_s: float, caused_by: str | None, sample: str) -> dict:
        payload = {
            "event_id": event_id,
            "onset_s": onset_s,
            "sample_asset_id": asset_ids[sample],
            **rights,
        }
        if caused_by is not None:
            payload["caused_by"] = caused_by
        return payload

    mix_payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
            # Stem del ACTOR: dos acciones/causas distintas en el mismo stem.
            "actor": [
                event("step-1", 0.5, "actor-step", "step"),
                event("step-2", 1.5, "actor-clap", "clap"),
            ],
            # Stem de VOZ: causa propia, ajena a las causas del actor.
            "speech": [event("line-1", 0.8, "line-take", "line")],
            # Stem de AMBIENTE: sin causa vinculada (independiente).
            "ambience": [event("wind", 0.0, None, "wind")],
        },
    }
    mix_url = f"/studio/api/projects/{project_id}/sound/mix"
    assert client.put(mix_url, json=mix_payload).status_code == 200

    # --- 1) Render real de los tres stems separados -------------------------
    rendered = client.post(f"/studio/api/projects/{project_id}/sound/render")
    assert rendered.status_code == 200, rendered.text
    body = rendered.json()
    assert body["audio_generation"] == "ffmpeg_assembled"
    assert body["separation_claim"] == "rendered_stems"
    assert body["simulated"] is False
    assert body["engine"] == "ffmpeg"
    assert body["skipped_stems"] == []
    stem_entries = {stem["stem"]: stem for stem in body["stems"]}
    assert set(stem_entries) == {"actor", "speech", "ambience"}
    assert stem_entries["actor"]["event_ids"] == ["step-1", "step-2"]
    assert stem_entries["speech"]["event_ids"] == ["line-1"]
    assert stem_entries["ambience"]["event_ids"] == ["wind"]

    # Cada stem es un WAV real con métricas MEDIDAS (no placeholders declarados).
    snapshots: dict[str, dict] = {}
    wav_paths: dict[str, Path] = {}
    for name, entry in stem_entries.items():
        wav_paths[name], snapshots[name] = _assert_measured_fields(
            client, project_id, entry
        )
    # Tres stems separados: ficheros distintos, no una única mezcla.
    assert len({snap["sha256"] for snap in snapshots.values()}) == 3
    # Los stems suenan de verdad (ventanas de sus eventos con energía).
    for name, window in (
        ("actor", DELETED_EVENT_WINDOW),
        ("actor", KEPT_EVENT_WINDOW),
        ("speech", (0.75, 1.25)),
        ("ambience", (0.0, 0.5)),
    ):
        assert _window_rms_s16(wav_paths[name], *window) > SOUND_RMS_MIN, name

    old_actor_asset_id = stem_entries["actor"]["asset_id"]

    # --- 2) Borrar UNA causa del actor ---------------------------------------
    deleted = client.delete(
        f"/studio/api/projects/{project_id}/sound/causes/actor-step"
    )
    assert deleted.status_code == 200, deleted.text
    deleted_body = deleted.json()
    # Curación de eventos: solo el evento vinculado a la causa desaparece.
    assert deleted_body["removed_event_ids"] == ["step-1"]
    remaining = {
        ev["event_id"] for events in deleted_body["stems"].values() for ev in events
    }
    assert remaining == {"step-2", "line-1", "wind"}
    # El stem 'actor' conserva otro evento: su render se marca obsoleto, no borrado.
    assert deleted_body["removed_stem_asset_ids"] == []
    assert deleted_body["stale_stems"] == ["actor"]

    # El asset del stem afectado sigue en el proyecto pero reporta obsolescencia.
    stale_asset = client.get(
        f"/studio/api/projects/{project_id}/assets/{old_actor_asset_id}"
    ).json()
    assert stale_asset["metadata"]["stale"] is True
    assert stale_asset["metadata"]["stale_reason"] == "audio_cause_deleted"

    # Semántica stale/conservación en el índice de stems.
    index = client.get(f"/studio/api/projects/{project_id}/sound/stems").json()
    by_stem = {entry["stem"]: entry for entry in index["stems"]}
    assert by_stem["actor"]["stale"] is True
    assert by_stem["speech"]["stale"] is False
    assert by_stem["ambience"]["stale"] is False

    # Los stems AJENOS conservan sus bytes y métricas exactas.
    for name in ("speech", "ambience"):
        assert by_stem[name]["asset_id"] == stem_entries[name]["asset_id"]
        preserved = _asset_file(client, project_id, stem_entries[name]["asset_id"])
        assert _sha256(preserved) == snapshots[name]["sha256"], name
        fresh = Path(_download_to_temp(client, project_id, stem_entries[name]["asset_id"]))
        integrated, true_peak = _measure_loudness(fresh)
        assert abs(integrated - snapshots[name]["integrated_loudness_lufs"]) < 0.1
        assert abs(true_peak - snapshots[name]["true_peak_dbfs"]) < 0.1

    # --- 3) Rerender: excluye SOLO la causa borrada (verificación de audio) --
    rerendered = client.post(f"/studio/api/projects/{project_id}/sound/render")
    assert rerendered.status_code == 200, rerendered.text
    rerender_body = rerendered.json()
    rerender_entries = {stem["stem"]: stem for stem in rerender_body["stems"]}
    # El stem del actor se regeneró sin el evento de la causa borrada...
    assert rerender_entries["actor"]["event_ids"] == ["step-2"]
    assert rerender_entries["actor"]["asset_id"] != old_actor_asset_id
    # ...y el índice vuelve a reportar todos los stems regenerados (no stale).
    index_after = {
        entry["stem"]: entry
        for entry in client.get(f"/studio/api/projects/{project_id}/sound/stems").json()["stems"]
    }
    assert set(index_after) == {"actor", "speech", "ambience"}
    assert all(entry["stale"] is False for entry in index_after.values())

    new_actor_path = Path(
        _download_to_temp(client, project_id, rerender_entries["actor"]["asset_id"])
    )
    old_actor_path = wav_paths["actor"]
    # Contenido: la ventana del evento BORRADO queda en silencio digital...
    old_deleted_rms = _window_rms_s16(old_actor_path, *DELETED_EVENT_WINDOW)
    new_deleted_rms = _window_rms_s16(new_actor_path, *DELETED_EVENT_WINDOW)
    assert old_deleted_rms > SOUND_RMS_MIN
    assert new_deleted_rms < SILENT_RMS_MAX, (
        f"el rerender aún suena en la ventana de la causa borrada: {new_deleted_rms}"
    )
    # ...y la del evento RETENIDO sigue sonando en ambos renders.
    for path in (old_actor_path, new_actor_path):
        assert _window_rms_s16(path, *KEPT_EVENT_WINDOW) > SOUND_RMS_MIN
    # El nuevo stem del actor también reporta mediciones reales.
    _assert_measured_fields(client, project_id, rerender_entries["actor"])

    # Los stems ajenos re-renderizados son byte-idénticos (determinismo) y sus
    # métricas no cambiaron: el rerender no alteró audio no vinculado.
    for name in ("speech", "ambience"):
        regenerated = _asset_file(client, project_id, rerender_entries[name]["asset_id"])
        assert _sha256(regenerated) == snapshots[name]["sha256"], name
        assert rerender_entries[name]["integrated_loudness_lufs"] == (
            snapshots[name]["integrated_loudness_lufs"]
        )
        assert rerender_entries[name]["true_peak_dbfs"] == (
            snapshots[name]["true_peak_dbfs"]
        )
        assert rerender_entries[name]["clipped"] is False
        assert rerender_entries[name]["sample_rate"] == TARGET_SAMPLE_RATE

    # --- 4) Segunda causa: vacía el stem del actor → su asset se ELIMINA -----
    emptied = client.delete(
        f"/studio/api/projects/{project_id}/sound/causes/actor-clap"
    )
    assert emptied.status_code == 200, emptied.text
    emptied_body = emptied.json()
    assert emptied_body["removed_event_ids"] == ["step-2"]
    assert emptied_body["removed_stem_asset_ids"] == [
        rerender_entries["actor"]["asset_id"]
    ]
    assert emptied_body["stale_stems"] == []
    assert (
        client.get(
            f"/studio/api/projects/{project_id}/assets/{rerender_entries['actor']['asset_id']}/file"
        ).status_code
        == 404
    )
    final_index = {
        entry["stem"]
        for entry in client.get(f"/studio/api/projects/{project_id}/sound/stems").json()["stems"]
    }
    assert final_index == {"speech", "ambience"}
    remaining_events = {
        ev["event_id"] for events in emptied_body["stems"].values() for ev in events
    }
    assert remaining_events == {"line-1", "wind"}
    # Los ajenos siguen intactos tras las dos eliminaciones causales.
    for name in ("speech", "ambience"):
        assert (
            _sha256(_asset_file(client, project_id, rerender_entries[name]["asset_id"]))
            == snapshots[name]["sha256"]
        ), name
