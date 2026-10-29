# M4 — Reel Studio: motores estables + agente dinámico

Finding IDs: `M4-SCR-001`..`M4-SCR-012` (producción), `M4-OPS-001`..`M4-OPS-004` (operación/VRAM/CI).

Estado: PROPUESTA (plan primero, ejecución por fases). No cambia código hasta aprobar.

---

## 1. El objetivo en una línea

Un creador de reels (salida ≤ 1 min) que puede mutar de gráficos abstractos a
personaje fotorreal a física 3D — **muy capaz pero muy estable**. Capaz donde
importa la calidad; estable donde importa el coste y el fallo.

La regla que lo hace posible: **lo que deja la GPU ociosa debe estar caliente
antes de arrancar el reel; lo que se solapa con el trabajo del agente se trae
en caliente (on the run).**

---

## 2. Las tres capas (decisión de diseño central)

| Capa | Ejemplo | Dónde vive | Coste de instalar | Quién decide |
|---|---|---|---|---|
| **Motor** | ComfyUI, Blender, editor, Hyperframes | sandbox (caliente) | minutos (GPU ociosa) | horneado en imagen; instalar es raro |
| **Submotor** | nodo ComfyUI, add-on Blender | dentro de un motor | segundos (host ya caliente) | **agente, on the run** |
| **Modelo** | checkpoint, LoRA, voz | `/workspace` | se solapa con razonar | agente, on the run |

- Los **motores** son pocos y fijos: el agente casi nunca los instala.
- Los **submotores** son muchos y dinámicos: el agente vive aquí la mayor parte
  del tiempo. Seguros porque el motor host ya está caliente (sin 10 min ociosos).
- Los **modelos** siempre se traen on the run: el fetch se solapa con el razonamiento.

### Escotilla de escape (no es una jaula)
El agente **puede** instalar un motor nuevo a mitad de ejecución cuando el set
caliente no logra la tarea — porque la meta es terminar. Pero es el camino lento
y deliberado, tras una decisión razonada (ver §6). Un motor instalado que
demuestra valor se **promueve** al set caliente en el siguiente build de imagen.

---

## 3. Qué ya existe (no reinventar)

El patrón "motor = subproceso aislado con respuesta tipada" **ya está probado**:

| Pieza existente | Qué aporta al contrato de motor |
|---|---|
| `studio/comfy_studio/blender.py` (`BlenderRunner`) | subproceso CLI aislado (GPL + fallos), modos `render`/`export`, `STUDIO_STUB_BLENDER`, timeout, redacción, log propio |
| `studio/comfy_studio/ml/runner.py` (`run_worker`) | subproceso con respuesta **tipada**, nunca `shell=True`, timeouts por subcomando, `require_real` vs fallback simulado |
| `studio/comfy_studio/ml/probe.py` (`probe_capabilities`) | **health-check**: qué puede el entorno antes de usarlo |
| `studio/comfy_studio/comfyui_client.py` | `GET /object_info` (listar capacidades), `POST /prompt`, Manager para nodos |
| `studio/comfy_studio/agent/tools.py` | registro de las 19 tools; es donde entran las nuevas verbos de motor |
| `config/constraints.txt`, `config/nodes.lock` | fijado de versiones (regla 6 de `AGENTS.md`) |
| `comfyctl/src/strict.rs` | sondas strict-check que fallan el pod ruidosamente |

**Conclusión:** el contrato de motor es una *generalización* de `blender.py` +
`ml/runner.py`, no código nuevo desde cero.

---

## 4. El contrato de motor (4 verbos) — `M4-SCR-001`

Cada motor caliente expone cuatro verbos uniformes para que el agente los vea
como herramientas iguales:

| Verbo | ComfyUI | Blender | Para qué |
|---|---|---|---|
| `list` | `GET /object_info` | add-ons activos | el agente ve qué submotores hay |
| `install` | Manager (nodo) bajo `PIP_CONSTRAINT` | enable/pip add-on fijado | traer el submotor que falta (rápido) |
| `verify` | re-leer object_info | import check | confirmar que cargó antes de usar |
| `run` | `POST /prompt` | `--python` script | ejecutar el trabajo |

Interfaz Python única (`studio/comfy_studio/engines/base.py`, nuevo), dos+
implementaciones. El panel y `/studio/api/chat` **no cambian**: mismo endpoint,
mismos eventos SSE. Toda respuesta es tipada (como `MlWorkerResponse`), toda
falla lleva stderr redactado, todo corre con timeout y tope de coste.

---

## 5. El roster caliente inicial — `M4-SCR-002`

| Motor | Estado hoy | Cubre etapa del reel |
|---|---|---|
| ComfyUI | ✅ residente | storyboard, dataset, imagen→3D, relight, upscale |
| Blender | ✅ runner existe | escena, CGI, VFX, física |
| ffmpeg (NVENC) | ✅ | concat, formato, master |
| Editor (Remotion **o** OTIO+ffmpeg) | ❌ **falta** | merge del borrador, corte final |
| Vídeo pesado (Minimax/API) | ⚠️ no cableado | level-up del render de Blender |
| Audio (TTS/STT/música/lipsync) | ⚠️ parcial (TTS) | acabado por escena |

Dos gaps reales de motor: **editor** (aparece dos veces en el pipeline) y
**cliente de vídeo pesado**. El resto son submotores/modelos on the run.

### VRAM: caliente ≠ todos encendidos — `M4-OPS-001`
No caben todos en GPU a la vez. Dos niveles:

| Nivel | Estado | Coste de arranque |
|---|---|---|
| Residente | siempre en GPU (ComfyUI) | cero |
| Caliente intercambiable | instalado + sano, carga al primer uso | segundos (load), no minutos (install) |

Un perfil (`MODEL_PROFILE`) decide qué motor está encendido. Instalado en build,
cargado en segundos en ejecución. **Nunca instalado en ejecución.**

---

## 6. La puerta de decisión de instalar motor — `M4-SCR-003`

Para que instalar sea deliberado (no 10 min ociosos por accidente), el agente
razona antes:

1. ¿El set caliente realmente falló esta tarea? (no instalar lo que ya tienes)
2. ¿El coste de GPU ociosa vale frente al valor de la tarea?
3. ¿Entra en el sandbox? (loopback, redacción, tope de coste)
4. Si funcionó y repite valor → candidato a hornear caliente en el próximo build.

---

## 7. El pipeline del reel sobre estas capas

```
IDEA + refs (mp4/mov/mp3/3d/youtube)
 PRE   → guion → storyboard borrador (modelos rápidos) → [APROBACIÓN humana]
 BUILD → assets (guardados en librería del proyecto, reusados en toda escena)
 SHOOT → escenas Blender → level-up con vídeo pesado   (por escena, rehacible)
 FINISH→ relight / lipsync / música / voz / upscale     (por escena)
 POST  → editor: corte, color, transiciones → reel → descarga
         ↑ cada motor = servicio caliente expuesto como una tool
           cada nodo/add-on/LoRA = traído on the run
           cada asset = recordado para la siguiente escena y el siguiente reel
```

La **puerta de aprobación tras el storyboard** es lo que hace asequible el CGI
pesado: solo pagas BUILD→POST sobre una idea ya aprobada.

### La capa que falta para ser estudio (no solo generador de clips) — `M4-SCR-004`
**Memoria entre escenas:** el mismo personaje, coche y voz deben sobrevivir de la
escena 1 a la 7 y de un reel al siguiente. Librería de assets por proyecto +
continuidad. Sin esto hay deriva del personaje.

---

## 8. Estabilidad: cómo "top level" no se vuelve "fallo constante"

| Riesgo | Mitigación (ya en el repo o derivada) |
|---|---|
| Deps pesadas rompen torch | fijado duro en `constraints.txt`/`nodes.lock`; CI valida; default sin cambios |
| Motor extra quiere red/telemetría | env-disable; base URL solo loopback |
| Fuga de secreto por logs del motor | todo log pasa `SecretRedactor` |
| Skill/instalación de código arbitrario | submotores declarativos; sin Python crudo |
| No determinismo vs cultura de tests | mantener los modos stub (`STUDIO_STUB_*`) en cada motor |
| Instalar motor = GPU ociosa | puerta de decisión §6; caliente es el default |
| Motor roto arranca el pod | sonda strict-check nueva por motor (`comfyctl/src/strict.rs`) |

El interruptor maestro: `STUDIO_DYNAMIC=1`. Off = pod determinista de hoy.
On = el agente puede componer grafos, instalar submotores, traer modelos,
y (con la puerta §6) instalar motores. Un build malo vuelve con una env var.

---

## 9. Plan por fases (orden de valor, cada fase entrega algo probado)

| Fase | Entregable | Prueba de hecho |
|---|---|---|
| F0 | este doc + contrato `engines/base.py` (interfaz + stub) | `pytest` del contrato pasa |
| F1 | motor **editor** cableado (merge storyboard + corte final) | un reel borrador se ensambla de clips |
| F2 | `build_graph` + 4 verbos en ComfyUI (list/install/verify/run) | agente compone un grafo no-plantilla y corre |
| F3 | puerta storyboard + aprobación humana | borrador rápido, espera sí, luego BUILD |
| F4 | librería de assets / continuidad por proyecto | personaje igual en 2 escenas |
| F5 | cliente vídeo pesado + acabado por escena (lipsync/música/STT) | una escena sube de nivel |
| F6 | puerta de instalación de motor + promoción a caliente | motor instalado a demanda, con tope |
| F7 | sondas strict-check por motor + perfiles VRAM | pod falla ruidoso si un motor no está sano |

Cada fase respeta las 6 reglas de `AGENTS.md` y mantiene el default determinista.

---

## 10. Decisión abierta (requiere el humano)

**Motor editor: Remotion vs OTIO+ffmpeg.**
- Remotion: timeline por código, títulos/motion graphics fuertes; pide Node + Chromium headless.
- OTIO+ffmpeg: más ligero, menos capaz en motion graphics.

Se decide al empezar F1. El resto del plan no depende de cuál.

---

## 11. Estado de construcción (actualizado por implementación)

Evidencia: `ruff` limpio + `pytest -k m4` → **77 tests pasan** (0.69s). Suite
completa `studio/tests`: 322/323 (el único fallo es un bug previo de UTF-8 en
Windows en `ml/worker.py`, ajeno a M4).

| Fase | Entregable | Estado | Evidencia |
|---|---|---|---|
| F0 | contrato `engines/__init__.py` + `base.py` | ✅ hecho | test_m4_engine_contract.py (8) |
| F1 | motor editor (stub-first, agnóstico de backend) | ✅ esqueleto | test_m4_editor_engine.py (6) |
| F2 | adaptador ComfyUI (4 verbos sobre el cliente) | ✅ hecho | test_m4_comfyui_engine.py (6) |
| — | registro de motores | ✅ hecho | test_m4_engine_registry.py (5) |
| — | adaptador Blender (envuelve runner existente) | ✅ hecho | test_m4_blender_engine.py (7) |
| — | sonda de salud / strict-check (fail-loud) | ✅ hecho | test_m4_engine_health.py (7) |
| F3 | puerta de aprobación del storyboard | ✅ hecho | test_m4_storyboard_gate.py (7) |
| F4 | librería de assets / continuidad | ✅ hecho | test_m4_asset_library.py (7) |
| F5 | motor de vídeo pesado (API, cost cap) | ✅ esqueleto | test_m4_heavy_video.py (9) |
| F5 | motor de audio (tts/stt/music/lipsync) | ✅ esqueleto | test_m4_audio_engine.py (9) |
| F6 | puerta de decisión de instalación + promoción | ✅ hecho | test_m4_install_gate.py (8) |

### Lo que falta (deliberadamente diferido)
- **Decisión humana**: backend del editor (Remotion vs OTIO+ffmpeg) — F1.
- **Cableado de backend real** (fuera de stub): editor `_render_backend`,
  vídeo pesado `_call_api`, audio real (IndexTTS/worker), ComfyUI-Manager
  `install`, enable de add-ons de Blender. Todos fallan TIPADO hoy, nunca fingen.
- **F7**: sonda por motor en `comfyctl/src/strict.rs` que consuma
  `engines/health.py::health_report` al arrancar el pod.

Cada backend real entra con sus deps fijadas (`constraints.txt`/`nodes.lock`),
su modo stub intacto y su sonda strict-check — manteniendo el default
determinista y "capaz pero estable".
