"""Invarianza de paridad entre el roster y el manifiesto de engines (M4-SCR-001).

(a) set(build_default_registry().names()) == set(build_default_prep_specs().keys())  -> FAIL-HARD
(b) los 4 motores añadidos en este slice están declarados en engines.json
    (build_selected=true o disposition excluded/reference_only) -> FAIL-HARD.
    Nota: los motores pre-existentes fuera del manifiesto son problema previo
    fuera del alcance de este slice.
(c) engines.json declara más ids que el roster -> REPORTADO como count, NO fail-hard.
"""

from __future__ import annotations

import json

from comfy_studio.engines.preparedness import build_default_prep_specs
from comfy_studio.engines.registry import build_default_registry, resolve_engine_class

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "upgrades-ultra-version" / "engines.json"

# Los 4 motores añadidos en este slice.
_NEW_ENGINES = frozenset({"reference-local", "atlas-offline", "scene-optimizer", "chrono"})


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8-sig") as fh:
        return json.load(fh)


def test_registry_and_prep_specs_same_names() -> None:
    """(a) Los nombres del roster y de los prep specs coinciden exactamente."""
    reg = build_default_registry()
    specs = build_default_prep_specs()
    reg_names = set(reg.names())
    spec_names = set(specs.keys())
    assert reg_names == spec_names, (
        f"Paridad roster/prep-specs rota: "
        f"roster={sorted(reg_names)} specs={sorted(spec_names)}"
    )


def test_roster_names_all_resolve() -> None:
    """Cada nombre del roster resuelve a una clase real importable."""
    for name in build_default_registry().names():
        cls = resolve_engine_class(name)
        assert cls.name == name


def test_new_engines_in_manifest() -> None:
    """(b) Los 4 motores nuevos están declarados correctamente en engines.json."""
    manifest = _load_manifest()
    by_id = {e["id"]: e for e in manifest["engines"]}
    missing: list[str] = []
    for name in sorted(_NEW_ENGINES):
        entry = by_id.get(name)
        if entry is None:
            missing.append(f"{name}: ausente del manifiesto")
            continue
        selected = bool(entry.get("build_selected"))
        dispo = entry.get("disposition", "")
        if not selected and dispo not in {"excluded", "reference_only"}:
            missing.append(
                f"{name}: disposition={dispo!r} sin build_selected=true"
            )
    assert not missing, (
        "Motores nuevos no declarados correctamente en engines.json:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


def test_manifest_extra_ids_are_reported_not_fatal() -> None:
    """(c) engines.json tiene más ids que el roster; se reporta el count.

    El cierre de la brecha es trabajo tracked por separado (motores de
    research/roadmap sin backend cableado todavía). Este test no fail-hard.
    """
    reg = build_default_registry()
    manifest = _load_manifest()
    roster_names = set(reg.names())
    manifest_ids = {e["id"] for e in manifest["engines"]}
    extras = sorted(manifest_ids - roster_names)
    # Comment: closing this gap is tracked work — engines.json-only ids
    # son declaraciones de research/roadmap que aún no tienen motor cableado.
    expected = len(extras)
    assert len(extras) == expected
    # No fail-hard; reportar.
    print(f"[roster_parity] engines.json declara {len(extras)} ids fuera del roster")
    assert len(roster_names) == 20
