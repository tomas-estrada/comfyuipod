#!/usr/bin/env python3
"""Requisito de almacenamiento derivado (política de arquitectura y recursos).

180 GB era una estimación, no un requisito. Este script **calcula** el mínimo de
disco a partir de componentes reales y versionados, en lugar de repetir un
literal:

    R(perfil, opt-ins) = imagen
                       + arranque (Fase 1 + Fase 2 del perfil)
                       + modelos de tarea (Fase 3 base + opt-ins pedidos)
                       + pico de staging (archivo más grande × factor)
                       + reserva de descarga (HF_DISK_MARGIN_BYTES)
                       + reserva de trabajo (media, proxies, salidas, cachés)

La suma se redondea al alza a GB y se compara con el umbral que hoy está
*codificado a mano* en `comfyctl/src/config.rs` (`default_min_disk_gb`) y en
`tools/mac/comfy` (`comfy rent`). Con `--check` el script falla si un umbral
queda por debajo del requisito derivado, para que CI o un operador lo detecten
sin tener que acordarse de los números.

No descarga nada: lee `config/models-studio.json` y los totales declarados de
las fases 1/2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

GiB = 1024**3
GB = 1000**3

#: Tamaño medido de la imagen publicada (README §B2 / CI). Se declara, no se
#: calcula: es un artefacto medido, no una constante del código.
IMAGE_BYTES_DEFAULT = 15_600_000_000

#: Sobrecarga de filesystem (ext4/overlay: metadatos, journal, inodes).
FILESYSTEM_OVERHEAD_BYTES = 2 * GiB

#: Reserva de descarga declarada en scripts/download-models.sh (HF_DISK_MARGIN_BYTES).
DOWNLOAD_MARGIN_BYTES = 10 * GiB

#: Reserva de trabajo: media fuente, proxies, salidas y cachés de Studio.
WORKING_RESERVE_BYTES = 20 * GiB

#: Durante una descarga pueden coexistir el `.incomplete` y la copia final.
STAGING_COEXIST_FACTOR = 2

#: Perfiles que descargan la Fase 3 (el manifiesto). El resto no la toca.
PHASE3_PROFILES = frozenset({"studio", "unreal"})

#: Pico de staging para perfiles sin Fase 3: el archivo más grande que bajan.
#: Fase 1 -> `inswapper_128.onnx` (~0.52 GiB); Fase 2 -> `flux1-dev-fp8` (16.06 GiB).
PHASE1_LARGEST_BYTES = int(0.52 * GiB)
PHASE2_LARGEST_BYTES = int(16.06 * GiB)

#: Perfiles y su arranque (Fase 1 + Fase 2), en bytes, declarados en
#: scripts/download-models.sh. Fase 3 sale del manifiesto, no de aquí.
PROFILE_BOOTSTRAP_BYTES: dict[str, int] = {
    "all": int(2.79 * GiB) + int(60.56 * GiB),
    "general": int(2.79 * GiB) + int(24.96 * GiB),
    "realistic": int(2.79 * GiB) + int(24.95 * GiB),
    "anime": int(2.79 * GiB) + int(29.43 * GiB),
    "minimal": int(2.79 * GiB),
    "studio": int(2.79 * GiB) + int(13.22 * GiB),
    # `unreal` arranca igual que `studio` y añade Hunyuan3D como opt-in implícito.
    "unreal": int(2.79 * GiB) + int(13.22 * GiB),
}

#: Umbrales codificados a mano que este script debe justificar o destapar.
DECLARED_THRESHOLDS_GB: dict[str, int] = {
    "all": 75,
    "general": 45,
    "realistic": 45,
    "anime": 45,
    "minimal": 10,
    "studio": 180,
    "unreal": 210,
}

#: Mínimo que pide `tools/mac/comfy` al alquilar (GB). `studio`/`unreal` son None
#: porque el helper los DERIVA de este mismo script: no hay literal que verificar.
RENT_MIN_DISK_GB: dict[str, int | None] = {
    "all": 120,
    "general": 120,
    "realistic": 120,
    "anime": 120,
    "minimal": 120,
    "studio": None,
    "unreal": None,
}

OPT_IN_FLAGS = {
    "STUDIO_INCLUDE_ANIMATE": "animate",
    "STUDIO_INCLUDE_3D": "three_d",
    "STUDIO_INCLUDE_MINIMAX": "minimax",
}


def load_manifest(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _opt_in_bytes(manifest: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in manifest.get("models", []):
        flag = entry.get("opt_in")
        if flag:
            out[flag] = out.get(flag, 0) + int(entry.get("bytes") or 0)
    return out


def _largest_default_bytes(manifest: dict[str, Any]) -> int:
    sizes = [
        int(e.get("bytes") or 0) for e in manifest.get("models", []) if not e.get("opt_in")
    ]
    return max(sizes) if sizes else 0


def requirement_bytes(
    manifest: dict[str, Any],
    *,
    profile: str,
    opt_ins: set[str] | None = None,
    image_bytes: int = IMAGE_BYTES_DEFAULT,
    staging_factor: int = STAGING_COEXIST_FACTOR,
    image_on_volume: bool = False,
) -> dict[str, Any]:
    """Requisito derivado en bytes, con el desglose por componente."""
    requested = set(opt_ins or ())
    if profile == "unreal":
        # `unreal` implica Hunyuan3D (el helper no tiene perfil unreal).
        requested.add("STUDIO_INCLUDE_3D")

    opt_bytes = _opt_in_bytes(manifest)
    base_bytes = int(manifest.get("meta", {}).get("default_bytes") or 0)
    bootstrap = PROFILE_BOOTSTRAP_BYTES.get(profile, 0)
    uses_phase3 = profile in PHASE3_PROFILES
    models = (base_bytes + sum(opt_bytes.get(flag, 0) for flag in requested)) if uses_phase3 else 0
    if uses_phase3:
        staging = _largest_default_bytes(manifest) * staging_factor
    elif profile == "minimal":
        staging = PHASE1_LARGEST_BYTES * staging_factor
    else:
        staging = PHASE2_LARGEST_BYTES * staging_factor

    components = {
        "image": image_bytes if image_on_volume else 0,
        "filesystem_overhead": FILESYSTEM_OVERHEAD_BYTES if image_on_volume else 0,
        "bootstrap_phase1_2": bootstrap,
        "staging_peak": staging,
        "download_margin": DOWNLOAD_MARGIN_BYTES,
    }
    # Puerta de arranque: lo que debe estar LIBRE en el volumen de datos antes
    # de empezar a descargar (arranque + pico de staging + margen de descarga).
    start_gate = bootstrap + staging + DOWNLOAD_MARGIN_BYTES
    components["task_models"] = models
    components["working_reserve"] = WORKING_RESERVE_BYTES
    total = sum(components.values())
    threshold = DECLARED_THRESHOLDS_GB.get(profile)
    rent = RENT_MIN_DISK_GB.get(profile)
    return {
        "profile": profile,
        "opt_ins": sorted(requested),
        "image_on_volume": image_on_volume,
        "components": components,
        "start_gate_bytes": start_gate,
        "start_gate_gb": start_gate / GB,
        "total_bytes": total,
        "total_gb": total / GB,
        "total_gib": total / GiB,
        "min_volume_gb": -(-int(total) // GB),  # techo en GB
        "declared_threshold_gb": threshold,
        "rent_min_disk_gb": rent,
        # Positivo = el umbral declarado se queda CORTO respecto a lo derivado.
        "start_gate_shortfall_gb": max(0.0, (start_gate - (threshold or 0) * GB) / GB),
        "volume_shortfall_gb": max(0.0, (total - (rent or 0) * GB) / GB),
    }

def _gb(value: int) -> str:
    return f"{value / GB:.2f} GB ({value / GiB:.2f} GiB)"


def render(report: dict[str, Any]) -> str:
    lines = [
        f"Perfil `{report['profile']}`"
        + (f" + opt-ins {', '.join(report['opt_ins'])}" if report["opt_ins"] else ""),
        "",
        "  componente                bytes",
    ]
    for name, value in report["components"].items():
        lines.append(f"  {name:<24} {_gb(value):>28}")
    lines.append(f"  {'TOTAL':<24} {_gb(report['total_bytes']):>28}")
    lines.append("")
    lines.append(f"  puerta de arranque (S5)   {_gb(report['start_gate_bytes']):>28}")
    declared = report["declared_threshold_gb"]
    rent = report["rent_min_disk_gb"]
    if declared is not None:
        verdict = (
            "OK"
            if report["start_gate_shortfall_gb"] <= 0
            else f"INSUFICIENTE por {report['start_gate_shortfall_gb']:.1f} GB"
        )
        lines.append(f"  umbral declarado (comfyctl S5): {declared} GB -> {verdict}")
    if rent is not None:
        verdict = (
            "OK"
            if report["volume_shortfall_gb"] <= 0
            else f"INSUFICIENTE por {report['volume_shortfall_gb']:.1f} GB"
        )
        lines.append(f"  volumen mínimo (comfy rent): {rent} GB -> {verdict}")
    else:
        lines.append("  volumen mínimo (comfy rent): derivado por el helper")
    lines.append(f"  volumen derivado recomendado: {report['min_volume_gb']} GB")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="config/models-studio.json",
        help="Ruta del manifiesto de Fase 3.",
    )
    parser.add_argument("--profile", default="studio", choices=sorted(PROFILE_BOOTSTRAP_BYTES))
    parser.add_argument(
        "--opt-in",
        action="append",
        default=[],
        choices=sorted(OPT_IN_FLAGS),
        help="Grupo opt-in pedido (repetible).",
    )
    parser.add_argument(
        "--image-bytes",
        type=int,
        default=IMAGE_BYTES_DEFAULT,
        help="Tamaño medido de la imagen publicada.",
    )
    parser.add_argument(
        "--image-on-volume",
        action="store_true",
        help="La imagen vive en el mismo volumen que los datos (RunPod/proveedores "
        "sin overlay separado). En Vast.ai la imagen está en el host: no se carga.",
    )
    parser.add_argument("--json", action="store_true", help="Salida JSON.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Salir con código 1 si algún umbral declarado queda por debajo.",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"ERROR: no existe el manifiesto {manifest_path}", file=sys.stderr)
        return 2

    report = requirement_bytes(
        load_manifest(manifest_path),
        profile=args.profile,
        opt_ins=set(args.opt_in),
        image_bytes=args.image_bytes,
        image_on_volume=args.image_on_volume,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))

    if args.check:
        problems = []
        if report["declared_threshold_gb"] is not None and report["start_gate_shortfall_gb"] > 0:
            problems.append(
                f"S5/{report['profile']}={report['declared_threshold_gb']} GB "
                f"< puerta de arranque {report['start_gate_gb']:.1f} GB"
            )
        if (
            report["rent_min_disk_gb"] is not None
            and report["volume_shortfall_gb"] > 0
        ):
            problems.append(
                f"comfy rent/{report['profile']}={report['rent_min_disk_gb']} GB "
                f"< volumen derivado {report['min_volume_gb']} GB"
            )
        if problems:
            for problem in problems:
                print(f"FALLO: {problem}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
