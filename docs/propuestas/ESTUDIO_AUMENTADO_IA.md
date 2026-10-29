# M5 — Estudio aumentado por IA: roster completo de motores

Finding IDs: `M5-SCR-001`..`M5-SCR-020`, `M5-OPS-001`..`M5-OPS-006`.

Estado: PLAN (ejecución por fases, cada motor entra gate-green). Hardware objetivo:
Blackwell NVIDIA + muchos cores + mucha RAM → optimizar CAPAZ, no ligero.

## Principio (la visión del usuario)

NO es un estudio común. Es un **estudio aumentado por IA con TODAS las features
(motores) ya habilitadas de fábrica**. Lo único que el agente hace en runtime:
  - añadir SUB-motores que falten (nodos ComfyUI, add-ons Blender, códecs),
  - traer MODELOS/LoRAs/checkpoints/voces,
  - DISEÑAR workflows y convertir ideas abstractas en producción cinematográfica.

Los MOTORES (servicios) están horneados y calientes; el agente nunca espera a
instalar uno (eso dejaría la GPU ociosa). Capa de 3 niveles (ver M4 doc):
MOTOR = horneado/caliente · SUB-MOTOR = on the run · MODELO = on the run.

## Roster completo de motores (todos horneados, enabled out of the box)

| Motor | Rol en la producción | Tier | Estado |
|---|---|---|---|
| ComfyUI | gen imagen/vídeo/3D, LoRA, relight, upscale | resident | ✅ wired (M4) |
| Blender | 3D, cámara, física, VFX, AR, export USD/glTF/FBX | warm | ✅ wired (M4) |
| editor (Remotion) | timeline por código, títulos, transiciones, corte final | warm | ✅ wired (M4) |
| ffmpeg (NVENC) | cut/concat/grade/formato/master | warm | ✅ wired |
| heavy_video | level-up del render (Minimax/best API) | installable | ⬜ wire M5 |
| audio | tts / stt / música / lipsync | warm | ⬜ wire M5 |
| **voice_clone (RVC)** | clonar voz, cantar, timbre del personaje | warm | ⬜ nuevo M5 |
| **music_gen** | pista original (MusicGen/Stable Audio) | warm | ⬜ nuevo M5 |
| **lipsync** | sincronía labial vídeo+audio | warm | ⬜ nuevo M5 |
| **upscaler** | Real-ESRGAN / super-res de salida | warm | ⬜ nuevo M5 |
| **capture3d (Nerfstudio)** | foto/vídeo real → escena 3D (NeRF/Gaussian Splatting) | installable | ⬜ nuevo M5 |
| **compositor (Natron)** | componer capas de varios motores en un frame | warm | ⬜ nuevo M5 |
| comfyui_manager | instalar nodos (sub-motores) bajo PIP_CONSTRAINT | — | ⬜ wire M5 |

## Qué significa "wire" (gate-green, sin trucos)

Cada motor, al cablearse:
1. implementa el contrato de 4 verbos (`list/install/verify/run`) → `EngineResult` tipado;
2. mantiene su modo stub (`STUDIO_STUB_<NAME>=1`) para CI sin GPU/binario;
3. backend real aislado en subproceso, timeout, redacción de secretos;
4. secretos (API keys) leídos en la llamada, nunca persistidos/registrados;
5. test Python verde + ruff + (si toca Rust) cargo fmt/clippy -D/test;
6. entra al `registry` con su tier; `health.py` lo cubre; strict-check lo observa.

## Orden de ejecución (cada fase = 1 motor, commit gate-green)

| # | Motor | Entregable + prueba |
|---|---|---|
| 1 | heavy_video `_call_api` | cliente API real (Minimax), stub en CI, cost cap; test |
| 2 | audio tts/stt real | delega a IndexTTS/worker existentes; test |
| 3 | voice_clone (RVC) | engine nuevo, 4 verbos, stub; test |
| 4 | music_gen | engine nuevo (MusicGen/Stable Audio), stub; test |
| 5 | lipsync | engine nuevo, stub; test |
| 6 | upscaler | engine nuevo (Real-ESRGAN vía ComfyUI node); test |
| 7 | comfyui_manager install | cablear endpoint Manager bajo PIP_CONSTRAINT; test |
| 8 | blender add-on enable | cablear enable/pip de add-on fijado; test |
| 9 | capture3d (Nerfstudio) | engine nuevo installable, stub; test |
| 10 | compositor (Natron) | engine nuevo, CLI render, stub; test |
| 11 | registry + health | registrar todos; strict-check S-probe por motor (F7) |
| 12 | Dockerfile + constraints | hornear binarios/deps de los motores nuevos, pinned |

Guardrails intactos en todas: cost cap, redacción, loopback-only, default
determinista (`STUDIO_DYNAMIC` gobierna solo sub-motores/modelos, no los motores).

## El agente, en una línea

El agente no instala motores: **diseña el workflow** (idea abstracta → shot list
→ qué motor por shot → ensamblar) y, cuando un shot necesita algo que el motor
caliente no tiene aún, **añade el sub-motor/modelo/códec** on the run. Los motores
ya están. Esa es la diferencia entre un estudio común y uno aumentado por IA.
