from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from comfy_studio.engines.process import ProcessCancelled, run_cancellable


def test_cancellation_terminates_running_process_promptly():
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(ProcessCancelled):
            run_cancellable(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                cancel_event=cancel,
                capture_output=True,
                text=True,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 3


def test_without_cancel_signal_keeps_subprocess_run_result():
    result = run_cancellable(
        [sys.executable, "-c", "print('ok')"],
        cancel_event=None,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.stdout.strip() == "ok"
