#!/usr/bin/env python3
"""Comprueba que el manifiesto core y `download-models.sh` dicen lo mismo.

Política (punto 7): «Keep installation decisions, dependency resolution and
execution recipes deterministic and versioned.» La selección de qué descarga
cada `MODEL_PROFILE` vive en un `case`/regex dentro de
`scripts/download-models.sh`; este script la extrae estáticamente y la compara
con el campo `profiles` de `config/models-core.json`.

Es el mismo patrón que la paridad O-11 entre `download-models.sh` y
`comfyctl/src/collect/models.rs`: si los dos se separan, CI falla.

Uso:  python3 scripts/check_profile_parity.py [--fix-hint]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "download-models.sh"
MANIFEST = REPO_ROOT / "config" / "models-core.json"

#: Perfiles que reconoce el script.
PROFILES = ("all", "general", "realistic", "anime", "minimal", "studio", "unreal")

_GUARD_RE = re.compile(
    r'if\s+\[\[\s*"\$\{MODEL_PROFILE\}"\s*=~\s*\^\(([^)]*)\)\$\s*\]\]\s*;\s*then'
)
# Forma de igualdad simple (`if [ "${MODEL_PROFILE}" = "all" ]; then`). Sin esto
# la guarda era invisible para el checker, que la trataba como "sin guarda" y por
# tanto la ensanchaba a «todos salvo minimal»: un manifiesto demasiado amplio
# podía coincidir con una lectura errónea y nadie lo detectaba.
# Deliberadamente NO casa formas compuestas (`!=`, `&&`, `||`): esas se dejan sin
# parsear en vez de admitir una conjetura.
_GUARD_EQ_RE = re.compile(
    r'if\s+\[\s*"\$\{MODEL_PROFILE\}"\s*=\s*"([^"]+)"\s*\]\s*;\s*then'
)
_HF_FETCH_RE = re.compile(r'hf_fetch\s+"([^"]+)"\s+"([^"]+)"')


def _function_body(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    depth = 0
    i = start + len(f"{name}() {{") - 1
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    return source[start:]


def _fetch_filename(lines: list[str], start: int) -> str | None:
    """4.º argumento de `hf_fetch` (el nombre de fichero), en una o varias líneas."""
    args: list[str] = []
    j = start
    while j < len(lines) and len(args) < 5:
        args.extend(re.findall(r'"([^"]*)"', lines[j]))
        if not lines[j].rstrip().endswith("\\"):
            if len(args) >= 5 or j > start:
                break
        j += 1
    return args[3] if len(args) >= 4 else None


def _parse_plan(lines: list[str], default: set[str]) -> dict[str, set[str]]:
    """`filename -> perfiles`, leído estáticamente de un cuerpo de función.

    `default` es lo que aplica a un `hf_fetch` sin guarda de perfil: en la Fase 1
    es «todos» (siempre corre), en la Fase 2 es «todos salvo minimal» (que
    retorna antes).
    """
    plan: dict[str, set[str]] = {}
    current: set[str] | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        guard = _GUARD_RE.search(line)
        guard_eq = _GUARD_EQ_RE.search(line)
        if guard:
            current = {p.strip() for p in guard.group(1).split("|")}
            i += 1
            continue
        if guard_eq:
            current = {guard_eq.group(1)}
            i += 1
            continue
        if line.strip() == "fi":
            current = None
            i += 1
            continue
        if line.strip().startswith("return 0"):
            current = None
            i += 1
            continue
        fetch = _HF_FETCH_RE.search(line)
        if fetch:
            filename = _fetch_filename(lines, i)
            if filename:
                applies = set(current) if current else set(default)
                plan.setdefault(filename, set()).update(applies)
        i += 1
    return plan


def parse_plan() -> dict[str, set[str]]:
    """Plan completo: Fase 1 (siempre) + Fase 2 (por perfil)."""
    source = SCRIPT.read_text(encoding="utf-8")
    plan = _parse_plan(_function_body(source, "run_phase1").split("\n"), set(PROFILES))
    for name, profiles in _parse_plan(
        _function_body(source, "run_phase2").split("\n"), set(PROFILES) - {"minimal"}
    ).items():
        plan.setdefault(name, set()).update(profiles)
    return plan


def manifest_plan() -> dict[str, set[str]]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    plan: dict[str, set[str]] = {}
    for entry in data.get("models", []):
        profiles = entry.get("profiles")
        if profiles is None:
            continue
        plan[str(entry.get("filename"))] = {str(p) for p in profiles}
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix-hint", action="store_true", help="Muestra el diff sugerido.")
    args = parser.parse_args()

    bash = parse_plan()
    declared = manifest_plan()

    only_bash = sorted(set(bash) - set(declared))
    only_manifest = sorted(set(declared) - set(bash))
    mismatched = sorted(
        name for name in set(bash) & set(declared) if bash[name] != declared[name]
    )

    if not (only_bash or only_manifest or mismatched):
        print(
            f"OK: {len(bash)} activos con perfiles coincidentes entre "
            "download-models.sh y config/models-core.json"
        )
        return 0

    print("ERROR: la selección por perfil diverge entre bash y manifiesto:", file=sys.stderr)
    for name in only_bash:
        print(
            f"  - {name}: en bash ({sorted(bash[name])}) "
            "pero sin 'profiles' en el manifiesto",
            file=sys.stderr,
        )
    for name in only_manifest:
        print(
            f"  - {name}: en el manifiesto ({sorted(declared[name])}) pero no en bash",
            file=sys.stderr,
        )
    for name in mismatched:
        print(
            f"  - {name}: bash={sorted(bash[name])} manifiesto={sorted(declared[name])}",
            file=sys.stderr,
        )
    if args.fix_hint:
        print("\nSugerencia de 'profiles' para config/models-core.json:", file=sys.stderr)
        for name in sorted(bash):
            print(f'  "{name}": {json.dumps(sorted(bash[name]))}', file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
