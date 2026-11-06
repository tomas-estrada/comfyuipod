"""Bordes puros y fallos tempranos de `edl/ffmpeg_edl.py` sin ejecutar ffmpeg.

- `cut_subprocess_timeout`: duraciones 0 / negativas / absurdas / no finitas,
  `base_s` ausente, cero o negativa, `cap_s` por debajo de la base y el redondeo
  exacto en el límite del techo.
- `execute_edl_ffmpeg`: guardas de validación previas a arrancar cualquier
  subprocess (proyecto o versión de EDL inexistentes, cortes vacíos o sobre
  cota, `ffmpeg` ausente, corte imposible, fuente sin vídeo, fuente que ffprobe
  no puede verificar) y la derivación real del timeout de cada corte en el
  punto de uso (~línea 319), sustituyendo `subprocess` por un doble que
  registra la llamada en lugar de arrancar `ffmpeg`.

Nada de este fichero ejecuta `ffmpeg`, `ffprobe` ni ningún binario externo: los
únicos puntos de contacto con el entorno se sustituyen por dobles que fallan en
señalar cualquier intento de ejecución real.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from comfy_studio.edl import ffmpeg_edl
from comfy_studio.edl.ffmpeg_edl import (
    DEFAULT_FFMPEG_TIMEOUT_S,
    MAX_EDL_CUTS,
    MAX_FFMPEG_TIMEOUT_S,
    cut_subprocess_timeout,
    resolve_aspect_dimensions,
)
from comfy_studio.projects import ProjectStore


class _NoSubprocess:
    """Doble de `subprocess` que registra las llamadas y jamás arranca un binario.

    - `forbidden` (por defecto): estalla ante cualquier `run`, para los tests
      que no deben llegar ni siquiera a construir el comando.
    - `timeout`: lanza su propio `TimeoutExpired`, que el `except` homónimo de
      `ffmpeg_edl` debe capturar.
    - `fail` / `ok`: devuelve un resultado que hace fallar la rama de error
      (returncode != 0, o returncode 0 sin fichero de segmento).
    """

    class TimeoutExpired(Exception):
        """Excepción homónima para que los `except` del módulo sigan siendo válidos."""

    def __init__(self, behavior: str = "forbidden", stderr: bytes = b"") -> None:
        self.behavior = behavior
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    def run(self, cmd: Any, **kwargs: Any) -> Any:
        self.calls.append({"cmd": list(cmd), "timeout": kwargs.get("timeout")})
        if self.behavior == "timeout":
            raise self.TimeoutExpired(cmd, kwargs.get("timeout"))
        if self.behavior == "forbidden":
            raise AssertionError("no debe lanzarse ningún subprocess desde ffmpeg_edl")
        returncode = 1 if self.behavior == "fail" else 0
        return SimpleNamespace(returncode=returncode, stderr=self.stderr)


@pytest.fixture(autouse=True)
def _sin_binarios_externos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red de seguridad: ningún test de este fichero puede ejecutar ffmpeg/ffprobe."""
    monkeypatch.setattr(ffmpeg_edl, "subprocess", _NoSubprocess())
    # Sin `ffmpeg` en el PATH ninguna rama puede llegar a un subprocess real.
    monkeypatch.setattr(ffmpeg_edl, "find_ffmpeg", lambda: None)

    def _probe_bomba(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("probe_media_info solo debe llamarse donde el test lo sustituye")

    monkeypatch.setattr(ffmpeg_edl, "probe_media_info", _probe_bomba)


@pytest.fixture()
def proyecto(studio_data_dir: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="EDL fallo temprano")
    return store, project["id"]


def _guardar_edl(store: ProjectStore, project_id: str, cuts: list[dict[str, Any]]) -> None:
    store.save_edl_version(
        project_id,
        edl={
            "schema": "comfy_studio.edl.v1",
            "title": "EDL de prueba",
            "aspect_ratio": "16:9",
            "cuts": cuts,
        },
    )


# --- cut_subprocess_timeout: bordes de entrada ---------------------------------


@pytest.mark.parametrize(
    ("duration", "base_s", "expected"),
    [
        (-5.0, 60.0, 60.0),  # duración negativa -> anclada en la base
        (0.0, 30.0, 30.0),  # duración cero -> propaga la base personalizada
        (0.0, 0.0, 1.0),  # base 0 -> recortada a 1.0
        (0.0, -12.5, 1.0),  # base negativa -> recortada a 1.0
        (10.0, -3.0, 41.0),  # la base recortada alimenta el escalado: 4*10 + 1
    ],
)
def test_cut_timeout_clamps_non_positive_bases_and_durations(
    duration: float,
    base_s: float,
    expected: float,
) -> None:
    assert cut_subprocess_timeout(duration, base_s=base_s) == expected


def test_cut_timeout_scales_with_fractional_duration_and_default_base() -> None:
    assert cut_subprocess_timeout(2.5, base_s=60.0) == 70.0
    assert cut_subprocess_timeout(0.25, base_s=60.0) == 61.0
    assert cut_subprocess_timeout(90.0) == 4.0 * 90.0 + DEFAULT_FFMPEG_TIMEOUT_S


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (99.5, 458.0),  # justo por debajo del techo: 4*99.5 + 60
        (100.0, 460.0),  # exactamente en el techo
        (100.5, 460.0),  # por encima del techo -> recortado
    ],
)
def test_cut_timeout_boundary_against_custom_cap(duration: float, expected: float) -> None:
    assert cut_subprocess_timeout(duration, base_s=60.0, cap_s=460.0) == expected


def test_cut_timeout_caps_absurd_and_infinite_durations() -> None:
    """Una duración absurda jamás destraba un subprocess sin techo."""
    assert cut_subprocess_timeout(1e9) == MAX_FFMPEG_TIMEOUT_S
    assert cut_subprocess_timeout(float("inf")) == MAX_FFMPEG_TIMEOUT_S


def test_cut_timeout_never_propagates_non_finite_inputs() -> None:
    """Un timeout NaN sería indistinguible de 'sin límite' para subprocess.run."""
    assert cut_subprocess_timeout(float("nan"), base_s=60.0) == 60.0
    assert cut_subprocess_timeout(0.0, base_s=float("nan")) == 1.0
    assert cut_subprocess_timeout(0.0, base_s=60.0, cap_s=float("nan")) == 60.0


def test_cut_timeout_cap_never_drops_below_clamped_base() -> None:
    assert cut_subprocess_timeout(0.0, base_s=60.0, cap_s=10.0) == 60.0
    assert cut_subprocess_timeout(0.0, base_s=0.0, cap_s=0.0) == 1.0


def test_cut_timeout_invariants_across_grid() -> None:
    """Nunca por debajo de la base recortada ni por encima del techo efectivo."""
    durations = [0.0, 0.5, 1.0, 30.0, 150.0, 420.0, 1000.0, 1e9, float("inf")]
    bases = [0.0, 1.0, 60.0, 300.0]
    caps = [0.0, 30.0, 450.0, 5000.0]
    for base_s in bases:
        base = max(1.0, base_s)
        for cap_s in caps:
            cap = max(base, cap_s)
            for duration in durations:
                result = cut_subprocess_timeout(duration, base_s=base_s, cap_s=cap_s)
                # Si el resultado fuese NaN, estas comparaciones fallarían.
                assert base <= result <= cap, (duration, base_s, cap_s, result)


def test_cut_timeout_coerces_numeric_strings_and_rejects_garbage() -> None:
    assert cut_subprocess_timeout(1.0, base_s="30.5") == 34.5
    with pytest.raises(ValueError):
        cut_subprocess_timeout(1.0, base_s="abc")


# --- execute_edl_ffmpeg: guardas previas a cualquier subprocess -----------------


def test_execute_edl_rejects_unknown_project(
    proyecto: tuple[ProjectStore, str],
) -> None:
    store, _project_id = proyecto
    with pytest.raises(ValueError, match="Proyecto no encontrado"):
        ffmpeg_edl.execute_edl_ffmpeg(store, "proyecto-que-no-existe")


def test_execute_edl_rejects_missing_edl_version(
    proyecto: tuple[ProjectStore, str],
) -> None:
    store, project_id = proyecto
    with pytest.raises(ValueError, match="Versión de EDL no encontrada"):
        ffmpeg_edl.execute_edl_ffmpeg(store, project_id, edl_id="edl-que-no-existe")
    with pytest.raises(ValueError, match="Versión de EDL no encontrada"):
        ffmpeg_edl.execute_edl_ffmpeg(store, project_id, version=99)


def test_execute_edl_requires_a_version_when_no_cuts_are_given(
    proyecto: tuple[ProjectStore, str],
) -> None:
    store, project_id = proyecto
    with pytest.raises(ValueError, match="ninguna versión de EDL"):
        ffmpeg_edl.execute_edl_ffmpeg(store, project_id)


def test_execute_edl_empty_cuts_raise_and_document_pre_validation_save(
    proyecto: tuple[ProjectStore, str],
) -> None:
    """Comportamiento actual, bug menor NO parcheado: con `cuts=[]` la versión se
    guarda en el store ANTES de fallar la validación, así que queda un EDL vacío
    registrado. Si algún día la validación se mueve antes del `save_edl_version`,
    este test debe actualizarse conscientemente."""
    store, project_id = proyecto
    with pytest.raises(ValueError, match="lista de cortes del EDL está vacía"):
        ffmpeg_edl.execute_edl_ffmpeg(store, project_id, cuts=[])
    saved = store.get_edl_version(project_id)
    assert saved is not None
    assert saved["edl"].get("cuts") == []


def test_execute_edl_rejects_cut_count_over_cap(
    proyecto: tuple[ProjectStore, str],
) -> None:
    store, project_id = proyecto
    cuts = [{"in_sec": float(i), "out_sec": float(i) + 1.0} for i in range(MAX_EDL_CUTS + 1)]
    _guardar_edl(store, project_id, cuts)
    with pytest.raises(ValueError, match="máximo permitido"):
        ffmpeg_edl.execute_edl_ffmpeg(store, project_id)


def test_execute_edl_accepts_cut_count_at_cap_and_fails_fast_without_ffmpeg(
    proyecto: tuple[ProjectStore, str],
) -> None:
    """200 cortes pasan la cota M3-SCR-005 y llegan a la puerta de `find_ffmpeg`
    (el fixture autouse la devuelve `None`): el fallo es 'ffmpeg ausente', no
    la cota de cortes."""
    store, project_id = proyecto
    cuts = [{"in_sec": float(i), "out_sec": float(i) + 1.0} for i in range(MAX_EDL_CUTS)]
    _guardar_edl(store, project_id, cuts)
    with pytest.raises(RuntimeError, match="No se encontró"):
        ffmpeg_edl.execute_edl_ffmpeg(store, project_id)


@pytest.mark.parametrize(("in_sec", "out_sec"), [(5.0, 5.0), (6.0, 2.0)])
def test_execute_edl_rejects_impossible_cut_before_any_subprocess(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
    in_sec: float,
    out_sec: float,
) -> None:
    store, project_id = proyecto
    # Sustituye al `None` del fixture: la validación de cortes ocurre DESPUÉS de
    # `find_ffmpeg` y antes de cualquier subprocess (el doble de subprocess estalla).
    monkeypatch.setattr(ffmpeg_edl, "find_ffmpeg", lambda: "ffmpeg-sin-ejecutar")
    with pytest.raises(ValueError, match="Corte #1 inválido"):
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": in_sec, "out_sec": out_sec}],
        )


def test_execute_edl_requires_a_video_source_before_probing(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, project_id = proyecto
    monkeypatch.setattr(ffmpeg_edl, "find_ffmpeg", lambda: "ffmpeg-sin-ejecutar")
    with pytest.raises(ValueError, match="ningún asset de vídeo fuente"):
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
        )


@pytest.mark.parametrize("use_preferred_source", [True, False])
def test_execute_edl_fails_when_source_probe_fails(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
    use_preferred_source: bool,
) -> None:
    """Un origen que ffprobe no puede verificar aborta ANTES de recortar nada
    (M3-IDEA-010), tanto por la vía del asset preferido como por el fallback."""
    store, project_id = proyecto
    asset = store.save_upload(
        project_id,
        filename="src.mp4",
        content_type="video/mp4",
        data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
        max_upload_bytes=1024 * 1024,
        category="uploads",
    )
    monkeypatch.setattr(ffmpeg_edl, "find_ffmpeg", lambda: "ffmpeg-sin-ejecutar")

    probes: list[dict[str, Any]] = []

    def _probe_falso(src: Path, **kwargs: Any) -> dict[str, Any]:
        probes.append({"src": src, **kwargs})
        return {"probe_ok": False, "has_audio": False}

    monkeypatch.setattr(ffmpeg_edl, "probe_media_info", _probe_falso)
    overrides: dict[str, Any] = {}
    if use_preferred_source:
        overrides["source_asset_id"] = asset["id"]
    with pytest.raises(RuntimeError, match="no pudo verificarse con ffprobe"):
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
            **overrides,
        )
    assert len(probes) == 1
    assert probes[0]["timeout_s"] == ffmpeg_edl.DEFAULT_FFPROBE_TIMEOUT_S


def test_execute_edl_falls_back_to_previous_render_as_source(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin clips de entrada, la resolución de fuente acepta en el bucle de
    fallback un render previo del EDL (aunque la verificación posterior falle)."""
    store, project_id = proyecto
    store.save_upload(
        project_id,
        filename="previo.mp4",
        content_type="video/mp4",
        data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
        max_upload_bytes=1024 * 1024,
        category="outputs",
        metadata={"edl_render": True},
    )
    monkeypatch.setattr(ffmpeg_edl, "find_ffmpeg", lambda: "ffmpeg-sin-ejecutar")
    monkeypatch.setattr(
        ffmpeg_edl, "probe_media_info", lambda *args, **kwargs: {"probe_ok": False}
    )
    with pytest.raises(RuntimeError, match="no pudo verificarse con ffprobe"):
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
        )


def _preparar_fuente_verificable(
    store: ProjectStore,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Vídeo de entrada + ffmpeg 'encontrado' + ffprobe que verifica: el corte
    llega hasta el punto de uso del subprocess sin arrancar ningún binario."""
    asset = store.save_upload(
        project_id,
        filename="src.mp4",
        content_type="video/mp4",
        data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
        max_upload_bytes=1024 * 1024,
        category="uploads",
    )
    monkeypatch.setattr(ffmpeg_edl, "find_ffmpeg", lambda: "ffmpeg-sin-ejecutar")
    monkeypatch.setattr(
        ffmpeg_edl,
        "probe_media_info",
        lambda *args, **kwargs: {"probe_ok": True, "has_audio": False},
    )
    return asset


def test_execute_edl_derives_per_cut_timeout_at_use_site_without_running_ffmpeg(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El timeout de cada corte se deriva en el punto de uso (~línea 319) como
    `4 × duración + ffmpeg_timeout_s`, y un timeout del subprocess se convierte
    en un error explícito. El doble registra el valor sin arrancar ffmpeg."""
    store, project_id = proyecto
    _preparar_fuente_verificable(store, project_id, monkeypatch)
    recorder = _NoSubprocess(behavior="timeout")
    monkeypatch.setattr(ffmpeg_edl, "subprocess", recorder)
    with pytest.raises(
        RuntimeError, match="excedió el tiempo límite al recortar segmento #1"
    ) as excinfo:
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 1.0, "out_sec": 3.5}],
            ffmpeg_timeout_s=45.0,
        )
    assert len(recorder.calls) == 1
    # corte de 2.5 s con base 45 s -> 4 * 2.5 + 45 = 55 s, y el mensaje lo refleja.
    assert recorder.calls[0]["timeout"] == 55.0
    assert "(55s para un corte de 2.50s" in str(excinfo.value)


def test_execute_edl_surfaces_ffmpeg_nonzero_exit_without_running_it(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, project_id = proyecto
    _preparar_fuente_verificable(store, project_id, monkeypatch)
    recorder = _NoSubprocess(behavior="fail", stderr=b"boom")
    monkeypatch.setattr(ffmpeg_edl, "subprocess", recorder)
    with pytest.raises(RuntimeError, match="Error de ffmpeg al recortar segmento #1") as excinfo:
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
        )
    assert "boom" in str(excinfo.value)
    assert len(recorder.calls) == 1


def test_execute_edl_rejects_zero_exit_without_segment_file(
    proyecto: tuple[ProjectStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """returncode 0 pero sin fichero de segmento también es fallo (el `or` de la
    condición): el error no se silencia con un exit code limpio."""
    store, project_id = proyecto
    _preparar_fuente_verificable(store, project_id, monkeypatch)
    recorder = _NoSubprocess(behavior="ok")
    monkeypatch.setattr(ffmpeg_edl, "subprocess", recorder)
    with pytest.raises(RuntimeError, match="Error de ffmpeg al recortar segmento #1") as excinfo:
        ffmpeg_edl.execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
        )
    assert str(excinfo.value).rstrip().endswith("segmento #1:")


# --- helper puro alimentado por el executor -------------------------------------


@pytest.mark.parametrize(
    ("aspect", "expected"),
    [
        ("16:9", (1280, 720)),
        ("9:16", (720, 1280)),
        (" 16:9 ", (1280, 720)),  # se limpia el espacio antes de buscar
        ("21:9", (1280, 720)),  # desconocido -> 16:9
        ("", (1280, 720)),
        (None, (1280, 720)),  # en la práctica llega `None` desde parámetros opcionales
    ],
)
def test_resolve_aspect_dimensions_falls_back_to_16x9(
    aspect: str | None,
    expected: tuple[int, int],
) -> None:
    assert resolve_aspect_dimensions(aspect) == expected
