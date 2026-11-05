#!/usr/bin/env python3
"""Validate the research specification, never implementation or GPU results."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise SystemExit("INVALID SPEC: " + message)


def unique_ids(rows, field, label):
    values = [row[field] for row in rows]
    require(len(values) == len(set(values)), f"duplicate {label}")
    return set(values)


def main():
    names = [
        "README.md", "GOAL.md", "MISSION.md", "requirements.md", "design.md",
        "tasks.md", "research.md", "programs.md", "acceptance.md",
        "capabilities.json", "engines.json", "backlog.json", "sources.json",
        "sources.md", "snapshot.json", "runner-guide.md",
        "contexto/Marco_Anterior.md",
    ]
    for name in names:
        require((ROOT / name).is_file(), f"missing {name}")
    goal = (ROOT / "GOAL.md").read_text(encoding="utf-8")
    require(len(goal) < 16000, "goal exceeds 16000 characters")
    require(len(goal) < 2000, "portable goal is no longer concise")
    caps = read_json("capabilities.json")["capabilities"]
    engines = read_json("engines.json")["engines"]
    work = read_json("backlog.json")["work_items"]
    sources = read_json("sources.json")["sources"]
    c_ids = unique_ids(caps, "id", "capability IDs")
    e_ids = unique_ids(engines, "id", "engine IDs")
    w_ids = unique_ids(work, "id", "work IDs")
    s_ids = unique_ids(sources, "id", "source IDs")
    require(c_ids == {f"C{i:02}" for i in range(1, 35)}, "C01-C34 coverage")
    require(w_ids == {f"W{i:02}" for i in range(27)}, "W00-W26 coverage")
    n_ids = [e["catalog_id"] for e in engines if "catalog_id" in e]
    require(set(n_ids) == {f"N{i:02}" for i in range(1, 31)}, "N01-N30 coverage")
    require(len(n_ids) == 30, "duplicate N candidates")
    programs = (ROOT / "programs.md").read_text(encoding="utf-8")
    p_ids = set(re.findall(r"^## (P[0-9]{2})[.]", programs, re.MULTILINE))
    require(p_ids == {f"P{i:02}" for i in range(1, 15)}, "P01-P14 coverage")
    acceptance = (ROOT / "acceptance.md").read_text(encoding="utf-8")
    a_ids = set(re.findall(r"^### (A[0-9]{2})[.]", acceptance, re.MULTILINE))
    require(a_ids == {f"A{i:02}" for i in range(30)}, "A00-A29 coverage")
    deps = {}
    for item in work:
        require(set(item["depends_on"]) <= w_ids, item["id"] + " missing dependency")
        require(set(item["programs"]) <= p_ids, item["id"] + " missing program")
        require(set(item["acceptance_ids"]) <= a_ids, item["id"] + " missing acceptance")
        require(set(item["capabilities"]) <= c_ids, item["id"] + " missing capability")
        deps[item["id"]] = item["depends_on"]
    visiting, done = set(), set()

    def visit(node):
        require(node not in visiting, f"dependency cycle at {node}")
        if node in done:
            return
        visiting.add(node)
        for dep in deps[node]:
            visit(dep)
        visiting.remove(node)
        done.add(node)

    for node in deps:
        visit(node)
    for cap in caps:
        require(cap["mandatory"], cap["id"] + " silently demoted")
        require(set(cap["engines"]) <= e_ids, cap["id"] + " missing engine")
        require(set(cap["work_items"]) <= w_ids, cap["id"] + " missing work item")
        require(set(cap["programs"]) <= p_ids, cap["id"] + " missing program")
        require(set(cap["sources"]) <= s_ids, cap["id"] + " missing source")
    for engine in engines:
        require(set(engine["source_ids"]) <= s_ids, engine["id"] + " missing source")
        require(not engine["runtime_installs_allowed"], engine["id"] + " runtime install")
        require(
            not engine["build_selected"] or engine["disposition"] == "required",
            engine["id"] + " conditional/excluded engine selected without review",
        )
    later = next(item for item in work if item["id"] == "W26")
    require(later["scope"] == "later_gpu", "GPU scope no longer explicit")
    require(
        later["status"] == "pending_separate_gpu_authorization",
        "GPU work must remain unauthorized in the original specification",
    )
    for path in ROOT.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            if target and not any(ch in target for ch in "<>"):
                require((path.parent / target).exists(), f"broken local link: {path.name}: {target}")
    print(
        f"SPEC OK: {len(caps)} capabilities, {len(p_ids)} programs, "
        f"{len(n_ids)} candidates, {len(work)} work packages, "
        f"{len(sources)} sources; goal {len(goal)} characters."
    )
    print("Structural validation only. App implementation and GPU evidence are not certified.")


if __name__ == "__main__":
    main()

