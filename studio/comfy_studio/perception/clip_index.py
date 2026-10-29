"""Índice semántico visual CLIP ViT-L/14 sobre sqlite-vec (VAL-PERC-002)."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from pathlib import Path
from typing import Any

from .. import SIMULATED_ML_NOTICE
from ..ml import is_real_backend, require_real_payload, run_worker
from ..projects import ProjectStore
from .keyframes import extract_image_rgb_signature, extract_video_keyframes
from .store import CLIP_EMBED_DIM, PerceptionVectorStore

CLIP_MODEL_ID = "openai/clip-vit-large-patch14"
CLIP_LOCAL_REL_DIR = "models/clip/clip-vit-large-patch14"

# Ejes conceptuales compartidos imagen <-> texto (dimensiones 0..15)
_CONCEPT_AXES: dict[int, set[str]] = {
    0: {
        "rojo",
        "roja",
        "rojos",
        "red",
        "fuego",
        "marte",
        "atardecer",
        "calido",
        "carmesí",
        "carmesi",
        "crimson",
        "lava",
        "incendio",
    },
    1: {
        "verde",
        "verdes",
        "green",
        "bosque",
        "selva",
        "campo",
        "naturaleza",
        "planta",
        "hierba",
        "prado",
        "esmeralda",
    },
    2: {
        "azul",
        "azules",
        "blue",
        "oceano",
        "mar",
        "cielo",
        "agua",
        "playa",
        "costa",
        "marino",
        "ola",
        "olas",
    },
    3: {
        "amarillo",
        "amarilla",
        "yellow",
        "sol",
        "arena",
        "desierto",
        "dorado",
        "oro",
        "duna",
        "solar",
        "amanecer",
    },
    4: {
        "magenta",
        "purple",
        "violeta",
        "morado",
        "neon",
        "cyberpunk",
        "rosa",
        "pink",
        "fucsia",
    },
    5: {
        "cian",
        "cyan",
        "turquesa",
        "hielo",
        "glaciar",
        "celeste",
        "artico",
    },
    6: {
        "blanco",
        "blanca",
        "white",
        "claro",
        "luminoso",
        "nieve",
        "luz",
        "brillante",
        "dia",
        "iluminado",
    },
    7: {
        "negro",
        "negra",
        "black",
        "oscuro",
        "oscura",
        "noche",
        "espacio",
        "sombra",
        "nocturno",
        "nocturna",
        "cosmos",
    },
    8: {
        "astronauta",
        "espacial",
        "traje",
        "casco",
        "cohete",
        "nave",
        "luna",
    },
    9: {
        "persona",
        "personas",
        "rostro",
        "cara",
        "retrato",
        "sujeto",
        "hablando",
        "locutor",
        "presentador",
        "entrevista",
        "actor",
        "actriz",
    },
    10: {
        "ciudad",
        "calle",
        "edificio",
        "urbano",
        "urbana",
        "rascacielos",
        "avenida",
    },
    11: {
        "interior",
        "estudio",
        "oficina",
        "habitacion",
        "sala",
        "set",
    },
    12: {
        "coche",
        "auto",
        "vehiculo",
        "carretera",
        "autopista",
        "conduciendo",
    },
    13: {
        "montaña",
        "montana",
        "roca",
        "cañon",
        "canon",
        "acantilado",
        "valle",
    },
    14: {
        "faro",
        "tormenta",
        "lluvia",
        "relampago",
        "viento",
        "niebla",
    },
    15: {
        "accion",
        "caminando",
        "corriendo",
        "movimiento",
        "salto",
        "baile",
    },
}


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower().replace("_", " ").replace("-", " ")
    decomposed = unicodedata.normalize("NFD", lowered)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9\s]+", " ", stripped)


def _tokenize(text: str) -> list[str]:
    norm = _normalize_text(text)
    return [tok for tok in norm.split() if len(tok) >= 2]


def _hash_token_to_dim(
    token: str, start_dim: int = 16, end_dim: int = CLIP_EMBED_DIM
) -> tuple[int, float]:
    span = max(1, end_dim - start_dim)
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    dim = start_dim + (int.from_bytes(digest[:4], "big") % span)
    sign = 1.0 if (digest[4] % 2 == 0) else -1.0
    return dim, sign


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-9:
        out = [0.0] * len(vec)
        if out:
            out[0] = 1.0
        return out
    return [v / norm for v in vec]


def embed_text_clip(text: str) -> list[float]:
    """Genera un embedding normalizado para una consulta textual.

    En backend `real` delega en el worker ML (CLIP ViT-L/14, 768-d). En
    `sim`/`auto` usa la simulación determinista actual. Nunca falla silenciosamente:
    si el worker en modo `real` devuelve error, se propaga (sin fallback a sim).
    """
    if is_real_backend():
        resp = run_worker("clip_embed_text", {"text": text})
        payload = require_real_payload(resp, "clip_embed_text")
        emb = payload.get("embedding")
        if not isinstance(emb, list):
            raise RuntimeError(
                f"worker clip_embed_text devolvió payload inválido: {payload}"
            )
        return [float(v) for v in emb]

    vec = [0.0] * CLIP_EMBED_DIM
    tokens = _tokenize(text)
    if not tokens:
        vec[0] = 1.0
        return vec

    token_set = set(tokens)
    for axis_idx, keywords in _CONCEPT_AXES.items():
        overlap = token_set & keywords
        if not overlap:
            # Soportar prefijos (p.ej. 'roja'/'rojos' -> 'roj')
            for tok in token_set:
                if len(tok) >= 4 and any(
                    k.startswith(tok[:4]) or tok.startswith(k[:4]) for k in keywords
                ):
                    overlap = {tok}
                    break
        if overlap:
            vec[axis_idx] += 3.0 * len(overlap)

    for tok in tokens:
        dim, sign = _hash_token_to_dim(tok)
        vec[dim] += 1.5 * sign
        if len(tok) >= 4:
            for i in range(len(tok) - 2):
                trigram = tok[i : i + 3]
                tdim, tsign = _hash_token_to_dim(f"3g:{trigram}")
                vec[tdim] += 0.35 * tsign

    return _l2_normalize(vec)


def embed_image_clip(
    image_path: Path | str | None = None,
    *,
    caption: str = "",
    visual_tags: list[str] | None = None,
    rgb_mean: list[float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[float]:
    """Genera un embedding multimodal CLIP combinando señal visual RGB y semántica.

    En backend `real` delega en el worker ML. En `sim`/`auto` usa la
    simulación determinista actual.
    """
    if is_real_backend():
        payload_req: dict[str, Any] = {
            "caption": caption,
            "visual_tags": visual_tags or [],
            "rgb_mean": rgb_mean,
            "metadata": metadata or {},
        }
        if image_path is not None:
            payload_req["image_path"] = str(image_path)
        resp = run_worker("clip_embed_image", payload_req)
        payload = require_real_payload(resp, "clip_embed_image")
        emb = payload.get("embedding")
        if not isinstance(emb, list):
            raise RuntimeError(
                f"worker clip_embed_image devolvió payload inválido: {payload}"
            )
        return [float(v) for v in emb]

    sig: dict[str, Any] = {}
    if image_path is not None and Path(image_path).is_file():
        sig = extract_image_rgb_signature(image_path)

    eff_rgb = rgb_mean or sig.get("rgb_mean") or [128.0, 128.0, 128.0]
    eff_tags = list(visual_tags or []) + list(sig.get("visual_tags") or [])
    meta_dict = dict(metadata or {})
    for extra_key in ("caption", "description", "scene", "label", "tags", "dominant_color"):
        val = meta_dict.get(extra_key)
        if isinstance(val, str) and val.strip():
            eff_tags.append(val.strip())
        elif isinstance(val, list):
            eff_tags.extend(str(x) for x in val if x)

    combined_text = " ".join([caption, *eff_tags])
    vec = embed_text_clip(combined_text) if combined_text.strip() else [0.0] * CLIP_EMBED_DIM

    # Reforzar directamente los ejes cromáticos (0..7) según los valores RGB reales del frame
    r, g, b = (float(eff_rgb[0]), float(eff_rgb[1]), float(eff_rgb[2]))
    rn, gn, bn = r / 255.0, g / 255.0, b / 255.0
    lum = 0.299 * rn + 0.587 * gn + 0.114 * bn

    vec[0] += max(0.0, rn - max(gn, bn)) * 4.0  # rojo
    vec[1] += max(0.0, gn - max(rn, bn)) * 4.0  # verde
    vec[2] += max(0.0, bn - max(rn, gn)) * 4.0  # azul
    vec[3] += max(0.0, min(rn, gn) - bn) * 3.5  # amarillo
    vec[4] += max(0.0, min(rn, bn) - gn) * 3.5  # magenta
    vec[5] += max(0.0, min(gn, bn) - rn) * 3.5  # cian
    if lum > 0.78:
        vec[6] += (lum - 0.5) * 4.0  # blanco / claro
    elif lum < 0.22:
        vec[7] += (0.5 - lum) * 4.0  # negro / oscuro

    return _l2_normalize(vec)


class ClipVisualIndex:
    """Servicio de indexación y búsqueda semántica texto→shots con CLIP y sqlite-vec."""

    def __init__(
        self,
        store: ProjectStore,
        vector_store: PerceptionVectorStore | None = None,
        *,
        ffmpeg_timeout_s: float | None = None,
        ffprobe_timeout_s: float | None = None,
    ) -> None:
        self.store = store
        self.vector_store = vector_store or PerceptionVectorStore(store.data_dir)
        # M1.5: timeouts configurables inyectados por `StudioConfig`; `None` mantiene
        # los defaults históricos de `keyframes.py`.
        from .keyframes import DEFAULT_FFMPEG_KEYFRAME_TIMEOUT_S, DEFAULT_FFPROBE_TIMEOUT_S

        self.ffmpeg_timeout_s = (
            ffmpeg_timeout_s if ffmpeg_timeout_s is not None else DEFAULT_FFMPEG_KEYFRAME_TIMEOUT_S
        )
        self.ffprobe_timeout_s = (
            ffprobe_timeout_s if ffprobe_timeout_s is not None else DEFAULT_FFPROBE_TIMEOUT_S
        )

    def index_keyframe_asset(
        self,
        project_id: str,
        keyframe_asset: dict[str, Any],
        *,
        caption_override: str | None = None,
        extra_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        aid = keyframe_asset["id"]
        path = self.store.get_asset_path(project_id, aid)
        meta = dict(keyframe_asset.get("metadata") or {})
        source_asset_id = str(meta.get("source_asset_id") or aid)
        ts_sec = float(meta.get("timestamp_sec") or 0.0)
        shot_start = float(
            meta.get("shot_start_sec") if meta.get("shot_start_sec") is not None else ts_sec
        )
        shot_end = float(
            meta.get("shot_end_sec") if meta.get("shot_end_sec") is not None else (shot_start + 2.0)
        )

        visual_tags = list(meta.get("visual_tags") or [])
        if extra_tags:
            for t in extra_tags:
                if t not in visual_tags:
                    visual_tags.append(t)

        caption = (
            caption_override
            or str(meta.get("caption") or "")
            or str(keyframe_asset.get("original_filename") or "")
        )
        rgb_mean = meta.get("rgb_mean")

        if not visual_tags and path is not None and Path(path).is_file():
            sig_info = extract_image_rgb_signature(path)
            visual_tags = list(sig_info.get("visual_tags") or [])
            if not rgb_mean:
                rgb_mean = sig_info.get("rgb_mean")

        emb = embed_image_clip(
            path,
            caption=caption,
            visual_tags=visual_tags,
            rgb_mean=rgb_mean if isinstance(rgb_mean, list) else None,
            metadata=meta,
        )

        record = self.vector_store.upsert_clip_shot(
            project_id=project_id,
            source_asset_id=source_asset_id,
            keyframe_asset_id=aid,
            timestamp_sec=ts_sec,
            shot_start_sec=shot_start,
            shot_end_sec=shot_end,
            embedding=emb,
            caption=caption,
            visual_tags=visual_tags,
            metadata={
                **meta,
                "tags": visual_tags,
                "visual_tags": visual_tags,
                "filename": keyframe_asset.get("filename"),
                "original_filename": keyframe_asset.get("original_filename"),
                "preview_url": keyframe_asset.get("preview_url"),
                "view_url": keyframe_asset.get("view_url"),
            },
        )
        return record

    def index_asset(
        self,
        project_id: str,
        asset_id: str,
        *,
        scene_threshold: float = 0.25,
        interval_sec: float = 2.0,
        caption_override: str | None = None,
    ) -> dict[str, Any]:
        """Extrae keyframes si es vídeo e indexa un único asset en `index.db`."""
        captions_map = {asset_id: caption_override} if caption_override else None
        res = self.index_project_media(
            project_id,
            asset_ids=[asset_id],
            scene_threshold=scene_threshold,
            interval_sec=interval_sec,
            captions_by_asset_id=captions_map,
        )
        return {
            **res,
            "asset_id": asset_id,
        }

    def index_project_media(
        self,
        project_id: str,
        *,
        asset_ids: list[str] | None = None,
        extract_missing_keyframes: bool = True,
        scene_threshold: float = 0.25,
        interval_sec: float = 2.0,
        captions_by_asset_id: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Extrae keyframes si faltan e indexa los planos en sqlite-vec (`index.db`)."""
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")

        all_assets = self.store.list_assets(project_id)
        if asset_ids:
            target_set = set(asset_ids)
            selected_assets = [a for a in all_assets if a["id"] in target_set]
            if not selected_assets:
                raise ValueError(
                    f"Ninguno de los asset_ids indicados existe en el proyecto {project_id!r}."
                )
        else:
            selected_assets = all_assets

        extracted_keyframes: list[dict[str, Any]] = []
        if extract_missing_keyframes:
            existing_kfs = self.store.list_assets(project_id, category="keyframes")
            sources_with_kfs = {
                str((kf.get("metadata") or {}).get("source_asset_id") or "") for kf in existing_kfs
            }
            for asset in selected_assets:
                if asset.get("kind") == "video" and (
                    asset_ids is not None or asset["id"] not in sources_with_kfs
                ):
                    kfs = extract_video_keyframes(
                        self.store,
                        project_id,
                        asset["id"],
                        scene_threshold=scene_threshold,
                        interval_sec=interval_sec,
                        ffmpeg_timeout_s=self.ffmpeg_timeout_s,
                        ffprobe_timeout_s=self.ffprobe_timeout_s,
                    )
                    extracted_keyframes.extend(kfs)

        # Recargar keyframes del proyecto para indexarlos
        keyframes_to_index: list[dict[str, Any]] = []
        current_kfs = self.store.list_assets(project_id, category="keyframes")
        if asset_ids:
            target_set = set(asset_ids)
            for kf in current_kfs:
                src_id = str((kf.get("metadata") or {}).get("source_asset_id") or "")
                if kf["id"] in target_set or src_id in target_set:
                    keyframes_to_index.append(kf)
            indexed_kf_ids = {k["id"] for k in keyframes_to_index}
            for asset in selected_assets:
                if asset.get("kind") == "image" and asset["id"] not in indexed_kf_ids:
                    keyframes_to_index.append(asset)
        else:
            keyframes_to_index = list(current_kfs)
            indexed_kf_ids = {k["id"] for k in keyframes_to_index}
            for asset in selected_assets:
                if asset.get("kind") == "image" and asset["id"] not in indexed_kf_ids:
                    keyframes_to_index.append(asset)

        indexed_shots: list[dict[str, Any]] = []
        skipped_synthetic = 0
        captions_map = captions_by_asset_id or {}
        for kf_asset in keyframes_to_index:
            # M1.7 (M3-SCR-007): un keyframe marcado `synthetic` es un PNG de relleno,
            # no un fotograma del clip. Indexarlo haría que la búsqueda semántica
            # "encontrase" planos inexistentes con embeddings de un color sólido.
            if bool((kf_asset.get("metadata") or {}).get("synthetic")):
                skipped_synthetic += 1
                continue
            cap_override = captions_map.get(kf_asset["id"])
            shot = self.index_keyframe_asset(
                project_id,
                kf_asset,
                caption_override=cap_override,
            )
            indexed_shots.append(shot)

        results_by_asset: dict[str, list[dict[str, Any]]] = {}
        for shot in indexed_shots:
            src_id = str(shot["source_asset_id"])
            results_by_asset.setdefault(src_id, []).append(shot)
        results_list = [
            {"asset_id": src_id, "shots": shots_list}
            for src_id, shots_list in results_by_asset.items()
        ]

        return {
            "project_id": project_id,
            "model": CLIP_MODEL_ID,
            "embed_dim": CLIP_EMBED_DIM,
            "assets_indexed": len(results_list),
            "extracted_keyframes_count": len(extracted_keyframes),
            "indexed_shots_count": len(indexed_shots),
            "shots_indexed": len(indexed_shots),
            "skipped_synthetic_count": skipped_synthetic,
            "shots": indexed_shots,
            "results": results_list,
            # M3-IDEA-005: los embeddings son de un extractor determinista local,
            # no de CLIP ViT-L/14 real (M3-IDEA-002 pendiente).
            "simulated": True,
            "simulation_notice": SIMULATED_ML_NOTICE,
        }

    def find_shots_text(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int = 5,
        limit: int | None = None,
        source_asset_id: str | None = None,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Busca planos en `index.db` (sqlite-vec) ordenados por similitud semántica con `query`."""
        if self.store.get_project(project_id) is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")

        eff_top_k = int(limit) if limit is not None else int(top_k)
        eff_source_id = asset_id if asset_id is not None else source_asset_id
        cleaned = (query or "").strip()
        if not cleaned:
            hits = self.vector_store.list_clip_shots(
                project_id,
                source_asset_id=eff_source_id,
            )[: max(1, eff_top_k)]
            for rank, item in enumerate(hits, start=1):
                item["rank"] = rank
                item.setdefault("similarity", 1.0)
                item.setdefault("score", 1.0)
                item.setdefault("distance", 0.0)
            return hits

        query_emb = embed_text_clip(cleaned)
        hits = self.vector_store.search_clip_shots(
            project_id,
            query_emb,
            top_k=eff_top_k,
            source_asset_id=eff_source_id,
        )
        for rank, item in enumerate(hits, start=1):
            item["rank"] = rank
            kf_asset = self.store.get_asset(project_id, item["keyframe_asset_id"])
            if kf_asset is not None:
                item["preview_url"] = kf_asset["preview_url"]
                item["view_url"] = kf_asset["view_url"]
                item["download_url"] = kf_asset["download_url"]
                item["rel_path"] = kf_asset["rel_path"]
        return hits
