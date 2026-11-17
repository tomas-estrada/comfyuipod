from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.asr.whisperx import transcribe_project_asset
from comfy_studio.ml import MlWorkerResponse
from comfy_studio.ml import worker as ml_worker
from comfy_studio.projects import ProjectStore


def _project_with_audio(tmp_path: Path) -> tuple[ProjectStore, str, str]:
    store = ProjectStore(tmp_path / "studio")
    project_id = store.create_project(name="ASR persistence")["id"]
    asset = store.save_upload(
        project_id,
        filename="audio.wav",
        content_type="audio/wav",
        data=b"audio-fixture",
        max_upload_bytes=1024,
        category="uploads",
    )
    return store, project_id, asset["id"]


def test_real_transcription_is_normalized_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project_id, asset_id = _project_with_audio(tmp_path)
    monkeypatch.setattr("comfy_studio.asr.whisperx.is_real_backend", lambda: True)
    monkeypatch.setattr(
        "comfy_studio.asr.whisperx.run_worker",
        lambda *_args, **_kwargs: MlWorkerResponse(
            ok=True,
            subcommand="asr_transcribe",
            payload={
                "text": "Hola mundo",
                "language": "es",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "Hola mundo",
                        "words": [
                            {"word": "Hola", "start": 0.0, "end": 0.4, "probability": 0.9},
                            {"word": "mundo", "start": 0.4, "end": 1.0, "probability": 0.8},
                        ],
                    }
                ],
                "duration_sec": 1.0,
                "backend": "faster-whisper",
                "model": "large-v3",
                "simulated": False,
            },
        ),
    )

    result = transcribe_project_asset(store, project_id, asset_id)

    persisted = store.get_transcription(project_id, result["id"])
    assert persisted is not None
    assert persisted["asset_id"] == asset_id
    assert persisted["text"] == "Hola mundo"
    assert [word["word"] for word in persisted["words"]] == ["Hola", "mundo"]
    assert persisted["compute_config"]["asr"]["simulated"] is False
    assert result["simulated"] is False
    assert "simulation_notice" not in result


def test_real_transcription_surfaces_missing_backend_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project_id, asset_id = _project_with_audio(tmp_path)
    monkeypatch.setattr("comfy_studio.asr.whisperx.is_real_backend", lambda: True)
    monkeypatch.setattr(
        "comfy_studio.asr.whisperx.run_worker",
        lambda *_args, **_kwargs: MlWorkerResponse(
            ok=True,
            subcommand="asr_transcribe",
            payload={
                "segments": [],
                "words": [],
                "simulated": True,
                "missing_reason": "faltan los pesos locales de faster-whisper large-v3",
            },
        ),
    )

    with pytest.raises(RuntimeError, match="faltan los pesos locales"):
        transcribe_project_asset(store, project_id, asset_id)


def test_worker_marks_asr_simulation_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml_worker.backends, "asr_transcribe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ml_worker.backends, "_whisper_model_dir", lambda: tmp_path)
    monkeypatch.setattr(ml_worker.importlib.util, "find_spec", lambda _name: None)

    result = ml_worker.cmd_asr_transcribe({"asset_path": "/tmp/audio.wav"})

    assert result["payload"]["simulated"] is True
    assert result["payload"]["missing_reason"] == (
        "el paquete faster-whisper no está instalado en el worker"
    )
