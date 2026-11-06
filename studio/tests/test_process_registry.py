from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from comfy_studio.engines.process import run_cancellable
from comfy_studio.engines.process_registry import (
    cleanup_process_groups,
    process_group_scope,
    process_groups_clean,
    register_process_group,
)
from comfy_studio.orchestration.gpu_lease import AdmissionBudget, GpuLease


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="production leases run on Linux")
def test_orphan_lease_cleanup_stops_registered_child_group(tmp_path: Path) -> None:
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()

    owner = GpuLease(
        lock_dir=tmp_path / "lease",
        budget=AdmissionBudget(vram_mb=96_000, ram_mb=256_000),
        pid=dead_owner.pid,
    )
    grant = owner.acquire(owner="orphan", engine="upscaler", vram_mb=1000, ram_mb=1000)
    assert grant is not None
    process_dir = owner.child_process_dir(grant)

    with process_group_scope(process_dir):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        marker = register_process_group(child.pid)
    assert marker is not None and marker.exists()

    contender = GpuLease(
        lock_dir=tmp_path / "lease",
        budget=AdmissionBudget(vram_mb=96_000, ram_mb=256_000),
        recover_orphan=lambda orphan: cleanup_process_groups(
            tmp_path / "lease" / "children" / str(orphan.token)
        ),
    )
    recovered = contender.acquire(
        owner="next-engine", engine="qa", vram_mb=1000, ram_mb=1000
    )
    child.wait(timeout=3)
    assert recovered is not None and recovered.owner == "next-engine"
    assert child.poll() is not None
    assert process_groups_clean(process_dir)
    assert contender.release(recovered)["released"] is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="production leases run on Linux")
def test_failed_process_cleanup_keeps_lease_quarantined(tmp_path: Path) -> None:
    lease = GpuLease(
        lock_dir=tmp_path / "lease",
        budget=AdmissionBudget(vram_mb=96_000, ram_mb=256_000),
    )
    grant = lease.acquire(owner="owner", engine="upscaler", vram_mb=1000, ram_mb=1000)
    assert grant is not None

    result = lease.release(
        grant,
        cleanup_verified=False,
        unload=lambda: pytest.fail("unsafe unload"),
    )

    assert result["released"] is False
    assert result["cleanup_failed"] is True
    assert lease.current() == grant
    assert lease.release(grant, unload=lambda: True)["released"] is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="production leases run on Linux")
def test_leased_runner_reaps_grandchild_after_command_exits(tmp_path: Path) -> None:
    process_dir = tmp_path / "children" / "token"
    child_code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )

    with process_group_scope(process_dir):
        result = run_cancellable(
            [sys.executable, "-c", child_code], capture_output=True, timeout=5
        )

    assert result.returncode == 0
    assert process_groups_clean(process_dir)
