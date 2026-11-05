"""Guarda de espacio en disco de scripts/download-models.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "download-models.sh"


def _extract_disk_functions() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    blocks: list[str] = []
    for name in ("disk_free_bytes", "require_disk_space"):
        start = text.index(f"{name}() {{")
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        blocks.append(text[start:end])
    return "\n\n".join(blocks)


def _run_disk_guard(
    func_call: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if not SCRIPT.is_file():
        pytest.skip(f"script no encontrado: {SCRIPT}")
    run_env = os.environ.copy()
    run_env.update(
        {
            "HF_DISK_GUARD": "1",
            "HF_DISK_MARGIN_BYTES": "0",
            "COMFY_DATA_DIR": "/tmp/comfyui-data-test",
            "COMFY_APP_DIR": "/tmp/comfyui-app-test",
        }
    )
    if env:
        run_env.update(env)
    # El script no es source-safe (detecta HF_CLI y el case final ejecuta fases),
    # así que evaluamos solo las funciones del guardia bajo un stub de log.
    snippet = f"""
set -euo pipefail
log() {{ :; }}
DISK_SAFETY_MARGIN_BYTES="${{HF_DISK_MARGIN_BYTES:-0}}"
HF_DISK_GUARD="${{HF_DISK_GUARD:-1}}"
{_extract_disk_functions()}
{func_call}
"""
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=30,
        check=False,
    )


def test_require_disk_space_succeeds_when_staging_path_missing_under_existing_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "models"
    parent.mkdir()
    missing_staging = parent / ".hf_stage" / "nested"
    assert not missing_staging.exists()
    result = _run_disk_guard(f'require_disk_space "{missing_staging}" 0')
    assert result.returncode == 0, result.stderr + result.stdout


def test_require_disk_space_fails_closed_when_space_unmeasurable() -> None:
    fake_bin = Path(tempfile.mkdtemp())
    try:
        df_path = fake_bin / "df"
        df_path.write_text("#!/usr/bin/env bash\necho bad\n", encoding="utf-8")
        df_path.chmod(0o755)
        result = _run_disk_guard(
            'require_disk_space "/definitely/missing/stage" 0',
            env={"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
        )
        assert result.returncode == 1
    finally:
        shutil.rmtree(fake_bin, ignore_errors=True)
