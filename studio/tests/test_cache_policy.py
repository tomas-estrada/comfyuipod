"""Tests de la política de retención de caché de pesos.

La propiedad que más importa: **nunca** se toca media del usuario, estado de
proyecto ni salidas. No es una lista de exclusiones que mantener al día: el
planificador solo considera rutas declaradas en los manifiestos, así que todo
lo demás queda fuera por construcción.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
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
    """Escribe un manifiesto de mentira; `_plan_with` apunta a él por ruta absoluta."""
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


def _env_without_manifest_overrides(**extra: str) -> dict[str, str]:
    """Entorno del proceso CLI sin los overrides de manifiesto del repo."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("STUDIO_MANIFEST", "STUDIO_CORE_MANIFEST")
    }
    env.update(extra)
    return env


def _env_with_no_manifests(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Entorno donde los dos catálogos apuntan a rutas que no existen.

    Así el proceso CLI real se queda sin manifiestos que leer, que es el estado
    que R31 quiere hacer distinguible en `--json`.
    """
    return _env_without_manifest_overrides(
        COMFY_DATA_DIR=str(tmp_path / "data"),
        STUDIO_CORE_MANIFEST=str(tmp_path / "absent-core.json"),
        STUDIO_MANIFEST=str(tmp_path / "absent-studio.json"),
        **extra,
    )


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
# Resolución de manifiestos (independiente del CWD)
# --------------------------------------------------------------------------


def _fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Réplica del layout del paquete, para fijar desde dónde se resuelve.

    `parents[2]` del módulo deja de ser la raíz del repo cuando el paquete se
    instala con `pip install /opt/studio`, así que se falsifica su `__file__`
    igual que hace `test_capability_resolver.py`.
    """
    from comfy_studio import cache_policy

    # La lista real prioriza el árbol de fuentes y termina en la ruta horneada.
    assert cache_policy._RELATIVE_MANIFEST_BASES[0] == "config"
    assert "/etc/comfyui" in cache_policy._RELATIVE_MANIFEST_BASES

    root = tmp_path / "repo"
    pkg = root / "studio" / "comfy_studio"
    pkg.mkdir(parents=True)
    (pkg / "cache_policy.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(cache_policy, "__file__", str(pkg / "cache_policy.py"))
    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)
    monkeypatch.delenv("STUDIO_CORE_MANIFEST", raising=False)
    # Se acota la lista al repo simulado: la ruta horneada de la imagen no debe
    # colar un manifiesto real en el resultado.
    monkeypatch.setattr(cache_policy, "_RELATIVE_MANIFEST_BASES", ("config", ""))
    return root


def _write_manifest_at(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "profile": "core", "meta": {}, "models": entries}),
        encoding="utf-8",
    )
    return path


def test_manifests_resolve_against_the_package_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desde otro directorio el plan sigue viendo los activos declarados.

    `scripts/prune_model_cache.py --json` desde `/tmp` leía `models-core.json`
    relativo al CWD: no lo encontraba y devolvía `keep_count: 0` con la lista de
    expulsiones vacía, que se lee como «el volumen está bien». La política
    quedaba inoperante sin decir nada.
    """
    from comfy_studio import cache_policy

    root = _fake_package(tmp_path, monkeypatch)
    _write_manifest_at(
        root / "config" / "models-core.json",
        [_entry("task.bin", "models/checkpoints/task.bin", 2048, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json",))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/task.bin", 2048)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert not (elsewhere / "models-core.json").exists(), "el CWD no debe tener manifiesto"

    plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 1, "el activo declarado debe verse desde cualquier CWD"
    assert plan.keep_bytes == 2048


def test_studio_manifest_env_var_wins_over_the_known_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`STUDIO_MANIFEST` manda en su catálogo, igual que `model_assets`."""
    from comfy_studio import cache_policy

    root = _fake_package(tmp_path, monkeypatch)
    repo_manifest = _write_manifest_at(
        root / "config" / "models-studio.json",
        [_entry("repo.bin", "models/checkpoints/repo.bin", 4096, "task")],
    )
    explicit = _write_manifest_at(
        tmp_path / "explicit.json",
        [_entry("explicit.bin", "models/checkpoints/explicit.bin", 1024, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-studio.json",))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/repo.bin", 4096)
    _seed(data_dir, "models/checkpoints/explicit.bin", 1024)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STUDIO_MANIFEST", str(explicit))
    monkeypatch.chdir(tmp_path)

    assert cache_policy.manifest_paths() == [explicit]
    assert repo_manifest.is_file(), "el manifiesto del repo sigue en su sitio"

    plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 1, "solo se lee el manifiesto explícito"
    assert plan.keep_bytes == 1024


def test_core_manifest_env_var_wins_only_for_the_core_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`STUDIO_CORE_MANIFEST` es el override del core, y solo del core."""
    from comfy_studio import cache_policy

    root = _fake_package(tmp_path, monkeypatch)
    repo_core = _write_manifest_at(
        root / "config" / "models-core.json",
        [_entry("repo-core.bin", "models/checkpoints/repo-core.bin", 4096, "task")],
    )
    repo_studio = _write_manifest_at(
        root / "config" / "models-studio.json",
        [_entry("repo-studio.bin", "models/checkpoints/repo-studio.bin", 2048, "task")],
    )
    explicit_core = _write_manifest_at(
        tmp_path / "explicit-core.json",
        [_entry("explicit-core.bin", "models/checkpoints/explicit-core.bin", 1024, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json", "models-studio.json"))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/repo-core.bin", 4096)
    _seed(data_dir, "models/checkpoints/repo-studio.bin", 2048)
    _seed(data_dir, "models/checkpoints/explicit-core.bin", 1024)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(explicit_core))
    monkeypatch.chdir(tmp_path)

    paths = cache_policy.manifest_paths()
    assert paths == [explicit_core, repo_studio], paths
    assert repo_core.is_file(), "el manifiesto core del repo sigue en su sitio"

    plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 2
    assert plan.keep_bytes == 1024 + 2048


def test_studio_override_does_not_drop_the_core_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R17: apuntar `STUDIO_MANIFEST` no puede dejar el core fuera del plan.

    Con una sola variable compartida, fijar el override documentado de Fase 3
    hacía que `manifest_paths()` devolviera únicamente el manifiesto de estudio:
    el catálogo core desaparecía del planificador, `keep_bytes` se quedaba corto
    en sus ~68 GB y ningún activo de Fases 1/2 era candidato a expulsión. Los
    dos catálogos son disjuntos, así que no hay solape que compense la pérdida.
    """
    from comfy_studio import cache_policy

    root = _fake_package(tmp_path, monkeypatch)
    repo_core = _write_manifest_at(
        root / "config" / "models-core.json",
        [_entry("core.bin", "models/checkpoints/core.bin", 4096, "task")],
    )
    explicit_studio = _write_manifest_at(
        tmp_path / "explicit-studio.json",
        [_entry("studio.bin", "models/checkpoints/studio.bin", 1024, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json", "models-studio.json"))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/core.bin", 4096)
    _seed(data_dir, "models/checkpoints/studio.bin", 1024)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STUDIO_MANIFEST", str(explicit_studio))
    monkeypatch.chdir(tmp_path)

    paths = cache_policy.manifest_paths()
    assert repo_core in paths, "el catálogo core no puede quedar fuera del plan"
    assert explicit_studio in paths

    # Sin presupuesto, el core sigue contando para la retención.
    plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 2, "el core debe seguir contando para la retención"
    assert plan.keep_bytes == 4096 + 1024

    # Y con presupuesto sigue siendo candidato a expulsión.
    tight = cache_policy.plan_retention(policy=RetentionPolicy(budget_bytes=1))
    assert "core.bin" in {e.filename for e in tight.evictions}


def test_missing_manifest_warns_instead_of_reporting_a_healthy_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sin manifiesto la política avisa: un plan vacío no es «todo está bien»."""
    from comfy_studio import cache_policy

    _fake_package(tmp_path, monkeypatch)
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json",))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/task.bin")
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.cache_policy"):
        plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 0
    assert "manifiesto" in caplog.text, "la ausencia de manifiesto debe registrarse"


def test_plan_declares_which_manifests_it_read(tmp_path: Path, monkeypatch) -> None:
    """R31: `manifests` / `manifests_found` distinguen «nada declarado» de «sano».

    El aviso por log no llega al consumidor de `--json` (`scripts/
    prune_model_cache.py` no configura logging), así que el plan tiene que
    cargar la cuenta también en el payload.
    """
    from comfy_studio import cache_policy

    root = _fake_package(tmp_path, monkeypatch)
    _write_manifest_at(
        root / "config" / "models-core.json",
        [_entry("core.bin", "models/checkpoints/core.bin", 2048, "task")],
    )
    _write_manifest_at(
        root / "config" / "models-studio.json",
        [_entry("studio.bin", "models/checkpoints/studio.bin", 1024, "task")],
    )
    monkeypatch.setattr(
        cache_policy, "MANIFEST_NAMES", ("models-core.json", "models-studio.json")
    )

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/core.bin", 2048)
    _seed(data_dir, "models/checkpoints/studio.bin", 1024)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)

    plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 2
    assert len(plan.manifests) == 2
    assert [Path(p).name for p in plan.manifests] == ["models-core.json", "models-studio.json"]
    assert plan.unreadable_manifests == []

    public = plan.to_public_dict()
    assert public["manifests_found"] == 2
    assert len(public["manifests"]) == 2
    assert public["unreadable_manifests"] == []


def test_empty_plan_because_nothing_is_declared_is_not_a_healthy_volume(
    tmp_path: Path, monkeypatch
) -> None:
    """R31: con cero manifiestos el plan lo dice, no lo disimula."""
    from comfy_studio import cache_policy

    _fake_package(tmp_path, monkeypatch)
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json", "models-studio.json"))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/task.bin", 4096)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)

    plan = cache_policy.plan_retention(policy=RetentionPolicy())
    assert plan.keep_count == 0, "sin declaración no hay activos que evaluar"
    assert plan.manifests == []
    public = plan.to_public_dict()
    assert public["manifests_found"] == 0
    assert public["manifests"] == []


def test_unreadable_manifest_is_reported_not_skipped_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R30: un manifiesto localizado pero ilegible se descarta **diciéndolo**.

    El `continue` mudo devolvía `keep_count: 0` con stderr vacío y exit 0, que
    es el «plan vacío confidente» que el docstring promete no emitir. Con la
    resolución contra la raíz del paquete el camino ya es alcanzable.
    """
    from comfy_studio import cache_policy

    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)
    monkeypatch.delenv("STUDIO_CORE_MANIFEST", raising=False)
    broken = tmp_path / "models-core.json"
    broken.write_text('{"models": [{"filename": "roto"', encoding="utf-8")
    good = _write_manifest_at(
        tmp_path / "models-studio.json",
        [_entry("bueno.bin", "models/checkpoints/bueno.bin", 1024, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", (str(broken), str(good)))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/bueno.bin", 1024)
    _seed(data_dir, "models/checkpoints/roto.bin", 4096)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))

    with caplog.at_level(logging.WARNING, logger="comfy_studio.cache_policy"):
        plan = cache_policy.plan_retention(policy=RetentionPolicy())

    # El manifiesto roto se descarta, pero consta: no es un plan vacío sano.
    assert plan.unreadable_manifests == [str(broken)]
    assert str(broken) in caplog.text, "la ruta ilegible debe registrarse con su path"
    assert plan.manifests == [str(good)], "el manifiesto legible sigue contando"

    public = plan.to_public_dict()
    assert public["unreadable_manifests"] == [str(broken)]
    assert public["manifests_found"] == 1
    assert public["manifests"] == [str(good)]

    # El activo del manifiesto legible se sigue viendo: no se pierde todo el plan.
    assert plan.keep_count == 1
    assert plan.keep_bytes == 1024


def test_unreadable_manifest_still_evicts_within_the_readable_one(
    tmp_path: Path, monkeypatch
) -> None:
    """R30: descartar un manifiesto roto no desactiva la política sobre el otro."""
    from comfy_studio import cache_policy

    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)
    monkeypatch.delenv("STUDIO_CORE_MANIFEST", raising=False)
    broken = tmp_path / "models-core.json"
    broken.write_text("no es json", encoding="utf-8")
    good = _write_manifest_at(
        tmp_path / "models-studio.json",
        [_entry("task.bin", "models/checkpoints/task.bin", 2048, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", (str(broken), str(good)))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/task.bin", 2048)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))

    plan = cache_policy.plan_retention(policy=RetentionPolicy(budget_bytes=1))
    assert plan.unreadable_manifests == [str(broken)]
    assert "task.bin" in {e.filename for e in plan.evictions}
    assert plan.keep_count == 0
    assert plan.freed_bytes == 2048


def test_non_dict_manifest_payload_is_reported_not_silently_skipped(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R30: un JSON válido que no es el objeto esperado también se descarta avisando."""
    from comfy_studio import cache_policy

    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)
    monkeypatch.delenv("STUDIO_CORE_MANIFEST", raising=False)
    weird = tmp_path / "models-core.json"
    weird.write_text(json.dumps(["no", "un", "objeto"]), encoding="utf-8")
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", (str(weird),))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/task.bin")
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))

    with caplog.at_level(logging.WARNING, logger="comfy_studio.cache_policy"):
        plan = cache_policy.plan_retention(policy=RetentionPolicy())

    assert plan.keep_count == 0
    assert plan.unreadable_manifests == [str(weird)]
    assert str(weird) in caplog.text
    assert plan.to_public_dict()["unreadable_manifests"] == [str(weird)]


def test_missing_override_is_reported_not_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R42: un override que no existe no puede borrar un catálogo en silencio.

    `_manifest_candidates()` devolvía **solo** la ruta explícita: si
    `STUDIO_CORE_MANIFEST` tenía una errata, el catálogo core no llegaba a
    `manifest_paths()` y el plan se emitía con el otro catálogo presente —
    `manifests_found: 1`, `unreadable_manifests: []` y un `keep_bytes` que se lee
    como un volumen sano corto por el catálogo entero. Ahora el plan lo declara
    en `missing_overrides` y lo avisa por log.
    """
    from comfy_studio import cache_policy

    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)
    root = _fake_package(tmp_path, monkeypatch)
    repo_core = _write_manifest_at(
        root / "config" / "models-core.json",
        [_entry("core.bin", "models/checkpoints/core.bin", 4096, "task")],
    )
    repo_studio = _write_manifest_at(
        root / "config" / "models-studio.json",
        [_entry("studio.bin", "models/checkpoints/studio.bin", 1024, "task")],
    )
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json", "models-studio.json"))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/core.bin", 4096)
    _seed(data_dir, "models/checkpoints/studio.bin", 1024)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    typo = tmp_path / "typo-core.json"
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(typo))
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.cache_policy"):
        resolution = cache_policy.resolve_manifests()
        paths = cache_policy.manifest_paths()
        plan = cache_policy.plan_retention(policy=RetentionPolicy())

    # El catálogo core no se lee (su override no es ningún fichero)...
    assert paths == [repo_studio], paths
    assert repo_core.is_file(), "el manifiesto del repo sigue en su sitio"
    # ...pero su ausencia consta, en el resultado y en el log.
    assert resolution.missing_overrides == [str(typo)]
    assert plan.missing_overrides == [str(typo)]
    assert plan.to_public_dict()["missing_overrides"] == [str(typo)]
    assert str(typo) in caplog.text
    assert "core" in caplog.text, "el aviso debe nombrar el catálogo caído"
    # Y el plan no se disfraza de volumen sano: falta un catálogo entero.
    assert plan.keep_count == 1
    assert plan.keep_bytes == 1024
    assert plan.manifests == [str(repo_studio)]
    assert plan.unreadable_manifests == []
    # El catálogo presente sigue evaluándose con normalidad.
    tight = cache_policy.plan_retention(policy=RetentionPolicy(budget_bytes=1))
    assert "studio.bin" in {e.filename for e in tight.evictions}


def test_missing_override_on_both_catalogs_still_warns_about_the_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R42: con los dos overrides rotos el plan sigue diciendo que no evalúa nada."""
    from comfy_studio import cache_policy

    _fake_package(tmp_path, monkeypatch)
    monkeypatch.setattr(cache_policy, "MANIFEST_NAMES", ("models-core.json", "models-studio.json"))

    data_dir = tmp_path / "data"
    _seed(data_dir, "models/checkpoints/task.bin", 4096)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STUDIO_CORE_MANIFEST", str(tmp_path / "no-core.json"))
    monkeypatch.setenv("STUDIO_MANIFEST", str(tmp_path / "no-studio.json"))
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.cache_policy"):
        plan = cache_policy.plan_retention(policy=RetentionPolicy())

    assert plan.manifests == []
    assert plan.keep_count == 0
    assert sorted(Path(p).name for p in plan.missing_overrides) == [
        "no-core.json",
        "no-studio.json",
    ]
    assert "manifiesto" in caplog.text, "la política inoperante debe registrarse"
    assert plan.to_public_dict()["manifests_found"] == 0


def test_default_manifest_bases_are_shared_by_the_three_resolvers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R43: un CWD con su propio `config/` no puede hacer leer catálogos distintos.

    `cache_policy` resolvía los nombres relativos solo contra la raíz del paquete,
    mientras `capabilities.core_manifest_path()` y `model_assets.manifest_path()`
    probaban primero el directorio actual: desde un CWD con su propio
    `config/models-core.json`, el resolvedor leía ese manifiesto y el planificador
    el de la raíz del paquete. Los dos disentían sobre qué catálogo estaba en
    vigor. Ahora los tres comparten `model_assets.manifest_search_bases()`.
    """
    from comfy_studio import cache_policy, capabilities, model_assets

    # El CWD con su propio `config/`: el caso que los hacía divergir.
    cwd = tmp_path / "cwd"
    (cwd / "config").mkdir(parents=True)
    cwd_core = _write_manifest_at(
        cwd / "config" / "models-core.json",
        [_entry("cwd.bin", "models/checkpoints/cwd.bin", 111, "task")],
    )
    _write_manifest_at(
        cwd / "config" / "models-studio.json",
        [_entry("cwd-studio.bin", "models/checkpoints/cwd-studio.bin", 111, "task")],
    )

    # La raíz del paquete, con los suyos: otro contenido, para ver cuál gana.
    root = tmp_path / "pkg"
    pkg = root / "studio" / "comfy_studio"
    pkg.mkdir(parents=True)
    for module in ("cache_policy.py", "capabilities.py", "model_assets.py"):
        (pkg / module).write_text("# stub\n", encoding="utf-8")
    root_core = _write_manifest_at(
        root / "config" / "models-core.json",
        [_entry("pkg.bin", "models/checkpoints/pkg.bin", 222, "task")],
    )
    root_studio = _write_manifest_at(
        root / "config" / "models-studio.json",
        [_entry("pkg-studio.bin", "models/checkpoints/pkg-studio.bin", 222, "task")],
    )

    monkeypatch.setattr(cache_policy, "__file__", str(pkg / "cache_policy.py"))
    monkeypatch.setattr(capabilities, "__file__", str(pkg / "capabilities.py"))
    monkeypatch.setattr(model_assets, "__file__", str(pkg / "model_assets.py"))
    monkeypatch.setattr(cache_policy, "_RELATIVE_MANIFEST_BASES", ("config",))
    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)
    monkeypatch.delenv("STUDIO_CORE_MANIFEST", raising=False)
    monkeypatch.chdir(cwd)

    env = {"COMFY_DATA_DIR": str(tmp_path / "data")}
    # Los tres acaban en el manifiesto de la raíz del paquete.
    assert capabilities.core_manifest_path(env) == root_core
    assert model_assets.manifest_path(env) == root_studio
    assert cache_policy.manifest_paths(env) == [root_core, root_studio]
    assert cwd_core.is_file(), "el manifiesto del CWD sigue en su sitio"

    # Y con la raíz del paquete sin `config/` —el layout de la imagen, donde la
    # raíz es site-packages— el CWD sirve de último recurso para los tres.
    for path in (root_core, root_studio):
        path.unlink()
    assert capabilities.core_manifest_path(env) == cwd_core
    assert model_assets.manifest_path(env) == cwd / "config" / "models-studio.json"
    assert cache_policy.manifest_paths(env) == [
        cwd_core,
        cwd / "config" / "models-studio.json",
    ]


def test_json_payload_carries_the_manifest_count(tmp_path: Path) -> None:
    """R31: `--json` publica la cuenta de manifiestos, no solo el plan.

    El aviso por log no cruza el `--json` (el script no configura logging), así
    que un consumidor que parsea stdout leía un plan vacío indistinguible de un
    volumen sano.
    """
    manifest = json.loads(
        (REPO_ROOT / "config" / "models-studio.json").read_text(encoding="utf-8")
    )
    entry = next(m for m in manifest["models"] if str(m.get("dest_path", "")).startswith("models/"))

    data_dir = tmp_path / "data"
    seeded = _seed(data_dir, entry["dest_path"], 2048)

    env = _env_without_manifest_overrides(COMFY_DATA_DIR=str(data_dir))

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "prune_model_cache.py"), "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    payload = json.loads(proc.stdout)
    plan = payload["plan"]
    assert plan["manifests_found"] >= 1, "un plan vacío debe poder leerse como sano o no"
    assert plan["manifests"], "la lista de manifiestos viaja en el payload"
    assert plan["unreadable_manifests"] == []
    assert plan["keep_count"] >= 1
    assert plan["keep_bytes"] >= seeded.stat().st_size


def test_json_payload_marks_missing_manifests(tmp_path: Path) -> None:
    """R31: sin manifiestos, `--json` lo dice en el payload, no por stderr."""
    env = _env_with_no_manifests(tmp_path)
    (tmp_path / "data" / "models").mkdir(parents=True)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "prune_model_cache.py"), "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    payload = json.loads(proc.stdout)
    plan = payload["plan"]
    assert plan["manifests_found"] == 0
    assert plan["manifests"] == []
    assert plan["keep_count"] == 0
    assert "manifiesto" in proc.stderr, "la política inoperante debe registrarse también"


def test_human_render_marks_an_empty_plan_as_not_healthy(tmp_path: Path) -> None:
    """R31: la salida humana lleva la misma advertencia que el payload."""
    env = _env_with_no_manifests(tmp_path)
    (tmp_path / "data" / "models").mkdir(parents=True)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "prune_model_cache.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert "manifiestos leídos      0" in proc.stdout
    assert "no se evalúa ningún activo" in proc.stdout
    assert "activos en disco        0" in proc.stdout


def test_prune_script_plans_from_a_foreign_cwd(tmp_path: Path) -> None:
    """El CLI no puede depender del directorio desde el que se invoca.

    Se usan los manifiestos reales del repo (derivados, no literales) y se
    siembra en disco el primero que resuelva bajo `models/`.
    """
    manifest = json.loads(
        (REPO_ROOT / "config" / "models-studio.json").read_text(encoding="utf-8")
    )
    entry = next(m for m in manifest["models"] if str(m.get("dest_path", "")).startswith("models/"))

    data_dir = tmp_path / "data"
    seeded = _seed(data_dir, entry["dest_path"], 2048)

    env = _env_without_manifest_overrides(COMFY_DATA_DIR=str(data_dir))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "prune_model_cache.py"), "--json"],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    plan = json.loads(proc.stdout)["plan"]
    assert plan["keep_count"] >= 1, "desde otro CWD el plan debe ver los activos declarados"
    assert plan["keep_bytes"] >= seeded.stat().st_size
    assert seeded.is_file()


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
                "devices": [
                    {
                        "name": "RTX 5090",
                        "vram_total": 32 * 1024**3,
                        "vram_used": used,
                        "driver_version": "570.124.06",
                    }
                ],
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
    # El driver se mide en el mismo host y momento que el render: la receta debe
    # describir la máquina donde se midió de verdad, no la de `strict-check`.
    assert peak["driver"] == "570.124.06"


@pytest.mark.anyio
async def test_sampler_accepts_other_driver_key_names() -> None:
    """Las versiones de ComfyUI no publican la misma clave."""
    import asyncio

    from comfy_studio.comfyui_client import sample_system_stats_peak

    class _OldClient:
        async def system_stats(self) -> dict:
            return {
                "devices": [{"name": "RTX 4090", "vram_used": 1024**3, "driver": "550.90"}],
            }

    stop = asyncio.Event()

    async def _stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(_stopper())
    peak = await sample_system_stats_peak(_OldClient(), stop=stop, interval=0.01)
    await task
    assert peak["driver"] == "550.90"


@pytest.mark.anyio
async def test_driver_is_none_when_comfyui_does_not_publish_it() -> None:
    """Si ComfyUI no lo publica, se queda a None: no se inventa."""
    import asyncio

    from comfy_studio.comfyui_client import sample_system_stats_peak

    class _BareClient:
        async def system_stats(self) -> dict:
            return {"devices": [{"name": "RTX 4090", "vram_used": 1024**3}]}

    stop = asyncio.Event()

    async def _stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(_stopper())
    peak = await sample_system_stats_peak(_BareClient(), stop=stop, interval=0.01)
    await task
    assert peak["driver"] is None


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
