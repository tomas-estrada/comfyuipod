"""Backends reales de ML para el worker (M3-IDEA-001..004).

Cada backend usa imports perezosos: si la librería o los pesos no están
presentes en este pod, la función devuelve `None` y el worker cae al
payload simulado determinista (etiquetado `simulated: True`). Nunca se
falla en silencio: en modo `real`, `require_real_payload` eleva el error.

Backends:
  - CLIP ViT-L/14 (open_clip) para embeddings texto/imagen de 768-d.
  - InsightFace buffalo_l para detección + embeddings faciales de 512-d.
  - faster-whisper large-v3 para transcripción ASR.
  - FunASR cam++ para diarización.

Todos los modelos se resuelven bajo `COMFY_DATA_DIR` (pesos descargados
por las fases de download-models.sh, nunca en runtime por red).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SIMULATED_ML_NOTICE_LOCAL = (
    "ML simulado determinista: la librería o los pesos reales no están "
    "disponibles en este entorno. No evalúes calidad de modelo con esta salida."
)


def _comfy_data_dir() -> Path:
    return Path(os.environ.get("COMFY_DATA_DIR") or "/workspace/comfyui-data")


def _clip_model_dir() -> Path:
    return _comfy_data_dir() / "models" / "clip" / "clip-vit-large-patch14"


def _insightface_root() -> Path:
    return _comfy_data_dir() / "models" / "insightface" / "models"


def _whisper_model_dir() -> Path:
    return _comfy_data_dir() / "models" / "faster_whisper" / "large-v3"


# ---------------------------------------------------------------------------
# CLIP ViT-L/14 (open_clip) — 768-d text/image embeddings
# ---------------------------------------------------------------------------

_CLIP_CACHE: dict[str, Any] = {}


def _load_clip() -> tuple[Any, Any, Any] | None:
    if "clip" in _CLIP_CACHE:
        return _CLIP_CACHE["clip"]
    try:
        import open_clip  # noqa: PLC0415
        import torch  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    model_dir = _clip_model_dir()
    if not model_dir.is_dir():
        return None
    try:
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-L-14", pretrained=str(model_dir)
        )
    except Exception:  # noqa: BLE001
        return None
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    tokenizer = open_clip.get_tokenizer("ViT-L-14")
    _CLIP_CACHE["clip"] = (model, preprocess, tokenizer, device)
    return _CLIP_CACHE["clip"]


def clip_embed_text(text: str) -> dict[str, Any] | None:
    loaded = _load_clip()
    if loaded is None:
        return None
    model, _, tokenizer, device = loaded
    import torch  # noqa: PLC0415

    with torch.no_grad():
        tokens = tokenizer([text or ""]).to(device)
        features = model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
        vec = features[0].float().cpu().tolist()
    return {
        "embedding": vec,
        "dim": len(vec),
        "model": "openai/clip-vit-large-patch14",
        "simulated": False,
    }


def clip_embed_image(image_path: str | Path) -> dict[str, Any] | None:
    loaded = _load_clip()
    if loaded is None:
        return None
    model, preprocess, _, device = loaded
    import torch  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    with torch.no_grad():
        tensor = preprocess(img).unsqueeze(0).to(device)
        features = model.encode_image(tensor)
        features = features / features.norm(dim=-1, keepdim=True)
        vec = features[0].float().cpu().tolist()
    return {
        "embedding": vec,
        "dim": len(vec),
        "model": "openai/clip-vit-large-patch14",
        "simulated": False,
    }


# ---------------------------------------------------------------------------
# InsightFace buffalo_l — detección + embeddings faciales 512-d
# ---------------------------------------------------------------------------

_INSIGHTFACE_CACHE: dict[str, Any] = {}


def _load_insightface() -> Any | None:
    if "app" in _INSIGHTFACE_CACHE:
        return _INSIGHTFACE_CACHE["app"]
    root = _insightface_root()
    det_onnx = root / "buffalo_l" / "det_10g.onnx"
    if not det_onnx.is_file():
        return None
    try:
        from insightface.app import FaceAnalysis  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        app = FaceAnalysis(name="buffalo_l", root=str(root), providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(640, 640))
    except Exception:  # noqa: BLE001
        return None
    _INSIGHTFACE_CACHE["app"] = app
    return app


def faces_detect(image_path: str | Path) -> dict[str, Any] | None:
    app = _load_insightface()
    if app is None:
        return None
    try:
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        img = np.asarray(Image.open(image_path).convert("RGB"))
    except Exception:  # noqa: BLE001
        return None
    faces_raw = app.get(img)
    faces_out: list[dict[str, Any]] = []
    for face in faces_raw:
        embedding = face.normed_embedding
        if embedding is None:
            continue
        x1, y1, x2, y2 = face.bbox.tolist()
        h, w = img.shape[:2]
        faces_out.append(
            {
                "bbox": [
                    max(0.0, x1 / w),
                    max(0.0, y1 / h),
                    min(1.0, x2 / w),
                    min(1.0, y2 / h),
                ],
                "det_score": float(face.det_score),
                "embedding": [float(v) for v in embedding],
                "dim": int(embedding.shape[0]),
            }
        )
    return {
        "faces": faces_out,
        "model_pack": "buffalo_l",
        "dim": 512,
        "simulated": False,
    }


# ---------------------------------------------------------------------------
# faster-whisper large-v3 — transcripción ASR
# ---------------------------------------------------------------------------

_ASR_CACHE: dict[str, Any] = {}


def _load_whisper() -> Any | None:
    if "model" in _ASR_CACHE:
        return _ASR_CACHE["model"]
    model_dir = _whisper_model_dir()
    if not model_dir.is_dir():
        return None
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        device = "cuda" if _cuda_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
    except Exception:  # noqa: BLE001
        return None
    _ASR_CACHE["model"] = model
    return model


def _cuda_available() -> bool:
    try:
        import torch  # noqa: PLC0415

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def asr_transcribe(audio_path: str | Path, *, language: str | None = None) -> dict[str, Any] | None:
    model = _load_whisper()
    if model is None:
        return None
    try:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language or None,
            vad_filter=True,
            word_timestamps=True,
        )
        segments = list(segments_iter)
    except Exception:  # noqa: BLE001
        return None
    seg_out: list[dict[str, Any]] = []
    words_out: list[dict[str, Any]] = []
    full_text_parts: list[str] = []
    for seg in segments:
        seg_out.append(
            {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": seg.text.strip(),
                "no_speech_prob": float(getattr(seg, "no_speech_prob", 0.0)),
            }
        )
        full_text_parts.append(seg.text.strip())
        for word in getattr(seg, "words", None) or []:
            words_out.append(
                {
                    "start": float(word.start),
                    "end": float(word.end),
                    "word": word.word.strip(),
                    "probability": float(word.probability),
                }
            )
    return {
        "text": " ".join(p for p in full_text_parts if p).strip(),
        "segments": seg_out,
        "words": words_out,
        "language": getattr(info, "language", None) or language or "es",
        "backend": "faster-whisper",
        "model": "large-v3",
        "simulated": False,
    }


# ---------------------------------------------------------------------------
# FunASR cam++ — diarización
# ---------------------------------------------------------------------------


def diarize(audio_path: str | Path) -> dict[str, Any] | None:
    try:
        from funasr import AutoModel  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    campplus = (
        _comfy_data_dir() / "models" / "funasr" / "campplus" / "campplus_cn_common.bin"
    )
    if not campplus.is_file():
        return None
    try:
        model = AutoModel(model=str(campplus.parent))
        res = model.generate(input=str(audio_path))
    except Exception:  # noqa: BLE001
        return None
    speakers: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    by_speaker: dict[str, Any] = {}
    for item in res or []:
        spk = str(item.get("spk", "spk_0")) if isinstance(item, dict) else "spk_0"
        start = float(item.get("start", 0.0)) if isinstance(item, dict) else 0.0
        end = float(item.get("end", 0.0)) if isinstance(item, dict) else 0.0
        turns.append({"speaker": spk, "start": start, "end": end})
        entry = by_speaker.setdefault(spk, {"total_sec": 0.0, "turns": 0})
        entry["total_sec"] += max(0.0, end - start)
        entry["turns"] += 1
    for spk, entry in by_speaker.items():
        speakers.append({"id": spk, **entry})
    return {
        "speakers": speakers,
        "turns": turns,
        "by_speaker": by_speaker,
        "backend": "funasr_campplus",
        "simulated": False,
    }
