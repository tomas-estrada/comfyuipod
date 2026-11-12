#!/usr/bin/env python3
"""Validador del manifiesto de engines (plan Ultra, W07 / acceptance A07).

`upgrades-ultra-version/engines.json` declara 89 engines con su disposición
(required/alternative/conditional/excluded/reference_only), revisión de código,
lock exigido antes de build, revisión de licencia (`terms_review`) y gate de
release. Este script es un **release gate de coherencia**: no construye nada ni
descarga pesos; comprueba que el manifiesto cumpla las reglas que la política
exige antes de aceptar un candidato pre-GPU.

Comprueba (A07 «lista seleccionada con code SHA, dependency lock/hashes, avisos
y decisión de redistribución por componente»):

1. Los contadores declarados (`counts`) cuadran con las entradas reales.
2. Las disposiciones son del conjunto permitido por `policy`.
3. Todo engine `build_selected` tiene `terms_review` (revisión de licencia) no
   vacío: no se hornea un componente sin decidir su redistribución.
4. Un engine marcado `lock_required_before_build` declara una `code_revision`
   (o queda señalado como bloqueo de release, no como listo).
5. `excluded`/`reference_only` NO están `build_selected` (no se hornea lo que la
   política excluye).
6. Las `capabilities` referenciadas existen en el rango declarado (C01..Cnn).

Devuelve 0 si todo cuadra; con `--check` sale distinto de 0 ante el primer
incumplimiento, para CI. `--json` emite el informe estructurado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "upgrades-ultra-version" / "engines.json"
DEFAULT_CAPS = REPO_ROOT / "upgrades-ultra-version" / "capabilities.json"
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

ALLOWED_DISPOSITIONS = {
    "required",
    "alternative",
    "conditional",
    "excluded",
    "reference_only",
}


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle, object_pairs_hook=_unique_object_pairs)


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _capability_ids(caps_path: Path) -> set[str]:
    """Ids C01..Cnn declarados en capabilities.json (vacío si no está)."""
    if not caps_path.is_file():
        return set()
    data = load_json(caps_path)
    out: set[str] = set()
    items = data if isinstance(data, list) else data.get("capabilities") or []
    for item in items:
        cid = item.get("id") if isinstance(item, dict) else None
        if cid:
            out.add(str(cid))
    return out


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_tracked_paths(repo_root: Path) -> set[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return {
        raw_path.decode("utf-8", errors="surrogateescape")
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    }


def validate(
    manifest: dict[str, Any],
    cap_ids: set[str],
    *,
    repo_root: Path | None = None,
    require_git_tracked: bool | None = None,
) -> tuple[list[str], list[str]]:
    """Devuelve (incumplimientos, release_blockers).

    - **incumplimientos**: incoherencias REALES del manifiesto (disposición
      inválida, excluido+selected, capability inexistente, contadores, id
      duplicado, licencia sin revisar). Invalidan el manifiesto: `--check` falla.
    - **release_blockers**: estados pendientes ESPERADOS antes del build (lock de
      código sin fijar todavía). No son errores del manifiesto; son el trabajo
      de fijado que W07 debe cerrar antes del release. Se reportan pero no
      invalidan la coherencia (A07: «el release queda bloqueado con números»,
      no «el manifiesto está mal»).
    """
    problems: list[str] = []
    blockers: list[str] = []
    evidence_root = (repo_root or REPO_ROOT).resolve()
    if require_git_tracked is None:
        require_git_tracked = True
    tracked_paths = _git_tracked_paths(evidence_root) if require_git_tracked else None
    engines = manifest.get("engines", [])

    # 1. Contadores.
    counts = manifest.get("counts", {})
    if "engines" in counts and int(counts["engines"]) != len(engines):
        problems.append(
            f"counts.engines={counts['engines']} != entradas reales {len(engines)}"
        )

    seen_ids: set[str] = set()
    for eng in engines:
        eid = eng.get("id", "<sin-id>")
        if eid in seen_ids:
            problems.append(f"id duplicado: {eid}")
        seen_ids.add(eid)

        # 2. Disposición válida.
        dispo = eng.get("disposition")
        if dispo not in ALLOWED_DISPOSITIONS:
            problems.append(f"{eid}: disposition inválida {dispo!r}")

        selected = bool(eng.get("build_selected"))

        # 3. Seleccionado para build => revisión de licencia resuelta.
        if selected and not (eng.get("terms_review") or "").strip():
            problems.append(f"{eid}: build_selected sin terms_review (licencia sin revisar)")

        # 4. A07 readiness evidence. These are release blockers, not manifest
        # incoherences: unknown evidence must stay visible without inventing it.
        if selected:
            revision = str(eng.get("code_revision") or "")
            if eng.get("lock_required_before_build") and not revision:
                blockers.append(f"{eid}: lock de código pendiente de fijar antes del build")
            elif not COMMIT_SHA_RE.fullmatch(revision):
                blockers.append(f"{eid}: A07 requiere code_revision como SHA inmutable de 40 hex")

            hashes = eng.get("dependency_lock_hashes")
            if not isinstance(hashes, dict) or not hashes or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(value), re.IGNORECASE)
                for value in hashes.values()
            ):
                blockers.append(f"{eid}: A07 requiere hashes SHA-256 de dependency locks")
            else:
                for lock_name, expected_hash in hashes.items():
                    if not isinstance(lock_name, str) or not lock_name.strip():
                        blockers.append(f"{eid}: nombre de dependency lock inválido")
                        continue
                    lock_path = Path(lock_name)
                    if lock_path.is_absolute():
                        blockers.append(
                            f"{eid}: dependency lock {lock_name} fuera de la raíz del repositorio"
                        )
                        continue
                    try:
                        resolved_lock = (evidence_root / lock_path).resolve(strict=True)
                    except (OSError, RuntimeError, ValueError):
                        blockers.append(
                            f"{eid}: lock file {lock_name} no existe, no es accesible "
                            "o tiene una ruta inválida"
                        )
                        continue
                    if not resolved_lock.is_relative_to(evidence_root):
                        blockers.append(
                            f"{eid}: dependency lock {lock_name} fuera de la raíz del repositorio"
                        )
                        continue
                    if not resolved_lock.is_file():
                        blockers.append(f"{eid}: lock file {lock_name} no es un archivo")
                        continue
                    if require_git_tracked:
                        if tracked_paths is None:
                            blockers.append(
                                f"{eid}: no se pudo comprobar el versionado Git de {lock_name}"
                            )
                            continue
                        if lock_name not in tracked_paths:
                            blockers.append(
                                f"{eid}: lock file {lock_name} no está versionado"
                            )
                            continue
                        resolved_name = resolved_lock.relative_to(evidence_root).as_posix()
                        if resolved_name not in tracked_paths:
                            blockers.append(
                                f"{eid}: target de lock {resolved_name} no está versionado"
                            )
                            continue
                    try:
                        actual_hash = _sha256_file(resolved_lock)
                    except OSError:
                        blockers.append(f"{eid}: lock file {lock_name} no es legible")
                        continue
                    if actual_hash.casefold() != str(expected_hash).casefold():
                        blockers.append(
                            f"{eid}: SHA-256 no coincide para dependency lock {lock_name}"
                        )

            if eng.get("redistribution_decision") not in {"allowed", "conditional", "blocked"}:
                blockers.append(f"{eid}: A07 requiere decisión explícita de redistribución")
            if eng.get("notices_reviewed") is not True:
                blockers.append(f"{eid}: A07 requiere revisión explícita de notices")
            if eng.get("implementation_status") != "implemented":
                blockers.append(
                    f"{eid}: adapter/entrypoint real aún no está declarado implementado"
                )

        # 5. excluded / reference_only no se hornean.
        if dispo in {"excluded", "reference_only"} and selected:
            problems.append(f"{eid}: disposition {dispo} no puede estar build_selected")

        # 6. capabilities referenciadas existen (si tenemos el catálogo).
        if cap_ids:
            for cap in eng.get("capabilities", []) or []:
                if cap not in cap_ids:
                    problems.append(f"{eid}: capability inexistente {cap}")

    return problems, blockers


def build_report(
    manifest: dict[str, Any], problems: list[str], blockers: list[str]
) -> dict[str, Any]:
    engines = manifest.get("engines", [])
    dispo_counts: dict[str, int] = {}
    selected = 0
    for eng in engines:
        dispo_counts[eng.get("disposition", "?")] = (
            dispo_counts.get(eng.get("disposition", "?"), 0) + 1
        )
        if eng.get("build_selected"):
            selected += 1
    return {
        "total_engines": len(engines),
        "build_selected": selected,
        "dispositions": dispo_counts,
        "problems": problems,
        "release_blockers": blockers,
        "a07_evidence_scope": (
            "Manifest declarations only; image inventory, offline boot and runtime "
            "probes require separate CI evidence."
        ),
        "coherent": not problems,
        "release_ready": not problems and not blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--capabilities", type=Path, default=DEFAULT_CAPS)
    parser.add_argument("--json", action="store_true", help="informe JSON")
    parser.add_argument(
        "--check", action="store_true", help="exit != 0 ante incumplimientos (CI)"
    )
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    cap_ids = _capability_ids(args.capabilities)
    problems, blockers = validate(manifest, cap_ids)
    report = build_report(manifest, problems, blockers)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"engines: {report['total_engines']} | build_selected: {report['build_selected']}")
        print(f"dispositions: {report['dispositions']}")
        if problems:
            print(f"INCUMPLIMIENTOS DE COHERENCIA ({len(problems)}):")
            for p in problems:
                print(f"  - {p}")
        else:
            print("COHERENTE: manifiesto de engines sin incoherencias")
        if blockers:
            print(f"BLOQUEOS DE RELEASE pendientes ({len(blockers)}) -- esperados pre-build:")
            for b in blockers[:5]:
                print(f"  - {b}")
            if len(blockers) > 5:
                print(f"  ... y {len(blockers) - 5} más")

    # --check falla SOLO por incoherencias reales; missing A07 evidence remains
    # a visible release blocker until the evidence is supplied and verified.
    if args.check and problems:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
