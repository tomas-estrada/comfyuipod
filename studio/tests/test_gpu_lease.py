"""Lease global de GPU entre procesos (plan Ultra, W03 / acceptance A04).

Ensayo en CPU con procesos reales: no toca CUDA. Los picos reales de VRAM se
validan en A29 (campaña GPU). Verifica exclusión, admisión, fencing, huérfanos,
cancelación/unload y checkpoint/resume.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from comfy_studio.orchestration.gpu_lease import (
    AdmissionBudget,
    FencingError,
    GpuLease,
    LeaseAdmissionError,
    LeaseUnavailable,
    lock_dir_for,
)

BUDGET = AdmissionBudget(vram_mb=96 * 1024, ram_mb=256 * 1024)


def _lease(tmp_path: Path, **kw) -> GpuLease:
    return GpuLease(lock_dir=tmp_path / ".gpu-lease", budget=BUDGET, **kw)


# --------------------------------------------------------------------------
# Exclusión y admisión
# --------------------------------------------------------------------------


def test_only_one_heavy_lease_at_a_time(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    grant = lease.acquire(owner="comfyui", engine="wan_t2v", vram_mb=40000, ram_mb=16000)
    assert grant is not None and grant.token >= 1

    # Un segundo intento (mismo proceso vivo como owner) debe fallar: ocupado.
    other = _lease(tmp_path, pid=grant.pid + 1_000_000)  # pid que fingiremos vivo
    with pytest.raises(LeaseUnavailable):
        other.acquire(owner="blender", engine="blender", vram_mb=20000, ram_mb=8000)

    out = lease.release(grant)
    assert out["released"] is True and out["cleanup_failed"] is False
    # Liberado: ahora otro sí puede.
    g2 = lease.acquire(owner="blender", engine="blender", vram_mb=20000, ram_mb=8000)
    assert g2 is not None and g2.token > grant.token  # token monótono


def test_cpu_stage_does_not_take_the_gpu_lock(tmp_path: Path) -> None:
    """Un stage CPU corre sin tomar el lock ni cargar CUDA (A04)."""
    lease = _lease(tmp_path)
    heavy = lease.acquire(owner="comfyui", engine="wan_t2v", vram_mb=40000, ram_mb=16000)
    assert heavy is not None
    # El stage CPU se admite por RAM y NO toca el lock de GPU: devuelve None.
    cpu = lease.acquire(owner="ffmpeg", engine="encode", vram_mb=0, ram_mb=4000, heavy=False)
    assert cpu is None
    # El heavy sigue siendo el único dueño del lock.
    assert lease.current().owner == "comfyui"


def test_admission_rejects_over_budget(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with pytest.raises(LeaseAdmissionError):
        lease.acquire(owner="x", engine="huge", vram_mb=200000, ram_mb=16000)
    with pytest.raises(LeaseAdmissionError):
        lease.acquire(owner="x", engine="huge", vram_mb=1000, ram_mb=999999)


# --------------------------------------------------------------------------
# Fencing y huérfanos
# --------------------------------------------------------------------------


def test_fencing_blocks_a_stale_owner_from_releasing_new_lease(tmp_path: Path) -> None:
    """Un owner antiguo no puede liberar el lease de uno nuevo (A04)."""
    lease = _lease(tmp_path)
    old = lease.acquire(owner="a", engine="e", vram_mb=1000, ram_mb=1000)
    lease.release(old)
    new = lease.acquire(owner="b", engine="e", vram_mb=1000, ram_mb=1000)
    # El owner viejo intenta liberar con su token caducado -> fencing.
    with pytest.raises(FencingError):
        lease.release(old)
    # El nuevo sí libera.
    assert lease.release(new)["released"] is True


def test_live_owner_lease_is_not_recoverable(tmp_path: Path) -> None:
    """Un owner VIVO conserva la GPU aunque otro quiera recuperarla (A04)."""
    me = os.getpid()
    lease = _lease(tmp_path, pid=me)
    grant = lease.acquire(owner="comfyui", engine="e", vram_mb=1000, ram_mb=1000)
    assert grant.pid == me and me == os.getpid()  # owner vivo
    # force_break no debe romper un lock cuyo owner (este proceso) sigue vivo.
    assert lease.force_break() is False
    assert lease.current() is not None


def test_dead_owner_lease_is_recovered(tmp_path: Path) -> None:
    """Si el owner murió, su lease se recupera (no se deja la GPU bloqueada)."""
    # Un pid casi seguro muerto: lanzamos y esperamos un proceso trivial.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lease = _lease(tmp_path, pid=dead.pid)
    # Sembramos un lock cuyo owner (dead.pid) ya no existe.
    lease.acquire(owner="zombie", engine="e", vram_mb=1000, ram_mb=1000)
    # Otro proceso (vivo) debe poder recuperarlo.
    live = _lease(tmp_path, pid=os.getpid())
    recovered = live.acquire(owner="nuevo", engine="e", vram_mb=1000, ram_mb=1000)
    assert recovered is not None and recovered.owner == "nuevo"


# --------------------------------------------------------------------------
# Unload comprobado / cleanup
# --------------------------------------------------------------------------


def test_release_runs_and_verifies_unload(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    grant = lease.acquire(owner="comfyui", engine="e", vram_mb=1000, ram_mb=1000)
    calls = {"n": 0}

    def _unload() -> bool:
        calls["n"] += 1
        return True

    out = lease.release(grant, unload=_unload)
    assert calls["n"] == 1
    assert out["released"] is True and out["cleanup_failed"] is False


def test_failed_unload_reports_cleanup_failed_but_frees_lock(tmp_path: Path) -> None:
    """Un unload roto no deja la GPU bloqueada; se reporta cleanup_failed."""
    lease = _lease(tmp_path)
    grant = lease.acquire(owner="comfyui", engine="e", vram_mb=1000, ram_mb=1000)

    def _boom() -> bool:
        raise RuntimeError("free() colgó")

    out = lease.release(grant, unload=_boom)
    assert out["cleanup_failed"] is True
    assert out["released"] is True
    assert lease.current() is None  # el lock se liberó pese al fallo


# --------------------------------------------------------------------------
# Checkpoint/resume no duplica ni reinicia el contador
# --------------------------------------------------------------------------


def test_checkpoint_resume_does_not_duplicate_stage(tmp_path: Path) -> None:
    """Reabrir el árbitro (resume) ve el mismo lease, no crea uno nuevo."""
    lease = _lease(tmp_path, pid=os.getpid())
    grant = lease.acquire(owner="comfyui", engine="e", vram_mb=1000, ram_mb=1000)

    # «Resume»: otro objeto GpuLease sobre el mismo lock_dir y mismo pid.
    resumed = _lease(tmp_path, pid=os.getpid())
    seen = resumed.current()
    assert seen is not None
    assert seen.token == grant.token  # el contador no se reinicia
    assert seen.owner == grant.owner
    # Y adquirir de nuevo con el owner vivo falla: no se duplica el stage.
    with pytest.raises(LeaseUnavailable):
        resumed.acquire(owner="comfyui", engine="e", vram_mb=1000, ram_mb=1000)


# --------------------------------------------------------------------------
# Ensayo multiproceso real (A04: «no mutex de thread ni cola solo de ComfyUI»)
# --------------------------------------------------------------------------


def test_two_real_processes_contend_only_one_wins(tmp_path: Path) -> None:
    """Dos procesos reales intentan el heavy lease; exactamente uno lo gana."""
    lock_dir = tmp_path / ".gpu-lease"
    script = (
        "import sys, time\n"
        "from comfy_studio.orchestration.gpu_lease import (\n"
        "    GpuLease, AdmissionBudget, LeaseUnavailable)\n"
        "lease = GpuLease(lock_dir=sys.argv[1], budget=AdmissionBudget(98304, 262144))\n"
        "try:\n"
        "    g = lease.acquire(owner=sys.argv[2], engine='e', vram_mb=1000, ram_mb=1000)\n"
        "    print('ACQUIRED' if g else 'NONE')\n"
        "    time.sleep(1.5)\n"  # mantener el lease para que el otro choque
        "except LeaseUnavailable:\n"
        "    print('BLOCKED')\n"
    )
    env = dict(os.environ)
    repo_studio = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo_studio + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(lock_dir), f"owner{i}"],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        for i in range(2)
    ]
    outs = [p.communicate(timeout=30)[0].strip() for p in procs]
    results = [o.splitlines()[-1] if o else "" for o in outs]
    assert results.count("ACQUIRED") == 1, f"exactamente uno debe ganar: {results}"
    assert results.count("BLOCKED") == 1, f"el otro debe quedar bloqueado: {results}"


def test_lock_dir_for_uses_comfy_data_dir() -> None:
    d = lock_dir_for({"COMFY_DATA_DIR": "/data/x"})
    assert str(d).replace("\\", "/").endswith("/data/x/.gpu-lease")
