"""Degradación global: sin backend nadie finge resultados GPU (sin GPU)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.ml.probe import probe_capabilities
from comfy_studio.ml.runner import MlWorkerResponse
from comfy_studio.perception.clip_index import embed_image_clip, embed_text_clip
from comfy_studio.perception.face_index import detect_faces_in_image
from comfy_studio.perception.store import CLIP_EMBED_DIM, PerceptionVectorStore

# --- backend ausente, modo sim/auto: degradación honesta --------------------


def test_sim_sin_pesos_embed_text_determinista(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "sim")
    emb = embed_text_clip("plano con gorra verde en bosque")
    assert len(emb) == CLIP_EMBED_DIM
    assert embed_text_clip("plano con gorra verde en bosque") == emb


def test_sim_sin_caras_explicitas_sin_fingir_deteccion_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "sim")
    assert detect_faces_in_image(None, metadata={"no_faces": True}) == []


def test_indice_vacio_no_finge_vecinos(tmp_path: Path) -> None:
    store = PerceptionVectorStore(tmp_path)
    assert store.search_clip_shots("proj", [1.0] + [0.0] * (CLIP_EMBED_DIM - 1)) == []
    assert store.search_face_detections("proj", [1.0] + [0.0] * 511) == []


def test_probe_sin_pesos_probe_ok_false(tmp_path: Path) -> None:
    caps = probe_capabilities(data_dir=tmp_path)
    assert caps.probe_ok is False
    assert caps.available_backends == []
    assert caps.reason


# --- modo real: fail-closed, nunca simulación silenciosa --------------------


def _resp_simulada(subcommand: str) -> MlWorkerResponse:
    return MlWorkerResponse(
        ok=True,
        subcommand=subcommand,
        payload={
            "embedding": [0.0] * CLIP_EMBED_DIM,
            "dim": CLIP_EMBED_DIM,
            "simulated": True,
            "simulation_notice": "ML simulado",
        },
    )


def _resp_fallo(subcommand: str) -> MlWorkerResponse:
    return MlWorkerResponse(
        ok=False,
        subcommand=subcommand,
        error="worker sin pesos reales",
        exit_code=1,
    )


def test_real_con_simulacion_rechaza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "real")
    monkeypatch.setattr(
        "comfy_studio.perception.clip_index.run_worker",
        lambda *a, **k: _resp_simulada("clip_embed_text"),
    )
    with pytest.raises(RuntimeError, match="modo real devolvió simulación"):
        embed_text_clip("verde")


def test_real_con_fallo_rechaza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "real")
    monkeypatch.setattr(
        "comfy_studio.perception.clip_index.run_worker",
        lambda *a, **k: _resp_fallo("clip_embed_text"),
    )
    with pytest.raises(RuntimeError, match="worker clip_embed_text falló"):
        embed_text_clip("verde")


def test_real_imagen_con_simulacion_rechaza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "real")
    monkeypatch.setattr(
        "comfy_studio.perception.clip_index.run_worker",
        lambda *a, **k: _resp_simulada("clip_embed_image"),
    )
    with pytest.raises(RuntimeError, match="modo real devolvió simulación"):
        embed_image_clip(caption="verde")


def test_real_caras_con_simulacion_rechaza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "real")

    def _fake(*a, **k):  # noqa: ANN001, ANN002
        return MlWorkerResponse(
            ok=True,
            subcommand="faces_detect",
            payload={"faces": [], "dim": 512, "simulated": True},
        )

    monkeypatch.setattr(
        "comfy_studio.perception.face_index.run_worker", _fake
    )
    with pytest.raises(RuntimeError, match="modo real devolvió simulación"):
        detect_faces_in_image(None, metadata={})
