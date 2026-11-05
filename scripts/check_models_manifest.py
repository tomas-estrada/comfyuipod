#!/usr/bin/env python3
"""Validador del manifiesto de modelos de Fase 3 (config/models-studio.json).

Verifica sin descargar los pesos:
1. Integridad estructural y sumas exactas por categoría y globales (meta vs entradas).
2. Políticas de licencia y repositorios: prohibición de variantes FaceID/InstantID,
   repositorios gated (pyannote, h94/CLIP-ViT-H-14..., FLUX.1-dev, F5-TTS, XTTS) y
   nombres antiguos redirigidos de Wan.
3. En modo online (por defecto): petición HTTP HEAD a cada URL en HuggingFace
   leyendo la cabecera `x-linked-size` (o `content-length`) y comprobando que
   coincide al byte con `bytes` del manifiesto.
4. En modo `--fixture`: auto-test unitario determinista (casos válidos y corruptos).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

FORBIDDEN_SUBSTRINGS = (
    "faceid",
    "instantid",
    "pyannote/",
    "h94/clip-vit-h-14-laion2b-s32b-b79k",
    "flux.1-dev",
    "f5-tts",
    "coqui/xtts",
    "xtts-v2",
    "wan_2.2_comfyui_repackaged",
    "wan_2.1_comfyui_repackaged",
)

REQUIRED_CATEGORIES = {
    "wan_core",
    "vace_fp8",
    "lora_turbo_5b",
    "latentsync",
    "liveportrait",
    "indextts",
    "faster_whisper",
    "wav2vec2_aligners",
    "clip_vit_l14",
    "ipadapter_sdxl",
    "campplus",
    "animate_opt_in",
}

# M3-D: flags opt-in válidos por entrada del manifiesto
VALID_OPT_IN_FLAGS = frozenset({
    "STUDIO_INCLUDE_ANIMATE",
    "STUDIO_INCLUDE_3D",
    "STUDIO_INCLUDE_MINIMAX",
})

# Manifiestos conocidos: `studio` (Fase 3, vídeo/voz) y `core` (Fases 1/2).
VALID_PROFILES = frozenset({"studio", "core"})

# Política de arquitectura: clases de activo admitidas. `bootstrap` reservado
# para lo genuinamente necesario para operar; `task` se adquiere para la tarea
# elegida; `optional` son variantes y mejoras (coincide con los grupos opt-in).
VALID_ASSET_CLASSES = frozenset({"bootstrap", "task", "optional"})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_head_size(url: str, timeout: float = 20.0, retries: int = 3) -> tuple[int, int]:
    """Devuelve (http_status, size_bytes) mediante HEAD sin seguir redirección al blob CDN."""
    opener = urllib.request.build_opener(_NoRedirectHandler)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "ComfyUIPod-ManifestChecker/1.0"},
        )
        try:
            resp = opener.open(req, timeout=timeout)
            status = resp.status
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            hdrs = {k.lower(): v for k, v in exc.headers.items()}
            if status in (401, 403, 404, 451):
                raise RuntimeError(f"HTTP {status} (inaccesible o gated) en {url}") from exc
            if status not in (200, 301, 302, 303, 307, 308):
                last_err = RuntimeError(f"HTTP {status} inesperado en {url}")
                if attempt < retries:
                    time.sleep(1.5 * attempt)
                continue
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
            continue

        raw_size = hdrs.get("x-linked-size") or hdrs.get("content-length")
        if raw_size is None:
            last_err = RuntimeError(
                f"Sin cabecera x-linked-size/content-length en {url} (HTTP {status})"
            )
            if attempt < retries:
                time.sleep(1.5 * attempt)
            continue
        return status, int(raw_size)

    raise RuntimeError(f"Fallo tras {retries} intentos en {url}: {last_err}")


HEX = set("0123456789abcdef")


def _expected_url(
    repo_id: str,
    remote_path: str,
    repo_type: object,
    revision: object = None,
) -> str:
    """URL canónica del activo.

    Los repositorios de tipo `dataset` se resuelven bajo `/datasets/`; el resto
    (modelos, espacios) bajo la raíz. Aceptar solo la forma de modelo hacía
    fallar los activos dataset correctamente declarados.

    Con `revision` fijada la URL apunta a ese commit (inmutable) en lugar de a
    `main`, que es lo que permite verificar integridad de forma determinista.
    """
    prefix = "datasets/" if str(repo_type or "").strip().lower() == "dataset" else ""
    ref = str(revision) if revision else "main"
    return f"https://huggingface.co/{prefix}{repo_id}/resolve/{ref}/{remote_path}"


def _validate_integrity(entry: dict[str, Any], prefix: str, errors: list[str]) -> None:
    """Revisión fijada e integridad, «where available» (política de activos).

    HuggingFace publica el SHA-256 del objeto LFS o, para ficheros pequeños
    guardados en git, el SHA-1 del blob. Se exige `revision` siempre y al menos
    un hash; el algoritmo debe coincidir con la longitud.
    """
    revision = entry.get("revision")
    if not isinstance(revision, str) or len(revision) != 40 or set(revision) > HEX:
        errors.append(f"{prefix}: revision inválida {revision!r} (se esperaba un SHA-1 de git)")

    digests = {
        name: entry.get(name)
        for name in ("sha256", "sha1")
        if entry.get(name) is not None
    }
    if not digests:
        errors.append(f"{prefix}: sin hash de integridad (sha256 o sha1)")
    for name, value in digests.items():
        expected_len = 64 if name == "sha256" else 40
        if not isinstance(value, str) or len(value) != expected_len or set(value) > HEX:
            errors.append(f"{prefix}: {name} inválido {value!r}")
    if len(digests) > 1:
        errors.append(f"{prefix}: hash redundante ({sorted(digests)}), deja uno")


def validate_manifest_data(
    data: dict[str, Any],
    *,
    require_all_categories: bool = True,
    head_fn: Callable[[str], tuple[int, int]] | None = None,
    workers: int = 8,
) -> list[str]:
    """Valida el diccionario del manifiesto; devuelve lista de errores (vacía si todo es válido)."""
    errors: list[str] = []

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(f"version inválida: {version!r} (se esperaba un entero >= 1)")
    elif data.get("profile") == "studio" and version != 2:
        errors.append(f"version inválida: {version!r} (esperado 2 para 'studio')")

    profile = data.get("profile")
    if profile not in VALID_PROFILES:
        errors.append(f"profile inválido: {profile!r} (esperado uno de {sorted(VALID_PROFILES)})")

    models = data.get("models")
    if not isinstance(models, list) or not models:
        errors.append("'models' debe ser una lista no vacía")
        return errors

    meta = data.get("meta")
    if not isinstance(meta, dict):
        errors.append("'meta' debe ser un objeto")
        return errors

    seen_dest: set[str] = set()
    seen_urls: set[str] = set()
    cat_counts: dict[str, int] = {}
    cat_bytes: dict[str, int] = {}
    default_entries = 0
    default_bytes = 0
    opt_in_entries = 0
    opt_in_bytes = 0

    for idx, entry in enumerate(models):
        prefix = f"models[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: debe ser un objeto")
            continue

        for req_key in (
            "category",
            "repo_id",
            "remote_path",
            "url",
            "bytes",
            "target_dir",
            "filename",
            "dest_path",
            "license",
        ):
            if req_key not in entry:
                errors.append(f"{prefix}: falta campo obligatorio '{req_key}'")

        repo_id = str(entry.get("repo_id", ""))
        remote_path = str(entry.get("remote_path", ""))
        url = str(entry.get("url", ""))
        target_dir = str(entry.get("target_dir", ""))
        filename = str(entry.get("filename", ""))
        dest_path = str(entry.get("dest_path", ""))
        category = str(entry.get("category", ""))
        size = entry.get("bytes")
        opt_in = entry.get("opt_in")

        expected_url = _expected_url(
            repo_id, remote_path, entry.get("repo_type"), entry.get("revision")
        )
        if url != expected_url:
            errors.append(f"{prefix}: url {url!r} != {expected_url!r}")

        _validate_integrity(entry, prefix, errors)

        expected_dest = f"{target_dir}/{filename}"
        if dest_path != expected_dest:
            errors.append(f"{prefix}: dest_path {dest_path!r} != {expected_dest!r}")

        if not isinstance(size, int) or size <= 0:
            errors.append(f"{prefix}: bytes inválido {size!r}")
            size = 0

        if dest_path in seen_dest:
            errors.append(f"{prefix}: dest_path duplicado {dest_path!r}")
        seen_dest.add(dest_path)

        if url in seen_urls:
            errors.append(f"{prefix}: url duplicada {url!r}")
        seen_urls.add(url)

        combined_lower = f"{repo_id} {remote_path} {url} {dest_path}".lower()
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in combined_lower:
                errors.append(f"{prefix}: contiene término prohibido o repo gated/antiguo {bad!r}")

        if opt_in is not None and opt_in not in VALID_OPT_IN_FLAGS:
            errors.append(f"{prefix}: opt_in inválido {opt_in!r}")

        cat_counts[category] = cat_counts.get(category, 0) + 1
        cat_bytes[category] = cat_bytes.get(category, 0) + size

        if opt_in:
            opt_in_entries += 1
            opt_in_bytes += size
        else:
            default_entries += 1
            default_bytes += size

    if require_all_categories:
        missing_cats = REQUIRED_CATEGORIES - set(cat_counts.keys())
        if data.get("profile") == "studio" and missing_cats:
            errors.append(f"Faltan categorías requeridas: {sorted(missing_cats)}")

    if meta.get("default_entries") != default_entries:
        errors.append(
            f"meta.default_entries={meta.get('default_entries')} != calculado={default_entries}"
        )
    if meta.get("default_bytes") != default_bytes:
        errors.append(
            f"meta.default_bytes={meta.get('default_bytes')} != calculado={default_bytes}"
        )
    if meta.get("opt_in_entries") != opt_in_entries:
        errors.append(
            f"meta.opt_in_entries={meta.get('opt_in_entries')} != calculado={opt_in_entries}"
        )
    if meta.get("opt_in_bytes") != opt_in_bytes:
        errors.append(
            f"meta.opt_in_bytes={meta.get('opt_in_bytes')} != calculado={opt_in_bytes}"
        )

    meta_cats = meta.get("categories")
    if isinstance(meta_cats, dict):
        for cat, info in meta_cats.items():
            if cat not in cat_counts:
                errors.append(f"meta.categories[{cat!r}] no tiene entradas en models")
                continue
            if info.get("entries") != cat_counts[cat]:
                errors.append(
                    f"meta.categories[{cat!r}].entries={info.get('entries')} != {cat_counts[cat]}"
                )
            if info.get("bytes") != cat_bytes[cat]:
                errors.append(
                    f"meta.categories[{cat!r}].bytes={info.get('bytes')} != {cat_bytes[cat]}"
                )
        for cat in cat_counts:
            if cat not in meta_cats:
                errors.append(
                    f"Categoría {cat!r} presente en models pero ausente en meta.categories"
                )
    else:
        errors.append("meta.categories debe ser un objeto")

    # Clasificación de activos (política de arquitectura): bootstrap / task /
    # optional. Nada es bootstrap sin declararlo, así que una clase ausente o
    # desconocida es un error y el desglose de meta debe cuadrar con las entradas.
    for index, model in enumerate(models):
        declared = model.get("class")
        if declared not in VALID_ASSET_CLASSES:
            errors.append(
                f"models[{index}].class={declared!r} inválida "
                f"(esperado uno de {sorted(VALID_ASSET_CLASSES)})"
            )
        elif bool(model.get("opt_in")) != (declared == "optional"):
            errors.append(
                f"models[{index}] ({model.get('filename')!r}): class={declared!r} "
                "no coherente con opt_in (los opt-in deben ser 'optional')"
            )

    class_counts = {"bootstrap": 0, "task": 0, "optional": 0}
    class_bytes = {"bootstrap": 0, "task": 0, "optional": 0}
    for model in models:
        declared = model.get("class")
        if declared in class_counts:
            class_counts[declared] += 1
            class_bytes[declared] += int(model.get("bytes") or 0)

    meta_classes = meta.get("classes")
    if not isinstance(meta_classes, dict):
        errors.append("meta.classes debe ser un objeto con el desglose por clase")
    else:
        for name in class_counts:
            info = meta_classes.get(name)
            if not isinstance(info, dict):
                errors.append(f"meta.classes[{name!r}] debe ser un objeto")
                continue
            if info.get("entries") != class_counts[name]:
                errors.append(
                    f"meta.classes[{name!r}].entries={info.get('entries')} "
                    f"!= calculado={class_counts[name]}"
                )
            if info.get("bytes") != class_bytes[name]:
                errors.append(
                    f"meta.classes[{name!r}].bytes={info.get('bytes')} "
                    f"!= calculado={class_bytes[name]}"
                )

    if head_fn is not None and not errors:
        def _check_one(item: dict[str, Any]) -> str | None:
            url = item["url"]
            expected = item["bytes"]
            try:
                _status, remote_size = head_fn(url)
            except Exception as exc:
                return f"HEAD fallo para {url}: {exc}"
            if remote_size != expected:
                return (
                    f"Divergencia de bytes en {url}: manifest={expected}, "
                    f"x-linked-size={remote_size}"
                )
            return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_check_one, entry) for entry in models]
            for fut in as_completed(futures):
                err = fut.result()
                if err:
                    errors.append(err)

    return errors


def run_fixture_self_test() -> int:
    """Ejecuta pruebas unitarias deterministas sobre el validador."""
    sample_revision = "ee6f4a40737a995bf5818954cfce6d59443b0f04"
    sample_url = (
        "https://huggingface.co/Comfy-Org/Wan-2.2/resolve/"
        f"{sample_revision}/split_files/vae/wan2.2_vae.safetensors"
    )
    valid_manifest: dict[str, Any] = {
        "version": 2,
        "profile": "studio",
        "meta": {
            "default_entries": 1,
            "default_bytes": 1409400960,
            "opt_in_entries": 1,
            "opt_in_bytes": 18413068672,
            "classes": {
                "bootstrap": {"entries": 0, "bytes": 0},
                "task": {"entries": 1, "bytes": 1409400960},
                "optional": {"entries": 1, "bytes": 18413068672},
            },
            "categories": {
                "wan_core": {"entries": 1, "bytes": 1409400960, "license": "Apache-2.0"},
                "animate_opt_in": {"entries": 1, "bytes": 18413068672, "license": "Apache-2.0"},
            },
        },
        "models": [
            {
                "category": "wan_core",
                "repo_id": "Comfy-Org/Wan-2.2",
                "remote_path": "split_files/vae/wan2.2_vae.safetensors",
                "url": sample_url,
                "bytes": 1409400960,
                "target_dir": "models/vae",
                "filename": "wan2.2_vae.safetensors",
                "dest_path": "models/vae/wan2.2_vae.safetensors",
                "license": "Apache-2.0",
                "revision": sample_revision,
                "sha256": "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
                "class": "task",
                "opt_in": None,
            },
            {
                "category": "animate_opt_in",
                "repo_id": "Comfy-Org/Wan-2.2",
                "remote_path": (
                    "split_files/diffusion_models/"
                    "wan2.2_animate_14B_int8_convrot.safetensors"
                ),
                "url": (
                    "https://huggingface.co/Comfy-Org/Wan-2.2/resolve/"
                    f"{sample_revision}"
                    "/split_files/diffusion_models/wan2.2_animate_14B_int8_convrot.safetensors"
                ),
                "bytes": 18413068672,
                "target_dir": "models/diffusion_models",
                "filename": "wan2.2_animate_14B_int8_convrot.safetensors",
                "dest_path": "models/diffusion_models/wan2.2_animate_14B_int8_convrot.safetensors",
                "license": "Apache-2.0",
                "revision": sample_revision,
                "sha1": "135542a23eb13611cafa71878d3c33e355d896a7",
                "class": "optional",
                "opt_in": "STUDIO_INCLUDE_ANIMATE",
            },
        ],
    }

    fake_sizes = {
        sample_url: (302, 1409400960),
        valid_manifest["models"][1]["url"]: (302, 18413068672),
    }

    # Caso 1: manifiesto válido pasa
    errs = validate_manifest_data(
        valid_manifest,
        require_all_categories=False,
        head_fn=lambda u: fake_sizes[u],
    )
    assert not errs, f"Fixture válido falló inesperadamente: {errs}"

    # Caso 2: divergencia de bytes detectada por HEAD
    corrupted_head = validate_manifest_data(
        valid_manifest,
        require_all_categories=False,
        head_fn=lambda u: (302, 999),
    )
    assert any("Divergencia de bytes" in e for e in corrupted_head), (
        f"No detectó divergencia de bytes: {corrupted_head}"
    )

    # Caso 3: modelo prohibido FaceID detectado
    bad_faceid = copy.deepcopy(valid_manifest)
    bad_faceid["models"][0]["remote_path"] = "sdxl_models/ip-adapter-faceid_sdxl.bin"
    bad_faceid["models"][0]["url"] = (
        "https://huggingface.co/h94/IP-Adapter-FaceID/resolve/main/sdxl_models/ip-adapter-faceid_sdxl.bin"
    )
    bad_faceid["models"][0]["repo_id"] = "h94/IP-Adapter-FaceID"
    errs_faceid = validate_manifest_data(bad_faceid, require_all_categories=False)
    assert any("faceid" in e.lower() for e in errs_faceid), (
        f"No detectó término prohibido faceid: {errs_faceid}"
    )

    # Caso 4: desajuste en suma de meta.default_bytes
    bad_meta = copy.deepcopy(valid_manifest)
    bad_meta["meta"]["default_bytes"] += 1
    errs_meta = validate_manifest_data(bad_meta, require_all_categories=False)
    assert any("meta.default_bytes" in e for e in errs_meta), (
        f"No detectó error en meta.default_bytes: {errs_meta}"
    )

    print("OK [fixture]: 4/4 casos de auto-test superados.")
    return 0


MANIFESTS = ("config/models-studio.json", "config/models-core.json")


def _validate_one(path: Path, *, offline: bool, workers: int) -> int:
    if not path.is_file():
        print(f"ERROR: No existe el manifiesto en {path}", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    head_callable = None if offline else fetch_head_size
    errors = validate_manifest_data(
        data,
        require_all_categories=True,
        head_fn=head_callable,
        workers=workers,
    )
    if errors:
        print(f"ERROR: {len(errors)} fallo(s) en {path}:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    meta = data["meta"]
    classes = meta.get("classes") or {}
    bootstrap = classes.get("bootstrap", {})
    mode_label = "offline" if offline else "HEAD x-linked-size"
    print(
        f"OK [{mode_label}]: {meta['default_entries']} entradas por defecto "
        f"({meta['default_bytes']:,} B = {meta['default_bytes'] / 1e9:.2f} GB) + "
        f"{meta['opt_in_entries']} opt-in ({meta['opt_in_bytes']:,} B) "
        f"verificadas en {path}; bootstrap={bootstrap.get('entries', 0)} "
        f"({bootstrap.get('bytes', 0):,} B)."
    )
    return 0


def _validate_all(args: argparse.Namespace) -> int:
    status = 0
    for name in MANIFESTS:
        status |= _validate_one(Path(name), offline=args.offline, workers=args.workers)
    return 1 if status else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica manifiestos de modelos.")
    parser.add_argument(
        "--manifest",
        default="config/models-studio.json",
        help="Ruta al archivo JSON del manifiesto (default: config/models-studio.json)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Valida todos los manifiestos conocidos (studio + core)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Valida esquema y sumas sin realizar peticiones HTTP HEAD",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Ejecuta auto-test unitario con fixtures en memoria",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Hilos concurrentes para peticiones HEAD (default: 8)",
    )
    args = parser.parse_args()

    if args.fixture:
        return run_fixture_self_test()

    if args.all:
        return _validate_all(args)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"ERROR: No existe el manifiesto en {manifest_path}", file=sys.stderr)
        return 1

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    head_callable = None if args.offline else fetch_head_size
    errors = validate_manifest_data(
        data,
        require_all_categories=True,
        head_fn=head_callable,
        workers=args.workers,
    )
    if errors:
        print(f"ERROR: {len(errors)} fallo(s) en {manifest_path}:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    meta = data["meta"]
    mode_label = "offline" if args.offline else "HEAD x-linked-size"
    print(
        f"OK [{mode_label}]: {meta['default_entries']} entradas por defecto "
        f"({meta['default_bytes']:,} B = {meta['default_bytes'] / 1e9:.2f} GB) + "
        f"{meta['opt_in_entries']} opt-in ({meta['opt_in_bytes']:,} B) "
        f"verificadas en {manifest_path}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
