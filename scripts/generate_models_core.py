#!/usr/bin/env python3
"""Genera config/models-core.json (Fases 1 y 2) a partir de download-models.sh.

El manifiesto de Fase 3 (`config/models-studio.json`) ya era máquina-legible;
las Fases 1 y 2 solo vivían como llamadas `hf_fetch` en bash. Este script las
extrae, verifica el tamaño exacto con `HEAD` (`x-linked-size`, sin descargar) y
escribe un manifiesto versionado con la misma forma, para que el resolvedor de
capacidades pueda determinar qué activos faltan para una plantilla concreta.

Uso:  python3 scripts/generate_models_core.py [--write]

Sin `--write` solo imprime el resultado (dry-run).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "download-models.sh"
OUT = REPO_ROOT / "config" / "models-core.json"

HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{remote}"
HF_DATASET_RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/main/{remote}"

# Clasificación por política de arquitectura: bootstrap = necesario para operar
# (stack facial/VAE que la Fase 1 trae antes de abrir ComfyUI); task = selección
# de modelo o mejora que se adquiere para la tarea.
BOOTSTRAP_FILENAMES = {
    "buffalo_l.zip",
    "det_10g.onnx",
    "w600k_r50.onnx",
    "inswapper_128.onnx",
    "codeformer-v0.1.0.pth",
    "GFPGANv1.4.pth",
    "detection_Resnet50_Final.pth",
    "parsing_parsenet.pth",
    "face_yolov8m.pt",
    "sam_vit_b_01ec64.pth",
    "sdxl.vae.safetensors",
    "vae-ft-mse-840000-ema-pruned.safetensors",
}

LICENSES = {
    "Gourieff/ReActor": "ReActor (InsightFace buffalo_l: no comercial)",
    "kg-09/face_detection_models": "MIT",
    "madebyollin/sdxl-vae-fp16-fix": "MIT",
    "stabilityai/sd-vae-ft-mse-original": "MIT",
    "Kim2091/UltraSharp": "AGPL-3.0",
    "Kim2091/AnimeSharp": "AGPL-3.0",
    "RunDiffusion/Juggernaut-XI-v11": "RunDiffusion (revisar)",
    "SG161222/Realistic_Vision_V6.0_B1_noVAE": "CreativeML OpenRAIL-M",
    "SG161222/RealVisXL_V5.0": "CreativeML OpenRAIL-M",
    "Ine007/waiIllustriousSDXL_v160": "Illustrious (revisar)",
    "OnomaAIResearch/Illustrious-XL-v2.0": "Illustrious (revisar)",
    "Laxhar/noobai-XL-1.1": "noobai (revisar)",
    "xinsir/controlnet-union-sdxl-1.0": "Apache-2.0",
    "h94/IP-Adapter": "Apache-2.0 (sin pesos FaceID)",
    "Comfy-Org/flux1-dev": "FLUX.1-dev (no comercial)",
}

CATEGORIES = {
    "insightface": "insightface",
    "facerestore_models": "facerestore",
    "facedetection": "facedetection",
    "ultralytics": "ultralytics",
    "sams": "sams",
    "vae": "vae",
    "upscale_models": "upscalers",
    "checkpoints": "checkpoints",
    "controlnet": "controlnet",
    "ipadapter": "ipadapter",
    "clip_vision": "clip_vision",
}


def parse_entries() -> list[dict]:
    src = SCRIPT.read_text(encoding="utf-8")
    pattern = re.compile(
        r'hf_fetch\s+"([^"]+)"\s+"([^"]+)"[ \t]*\\\s*\n\s*'
        r'"([^"]+)"\s+"([^"]+)"\s+"([^"]+)"'
    )
    entries: list[dict] = []
    for match in pattern.finditer(src):
        repo, remote, target, filename, repo_type = match.groups()
        # ${MODELS_DIR}/x -> models/x (misma resolución que download-models.sh)
        dest = re.sub(r"^\$\{MODELS_DIR\}/?", "models/", target)
        if dest == "models":
            dest = "models"
        entries.append(
            {
                "repo_id": repo,
                "remote_path": remote,
                "target_dir": dest,
                "filename": filename,
                "repo_type": repo_type,
            }
        )
    # buffalo_l.zip se descarga y se extrae: se declara con `extract`.
    entries.insert(
        0,
        {
            "repo_id": "Gourieff/ReActor",
            "remote_path": "models/buffalo_l.zip",
            "target_dir": "models/insightface/models/buffalo_l",
            "filename": "buffalo_l.zip",
            "repo_type": "dataset",
            "extract": True,
        },
    )
    return entries


def category_for(target_dir: str) -> str:
    """`models/checkpoints/x` -> `checkpoints`; `models` -> `models`."""
    if target_dir.startswith("models/"):
        leaf = target_dir[len("models/") :].split("/")[0]
    else:
        leaf = target_dir
    return CATEGORIES.get(leaf, leaf or "misc")


def head_size(url: str, attempts: int = 3) -> int | None:
    for _attempt in range(attempts):
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                size = response.headers.get("x-linked-size")
                if not size:
                    size = response.headers.get("content-length")
                if size:
                    return int(size)
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    raw = parse_entries()
    print(f"{len(raw)} entradas extraídas de download-models.sh")

    def resolve(entry: dict) -> tuple:
        template = HF_DATASET_RESOLVE if entry["repo_type"] == "dataset" else HF_RESOLVE
        url = template.format(repo=entry["repo_id"], remote=entry["remote_path"])
        size = head_size(url)
        return entry, url, size

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(resolve, raw))

    models = []
    missing = []
    for entry, url, size in results:
        if size is None:
            missing.append(url)
            continue
        dest = f'{entry["target_dir"]}/{entry["filename"]}'
        record = {
            "category": category_for(entry["target_dir"]),
            "repo_id": entry["repo_id"],
            "remote_path": entry["remote_path"],
            "url": url,
            "bytes": size,
            "target_dir": entry["target_dir"],
            "filename": entry["filename"],
            "dest_path": dest,
            "license": LICENSES.get(entry["repo_id"], "revisar"),
            "repo_type": entry["repo_type"],
            "class": "bootstrap" if entry["filename"] in BOOTSTRAP_FILENAMES else "task",
            "opt_in": None,
        }
        if entry.get("extract"):
            record["extract"] = True
        models.append(record)

    if missing:
        print("ERROR: sin tamaño verificable (¿gated o movido?):", file=sys.stderr)
        for url in missing:
            print(f"  {url}", file=sys.stderr)
        return 1

    classes = {"bootstrap": [0, 0], "task": [0, 0], "optional": [0, 0]}
    for record in models:
        classes[record["class"]][0] += 1
        classes[record["class"]][1] += record["bytes"]

    manifest = {
        "version": 1,
        "profile": "core",
        "meta": {
            "default_entries": len(models),
            "default_bytes": sum(m["bytes"] for m in models),
            "opt_in_entries": 0,
            "opt_in_bytes": 0,
            "classes": {
                name: {"entries": counts[0], "bytes": counts[1]}
                for name, counts in classes.items()
            },
            "categories": {},
        },
        "models": models,
    }
    for record in models:
        bucket = manifest["meta"]["categories"].setdefault(
            record["category"], {"entries": 0, "bytes": 0}
        )
        bucket["entries"] += 1
        bucket["bytes"] += record["bytes"]

    print(json.dumps(manifest["meta"], ensure_ascii=False, indent=1))
    if args.write:
        OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"escrito {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
