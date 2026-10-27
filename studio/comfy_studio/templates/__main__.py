"""CLI de validación estructural de plantillas (`python -m comfy_studio.templates`)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .validator import TemplateValidationError, validate_all_templates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Valida estructuralmente las plantillas ComfyUI de comfy_studio."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directorio opcional con archivos JSON de plantillas.",
    )
    args = parser.parse_args(argv)

    try:
        validated = validate_all_templates(args.data_dir)
    except TemplateValidationError as exc:
        print(f"ERROR de validación de plantillas: {exc}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(validated)} plantillas validadas correctamente: "
        f"{', '.join(sorted(validated.keys()))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
