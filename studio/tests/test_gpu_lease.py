"""Lease global de GPU entre procesos (plan Ultra, W03 / acceptance A04).

Ensayo en CPU con procesos reales: no toca CUDA. Los picos reales de VRAM se
validan en A29 (campaña GPU). Verifica exclusión, admisión, fencing, huérfanos,
cancelación/unload y checkpoint/resume.
"""

from __future__ import annotations

import json
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
    LeaseError,
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


def test_corrupt_fencing_sequence_fails_closed(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    lease._token_file.write_text("not-a-token", encoding="utf-8")

    with pytest.raises(LeaseError, match="secuencia de fencing ilegible"):
        lease.acquire(owner="x", engine="e", vram_mb=1000, ram_mb=1000)

    assert lease.current() is None
    assert not lease._lock_file.exists()


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


def test_dead_owner_without_cleanup_verification_keeps_gpu_quarantined(tmp_path: Path) -> None:
    """A dead API process is insufficient evidence that its engine released the GPU."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    owner = _lease(tmp_path, pid=dead.pid)
    grant = owner.acquire(owner="zombie", engine="comfyui", vram_mb=1000, ram_mb=1000)

    contender = _lease(tmp_path, pid=os.getpid())
    with pytest.raises(LeaseUnavailable, match="cleanup del engine no verificado"):
        contender.acquire(owner="nuevo", engine="blender", vram_mb=1000, ram_mb=1000)
    assert contender.current() == grant


def test_dead_owner_recovers_only_after_engine_cleanup_verifies(tmp_path: Path) -> None:
    """A verifier may release an orphan only after confirming engine cleanup."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    owner = _lease(tmp_path, pid=dead.pid)
    grant = owner.acquire(owner="zombie", engine="comfyui", vram_mb=1000, ram_mb=1000)

    seen = []
    contender = _lease(
        tmp_path,
        pid=os.getpid(),
        recover_orphan=lambda orphan: seen.append(orphan) or orphan.token == grant.token,
    )
    recovered = contender.acquire(owner="nuevo", engine="blender", vram_mb=1000, ram_mb=1000)
    assert seen == [grant]
    assert recovered is not None and recovered.owner == "nuevo"
    assert recovered.token > grant.token


def test_failed_orphan_cleanup_keeps_gpu_quarantined(tmp_path: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    owner = _lease(tmp_path, pid=dead.pid)
    grant = owner.acquire(owner="zombie", engine="comfyui", vram_mb=1000, ram_mb=1000)

    contender = _lease(tmp_path, pid=os.getpid(), recover_orphan=lambda _grant: False)
    with pytest.raises(LeaseUnavailable):
        contender.acquire(owner="nuevo", engine="blender", vram_mb=1000, ram_mb=1000)
    assert contender.current() == grant


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


def test_failed_unload_reports_cleanup_failed_and_quarantines_lock(tmp_path: Path) -> None:
    """Un unload no probado bloquea nuevas admisiones hasta limpiar (A04)."""
    lease = _lease(tmp_path)
    grant = lease.acquire(owner="comfyui", engine="e", vram_mb=1000, ram_mb=1000)

    def _boom() -> bool:
        raise RuntimeError("free() colgó")

    out = lease.release(grant, unload=_boom)
    assert out["cleanup_failed"] is True
    assert out["released"] is False
    assert lease.current() == grant
    with pytest.raises(LeaseUnavailable):
        lease.acquire(owner="blender", engine="e", vram_mb=1000, ram_mb=1000)
    # La limpieza puede reintentarse por el owner actual.
    retried = lease.release(grant, unload=lambda: True)
    assert retried["released"] is True and retried["cleanup_failed"] is False
    assert lease.current() is None


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


@pytest.mark.parametrize("recover_dead_owner", [False, True])
def test_two_real_processes_contend_only_one_wins(
    tmp_path: Path, recover_dead_owner: bool
) -> None:
    """Contenders reales, también al recuperar un owner muerto, tienen un ganador."""
    lock_dir = tmp_path / ".gpu-lease"
    if recover_dead_owner:
        lock_dir.mkdir(parents=True)
        (lock_dir / "heavy.lock").write_text(
            json.dumps(
                {
                    "owner": "dead-owner",
                    "token": 1,
                    "pid": 0,
                    "engine": "wan_t2v",
                    "vram_mb": 1000,
                    "ram_mb": 1000,
                    "acquired_at": 0,
                }
            ),
            encoding="utf-8",
        )
    script = (
        "import sys, time\n"
        "from comfy_studio.orchestration.gpu_lease import (\n"
        "    GpuLease, AdmissionBudget, LeaseUnavailable)\n"
        "lease = GpuLease(lock_dir=sys.argv[1], budget=AdmissionBudget(98304, 262144), "
        "recover_orphan=lambda grant: True)\n"
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
    # Varios contendientes a la vez exponen la carrera de forma fiable (en CI
    # Linux, dos no bastaban para que el timing la revelara siempre).
    n = 4
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(lock_dir), f"owner{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for i in range(n)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        # Ningún proceso debe reventar con una excepción (p. ej. la vieja carrera
        # de token.seq que lanzaba FileNotFoundError).
        assert p.returncode == 0, f"un contendiente reventó: rc={p.returncode} err={err[-300:]}"
        results.append(out.strip().splitlines()[-1] if out.strip() else "")
    assert results.count("ACQUIRED") == 1, f"exactamente uno debe ganar: {results}"
    assert results.count("BLOCKED") == n - 1, f"el resto debe bloquearse: {results}"


def test_lock_dir_for_uses_comfy_data_dir() -> None:
    d = lock_dir_for({"COMFY_DATA_DIR": "/data/x"})
    assert str(d).replace("\\", "/").endswith("/data/x/.gpu-lease")
