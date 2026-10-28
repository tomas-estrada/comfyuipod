"""Punto de entrada CLI (`python -m comfy_studio.server`) invocado por comfyctl."""

from __future__ import annotations

import argparse

import uvicorn

from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig, sanitize_loopback_host


def main(argv: list[str] | None = None) -> None:
    cfg = StudioConfig.from_env()
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
