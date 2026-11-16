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

**Modo estricto de release (`--require-release-ready`)**: gate de promoción
A07. Falla con exit 1 ante cualquier incoherencia de coherencia y con exit 2
ante CUALQUIER bloqueo de release A07 (evidencia pendiente). Es el modo que
exige el workflow antes de promover una imagen candidata a `latest`: el
manifiesto debe estar coherente Y sin bloqueos. `--check` conserva su
semántica estructural (solo falla por incoherencias) para el desarrollo
incremental y para PRs candidatos.
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


def _source_lock_blockers(
    engine_id: str, lock: dict[str, Any], *, repo_root: Path
) -> list[str]:
    """Validate pinned/internal code at its Git revision and integration/test hashes at HEAD."""
    blockers: list[str] = []
    revision = str(lock.get("source_revision") or "")

    # Los mapas de fuentes fijadas a la revisión (pinned/internas) se validan con
    # la misma regla: ruta relativa segura, SHA-256 de 64 hex y rederivación vía
    # `git show <source_revision>:<ruta>`, que además comprueba que la ruta está
    # versionada en esa revisión.
    pinned_maps: list[tuple[str, Any]] = [
        ("pinned_source_sha256", lock.get("pinned_source_sha256")),
        ("internal_source_sha256", lock.get("internal_source_sha256")),
    ]
    has_pinned_sources = any(bool(mapping) for _, mapping in pinned_maps)
    if has_pinned_sources and not COMMIT_SHA_RE.fullmatch(revision):
        blockers.append(f"{engine_id}: source lock requiere source_revision SHA inmutable")

    for field_name, pinned_sources in pinned_maps:
        if pinned_sources is None:
            continue
        if not isinstance(pinned_sources, dict):
            blockers.append(f"{engine_id}: {field_name} debe ser un objeto")
            continue
        for source_name, expected_hash in pinned_sources.items():
            relative = Path(str(source_name))
            if relative.is_absolute() or ".." in relative.parts:
                blockers.append(
                    f"{engine_id}: {field_name} contiene ruta inválida {source_name}"
                )
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash), re.IGNORECASE):
                blockers.append(
                    f"{engine_id}: {field_name} contiene SHA-256 inválido para {source_name}"
                )
                continue
            if not COMMIT_SHA_RE.fullmatch(revision):
                # No se puede consultar un ref mutable o vacío; la falta de
                # revisión ya quedó registrada como bloqueo arriba.
                continue
            result = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"{revision}:{relative.as_posix()}"],
                check=False,
                capture_output=True,
            )
            actual_hash = hashlib.sha256(result.stdout).hexdigest().casefold()
            if result.returncode != 0:
                blockers.append(
                    f"{engine_id}: {source_name} no existe en source_revision {revision}"
                )
            elif actual_hash != str(expected_hash).casefold():
                blockers.append(
                    f"{engine_id}: SHA-256 de {source_name} "
                    f"no coincide con source_revision {revision}"
                )

    integration_sources = lock.get("integration_source_sha256") or {}
    if not isinstance(integration_sources, dict):
        blockers.append(f"{engine_id}: integration_source_sha256 debe ser un objeto")
        integration_sources = {}
    for source_name, expected_hash in integration_sources.items():
        relative = Path(str(source_name))
        if relative.is_absolute() or ".." in relative.parts:
            blockers.append(f"{engine_id}: integration source contiene ruta inválida {source_name}")
            continue
        try:
            actual_hash = _sha256_file(repo_root / relative)
        except OSError:
            blockers.append(f"{engine_id}: integration source {source_name} no es legible")
            continue
        if actual_hash.casefold() != str(expected_hash).casefold():
            blockers.append(
                f"{engine_id}: SHA-256 no coincide para integration source {source_name}"
            )

    verification_test = lock.get("verification_test")
    if verification_test is not None:
        if not isinstance(verification_test, dict):
            blockers.append(f"{engine_id}: verification_test debe ser un objeto")
        else:
            test_path = str(verification_test.get("path") or "")
            expected_hash = str(verification_test.get("sha256") or "")
            relative = Path(test_path)
            if relative.is_absolute() or ".." in relative.parts or not test_path:
                blockers.append(f"{engine_id}: verification_test contiene ruta inválida")
            elif not re.fullmatch(r"[0-9a-f]{64}", expected_hash, re.IGNORECASE):
                blockers.append(f"{engine_id}: verification_test contiene SHA-256 inválido")
            else:
                try:
                    actual_hash = _sha256_file(repo_root / relative)
                except OSError:
                    blockers.append(f"{engine_id}: verification_test {test_path} no es legible")
                else:
                    if actual_hash.casefold() != expected_hash.casefold():
                        blockers.append(f"{engine_id}: SHA-256 no coincide para verification_test")
    return blockers


def _implementation_evidence_blocker(
    engine_id: str,
    label: str,
    value: Any,
    *,
    repo_root: Path,
    tracked_paths: set[str] | None,
    require_git_tracked: bool,
) -> str | None:
    """Require implementation evidence to be a real, tracked repository file."""
    if not isinstance(value, str) or not value.strip():
        return f"{engine_id}: A07 requiere evidence_{label} (ruta de archivo versionada)"
    relative_path = Path(value)
    if relative_path.is_absolute():
        return f"{engine_id}: evidence_{label} {value} fuera de la raíz del repositorio"
    try:
        resolved_path = (repo_root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return f"{engine_id}: evidence_{label} {value} no existe o tiene una ruta inválida"
    if not resolved_path.is_relative_to(repo_root):
        return f"{engine_id}: evidence_{label} {value} fuera de la raíz del repositorio"
    if not resolved_path.is_file():
        return f"{engine_id}: evidence_{label} {value} no es un archivo"
    if require_git_tracked:
        if tracked_paths is None:
            return (
                f"{engine_id}: no se pudo comprobar el versionado Git de "
                f"evidence_{label} {value}"
            )
        if value not in tracked_paths:
            return f"{engine_id}: evidence_{label} {value} no está versionado"
        resolved_name = resolved_path.relative_to(repo_root).as_posix()
        if resolved_name not in tracked_paths:
            return f"{engine_id}: target de evidence_{label} {resolved_name} no está versionado"
    return None


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
                    elif resolved_lock.suffix.casefold() == ".json":
                        try:
                            lock_data = load_json(resolved_lock)
                        except (OSError, ValueError):
                            lock_data = None
                        if isinstance(lock_data, dict):
                            blockers.extend(
                                _source_lock_blockers(
                                    eid, lock_data, repo_root=evidence_root
                                )
                            )

            if eng.get("redistribution_decision") not in {"allowed", "conditional", "blocked"}:
                blockers.append(f"{eid}: A07 requiere decisión explícita de redistribución")
            if eng.get("notices_reviewed") is not True:
                blockers.append(f"{eid}: A07 requiere revisión explícita de notices")
            if eng.get("implementation_status") != "implemented":
                blockers.append(
                    f"{eid}: adapter/entrypoint real aún no está declarado implementado"
                )
            else:
                for field_name in ("entrypoint", "test"):
                    evidence_blocker = _implementation_evidence_blocker(
                        eid,
                        field_name,
                        eng.get(f"evidence_{field_name}"),
                        repo_root=evidence_root,
                        tracked_paths=tracked_paths,
                        require_git_tracked=require_git_tracked,
                    )
                    if evidence_blocker:
                        blockers.append(evidence_blocker)

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
    selected_ids: set[str] = set()
    for eng in engines:
        dispo_counts[eng.get("disposition", "?")] = (
            dispo_counts.get(eng.get("disposition", "?"), 0) + 1
        )
        if eng.get("build_selected"):
            selected += 1
            selected_ids.add(str(eng.get("id")))
    # Resumen útil para el gate estricto: bloqueos por engine y cuántos de los
    # engines seleccionados quedan pendientes (los no seleccionados nunca
    # generan bloqueos A07).
    blockers_by_engine: dict[str, int] = {}
    for blocker in blockers:
        eid = blocker.split(":", 1)[0]
        blockers_by_engine[eid] = blockers_by_engine.get(eid, 0) + 1
    selected_engines_with_blockers = sum(
        1 for eid in blockers_by_engine if eid in selected_ids
    )
    return {
        "total_engines": len(engines),
        "build_selected": selected,
        "dispositions": dispo_counts,
        "problems": problems,
        "release_blockers": blockers,
        "release_blockers_by_engine": blockers_by_engine,
        "selected_engines_with_blockers": selected_engines_with_blockers,
        "a07_evidence_scope": (
            "Manifest declarations only; image inventory, offline boot and runtime "
            "probes require separate CI evidence."
        ),
        "coherent": not problems,
        "release_ready": not problems and not blockers,
    }


def _print_strict_summary(report: dict[str, Any]) -> None:
    """Resumen útil del gate estricto: cuántos engines seleccionados quedan."""
    blockers = report["release_blockers"]
    if report["coherent"] and not blockers:
        print("RELEASE LISTA: manifiesto coherente y sin bloqueos de release A07")
        return
    selected = report["build_selected"]
    pending = report["selected_engines_with_blockers"]
    if not report["coherent"]:
        print(
            "RELEASE NO LISTA: "
            f"{len(report['problems'])} incoherencias de manifiesto "
            f"y {len(blockers)} bloqueos de release"
        )
        return
    print(
        f"RELEASE NO LISTA: {len(blockers)} bloqueos A07 en "
        f"{pending} de {selected} engines seleccionados"
    )
    ranked = sorted(
        report["release_blockers_by_engine"].items(),
        key=lambda item: (-item[1], item[0]),
    )
    for eid, count in ranked[:10]:
        print(f"  - {eid}: {count} bloqueos")
    if len(ranked) > 10:
        print(f"  ... y {len(ranked) - 10} engines más con bloqueos")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--capabilities", type=Path, default=DEFAULT_CAPS)
    parser.add_argument("--json", action="store_true", help="informe JSON")
    parser.add_argument(
        "--check", action="store_true", help="exit != 0 ante incumplimientos (CI)"
    )
    parser.add_argument(
        "--require-release-ready",
        action="store_true",
        help=(
            "modo estricto A07: exit 1 ante incoherencias, exit 2 ante cualquier "
            "bloqueo de release; gate de promoción a latest"
        ),
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
        if args.require_release_ready:
            _print_strict_summary(report)

    # --check falla SOLO por incoherencias reales; missing A07 evidence remains
    # a visible release blocker until the evidence is supplied and verified.
    # El modo estricto (--require-release-ready) es el gate de promoción A07:
    # falla ante incoherencias (exit 1) O ante cualquier bloqueo (exit 2).
    if args.require_release_ready:
        if problems:
            return 1
        if blockers:
            return 2
        return 0
    if args.check and problems:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
