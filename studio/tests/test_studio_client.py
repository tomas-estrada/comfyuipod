"""StudioClient tipado para la UI agéntica (plan Ultra, W21 / A23).

Acciones idempotentes (reconnect no duplica); error no deja spinner infinito ni
éxito falso; cancelación/reanudación honestas. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.studio_client import (
    ActionState,
    ClientError,
    StudioClient,
)


def test_submit_creates_running_action() -> None:
    c = StudioClient()
    a = c.submit("k1", "create_project")
    assert a.state is ActionState.RUNNING
    assert a.kind == "create_project"


def test_resubmit_same_key_does_not_duplicate() -> None:
    """A23: reconnect no duplica acciones."""
    c = StudioClient()
    a1 = c.submit("k1", "upload")
    a2 = c.submit("k1", "upload")  # mismo idempotency key
    assert a1 is a2  # la misma acción, no una segunda


def test_complete_sets_done_and_result() -> None:
    c = StudioClient()
    c.submit("k1", "send_brief")
    c.complete("k1", {"brief_id": "b1"})
    st = c.ui_status("k1")
    assert st["success"] is True
    assert st["show_spinner"] is False


def test_error_stops_spinner_and_has_no_false_success() -> None:
    """A23: un evento de error no deja spinner infinito ni éxito falso."""
    c = StudioClient()
    c.submit("k1", "render")
    c.fail("k1", "ComfyUI 503")
    st = c.ui_status("k1")
    assert st["state"] == "error"
    assert st["show_spinner"] is False   # spinner apagado
    assert st["success"] is False        # no éxito falso
    assert st["error"] == "ComfyUI 503"


def test_running_action_shows_spinner() -> None:
    c = StudioClient()
    c.submit("k1", "render")
    assert c.ui_status("k1")["show_spinner"] is True


def test_cancel_marks_cancelled_and_late_complete_ignored() -> None:
    c = StudioClient()
    c.submit("k1", "render")
    c.cancel("k1")
    # Una respuesta tardía NO revive una acción cancelada.
    c.complete("k1", {"output": "x"})
    assert c.resume("k1").state is ActionState.CANCELLED


def test_cancel_after_done_does_not_revert_result() -> None:
    c = StudioClient()
    c.submit("k1", "render")
    c.complete("k1", {"output": "x"})
    c.cancel("k1")  # ya terminal
    assert c.resume("k1").state is ActionState.DONE


def test_resume_after_reconnect_returns_same_action_not_reexecute() -> None:
    """A23: reanudar tras reconnect no re-ejecuta la acción."""
    c = StudioClient()
    c.submit("k1", "render")
    c.complete("k1", {"output": "reel.mp4"})
    resumed = c.resume("k1")
    assert resumed.state is ActionState.DONE
    assert resumed.result == {"output": "reel.mp4"}


def test_unknown_action_rejected() -> None:
    c = StudioClient()
    with pytest.raises(ClientError, match="desconocida"):
        c.resume("nope")
