"""Preparación de motores: «instalado» no es «en ejecución» (política de arquitectura).

La política del repositorio exige que la salud y el descubrimiento de capacidades
distingan estados que hoy se confunden en un único `wired` booleano:

  - software instalado (binario/entrypoint presente);
  - integración configurada (variables y wiring del estudio resueltos);
  - pesos requeridos ausentes;
  - modelo disponible en disco;
  - compatibilidad de hardware;
  - modelo cargado en memoria;
  - ejecución realmente validada.

Este módulo implementa esa taxonomía con una sonda **barata y determinista**: solo
`stat` de rutas, `shutil.which` e `importlib.util.find_spec`. **Nunca descarga
pesos, nunca carga un modelo y nunca arranca la GPU** — eso queda para
`comfyctl strict-check` (S1–S10, hardware) y para el verbo `run` de cada motor.

`model_loaded` y `execution_validated` son falsos por defecto a propósito: un
chequeo de salud no puede afirmar que un modelo está cargado. Solo los flipa
`record_successful_run()`, llamado por quien ejecuta de verdad.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Defaults de rutas cuando el entorno no las define (coinciden con la imagen).
_DEFAULT_ENV: dict[str, str] = {
    "COMFY_APP_DIR": "/opt/ComfyUI",
    "COMFY_DATA_DIR": "/workspace/comfyui-data",
}


@dataclass(frozen=True, slots=True)
class EnginePrepSpec:
    """Declaración de lo que un motor necesita para estar *preparado*.

    `binaries`, `venvs` y `weight_paths` admiten placeholders `{VAR}` que se
    expanden con el entorno. `binaries` es **cualquiera-de** (candidatos
    alternativos, igual que `find_blender_bin()`); el resto es **todos**.
    """

    name: str
    #: Entrypoints candidatos: nombre resoluble en PATH o ruta absoluta ejecutable.
    binaries: tuple[str, ...] = ()
    #: Ficheros que deben existir (no ejecutables), p. ej. `main.py`.
    files: tuple[str, ...] = ()
    #: Módulos Python importables (se comprueban con `find_spec`, sin importar).
    modules: tuple[str, ...] = ()
    #: Entornos aislados que deben existir en disco.
    venvs: tuple[str, ...] = ()
    #: Variables de entorno que configuran la integración con el estudio.
    config_env: tuple[str, ...] = ()
    #: Rutas de pesos; si faltan, el motor está instalado pero «sin pesos».
    weight_paths: tuple[str, ...] = ()
    #: Ficheros que deben existir dentro de cada `weight_paths` (vacío = contenido
    #: no vacío). Permite exigir los archivos exactos de un grupo de pesos.
    weight_files: tuple[str, ...] = ()
    #: True si el motor declara que necesita pesos para ejecutar.
    weights_required: bool = False
    #: True si el motor necesita hardware (GPU) presente para ejecutar.
    hardware_required: bool = False
    #: Nota corta sobre qué falta o qué es opcional.
    note: str = ""


@dataclass(frozen=True, slots=True)
class Preparedness:
    """Taxonomía de preparación de un motor (serializable)."""

    installed: bool
    integration: bool
    weights_required: bool
    #: None cuando el motor no declara pesos (no aplica).
    weights_present: bool | None
    #: «not_required» | «ready» | «unavailable»: hecho barato (binario NVIDIA
    #: en PATH o `/dev/nvidia0`). La validación profunda de hardware es de
    #: `comfyctl strict-check` (S1–S10).
    hardware: str
    model_loaded: bool
    execution_validated: bool
    missing: tuple[str, ...]
    state: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "integration": self.integration,
            "weights_required": self.weights_required,
            "weights_present": self.weights_present,
            "hardware": self.hardware,
            "model_loaded": self.model_loaded,
            "execution_validated": self.execution_validated,
            "missing": list(self.missing),
            "state": self.state,
        }


#: Última ejecución correcta por motor, en memoria de proceso. Un chequeo de
#: salud nunca la inventa: solo la escribe quien ejecutó el motor de verdad.
_RUN_LEDGER: dict[str, bool] = {}


def record_successful_run(name: str) -> None:
    """Marca que el motor ejecutó correctamente al menos una vez en este pod."""
    _RUN_LEDGER[name] = True


def reset_run_ledger() -> None:
    """Limpia el ledger (tests)."""
    _RUN_LEDGER.clear()


def execution_validated(name: str) -> bool:
    return bool(_RUN_LEDGER.get(name))


def note_engine_run(name: str, ok: bool) -> None:
    """Registra el resultado de una ejecución real (llamado por quien ejecuta).

    Solo una ejecución correcta flipa `execution_validated`; un fallo no borra
    una validación anterior, porque el motor sí demostró ser capaz.
    """
    if ok:
        record_successful_run(name)


def _expand(value: str, env: Mapping[str, str]) -> str:
    """Expande `{VAR}` con el entorno (más defaults de imagen) y `~`.

    Cualquier `{VAR}` presente en el mapeo se sustituye; lo ausente queda
    literal (y por tanto no existe en disco). Así lo documenta `EnginePrepSpec`.
    """
    out = value
    for key, default in _DEFAULT_ENV.items():
        out = out.replace("{" + key + "}", env.get(key, default))
    for key, val in env.items():
        if key and isinstance(val, str):
            out = out.replace("{" + key + "}", val)
    return os.path.expanduser(out)


def _resolve_binary(candidate: str, env: Mapping[str, str]) -> str | None:
    """Resuelve un candidato a ruta ejecutable, o None."""
    path = _expand(candidate, env)
    if os.path.isabs(path):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        return None
    return shutil.which(path, path=env.get("PATH"))


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - módulo roto
        return False


def _path_has_content(path: str) -> bool:
    """Un fichero con bytes o un directorio con al menos una entrada.

    La disponibilidad de pesos es un hecho del sistema de archivos: un
    directorio vacío o un fichero de 0 bytes NO cuentan como presente.
    """
    try:
        target = Path(path)
        if target.is_file():
            return target.stat().st_size > 0
        if target.is_dir():
            return any(target.iterdir())
    except OSError:  # pragma: no cover - carrera de permisos
        return False
    return False


def _hardware_available(env: Mapping[str, str]) -> bool:
    """Hecho barato y determinista: binario NVIDIA en PATH o `/dev/nvidia0`.

    Sin GPU no hay GPU: la sonda nunca afirma hardware que no está en disco.
    """
    if os.path.exists("/dev/nvidia0"):
        return True
    for directory in env.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, "nvidia-smi")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


@dataclass(frozen=True, slots=True)
class WeightsStatus:
    """Estado de pesos de un motor: un hecho de disco, nunca una env var."""

    required: bool
    #: None cuando el motor no declara pesos (no aplica).
    present: bool | None
    missing: tuple[str, ...]

    @property
    def state(self) -> str:
        if not self.required:
            return "not_required"
        return "present" if self.present else "missing"


def probe_weights(
    spec: EnginePrepSpec, *, env: Mapping[str, str] | None = None
) -> WeightsStatus:
    """Sonda de pesos: fichero no vacío, directorio con contenido o archivos
    concretos (`weight_files`) dentro de cada ruta declarada."""
    if not spec.weights_required:
        return WeightsStatus(required=False, present=None, missing=())
    environment = os.environ if env is None else env
    missing: list[str] = []
    for weight in spec.weight_paths:
        target = _expand(weight, environment)
        if spec.weight_files:
            base = Path(target)
            if not base.is_dir() or any(
                not (base / name).is_file() for name in spec.weight_files
            ):
                missing.append(weight)
        elif not _path_has_content(target):
            missing.append(weight)
    return WeightsStatus(required=True, present=not missing, missing=tuple(missing))


def weights_status(
    name: str, *, env: Mapping[str, str] | None = None
) -> WeightsStatus:
    """Estado de pesos de un motor del roster (para los adaptadores)."""
    spec = build_default_prep_specs().get(name)
    if spec is None:
        return WeightsStatus(required=False, present=None, missing=())
    return probe_weights(spec, env=env)


def probe_preparedness(
    spec: EnginePrepSpec,
    *,
    env: Mapping[str, str] | None = None,
    module_finder: Callable[[str], bool] | None = None,
) -> Preparedness:
    """Sonda barata de preparación. Sin red, sin GPU, sin cargar modelos."""
    environment = os.environ if env is None else env
    finder = module_finder or _module_available
    missing: list[str] = []

    binaries_ok = True
    if spec.binaries:
        resolved = [
            found
            for found in (_resolve_binary(c, environment) for c in spec.binaries)
            if found is not None
        ]
        binaries_ok = bool(resolved)
        if not binaries_ok:
            missing.append(f"binario:{spec.binaries[0]}")

    modules_ok = True
    for mod in spec.modules:
        if not finder(mod):
            modules_ok = False
            missing.append(f"modulo:{mod}")

    venvs_ok = True
    for venv in spec.venvs:
        if not os.path.isdir(_expand(venv, environment)):
            venvs_ok = False
            missing.append(f"venv:{venv}")

    files_ok = True
    for required in spec.files:
        if not os.path.isfile(_expand(required, environment)):
            files_ok = False
            missing.append(f"fichero:{required}")

    installed = binaries_ok and modules_ok and venvs_ok and files_ok

    integration_ok = True
    for var in spec.config_env:
        if not environment.get(var, "").strip():
            integration_ok = False
            missing.append(f"config:{var}")

    weights = probe_weights(spec, env=environment)
    weights_present = weights.present
    missing.extend(f"pesos:{weight}" for weight in weights.missing)

    hardware = "not_required"
    if spec.hardware_required:
        hardware = "ready" if _hardware_available(environment) else "unavailable"
        if hardware == "unavailable":
            missing.append("hardware:gpu")

    if not installed:
        state = "not_installed"
    elif not integration_ok:
        state = "integration_missing"
    elif weights_present is False:
        state = "weights_missing"
    elif hardware == "unavailable":
        state = "hardware_unavailable"
    else:
        state = "ready"

    return Preparedness(
        installed=installed,
        integration=integration_ok,
        weights_required=spec.weights_required,
        weights_present=weights_present,
        hardware=hardware,
        model_loaded=False,
        execution_validated=execution_validated(spec.name),
        missing=tuple(missing),
        state=state,
    )


@dataclass(slots=True)
class PrepReport:
    """Resumen agregado de la taxonomía para el roster completo."""

    engines: dict[str, Preparedness] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out = {
            "ready": 0,
            "weights_missing": 0,
            "not_installed": 0,
            "integration_missing": 0,
            "hardware_unavailable": 0,
        }
        for prep in self.engines.values():
            out[prep.state] = out.get(prep.state, 0) + 1
        return out

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "engines": {name: prep.to_public_dict() for name, prep in self.engines.items()},
        }


def build_default_prep_specs() -> dict[str, EnginePrepSpec]:
    """Declaración de preparación de los 21 motores del roster.

    Refleja lo que la imagen hornea realmente (`Dockerfile`) y lo que
    `download-models.sh` trae como activo de tarea. Un motor puede estar
    `installed` y a la vez `weights_missing`: es el caso normal de un motor
    correctamente preparado cuyos pesos son dependencia de tarea.
    """
    specs = [
        EnginePrepSpec(
            name="comfyui",
            files=("{COMFY_APP_DIR}/main.py",),
            config_env=("COMFY_APP_DIR",),
            hardware_required=True,
            note="núcleo + 12 custom nodes fijados por SHA en nodes.lock",
        ),
        EnginePrepSpec(
            name="blender",
            binaries=(
                "{STUDIO_BLENDER_BIN}",
                "blender",
                "/opt/blender/blender",
                "/usr/local/bin/blender",
                "/usr/bin/blender",
            ),
            hardware_required=True,
            note="sin binario en la imagen: modo 3D requiere Blender instalado",
        ),
        EnginePrepSpec(
            name="editor",
            binaries=("node", "npx"),
            venvs=("{STUDIO_REMOTION_PROJECT}",),
            config_env=("STUDIO_REMOTION_PROJECT",),
            note="Remotion horneado en /opt/remotion (Chrome Headless Shell incluido)",
        ),
        EnginePrepSpec(
            name="ffmpeg",
            binaries=("{STUDIO_FFMPEG_BIN}", "ffmpeg"),
            note="libx264 + h264_nvenc verificados en el arranque",
        ),
        EnginePrepSpec(
            name="heavy_video",
            config_env=(
                "STUDIO_HEAVY_VIDEO_BASE_URL",
                "STUDIO_HEAVY_VIDEO_MODEL",
                "STUDIO_HEAVY_VIDEO_KEY",
            ),
            note="cliente de API externa: no hay binario local que preparar",
        ),
        EnginePrepSpec(
            name="audio",
            binaries=("{STUDIO_FFMPEG_BIN}", "ffmpeg"),
            weight_paths=("{COMFY_DATA_DIR}/models/whisper/faster-whisper-large-v3",),
            weights_required=True,
            note="TTS (Kokoro) horneado; STT real usa los pesos faster-whisper",
        ),
        EnginePrepSpec(
            name="voice_clone",
            binaries=("{STUDIO_VOICE_CLONE_BIN}",),
            modules=("fairseq",),
            weight_paths=("{STUDIO_VOICE_CLONE_MODEL_DIR}",),
            weights_required=True,
            note="RVC horneado --no-deps; fairseq y pesos quedan fuera de la imagen",
        ),
        EnginePrepSpec(
            name="indextts",
            binaries=("{STUDIO_INDEXTTS_BIN}",),
            venvs=("{COMFY_DATA_DIR}/venvs/indextts",),
            weight_paths=("{STUDIO_INDEXTTS_MODEL_DIR}",),
            weight_files=("config.yaml", "gpt.pth"),
            weights_required=True,
            hardware_required=True,
            note="adaptador horneado; el venv aislado se crea en el volumen",
        ),
        EnginePrepSpec(
            name="music_gen",
            binaries=("{STUDIO_MUSIC_GEN_BIN}",),
            modules=("audiocraft",),
            weight_paths=("{COMFY_DATA_DIR}/models/musicgen",),
            weights_required=True,
            hardware_required=True,
            note="audiocraft horneado --no-deps; deps pesadas y pesos en runtime",
        ),
        EnginePrepSpec(
            name="lipsync",
            binaries=("{STUDIO_LIPSYNC_BIN}",),
            weight_paths=("{STUDIO_LIPSYNC_MODEL_DIR}/wav2lip_gan.pth",),
            weights_required=True,
            hardware_required=True,
            note="repo horneado por SHA; el checkpoint no está en la imagen",
        ),
        EnginePrepSpec(
            name="upscaler",
            binaries=("{STUDIO_UPSCALER_BIN}",),
            weight_paths=("{STUDIO_UPSCALER_MODEL_DIR}",),
            weights_required=True,
            hardware_required=True,
            note="Real-ESRGAN ncnn-vulkan + modelos horneados en la imagen",
        ),
        EnginePrepSpec(
            name="capture3d",
            binaries=("ns-train",),
            modules=("nerfstudio", "tinycudann"),
            hardware_required=True,
            note="nerfstudio horneado; tiny-cuda-nn se compila en el volumen",
        ),
        EnginePrepSpec(
            name="compositor",
            binaries=("{STUDIO_COMPOSITOR_BIN}",),
            note="Natron 2.5.0 portable horneado",
        ),
        EnginePrepSpec(
            name="qa",
            binaries=("{STUDIO_FFMPEG_BIN}", "ffmpeg"),
            note="nivel 1 (blackdetect/freezedetect) horneado; VBench es opcional",
        ),
        EnginePrepSpec(
            name="roto",
            modules=("sam3",),
            weight_paths=("{STUDIO_ROTO_MODEL_DIR}",),
            weights_required=True,
            hardware_required=True,
            note="paquete sam3 horneado; pesos HF-gated y inferencia pendiente",
        ),
        EnginePrepSpec(
            name="llm_server",
            binaries=("{STUDIO_LLM_SERVER_BIN}", "llama-server"),
            weight_paths=("{STUDIO_LLM_SERVER_MODEL}",),
            weights_required=True,
            note="llama-server v0.9.0 horneado; modelo .gguf en runtime",
        ),
        EnginePrepSpec(
            name="ad_studio",
            note="compuesto: reutiliza ffmpeg, EDL, audio, music_gen y qa",
        ),
        EnginePrepSpec(
            name="reference-local",
            binaries=("{STUDIO_FFMPEG_BIN}", "ffmpeg", "ffprobe"),
            modules=("comfy_studio.inspiration",),
            note="análisis local de referencias; requiere ffmpeg/ffprobe y módulo inspiration",
        ),
        EnginePrepSpec(
            name="atlas-offline",
            modules=("comfy_studio.atlas.packaging",),
            config_env=("STUDIO_ATLAS_ROOT",),
            note="empaquetado/verificación del bundle del atlas; módulo packaging",
        ),
        EnginePrepSpec(
            name="scene-optimizer",
            modules=("comfy_studio.cinematography",),
            note="validación de composición/plan de cámara; módulo cinematography",
        ),
        EnginePrepSpec(
            name="chrono",
            modules=("comfy_studio.physics",),
            note="proxy cinemático CPU; módulo physics (no requiere Chrono/Blender real)",
        ),
    ]
    return {spec.name: spec for spec in specs}


def probe_all(
    specs: Mapping[str, EnginePrepSpec] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    module_finder: Callable[[str], bool] | None = None,
) -> PrepReport:
    """Sonda la taxonomía de todos los motores declarados."""
    table = build_default_prep_specs() if specs is None else dict(specs)
    report = PrepReport()
    for name, spec in table.items():
        report.engines[name] = probe_preparedness(
            spec, env=env, module_finder=module_finder
        )
    return report
