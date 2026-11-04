#!/usr/bin/env python3
"""Fija `revision` y `sha256` en un manifiesto de modelos.

La política exige verificar «source, revision, license, size and integrity where
available». HuggingFace publica ambos en la respuesta `HEAD` de `resolve`:

  - `X-Repo-Commit`: el commit exacto del fichero (revisión inmutable);
  - `X-Linked-Etag`: el SHA-256 hex del objeto LFS (integridad).

Con ellos la URL de descarga deja de apuntar a `main` (mutable) y pasa a apuntar
a la revisión fijada, y el cliente puede verificar el hash tras descargar.

Uso:  python3 scripts/pin_manifest_integrity.py [manifest ...]

Sin argumentos procesa los dos manifiestos conocidos. Es idempotente: reejecutar
con las mismas respuestas no cambia el fichero.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ("config/models-studio.json", "config/models-core.json")

HEX_DIGITS = set("0123456789abcdef")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """No seguir el 302: las cabeceras que interesan las pone huggingface.co,
    no el CDN de xet al que redirige."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

#: Saltos máximo siguiendo renombres de repo dentro de hugginggingface.co.
_MAX_HOPS = 3


def head_metadata(url: str, attempts: int = 3) -> tuple[str | None, str | None, str | None]:
    """Devuelve (revision, sha256_o_sha1, url_resuelta).

    Sigue como máximo `_MAX_HOPS` redirecciones *dentro* de huggingface.co (los
    renombres de repo, p. ej. `flux1-dev` -> `Flux-1-Dev`), pero nunca la del CDN
    de xet: es esa respuesta intermedia la que publica `X-Repo-Commit` y
    `X-Linked-Etag`.
    """
    current = url
    for _hop in range(_MAX_HOPS):
        for _attempt in range(attempts):
            try:
                request = urllib.request.Request(current, method="HEAD")
                with _OPENER.open(request, timeout=30) as response:
                    headers, location = response.headers, None
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    break
                headers, location = exc.headers, exc.headers.get("location")
            except OSError:
                break

            revision = headers.get("x-repo-commit")
            digest = (headers.get("x-linked-etag") or "").strip().strip('"').lower()
            if revision and digest:
                return revision, digest, current
            if location and location.startswith("/"):
                current = f"https://huggingface.co{location}"
                break
        else:  # pragma: no cover - sin reintentos pendientes
            continue
    return None, None, None


def _repo_from_url(url: str) -> str | None:
    """`https://huggingface.co/datasets/A/B/resolve/<rev>/x` -> `A/B`."""
    marker = "/resolve/"
    if marker not in url:
        return None
    head = url.split(marker, 1)[0]
    for prefix in ("https://huggingface.co/datasets/", "https://huggingface.co/"):
        if head.startswith(prefix):
            return head[len(prefix) :]
    return None


def _is_sha256(value: object) -> bool:    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= HEX_DIGITS
    )

def pin(path: Path) -> int:
    if not path.is_file():
        print(f"ERROR: no existe {path}", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    models = data.get("models") or []
    if not models:
        print(f"ERROR: {path} no tiene entradas", file=sys.stderr)
        return 1

    def resolve(entry: dict) -> tuple[dict, str | None, str | None, str | None]:
        url = str(entry.get("url") or "")
        revision, digest, resolved = head_metadata(url)
        return entry, revision, digest, resolved

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(resolve, models))

    changed = 0
    pinned = {"sha256": 0, "sha1": 0, "none": 0}
    for entry, revision, digest, resolved in results:
        if not revision:
            print(
                f"ERROR: sin metadatos de integridad para {entry.get('filename')!r} "
                f"({entry.get('url')})",
                file=sys.stderr,
            )
            return 1

        # HF publica el SHA-256 del objeto LFS; para ficheros pequeños guardados
        # en git publica el SHA-1 del blob. Se registra el que haya («where
        # available»), con el algoritmo explícito.
        algorithm = "sha256" if digest and len(digest) == 64 else "sha1" if digest else None
        if algorithm:
            pinned[algorithm] += 1
        else:
            pinned["none"] += 1

        before = json.dumps(entry, sort_keys=True)
        entry["revision"] = revision
        if algorithm:
            entry[algorithm] = digest
        for stale in ("sha256", "sha1"):
            if stale != algorithm and stale in entry:
                del entry[stale]
        if resolved:
            # La URL canónica apunta a la revisión fijada, no a `main`.
            pinned_url = resolved.replace("/resolve/main/", f"/resolve/{revision}/")
            if entry.get("url") != pinned_url:
                entry["url"] = pinned_url
            # Si el repo cambió de nombre aguas arriba, `repo_id` debe seguirlo.
            resolved_repo = _repo_from_url(pinned_url)
            if resolved_repo and entry.get("repo_id") != resolved_repo:
                entry["repo_id"] = resolved_repo
                print(
                    f"  nota: {entry.get('filename')!r} repositorio renombrado "
                    f"-> {resolved_repo}"
                )
        if json.dumps(entry, sort_keys=True) != before:
            changed += 1

    meta = data.setdefault("meta", {})
    integrity = {
        "algorithm": "sha256 (LFS) / sha1 (git blob) según publique HuggingFace",
        "source": "HuggingFace X-Linked-Etag",
        "pinned_entries": pinned["sha256"] + pinned["sha1"],
        "total_entries": len(models),
        "by_algorithm": dict(pinned),
    }
    if meta.get("integrity") != integrity:
        meta["integrity"] = integrity
        changed += 1

    if changed:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print(f"{path}: {pinned}/{len(models)} entradas fijadas ({changed} cambios)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", nargs="*", help="Manifiestos a fijar.")
    args = parser.parse_args()
    targets = [Path(name) for name in (args.manifests or MANIFESTS)]
    status = 0
    for target in targets:
        status |= pin(target)
    return 1 if status else 0


if __name__ == "__main__":
    raise SystemExit(main())
