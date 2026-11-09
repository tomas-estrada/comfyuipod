#!/usr/bin/env python3
"""Mantiene los hashes CSP de `config/nginx.conf` en sincronía con el HTML.

`/home` (portal) y `/status` incrustan exactamente un `<style>` y un `<script>`
inline. En lugar de `'unsafe-inline'` — que anula la protección contra XSS de la
directiva `script-src` — la CSP usa el hash sha256 del contenido de cada bloque.

Riesgo que este script elimina: si alguien edita `config/portal/index.html` o
`comfyctl/src/status_html.html` sin regenerar el hash, el navegador rechaza el
bloque y la página queda en blanco. `--check` (el modo que usa CI) falla en ese
caso en vez de dejar pasar el desfase.

Uso:
    python3 scripts/update_csp_hashes.py            # reescribe nginx.conf
    python3 scripts/update_csp_hashes.py --check    # solo verifica (CI)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NGINX = REPO_ROOT / "config" / "nginx.conf"

#: fichero -> ubicaciones de nginx que lo sirven (para el mensaje de error).
TARGETS: tuple[tuple[Path, str], ...] = (
    (REPO_ROOT / "config" / "portal" / "index.html", "location = /home"),
    (REPO_ROOT / "comfyctl" / "src" / "status_html.html", "grupo /status"),
)


def block_hashes(path: Path) -> dict[str, str]:
    """Hash sha256 (base64) del contenido de los bloques `<style>`/`<script>`.

    Normaliza CRLF -> LF porque el `Dockerfile` hace exactamente eso con
    `/etc/comfyui/portal/index.html` antes de servirlo: si no, el hash se
    calcularía sobre un contenido que el navegador nunca ve.
    """
    text = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    out: dict[str, str] = {}
    for tag in ("style", "script"):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if match is None:
            raise SystemExit(f"{path}: no se encontró el bloque <{tag}>")
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        out[tag] = base64.b64encode(digest).decode("ascii")
    return out


def expected_policy(hashes: dict[str, str]) -> str:
    return (
        "default-src 'self'; img-src 'self' data:; "
        f"style-src 'self' 'sha256-{hashes['style']}'; "
        f"script-src 'self' 'sha256-{hashes['script']}'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="No escribe nada; falla si nginx.conf está desfasado (modo CI).",
    )
    args = parser.parse_args()

    conf = NGINX.read_text(encoding="utf-8")
    problems: list[str] = []
    # Las directivas reales, sin comentarios: los comentarios mencionan
    # 'unsafe-inline' a propósito explicativo y no deben disparar el aviso.
    directives = "\n".join(
        line for line in conf.splitlines() if not line.lstrip().startswith("#")
    )

    for path, where in TARGETS:
        hashes = block_hashes(path)
        policy = expected_policy(hashes)
        if f'"{policy}"' in conf:
            print(f"[OK] {where}: CSP con hash sincronizada ({path.name})")
            continue
        if "'unsafe-inline'" in directives:
            problems.append(
                f"{where}: la CSP sigue usando 'unsafe-inline' para {path.name}"
            )
        else:
            problems.append(
                f"{where}: el hash de {path.name} no coincide con nginx.conf "
                "(se editó el HTML sin regenerar; la página quedaría en blanco)"
            )
        if not args.check:
            # Sustituir la directiva de esa ubicación concreta.
            conf = _replace_policy(conf, where, policy)
            NGINX.write_text(conf, encoding="utf-8")
            print(f"[FIX] {where}: CSP actualizada con el hash de {path.name}")

    if problems and args.check:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print(
            "\nEjecuta `python3 scripts/update_csp_hashes.py` para regenerar.",
            file=sys.stderr,
        )
        return 1
    return 0


def _replace_policy(conf: str, where: str, policy: str) -> str:
    """Reescribe la directiva Content-Security-Policy de la ubicación `where`."""
    if where.startswith("location = "):
        anchor = f"        {where} {{"
    else:
        anchor = "        location ~ ^/(status|status/api|report\\.md|diag|logs/.*)$ {"
    start = conf.index(anchor)
    # El bloque termina en la llave de cierre al mismo nivel.
    end = conf.index("\n        }", start)
    block = conf[start:end]
    new_block = re.sub(
        r'add_header Content-Security-Policy "[^"]*" always;',
        f'add_header Content-Security-Policy "{policy}" always;',
        block,
    )
    if "Content-Security-Policy" not in new_block:
        raise SystemExit(f"{where}: no se encontró la directiva CSP que reemplazar")
    return conf[:start] + new_block + conf[end:]


if __name__ == "__main__":
    raise SystemExit(main())
