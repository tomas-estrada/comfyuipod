"""Manifest-backed CLI adapter for the legacy shell downloader."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from comfy_studio import model_assets
from comfy_studio.asset_acquire import acquire


def _manifest_candidates() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2]
    result: list[Path] = []
    for raw in (
        os.environ.get("STUDIO_CORE_MANIFEST", "/etc/comfyui/models-core.json"),
        os.environ.get("STUDIO_MANIFEST", "/etc/comfyui/models-studio.json"),
    ):
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        if path not in result:
            result.append(path)
    return tuple(result)


def _find_need(repo: str, remote: str, dest: Path, repo_type: str) -> SimpleNamespace:
    env = dict(os.environ)
    matches: list[SimpleNamespace] = []
    for manifest in _manifest_candidates():
        try:
            models = json.loads(manifest.read_text(encoding="utf-8")).get("models", [])
        except (OSError, ValueError, AttributeError):
            continue
        for item in models:
            if not isinstance(item, dict):
                continue
            if (item.get("repo_id"), item.get("remote_path"), item.get("repo_type", "model")) != (
                repo,
                remote,
                repo_type,
            ):
                continue
            raw_dest = item.get("dest_path")
            if not raw_dest:
                continue
            normalized_dest = str(raw_dest).replace("${COMFY_DATA_DIR}", "{COMFY_DATA_DIR}")
            normalized_dest = normalized_dest.replace("${COMFY_APP_DIR}", "{COMFY_APP_DIR}")
            if model_assets.resolve_dest(normalized_dest, env) != dest:
                continue
            matches.append(
                SimpleNamespace(
                    filename=str(item.get("filename") or dest.name),
                    dest_path=str(dest),
                    bytes=int(item.get("bytes") or 0),
                    asset_class=str(item.get("class") or "task"),
                    present=False,
                    source=manifest.name,
                    repo_id=repo,
                    remote_path=remote,
                    repo_type=repo_type,
                    license=item.get("license"),
                    url=item.get("url"),
                    manifest_dest=str(raw_dest),
                    revision=item.get("revision"),
                    sha256=item.get("sha256"),
                    sha1=item.get("sha1"),
                )
            )
    if not matches:
        raise ValueError(f"asset is not declared at destination in configured manifests: {repo}/{remote}")
    signatures = {
        (m.bytes, m.revision, m.sha256, m.sha1, m.manifest_dest)
        for m in matches
    }
    if len(signatures) != 1:
        raise ValueError(f"conflicting manifest declarations for {repo}/{remote} at {dest}")
    need = matches[0]
    if not need.revision:
        raise ValueError(f"manifest entry lacks a pinned revision: {repo}/{remote}")
    if not need.sha256 and not need.sha1:
        raise ValueError(f"manifest entry lacks an integrity digest: {repo}/{remote}")
    return need


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--repo-type", default="model")
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        need = _find_need(args.repo, args.remote, args.dest, args.repo_type)
    except (OSError, ValueError) as exc:
        print(f"asset acquisition rejected: {exc}", file=sys.stderr)
        return 2
    report = acquire(
        [need],
        env=dict(os.environ),
        hf_cli=os.environ.get("HF_CLI"),
        stage_dir=args.stage,
        timeout_s=args.timeout,
        margin_bytes=int(os.environ.get("HF_DISK_MARGIN_BYTES", "10737418240")),
        max_attempts=1,
    )
    entry = report.entries[0]
    if entry.status not in {"downloaded", "skipped"} or entry.error:
        print(f"asset acquisition failed: {entry.error or entry.status}", file=sys.stderr)
        return 1
    print(f"{entry.status}: {entry.filename} ({entry.bytes} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
