"""Módulo de percepción visual y facial de Comfy Studio (M3: Keyframes, CLIP, InsightFace)."""

from __future__ import annotations

from .clip_index import (
    CLIP_LOCAL_REL_DIR,
    CLIP_MODEL_ID,
    ClipVisualIndex,
    embed_image_clip,
    embed_text_clip,
)
from .face_index import (
    INSIGHTFACE_DET_ONNX_REL,
    INSIGHTFACE_LICENSE_NOTE,
    INSIGHTFACE_MODEL_PACK,
    INSIGHTFACE_NC_LICENSE_NOTICE,
    FaceIndex,
    InsightFaceIndex,
    combined_search,
    compute_face_embedding_from_signature,
    detect_faces_in_image,
)
from .keyframes import (
    classify_rgb_semantic_tags,
    extract_image_rgb_signature,
    extract_video_keyframes,
    probe_media_info,
)
from .store import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    PerceptionVectorStore,
    cosine_similarity,
    pack_float32_vector,
    unpack_float32_vector,
)

__all__ = [
    "CLIP_EMBED_DIM",
    "CLIP_LOCAL_REL_DIR",
    "CLIP_MODEL_ID",
    "FACE_EMBED_DIM",
    "INSIGHTFACE_DET_ONNX_REL",
    "INSIGHTFACE_LICENSE_NOTE",
    "INSIGHTFACE_MODEL_PACK",
    "INSIGHTFACE_NC_LICENSE_NOTICE",
    "ClipVisualIndex",
    "FaceIndex",
    "InsightFaceIndex",
    "PerceptionVectorStore",
    "classify_rgb_semantic_tags",
    "combined_search",
    "compute_face_embedding_from_signature",
    "cosine_similarity",
    "detect_faces_in_image",
    "embed_image_clip",
    "embed_text_clip",
    "extract_image_rgb_signature",
    "extract_video_keyframes",
    "pack_float32_vector",
    "probe_media_info",
    "unpack_float32_vector",
]
