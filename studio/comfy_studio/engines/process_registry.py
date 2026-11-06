"""Track child process groups owned by a heavy engine lease."""

from __future__ import annotations

import contextvars
import glob
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_ACTIVE_DIRECTORY: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "studio_engine_process_directory", default=None
)


def process_group_scope_active() -> bool:
    return _ACTIVE_DIRECTORY.get() is not None


@contextmanager
def process_group_scope(directory: Path) -> Iterator[None]:
    """Register process groups started during one leased engine invocation."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = _ACTIVE_DIRECTORY.set(directory)
    try:
        yield
    finally:
        _ACTIVE_DIRECTORY.reset(token)


def register_process_group(pgid: int) -> Path | None:
    directory = _ACTIVE_DIRECTORY.get()
    if directory is None:
        return None
    if os.name != "posix" or pgid <= 1 or pgid == os.getpgrp():
        raise RuntimeError("unsafe engine process group")
    marker = directory / str(pgid)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(marker, flags, 0o600)
    try:
        os.write(fd, f"{pgid}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return marker


def unregister_process_group(marker: Path | None) -> None:
    if marker is not None:
        marker.unlink(missing_ok=True)


def process_group_exists(pgid: int) -> bool:
    if os.name != "posix":
        return True
    if os.path.isdir("/proc"):
        try:
            for stat_path in glob.glob("/proc/[0-9]*/stat"):
                try:
                    raw = Path(stat_path).read_text(encoding="ascii")
                    fields = raw[raw.rfind(")") + 2 :].split()
                    state, group = fields[0], int(fields[2])
                except (OSError, ValueError, IndexError):
                    continue
                if group == pgid and state not in {"Z", "X"}:
                    return True
            return False
        except OSError:
            pass
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(pgid: int, *, grace_s: float = 2.0) -> bool:
    """Terminate a recorded process group and confirm that it is gone."""
    if os.name != "posix" or pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if _wait_until_group_gone(pgid, max(0.0, grace_s)):
        return True
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return _wait_until_group_gone(pgid, max(1.0, grace_s))


def _wait_until_group_gone(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    absent_checks = 0
    while time.monotonic() < deadline:
        if not process_group_exists(pgid):
            absent_checks += 1
            if absent_checks >= 3:
                return True
        else:
            absent_checks = 0
        time.sleep(0.05)
    return not process_group_exists(pgid)


def cleanup_process_groups(directory: Path) -> bool:
    """Stop every recorded group; malformed records fail closed."""
    directory = Path(directory)
    if not directory.exists():
        return True
    try:
        markers = list(directory.iterdir())
    except OSError:
        return False
    ok = True
    for marker in markers:
        try:
            pgid = int(marker.name)
            recorded = int(marker.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            ok = False
            continue
        if pgid != recorded or pgid <= 1 or not stop_process_group(pgid):
            ok = False
            continue
        marker.unlink(missing_ok=True)
    if ok:
        try:
            directory.rmdir()
        except OSError:
            return False
    return ok


def process_groups_clean(directory: Path) -> bool:
    """Whether all registered groups are confirmed absent."""
    try:
        return not Path(directory).exists() or not any(Path(directory).iterdir())
    except OSError:
        return False


__all__ = [
    "cleanup_process_groups",
    "process_group_exists",
    "process_group_scope_active",
    "process_group_scope",
    "process_groups_clean",
    "register_process_group",
    "stop_process_group",
    "unregister_process_group",
]
