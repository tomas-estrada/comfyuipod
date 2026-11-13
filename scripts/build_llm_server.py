#!/usr/bin/env python3
"""Build the pinned llama.cpp CPU server and retain its upstream notices."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _read_memory_limit() -> int:
    limits = []
    for limit_path, usage_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        try:
            raw = limit_path.read_text().strip()
            value = int(raw)
        except (OSError, ValueError):
            continue
        if value > 0 and value < (1 << 60):
            try:
                used = int(usage_path.read_text().strip())
            except (OSError, ValueError):
                used = 0
            limits.append(max(0, value - used))
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                limits.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        available = page_size * available_pages
        if available > 0:
            limits.append(available)
    except (OSError, ValueError):
        pass
    if not limits:
        try:
            physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if physical > 0:
                limits.append(physical)
        except (OSError, ValueError):
            pass
    return min(limits) if limits else 4 * 1024**3


def _available_cpu_count() -> int:
    counts = []
    try:
        counts.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    for quota_path, period_path in (
        (Path("/sys/fs/cgroup/cpu.max"), None),
        (Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")),
    ):
        try:
            if period_path is None:
                quota, period = quota_path.read_text().split()
                if quota == "max":
                    continue
                quota_value, period_value = int(quota), int(period)
            else:
                quota_value = int(quota_path.read_text())
                period_value = int(period_path.read_text())
                if quota_value < 0:
                    continue
            if quota_value > 0 and period_value > 0:
                counts.append(max(1, quota_value // period_value))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    counts.append(os.cpu_count() or 1)
    return max(1, min(counts))


def build_jobs(memory_bytes: int, cpu_count: int | None = None) -> int:
    """Bound parallel compile jobs to both visible CPUs and available RAM."""
    cpus = cpu_count or _available_cpu_count()
    # llama.cpp C++ compilation is memory-heavy; budget 4 GiB per worker.
    return max(1, min(cpus, memory_bytes // (4 * 1024**3) or 1))


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def build(lock_path: Path, install_prefix: Path, work_root: Path | None = None) -> None:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    revision = lock["revision"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("revision debe ser un SHA Git de 40 caracteres")
    if lock["architecture"] != "linux-amd64":
        raise ValueError("este lock solo declara linux-amd64")

    if work_root is None:
        temp = tempfile.TemporaryDirectory(prefix="llama-cpp-build-")
        root = Path(temp.name)
    else:
        temp = None
        root = work_root
        root.mkdir(parents=True, exist_ok=True)
    try:
        source = root / "source"
        build_dir = root / "build"
        _run(["git", "init", str(source)])
        _run(["git", "-C", str(source), "remote", "add", "origin", lock["source"]])
        _run(["git", "-C", str(source), "fetch", "--depth=1", "origin", revision])
        _run(["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"])
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != revision:
            raise RuntimeError(f"checkout cambió de pin: {actual} != {revision}")

        options = [f"-D{key}={value}" for key, value in lock["cmake_options"].items()]
        _run(["cmake", "-S", str(source), "-B", str(build_dir), *options])
        jobs = build_jobs(_read_memory_limit())
        _run(
            [
                "cmake",
                "--build",
                str(build_dir),
                "--target",
                lock["target"],
                "--parallel",
                str(jobs),
            ]
        )
        binary = build_dir / "bin" / lock["target"]
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"no se generó el binario ejecutable {binary}")
        install_prefix.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, install_prefix / "llama-server")

        notice_root = Path("/usr/share/licenses/llama.cpp")
        for relative in lock["license_files"]:
            source_notice = source / relative
            if not source_notice.is_file():
                raise RuntimeError(f"notice listado en lock ausente: {relative}")
            destination = notice_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_notice, destination)
        _run([str(install_prefix / "llama-server"), "--version"])
    finally:
        if temp is not None:
            temp.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--install-prefix", type=Path, default=Path("/usr/local/bin"))
    args = parser.parse_args()
    build(args.lock, args.install_prefix)


if __name__ == "__main__":
    main()
