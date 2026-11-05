"""Tests del modelo de coste de sesión (política, punto 6).

«Optimize the cost of producing an acceptable deliverable, including startup,
transfers, downloads, inference, evaluation, storage and API usage.»

Lo que estos tests fijan es lo que suele ignorarse: la GPU se factura mientras
se descarga.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comfy_studio.projects import TERMINAL_STATUSES

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "storage_requirement.py"


def _run(args: list[str]) -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--cost", "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    return json.loads(out.stdout)


def test_cost_model_charges_the_gpu_while_downloading() -> None:
    """El coste no es solo GPU x horas: la descarga consume GPU facturada."""
    report = _run(["--profile", "studio", "--hours", "3"])
    cost = report["cost"]
    assert cost["model_bytes"] > 150e9, "el perfil studio arrastra >150 GB de modelos"
    assert cost["download_hours"] > 0
    assert cost["gpu_idle_cost"] > 0
    assert cost["productive_hours"] < cost["hours"]
    # El total incluye GPU + disco + transferencia.
    expected = cost["gpu_cost"] + cost["disk_cost"] + cost["transfer_cost"]
    assert abs(cost["total_cost"] - expected) < 0.01


def test_cost_per_productive_hour_exceeds_cost_per_hour() -> None:
    """La métrica honesta es el coste por hora productiva, que es mayor."""
    report = _run(["--profile", "studio", "--hours", "3"])
    cost = report["cost"]
    assert cost["cost_per_productive_hour"] > cost["gpu_cost"] / cost["hours"]


def test_image_is_transferred_even_when_not_on_the_volume() -> None:
    """La imagen se transfiere en cada arranque en frío, aunque no ocupe el volumen."""
    report = _run(["--profile", "studio"])
    cost = report["cost"]
    assert report["image_on_volume"] is False
    assert cost["image_bytes"] > 10e9
    assert cost["transfer_bytes"] == cost["image_bytes"] + cost["model_bytes"]
    assert cost["cold_start_hours"] > 0


def test_faster_link_reduces_idle_gpu_cost() -> None:
    """Más ancho de bajada = menos GPU facturada sin producir."""
    slow = _run(["--profile", "studio", "--hours", "3", "--inet-down-mbps", "100"])["cost"]
    fast = _run(["--profile", "studio", "--hours", "3", "--inet-down-mbps", "5000"])["cost"]
    assert fast["download_hours"] < slow["download_hours"]
    assert fast["gpu_idle_cost"] < slow["gpu_idle_cost"]
    assert fast["productive_hours"] > slow["productive_hours"]


def test_short_session_is_dominated_by_transfer() -> None:
    """En una sesión muy corta, la transferencia manda: no compensa «ahorrar» GPU."""
    short = _run(["--profile", "studio", "--hours", "0.5"])["cost"]
    assert short["productive_hours"] < short["hours"] / 2
    assert short["cost_per_productive_hour"] > short["total_cost"] / short["hours"]


def test_minimal_profile_has_no_model_transfer() -> None:
    """`minimal` no arrastra la Fase 3: su coste de transferencia es solo la imagen."""
    report = _run(["--profile", "minimal", "--hours", "1"])
    cost = report["cost"]
    assert cost["model_bytes"] < 5e9
    assert cost["transfer_bytes"] == cost["image_bytes"] + cost["model_bytes"]


def test_cost_is_optional_in_the_cli() -> None:
    """Sin `--cost`, la salida JSON no lo incluye."""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--profile", "studio", "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    payload = json.loads(out.stdout)
    assert "cost" not in payload


# --------------------------------------------------------------------------
# Puerta de arranque (S5): se deriva de lo que `--bootstrap` baja de verdad
# --------------------------------------------------------------------------

PROFILES = ("all", "general", "realistic", "anime", "minimal", "studio", "unreal")
GiB = 1024**3
GB = 1000**3


def _core_manifest() -> dict:
    return json.loads((REPO_ROOT / "config" / "models-core.json").read_text(encoding="utf-8"))


def _run_raw(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )


def test_start_gate_ignores_the_dead_phase1_phase2_bootstrap() -> None:
    """El arranque ya no baja la Fase 1 + Fase 2: la puerta no puede cobrarlas.

    `PROFILE_BOOTSTRAP_BYTES` sumaba 2.79 + 60.56 GiB por perfil, así que un pod
    con 100 GB libres era rechazado por una descarga que `--bootstrap` ya no
    hace (hoy baja 0 bytes: el manifiesto core no declara ninguna entrada de
    clase `bootstrap`).
    """
    report = _run(["--profile", "all"])
    components = report["components"]
    assert "bootstrap_phase1_2" not in components, "el componente viejo debe desaparecer"
    assert "bootstrap" not in components, "el nombre colisiona con la clase del manifiesto"
    assert components["startup_download"] == 0
    assert report["start_gate_bytes"] == (
        components["startup_download"]
        + report["boot_staging_bytes"]
        + components["download_margin"]
    )


def test_the_gate_identity_holds_for_every_profile() -> None:
    """La puerta es una suma explícita por componente, no una tabla por perfil.

    Si alguien reintroduce los literales de Fase 1 + Fase 2 (`all` = 2.79 + 60.56
    GiB), `startup_download` deja de ser 0 y la identidad se rompe: el test falla
    antes de que la puerta llegue a `strict_min_disk_gb`.
    """
    for profile in PROFILES:
        report = _run(["--profile", profile])
        components = report["components"]
        # `--bootstrap` ejecuta el mismo plan para todos los perfiles.
        assert components["startup_download"] == 0, profile
        gate = components["startup_download"] + report["boot_staging_bytes"]
        gate += components["download_margin"]
        assert report["start_gate_bytes"] == gate, profile
        assert report["start_gate_gb"] == -(-gate // GB), profile


def test_bootstrap_bytes_are_derived_from_the_core_manifest(tmp_path: Path) -> None:
    """Si el core declara una entrada `bootstrap`, la puerta la cobra tal cual.

    Al revés también: un `task` del core NO es arranque, es adquisición a
    demanda, y solo cuenta en el pico de staging.
    """
    core = tmp_path / "models-core.json"
    core.write_text(
        json.dumps(
            {
                "models": [
                    {"class": "task", "bytes": 5 * GiB, "profiles": ["all"]},
                    {"class": "bootstrap", "bytes": 3 * GiB},
                    {"class": "task", "bytes": 4 * GiB, "profiles": ["all"]},
                    {"class": "task", "bytes": 9 * GiB, "profiles": ["studio"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = json.loads(
        _run_raw(["--profile", "all", "--core-manifest", str(core), "--json"]).stdout
    )
    components = report["components"]
    assert components["startup_download"] == 3 * GiB
    # El pico del volumen es el archivo más grande que `all` puede pedir, no el
    # del perfil ajeno.
    assert components["staging_peak"] == 2 * 5 * GiB
    # La puerta cobra el pico de ARRANQUE (la entrada `bootstrap` más grande
    # coexistiendo con su `.incomplete`), no el pico a demanda: ese ocurre
    # después, cuando una capacidad lo pide.
    assert report["boot_staging_bytes"] == 2 * 3 * GiB
    assert report["start_gate_bytes"] == 3 * GiB + 2 * 3 * GiB + 10 * GiB


def test_staging_peak_comes_from_the_core_manifest_profiles() -> None:
    """El pico de staging es el archivo más grande que ESE perfil puede pedir."""
    core = _core_manifest()
    minimal = _run(["--profile", "minimal"])["components"]["staging_peak"]
    every = _run(["--profile", "all"])["components"]["staging_peak"]
    largest_all = max(
        int(m["bytes"]) for m in core["models"] if "all" in (m.get("profiles") or [])
    )
    assert every == 2 * largest_all
    assert minimal < every, "`minimal` no arrastra el checkpoint de la Fase 2"
    assert minimal == 2 * 554_253_681, "inswapper_128.onnx, derivado del manifiesto"


def test_every_profile_gate_fits_a_100_gb_pod(tmp_path: Path) -> None:
    """El caso real que destapó el finding: 100 GB libres deben bastar.

    S5 usa `start_gate_gb` como `strict_min_disk_gb` y falla de forma fatal bajo
    STRICT_START=1, así que una puerta inflada deja el pod sin arrancar por una
    descarga que no ocurre.
    """
    out = tmp_path / "storage-requirement.json"
    _run_raw(["--all-profiles", "--out", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload["profiles"]) == set(PROFILES)
    for profile, entry in payload["profiles"].items():
        assert entry["start_gate_gb"] > 0, profile
        assert entry["start_gate_gb"] <= 100, (
            f"{profile}: puerta de {entry['start_gate_gb']} GB para un arranque "
            "que no descarga nada"
        )


def test_check_still_passes_for_the_blocking_profile() -> None:
    """`--check` es el cortacircuitos de CI: debe seguir funcionando."""
    result = _run_raw(["--profile", "studio", "--check"])
    assert result.returncode == 0, result.stderr
    assert "INSUFICIENTE" not in result.stdout


def test_check_reports_a_gate_that_a_threshold_cannot_cover() -> None:
    """Un umbral por debajo de la puerta debe seguir haciendo fallar `--check`."""
    result = _run_raw(["--profile", "minimal", "--check"])
    assert result.returncode == 1
    assert "FALLO: S5/minimal=10 GB" in result.stderr


def test_missing_core_manifest_fails_closed() -> None:
    """Sin el manifiesto core no se puede derivar la puerta: mejor no adivinar."""
    result = _run_raw(["--core-manifest", "/no/existe/models-core.json", "--json"])
    assert result.returncode == 2
    assert "manifiesto core" in result.stderr


# --------------------------------------------------------------------------
# Override del catálogo core (R32): los tres consumidores deben derivar del
# mismo manifiesto
# --------------------------------------------------------------------------


def _run_env(args: list[str], env: dict[str, str] | None = None) -> dict:
    """Como `_run`, pero con `STUDIO_CORE_MANIFEST` bajo control del test."""
    environment = {k: v for k, v in os.environ.items() if k != "STUDIO_CORE_MANIFEST"}
    environment.update(env or {})
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=environment,
    )
    return json.loads(out.stdout)


def _core_with_bootstrap(tmp_path: Path, bytes_: int) -> Path:
    path = tmp_path / f"models-core-{bytes_}.json"
    path.write_text(
        json.dumps({"models": [{"class": "bootstrap", "bytes": bytes_}]}),
        encoding="utf-8",
    )
    return path


def test_studio_core_manifest_override_is_honoured(tmp_path: Path) -> None:
    """R32: sin leer la variable, el requisito salía de otro catálogo.

    `download-models.sh` y `capabilities.core_manifest_path()` ya leían
    `STUDIO_CORE_MANIFEST`; este script no, así que un despliegue que apuntaba el
    catálogo core a otro sitio derivaba el disco de uno y descargaba del otro.
    """
    alt = _core_with_bootstrap(tmp_path, 7 * GiB)
    sin_override = _run_env(["--profile", "minimal"])["components"]["startup_download"]
    con_override = _run_env(
        ["--profile", "minimal"], {"STUDIO_CORE_MANIFEST": str(alt)}
    )["components"]["startup_download"]
    assert con_override == 7 * GiB
    assert sin_override == 0, "el catálogo del árbol no declara bootstrap"


def test_core_manifest_override_expands_the_data_dir_placeholder(tmp_path: Path) -> None:
    """El override se expande con la misma regla que el resolvedor de Fase 3."""
    alt = _core_with_bootstrap(tmp_path, 7 * GiB)
    report = _run_env(
        ["--profile", "minimal"],
        {"STUDIO_CORE_MANIFEST": "{COMFY_DATA_DIR}/models-core-7516192768.json",
         "COMFY_DATA_DIR": str(tmp_path)},
    )
    assert report["components"]["startup_download"] == 7 * GiB
    assert alt.is_file()


def test_explicit_core_manifest_still_wins_over_the_env(tmp_path: Path) -> None:
    """`--core-manifest` es lo más explícito: mantener su precedencia."""
    explicit = _core_with_bootstrap(tmp_path, 3 * GiB)
    desde_entorno = _core_with_bootstrap(tmp_path, 9 * GiB)
    report = _run_env(
        ["--profile", "minimal", "--core-manifest", str(explicit)],
        {"STUDIO_CORE_MANIFEST": str(desde_entorno)},
    )
    assert report["components"]["startup_download"] == 3 * GiB


def test_unresolvable_override_falls_back_like_the_other_consumers() -> None:
    """Un override que no existe no aborta: `download-models.sh` y el resolvedor
    tampoco lo hacen, y abortar aquí no arreglaría el desacuerdo."""
    report = _run_env(["--profile", "minimal"], {"STUDIO_CORE_MANIFEST": "/no/existe/core.json"})
    assert report["components"]["startup_download"] == 0


def test_placeholder_expansion_matches_the_resolver_helper(monkeypatch) -> None:
    """La expansión local no puede divergir de `model_assets.expand_placeholders`.

    Se comprueba con el helper importable y sin él (caso de la imagen), porque
    el script delega en uno u otro según dónde se ejecute.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import storage_requirement

    from comfy_studio import model_assets

    entornos = (
        {},
        {"COMFY_APP_DIR": "/opt/otra", "COMFY_DATA_DIR": "/datos/otro"},
        {"COMFY_DATA_DIR": ""},
    )
    valores = (
        "{COMFY_DATA_DIR}/models-core.json",
        "{COMFY_APP_DIR}/core.json",
        "config/models-core.json",
    )
    for env in entornos:
        for raw in valores:
            assert storage_requirement._expand_placeholders(raw, env) == (
                model_assets.expand_placeholders(raw, env)
            ), (raw, env)

    monkeypatch.setitem(sys.modules, "comfy_studio", None)
    monkeypatch.setitem(sys.modules, "comfy_studio.model_assets", None)
    for env in entornos:
        for raw in valores:
            assert storage_requirement._expand_placeholders(raw, env) == (
                model_assets.expand_placeholders(raw, env)
            ), (raw, env)


def test_core_manifest_override_is_documented() -> None:
    """R33: la variable existe en el código; tiene que existir en la política."""
    doc = (REPO_ROOT / "docs" / "POLITICA_ARQUITECTURA_RECURSOS.md").read_text(
        encoding="utf-8"
    )
    seccion = doc.split("## 4.", 1)[1].split("\n## ", 1)[0]
    assert "STUDIO_CORE_MANIFEST" in seccion
    assert "STUDIO_MANIFEST" in seccion
    assert "Fase 3" in seccion, "la política debe decir de qué catálogo es cada una"


def test_cost_model_counts_the_startup_download() -> None:
    """El renombre no puede perder bytes: `model_bytes` sigue sumando el arranque."""
    report = _run(["--profile", "studio"])
    components = report["components"]
    assert report["cost"]["model_bytes"] == (
        components["startup_download"] + components["task_models"]
    )


# --------------------------------------------------------------------------
# Liberación de VRAM cuando no queda trabajo (política, puntos 3 y 5)
# --------------------------------------------------------------------------


#: El doble NO tiene vocabulario propio: aplica la MISMA regla que el store
#: real (`ProjectStore.has_pending_work`), que es una lista blanca de estados
#: TERMINALES. La versión anterior enumeraba los estados en vuelo («lista
#: negra») y afirmaba en el comentario que coincidía con el contrato real: no
#: coincidía. Un estado fuera de las dos listas —`failed`, o uno que nadie
#: enumeró— era «en vuelo» para el store y «no pendiente» para el stub, así que
#: estos tests podían pasar (o fallar) por el motivo equivocado. Importando la
#: tupla del store, la deriva es imposible por construcción.
class _StoreStub:
    """Store mínimo que responde la consulta de existencia del contrato R3."""

    def __init__(self, jobs: list[dict]) -> None:
        self._jobs = jobs

    def list_jobs(self, **_kwargs):  # noqa: ANN001
        return self._jobs

    def has_pending_work(self, exclude_job_id: str | None = None) -> bool:
        for job in self._jobs:
            if exclude_job_id is not None and job.get("id") == exclude_job_id:
                continue
            if job.get("status") not in TERMINAL_STATUSES:
                return True
        return False


class _ClientStub:
    def __init__(self, jobs: list[dict]) -> None:
        self._store = _StoreStub(jobs)
        self.calls: list[dict] = []

    async def free_memory(self, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        return {"freed": True}


@pytest.mark.anyio
async def test_gpu_is_released_when_no_work_remains() -> None:
    """«Do not keep every engine or model resident in VRAM merely to describe the
    studio as ready.» `POST /free` existía pero nunca se llamaba."""

    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([{"status": "done"}])
    client._unload_when_idle = True

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {"freed": True}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [{"unload_models": True, "free_memory": True}]


@pytest.mark.anyio
async def test_gpu_is_not_released_while_work_is_pending() -> None:
    """Liberar con la cola llena penalizaría el rendimiento sin ganar nada."""
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([{"status": "done"}, {"status": "running"}])
    client._unload_when_idle = True

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == []
    # La razón es la respuesta del store, no un fallo del camino: sin el guard
    # este test tendría que pasar a llamar a `/free`.
    assert client._store.has_pending_work() is True


@pytest.mark.anyio
async def test_the_finished_render_does_not_count_as_pending_work() -> None:
    """R47: el guard excluye el job que acaba de terminar.

    Sin la exclusión, el render recién terminado (todavía no terminal en el
    store) mantendría `has_pending_work` en True para siempre y no se
    descargaría nunca nada. El stub replica el contrato real, así que la
    exclusión se prueba de punta a punta.
    """
    from comfy_studio.comfyui_client import ComfyUIClient

    store = _StoreStub([{"id": "job-1", "status": "running"}, {"status": "done"}])
    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = store
    client._unload_when_idle = True

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {"freed": True}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [], "el job en vuelo no se excluyó: no se descargó nada"

    await ComfyUIClient._release_gpu_when_idle(client, exclude_job_id="job-1")
    assert calls == [{"unload_models": True, "free_memory": True}]


@pytest.mark.anyio
async def test_a_fetching_job_still_counts_as_pending_work() -> None:
    """R51: «fetching» es un job en vuelo (recovery.py, routers/core.py)."""
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub(
        [{"id": "job-1", "status": "running"}, {"id": "job-2", "status": "fetching"}]
    )
    client._unload_when_idle = True

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {"freed": True}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client, exclude_job_id="job-1")
    assert calls == []


def test_the_store_stub_answers_what_the_real_store_answers(tmp_path: Path) -> None:
    """R58: el doble aplica la regla del store, no una parecida.

    El store real es una lista blanca de estados terminales; el stub era una
    lista negra de estados en vuelo. Para un estado fuera de las dos listas las
    respuestas eran opuestas, y ningún test lo cruzaba: la diferencia estaba a
    un estado nuevo de distancia de esconder un bug de la clase R51. Aquí se
    recorre el vocabulario real —terminales, en vuelo y uno que nadie enumeró—
    y se exige que el doble y el store den la misma respuesta.
    """
    from comfy_studio.projects import ProjectStore

    statuses = [
        *TERMINAL_STATUSES,
        "queued",
        "running",
        "cancelling",
        "fetching",
        "estado-que-nadie-enumero",
    ]
    for index, status in enumerate(statuses):
        store = ProjectStore(tmp_path / f"vocabulario-{index}")
        project = store.create_project(name="Vocabulario")
        job = store.create_job(
            kind="render", project_id=project["id"], template_id="wan_t2v", status=status
        )
        stub_answer = _StoreStub([{"id": job["id"], "status": status}]).has_pending_work()
        assert stub_answer is store.has_pending_work(), (
            f"el stub y el store discrepan sobre {status!r}"
        )

    # Y la partición es la que dice el store: los terminales no cuentan.
    for status in TERMINAL_STATUSES:
        assert _StoreStub([{"status": status}]).has_pending_work() is False, (
            f"{status!r} es terminal para el store pero en vuelo para el stub"
        )


@pytest.mark.anyio
async def test_release_can_be_disabled() -> None:
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([])
    client._unload_when_idle = False

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == []


@pytest.mark.anyio
async def test_release_failure_never_breaks_a_finished_render() -> None:
    """Liberar VRAM es una optimización: no puede tumbar un render ya persistido."""
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([{"status": "done"}])
    client._unload_when_idle = True

    async def _boom(**kwargs):  # noqa: ANN001
        raise RuntimeError("ComfyUI no responde")

    client.free_memory = _boom
    await ComfyUIClient._release_gpu_when_idle(client)  # no lanza


def test_unload_flag_is_configurable() -> None:
    from comfy_studio.config import StudioConfig

    default = StudioConfig.from_env({})
    assert default.unload_models_when_idle is True
    off = StudioConfig.from_env({"STUDIO_UNLOAD_WHEN_IDLE": "0"})
    assert off.unload_models_when_idle is False
    assert "STUDIO_UNLOAD_WHEN_IDLE" in off.to_public_dict()


# --------------------------------------------------------------------------
# Paridad perfiles <-> manifiesto (política, punto 7)
# --------------------------------------------------------------------------


def test_profile_parity_holds() -> None:
    """La selección por perfil es dato versionado, no un `case` en bash."""
    import subprocess

    r = subprocess.run(
        [sys.executable, "scripts/check_profile_parity.py"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "perfiles coincidentes" in r.stdout


def test_profile_parity_detects_a_divergence(tmp_path: Path) -> None:
    """Si el bash y el manifiesto se separan, el checker falla con el detalle."""

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_profile_parity import manifest_plan, parse_plan

    bash = parse_plan()
    declared = manifest_plan()
    assert bash, "el parser debe extraer el plan del bash"
    assert set(bash) == set(declared), "manifiesto y bash deben cubrir los mismos activos"
    for name, profiles in bash.items():
        assert declared[name] == profiles, name


def test_parser_extracts_profile_guards() -> None:
    """El parser lee las guardas `=~ ^(all|general)$` del bash."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_profile_parity import parse_plan

    plan = parse_plan()
    # Fase 1 corre siempre; Fase 2 está guardada por perfil.
    assert "sdxl.vae.safetensors" in plan
    assert "minimal" in plan["sdxl.vae.safetensors"]
    assert "flux1-dev-fp8.safetensors" in plan
    assert "minimal" not in plan["flux1-dev-fp8.safetensors"]
    assert "all" in plan["flux1-dev-fp8.safetensors"]


# --------------------------------------------------------------------------
# Ajuste al GPU objetivo (política, punto 5)
# --------------------------------------------------------------------------


def test_recipe_reports_whether_it_fits_the_target_gpu(tmp_path: Path) -> None:
    """La receta medida debe decir si cabe en ~32 GB, no solo dar números."""
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "db.sqlite3")
    store.record_render_measurement(
        template_id="wan_i2v",
        params={"width": 832, "frames": 81},
        duration_s=120.0,
        status="ok",
        vram_peak_mb=20_000,
        ram_peak_mb=30_000,
        gpu_name="RTX 5090",
        driver="580.00",
        cuda="12.8",
    )
    recipe = store.recipe_for_template("wan_i2v")
    assert recipe is not None and recipe["measured"] is True
    fit = recipe["vram_fit"]
    # Perfil vigente: 96 GB anunciados, 94 GB utilizables tras la reserva.
    assert fit["target_vram_gb"] == 96
    assert fit["target_vram_usable_gb"] == 94.0
    assert fit["fits"] is True
    assert fit["headroom_mb"] == int(94 * 1024) - 20_000
    assert fit["optimisations"] == []
    # La RAM se juzga aparte, contra el perfil de RAM del mismo perfil.
    ram_fit = recipe["ram_fit"]
    assert ram_fit["target_ram_gb"] == 128
    assert ram_fit["fits"] is True


def test_recipe_lists_optimisations_when_it_does_not_fit(tmp_path: Path) -> None:
    """Si no cabe, se miden las opciones de la política antes de subir de hardware."""
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "db.sqlite3")
    store.record_render_measurement(
        template_id="wan_i2v",
        params={"width": 1920, "frames": 161},
        duration_s=1800.0,
        status="ok",
        vram_peak_mb=100_000,
    )
    recipe = store.recipe_for_template("wan_i2v")
    fit = recipe["vram_fit"]
    assert fit["fits"] is False
    assert fit["headroom_mb"] < 0
    assert "96" in fit["note"] or "94" in fit["note"]
    ids = [o["id"] for o in fit["optimisations"]]
    # El orden es el de la política: de lo más barato a lo más invasivo.
    assert ids[0] == "precision_quantization"
    assert "vae_tiling" in ids and "sequential_offload" in ids
    assert ids[-1] == "workload_params"


def test_fit_assessment_never_claims_an_optimisation_works() -> None:
    """«These methods are model-specific. Do not apply generic flags blindly.»"""
    from comfy_studio.projects import assess_vram_fit

    fit = assess_vram_fit(vram_peak_mb=40_000)
    for option in fit["optimisations"]:
        assert option["note"], "cada opción debe decir por qué podría no servir"
        assert "works" not in option
        assert "guaranteed" not in option


def test_fit_without_measurement_says_so() -> None:
    """Sin medición no se afirma nada: la política pide resultados medidos."""
    from comfy_studio.projects import assess_vram_fit

    fit = assess_vram_fit(vram_peak_mb=None)
    assert fit["measured"] is False
    assert fit["fits"] is None
    assert "sin medición" in fit["note"]


def test_host_ram_is_not_treated_as_vram() -> None:
    """El veredicto de RAM vive en su propia función, no dentro del de VRAM."""
    from comfy_studio.hardware_profile import BLACKWELL_96_128
    from comfy_studio.projects import assess_ram_fit, assess_vram_fit

    profile = BLACKWELL_96_128
    vram = assess_vram_fit(vram_peak_mb=40_000, ram_peak_mb=60_000, profile=profile)
    ram = assess_ram_fit(ram_peak_mb=60_000, profile=profile)
    assert vram["fits"] is True
    # La RAM medida viaja con la receta, pero su veredicto es aparte.
    assert vram["ram_peak_mb"] == 60_000
    assert "host_ram" not in vram
    assert ram["fits"] is True
    assert ram["target_ram_gb"] == 128


def test_target_vram_is_configurable() -> None:
    from comfy_studio.config import StudioConfig

    assert StudioConfig.from_env({}).target_vram_gb == 96
    assert StudioConfig.from_env({"STUDIO_TARGET_VRAM_GB": "48"}).target_vram_gb == 48


def test_manual_target_still_pays_the_operational_reserve() -> None:
    """Un override manual también son GB del datasheet: se le resta la reserva.

    Sin la reserva, el payload publicaba `target_vram_gb`/`*_usable_gb` del
    perfil mientras juzgaba contra el override crudo: la carga de 50 GB se
    marcaba «no cabe» con 2048 MB de holgura perdidos y la nota citaba 94 GB.
    """
    from comfy_studio.hardware_profile import BLACKWELL_96_128
    from comfy_studio.projects import assess_ram_fit, assess_vram_fit

    profile = BLACKWELL_96_128  # reserva: 2 GB de VRAM, 8 GB de RAM

    vram = assess_vram_fit(
        vram_peak_mb=50 * 1024, profile=profile, target_vram_gb=48
    )
    assert vram["target_vram_gb"] == 48
    assert vram["judged_against_gb"] == 46.0
    assert vram["target_vram_usable_gb"] == 46.0
    assert vram["target_vram_mb"] == int(46 * 1024)
    assert vram["fits"] is False
    assert vram["headroom_mb"] == int(46 * 1024) - 50 * 1024
    assert vram["utilisation_pct"] == round(100.0 * 50 * 1024 / (46 * 1024), 1)
    # La nota cita el número contra el que se juzgó, no el del perfil.
    assert "46 GB utilizables" in vram["note"]
    assert "override de 48 GB" in vram["note"]
    assert "94 GB" not in vram["note"]

    ram = assess_ram_fit(ram_peak_mb=60 * 1024, profile=profile, target_ram_gb=64)
    assert ram["target_ram_gb"] == 64
    assert ram["judged_against_gb"] == 56.0
    assert ram["target_ram_mb"] == int(56 * 1024)
    assert ram["fits"] is False
    assert "56 GB utilizables" in ram["note"]
    assert "120 GB" not in ram["note"]

    # Un override holgado sigue cabiendo, y sigue siendo la base publicada.
    fits = assess_vram_fit(
        vram_peak_mb=40 * 1024, profile=profile, target_vram_gb=128
    )
    assert fits["fits"] is True
    assert fits["judged_against_gb"] == 126.0
    assert fits["target_vram_gb"] == 128
    assert "note" not in fits

    # Sin override, el payload es el del perfil y no cambia nada.
    plain = assess_vram_fit(vram_peak_mb=50 * 1024, profile=profile)
    assert plain["target_vram_gb"] == 96
    assert plain["judged_against_gb"] == 94.0
    assert plain["target_vram_usable_gb"] == 94.0
    assert plain["fits"] is True

    # Un override por debajo de la reserva deja 0 GB utilizables: no cabía nada
    # antes y ahora tampoco, pero sin dividir por cero.
    starved_vram = assess_vram_fit(
        vram_peak_mb=1024, profile=profile, target_vram_gb=1
    )
    assert starved_vram["judged_against_gb"] == 0.0
    assert starved_vram["target_vram_mb"] == 0
    assert starved_vram["fits"] is False
    assert starved_vram["utilisation_pct"] is None
    assert "0 GB utilizables" in starved_vram["note"]

    starved_ram = assess_ram_fit(
        ram_peak_mb=1024, profile=profile, target_ram_gb=4
    )
    assert starved_ram["judged_against_gb"] == 0.0
    assert starved_ram["target_ram_mb"] == 0
    assert starved_ram["fits"] is False
    assert starved_ram["utilisation_pct"] is None


# --------------------------------------------------------------------------
# Perfil de hardware vigente: RTX PRO 6000 Blackwell 96 GB, RAM 128/256
# --------------------------------------------------------------------------


def test_default_profile_is_blackwell_96() -> None:
    """La arquitectura vigente es 96 GB, no los ~32 GB del objetivo antiguo."""
    from comfy_studio.hardware_profile import DEFAULT_PROFILE, RAM_OPTIONS_GB

    assert DEFAULT_PROFILE.gpu.startswith("NVIDIA RTX PRO 6000 Blackwell")
    assert DEFAULT_PROFILE.vram_gb == 96
    assert DEFAULT_PROFILE.ram_gb in RAM_OPTIONS_GB
    assert RAM_OPTIONS_GB == (128, 256)


def test_usable_targets_apply_the_operational_reserve() -> None:
    """Los GB del datasheet no son los utilizables: hay reserva explícita."""
    from comfy_studio.hardware_profile import BLACKWELL_96_128

    assert BLACKWELL_96_128.vram_usable_gb == 94.0
    assert BLACKWELL_96_128.ram_usable_gb == 120.0
    assert BLACKWELL_96_128.vram_usable_gb < BLACKWELL_96_128.vram_gb
    assert BLACKWELL_96_128.ram_usable_gb < BLACKWELL_96_128.ram_gb


def test_a_40gb_recipe_fits_the_96gb_profile() -> None:
    """«Una receta no debe rechazarse por superar 32 GB cuando cabe en el perfil.»

    Con el objetivo antiguo de 32 GB, una receta de 40 GB se marcaba «no cabe» y
    sacaba la lista de optimizaciones. En el perfil vigente cabe de sobra.
    """
    from comfy_studio.hardware_profile import BLACKWELL_96_128
    from comfy_studio.projects import assess_vram_fit

    fit = assess_vram_fit(vram_peak_mb=40 * 1024, profile=BLACKWELL_96_128)
    assert fit["fits"] is True
    assert fit["optimisations"] == []
    assert fit["utilisation_pct"] < 50


def test_the_profile_must_be_propagated_to_the_recipe(tmp_path: Path) -> None:
    """El veredicto se calcula con el perfil, no contra el objetivo por omisión.

    `recipe_for_template` no pasaba el perfil a `assess_vram_fit`, así que la API
    anunciaba un objetivo de 96 GB mientras la receta seguía diciendo «no cabe»
    contra 32.
    """
    from comfy_studio.hardware_profile import CONSUMER_32, HardwareProfile
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "db.sqlite3")
    store.record_render_measurement(
        template_id="wan_i2v",
        params={"width": 1280, "frames": 121},
        duration_s=900.0,
        status="ok",
        vram_peak_mb=40_000,
    )

    without = store.recipe_for_template("wan_i2v")
    assert without["vram_fit"]["fits"] is True  # el perfil por omisión ya es 96

    with_32 = store.recipe_for_template("wan_i2v", profile=CONSUMER_32)
    assert with_32["vram_fit"]["target_vram_gb"] == 32
    assert with_32["vram_fit"]["fits"] is False
    assert with_32["vram_fit"]["optimisations"], "en 32 GB debe sugerir cómo medir"

    custom = HardwareProfile(name="x", gpu="x", vram_gb=96, ram_gb=128, vram_reserve_gb=0)
    with_96 = store.recipe_for_template("wan_i2v", profile=custom)
    assert with_96["vram_fit"]["target_vram_mb"] == 96 * 1024
    assert with_96["vram_fit"]["fits"] is True


def test_ram_and_vram_are_judged_separately() -> None:
    """La RAM de host no amplía lo que cabe en el GPU."""
    from comfy_studio.hardware_profile import BLACKWELL_96_256
    from comfy_studio.projects import assess_ram_fit, assess_vram_fit

    profile = BLACKWELL_96_256
    assert profile.ram_gb == 256 and profile.vram_gb == 96

    vram = assess_vram_fit(vram_peak_mb=90 * 1024, profile=profile)
    ram = assess_ram_fit(ram_peak_mb=200 * 1024, profile=profile)
    assert vram["fits"] is True
    assert ram["fits"] is True

    # 300 GB de RAM no arreglan una VRAM que no cabe.
    vram_over = assess_vram_fit(vram_peak_mb=100 * 1024, profile=profile)
    assert vram_over["fits"] is False
    ram_under = assess_ram_fit(ram_peak_mb=250 * 1024, profile=profile)
    assert ram_under["fits"] is False
    assert "RAM" in ram_under["note"]


def test_profile_is_selectable_by_name_and_ram_option() -> None:
    from comfy_studio.hardware_profile import resolve_profile, with_ram

    assert resolve_profile(None).vram_gb == 96
    assert resolve_profile("blackwell").vram_gb == 96
    assert resolve_profile("consumer").vram_gb == 32
    assert resolve_profile("no-existe").vram_gb == 96  # cae al principal
    assert with_ram(resolve_profile("blackwell"), 256).ram_gb == 256


# --------------------------------------------------------------------------
# W02 (plan Ultra) — regresión P0 #6: la puerta de arranque no arrastra el
# staging de la Fase 3 (que corre en segundo plano tras el arranque).
# --------------------------------------------------------------------------


def _run_sizing(args: list[str]) -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    return json.loads(out.stdout)


def test_w02_start_gate_excludes_phase3_task_staging() -> None:
    """El start gate usa el staging de ARRANQUE, no el de la tarea de Fase 3.

    La Fase 3 (y su pico de staging ~35 GB) se descarga en segundo plano
    DESPUÉS de que ComfyUI está listo; ese pico no pertenece a lo que debe estar
    libre para arrancar. El volumen total sí cuenta el mayor de los dos stagings.
    """
    report = _run_sizing(["--profile", "studio"])
    boot_staging = report["boot_staging_bytes"]
    task_staging = report["task_staging_bytes"]
    components = report["components"]

    # El start gate = bootstrap + staging de ARRANQUE + margen; no incluye el
    # staging de la tarea de Fase 3.
    start_gate = report["start_gate_bytes"]
    expected = components["startup_download"] + boot_staging + 10 * (1024**3)
    assert start_gate == expected, (
        f"start_gate {start_gate} debe ser bootstrap+boot_staging+margen {expected}, "
        f"no incluir task_staging {task_staging}"
    )

    # El volumen total sí reserva el mayor pico de staging (pueden no coincidir
    # en el tiempo, así que basta el mayor).
    assert components["staging_peak"] == max(boot_staging, task_staging)

    # Coherencia preservada: el umbral S5 declarado sigue cubriendo el gate.
    assert report["start_gate_shortfall_gb"] == 0.0


def test_w02_profile_without_phase3_has_no_task_staging() -> None:
    """Un perfil sin Fase 3 no tiene staging de tarea; su gate es el de arranque."""
    report = _run_sizing(["--profile", "general"])
    assert report["task_staging_bytes"] == 0
    assert report["components"]["task_models"] == 0
