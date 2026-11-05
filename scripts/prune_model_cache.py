#!/usr/bin/env python3
"""Aplica la política de retención de caché de pesos.

Por defecto **no borra nada**: muestra el plan. Con `--apply` lo ejecuta.

    python3 scripts/prune_model_cache.py                 # plan (dry-run)
    python3 scripts/prune_model_cache.py --budget-gb 120 # con presupuesto
    python3 scripts/prune_model_cache.py --apply         # ejecuta

La política (studio/comfy_studio/cache_policy.py) conserva siempre `bootstrap`
y `optional`, y solo expulsa activos `task` cuando el volumen supera el
presupuesto, por último uso real. Nunca considera rutas fuera de los
manifiestos, así que media del usuario, proyectos y salidas quedan fuera por
construcción.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "studio"))

from comfy_studio.cache_policy import RetentionPolicy, apply_plan, plan_retention  # noqa: E402

GiB = 1024**3


def _gb(value: int) -> str:
    return f"{value / GiB:.2f} GiB"


def render(plan, policy: RetentionPolicy) -> str:
    lines = [
        "Política de retención "
        f"v{policy.version} (presupuesto: "
        + (f"{_gb(policy.budget_bytes)}" if policy.budget_bytes else "sin límite")
        + ")",
        "",
        f"  activos en disco        {plan.keep_count} ({_gb(plan.keep_bytes)})",
        f"  a liberar               {len(plan.evictions)} ({_gb(plan.freed_bytes)})",
        f"  staging a limpiar       {len(plan.staging_cleanups)}",
    ]
    if plan.protected_seen:
        lines.append(f"  rutas protegidas vistas {len(plan.protected_seen)} (no se tocan)")
    if plan.evictions:
        lines.append("")
        lines.append("  expulsiones propuestas (task, más antiguo primero):")
        for eviction in plan.evictions:
            if eviction.last_used_ts is None:
                used = "nunca usado"
            else:
                age = int(max(0, time.time() - eviction.last_used_ts))
                used = f"usado hace {age}s"
            lines.append(
                f"    - {eviction.filename} [{eviction.asset_class}] "
                f"{_gb(eviction.bytes)} ({used})"
            )
    if plan.staging_cleanups:
        lines.append("")
        lines.append("  staging inactivo:")
        for staging in plan.staging_cleanups:
            lines.append(f"    - {staging['path']} ({_gb(staging['bytes'])})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Ejecuta el plan (por defecto no).")
    parser.add_argument("--budget-gb", type=float, default=None, help="Presupuesto en GiB.")
    parser.add_argument("--budget-bytes", type=int, default=None, help="Presupuesto en bytes.")
    parser.add_argument(
        "--staging-max-age-days",
        type=float,
        default=7.0,
        help="Edad máxima de un parcial de staging antes de limpiarlo.",
    )
    parser.add_argument("--json", action="store_true", help="Salida JSON.")
    args = parser.parse_args(argv)

    policy = RetentionPolicy(
        budget_bytes=(
            args.budget_bytes
            if args.budget_bytes is not None
            else int(args.budget_gb * GiB) if args.budget_gb is not None else None
        ),
        staging_max_age_s=int(args.staging_max_age_days * 24 * 3600),
    )

    plan = plan_retention(policy=policy)
    result = apply_plan(plan, dry_run=not args.apply)

    if args.json:
        payload = {
            "policy": policy.to_public_dict(),
            "plan": plan.to_public_dict(),
            "result": result,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(plan, policy))
        print()
        if args.apply:
            print(f"  aplicado: {result}")
        else:
            print("  DRY-RUN: no se ha borrado nada (usa --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
