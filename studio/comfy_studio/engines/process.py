"""Subprocess execution helpers shared by Studio engines."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from threading import Event
from typing import Any

from comfy_studio.engines.process_registry import (
    process_group_exists,
    process_group_scope_active,
    register_process_group,
    unregister_process_group,
)


class ProcessCancelled(Exception):
    """Raised after a cancelled engine process tree has been stopped."""


def run_cancellable(
    args: list[str],
    *,
    cancel_event: Event | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run a child process, terminating its process group on cancellation.

    `communicate(timeout=...)` drains both pipes while polling the cancellation
    signal, avoiding pipe deadlocks and keeping timeout semantics compatible
    with `subprocess.run`.
    """
    # Fast path for non-leased callers. A leased run needs a session even without
    # cancellation so an API crash can later identify and stop its child group.
    if cancel_event is None and not process_group_scope_active():
        return subprocess.run(args, timeout=timeout, **kwargs)

    popen_kwargs = dict(kwargs)
    check = popen_kwargs.pop("check", False)
    capture_output = popen_kwargs.pop("capture_output", False)
    if capture_output:
        if "stdout" in popen_kwargs or "stderr" in popen_kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            popen_kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("engine process cancelled before start")
    proc = subprocess.Popen(args, **popen_kwargs)
    marker = None
    try:
        marker = register_process_group(proc.pid) if os.name == "posix" else None
    except Exception:
        _terminate_tree(proc)
        raise
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_tree(proc)
                proc.communicate()
                raise ProcessCancelled("engine process cancelled")
            wait_s = 0.1
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_tree(proc)
                    stdout, stderr = proc.communicate()
                    raise subprocess.TimeoutExpired(
                        args, timeout, output=stdout, stderr=stderr
                    )
                wait_s = min(wait_s, remaining)
            try:
                stdout, stderr = proc.communicate(timeout=wait_s)
                if os.name == "posix" and process_group_exists(proc.pid):
                    _terminate_tree(proc)
                    if process_group_exists(proc.pid):
                        raise RuntimeError("engine child process group cleanup failed")
                completed = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
                if check and completed.returncode:
                    raise subprocess.CalledProcessError(
                        completed.returncode,
                        args,
                        output=completed.stdout,
                        stderr=completed.stderr,
                    )
                return completed
            except subprocess.TimeoutExpired:
                continue
    finally:
        if marker is not None and not process_group_exists(proc.pid):
            unregister_process_group(marker)


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Ask the process group to stop, then force it down after a short grace."""
    try:
        if os.name == "nt":
            taskkill = shutil.which("taskkill")
            if taskkill:
                subprocess.run(
                    [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            else:
                proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        if os.name != "nt":
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
