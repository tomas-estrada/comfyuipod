"""Índice de identidades faciales InsightFace buffalo_l sobre sqlite-vec (VAL-PERC-003).

Nota de licencia: InsightFace `buffalo_l` está sujeto a licencia no comercial
(investigación y uso personal no comercial). Ver `docs/decisiones.md`.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from .. import SIMULATED_ML_NOTICE
from ..ml import is_real_backend, require_real_payload, run_worker
from ..projects import ProjectStore
from .clip_index import ClipVisualIndex
from .keyframes import extract_image_rgb_signature
from .store import FACE_EMBED_DIM, PerceptionVectorStore, cosine_similarity

INSIGHTFACE_MODEL_PACK = "buffalo_l"
INSIGHTFACE_DET_ONNX_REL = "models/insightface/models/buffalo_l/det_10g.onnx"
INSIGHTFACE_LICENSE_NOTE = (
    "InsightFace buffalo_l: licencia no comercial (uso personal e investigación)."
)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-9:
        out = [0.0] * len(vec)
        if out:
            out[0] = 1.0
        return out
    return [v / norm for v in vec]


def _signature_token_vector(token: str, weight: float = 4.0) -> list[float]:
    vec = [0.0] * FACE_EMBED_DIM
    cleaned = (token or "").strip().lower()
    if not cleaned:
        return vec
    digest = hashlib.sha256(f"face_id:{cleaned}".encode()).digest()
    for i in range(16):
        dim = 32 + ((digest[i] + i * 3) % (FACE_EMBED_DIM - 32))
        sign = 1.0 if (digest[16 + i] % 2 == 0) else -1.0
        vec[dim] += weight * sign
    return vec


def compute_face_embedding_from_signature(
    sig: dict[str, Any],
    *,
    identity_hint: str | None = None,
) -> list[float]:
    """Construye un embedding facial determinista (64-d L2) desde la región central de cara."""
    vec = [0.0] * FACE_EMBED_DIM
    center_rgb = sig.get("center_rgb") or sig.get("rgb_mean") or [128.0, 128.0, 128.0]
    center_cells = sig.get("center_cells") or []

    cr, cg, cb = (
        float(center_rgb[0]) / 255.0,
        float(center_rgb[1]) / 255.0,
        float(center_rgb[2]) / 255.0,
    )

    # Ejes cromáticos/estructurales de la región facial central (independientes del fondo)
    vec[0] = (cr - 0.5) * 6.0
    vec[1] = (cg - 0.5) * 6.0
    vec[2] = (cb - 0.5) * 6.0
    vec[3] = (cr - max(cg, cb)) * 5.0
    vec[4] = (cg - max(cr, cb)) * 5.0
    vec[5] = (cb - max(cr, cg)) * 5.0

    for c_idx, cell_rgb in enumerate(center_cells[:16]):
        base = 6 + c_idx * 3
        if len(cell_rgb) >= 3 and base + 2 < FACE_EMBED_DIM:
            vec[base] = (float(cell_rgb[0]) - 0.5) * 4.0
            vec[base + 1] = (float(cell_rgb[1]) - 0.5) * 4.0
            vec[base + 2] = (float(cell_rgb[2]) - 0.5) * 4.0

    if identity_hint:
        tok_vec = _signature_token_vector(identity_hint, weight=4.5)
        for i in range(FACE_EMBED_DIM):
            vec[i] += tok_vec[i]

    return _l2_normalize(vec)


def detect_faces_in_image(
    image_path: Path | str | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detecta caras y calcula sus embeddings sobre una imagen o keyframe.

    En backend `real` delega en el worker ML (InsightFace buffalo_l, 512-d).
    En `sim`/`auto` usa la simulación determinista actual.
    """
    if is_real_backend():
        payload_req: dict[str, Any] = {"metadata": metadata or {}}
        if image_path is not None:
            payload_req["image_path"] = str(image_path)
        resp = run_worker("faces_detect", payload_req)
        payload = require_real_payload(resp, "faces_detect")
        faces = payload.get("faces")
        if not isinstance(faces, list):
            raise RuntimeError(
                f"worker faces_detect devolvió payload inválido: {payload}"
            )
        return faces

    meta = dict(metadata or {})
    if meta.get("no_faces") is True:
        return []

    sig: dict[str, Any] = {}
    if image_path is not None and Path(image_path).is_file():
        sig = extract_image_rgb_signature(image_path)

    explicit_faces = meta.get("faces")
    if isinstance(explicit_faces, list) and explicit_faces:
        detections: list[dict[str, Any]] = []
        for idx, item in enumerate(explicit_faces):
            if isinstance(item, dict):
                hint = str(
                    item.get("person") or item.get("name") or item.get("signature") or f"face_{idx}"
                )
                bbox = list(item.get("bbox") or [0.25, 0.15, 0.75, 0.85])
                score = float(item.get("det_score") or 0.98)
            else:
                hint = str(item)
                bbox = [0.25, 0.15, 0.75, 0.85]
                score = 0.98
            emb = compute_face_embedding_from_signature(sig, identity_hint=hint)
            detections.append(
                {
                    "bbox": bbox,
                    "det_score": score,
                    "embedding": emb,
                    "person_hint": hint,
                }
            )
        return detections

    hint_str = str(
        meta.get("face_signature") or meta.get("person") or meta.get("identity_name") or ""
    ).strip()

    emb = compute_face_embedding_from_signature(
        sig,
        identity_hint=hint_str or None,
    )
    return [
        {
            "bbox": [0.22, 0.14, 0.78, 0.86],
            "det_score": 0.96,
            "embedding": emb,
            "person_hint": hint_str or None,
        }
    ]


class InsightFaceIndex:
    """Servicio de registro de identidades y búsqueda de planos por cara y texto ∩ persona."""

    def __init__(
        self,
        store: ProjectStore,
        vector_store: PerceptionVectorStore | None = None,
        clip_index: ClipVisualIndex | None = None,
    ) -> None:
        self.store = store
        self.vector_store = vector_store or PerceptionVectorStore(store.data_dir)
        self.clip_index = clip_index or ClipVisualIndex(store, self.vector_store)

    def register_identity(
        self,
        project_id: str,
        *,
        name: str,
        reference_asset_id: str | None = None,
        identity_id: str | None = None,
        face_signature: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Registra o actualiza (renombra) una identidad con nombre y foto de referencia."""
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValueError("El nombre de la persona (`name`) no puede estar vacío.")

        ref_path: Path | None = None
        ref_meta: dict[str, Any] = dict(metadata or {})
        if reference_asset_id is not None:
            ref_asset = self.store.get_asset(project_id, reference_asset_id)
            ref_path = self.store.get_asset_path(project_id, reference_asset_id)
            if ref_asset is None or ref_path is None:
                raise ValueError(
                    f"Asset de referencia facial no encontrado: {reference_asset_id!r}"
                )
            ref_meta = {**(ref_asset.get("metadata") or {}), **ref_meta}

        if face_signature:
            ref_meta["face_signature"] = face_signature

        faces = detect_faces_in_image(ref_path, metadata=ref_meta)
        if not faces:
            raise ValueError(
                "No se detectó ninguna cara válida en la imagen de referencia suministrada."
            )
        rep_embedding = faces[0]["embedding"]

        saved_identity = self.store.save_identity(
            project_id,
            name=cleaned_name,
            reference_asset_id=reference_asset_id,
            identity_id=identity_id,
            metadata={
                **ref_meta,
                "model_pack": INSIGHTFACE_MODEL_PACK,
                "license": INSIGHTFACE_LICENSE_NOTE,
            },
        )
        self.vector_store.upsert_face_identity(
            identity_id=saved_identity["id"],
            project_id=project_id,
            name=saved_identity["name"],
            reference_asset_id=saved_identity["reference_asset_id"],
            embedding=rep_embedding,
            metadata=saved_identity["metadata"],
        )
        return saved_identity

    def rename_identity(
        self,
        project_id: str,
        identity_id: str,
        name: str = "",
    ) -> dict[str, Any] | None:
        """Renombra una identidad existente tanto en `studio.db` como en `index.db`.

        Devuelve `None` (sin lanzar) cuando la identidad no existe: el endpoint lo
        traduce a 404, mientras un nombre inválido sigue siendo 400.
        """
        updated = self.store.rename_identity(project_id, identity_id, name=name)
        if updated is None:
            return None

        vec_id = self.vector_store.get_face_identity(project_id, identity_id)
        if vec_id is not None:
            self.vector_store.upsert_face_identity(
                identity_id=updated["id"],
                project_id=project_id,
                name=updated["name"],
                reference_asset_id=updated["reference_asset_id"],
                embedding=vec_id["embedding"],
                metadata=updated["metadata"],
            )
        return updated

    def detect_and_index_asset(
        self,
        project_id: str,
        asset_id: str,
        *,
        source_asset_id: str | None = None,
        timestamp_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """Detecta e indexa caras en un asset de imagen o keyframe por su `asset_id`."""
        asset = self.store.get_asset(project_id, asset_id)
        if asset is None:
            return []
        if asset.get("kind") != "image":
            return []
        return self.index_keyframe_faces(
            project_id,
            asset,
            source_asset_id=source_asset_id,
            timestamp_sec=timestamp_sec,
        )

    def index_keyframe_faces(
        self,
        project_id: str,
        keyframe_asset: dict[str, Any],
        *,
        source_asset_id: str | None = None,
        timestamp_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """Detecta e indexa caras en un keyframe asociándolas a identidades registradas."""
        aid = keyframe_asset["id"]
        path = self.store.get_asset_path(project_id, aid)
        meta = dict(keyframe_asset.get("metadata") or {})
        eff_source_aid = str(source_asset_id or meta.get("source_asset_id") or aid)
        ts_sec = float(
            timestamp_sec if timestamp_sec is not None else (meta.get("timestamp_sec") or 0.0)
        )

        self.vector_store.clear_keyframe_face_detections(project_id, aid)
        detections = detect_faces_in_image(path, metadata=meta)

        identities = self.store.list_identities(project_id)
        id_embeddings: list[tuple[str, str, list[float]]] = []
        for ident in identities:
            vec_rec = self.vector_store.get_face_identity(project_id, ident["id"])
            if vec_rec is not None:
                id_embeddings.append((ident["id"], ident["name"], vec_rec["embedding"]))

        saved_dets: list[dict[str, Any]] = []
        for det in detections:
            emb = det["embedding"]
            matched_id: str | None = None
            matched_name: str | None = None
            best_sim = -1.0
            for iid, iname, iemb in id_embeddings:
                sim = cosine_similarity(emb, iemb)
                if sim > best_sim and sim >= 0.60:
                    best_sim = sim
                    matched_id = iid
                    matched_name = iname

            rec = self.vector_store.add_face_detection(
                project_id=project_id,
                source_asset_id=eff_source_aid,
                keyframe_asset_id=aid,
                timestamp_sec=ts_sec,
                bbox=det["bbox"],
                det_score=det["det_score"],
                embedding=emb,
                identity_id=matched_id,
                metadata={
                    "matched_identity_name": matched_name,
                    "matched_similarity": round(best_sim, 4) if matched_id else None,
                    "shot_start_sec": float(meta.get("shot_start_sec") or ts_sec),
                    "shot_end_sec": float(meta.get("shot_end_sec") or (ts_sec + 2.0)),
                    "caption": meta.get("caption") or keyframe_asset.get("original_filename"),
                },
            )
            saved_dets.append(rec)
        return saved_dets

    def index_project_faces(
        self,
        project_id: str,
        *,
        keyframe_asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Indexa las caras de todos los keyframes e imágenes del proyecto."""
        if self.store.get_project(project_id) is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")

        keyframes = self.store.list_assets(project_id, kind="image")
        if keyframe_asset_ids:
            target = set(keyframe_asset_ids)
            keyframes = [k for k in keyframes if k["id"] in target]

        all_dets: list[dict[str, Any]] = []
        for kf in keyframes:
            all_dets.extend(self.index_keyframe_faces(project_id, kf))

        return {
            "project_id": project_id,
            "model_pack": INSIGHTFACE_MODEL_PACK,
            "license_notice": INSIGHTFACE_LICENSE_NOTE,
            "keyframes_scanned": len(keyframes),
            "faces_detected": len(all_dets),
            "detections": all_dets,
            # M3-IDEA-005: la detección actual es sintética/determinista; los
            # pesos reales de buffalo_l no se cargan aún (M3-IDEA-003).
            "simulated": True,
            "simulation_notice": SIMULATED_ML_NOTICE,
        }

    def combined_search(
        self,
        project_id: str,
        *,
        identity_id: str | None = None,
        identity_name: str | None = None,
        person_name: str | None = None,
        reference_asset_id: str | None = None,
        text_query: str | None = None,
        top_k: int = 10,
        limit: int | None = None,
        min_similarity: float = 0.60,
    ) -> list[dict[str, Any]]:
        """Alias explícito de búsqueda combinada (CLIP texto ∩ InsightFace persona)."""
        return self.find_shots_face(
            project_id,
            identity_id=identity_id,
            identity_name=identity_name,
            person_name=person_name,
            reference_asset_id=reference_asset_id,
            text_query=text_query,
            top_k=top_k,
            limit=limit,
            min_similarity=min_similarity,
        )

    def find_shots_face(
        self,
        project_id: str,
        *,
        identity_id: str | None = None,
        identity_name: str | None = None,
        person_name: str | None = None,
        reference_asset_id: str | None = None,
        text_query: str | None = None,
        top_k: int = 10,
        limit: int | None = None,
        min_similarity: float = 0.60,
    ) -> list[dict[str, Any]]:
        """Busca planos por identidad o referencia, con intersección opcional texto ∩ persona."""
        if self.store.get_project(project_id) is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")

        eff_top_k = int(limit) if limit is not None else int(top_k)
        eff_name = identity_name or person_name
        query_embedding: list[float] | None = None
        resolved_identity: dict[str, Any] | None = None

        if identity_id:
            resolved_identity = self.store.get_identity(project_id, identity_id)
            vec_rec = self.vector_store.get_face_identity(project_id, identity_id)
            if vec_rec is not None:
                query_embedding = vec_rec["embedding"]
            elif resolved_identity is None:
                raise ValueError(f"Identidad no encontrada: {identity_id!r}")

        if query_embedding is None and eff_name:
            resolved_identity = self.store.get_identity_by_name(project_id, eff_name)
            if resolved_identity is not None:
                vec_rec = self.vector_store.get_face_identity(
                    project_id,
                    resolved_identity["id"],
                )
                if vec_rec is not None:
                    query_embedding = vec_rec["embedding"]
            else:
                vec_rec = self.vector_store.get_face_identity_by_name(project_id, eff_name)
                if vec_rec is not None:
                    query_embedding = vec_rec["embedding"]
            if query_embedding is None and reference_asset_id is None:
                raise ValueError(f"Identidad no encontrada: {eff_name!r}")

        if query_embedding is None and reference_asset_id:
            ref_asset = self.store.get_asset(project_id, reference_asset_id)
            ref_path = self.store.get_asset_path(project_id, reference_asset_id)
            if ref_asset is None or ref_path is None:
                raise ValueError(f"Asset de imagen de cara no encontrado: {reference_asset_id!r}")
            faces = detect_faces_in_image(ref_path, metadata=ref_asset.get("metadata"))
            if not faces:
                raise ValueError("No se detectó cara en la imagen de consulta.")
            query_embedding = faces[0]["embedding"]

        if query_embedding is None:
            raise ValueError(
                "Debe proporcionar `identity_id`, `identity_name` o `reference_asset_id`."
            )

        # Asegurar que las imágenes/keyframes del proyecto tengan caras indexadas
        keyframes = self.store.list_assets(project_id, kind="image")
        if keyframes:
            face_hits = self.vector_store.search_face_detections(
                project_id,
                query_embedding,
                top_k=max(eff_top_k * 3, 25),
                min_similarity=-1.0,
            )
            if not face_hits:
                self.index_project_faces(project_id)

        raw_face_matches = self.vector_store.search_face_detections(
            project_id,
            query_embedding,
            top_k=max(eff_top_k * 3, 25),
            min_similarity=min_similarity,
        )

        # Agrupar por keyframe_asset_id quedándonos con la detección más similar
        by_kf: dict[str, dict[str, Any]] = {}
        for hit in raw_face_matches:
            kfid = hit["keyframe_asset_id"]
            if kfid not in by_kf or hit["similarity"] > by_kf[kfid]["similarity"]:
                kf_asset = self.store.get_asset(project_id, kfid)
                meta = hit.get("metadata") or {}
                by_kf[kfid] = {
                    "keyframe_asset_id": kfid,
                    "source_asset_id": hit["source_asset_id"],
                    "timestamp_sec": hit["timestamp_sec"],
                    "shot_start_sec": float(meta.get("shot_start_sec") or hit["timestamp_sec"]),
                    "shot_end_sec": float(meta.get("shot_end_sec") or (hit["timestamp_sec"] + 2.0)),
                    "face_similarity": hit["similarity"],
                    "similarity": hit["similarity"],
                    "score": hit["similarity"],
                    "bbox": hit["bbox"],
                    "identity_id": (
                        resolved_identity["id"] if resolved_identity else hit.get("identity_id")
                    ),
                    "person_name": (
                        resolved_identity["name"]
                        if resolved_identity
                        else meta.get("matched_identity_name")
                    ),
                    "caption": meta.get("caption") or "",
                    "preview_url": kf_asset["preview_url"] if kf_asset else None,
                    "view_url": kf_asset["view_url"] if kf_asset else None,
                }

        cleaned_text = (text_query or "").strip()
        if cleaned_text:
            existing_clip = self.vector_store.list_clip_shots(project_id)
            if not existing_clip and keyframes:
                for kf in keyframes:
                    self.clip_index.index_keyframe_asset(project_id, kf)

            text_hits = self.clip_index.find_shots_text(
                project_id,
                cleaned_text,
                top_k=max(eff_top_k * 4, 30),
            )
            text_by_kf = {h["keyframe_asset_id"]: h for h in text_hits if h["similarity"] > 0.15}
            combined_shots: list[dict[str, Any]] = []
            for kfid, face_shot in by_kf.items():
                text_shot = text_by_kf.get(kfid)
                if text_shot is None:
                    continue
                combined_score = round(
                    0.55 * face_shot["face_similarity"] + 0.45 * text_shot["similarity"],
                    4,
                )
                merged = {
                    **face_shot,
                    "text_query": cleaned_text,
                    "text_similarity": text_shot["similarity"],
                    "similarity": combined_score,
                    "score": combined_score,
                    "caption": text_shot.get("caption") or face_shot.get("caption") or "",
                    "visual_tags": text_shot.get("visual_tags") or [],
                }
                combined_shots.append(merged)

            combined_shots.sort(key=lambda x: (-x["score"], x["timestamp_sec"]))
            final_shots = combined_shots[: max(1, eff_top_k)]
        else:
            final_shots = sorted(
                by_kf.values(),
                key=lambda x: (-x["face_similarity"], x["timestamp_sec"]),
            )[: max(1, eff_top_k)]

        for rank, item in enumerate(final_shots, start=1):
            item["rank"] = rank

        return final_shots


INSIGHTFACE_NC_LICENSE_NOTICE = INSIGHTFACE_LICENSE_NOTE
FaceIndex = InsightFaceIndex


def combined_search(
    face_index: InsightFaceIndex,
    project_id: str,
    *,
    identity_id: str | None = None,
    identity_name: str | None = None,
    person_name: str | None = None,
    reference_asset_id: str | None = None,
    text_query: str | None = None,
    top_k: int = 10,
    limit: int | None = None,
    min_similarity: float = 0.60,
) -> list[dict[str, Any]]:
    """Helper de búsqueda combinada (CLIP texto ∩ InsightFace persona)."""
    return face_index.combined_search(
        project_id,
        identity_id=identity_id,
        identity_name=identity_name,
        person_name=person_name,
        reference_asset_id=reference_asset_id,
        text_query=text_query,
        top_k=top_k,
        limit=limit,
        min_similarity=min_similarity,
    )
