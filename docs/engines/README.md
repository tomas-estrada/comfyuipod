# Motores del estudio

Tabla maestra de los **20 motores** registrados en `comfy_studio/engines/registry.py`.

| Nombre | Tier | Wired | Ruta real (CPU) | Módulo reutilizado | Semántica simulada |
|--------|------|-------|-----------------|-------------------|--------------------|
| comfyui | resident | sí | async HTTP | comfyui_client | No simulable; si no hay GPU, el verb falla tipificado |
| blender | warm | sí | subproceso headless | blender.py | `STUDIO_STUB_BLENDER=1` |
| editor | warm | sí | npx remotion | (stub F6) | stub con manifiesto vacío |
| ffmpeg | warm | sí | shutil.which | media_bins | No simulable; herramienta base |
| heavy_video | installable | sí | cliente HTTP | ml/runner | stub con artefacto vacío |
| audio | warm | sí | Kokoro / worker ASR | audio.py | `STUDIO_STUB_AUDIO=1` |
| voice_clone | warm | sí | RVC (venv runtime) | ml/runner | stub con WAV vacío |
| indextts | warm | sí | IndexTTS (venv) | ml/runner | stub con WAV vacío |
| music_gen | warm | sí | MusicGen (venv) | ml/runner | stub con WAV vacío |
| lipsync | warm | sí | Wav2Lip (venv) | ml/runner | stub con MP4 vacío |
| upscaler | warm | sí | Real-ESRGAN | ml/runner | stub con PNG vacío |
| capture3d | installable | sí | nerfstudio | ml/runner | stub con PLY vacío |
| compositor | warm | sí | Natron | ml/runner | stub con EXR vacío |
| qa | warm | sí | FFmpeg + VBench | ml/runner | nivel 1 (stub) |
| roto | warm | sí | SAM 3 | ml/runner | stub con PNG vacío |
| ad_studio | warm | sí | composición ffmpeg+audio | ad_studio | stub con MP4 vacío |
| **reference-local** | **resident** | **sí** | **CPU puro** | **inspiration.py** | **`STUDIO_STUB_REFERENCE_LOCAL=1`** |
| **atlas-offline** | **warm** | **sí** | **CPU puro** | **atlas/packaging.py** | **no; real si la raíz existe** |
| **scene-optimizer** | **warm** | **sí** | **CPU puro** | **cinematography.py** | **`STUDIO_STUB_SCENE_OPTIMIZER=1`** |
| **chrono** | **warm** | **sí** | **proxy CPU** | **physics.py** | **`STUDIO_STUB_CHRONO=1`** |

## Política de invarianza

1. **Ningún método lanza**: toda falla se tipifica en `EngineResult(ok=False, ...)`.
2. **Honestidad de `simulated`**: `False` solo cuando el backend real ejecutó.
3. **Paridad roster/prep-specs**: `set(registry.names()) == set(prep_specs.keys())`.
4. **Manifest coherente**: `engines.json` tiene `build_selected`, `terms_review`,
   `lock_required_before_build`, `runtime_installs_allowed=false`.

## Cómo añadir un motor

1. Implementar `studio/comfy_studio/engines/<name>.py` con los 4 verbos.
2. Registrar en `registry.py` (`build_default_registry`).
3. Añadir `EnginePrepSpec` en `preparedness.py`.
4. Declarar en `upgrades-ultra-version/engines.json`.
5. Tests en `studio/tests/test_engine_<name>.py`.
6. Documentar en `docs/engines/<name>.md`.
