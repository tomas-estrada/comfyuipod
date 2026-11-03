"""Punto de entrada CLI (`python -m comfy_studio.server`) invocado por comfyctl."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig, sanitize_loopback_host


def _resolve_workers() -> int:
    """Resuelve `STUDIO_SERVER_WORKERS` (M7-OPS-004).

    Solo se admite 1: el `ProgressBroker` de SSE es in-memory y cada worker
    tendría su propia copia, así que con varios workers los eventos de
    progreso/job no llegarían a todos los clientes y el estado divergiría.
    """
    raw = os.environ.get("STUDIO_SERVER_WORKERS", "1").strip() or "1"
    if not raw.isdigit() or int(raw) < 1:
        print(
            f"ERROR: STUDIO_SERVER_WORKERS='{raw}' inválido; debe ser un entero >= 1 (usa 1).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return int(raw)


def main(argv: list[str] | None = None) -> None:
    cfg = StudioConfig.from_env()
    workers = _resolve_workers()
    if workers != 1:
        print(
            f"ERROR: STUDIO_SERVER_WORKERS={workers} no está soportado: el servidor usa un "
            "ProgressBroker SSE en memoria y estado por proceso; con varios workers los "
            "eventos no llegarían a todos los clientes. Deja STUDIO_SERVER_WORKERS=1.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    parser = argparse.ArgumentParser(description="Comfy Studio FastAPI Server")
    parser.add_argument(
        "--host",
        default=cfg.studio_server_host,
        help="Host de escucha (por defecto 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=cfg.studio_server_port,
        help="Puerto de escucha (por defecto 18190)",
    )
    args = parser.parse_args(argv)
    app = create_app(cfg)
    host = sanitize_loopback_host(args.host)
    uvicorn.run(app, host=host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
