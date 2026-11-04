"""Tests de la política de retención de caché de pesos.

La propiedad que más importa: **nunca** se toca media del usuario, estado de
proyecto ni salidas. No es una lista de exclusiones que mantener al día: el
planificador solo considera rutas declaradas en los manifiestos, así que todo
lo demás queda fuera por construcción.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from comfy_studio.cache_policy import (
    RetentionPolicy,
    apply_plan,
    load_usage,
    record_use,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "models").mkdir(parents=True)
    monkeypatch.setenv("COMFY_DATA_DIR", str(root))
    return root


def _write_manifest(tmp_path: Path, entries: list[dict]) -> None:
    """Escribe un manifiesto de mentira junto al repo (el planificador lee del cwd)."""
    path = tmp_path / "models-test.json"
    path.write_text(
        json.dumps({"version": 1, "profile": "core", "meta": {}, "models": entries}),
        encoding="utf-8",
    )
    return path


def _seed(data_dir: Path, dest: str, size: int = 1024) -> Path:
    path = data_dir / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _entry(filename: str, dest: str, size: int, asset_class: str) -> dict:
    return {
        "filename": filename,
        "dest_path": dest,
        "bytes": size,
        "class": asset_class,
    }


def _plan_with(monkeypatch, tmp_path, entries, **kwargs):
    """Planifica usando un manifiesto de mentira en lugar de los del repo."""
    from comfy_studio import cache_policy

    path = _write_manifest(tmp_path, entries)
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", (str(path),))
    return cache_policy.plan_retention(**kwargs)


# --------------------------------------------------------------------------
# Uso real
# --------------------------------------------------------------------------


def test_usage_ledger_records_and_survives_reload(data_dir: Path) -> None:
    record_use(["a.bin"])
    record_use(["b.bin"])
    usage = load_usage()
    assert set(usage) == {"a.bin", "b.bin"}
    assert usage["a.bin"] <= usage["b.bin"]
    # Y persiste en disco.
    ledger = data_dir / ".model-usage.json"
    assert ledger.is_file()


def test_record_use_is_silent_on_failure(data_dir: Path, monkeypatch) -> None:
    """El ledger nunca debe romper un render."""
    monkeypatch.setattr(
        "comfy_studio.cache_policy.usage_ledger_path",
        lambda env=None: Path("/proc/nonexistent/dir/x.json"),
    )
    record_use(["a.bin"])  # no lanza


# --------------------------------------------------------------------------
# Conservar vs expulsar
# --------------------------------------------------------------------------


def test_pinned_classes_are_never_evicted(data_dir: Path, monkeypatch) -> None:
    _seed(data_dir, "models/checkpoints/bootstrap.safetensors", 500)
    _seed(data_dir, "models/checkpoints/optional.safetensors", 500)
    _seed(data_dir, "models/checkpoints/task.safetensors", 500)

    entries = [
        _entry(
            "bootstrap.safetensors", "models/checkpoints/bootstrap.safetensors", 500, "bootstrap"
        ),
        _entry("optional.safetensors", "models/checkpoints/optional.safetensors", 500, "optional"),
        _entry("task.safetensors", "models/checkpoints/task.safetensors", 500, "task"),
    ]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=600)
    )
    evicted = {e.filename for e in plan.evictions}
    assert "bootstrap.safetensors" not in evicted
    assert "optional.safetensors" not in evicted
    assert "task.safetensors" in evicted


def test_no_budget_means_nothing_is_evicted(data_dir: Path, monkeypatch) -> None:
    _seed(data_dir, "models/checkpoints/task.safetensors")
    entries = [
        _entry("task.safetensors", "models/checkpoints/task.safetensors", 1024, "task"),
    ]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=None)
    )
    assert plan.evictions == []


def test_eviction_order_is_least_recently_used(data_dir: Path, monkeypatch) -> None:
    """Los no usados salen primero; entre usados, el más antiguo."""
    for name in ("old.bin", "new.bin", "never.bin"):
        _seed(data_dir, f"models/checkpoints/{name}")

    record_use(["old.bin"])
    time.sleep(0.01)
    record_use(["new.bin"])

    entries = [
        _entry(name, f"models/checkpoints/{name}", 100, "task")
        for name in ("old.bin", "new.bin", "never.bin")
    ]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=150)
    )
    order = [e.filename for e in plan.evictions]
    assert order[0] == "never.bin", "un activo nunca usado sale primero"
    assert order.index("old.bin") < order.index("new.bin")


def test_protected_paths_are_reported_and_never_evicted(data_dir: Path, monkeypatch) -> None:
    """Aunque un manifiesto declarara por error una ruta de usuario, no se toca."""
    for dest in ("output/render.mp4", "input/source.mp4", "user/settings.json", "studio/studio.db"):
        _seed(data_dir, dest)

    entries = [
        _entry(Path(dest).name, dest, 10, "task")
        for dest in (
            "output/render.mp4",
            "input/source.mp4",
            "user/settings.json",
            "studio/studio.db",
        )
    ]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=1)
    )
    assert len(plan.protected_seen) == 4
    assert plan.evictions == []
    for dest in ("output/render.mp4", "input/source.mp4", "user/settings.json", "studio/studio.db"):
        assert (data_dir / dest).is_file(), f"{dest} debe conservarse"


def test_only_manifest_paths_are_considered(data_dir: Path, monkeypatch) -> None:
    """Un peso que no está en ningún manifiesto no se expulsa jamás."""
    _seed(data_dir, "models/checkpoints/undeclared.safetensors", 4096)
    entries = [
        _entry("declared.safetensors", "models/checkpoints/declared.safetensors", 10, "task")
    ]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=1)
    )
    assert all(e.filename != "undeclared.safetensors" for e in plan.evictions)
    assert (data_dir / "models/checkpoints/undeclared.safetensors").is_file()


# --------------------------------------------------------------------------
# Dry-run y aplicación
# --------------------------------------------------------------------------


def test_dry_run_deletes_nothing(data_dir: Path, monkeypatch) -> None:
    target = _seed(data_dir, "models/checkpoints/task.bin")
    entries = [_entry("task.bin", "models/checkpoints/task.bin", 1024, "task")]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=1)
    )
    assert plan.evictions, "debe haber algo que expulsar"
    result = apply_plan(plan, dry_run=True)
    assert result["applied"] is False
    assert target.is_file(), "el dry-run no debe borrar"


def test_apply_removes_only_the_planned_assets(data_dir: Path, monkeypatch) -> None:
    doomed = _seed(data_dir, "models/checkpoints/task.bin")
    keep = _seed(data_dir, "models/checkpoints/bootstrap.bin")
    user = _seed(data_dir, "output/render.mp4")

    entries = [
        _entry("task.bin", "models/checkpoints/task.bin", 1024, "task"),
        _entry("bootstrap.bin", "models/checkpoints/bootstrap.bin", 1024, "bootstrap"),
        _entry("render.mp4", "output/render.mp4", 1024, "task"),
    ]
    plan = _plan_with(
        monkeypatch, data_dir.parent, entries, policy=RetentionPolicy(budget_bytes=1024)
    )
    result = apply_plan(plan, dry_run=False)
    assert result["applied"] is True
    assert not doomed.exists()
    assert keep.is_file()
    assert user.is_file()


def test_stale_staging_is_cleaned_but_fresh_is_kept(data_dir: Path, monkeypatch) -> None:
    staging = data_dir / "models" / ".hf_stage_capability"
    staging.mkdir(parents=True)
    partial = staging / "model.safetensors.incomplete"
    partial.write_bytes(b"parcial")

    import os as _os

    old = time.time() - 30 * 24 * 3600
    _os.utime(staging, (old, old))
    _os.utime(partial, (old, old))

    from comfy_studio import cache_policy

    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ())
    plan = cache_policy.plan_retention(policy=RetentionPolicy(staging_max_age_s=7 * 24 * 3600))
    assert any(".hf_stage_capability" in s["path"] for s in plan.staging_cleanups)

    apply_plan(plan, dry_run=False)
    assert not staging.exists(), "el staging inactivo se limpia"

    # Uno reciente se conserva: es una descarga reanudable, no basura.
    fresh = data_dir / "models" / ".hf_stage_phase3"
    fresh.mkdir(parents=True)
    (fresh / "x.incomplete").write_bytes(b"parcial")
    plan2 = cache_policy.plan_retention(policy=RetentionPolicy(staging_max_age_s=7 * 24 * 3600))
    assert not any(".hf_stage_phase3" in s["path"] for s in plan2.staging_cleanups)
    assert fresh.is_dir()


# --------------------------------------------------------------------------
# Recetas de ejecución medidas (política, punto 5)
# --------------------------------------------------------------------------


def test_render_measurements_are_stored_and_aggregated(tmp_path: Path) -> None:
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "studio.db")
    assert store.get_schema_version() == 3

    store.record_render_measurement(
        template_id="wan_t2v", duration_s=38.2, vram_peak_mb=12400, gpu_name="RTX 5090", cuda="12.8"
    )
    store.record_render_measurement(
        template_id="wan_t2v", duration_s=41.7, vram_peak_mb=13100, gpu_name="RTX 5090", cuda="12.8"
    )
    rows = store.list_render_measurements(template_id="wan_t2v")
    assert len(rows) == 2

    recipe = store.recipe_for_template("wan_t2v")
    assert recipe is not None and recipe["measured"] is True
    assert recipe["samples"] == 2
    assert recipe["vram_peak_mb_max"] == 13100
    assert recipe["duration_s_avg"] == 39.95
    assert recipe["gpu_name"] == "RTX 5090"


def test_recipe_is_none_without_measurements(tmp_path: Path) -> None:
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "studio.db")
    assert store.recipe_for_template("never_rendered") is None
    assert store.list_render_measurements() == []


def test_recording_a_measurement_never_raises(tmp_path: Path, monkeypatch) -> None:
    """Una medición ausente no debe tumbar un render."""
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "studio.db")

    def _boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("db llena")

    monkeypatch.setattr(store, "_connect", _boom)
    store.record_render_measurement(template_id="x", duration_s=1.0)  # no lanza


def test_capability_resolution_reuses_the_measured_recipe(tmp_path: Path, monkeypatch) -> None:
    """La resolución de una capacidad reutiliza la receta medida si existe."""
    from comfy_studio.capabilities import resolve
    from comfy_studio.projects import ProjectStore

    data = tmp_path / "data"
    (data / "models").mkdir(parents=True)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "studio"))

    store = ProjectStore(tmp_path / "studio")
    store.record_render_measurement(
        template_id="face_fix", duration_s=12.5, vram_peak_mb=4200, gpu_name="RTX 5090"
    )
    payload = resolve("video.face_restore").to_public_dict()
    assert "recipe" in payload
    recipe = payload["recipe"]
    assert recipe is not None and recipe["measured"] is True
    assert recipe["vram_peak_mb_max"] == 4200


def test_resolution_without_measurements_still_works(tmp_path: Path, monkeypatch) -> None:
    from comfy_studio.capabilities import resolve

    data = tmp_path / "data"
    (data / "models").mkdir(parents=True)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "empty-studio"))
    payload = resolve("video.face_restore").to_public_dict()
    assert payload["recipe"] is None
    assert payload["selected_template"] == "face_fix"


@pytest.mark.anyio
async def test_system_stats_sampler_measures_the_real_peak() -> None:
    """El pico se mide durante el render, no se declara."""
    import asyncio

    from comfy_studio.comfyui_client import sample_system_stats_peak

    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def system_stats(self) -> dict:
            self.calls += 1
            # VRAM creciente: el pico debe ser el último valor.
            used = 1024**3 * (10 + self.calls)
            return {
                "devices": [{"name": "RTX 5090", "vram_total": 32 * 1024**3, "vram_used": used}],
                "system": {"ram_used": 1024**2 * 100},
                "cuda_version": "12.8",
            }

    client = _FakeClient()
    stop = asyncio.Event()

    async def _stopper() -> None:
        await asyncio.sleep(0.08)
        stop.set()

    task = asyncio.create_task(_stopper())
    peak = await sample_system_stats_peak(client, stop=stop, interval=0.01)
    await task

    assert peak["samples"] >= 2
    assert peak["vram_peak_mb"] == 1024 * (10 + client.calls)
    assert peak["gpu_name"] == "RTX 5090"
    assert peak["cuda"] == "12.8"
    assert peak["ram_peak_mb"] == 100


@pytest.mark.anyio
async def test_sampler_is_silent_when_comfyui_does_not_answer() -> None:
    """Una medición ausente nunca debe tumbar el render."""
    import asyncio

    from comfy_studio.comfyui_client import sample_system_stats_peak

    class _Dead:
        async def system_stats(self) -> dict:
            raise RuntimeError("ComfyUI caído")

    peak = await sample_system_stats_peak(_Dead(), stop=asyncio.Event(), interval=0.01)
    assert peak["vram_peak_mb"] is None
    assert peak["samples"] == 0
