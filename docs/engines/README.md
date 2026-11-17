# Motores del estudio

El roster activo se obtiene de `build_default_registry()` en [`studio/comfy_studio/engines/registry.py`](../../studio/comfy_studio/engines/registry.py). En el snapshot revisado contiene 20 nombres. `wired=true` es una declaración de ruta conectada al registro; **no** prueba que el software esté instalado, que existan pesos, que el hardware sea compatible ni que haya una ejecución validada. Para esos hechos consulta `engines/preparedness.py`, el resultado del verbo `verify` y una ejecución/artifact de la ruta correspondiente.

| Motor | Tier | Ruta declarada / condición importante |
|---|---|---|
| `ad_studio` | warm | Brief, guion, storyboard y variantes con los componentes locales configurados; el render depende de FFmpeg/TTS/música disponibles. |
| `audio` | warm | TTS Kokoro y worker ASR; STT real necesita el paquete y pesos faster-whisper. |
| `blender` | warm | Blender 5.2.4 headless fijado en la imagen; los assets/pesos externos no están garantizados. |
| `capture3d` | installable | Ruta Nerfstudio; preparación y requisitos de runtime/hardware se verifican por separado. |
| `chrono` | warm | Proxy cinemático CPU determinista de `physics.py`; **no** es el solver Project Chrono. |
| `comfyui` | resident | Cliente HTTP al ComfyUI del pod por loopback; GPU/receta se verifica al ejecutar. |
| `compositor` | warm | Integración de composición Natron preparada por la imagen; render no acreditado por el registro. |
| `editor` | warm | Remotion local mediante `npx`; su paquete/lock y los tests del editor son la evidencia relevante. |
| `ffmpeg` | warm | Binarios locales `ffmpeg`/`ffprobe`, usados también por otros motores. |
| `heavy_video` | installable | Cliente MiniMax configurable mediante `STUDIO_HEAVY_VIDEO_*`; no implica una llamada facturada automática. |
| `indextts` | warm | Entorno aislado y pesos por tarea; revisar disponibilidad antes de prometer clonación de voz. |
| `lipsync` | warm | Código preparado; el checkpoint Wav2Lip se comprueba aparte y puede faltar. |
| `llm_server` | warm | `llama-server` CPU incluido; pesos GGUF son del usuario y el proceso no se inicia/supervisa automáticamente. |
| `music_gen` | warm | AudioCraft/servicios asociados; dependencias pesadas y pesos no equivalen a disponibilidad inmediata. |
| `qa` | warm | FFmpeg local; VBench necesita el entorno aislado opcional. |
| `reference-local` | warm | Análisis local de cortes/beats/OCR/cámara en CPU; es heurístico y no reemplaza un análisis semántico VLM. |
| `roto` | warm | Ruta SAM 3; los pesos HF-gated deben estar disponibles y autorizados. |
| `scene-optimizer` | warm | Validación CPU de composición y cámara; no demuestra calidad de render neural. |
| `upscaler` | warm | Ruta Real-ESRGAN; comprobar el modelo exacto y la receta antes de declarar listo. |
| `voice_clone` | warm | RVC puede requerir fairseq, que no se hornea en la imagen; IndexTTS es una ruta separada. |

La taxonomía de `preparedness.py` separa `installed`, integración, pesos, hardware, modelo cargado y ejecución validada. Un check de salud no simula una ejecución exitosa ni carga una GPU. El estado del roster y el manifiesto de motores seleccionados `upgrades-ultra-version/engines.json` son contratos distintos: el primero describe herramientas registradas en Studio; el segundo aplica el gate de build A07.

`generate_music` usa el flujo nativo de ComfyUI `ace_step_music`, descrito en
[`ace-step.md`](ace-step.md). El motor del registro `music_gen` es una vía
separada; no representa la implementación activa de esa herramienta.

La compilación CPU de briefs aprobados a planes DAG está descrita en
[`mission-compiler.md`](mission-compiler.md). Es parte de la orquestación de
producciones y no es un motor de render del registro anterior.

La memoria durable de decisiones y recetas del agente vive en ProjectStore y no
es un proceso de engine separado; su alcance, límites y lock están en
[`production-memory.md`](production-memory.md).

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
