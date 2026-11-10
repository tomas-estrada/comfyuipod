#!/usr/bin/env python3
"""Validador estructural de los diagramas PlantUML de docs/architecture/.

No sustituye a un renderer (no hay JRE en el entorno de generación), pero
comprueba lo que un renderer detectaría como error de sintaxis o de coherencia:

  1. `@startuml`/`@enduml` balanceados y únicos.
  2. Toda referencia en una arista apunta a un elemento declarado.
  3. Toda transición de máquina de estados apunta a un estado declarado.
  4. Ningún identificador declarado dos veces.
  5. Máquinas de estados: estado inicial presente, sin estados inalcanzables.
  6. Coherencia entre vistas: los participantes de 02-states son un subconjunto
     de los componentes declarados en 01-components.

Uso: python3 scripts/check_architecture_puml.py [--verbose]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPONENTS = REPO / "docs" / "architecture" / "01-components.puml"
STATES = REPO / "docs" / "architecture" / "02-states.puml"

# Identificadores PlantUML válidos.
ID = r"[A-Za-z_][A-Za-z0-9_]*"

# Palabras clave que no son identificadores de elemento.
KEYWORDS = {
    "package", "rectangle", "node", "actor", "cloud", "database", "folder",
    "frame", "state", "card", "class", "interface", "component", "together",
    "legend", "note", "title", "skinparam", "pragma", "hide", "show", "scale",
    "left", "right", "top", "bottom", "of", "as", "on", "startuml", "enduml",
    "if", "then", "else", "endif", "loop", "endloop", "alt", "group",
    "activate", "deactivate", "destroy", "create", "return", "autonumber",
    "newpage", "split", "repeat", "while", "endwhile", "fork", "forkagain",
    "endfork", "kill", "order", "also", "ref", "over", "nwdiag", "artifact",
    "agent", "boundary", "control", "entity", "queue", "collections", "usecase",
    "person", "women", "men", "hexagon", "storage", "circle", "diamond",
    "process", "json", "yaml", "archimate", "true", "false",
}


class Diagram:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.declared: dict[str, int] = {}
        self.errors: list[str] = []
        self._parse()

    # -- parseo -------------------------------------------------------------
    def _parse(self) -> None:
        starts = self.text.count("@startuml")
        ends = self.text.count("@enduml")
        if starts != 1:
            self.errors.append(f"{self.path.name}: {starts} directivas @startuml (se esperaba 1)")
        if ends != 1:
            self.errors.append(f"{self.path.name}: {ends} directivas @enduml (se esperaba 1)")

        in_block_comment = False
        for lineno, raw in enumerate(self.text.splitlines(), 1):
            line = raw.strip()

            if line.startswith("'"):
                continue
            if line.startswith("/'"):
                in_block_comment = True
                continue
            if in_block_comment:
                if "'/" in line:
                    in_block_comment = False
                continue
            if not line or line.startswith("!"):
                continue

            self._declarations(line, lineno)

    def _declarations(self, line: str, lineno: int) -> None:
        # <tipo> "<alias>" [as <id>] [{ ... }]
        m = re.match(
            rf'^(?:package|rectangle|node|actor|cloud|database|folder|frame|state|component)\s+'
            rf'(?:"[^"]*"|[^\s{{]+)\s+(?:as\s+)?({ID})\s*(?:#\w+\s*)?\{{?$',
            line,
        )
        if m:
            self._declare(m.group(1), lineno)
            return
        # state "<alias>" as <id>
        m = re.match(rf'^state\s+"[^"]*"\s+as\s+({ID})\b', line)
        if m:
            self._declare(m.group(1), lineno)
            return
        # state "<alias>" as <id> {   (cluster)
        m = re.match(rf'^state\s+"[^"]*"\s+as\s+({ID})\s*\{{$', line)
        if m:
            self._declare(m.group(1), lineno)
            return
        # <id> : <alias>   (definición de estado con etiqueta)
        m = re.match(rf'^({ID})\s*:\s*.+$', line)
        if m and m.group(1) not in KEYWORDS:
            self._declare(m.group(1), lineno)
            return
        # [*] no se declara.

    def _declare(self, name: str, lineno: int) -> None:
        if name in self.declared:
            self.errors.append(
                f"{self.path.name}:{lineno}: identificador duplicado {name!r} "
                f"(ya declarado en la línea {self.declared[name]})"
            )
        else:
            self.declared[name] = lineno

    # -- referencias --------------------------------------------------------
    def references(self) -> set[str]:
        """Identificadores usados como origen/destino de aristas o transiciones."""
        refs: set[str] = set()
        for raw in self.text.splitlines():
            line = raw.strip()
            if line.startswith("'") or line.startswith("!") or not line:
                continue
            # A --> B : label   /  A -> B  /  A --> [*]
            m = re.match(rf'^({ID}|\[\*\])?\s*-+\>>?\s*({ID}|\[\*\])?\s*:', line)
            if m:
                for g in (m.group(1), m.group(2)):
                    if g and g != "[*]" and g not in KEYWORDS:
                        refs.add(g)
                continue
            # A --> B   (sin etiqueta)
            m = re.match(rf'^({ID}|\[\*\])?\s*-+\>>?\s*({ID}|\[\*\])?\s*$', line)
            if m:
                for g in (m.group(1), m.group(2)):
                    if g and g != "[*]" and g not in KEYWORDS:
                        refs.add(g)
                continue
            # nota "of X"
            m = re.search(rf'\bof\s+({ID})\b', line)
            if m and m.group(1) not in KEYWORDS:
                refs.add(m.group(1))
        return refs


def check_dangling(d: Diagram) -> list[str]:
    out: list[str] = []
    unknown = d.references() - set(d.declared)
    for name in sorted(unknown):
        out.append(f"{d.path.name}: referencia a elemento no declarado: {name!r}")
    return out


def check_state_machine(d: Diagram) -> list[str]:
    """Máquinas de estados: inicio presente y sin estados inalcanzables."""
    out: list[str] = []
    edges: dict[str, set[str]] = {}
    has_initial = False
    for raw in d.text.splitlines():
        line = raw.strip()
        if line.startswith("'") or line.startswith("!") or not line:
            continue
        m = re.match(rf'^({ID}|\[\*\])?\s*-+\>>?\s*({ID}|\[\*\])?\s*:', line)
        if not m:
            m = re.match(rf'^({ID}|\[\*\])?\s*-+\>>?\s*({ID}|\[\*\])?\s*$', line)
        if not m:
            continue
        src, dst = m.group(1), m.group(2)
        if src == "[*]":
            has_initial = True
            src = None
        if dst == "[*]":
            dst = None
        if src and dst:
            edges.setdefault(src, set()).add(dst)

    states = set(d.declared)
    if not has_initial:
        out.append(f"{d.path.name}: ninguna transición desde [*] (falta estado inicial)")
    # Un cluster (`state "X" as Y { ... }`) es un estado compuesto con su propia
    # máquina interna: es un punto de entrada válido por sí mismo, no un estado
    # hoja inalcanzable.
    clusters = {
        m.group(1)
        for m in re.finditer(
            r'^state\s+"[^"]*"\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:#\w+\s*)?\{$',
            d.text,
            re.M,
        )
    }
    reachable: set[str] = set()
    # alcanzabilidad desde los objetivos de [*]
    roots = set()
    for raw in d.text.splitlines():
        line = raw.strip()
        m = re.match(rf'^\[\*\]\s*-+\>>?\s*({ID})', line)
        if m:
            roots.add(m.group(1))
    stack = list(roots | clusters)
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(edges.get(cur, ()))
    for name in sorted(states - reachable):
        out.append(f"{d.path.name}: estado inalcanzable desde [*]: {name!r}")
    return out


#: Correspondencia explícita entre los participantes de la vista de
#: comportamiento (02-states) y los componentes de la vista estática
#: (01-components). Se declara a propósito en lugar de deducirla por coincidencia
#: de nombres: los participantes son agrupaciones de ciclo de vida, no
#: componentes, así que la correspondencia es una afirmación revisable y no un
#: accidente de nomenclatura.
PARTICIPANT_TO_COMPONENT = {
    "ComfyUILife": "comfyui",
    "ProdLife": "orch",
    "ShotLife": "orch",
    "EngineLife": "engines",
}


def check_cross_view(comp: Diagram, states: Diagram) -> list[str]:
    """Los participantes de 02-states deben mapear a componentes de 01-components."""
    out: list[str] = []
    participants = {
        m.group(1)
        for m in re.finditer(
            r'^state\s+"[^"]*"\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:#\w+\s*)?\{$',
            states.text,
            re.M,
        )
    }
    if not participants:
        out.append("02-states.puml: no se detectó ningún participante (check vacío)")
        return out
    comp_ids = set(comp.declared)
    for participant in sorted(participants):
        target = PARTICIPANT_TO_COMPONENT.get(participant)
        if target is None:
            out.append(
                f"02-states.puml: participante {participant!r} no está en "
                "PARTICIPANT_TO_COMPONENT (correspondencia sin declarar)"
            )
        elif target not in comp_ids:
            out.append(
                f"02-states.puml: {participant!r} mapea a {target!r}, que no es "
                "un componente declarado en 01-components.puml"
            )
    return out


def main() -> int:
    verbose = "--verbose" in sys.argv
    problems: list[str] = []

    for path in (COMPONENTS, STATES):
        if not path.exists():
            print(f"ERROR: falta {path}", file=sys.stderr)
            return 1
        d = Diagram(path)
        problems.extend(d.errors)
        problems.extend(check_dangling(d))
        if path is STATES:
            problems.extend(check_state_machine(d))
        if verbose:
            print(f"{path.name}: {len(d.declared)} elementos declarados")

    comp = Diagram(COMPONENTS)
    states = Diagram(STATES)
    problems.extend(check_cross_view(comp, states))

    if problems:
        print("ERROR: los diagramas PlantUML no son coherentes:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(
        f"OK: {len(comp.declared)} componentes y {len(states.declared)} estados; "
        "sin referencias colgantes, sin duplicados, con estado inicial y "
        "coherentes entre vistas."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
