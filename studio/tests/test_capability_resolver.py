"""Tests del resolvedor de capacidades y de la adquisición de activos.

Verifican el flujo que define la política de arquitectura:

  «necesito esta capacidad»
  → el motor ya está preparado;
  → el resolvedor identifica sus activos soportados;
  → los que faltan se descargan con la herramienta existente;
  → se selecciona una receta;
  → los activos reutilizables se conservan (segunda pasada = skip).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from comfy_studio import asset_acquire, model_assets
from comfy_studio.capabilities import (
    CAPABILITIES,
    CAPABILITY_REGISTRY_VERSION,
    AssetNeed,
    list_capabilities,
    needs_for_group,
    resolve,
)
from comfy_studio.projects import ProjectStore

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COMFY_DATA_DIR", str(root))
    return root


#: Contenido que escribe el `hf` de mentira. Su hash se fija en los activos
#: para que la verificación de integridad del manifiesto pase.
FAKE_CONTENT = b"pesos-de-mentira"


def _write_fake_hf(tmp_path: Path, payload: bytes = FAKE_CONTENT) -> Path:
    """Cliente `hf` de mentira, portable (Python, no bash).

    Replica `hf download <repo> <remote_path> ... --local-dir <dir>`: escribe
    `payload` en `<dir>/<remote_path>`. Se usa un launcher Python en vez de un
    script bash para que la suite corra igual en Linux (CI/pod) y en
    Windows/macOS (desarrollo). El pod de producción es Linux; el cliente real
    es el `hf` de verdad, este fake solo ejercita el cableado de adquisición.
    """
    helper = tmp_path / "fake_hf_impl.py"
    helper.write_text(
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "remote = args[2]\n"
        "local_dir = args[args.index('--local-dir') + 1]\n"
        "payload = bytes.fromhex(args[args.index('--__payload_hex') + 1])"
        " if '--__payload_hex' in args else b'" + payload.decode() + "'\n"
        "target = pathlib.Path(local_dir) / remote\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "target.write_bytes(payload)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = tmp_path / "fake-hf.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / "fake-hf"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


@pytest.fixture()
def fake_hf(tmp_path: Path) -> Path:
    """Cliente `hf` de mentira: crea el archivo remoto bajo --local-dir."""
    return _write_fake_hf(tmp_path)


def _env(fake_hf: Path, data_dir: Path) -> dict[str, str]:
    return {"COMFY_DATA_DIR": str(data_dir), "HF_CLI": str(fake_hf)}


def _pin_fake_hashes(resolution) -> None:
    """Alinea el hash declarado con lo que escribe el `hf` de mentira."""
    import hashlib

    digest = hashlib.sha256(FAKE_CONTENT).hexdigest()
    for need in list(resolution.assets) + list(resolution.missing):
        need.sha256 = digest
        need.sha1 = None


# --------------------------------------------------------------------------
# Registro de capacidades
# --------------------------------------------------------------------------


def test_registry_is_versioned_and_non_empty() -> None:
    assert CAPABILITY_REGISTRY_VERSION >= 1
    assert len(CAPABILITIES) >= 15
    for capability_id, spec in CAPABILITIES.items():
        assert spec["engine"], f"{capability_id} sin motor"
        assert spec["summary"], f"{capability_id} sin resumen"
        assert isinstance(spec["templates"], list)


def test_every_registered_template_exists() -> None:
    """Una capacidad que apunta a una plantilla inexistente es un error de datos."""
    from comfy_studio.templates import get_default_registry

    registry = get_default_registry()
    for capability_id, spec in CAPABILITIES.items():
        for template_id in spec["templates"]:
            assert registry.get_raw_definition(template_id) is not None, (
                f"{capability_id} apunta a la plantilla inexistente {template_id!r}"
            )


def test_list_capabilities_is_serializable() -> None:
    blob = json.dumps(list_capabilities())
    assert '"video.text_to_video"' in blob


# --------------------------------------------------------------------------
# Resolución
# --------------------------------------------------------------------------


def test_unknown_capability_is_reported_not_guessed() -> None:
    resolution = resolve("video.does_not_exist")
    assert resolution.state == "unknown_capability"
    assert resolution.templates == []


def test_resolution_reports_engine_prep_and_assets(data_dir: Path) -> None:
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    # El motor no está instalado en el host de tests.
    assert resolution.engine == "comfyui"
    assert resolution.engine_prep["state"] == "not_installed"
    # Los activos que faltan tienen prioridad sobre el estado del motor: es lo
    # primero que hay que poder hacer, y no depende de tener GPU. El detalle del
    # motor sigue disponible en `engine_prep`.
    assert resolution.state == "assets_missing"
    assert resolution.engine_prep["state"] == "not_installed"
    # Los activos se resuelven igual: es información independiente del motor.
    assert len(resolution.assets) == 4
    assert all(not a.present for a in resolution.assets)
    assert resolution.missing_bytes > 0


def test_assets_carry_absolute_paths_and_manifest_origin(data_dir: Path) -> None:
    resolution = resolve("image.generate", env={"COMFY_DATA_DIR": str(data_dir)})
    need = resolution.assets[0]
    assert need.filename == "Juggernaut-XI-byRunDiffusion.safetensors"
    assert need.source == "core"
    assert need.asset_class == "task"
    assert Path(need.dest_path).is_absolute()
    assert str(data_dir) in need.dest_path
    assert need.manifest_dest.startswith("models/")


def test_optional_group_is_classified_optional(data_dir: Path) -> None:
    resolution = resolve("video.animate", env={"COMFY_DATA_DIR": str(data_dir)})
    classes = {a.filename: a.asset_class for a in resolution.assets}
    assert classes["wan2.2_animate_14B_int8_convrot.safetensors"] == "optional"
    assert classes["umt5_xxl_fp8_e4m3fn_scaled.safetensors"] == "task"


def test_bootstrap_is_empty_by_design(data_dir: Path) -> None:
    """Nada es bootstrap: ni un VAE ni un restaurador facial lo son.

    «Pertenecer a la Fase 1 no convierte un VAE o restaurador facial en requisito
    para abrir la API y descubrir capacidades.» El arranque no descarga nada;
    todo se pide por capacidad. La política prohíbe llamar bootstrap a una
    colección grande, y aquí directamente no hay nada que lo sea.
    """
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    bootstrap = [a for a in resolution.assets if a.asset_class == "bootstrap"]
    assert bootstrap == [], "el arranque no debe arrastrar ningún activo"
    # Lo que antes era «bootstrap» ahora es task: se descarga si se pide.
    task = [a for a in resolution.assets if a.asset_class == "task"]
    assert task, "la capacidad debe seguir declarando sus dependencias"


def test_engine_without_recipe_reports_no_assets(data_dir: Path) -> None:
    resolution = resolve("video.upscale", env={"COMFY_DATA_DIR": str(data_dir)})
    assert resolution.selected_template is None
    assert resolution.assets == []
    assert resolution.engine == "upscaler"


# --------------------------------------------------------------------------
# El manifiesto core tiene que encontrarse también fuera del repo
# --------------------------------------------------------------------------


def test_core_manifest_is_found_from_a_site_packages_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La imagen instala el paquete: `parents[2]` ya no es la raíz del repo.

    `pip install /opt/studio` deja el módulo en
    `/opt/venvs/studio/lib/python3.12/site-packages/comfy_studio/`, así que
    buscar `config/models-core.json` «dos niveles arriba» no encuentra nada y
    todo activo sale `source='unknown'`, `bytes=0` y sin destino: la adquisición
    bajo demanda se queda sin origen declarado. El manifiesto va horneado en
    `/etc/comfyui/models-core.json`, así que se resuelve con la misma lista de
    candidatos que `model_assets.manifest_path()`.
    """
    import comfy_studio.capabilities as capabilities

    repo_manifest = REPO_ROOT / "config" / "models-core.json"
    assert repo_manifest.is_file(), "el manifiesto core debe existir en el repo"

    # La lista real prioriza el entorno —con su propia variable— y termina en la
    # ruta horneada.
    assert capabilities._CORE_MANIFEST_CANDIDATES[0] == "{STUDIO_CORE_MANIFEST}"
    assert "/etc/comfyui/models-core.json" in capabilities._CORE_MANIFEST_CANDIDATES
    # `STUDIO_MANIFEST` es el override de Fase 3: nunca aparece aquí.
    assert "{STUDIO_MANIFEST}" not in capabilities._CORE_MANIFEST_CANDIDATES

    # Réplica del layout de la imagen: sin `config/` alrededor del paquete.
    site_pkg = (
        tmp_path
        / "venvs"
        / "studio"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "comfy_studio"
    )
    site_pkg.mkdir(parents=True)
    (site_pkg / "capabilities.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(capabilities, "__file__", str(site_pkg / "capabilities.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STUDIO_CORE_MANIFEST", raising=False)
    monkeypatch.delenv("STUDIO_MANIFEST", raising=False)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {
        "COMFY_DATA_DIR": str(data_dir),
        "STUDIO_DATA_DIR": str(tmp_path / "studio"),
    }

    # Con la lista real de candidatos y este layout, ningún manifiesto core es
    # alcanzable: se reproduce el síntoma exacto de la imagen (activos sin
    # origen, sin tamaño y sin destino).
    assert capabilities.core_manifest_path(env) is None
    assert capabilities._load_core_manifest(env) == {}
    blind = resolve("video.face_restore", env=env, probe_engines=False)
    assert blind.assets, "la plantilla face_fix declara activos"
    assert all(a.source == "unknown" for a in blind.assets)
    assert blind.missing_bytes == 0
    assert all(a.dest_path == "" for a in blind.assets)

    # El manifiesto horneado en la imagen (`/etc/comfyui/models-core.json`) sí
    # se lee desde site-packages. Aquí no se puede escribir en `/etc`, así que
    # la ruta horneada se sustituye por una equivalente en el temporal.
    baked = tmp_path / "etc" / "comfyui" / "models-core.json"
    baked.parent.mkdir(parents=True)
    baked.write_text(repo_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(
        capabilities,
        "_CORE_MANIFEST_CANDIDATES",
        ("{STUDIO_CORE_MANIFEST}", str(baked)),
    )
    assert capabilities.core_manifest_path(env) == baked
    entries = capabilities._load_core_manifest(env)
    assert entries, "el catálogo core no puede quedar vacío en la imagen"

    resolution = resolve("video.face_restore", env=env, probe_engines=False)
    assert resolution.assets, "sin el manifiesto core no hay activos que adquirir"
    assert all(a.source == "core" for a in resolution.assets)
    assert resolution.missing_bytes > 0
    for need in resolution.assets:
        assert Path(need.dest_path).is_absolute()
        assert str(data_dir) in need.dest_path

    # El override explícito del core manda sobre las rutas horneadas.
    explicit = tmp_path / "explicit-core.json"
    explicit.write_text(repo_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    with_override = dict(env, STUDIO_CORE_MANIFEST=str(explicit))
    assert capabilities.core_manifest_path(with_override) == explicit
    assert capabilities._load_core_manifest(with_override) == entries


def test_studio_manifest_does_not_hijack_the_core_catalog(data_dir: Path) -> None:
    """`STUDIO_MANIFEST` es el override de Fase 3, nunca el del catálogo core.

    Los dos catálogos son disjuntos (26 entradas core frente a 87 de estudio,
    cero solape), así que usarla como override del core dejaba **todos** los
    activos de Fase 1/2 en `source='unknown'`, `bytes=0` y sin destino en cuanto
    un operario fijaba `STUDIO_MANIFEST` para lo que está documentado. Es una
    regresión condicional del modo de fallo que motivó la lista de candidatos
    horneados, y por eso el core tiene su propia variable.
    """
    import comfy_studio.capabilities as capabilities

    studio_manifest = REPO_ROOT / "config" / "models-studio.json"
    core_manifest = REPO_ROOT / "config" / "models-core.json"
    assert studio_manifest.is_file(), "el manifiesto studio debe existir en el repo"
    env = {
        "COMFY_DATA_DIR": str(data_dir),
        "STUDIO_MANIFEST": str(studio_manifest),
    }

    # La variable de Fase 3 sigue sirviendo para lo suyo...
    assert model_assets.manifest_path(env) == studio_manifest
    # ...y el core se resuelve por sus propias rutas, ajenas a ella.
    resolved = capabilities.core_manifest_path(env)
    assert resolved is not None, "el catálogo core no puede quedar ciego"
    assert resolved != studio_manifest

    declared = {
        entry["filename"]: int(entry.get("bytes") or 0)
        for entry in json.loads(core_manifest.read_text(encoding="utf-8"))["models"]
    }
    for capability in ("image.generate", "video.face_restore"):
        resolution = resolve(capability, env=env, probe_engines=False)
        assert resolution.assets, capability
        for need in resolution.assets:
            assert need.source == "core", (
                f"{capability}/{need.filename} perdió el origen core"
            )
            assert need.bytes == declared[need.filename]
            assert need.bytes > 0
            assert need.dest_path, f"{capability}/{need.filename} sin destino"
            assert Path(need.dest_path).is_absolute()


def test_core_manifest_expands_placeholders_like_the_studio_one(
    tmp_path: Path,
) -> None:
    """`{COMFY_DATA_DIR}`/`{COMFY_APP_DIR}` se expanden igual en los dos.

    `core_manifest_path()` llamaba a `os.path.expanduser` a secas, así que un
    override con marcadores divergía de `model_assets.manifest_path()`: el
    catálogo de Fase 3 se leía y el core quedaba inalcanzable.
    """
    import comfy_studio.capabilities as capabilities

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core_in_data = data_dir / "models-core.json"
    core_in_data.write_text(
        (REPO_ROOT / "config" / "models-core.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    studio_in_data = data_dir / "models-studio.json"
    studio_in_data.write_text('{"models": []}\n', encoding="utf-8")

    env = {
        "COMFY_DATA_DIR": str(data_dir),
        "COMFY_APP_DIR": str(tmp_path / "ComfyUI"),
        "STUDIO_CORE_MANIFEST": "{COMFY_DATA_DIR}/models-core.json",
        "STUDIO_MANIFEST": "{COMFY_DATA_DIR}/models-studio.json",
    }
    # El marcador se expande antes de juzgar si la ruta es absoluta: los dos
    # resolvedores acaban en el mismo tipo de ruta y en el fichero pedido.
    assert capabilities.core_manifest_path(env) == core_in_data
    assert model_assets.manifest_path(env) == studio_in_data
    assert capabilities._load_core_manifest(env), "el override core debe leerse"

    # Y el catálogo recuperado es el core, no el de estudio.
    resolution = resolve("image.generate", env=env, probe_engines=False)
    assert resolution.assets[0].source == "core"
    assert resolution.assets[0].bytes > 0


def test_retention_planner_keeps_the_core_catalog_under_the_studio_override(
    tmp_path: Path,
) -> None:
    """R17: los dos módulos tienen que coincidir sobre qué catálogo está en vigor.

    `capabilities` resuelve el core con `STUDIO_CORE_MANIFEST`, pero
    `cache_policy` usaba `STUDIO_MANIFEST` como override de **los dos**
    manifiestos: con el override documentado de Fase 3 fijado,
    `manifest_paths()` devolvía solo `models-studio.json` y el catálogo core
    quedaba fuera del planificador de retención —`keep_bytes` se quedaba corto
    en sus ~68 GB y ningún activo de Fases 1/2 era candidato a expulsión—,
    mientras el resolvedor de capacidades seguía viendo el core real.
    """
    from comfy_studio import cache_policy
    from comfy_studio.cache_policy import RetentionPolicy

    core_manifest = REPO_ROOT / "config" / "models-core.json"
    studio_manifest = REPO_ROOT / "config" / "models-studio.json"
    declared = json.loads(core_manifest.read_text(encoding="utf-8"))["models"]
    entry = next(
        m
        for m in declared
        if str(m.get("dest_path", "")).startswith("models/")
        and str(m.get("class") or "task") == "task"
    )

    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    env = {
        "COMFY_DATA_DIR": str(data_dir),
        "STUDIO_MANIFEST": str(studio_manifest),
    }
    seeded = model_assets.resolve_dest(str(entry["dest_path"]), env)
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_bytes(b"x" * 4096)

    paths = cache_policy.manifest_paths(env)
    assert core_manifest in paths, "el catálogo core no puede caerse del planificador"
    assert studio_manifest in paths, "el override de Fase 3 debe seguir mandando"

    # El core cuenta en la retención: sin él el volumen se mide sin sus 68 GB.
    plan = cache_policy.plan_retention(env=env, policy=RetentionPolicy())
    assert plan.keep_bytes >= seeded.stat().st_size
    assert plan.evictions == [], "sin presupuesto no se expulsa nada"

    # Y sigue siendo candidato a expulsión cuando hay presupuesto.
    tight = cache_policy.plan_retention(env=env, policy=RetentionPolicy(budget_bytes=1))
    assert entry["filename"] in {e.filename for e in tight.evictions}


# --------------------------------------------------------------------------
# Un manifiesto core inválido degrada a 'unknown', no rompe la herramienta
# --------------------------------------------------------------------------


def test_malformed_core_manifest_degrades_to_unknown(tmp_path: Path) -> None:
    """R18: una entrada mala no puede tumbar todo el cinturón de capacidades.

    El índice `{entry['filename']: entry ...}` quedaba **fuera** del
    `try`/`except`, así que un manifiesto core con una entrada sin `filename`
    (o con una lista en lugar de objeto) propagaba `KeyError`/`AttributeError`
    desde `resolve()` —y por tanto desde `list_capabilities` y
    `request_capability`—. Es alcanzable desde que el operario puede señalar su
    propio manifiesto con `STUDIO_CORE_MANIFEST`.
    """
    import comfy_studio.capabilities as capabilities

    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    base_env = {"COMFY_DATA_DIR": str(data_dir)}

    cases: dict[str, tuple[str, dict[str, dict[str, object]]]] = {
        "entry_without_filename": (
            '{"models": [{"dest_path": "models/x.bin", "bytes": 12}]}',
            {},
        ),
        "filename_not_a_string": ('{"models": [{"filename": 7}]}', {}),
        "empty_filename": (
            '{"models": [{"filename": ""}, {"filename": "ok.bin"}]}',
            {"ok.bin": {"filename": "ok.bin"}},
        ),
        "entries_not_dicts": ('{"models": ["ok.bin", 3, null, []]}', {}),
        "models_not_a_list": ('{"models": {"ok.bin": {}}}', {}),
        "top_level_list": ('[{"filename": "ok.bin"}]', {}),
        "top_level_string": ('"models-core.json"', {}),
        "not_json": ("{{{ esto no es JSON", {}),
    }
    for label, (blob, expected) in cases.items():
        path = tmp_path / f"{label}.json"
        path.write_text(blob, encoding="utf-8")
        env = dict(base_env, STUDIO_CORE_MANIFEST=str(path))
        assert capabilities._load_core_manifest(env) == expected, label
        # La resolución completa sigue funcionando: declara 'unknown' en vez
        # de inventar origen ni reventar.
        resolution = resolve("video.face_restore", env=env, probe_engines=False)
        assert resolution.assets, label
        assert all(a.source == "unknown" for a in resolution.assets), label
        assert all(a.bytes == 0 and a.dest_path == "" for a in resolution.assets), label
        # La respuesta del tool-belt sigue serializándose.
        json.dumps(resolution.to_public_dict())

    assert capabilities.list_capabilities(), "el catálogo no depende del manifiesto"


# --------------------------------------------------------------------------
# El campo `bytes` también es dato del operario (R41)
# --------------------------------------------------------------------------


def test_non_numeric_bytes_degrade_to_zero_instead_of_killing_resolve(tmp_path: Path) -> None:
    """R41: `bytes` no numérico no puede tumbar `resolve()` ni el tool-belt.

    `_load_core_manifest()` validaba la forma del JSON, pero el valor que
    devuelve lo consume `_asset_need()`, donde `int(entry.get("bytes") or 0)`
    estaba **fuera** de todo `try`/`except`. El manifiesto core puede venir de
    `STUDIO_CORE_MANIFEST`, que escribe el operario: una sola entrada con
    `bytes` no numérica hacía que `request_capability` reventara con
    `ValueError`/`TypeError`. `model_assets.class_bytes()` ya envolvia la misma
    conversión; aquí se aplica la misma regla.
    """
    from comfy_studio.templates import get_default_registry

    declared = list(get_default_registry().get_raw_definition("face_fix")["models"])
    assert declared, "la plantilla face_fix declara activos"

    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    base_env = {"COMFY_DATA_DIR": str(data_dir)}

    # Una forma por caso: las cinco que revientan con `int()` y las dos que no.
    shapes: dict[str, tuple[object, int]] = {
        "string": ("muchos", 0),
        "float_string": ("12.5", 0),
        "exponent_string": ("1e3", 0),
        "dict": ({"n": 1}, 0),
        "list": ([1], 0),
        "null": (None, 0),
        # Un float JSON sí lo trunca `int()`: no degrada, pero tampoco rompe.
        "float": (12.5, 12),
    }
    for label, (raw, expected) in shapes.items():
        path = tmp_path / f"bytes-{label}.json"
        path.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "filename": filename,
                            "dest_path": f"models/checkpoints/{filename}",
                            "bytes": raw,
                            "class": "task",
                        }
                        for filename in declared
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = dict(base_env, STUDIO_CORE_MANIFEST=str(path))
        resolution = resolve("video.face_restore", env=env, probe_engines=False)
        assert resolution.assets, label
        # El origen sigue siendo el core: la entrada se leyó, solo su tamaño no.
        assert all(a.source == "core" for a in resolution.assets), label
        assert all(a.bytes == expected for a in resolution.assets), label
        assert resolution.missing_bytes == expected * len(declared), label
        json.dumps(resolution.to_public_dict()), label


# --------------------------------------------------------------------------
# Un `bytes` no finito tampoco puede tumbar el resolvedor (R94)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "literal", "expected"),
    [
        ("Infinity", "Infinity", 0),
        ("-Infinity", "-Infinity", 0),
        ("overflow", "1e400", 0),
    ],
)
def test_non_finite_bytes_degrade_instead_of_killing_resolve(
    tmp_path: Path, label: str, literal: str, expected: int
) -> None:
    """R94: `Infinity` es JSON válido para `json.load`, y `int(inf)` no es `ValueError`.

    `json.load` acepta el literal no estándar `Infinity`, así que un manifiesto
    de operario con `"bytes": Infinity` llega a `_asset_need` como
    `float('inf')`. Allí `int(value or 0)` responde `OverflowError` —un
    `ArithmeticError` que no captura el `except (TypeError, ValueError)`—, y la
    excepción salía de `needs_for_group` y de `resolve()` sin informe. Es el
    mismo R85 que `asset_acquire._declared_bytes` ya cubre, un nivel más arriba
    de donde se consume el tamaño.
    """
    from comfy_studio.templates import get_default_registry

    declared = list(get_default_registry().get_raw_definition("face_fix")["models"])
    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    # El literal se escribe a pelo: `json.dumps` con `allow_nan` sí lo emite,
    # pero lo que se quiere fijar es justo la forma que un operario puede haber
    # escrito a mano en el manifiesto.
    path = tmp_path / f"bytes-{label}.json"
    path.write_text(
        '{"models": ['
        + ",".join(
            json.dumps(
                {
                    "filename": filename,
                    "dest_path": f"models/checkpoints/{filename}",
                    "bytes": "__BYTES__",
                    "class": "task",
                }
            ).replace('"__BYTES__"', literal)
            for filename in declared
        )
        + "]}",
        encoding="utf-8",
    )
    assert literal in path.read_text(encoding="utf-8"), label
    env = {"COMFY_DATA_DIR": str(data_dir), "STUDIO_CORE_MANIFEST": str(path)}

    resolution = resolve("video.face_restore", env=env, probe_engines=False)

    assert resolution.assets, label
    assert all(a.bytes == expected for a in resolution.assets), label
    assert resolution.missing_bytes == expected * len(declared), label
    json.dumps(resolution.to_public_dict()), label

    # Y `needs_for_group`, que consume las mismas entradas por la rama del
    # manifiesto de Fase 3: allí el `OverflowError` también salía sin informe.
    studio = tmp_path / f"studio-{label}.json"
    studio.write_text(
        '{"models": ['
        + ",".join(
            json.dumps(
                {
                    "filename": f"grupo-{index}.bin",
                    "dest_path": "models/checkpoints/grupo.bin",
                    "bytes": "__BYTES__",
                    "class": "task",
                    "opt_in": "STUDIO_INCLUDE_ANIMATE",
                }
            ).replace('"__BYTES__"', literal)
            for index in range(2)
        )
        + "]}",
        encoding="utf-8",
    )
    group_env = dict(env, STUDIO_MANIFEST=str(studio))
    needs = needs_for_group("STUDIO_INCLUDE_ANIMATE", env=group_env)
    assert [n.bytes for n in needs] == [expected, expected], label


# --------------------------------------------------------------------------
# El mismo `bytes` no finito en los otros tres consumidores (D4/R94)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "1e400"])
def test_non_finite_bytes_do_not_kill_model_assets(
    tmp_path: Path, literal: str
) -> None:
    """D4: `class_bytes()` y `summary()` degradan, no abortan.

    `class_bytes()` capturaba solo `TypeError`/`ValueError`, así que degradaba
    los no numéricos y propagaba los no finitos. `summary()` —que la consume—
    está cableada sin protección en `routers/core.py` (`/studio/api/status`),
    así que un manifiesto de operario con `"bytes": Infinity` tumbaba el estado
    del pod. La regla es ahora la que `capabilities._declared_bytes` ya aplica.
    """
    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    path = tmp_path / f"studio-{literal}.json"
    path.write_text(
        '{"models": ['
        + ",".join(
            json.dumps(
                {
                    "filename": f"activo-{index}.bin",
                    "dest_path": f"models/checkpoints/activo-{index}.bin",
                    "bytes": "__BYTES__",
                    "class": "task",
                }
            ).replace('"__BYTES__"', literal)
            for index in range(2)
        )
        + "]}",
        encoding="utf-8",
    )
    env = {"COMFY_DATA_DIR": str(data_dir), "STUDIO_MANIFEST": str(path)}

    totals = model_assets.class_bytes(env)

    assert totals["task"] == 0, literal
    summary = model_assets.summary(env)
    assert summary["classes"]["task"]["bytes"] == 0, literal
    json.dumps(summary), literal


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "1e400"])
def test_non_finite_bytes_do_not_kill_the_storage_requirement_script(
    tmp_path: Path, literal: str
) -> None:
    """D4: `scripts/storage_requirement.py` deriva el requisito, no muere.

    El script salía con rc=1 y una traza de `OverflowError` desde cualquiera de
    sus cuatro `int(entry.get("bytes") or 0)`: un manifiesto con `Infinity` no
    daba ningún requisito que publicar. Un tamaño ilegible es «no declarado»,
    cuenta 0, y el resto de las entradas siguen sumando.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import storage_requirement

    manifest = {
        "models": [
            {
                "filename": "bueno.bin",
                "dest_path": "models/checkpoints/bueno.bin",
                "bytes": 4096,
                "class": "task",
            },
            {
                "filename": "raro.bin",
                "dest_path": "models/checkpoints/raro.bin",
                "bytes": "__BYTES__",
                "class": "task",
            },
        ]
    }
    # El literal se escribe a mano: es la forma que un operario puede dejar en
    # el JSON, y `json.load` la acepta.
    raw = json.dumps(manifest).replace('"__BYTES__"', literal)
    parsed = json.loads(raw)
    assert parsed["models"][1]["bytes"] != 4096, literal

    assert storage_requirement._largest_default_bytes(parsed) == 4096, literal
    assert storage_requirement._opt_in_bytes(parsed) == {}, literal

    core = {
        "models": [
            {**manifest["models"][0], "class": "bootstrap"},
            {**manifest["models"][1], "class": "bootstrap"},
        ]
    }
    assert storage_requirement.bootstrap_download_bytes(core) == 4096, literal

    core_on_demand = {
        "models": [
            {**manifest["models"][0], "class": "task", "profiles": ["studio"]},
            {**manifest["models"][1], "class": "task", "profiles": ["studio"]},
        ]
    }
    assert (
        storage_requirement.on_demand_largest_bytes(core_on_demand, profile="studio")
        == 4096
    ), literal

    # Y el script entero, que es lo que ve el operario.
    path = tmp_path / f"manifest-{literal}.json"
    path.write_text(raw, encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "storage_requirement.py"),
            "--profile",
            "studio",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=dict(os.environ, STUDIO_MANIFEST=str(path)),
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["components"]["task_models"] >= 0, literal


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "1e400"])
def test_non_finite_bytes_do_not_kill_the_manifest_validator(literal: str) -> None:
    """D4: `check_models_manifest.py` informa del error, no revienta.

    El validador ya rechazaba la entrada con «bytes inválido» —`Infinity` no es
    un `int`—, pero el desglose por clase acumulaba con un `int()` pelado y el
    `OverflowError` se llevaba por delante la validación entera: el operario se
    quedaba sin la lista de errores que había ido a buscar.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_models_manifest

    assert check_models_manifest._declared_bytes({"bytes": 1}) == 1, literal
    assert check_models_manifest._declared_bytes({"bytes": None}) == 0, literal
    assert check_models_manifest._declared_bytes({"bytes": "muchos"}) == 0, literal
    assert check_models_manifest._declared_bytes(
        {"bytes": json.loads(literal)}
    ) == 0, literal

    # Y el validador entero sobre una entrada con el literal: la lista de
    # errores tiene que existir, no una traza.
    entry = {
        "category": "wan_core",
        "repo_id": "org/repo",
        "remote_path": "model.bin",
        "url": "https://huggingface.co/org/repo/resolve/main/model.bin",
        "bytes": "__BYTES__",
        "target_dir": "models/checkpoints",
        "filename": "model.bin",
        "dest_path": "models/checkpoints/model.bin",
        "license": "apache-2.0",
        "revision": "a" * 40,
        "sha256": "b" * 64,
        "class": "task",
    }
    raw = json.dumps({"models": [entry]}).replace('"__BYTES__"', literal)
    # El desglose de `meta` declara 0 bytes para la clase: es lo que el
    # validador tiene que **calcular** ahora que el literal no cuenta.
    data = json.loads(raw)
    data.update(
        version=2,
        profile="studio",
        meta={
            "default_entries": 1,
            "default_bytes": 0,
            "opt_in_entries": 0,
            "opt_in_bytes": 0,
            "categories": {"wan_core": {"entries": 1, "bytes": 0}},
            "classes": {
                "bootstrap": {"entries": 0, "bytes": 0},
                "task": {"entries": 1, "bytes": 0},
                "optional": {"entries": 0, "bytes": 0},
            },
        },
    )
    errors = check_models_manifest.validate_manifest_data(
        data, require_all_categories=False
    )
    assert any("bytes inválido" in e for e in errors), (literal, errors)
    # Y el desglose cuadra con el 0: si el `int()` pelado siguiera vivo, la
    # llamada de arriba habría abortado con `OverflowError`.
    assert not [e for e in errors if "classes" in e and "calculado" in e], (
        literal,
        errors,
    )


def test_numeric_bytes_still_come_through(tmp_path: Path) -> None:
    """La coerción no puede degradar a 0 lo que sí es un número."""
    from comfy_studio.templates import get_default_registry

    declared = list(get_default_registry().get_raw_definition("face_fix")["models"])
    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    path = tmp_path / "bytes-numeric.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "filename": filename,
                        "dest_path": f"models/checkpoints/{filename}",
                        # Unos entero, otros cadena de entero: `int()` acepta las dos.
                        "bytes": 2048 if index % 2 else "4096",
                        "class": "task",
                    }
                    for index, filename in enumerate(declared)
                ]
            }
        ),
        encoding="utf-8",
    )
    env = {"COMFY_DATA_DIR": str(data_dir), "STUDIO_CORE_MANIFEST": str(path)}
    resolution = resolve("video.face_restore", env=env, probe_engines=False)
    assert [a.bytes for a in resolution.assets] == [
        4096 if index % 2 == 0 else 2048 for index in range(len(declared))
    ]


# --------------------------------------------------------------------------
# Un manifiesto de Fase 3 mal formado tampoco puede romper el resolvedor (R45)
# --------------------------------------------------------------------------


def test_malformed_studio_manifest_degrades_instead_of_crashing(tmp_path: Path) -> None:
    """R45: el endurecimiento de R18 se aplicó a un catálogo nada más.

    `_asset_need()` cae en `model_assets.entries(env)`, que hace
    `manifest.get("models", [])` sobre lo que devolvió `load_manifest()`: un
    manifiesto de Fase 3 que sea JSON válido pero no un objeto —alcanzable por
    `STUDIO_MANIFEST`, que escribe el operario— propagaba `AttributeError` desde
    `resolve()`. `cache_policy._manifest_entries()` ya descartaba esa forma; el
    resolvedor de capacidades no.
    """
    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    empty_core = tmp_path / "core-vacio.json"
    empty_core.write_text('{"models": []}\n', encoding="utf-8")

    shapes: dict[str, str] = {
        "top_level_list": json.dumps(["no", "un", "objeto"]),
        "top_level_string": '"models-studio.json"',
        "top_level_number": "7",
        "models_not_a_list": '{"models": 5}',
    }
    for label, blob in shapes.items():
        studio = tmp_path / f"studio-{label}.json"
        studio.write_text(blob, encoding="utf-8")
        env = {
            "COMFY_DATA_DIR": str(data_dir),
            "STUDIO_CORE_MANIFEST": str(empty_core),
            "STUDIO_MANIFEST": str(studio),
        }
        # El core vacío fuerza el camino de Fase 3, que es el que reventaba.
        for capability in ("video.text_to_video", "video.animate", "video.lipsync"):
            resolution = resolve(capability, env=env, probe_engines=False)
            assert resolution.assets, (label, capability)
            assert all(a.source == "unknown" for a in resolution.assets), (label, capability)
            assert all(a.bytes == 0 and a.dest_path == "" for a in resolution.assets), (
                label,
                capability,
            )
            json.dumps(resolution.to_public_dict())

    # Y los grupos opt-in del tool-belt aguantan la misma forma.
    assert needs_for_group("STUDIO_INCLUDE_ANIMATE", env=env) == []


# --------------------------------------------------------------------------
# Acoplamiento entre módulos: el helper de expansión es público
# --------------------------------------------------------------------------


def test_core_manifest_expansion_goes_through_the_public_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R19: `capabilities` no depende de un nombre privado de `model_assets`.

    La expansión de `{COMFY_DATA_DIR}`/`{COMFY_APP_DIR}` vivía en
    `model_assets._expand`, privado: un renombrado que ruff no protege habría
    roto en silencio la resolución del manifiesto core. Ahora es pública y la
    usan los dos resolvedores.
    """
    import comfy_studio.capabilities as capabilities

    assert (
        model_assets.expand_placeholders(
            "{COMFY_DATA_DIR}/models-core.json", {"COMFY_DATA_DIR": "/data"}
        )
        == "/data/models-core.json"
    )
    assert model_assets.expand_placeholders("{COMFY_APP_DIR}/m.json", {}) == (
        "/opt/ComfyUI/m.json"
    )
    assert model_assets.expand_placeholders("~/m.json", {}) == os.path.expanduser("~/m.json")
    # El alias privado sigue siendo el mismo objeto: nadie queda descolgado.
    assert model_assets._expand is model_assets.expand_placeholders

    # El core pasa por el público: si el privado se rompe, el core no.
    def _boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("capabilities no debe llamar al helper privado")

    monkeypatch.setattr(model_assets, "_expand", _boom)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    manifest = data_dir / "models-core.json"
    manifest.write_text('{"models": []}\n', encoding="utf-8")
    env = {
        "COMFY_DATA_DIR": str(data_dir),
        "STUDIO_CORE_MANIFEST": "{COMFY_DATA_DIR}/models-core.json",
    }
    assert capabilities.core_manifest_path(env) == manifest


# --------------------------------------------------------------------------
# El perfil de hardware viaja con la receta
# --------------------------------------------------------------------------


def _recipe_store(tmp_path: Path) -> tuple[ProjectStore, dict[str, str]]:
    """Store con una medición de 40 GB: cabe en 96 GB, no en 32 GB."""
    data_dir = tmp_path / "data"
    (data_dir / "models").mkdir(parents=True)
    studio_dir = tmp_path / "studio"
    store = ProjectStore(studio_dir)
    store.record_render_measurement(
        template_id="face_fix",
        duration_s=12.5,
        vram_peak_mb=40 * 1024,
        gpu_name="RTX 5090",
    )
    env = {"COMFY_DATA_DIR": str(data_dir), "STUDIO_DATA_DIR": str(studio_dir)}
    return store, env


def test_recipe_is_judged_against_the_profile_from_the_environment(tmp_path: Path) -> None:
    """El resolvedor no puede juzgar la receta contra el perfil por omisión.

    `resolve()` llamaba a `recipe_for_template` sin perfil, así que un pod con
    `STUDIO_HARDWARE_PROFILE=consumer` veía el veredicto de 96 GB (y al revés).
    """
    from comfy_studio.hardware_profile import BLACKWELL_96_128, CONSUMER_32

    store, env = _recipe_store(tmp_path)
    env = dict(env, STUDIO_HARDWARE_PROFILE="consumer")

    resolution = resolve(
        "video.face_restore", env=env, probe_engines=False, store=store
    )
    assert resolution.profile is not None
    assert resolution.profile.name == CONSUMER_32.name
    fit = resolution.recipe["vram_fit"]
    assert fit["profile"] == CONSUMER_32.name
    assert fit["target_vram_usable_gb"] == 30.0
    assert fit["fits"] is False, "40 GB no caben en los 30 GB utilizables de 32 GB"

    # El perfil explícito manda sobre el del entorno.
    explicit = resolve(
        "video.face_restore",
        env=env,
        probe_engines=False,
        store=store,
        profile=BLACKWELL_96_128,
    )
    wide = explicit.recipe["vram_fit"]
    assert wide["profile"] == BLACKWELL_96_128.name
    assert wide["target_vram_usable_gb"] == 94.0
    assert wide["fits"] is True
    assert explicit.profile is BLACKWELL_96_128


def test_profile_defaults_to_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin perfil explícito se lee del proceso, no del objetivo por omisión.

    Es lo que hace la herramienta del agente: pasa `context.config.hardware_profile`,
    que sale del mismo entorno.
    """
    from comfy_studio.hardware_profile import CONSUMER_32

    store, env = _recipe_store(tmp_path)
    monkeypatch.setenv("STUDIO_HARDWARE_PROFILE", "consumer")

    resolution = resolve("video.face_restore", store=store, probe_engines=False)
    assert resolution.profile is not None
    assert resolution.profile.name == CONSUMER_32.name
    assert resolution.recipe["vram_fit"]["fits"] is False
    assert resolution.recipe["vram_fit"]["target_vram_usable_gb"] == 30.0


def test_unknown_capability_also_carries_the_profile(tmp_path: Path) -> None:
    """Ni el camino de error se salta el perfil: la respuesta es homogénea."""
    from comfy_studio.hardware_profile import DEFAULT_PROFILE

    store, env = _recipe_store(tmp_path)
    resolution = resolve(
        "video.does_not_exist", env=env, probe_engines=False, store=store
    )
    assert resolution.state == "unknown_capability"
    assert resolution.profile is not None
    assert resolution.profile.name == DEFAULT_PROFILE.name


def test_public_dict_publishes_the_profile_it_judged_with(tmp_path: Path) -> None:
    """La superficie de capacidades declara contra qué máquina juzgó.

    El perfil viajaba solo dentro de `recipe['vram_fit']`, así que el agente
    tenía que hurgar en la receta —y no hay receta cuando el pod no ha renderado
    todavía— para saber la base del veredicto. Ahora se publica al lado del
    resto del resultado.
    """
    from comfy_studio.hardware_profile import CONSUMER_32, DEFAULT_PROFILE

    store, env = _recipe_store(tmp_path)
    resolution = resolve(
        "video.face_restore",
        env=env,
        probe_engines=False,
        store=store,
        profile=CONSUMER_32,
    )
    payload = resolution.to_public_dict()
    assert payload["profile"]["name"] == CONSUMER_32.name
    assert payload["profile"]["vram_usable_gb"] == CONSUMER_32.vram_usable_gb
    assert payload["profile"]["ram_usable_gb"] == CONSUMER_32.ram_usable_gb
    # Es la misma base con la que se juzgó la receta, no una casualidad.
    assert payload["recipe"]["vram_fit"]["profile"] == payload["profile"]["name"]
    assert payload["recipe"]["vram_fit"]["target_vram_usable_gb"] == (
        CONSUMER_32.vram_usable_gb
    )
    json.dumps(payload)  # la respuesta del tool-belt tiene que serializarse

    # Sin receta medida el perfil sigue publicado: es el del entorno/objetivo.
    bare = resolve("image.generate", env=env, probe_engines=False).to_public_dict()
    assert bare["recipe"] is None
    assert bare["profile"]["name"] == DEFAULT_PROFILE.name
    assert bare["profile"]["vram_usable_gb"] == DEFAULT_PROFILE.vram_usable_gb


# --------------------------------------------------------------------------
# Adquisición
# --------------------------------------------------------------------------


def test_acquire_downloads_then_reuses(data_dir: Path, fake_hf: Path) -> None:
    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)

    _pin_fake_hashes(resolution)
    report = asset_acquire.acquire(resolution.missing, env=env)
    assert not report.failed
    assert all(e.status == "downloaded" for e in report.entries)
    assert report.downloaded_bytes > 0
    # Los archivos existen donde el manifiesto dice.
    for need in resolution.missing:
        assert Path(need.dest_path).is_file()

    # Segunda pasada: todo se reutiliza, cero descargas.
    second = asset_acquire.acquire(resolution.missing, env=env)
    assert all(e.status == "skipped" for e in second.entries)
    assert second.downloaded_bytes == 0

    # Y el resolvedor ya no ve activos faltantes.
    after = resolve("video.face_restore", env=env)
    assert after.missing == []


def test_acquire_never_writes_outside_the_data_dir(data_dir: Path, fake_hf: Path) -> None:
    """Un destino no absoluto se rechaza: nada de escrituras en el repo."""
    env = _env(fake_hf, data_dir)
    bogus = AssetNeed(
        filename="x.bin",
        dest_path="models/checkpoints/x.bin",
        bytes=10,
        asset_class="task",
        present=False,
        source="core",
        repo_id="repo/x",
        remote_path="x.bin",
    )
    report = asset_acquire.acquire([bogus], env=env)
    assert report.entries[0].status == "failed"
    assert "no absoluto" in (report.entries[0].error or "")
    assert not (REPO_ROOT / "models").exists()


def test_acquire_without_hf_client_fails_closed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sin cliente de HuggingFace resoluble (portable: no depende de que el PATH
    # del OS de desarrollo carezca de un `hf`), la adquisición falla cerrada.
    monkeypatch.setattr(asset_acquire, "resolve_hf_cli", lambda env=None: None)
    env = {"COMFY_DATA_DIR": str(data_dir)}
    report = asset_acquire.acquire(
        [
            AssetNeed(
                filename="x.bin",
                dest_path=str(data_dir / "x.bin"),
                bytes=10,
                asset_class="task",
                present=False,
                source="core",
                repo_id="repo/x",
                remote_path="x.bin",
            )
        ],
        env=env,
    )
    assert report.entries[0].status == "failed"
    assert "HuggingFace" in (report.entries[0].error or "")


def test_disk_guard_fails_closed_when_space_is_short(data_dir: Path, fake_hf: Path) -> None:
    """Con margen enorme y disco justo, la descarga se rechaza antes de empezar."""
    env = _env(fake_hf, data_dir)
    need = AssetNeed(
        filename="grande.bin",
        dest_path=str(data_dir / "grande.bin"),
        bytes=1024**3,
        asset_class="task",
        present=False,
        source="core",
        repo_id="repo/x",
        remote_path="grande.bin",
    )
    report = asset_acquire.acquire([need], env=env, margin_bytes=10**15)
    assert report.entries[0].status == "failed"
    assert "espacio insuficiente" in (report.entries[0].error or "")


def test_cancellation_stops_between_entries(data_dir: Path, fake_hf: Path) -> None:
    env = _env(fake_hf, data_dir)
    needs = needs_for_group("STUDIO_INCLUDE_ANIMATE", env=env)
    assert needs, "el grupo animate debe declarar activos"
    report = asset_acquire.acquire(
        needs, env=env, should_cancel=lambda: True
    )
    assert report.cancelled is True
    # Nada se descarga, pero ninguna necesidad desaparece del informe: si se
    # fueran, el llamador no sabría qué falta tras la cancelación.
    assert [e.status for e in report.entries] == ["skipped"] * len(needs)
    assert not any(Path(n.dest_path).exists() for n in needs)


def test_progress_callback_reports_each_phase(data_dir: Path, fake_hf: Path) -> None:
    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    _pin_fake_hashes(resolution)
    events: list[tuple[str, str]] = []
    asset_acquire.acquire(
        resolution.missing, env=env, progress=lambda f, s: events.append((f, s))
    )
    assert events, "debe emitir progreso"
    assert any(state == "downloaded" for _, state in events)


def test_group_needs_follow_the_manifest(data_dir: Path) -> None:
    env = {"COMFY_DATA_DIR": str(data_dir)}
    for flag, expected_count in (
        ("STUDIO_INCLUDE_ANIMATE", 1),
        ("STUDIO_INCLUDE_3D", 1),
        ("STUDIO_INCLUDE_MINIMAX", 6),
    ):
        needs = needs_for_group(flag, env=env)
        assert len(needs) == expected_count, flag
        assert all(n.source == "studio" for n in needs)


# --------------------------------------------------------------------------
# Integración con el tool-belt del agente
# --------------------------------------------------------------------------


def test_belt_exposes_the_new_tools() -> None:
    from comfy_studio.agent.tools import create_default_tool_belt

    names = set(create_default_tool_belt().list_names())
    assert {"list_capabilities", "request_capability", "download_model"}.issubset(names)


def test_request_capability_tool_acquires_and_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El flujo completo de la política, por la herramienta del agente."""
    import asyncio

    from comfy_studio.agent.tools import (
        RequestCapabilityArgs,
        _handle_request_capability,
        create_default_tool_belt,
    )
    from comfy_studio.config import StudioConfig
    from comfy_studio.progress import ProgressBroker
    from comfy_studio.projects import ProjectStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    script = _write_fake_hf(tmp_path, payload=b"pesos")
    os.environ["COMFY_DATA_DIR"] = str(data_dir)
    os.environ["HF_CLI"] = str(script)
    # El motor «preparado» exige un entrypoint real: se simula /opt/ComfyUI.
    app_dir = tmp_path / "ComfyUI"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("# stub\n", encoding="utf-8")
    os.environ["COMFY_APP_DIR"] = str(app_dir)
    store = ProjectStore(str(tmp_path / "studio.db"))
    project = store.create_project("test-capability")
    cfg = StudioConfig.from_env({"STUDIO_DATA_DIR": str(data_dir)})
    context = type(
        "Ctx",
        (),
        {
            "project_id": project["id"],
            "store": store,
            "comfyui_client": None,
            "broker": ProgressBroker(),
            "config": cfg,
        },
    )()

    # Este test cubre el cableado de la herramienta, no la integridad (que tiene
    # tests propios más abajo): se alinea el hash con lo que escribe el fake.
    import comfy_studio.asset_acquire as acquire_mod

    monkeypatch.setattr(acquire_mod, "_verify_integrity", lambda path, need: True)

    result = asyncio.run(
        _handle_request_capability(
            RequestCapabilityArgs(capability="video.face_restore"), context
        )
    )
    assert result["capability"] == "video.face_restore"
    assert result["downloaded"], "debe adquirir los activos que faltaban"
    assert result["selected_template"] == "face_fix"
    # Los activos ya están en disco, así que el estado ya NO es `assets_missing`.
    # Lo que viene después depende del hardware real del host: con GPU el motor
    # queda `ready` y el siguiente paso es encolar; sin GPU (CI, desarrollo) la
    # preparación es `hardware_unavailable` y el estado `engine_unprepared`. El
    # test no puede exigir `ready` en un host sin GPU, así que afirma las dos
    # caras de la relación en vez de una sola.
    prep_state = result["engine_prep"]["state"]
    if prep_state == "ready":
        assert result["state"] == "ready"
        assert "enqueue_render" in result["next_step"]
    else:
        assert result["state"] == "engine_unprepared"
        assert "no está preparado" in result["next_step"]
        assert result["engine_prep"]["hardware"] != "available"

    # Sin acquire: no descarga nada, solo informa.
    again = asyncio.run(
        _handle_request_capability(
            RequestCapabilityArgs(capability="video.face_restore", acquire=False), context
        )
    )
    assert "acquisition" not in again

    # Capacidad desconocida: error tipado, no silencio.
    from comfy_studio.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError, match="no registrada"):
        asyncio.run(
            _handle_request_capability(
                RequestCapabilityArgs(capability="video.nope"), context
            )
        )

    belt = create_default_tool_belt()
    assert "request_capability" in belt.list_names()


# --------------------------------------------------------------------------
# Integridad: revisión fijada y verificación de hash
# --------------------------------------------------------------------------


def test_manifest_assets_carry_revision_and_hash(data_dir: Path) -> None:
    """Cada activo declara la revisión (commit) y el hash que publica HF."""
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    for need in resolution.assets:
        assert need.revision and len(need.revision) == 40, need.filename
        assert need.sha256 or need.sha1, need.filename
        if need.sha256:
            assert len(need.sha256) == 64
        if need.sha1:
            assert len(need.sha1) == 40


def test_acquire_rejects_a_file_whose_hash_does_not_match(
    data_dir: Path, fake_hf: Path
) -> None:
    """Un activo corrupto o sustituido se rechaza, no se instala."""
    resolution = resolve("video.face_restore", env=_env(fake_hf, data_dir))
    need = resolution.missing[0]
    # El hash del manifiesto es el del fichero real de HuggingFace; el fake
    # escribe otro contenido, así que la verificación debe fallar.
    report = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    entry = report.entries[0]
    assert entry.status == "failed"
    assert "integridad" in (entry.error or "")
    assert not Path(need.dest_path).exists(), "no debe dejar el fichero en disco"


def test_acquire_accepts_a_file_whose_hash_matches(data_dir: Path, fake_hf: Path) -> None:
    """Con el hash correcto declarado, la descarga se acepta y se reutiliza."""
    import hashlib

    resolution = resolve("video.face_restore", env=_env(fake_hf, data_dir))
    need = resolution.missing[0]
    need.sha256 = hashlib.sha256(b"pesos-de-mentira").hexdigest()
    need.sha1 = None

    report = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    assert report.entries[0].status == "downloaded"
    assert Path(need.dest_path).is_file()

    # Y la segunda pasada lo reutiliza sin volver a descargar.
    second = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    assert second.entries[0].status == "skipped"


def test_acquire_without_declared_hash_still_works(data_dir: Path, fake_hf: Path) -> None:
    """«Where available»: sin hash se acepta el activo (tamaño ya verificado)."""
    resolution = resolve("video.face_restore", env=_env(fake_hf, data_dir))
    need = resolution.missing[0]
    need.sha256 = None
    need.sha1 = None
    # Sin hash, el tamaño es la única red de seguridad: se alinea con el que
    # escribe el `hf` de mentira para que lo que se compruebe sea la ausencia
    # de hash, no un tamaño que el fake no puede reproducir.
    need.bytes = len(FAKE_CONTENT)
    report = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    assert report.entries[0].status == "downloaded"
    assert Path(need.dest_path).is_file()


# --------------------------------------------------------------------------
# Cancelación durante la descarga (política, punto 4: «cancellation»)
# --------------------------------------------------------------------------


def test_cancel_midway_reports_cancelled_without_retrying(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """Cancelar durante la descarga no es un fallo: no se reintenta.

    El cliente real se cancela en `_run_hf_download`; aquí se comprueba el
    cableado de `acquire`/`_acquire_one`: estado `cancelled`, staging limpio y
    sin backoff (reintentar un cancelado gastaría ancho de banda inútilmente).
    """
    from comfy_studio import asset_acquire

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    _pin_fake_hashes(resolution)

    # El usuario cancela *durante* la primera descarga, no antes de empezar.
    state = {"cancel": False}

    def _user_cancelled() -> bool:
        return state["cancel"]

    def _download_then_cancel(argv: list[str], **_: object) -> int:
        stage = Path(argv[argv.index("--local-dir") + 1])
        target = stage / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        state["cancel"] = True
        return asset_acquire.CANCELLED_RETURNCODE

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_cancel)

    report = asset_acquire.acquire(
        resolution.missing, env=env, should_cancel=_user_cancelled
    )
    assert report.cancelled is True
    cancelled = [e for e in report.entries if e.status == "cancelled"]
    assert cancelled, [e.status for e in report.entries]
    for entry in cancelled:
        assert entry.attempts == 1, "un cancelado no debe reintentarse"
    # El staging queda limpio para que la recuperación no vea un fichero parcial.
    stage = Path(report.stage_dir)
    assert not any(stage.rglob("*.safetensors")), "quedó un parcial en staging"


@pytest.mark.skipif(
    os.name == "nt",
    reason="usa grupos de proceso POSIX, kill(1) y sleep(1); el pod es Linux",
)
def test_real_download_process_is_killed_on_cancel() -> None:
    """El proceso real se mata en ~1 s, con su árbol y sin tocar grupos ajenos."""
    import os
    import subprocess
    import time

    from comfy_studio.asset_acquire import (
        CANCELLED_RETURNCODE,
        TIMEOUT_RETURNCODE,
        _run_hf_download,
        _terminate_tree,
    )

    me = os.getpid()
    slow = [sys.executable, "-c", "import time; time.sleep(60)"]
    t0 = time.monotonic()
    rc = _run_hf_download(slow, timeout=60, should_cancel=lambda: time.monotonic() - t0 > 1.5)
    assert rc == CANCELLED_RETURNCODE
    assert time.monotonic() - t0 < 10, "la cancelación debe surtir efecto de inmediato"

    t0 = time.monotonic()
    assert _run_hf_download(slow, timeout=2, should_cancel=None) == TIMEOUT_RETURNCODE
    assert time.monotonic() - t0 < 10
    assert (
        _run_hf_download(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            timeout=10,
            should_cancel=None,
        )
        == 3
    )

    # Hijo sin sesión propia: muere con el grupo del cliente.
    script = (
        "import subprocess,sys,time\n"
        "p=subprocess.Popen(['sleep','300'])\n"
        "sys.stdout.write(str(p.pid)+chr(10)); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = int(proc.stdout.readline())
    _terminate_tree(proc)
    time.sleep(0.4)
    assert (
        subprocess.run(["kill", "-0", str(child)], capture_output=True).returncode != 0
    ), "quedó un descargador huérfano escribiendo en staging"
    subprocess.run(["kill", "-9", str(child)], capture_output=True)

    # Un proceso del grupo del test NO debe matar al test: `killpg` solo se
    # aplica cuando el cliente liderea su grupo.
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    _terminate_tree(sibling)
    time.sleep(0.3)
    assert subprocess.run(["kill", "-0", str(me)], capture_output=True).returncode == 0


# --------------------------------------------------------------------------
# Filesystems distintos (política, punto 1: «the actual filesystems involved»)
# --------------------------------------------------------------------------


def test_disk_guard_checks_the_destination_filesystem_too(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """Staging y destino pueden estar en volúmenes distintos.

    La guarda miraba solo staging: un destino en otro volumen podía quedarse sin
    espacio cuando el activo ya estaba descargado.
    """
    import shutil

    from comfy_studio import asset_acquire

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    _pin_fake_hashes(resolution)

    real = shutil.disk_usage

    def _fake_disk_usage(path):
        usage = real(path)
        # El staging tiene sitio de sobra; el destino está casi lleno.
        if str(path).endswith(asset_acquire.STAGE_DIRNAME):
            return type(usage)(10**12, 10**12, 0)
        return type(usage)(10, 10, 0)

    monkeypatch.setattr(asset_acquire.shutil, "disk_usage", _fake_disk_usage)
    report = asset_acquire.acquire(resolution.missing, env=env)
    assert report.entries
    assert all(e.status == "failed" for e in report.entries)
    assert any("espacio insuficiente" in (e.error or "") for e in report.entries)


def test_free_bytes_for_takes_the_most_restrictive_filesystem() -> None:
    """Con varios filesystems, manda el que menos sitio tenga."""
    from comfy_studio.asset_acquire import _free_bytes_for

    assert _free_bytes_for([Path("/tmp")]) is not None
    assert _free_bytes_for([]) is None


def test_move_survives_a_cross_device_destination(tmp_path: Path) -> None:
    """`os.replace` falla con EXDEV entre volúmenes: se copia y se borra.

    El EXDEV es del primer `rename` (staging y destino en volúmenes distintos);
    el temporal se crea en el directorio de destino, así que el segundo rename
    sí es válido y el activo se publica completo.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"contenido" * 1000)

    real_replace = real_os.replace
    calls = {"n": 0}

    def _exdev_once(a, b):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    real_os.replace = _exdev_once
    try:
        _move(src, dest)
    finally:
        real_os.replace = real_replace

    assert dest.read_bytes() == b"contenido" * 1000
    assert not src.exists(), "el original debe desaparecer tras copiarlo"
    assert not list(tmp_path.glob("dest.bin.partial-*")), "no queda el temporal"


def test_move_never_degrades_to_a_direct_copy(tmp_path: Path) -> None:
    """R12: si el rename es imposible *siempre*, se falla en lugar de copiar.

    El «último recurso» anterior copiaba directo a `dest` cuando el segundo
    `os.replace` también fallaba: una copia interrumpida dejaba el activo a
    medias bajo su nombre definitivo y el «skip» lo daba por bueno.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"contenido" * 1000)

    real_replace = real_os.replace

    def _always_exdev(a, b):  # noqa: ANN001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    real_os.replace = _always_exdev
    try:
        raised = False
        try:
            _move(src, dest)
        except OSError:
            raised = True
    finally:
        real_os.replace = real_replace

    assert raised, "sin rename no se publica nada"
    assert not dest.exists(), "un parcial con el nombre definitivo es peor que nada"
    assert not list(tmp_path.glob("dest.bin.partial-*")), "el temporal se limpia"
    assert src.is_file(), "la descarga sobrevive para reintentar"


def test_move_still_propagates_real_errors(tmp_path: Path) -> None:
    """EXDEV se tolera; cualquier otro error de `os.replace` debe salir."""
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"x")

    real_replace = real_os.replace

    def _boom(a, b):  # noqa: ANN001
        raise OSError(13, "Permission denied")

    real_os.replace = _boom
    try:
        raised = False
        try:
            _move(src, dest)
        except OSError:
            raised = True
    finally:
        real_os.replace = real_replace
    assert raised, "un error que no es EXDEV no debe degradarse a copia"


# --------------------------------------------------------------------------
# Semántica del hash: el SHA-1 de un blob de Git NO es el SHA-1 crudo
# --------------------------------------------------------------------------


class _Need:
    """Mínimo que consume `_verify_integrity`."""

    def __init__(self, *, sha256: str | None = None, sha1: str | None = None) -> None:
        self.sha256 = sha256
        self.sha1 = sha1


def test_git_blob_sha1_is_not_the_raw_sha1(tmp_path: Path) -> None:
    """Git antepone `blob <tamaño>\\0` antes de hashear.

    Es la trampa clásica: el SHA-1 crudo de los bytes y el OID del blob de git
    son distintos para el mismo contenido.
    """
    import hashlib

    from comfy_studio.asset_acquire import git_blob_sha1

    target = tmp_path / "fichero.bin"
    content = b"hola mundo\n" * 7
    target.write_bytes(content)

    raw = hashlib.sha1(content).hexdigest()
    blob = git_blob_sha1(target)
    assert blob != raw, "si coinciden, la implementación está hasheando en crudo"
    # Referencia independiente: `git hash-object`.
    assert blob == subprocess.run(
        ["git", "hash-object", str(target)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_sha1_entries_verify_as_git_blobs(tmp_path: Path) -> None:
    """Un activo fijado con el SHA-1 del blob de git debe verificarse como tal.

    Antes se calculaba el SHA-1 crudo y se comparaba contra el OID del blob:
    los 29 activos con hash SHA-1 del manifiesto fallaban todos.
    """
    from comfy_studio.asset_acquire import _verify_integrity, git_blob_sha1

    target = tmp_path / "config.json"
    target.write_bytes(b'{"a": 1}\n')

    assert _verify_integrity(target, _Need(sha1=git_blob_sha1(target))) is True
    bad = _verify_integrity(target, _Need(sha1="0" * 40))
    assert isinstance(bad, str)
    assert "blob de git" in bad
    # El error muestra también el SHA-1 crudo, para que un fijado con la
    # semántica equivocada se diagnostique en vez de decir «no coincide».
    assert "SHA-1 crudo" in bad


def test_sha256_entries_verify_as_raw_content(tmp_path: Path) -> None:
    """El oid LFS sí es el SHA-256 crudo del contenido."""
    import hashlib

    from comfy_studio.asset_acquire import _verify_integrity

    target = tmp_path / "model.safetensors"
    target.write_bytes(b"pesos" * 1000)
    raw = hashlib.sha1(b"pesos" * 1000)  # noqa: F841 - solo para dejar claro que no se usa

    ok = _verify_integrity(target, _Need(sha256=hashlib.sha256(b"pesos" * 1000).hexdigest()))
    assert ok is True
    bad = _verify_integrity(target, _Need(sha256="0" * 64))
    assert isinstance(bad, str) and "LFS" in bad


def test_assets_without_a_hash_are_accepted() -> None:
    """«Where available»: un activo sin hash se acepta."""
    import tempfile

    from comfy_studio.asset_acquire import _verify_integrity

    with tempfile.NamedTemporaryFile(suffix=".bin") as handle:
        assert _verify_integrity(Path(handle.name), _Need()) is True


def test_etag_is_the_git_blob_sha1_not_the_raw_one() -> None:
    """Comprobado contra la API de HuggingFace, no contra una suposición.

    Se descarga UN fichero público pequeño y se comparan las tres cosas: el
    `X-Linked-Etag` que publica HuggingFace, el SHA-1 crudo de sus bytes y el
    OID del blob de git. Si la semántica cambiara en HuggingFace, este test
    falla y hay que revisar el verificador.
    """
    import hashlib
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN001
            return None

    url = (
        "https://huggingface.co/stabilityai/sd-vae-ft-mse-original/"
        "resolve/main/README.md"
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="HEAD"), timeout=30) as resp:
            etag = (resp.headers.get("x-linked-etag") or "").strip().strip('"').lower()
    except urllib.error.HTTPError as exc:
        etag = (exc.headers.get("x-linked-etag") or "").strip().strip('"').lower()
    if not etag:
        pytest.skip("HuggingFace no publicó X-Linked-Etag; no se puede verificar")

    with urllib.request.urlopen(url, timeout=30) as resp:
        body = resp.read()
    raw = hashlib.sha1(body).hexdigest()
    blob = hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest()

    assert etag == blob, "el ETag debe ser el OID del blob de git"
    assert etag != raw, "el ETag no es el SHA-1 crudo de los bytes"


# --------------------------------------------------------------------------
# Instalación atómica, limpieza best-effort y cancelación (F7–F14, R12–R16)
#
# Estas pruebas viven aquí, y no en el módulo de producción, porque
# `pyproject.toml` fija `testpaths = ["tests"]` y pytest solo recoge
# `test_*.py`: dentro de `asset_acquire.py` no se ejecutaban en el gate
# documentado (`pytest studio/tests -q`) y un refactor podía reintroducir
# cualquiera de estos defectos con la puerta verde.
# --------------------------------------------------------------------------


def _acquire_need(
    *,
    filename: str = "model.bin",
    dest_path: str = "",
    bytes: int = 0,
    repo_id: str | None = "repo/model",
    remote_path: str | None = "model.bin",
    repo_type: str | None = "model",
    revision: str | None = None,
    sha256: str | None = None,
    sha1: str | None = None,
) -> AssetNeed:
    """Un `AssetNeed` completo: lo que consume `acquire`/`_acquire_one`."""
    return AssetNeed(
        filename=filename,
        dest_path=dest_path,
        bytes=bytes,
        asset_class="task",
        present=False,
        source="core",
        repo_id=repo_id,
        remote_path=remote_path,
        repo_type=repo_type,
        revision=revision,
        sha256=sha256,
        sha1=sha1,
    )


def _fake_download(content: bytes):
    """`_run_hf_download` de mentira: escribe `content` en el stage remoto."""

    def _run(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        target = Path(argv[argv.index("--local-dir") + 1]) / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return 0

    return _run


# --- F7: staging en el directorio de destino y rename atómico ---------------


def test_move_exdev_stages_in_dest_dir_then_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: EXDEV copia a un `.partial-<pid>` del dir de destino y renombra.

    Escribir directo a `dest` dejaría un parcial que el «skip» aceptaría como
    completo; el temporal se borra y `dest` solo aparece ya completo.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"contenido" * 1000)

    real_replace = real_os.replace
    calls = {"n": 0}

    def _exdev_once(a, b):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(real_os, "replace", _exdev_once)
    _move(src, dest)
    assert dest.read_bytes() == b"contenido" * 1000
    assert not src.exists(), "el original debe desaparecer tras la copia"
    assert not list(tmp_path.glob("dest.bin.partial-*")), "no debe quedar el temporal"


def test_move_exdev_leaves_dest_untouched_on_mid_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: un fallo a mitad de la copia EXDEV no crea ni altera `dest`."""
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"contenido" * 1000)

    def _always_exdev(*a, **k):  # noqa: ANN001
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(real_os, "replace", _always_exdev)

    def _boom(a, b):  # noqa: ANN001
        raise OSError("fallo a mitad de copia")

    monkeypatch.setattr(asset_acquire, "_copy_and_fsync", _boom)

    with pytest.raises(OSError):
        _move(src, dest)
    assert not dest.exists(), "un fallo a mitad de copia no debe crear dest"
    assert not list(tmp_path.glob("dest.bin.partial-*")), "el temporal se limpia"
    assert src.is_file(), "la descarga sobrevive para poder reintentar"


def test_move_fails_closed_when_the_rename_is_impossible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: si el rename del temporal también falla, no se copia directo.

    El «último recurso» de antes copiaba a `dest` sin atomicidad: una copia
    abortada a mitad dejaba 1024 de 4096 bytes bajo el nombre definitivo y el
    «skip» de la siguiente pasada lo daba por bueno.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"contenido" * 1000)

    calls: list[Path] = []

    def _replace(a, b):  # noqa: ANN001
        calls.append(Path(b))
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        # ENOSPC al reemplazar (overlayfs/btrfs) o EPERM en FUSE/NFS/SMB.
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(real_os, "replace", _replace)
    with pytest.raises(OSError):
        _move(src, dest)

    assert not dest.exists(), "nada se publica sin rename"
    assert not list(tmp_path.glob("dest.bin.partial-*")), "el temporal se borra"
    assert src.is_file(), "el stageado no se destruye: el fallo es recuperable"
    assert len(calls) == 2, "se reintenta el rename dentro del dir de destino"


def test_move_fsyncs_the_directory_on_the_same_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: el `rename` de toda la vida también fuerza la entrada del directorio.

    Sin `_fsync_dir(dest.parent)`, un corte de luz justo después del
    `os.replace` podía publicar el nombre nuevo sin su contenido.
    """
    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"x" * 32)
    synced: list[Path] = []
    monkeypatch.setattr(asset_acquire, "_fsync_dir", lambda p: synced.append(Path(p)))

    _move(src, dest)

    assert dest.is_file() and not src.exists()
    assert synced == [dest.parent], "el rename común también tiene que fsyncar"


def test_stale_partials_are_swept_before_acquiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: un `<dest>.partial-*` huérfano no sobrevive a la siguiente pasada.

    `cache_policy` solo mira los `dest_path` de los manifiestos y los
    `.hf_stage*`: un temporal de varios GB dejado por un `SIGKILL` no lo borraba
    nadie nunca. Uno de un proceso vivo se respeta: puede ser una copia en
    curso de otro `acquire` concurrente.
    """
    import hashlib

    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)

    # Temporal de un proceso ya terminado: el rastro de un SIGKILL.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    orphan = dest.parent / f"out.bin.partial-{dead.pid}-1"
    orphan.write_bytes(b"0" * 4096)
    # Temporal de este proceso, vivo: en vuelo, no se toca.
    live = dest.parent / f"out.bin.partial-{os.getpid()}-1"
    live.write_bytes(b"0" * 4096)

    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(b"pesos"),
        remote_path="out.bin",
        sha256=hashlib.sha256(b"pesos").hexdigest(),
    )
    monkeypatch.setattr(asset_acquire, "_run_hf_download", _fake_download(b"pesos"))
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )

    assert report.entries[0].status == "downloaded"
    assert not orphan.exists(), "el huérfano debe barrerse antes de descargar"
    assert live.exists(), "un temporal de un proceso vivo puede estar en vuelo"


def test_a_failed_install_is_not_mistaken_for_a_complete_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: el rename imposible deja entrada fallida, y la siguiente pasada reintenta."""
    import errno
    import hashlib
    import os as real_os

    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(b"pesos"),
        remote_path="out.bin",
        sha256=hashlib.sha256(b"pesos").hexdigest(),
    )
    monkeypatch.setattr(asset_acquire, "_run_hf_download", _fake_download(b"pesos"))

    real_replace = real_os.replace
    calls = {"n": 0}

    def _replace(a, b):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device")
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(real_os, "replace", _replace)
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    entry = report.entries[0]
    assert entry.status == "failed", "fallar cerrado, no publicar un activo a medias"
    assert "no se pudo instalar" in (entry.error or "")
    assert not dest.exists()

    # Con el rename operable, la misma necesidad se descarga: el «skip» no ha
    # aceptado el rastro del fallo como activo completo.
    monkeypatch.setattr(real_os, "replace", real_replace)
    attempts = {"n": 0}

    def _count(argv, *, timeout, should_cancel):
        attempts["n"] += 1
        return _fake_download(b"pesos")(argv, timeout=timeout, should_cancel=should_cancel)

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _count)
    second = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    assert attempts["n"] == 1, "un activo que no llegó a instalarse no se salta"
    assert second.entries[0].status == "downloaded"
    assert dest.read_bytes() == b"pesos"


# --- F8: revisión fijada -----------------------------------------------------


def test_acquire_passes_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: la revisión fijada del manifiesto se pasa al cliente (`--revision`)."""
    import hashlib

    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=17,
        remote_path="out.bin",
        revision="a" * 40,
        sha256=hashlib.sha256(b"pesos-de-mentira").hexdigest(),
    )
    captured: dict[str, list[str]] = {}

    def _capture(argv, *, timeout, should_cancel):
        captured["argv"] = list(argv)
        return _fake_download(b"pesos-de-mentira")(
            argv, timeout=timeout, should_cancel=should_cancel
        )

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _capture)
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    assert report.entries[0].status == "downloaded"
    assert "--revision" in captured["argv"], "debe fijar la revisión"
    idx = captured["argv"].index("--revision")
    assert captured["argv"][idx + 1] == "a" * 40


# --- F9: un fallo al instalar se reporta, no aborta -------------------------


def test_acquire_reports_install_failure_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9: un fallo al instalar (mkdir/move/stat) se reporta, no aborta acquire."""
    import hashlib

    stage = tmp_path / "stage"
    dest = tmp_path / "sub" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=17,
        remote_path="out.bin",
        revision="a" * 40,
        sha256=hashlib.sha256(b"pesos-de-mentira").hexdigest(),
    )
    monkeypatch.setattr(asset_acquire, "_run_hf_download", _fake_download(b"pesos-de-mentira"))

    def _boom(src, dest):  # noqa: ANN001
        raise OSError("no se pudo mover")

    monkeypatch.setattr(asset_acquire, "_move", _boom)
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo instalar en" in (entry.error or "")
    assert str(dest) in (entry.error or "")
    assert not list(stage.rglob("*")), "el staging debe quedar limpio tras el fallo"


# --- F10: la guarda de espacio mide el filesystem real ----------------------


def test_free_bytes_walks_to_first_existing_ancestor(tmp_path: Path) -> None:
    """F10: un staging inexistente se mide contra su ancestro existente cercano."""
    import shutil

    from comfy_studio.asset_acquire import _free_bytes

    base = tmp_path / "data"
    base.mkdir()
    deep = base / "models" / asset_acquire.STAGE_DIRNAME
    assert _free_bytes(deep) == shutil.disk_usage(base.resolve()).free


def test_free_bytes_returns_none_when_nothing_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: si ningún ancestro existe devuelve None (la guarda falla cerrada)."""
    from comfy_studio.asset_acquire import _free_bytes

    ghost = tmp_path / "no" / "existe" / "aqui"
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert _free_bytes(ghost) is None


# --- F11: sin hash, el tamaño es la red de seguridad ------------------------


def test_hashless_asset_with_wrong_size_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F11: sin hash, un activo cuyo tamaño no coincide con el declarado se rechaza."""
    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=1000,
        remote_path="out.bin",
        revision="a" * 40,
    )
    monkeypatch.setattr(asset_acquire, "_run_hf_download", _fake_download(b"x" * 500))
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    entry = report.entries[0]
    assert entry.status == "failed"
    assert "tamano" in (entry.error or "")
    assert "500" in (entry.error or "") and "1000" in (entry.error or "")
    assert not dest.exists()


def test_hashless_asset_with_matching_size_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F11: sin hash pero con el tamaño correcto, el activo se acepta."""
    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=500,
        remote_path="out.bin",
        revision="a" * 40,
    )
    monkeypatch.setattr(asset_acquire, "_run_hf_download", _fake_download(b"x" * 500))
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    assert report.entries[0].status == "downloaded"
    assert dest.is_file()


# --- F12/R16: cancelar limpia el `.incomplete` y la caché del cliente -------


def test_cancel_removes_incomplete_and_client_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F12/R16: cancelar limpia el `.incomplete` y la caché, no solo el stage.

    Se cubren las dos rutas que produce el cliente: el `.incomplete` heredado,
    junto al fichero stageado, y el que escribe `huggingface_hub` bajo
    `.cache/huggingface/download/` cuando se descarga con `--local-dir`.
    """
    import hashlib

    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=100,
        remote_path="out.bin",
        revision="a" * 40,
        sha256=hashlib.sha256(b"pesos").hexdigest(),
    )

    def _download_then_cancel(argv, *, timeout, should_cancel):
        token = Path(argv[argv.index("--local-dir") + 1])
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        # Layout heredado.
        target.with_name(target.name + ".incomplete").write_bytes(b"parcial")
        # Layout del cliente actual con `--local-dir`: desde D1 la caché vive
        # bajo el subárbol de esta adquisición.
        downloads = token / ".cache" / "huggingface" / "download" / "repo" / "model"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / (target.name + ".incomplete")).write_bytes(b"parcial")
        (token / ".cache" / "x").write_bytes(b"cache")
        return asset_acquire.CANCELLED_RETURNCODE

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_cancel)
    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        should_cancel=lambda: False,
    )
    assert report.entries[0].status == "cancelled"
    leftovers = [p.name for p in stage.rglob("*")]
    assert leftovers == [], f"quedaron restos en staging: {leftovers}"


def test_remove_partials_only_targets_this_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R16: el barrido del `.cache` es por nombre, no un `rmtree` a ciegas."""
    from comfy_studio.asset_acquire import _remove_partials

    stage = tmp_path / "stage"
    staged = stage / "sub" / "model.bin"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"parcial")
    legacy = staged.with_name("model.bin.incomplete")
    legacy.write_bytes(b"parcial")
    downloads = stage / ".cache" / "huggingface" / "download" / "repo" / "model"
    downloads.mkdir(parents=True)
    nested = downloads / "model.bin.incomplete"
    nested.write_bytes(b"parcial")
    other = downloads / "otro.bin.incomplete"
    other.write_bytes(b"parcial")

    _remove_partials(staged, stage)

    assert not staged.exists() and not legacy.exists() and not nested.exists()
    assert other.exists(), "el `.incomplete` de otro activo no se toca"


def test_a_cache_that_cannot_be_dropped_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R16: si `.cache` sobrevive, el informe no puede dar el staging por limpio."""
    import logging

    from comfy_studio.asset_acquire import _drop_client_cache

    stage = tmp_path / "stage"
    cache = stage / ".cache"
    cache.mkdir(parents=True)
    (cache / "x").write_bytes(b"cache")

    def _boom(path):  # noqa: ANN001
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(asset_acquire.shutil, "rmtree", _boom)
    with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
        _drop_client_cache(stage)

    assert cache.is_dir(), "el borrado falló: eso es lo que hay que decir en voz alta"
    assert any("caché" in r.getMessage() for r in caplog.records), caplog.text


def test_cancel_with_a_surviving_cache_still_reports_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R13/R16: la limpieza que no se puede hacer avisa, no aborta ni miente."""
    import hashlib
    import logging

    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=100,
        remote_path="out.bin",
        revision="a" * 40,
        sha256=hashlib.sha256(b"pesos").hexdigest(),
    )

    def _download_then_cancel(argv, *, timeout, should_cancel):
        token = Path(argv[argv.index("--local-dir") + 1])
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        cache = token / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "x").write_bytes(b"cache")
        cache.chmod(0o500)  # el árbol no se puede borrar
        return asset_acquire.CANCELLED_RETURNCODE

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_cancel)
    try:
        with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
            report = asset_acquire.acquire(
                [need],
                env={"HF_DISK_GUARD": "0"},
                hf_cli="/fake/hf",
                stage_dir=stage,
                should_cancel=lambda: False,
            )
    finally:
        for cache in stage.rglob(".cache"):
            cache.chmod(0o700)

    assert report.cancelled is True
    assert report.entries[0].status == "cancelled", "la cancelación se reporta igual"
    assert list(stage.rglob(".cache")), "la caché sigue ahí"
    assert any("caché" in r.getMessage() for r in caplog.records), caplog.text


def test_an_unremovable_partial_still_yields_a_report_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R13: un staging sin permiso de escritura no convierte el cancelado en excepción.

    `_remove_partials` se llama desde dentro del camino de cancelación: si
    propagara, el `PermissionError` escapaba de `_acquire_one` y de `acquire()`
    sin informe, y las necesidades restantes no se intentaban jamás.
    """
    import hashlib

    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=100,
        remote_path="out.bin",
        revision="a" * 40,
        sha256=hashlib.sha256(b"pesos").hexdigest(),
    )

    def _download_then_lock(argv, *, timeout, should_cancel):
        token = Path(argv[argv.index("--local-dir") + 1])
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        target.with_name(target.name + ".incomplete").write_bytes(b"parcial")
        # D1: el stageado vive en el subárbol de la adquisición, así que es ese
        # directorio el que hay que dejar sin escritura.
        token.chmod(0o500)  # ni el parcial ni su `.incomplete` se pueden borrar
        return asset_acquire.CANCELLED_RETURNCODE

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_lock)
    try:
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            should_cancel=lambda: False,
        )
    finally:
        for token in stage.glob("job-*"):
            token.chmod(0o700)

    entry = report.entries[0]
    assert entry.status == "cancelled", "hay informe, no una excepción"
    assert entry.filename == "out.bin"
    assert report.cancelled is True
    # El parcial sigue en disco porque no se pudo borrar; lo importante es que
    # el destino sigue vacío y la adquisición no abortó.
    assert [p for p in stage.rglob("out.bin")], "el stageado no se pudo borrar"
    assert not dest.exists()


# --- F13: `remote_path` con `..` se rechaza ----------------------------------


def test_remote_path_traversal_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F13: un `remote_path` con `..` se rechaza; nada fuera de staging se toca."""
    stage = tmp_path / "stage"
    outside = tmp_path / "secreto.bin"
    outside.write_bytes(b"no me borres")
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=100,
        remote_path="../secreto.bin",
        revision="a" * 40,
    )

    def _never(argv, *, timeout, should_cancel):
        raise AssertionError("no debe ejecutarse la descarga")

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _never)
    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    entry = report.entries[0]
    assert entry.status == "failed"
    assert "remote_path inseguro" in (entry.error or "")
    assert outside.is_file(), "no debe borrarse nada fuera de staging"


# --- F14: la cancelación se ve en el informe --------------------------------


def test_cancel_on_last_entry_sets_report_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F14: aunque la cancelación caiga en la última entrada, cancelled es True."""
    import hashlib

    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=100,
        remote_path="out.bin",
        revision="a" * 40,
        sha256=hashlib.sha256(b"pesos").hexdigest(),
    )

    def _cancel(argv, *, timeout, should_cancel):
        return asset_acquire.CANCELLED_RETURNCODE

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _cancel)
    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        should_cancel=lambda: False,
    )
    assert report.cancelled is True
    assert report.entries[0].status == "cancelled"


def test_midstream_cancel_reports_remaining_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F14: las necesidades descartadas por una cancelación a mitad no desaparecen."""
    import hashlib

    stage = tmp_path / "stage"
    digest = hashlib.sha256(b"pesos").hexdigest()
    needs = [
        _acquire_need(
            filename=f"m{i}.bin",
            dest_path=str(tmp_path / f"m{i}.bin"),
            bytes=100,
            remote_path=f"m{i}.bin",
            revision="a" * 40,
            sha256=digest,
        )
        for i in range(3)
    ]

    state = {"n": 0}

    def _download(argv, *, timeout, should_cancel):
        state["n"] += 1
        if state["n"] == 1:
            return asset_acquire.CANCELLED_RETURNCODE
        return _fake_download(b"pesos")(argv, timeout=timeout, should_cancel=should_cancel)

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download)

    cancel_calls = {"n": 0}

    def _should_cancel() -> bool:
        cancel_calls["n"] += 1
        return cancel_calls["n"] > 1  # False en la 1a pasada, True después

    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        should_cancel=_should_cancel,
    )
    assert report.cancelled is True
    statuses = {e.filename: e.status for e in report.entries}
    assert set(statuses) == {"m0.bin", "m1.bin", "m2.bin"}, "ninguna necesidad desaparece"
    assert statuses["m0.bin"] == "cancelled"
    assert statuses["m1.bin"] == "skipped"
    assert statuses["m2.bin"] == "skipped"


def test_cancel_on_the_first_poll_reports_every_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R14: una cancelación instantánea no borra las necesidades del informe.

    `_acquisition_cancel_hook` sondea el estado persistido del job, así que un
    job cancelado justo cuando arranca `request_capability` cancela en la
    primera consulta. Con la guarda anterior el informe se quedaba sin
    entradas: `downloaded` y `failed` vacíos, y el agente recibía «revisa
    `failed`» sin lista y sin pista de la cancelación.
    """
    import hashlib

    stage = tmp_path / "stage"
    digest = hashlib.sha256(b"pesos").hexdigest()
    needs = [
        _acquire_need(
            filename=f"m{i}.bin",
            dest_path=str(tmp_path / f"m{i}.bin"),
            bytes=100,
            remote_path=f"m{i}.bin",
            revision="a" * 40,
            sha256=digest,
        )
        for i in range(3)
    ]

    def _never(argv, *, timeout, should_cancel):
        raise AssertionError("cancelado antes de empezar: no debe descargarse nada")

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _never)
    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        should_cancel=lambda: True,
    )

    assert report.cancelled is True
    statuses = {e.filename: e.status for e in report.entries}
    assert statuses == {f"m{i}.bin": "skipped" for i in range(3)}
    public = report.to_public_dict()
    assert [e["filename"] for e in public["entries"]] == [f"m{i}.bin" for i in range(3)]
    assert public["failed"] == []
    assert public["cancelled"] is True
    assert not any(Path(n.dest_path).exists() for n in needs)


# W01 (plan Ultra) — regresiones de los 3 defectos P0 de integridad
# Estas pruebas FALLAN en el código previo al fix y PASAN tras él.
# --------------------------------------------------------------------------


def test_w01_preexisting_corrupt_file_is_not_skipped_as_present(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """P0 #1: un fichero existente con tamaño/hash incorrectos no es «present».

    El skip inicial aceptaba cualquier fichero no vacío. Un parcial de 7 bytes
    frente a 26 esperados se anunciaba como adquirido. Ahora se verifica tamaño
    e integridad antes de hacer skip; si no cuadra, se re-descarga.
    """
    import hashlib

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    need = resolution.missing[0]
    # El manifiesto espera el hash del contenido real del fake-hf.
    need.sha256 = hashlib.sha256(FAKE_CONTENT).hexdigest()
    need.sha1 = None
    need.bytes = len(FAKE_CONTENT)

    # Sembramos un fichero CORRUPTO preexistente en el destino (no vacío, pero
    # con otro contenido y otro tamaño).
    dest = Path(need.dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"corrupto")  # 8 bytes != len(FAKE_CONTENT)

    report = asset_acquire.acquire([need], env=env)
    entry = report.entries[0]
    # No debe quedar como «skipped» (eso sería aceptar el corrupto).
    assert entry.status != "skipped", "un corrupto preexistente no es present"
    # Se re-descargó y ahora el contenido es el correcto, verificado.
    assert entry.status == "downloaded"
    assert dest.read_bytes() == FAKE_CONTENT


def test_w01_download_argv_includes_pinned_revision(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """P0 #2: `hf download` recibe --revision cuando el manifiesto lo declara.

    Omitir el pin baja el contenido de `main`, que puede diferir del hash
    fijado. La prueba captura el argv real que se pasa al cliente.
    """
    import hashlib

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    need = resolution.missing[0]
    need.sha256 = hashlib.sha256(FAKE_CONTENT).hexdigest()
    need.sha1 = None
    need.revision = "a" * 40  # revisión fijada del manifiesto

    captured: dict[str, list[str]] = {}

    real = asset_acquire._run_hf_download

    def _capture(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _capture)
    asset_acquire.acquire([need], env=env)

    argv = captured.get("argv", [])
    assert "--revision" in argv, f"el pin no llegó al CLI: {argv}"
    assert argv[argv.index("--revision") + 1] == "a" * 40


def test_w01_exdev_failure_preserves_previous_destination(tmp_path: Path) -> None:
    """P0 #3: si la copia EXDEV falla a mitad, el destino previo sobrevive.

    El fallback antiguo escribía directo sobre `dest`: una copia interrumpida
    dejaba un parcial y destruía la versión válida anterior. Ahora copia a un
    temporal same-fs y hace replace atómico; un fallo no toca `dest`.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"nuevo-contenido" * 1000)
    previous = b"version-previa-valida" * 500
    dest.write_bytes(previous)  # destino previo que NO debe perderse

    real_replace = real_os.replace
    calls = {"n": 0}

    def _replace_exdev_then_fail(a, b):  # noqa: ANN001
        # 1ª llamada: el os.replace(src, dest) del happy path -> EXDEV.
        # 2ª llamada: el os.replace(tmp, dest) del fallback -> explota.
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        raise OSError(errno.ENOSPC, "No space left on device")

    real_os.replace = _replace_exdev_then_fail
    try:
        raised = False
        try:
            _move(src, dest)
        except OSError:
            raised = True
    finally:
        real_os.replace = real_replace

    assert raised, "un fallo a mitad de la publicación debe propagarse"
    # El destino previo sigue intacto: no se publicó un parcial.
    assert dest.read_bytes() == previous, "se destruyó/corrompió el destino previo"
    # No quedó ningún temporal en el directorio.
    leftovers = [p for p in dest.parent.iterdir() if p.name.startswith(f".{dest.name}.incoming")]
    assert leftovers == [], f"quedó un temporal sin limpiar: {leftovers}"


def test_w01_exdev_success_is_atomic_and_cleans_up(tmp_path: Path) -> None:
    """La ruta EXDEV feliz publica el contenido nuevo y borra origen/temporal."""
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    new_content = b"contenido-nuevo" * 1000
    src.write_bytes(new_content)

    real_replace = real_os.replace

    def _exdev_once_then_real(a, b):  # noqa: ANN001
        # Solo el primer replace(src,dest) finge EXDEV; el replace(tmp,dest) real.
        if Path(a) == src:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)

    real_os.replace = _exdev_once_then_real
    try:
        _move(src, dest)
    finally:
        real_os.replace = real_replace

    assert dest.read_bytes() == new_content
    assert not src.exists(), "el origen debe desaparecer tras publicar"
    leftovers = [p for p in dest.parent.iterdir() if ".incoming" in p.name]
    assert leftovers == [], "no debe quedar temporal tras una publicación correcta"
