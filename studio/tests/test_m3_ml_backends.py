"""Tests de M3: workers de ML, selección de backend y versión de índice."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.ml import (
    ML_DEFAULT_TIMEOUTS,
    ML_ENV_ALLOWLIST,
    is_real_backend,
    require_real_payload,
    resolve_perception_backend,
    run_worker,
)
from comfy_studio.ml.runner import MlWorkerResponse
from comfy_studio.perception.store import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    PerceptionVectorStore,
    pack_float32_vector,
    unpack_float32_vector,
)

# ---------------------------------------------------------------------------
# resolve_perception_backend / is_real_backend
# ---------------------------------------------------------------------------


def test_resolve_perception_backend_defaults_to_auto(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STUDIO_PERCEPTION_BACKEND", raising=False)
    assert resolve_perception_backend() == "auto"


def test_resolve_perception_backend_values(monkeypatch: pytest.MonkeyPatch):
    for val in ["auto", "real", "sim"]:
        monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", val)
        assert resolve_perception_backend() == val


def test_is_real_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "real")
    assert is_real_backend() is True
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "sim")
    assert is_real_backend() is False
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "auto")
    assert is_real_backend() is False


# ---------------------------------------------------------------------------
# run_worker
# ---------------------------------------------------------------------------


def test_run_worker_probe_returns_typed_response():
    resp = run_worker("probe", {})
    assert isinstance(resp, MlWorkerResponse)
    assert resp.subcommand == "probe"
    assert isinstance(resp.ok, bool)
    assert isinstance(resp.payload, dict)
    assert "backends" in resp.payload
    assert "weights_ok" in resp.payload
    assert "probe_ok" in resp.payload


def test_run_worker_unknown_subcommand_returns_error():
    resp = run_worker("no_such_subcommand", {})
    assert resp.ok is False
    assert resp.subcommand == "no_such_subcommand"
    assert resp.error is not None


def test_run_worker_env_allowlist_no_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_LLM_KEY", "sk-secret-key-12345")
    monkeypatch.setenv("COMFY_PASS", "super-secret-pass")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = run_worker.__globals__["_build_env"]()
    assert "STUDIO_LLM_KEY" not in env
    assert "COMFY_PASS" not in env
    assert "PATH" in env


# ---------------------------------------------------------------------------
# require_real_payload
# ---------------------------------------------------------------------------


def test_require_real_payload_ok_response():
    resp = MlWorkerResponse(
        ok=True,
        subcommand="clip_embed_text",
        payload={"embedding": [0.0] * 768, "simulated": False},
    )
    payload = require_real_payload(resp, "clip_embed_text")
    assert payload["simulated"] is False


def test_require_real_payload_error_response():
    resp = MlWorkerResponse(
        ok=False,
        subcommand="clip_embed_text",
        error="modelo no encontrado",
        exit_code=1,
    )
    with pytest.raises(RuntimeError, match="worker clip_embed_text falló"):
        require_real_payload(resp, "clip_embed_text")


def test_require_real_payload_simulated_in_real_mode():
    resp = MlWorkerResponse(
        ok=True,
        subcommand="clip_embed_text",
        payload={"embedding": [0.0] * 768, "simulated": True},
    )
    with pytest.raises(RuntimeError, match="modo real devolvió simulación"):
        require_real_payload(resp, "clip_embed_text")


# ---------------------------------------------------------------------------
# Health endpoint: backend reporting
# ---------------------------------------------------------------------------


def test_health_reports_sim_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "sim")
    app = create_app()
    client = TestClient(app)
    resp = client.get("/studio/api/health")
    assert resp.status_code == 200
    p = resp.json()["perception"]
    assert p["backend"] == "sim"
    assert p["simulated"] is True


def test_health_reports_real_backend_503(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_PERCEPTION_BACKEND", "real")
    app = create_app()
    client = TestClient(app)
    # En este entorno no hay pesos reales, así que real -> 503 en endpoints de percepción
    resp = client.get("/studio/api/projects/proj-1/perception/search", params={"query": "test"})
    assert resp.status_code == 503
    assert "real" in resp.json()["detail"].lower() or "backends" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# schema_meta / stored_dims / pack/unpack with dims
# ---------------------------------------------------------------------------


def test_store_uses_768_512_dims():
    assert CLIP_EMBED_DIM == 768
    assert FACE_EMBED_DIM == 512


def test_pack_unpack_float32_vector_explicit_dim():
    vec = [3.0, 4.0] + [0.0] * 766  # norm = 5.0, normalized -> [0.6, 0.8, 0, ...]
    blob = pack_float32_vector(vec, CLIP_EMBED_DIM)
    assert len(blob) == CLIP_EMBED_DIM * 4
    rt = unpack_float32_vector(blob, CLIP_EMBED_DIM)
    assert abs(rt[0] - 0.6) < 1e-6
    assert abs(rt[1] - 0.8) < 1e-6
    assert rt[2] == 0.0


def test_pack_float32_vector_pads_short_input():
    # [1.0, 2.0] -> norm = sqrt(5) -> normalized -> [0.4472, 0.8944, 0, 0]
    norm = math.sqrt(5.0)
    blob = pack_float32_vector([1.0, 2.0], 4)
    rt = unpack_float32_vector(blob, 4)
    assert abs(rt[0] - 1.0 / norm) < 1e-6
    assert abs(rt[1] - 2.0 / norm) < 1e-6
    assert rt[2] == 0.0
    assert rt[3] == 0.0


def test_schema_meta_initialized_on_new_index(tmp_path: Path):
    db_path = tmp_path / "index.db"
    store = PerceptionVectorStore(db_path)
    meta = store.get_schema_meta()
    assert meta["clip_dim"] == "768"
    assert meta["face_dim"] == "512"
    assert meta["clip_extractor"] == "openai/clip-vit-large-patch14"
    assert meta["face_extractor"] == "buffalo_l/w600k_r50"
    assert "built_at" in meta


def test_stored_dims_returns_stored_values(tmp_path: Path):
    db_path = tmp_path / "index.db"
    store = PerceptionVectorStore(db_path)
    clip_dim, face_dim = store.stored_dims()
    assert clip_dim == 768
    assert face_dim == 512


def test_stored_dims_mismatch_detected(tmp_path: Path):
    db_path = tmp_path / "index.db"
    store = PerceptionVectorStore(db_path)
    store.set_schema_meta("clip_dim", "64")
    store.set_schema_meta("face_dim", "64")
    clip_dim, face_dim = store.stored_dims()
    assert clip_dim == 64
    assert face_dim == 64
    assert clip_dim != CLIP_EMBED_DIM
    assert face_dim != FACE_EMBED_DIM


# ---------------------------------------------------------------------------
# ML timeouts and allowlist constants
# ---------------------------------------------------------------------------


def test_ml_default_timeouts():
    assert ML_DEFAULT_TIMEOUTS["probe"] == 30.0
    assert ML_DEFAULT_TIMEOUTS["asr_transcribe"] == 300.0
    assert ML_DEFAULT_TIMEOUTS["clip_embed_text"] == 120.0


def test_ml_env_allowlist_excludes_secrets():
    secrets = {"STUDIO_LLM_KEY", "COMFY_PASS", "STUDIO_API_TOKEN", "STUDIO_WEBHOOK_SECRET"}
    assert secrets.isdisjoint(ML_ENV_ALLOWLIST)


# ---------------------------------------------------------------------------
# worker.py probe as subprocess (CI-style smoke)
# ---------------------------------------------------------------------------


def test_worker_probe_subprocess_exits_zero():
    worker = str(Path(__file__).resolve().parent.parent / "comfy_studio" / "ml" / "worker.py")
    env = {k: v for k, v in os.environ.items() if k in ML_ENV_ALLOWLIST}
    env["COMFY_DATA_DIR"] = os.environ.get("COMFY_DATA_DIR", "")
    res = subprocess.run(
        [sys.executable, worker, "probe"],
        input=json.dumps({}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert res.returncode == 0, f"worker probe falló: {res.stderr}"
    parsed = json.loads(res.stdout)
    assert parsed["ok"] is True or parsed["ok"] is False
    assert "subcommand" in parsed


def test_worker_clip_embed_text_subprocess():
    worker = str(Path(__file__).resolve().parent.parent / "comfy_studio" / "ml" / "worker.py")
    env = {k: v for k, v in os.environ.items() if k in ML_ENV_ALLOWLIST}
    env["COMFY_DATA_DIR"] = os.environ.get("COMFY_DATA_DIR", "")
    res = subprocess.run(
        [sys.executable, worker, "clip_embed_text"],
        input=json.dumps({"text": "hola mundo"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env=env,
    )
    assert res.returncode == 0, f"worker clip_embed_text falló: {res.stderr}"
    parsed = json.loads(res.stdout)
    assert parsed["ok"] is True
    emb = parsed["payload"]["embedding"]
    assert len(emb) == 768

