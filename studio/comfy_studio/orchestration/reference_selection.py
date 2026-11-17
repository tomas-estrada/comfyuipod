"""Deterministic project-image selection for production shots (C23/W08).

Selection is deliberately CPU-only and explainable: project tags and asset names
are matched against each shot's description and prompt. The report preserves the
candidate count, selections, exclusions, and unresolved explicit IDs so callers
can review every choice instead of silently dropping references.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from comfy_studio.av_recipes import max_image_references_for
from comfy_studio.orchestration.state import ProductionState

_STOP_WORDS = frozenset(
    "a al algo ante bajo cada como con contra cual cuando de del desde donde el ella "
    "en entre era es esa ese eso esta este estos fue hacia hasta hay la las le les "
    "lo los más mas mi muy no o para pero por que se sin sobre su sus te the un una "
    "uno unos y ya".split()
)
_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)
_GENERATION_KINDS = frozenset({"gen", "generate", "t2v", "i2v"})


def _tokens(value: Any) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token.casefold()
        for token in _TOKEN_RE.split(normalized)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


def _asset_terms(asset: dict[str, Any]) -> tuple[set[str], set[str]]:
    metadata = asset.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    tags = metadata.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        tags = [tags]
    tag_terms = set().union(*(_tokens(tag) for tag in tags)) if tags else set()
    descriptive_fields = (
        asset.get("filename"),
        asset.get("original_filename"),
        metadata.get("description"),
        metadata.get("caption"),
        metadata.get("alt_text"),
        metadata.get("subject"),
    )
    name_terms = set().union(*(_tokens(value) for value in descriptive_fields))
    return tag_terms, name_terms


def _shot_terms(shot: Any, layer: Any) -> set[str]:
    prompt = layer.params.get("prompt", "")
    return _tokens(f"{shot.description} {prompt}")


def _score(terms: set[str], asset: dict[str, Any]) -> int:
    tags, names = _asset_terms(asset)
    # User-applied tags carry more intent than a filename match.
    return 3 * len(terms & tags) + len(terms & names)


def compile_project_references(
    state: ProductionState,
    assets: list[dict[str, Any]],
) -> dict[str, int]:
    """Select references against each generation layer's verified recipe cap.

    A recipe without a declared image input has capacity zero. For an unbound
    video-generation layer, a relevant image selects the verified Wan I2V
    recipe. Explicit IDs are project-scoped, de-duplicated, and checked against
    the same capacity. Every skipped or unresolved ID is included in the layer
    report, and unresolved explicit requests mark that layer unexecutable.
    """
    image_assets = sorted(
        (asset for asset in assets if asset.get("kind") == "image"),
        key=lambda asset: str(asset.get("id") or ""),
    )
    by_id = {str(asset.get("id")): asset for asset in image_assets if asset.get("id")}
    summary = {
        "shots_compiled": 0,
        "candidate_assets": len(image_assets),
        "selected": 0,
        "excluded": 0,
        "unresolved": 0,
    }

    for shot in state.all_shots():
        shot_compiled = False
        for layer in shot.layers:
            if layer.kind not in _GENERATION_KINDS:
                continue

            params = layer.params
            explicit_raw = params.get("reference_asset_ids")
            if explicit_raw is None and params.get("image_asset_id"):
                explicit_raw = [params["image_asset_id"]]
            if isinstance(explicit_raw, str):
                explicit_raw = [explicit_raw]
            explicit_ids = list(dict.fromkeys(
                str(asset_id).strip()
                for asset_id in (explicit_raw or [])
                if str(asset_id).strip()
            ))

            requested_capability = str(
                params.get("capability")
                or (
                    "video.image_to_video"
                    if layer.template_id == "wan_i2v"
                    else "video.text_to_video"
                )
            )
            candidate_template = layer.template_id
            if candidate_template is None and layer.kind in _GENERATION_KINDS:
                candidate_template = "wan_i2v"
                requested_capability = "video.image_to_video"
            capacity = max_image_references_for(
                requested_capability, template_id=candidate_template
            )

            terms = _shot_terms(shot, layer)
            ranked = sorted(
                (
                    (_score(terms, asset), str(asset["id"]), asset)
                    for asset in image_assets
                    if terms and _score(terms, asset) > 0
                ),
                key=lambda row: (-row[0], row[1]),
            )
            unresolved = [
                {"asset_id": asset_id, "reason": "not_in_project"}
                for asset_id in explicit_ids
                if asset_id not in by_id
            ]

            if explicit_ids:
                ordered_ids = [asset_id for asset_id in explicit_ids if asset_id in by_id]
                # Relevance decides which explicit refs fit the limit; ties keep
                # the caller's order, then the stable ID order is a fallback.
                explicit_order = {asset_id: index for index, asset_id in enumerate(ordered_ids)}
                ranked_explicit = sorted(
                    ordered_ids,
                    key=lambda asset_id: (
                        -_score(terms, by_id[asset_id]), explicit_order[asset_id], asset_id
                    ),
                )
                selected_ids = ranked_explicit[:capacity]
                excluded_ids = [
                    asset_id
                    for asset_id in ordered_ids
                    if asset_id not in selected_ids
                ]
                excluded = [
                    {
                        "asset_id": asset_id,
                        "reason": (
                            "model_has_no_image_reference_input"
                            if capacity == 0
                            else "model_input_capacity"
                        ),
                    }
                    for asset_id in excluded_ids
                ]
            else:
                # Automatic selection requires a positive lexical match. It
                # never attaches an unrelated image just because one exists.
                ranked_ids = [asset_id for _, asset_id, _ in ranked]
                selected_ids = ranked_ids[:capacity]
                ranked_id_set = set(ranked_ids)
                excluded = []
                for asset in image_assets:
                    asset_id = str(asset["id"])
                    if asset_id in selected_ids:
                        continue
                    if capacity == 0 and asset_id in ranked_id_set:
                        reason = "model_has_no_image_reference_input"
                    elif asset_id in ranked_id_set:
                        reason = "model_input_capacity"
                    else:
                        reason = "no_relevance_match"
                    excluded.append({"asset_id": asset_id, "reason": reason})

            selected = [
                {"asset_id": asset_id, "score": _score(terms, by_id[asset_id])}
                for asset_id in selected_ids
            ]
            if selected_ids and layer.template_id is None:
                layer.template_id = "wan_i2v"
                params["capability"] = "video.image_to_video"
            if selected_ids:
                params["image_asset_id"] = selected_ids[0]
                params.pop("reference_asset_ids", None)
            elif not explicit_ids:
                params.pop("image_asset_id", None)

            report = {
                "capacity": capacity,
                "candidate_count": len(image_assets),
                "selected": selected,
                "excluded": excluded,
                "unresolved": unresolved,
            }
            params["reference_selection"] = report
            summary["selected"] += len(selected)
            summary["excluded"] += len(excluded)
            summary["unresolved"] += len(unresolved)
            shot_compiled = True

            if unresolved or (explicit_ids and excluded):
                missing_ids = ", ".join(item["asset_id"] for item in unresolved)
                params["recipe_unresolved"] = "referencias explícitas no disponibles o " \
                    "por encima de la capacidad de imagen de la receta"
                if missing_ids:
                    params["recipe_unresolved"] += f": {missing_ids}"
            elif selected_ids:
                params.pop("recipe_unresolved", None)
            elif explicit_ids and capacity == 0:
                params["recipe_unresolved"] = (
                    "la plantilla seleccionada no acepta referencias de imagen"
                )

        if shot_compiled:
            summary["shots_compiled"] += 1

    return summary


__all__ = ["compile_project_references"]
