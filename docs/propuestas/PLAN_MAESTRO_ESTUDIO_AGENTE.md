# M6 — Plan Maestro: Estudio-Agente Avanzado

Finding IDs: `M6-SCR-001`..`M6-SCR-040`, `M6-OPS-001`..`M6-OPS-010`.

Estado: PLAN MAESTRO (reemplaza el MVP `ORQUESTACION_PRODUCCION.md`). Integra tres cosas:
(1) el `CATALOGO_MOTORES_OPENSOURCE_2026.md` clasificado con criterio de tradeoffs,
(2) las **capacidades del agente** como pilar de primera clase, y (3) la **orquestación
avanzada** con los 4 ejes afinados por el usuario.

**Premisa fijada por el usuario:** uso **NO comercial** → las licencias NC dejan de ser
bloqueo. Lo único que decide "hornear vs on-the-run" es **VRAM/arranque, madurez y
redundancia**, no la licencia. (Excepción geográfica, no de uso: los modelos Tencent
Hunyuan excluyen UE/UK/KR — se marca, no se usa si el pod corre ahí.) Hardware objetivo:
Blackwell + muchos cores + mucha RAM → optimizar CAPAZ, no ligero; podemos experimentar.

Construye SOBRE M4 (contrato de motores) + M5 (roster de 12 + F7 sonda strict). No es
una capa paralela: usa `Engine`/`EngineResult`/`EngineRegistry`, la `StoryboardGate` y el
`install_gate.py` ya probados.

---

## Parte I — Clasificación del compendio (criterio de tradeoffs)

Regla de decisión: **dónde vive cada cosa y cuándo se carga**, no "bueno vs malo".

### Grupo A — Motores nuevos a HORNEAR (alto valor, no redundante, caben warm)
| Motor | Por qué se hornea | Tier | Riesgo |
|---|---|---|---|
| **SAM 3** (rotoscopia por texto) | El LLM da "el robot" en vez de cajas; lo más agent-friendly del doc | warm | licencia SAM propia + token HF; acceso restringido |
| **VBench** (QA de vídeo generativo) | **Cierra el feedback loop del Eje 2**: un shot evalúa su salida y decide rehacer | warm | umbrales a calibrar con planos propios |
| **Kokoro-82M** (TTS español CPU) | Tapa el hueco real: no había TTS español; RVC solo convierte timbre | warm | sin clonación (narración/borrador) |
| **SeedVR2** (upscale vídeo sin flicker) | Supera a Real-ESRGAN que parpadea; un paso, temporalmente consistente | warm | VRAM 8–24 GB (3B/7B FP8) |
| **Chatterbox / CosyVoice 3** (TTS español + clonación) | Voz de personaje con clonación, español real | warm | Chatterbox marca de agua perceptual (no C2PA) |

### Grupo B — CEREBRO DEL AGENTE (PILAR, horneado) — prioridad del usuario
Un estudio que "hace cualquier cosa" necesita un agente capaz, no solo motores.
| Capacidad | Motor elegido | Por qué ese y no el otro |
|---|---|---|
| **Memoria persistente por proyecto** | **Letta** (antes MemGPT) | Memoria jerárquica core/recall/archival encaja con producciones largas; se elige UNO (Letta vs Mem0) para no duplicar. Letta por el REST loopback limpio y el control de tiers |
| **Compresión de contexto** | **LLMLingua-2** | Poda 2–5x de historiales/salidas de herramienta sin API externa; CPU/0.5–1 GB |
| **Recuperación léxica** | **BM25s** | BM25 en NumPy; complementa la búsqueda vectorial que ya existe en `perception/store.py` |
| **Re-ranking** | **FlashRank** | Reordena top-50→top-3 en CPU (ONNX, sin PyTorch); barato y determinista |

Nota: el cerebro NO es un `Engine` de render — es un subsistema aparte
(`studio/comfy_studio/agent_brain/`) que el loop de orquestación consume. Mismas reglas:
tipado, redacción, loopback, stub-first.

### Grupo C — LIBRERÍA AMPLIA DE SUBMOTORES (Eje 3) — NO hornear, on-the-run
Todo lo demás del compendio vive aquí: Wan-VACE, ConsisID, CameraCtrl/Wan-Fun, Depth
Anything, IC-Light, BiRefNet, LaMa, ProPainter, HunyuanVideo, TRELLIS.2, COLMAP/GLOMAP,
MASt3R, CoTracker3, RAFT, Genesis, Warp, DiffusionLight, StereoCrafter, MMAudio/FoleyCrafter,
YuE2/ACE-Step, 4D-Humans/WHAM/GVHMR, MediaPipe, EMOCA, etc.

No se hornean (inflarían la imagen sin razón): se instalan **on-the-run** vía
`install_gate.py` bajo `PIP_CONSTRAINT`/ComfyUI-Manager cuando un shot los pide. Son la
materia prima del Eje 3 (la librería sobre la que el agente busca y selecciona).

### Grupo D — Diferir por madurez/región (NO por licencia, ya que es NC-ok)
EffectCraft (muy joven; validar render headless antes de `warm`). Tencent Hunyuan*
(marcar exclusión regional UE/UK/KR). Todo lo demás del compendio es experimentable.

---

## Parte II — La librería de plantillas (Eje 3, decisión de arquitectura)

**Decisión (criterio del agente): híbrido gobernado por `STUDIO_DYNAMIC`.**

- **(a) Índice curado versionado** (`config/template_library/`): catálogo de workflows
  ComfyUI (JSON) + metadatos tipados (qué produce, nodos/modelos que requiere, tags de
  estilo, VRAM). Base determinista, offline, auditable, crece por commits. SIEMPRE activa.
- **(b) Descubrimiento dinámico** (solo con `STUDIO_DYNAMIC=1`): el agente consulta
  fuentes comunitarias (ComfyUI-Manager, repos de workflows) para ampliar el índice en
  runtime. Da la amplitud "anti-visión-de-túnel" que pide el usuario, pero gobernada por
  el gate y respetando loopback + la regla n.º 6 (instalar solo vía Manager+PIP_CONSTRAINT).

**El motor de selección** (`TemplateSelector`) hace, para un shot fuera de lo común:
1. **buscar** en el índice (léxico BM25s + semántico) candidatos por intención/estilo;
2. **cruzar** con las capacidades reales del motor (`ComfyUIEngine.list()` / `/object_info`
   + add-ons Blender) → descarta plantillas cuyos nodos/modelos no están y no se pueden
   instalar en el perfil actual;
3. **re-rankear** (FlashRank) los viables por aptitud a la intención;
4. **adaptar** el JSON (modelos, resoluciones, parámetros) al shot;
5. **encadenar** varias plantillas cuando un shot complejo lo exija (p.ej. generar →
   depth → relight → upscale), produciendo un sub-grafo.

Esto es exactamente lo que el usuario pidió: ni pocas plantillas fijas, ni inventar grafos
a ciegas — buscar/seleccionar en una librería amplia, cruzar con lo instalado, adaptar y
encadenar.

---

## Parte III — Orquestación avanzada (los 4 ejes)

### Base: Eje 4 — LangGraph
El lazo de producción es un **grafo LangGraph** con estado persistente (checkpointer
SQLite, reanudable tras corte — encaja con el patrón del repo). Nodos = fases/motores;
aristas condicionales = decisiones. Es biblioteca (sin segundo proceso), reutiliza los 19
tools y los motores como nodos. El estado es un `ProductionState` tipado (Pydantic) que
persiste en el proyecto.

### Eje 1 — Planificación jerárquica + replanificación ESCALONADA
- **Descomposición jerárquica:** idea → **secuencias** → **shots** (3–7s) → **capas por
  shot** (gen, depth, relight, comp). Cada nivel es un nodo del grafo que se expande.
- **Replanificación escalonada** cuando un shot falla su `acceptance` (orden exacto pedido):
  1. **reintento local** — mismos motor/plantilla, otra seed/params;
  2. **cambio de plantilla** — el `TemplateSelector` elige otra plantilla del índice;
  3. **escalar al planner global** — solo si 1 y 2 fallan: el planner LLM reconsidera el
     shot (o la secuencia) entero.
  Cada escalón se registra en el `ProductionState`; el salto de escalón es una arista
  condicional del grafo.

### Eje 2 — Ejecución adaptativa con FEEDBACK LOOP
- **Grafo adaptativo:** shots independientes en paralelo (aristas sin dependencia);
  dependientes en orden topológico (capture3d→blender→heavy_video→compositor).
- **Feedback loop:** tras ejecutar un shot, un nodo **evaluador** (VBench + checks
  FFmpeg: flicker/freeze/negro) puntúa la salida contra el `acceptance` del shot. Si no
  pasa → dispara la replanificación escalonada del Eje 1. Si pasa → avanza.
- **Bifurcación opcional:** probar 2 enfoques de un shot caro y quedarse con el de mejor
  score VBench (gobernado por cost cap).

### Eje 3 — Inteligencia creativa (el `TemplateSelector` de la Parte II)
Integrado como el nodo que, dado un shot, produce el sub-grafo de motores+plantillas a
ejecutar, sobre la librería amplia.

### Puerta de gasto (sin cambios de M4)
El borrador barato entra en la `StoryboardGate` existente; sin aprobación humana no se
gasta GPU pesada. El cost cap se reparte por shot vía `EngineRunSpec.cost_cap`.

---

## Parte IV — Orden de ejecución (fases gate-green, stub-first, CI verde)

Disciplina en todas: stub-first (`STUDIO_STUB_*`), `EngineResult`/estado tipado, redacción,
loopback, fix-repo-not-test, cada fase un commit verde (Rust + Python + build CI).

**Bloque 1 — Cerebro del agente (grupo B, pilar)**
| # | Entregable |
|---|---|
| 1 | `agent_brain/` scaffold + contrato tipado + stubs; registry del cerebro |
| 2 | LLMLingua-2 (compresión) horneado + wire, stub CI |
| 3 | BM25s + FlashRank (recuperación/re-rank) horneado + wire |
| 4 | Letta (memoria persistente) horneado + wire loopback, stub CI |

**Bloque 2 — Motores nuevos (grupo A)**
| # | Entregable |
|---|---|
| 5 | VBench como motor `qa` (cierra el feedback loop) + wire |
| 6 | Kokoro (TTS español) + Chatterbox/CosyVoice3 (clonación) en `audio`/`voice_clone` |
| 7 | SeedVR2 en `upscaler` (vídeo sin flicker) |
| 8 | SAM 3 como motor `roto` (rotoscopia por texto) |

**Bloque 3 — Librería de plantillas (Eje 3)**
| # | Entregable |
|---|---|
| 9 | `config/template_library/` + esquema de metadatos tipado; índice curado inicial |
| 10 | `TemplateSelector`: buscar(BM25s+sem)→cruzar(`list`/object_info)→rerank(FlashRank)→adaptar JSON |
| 11 | Encadenado de plantillas (sub-grafos) + descubrimiento dinámico gobernado por `STUDIO_DYNAMIC` |

**Bloque 4 — Orquestación avanzada (ejes 1/2/4 sobre LangGraph)**
| # | Entregable |
|---|---|
| 12 | LangGraph base: `ProductionState` tipado + checkpointer SQLite + nodos esqueleto |
| 13 | Planificación jerárquica (idea→secuencias→shots→capas), planner LLM con stub determinista |
| 14 | Ejecución adaptativa (paralelo/topológico) enlazando motores como nodos |
| 15 | Feedback loop (evaluador VBench+FFmpeg) + replanificación escalonada (local→plantilla→global) |
| 16 | Ensamblaje (editor+compositor) + reel final descargable |

**Bloque 5 — Superficie e integración**
| # | Entregable |
|---|---|
| 17 | API + tools del agente: `plan_production`, `get_plan`, `approve_plan`, `production_status` |
| 18 | strict-check S15 `orchestration_ready` + S16 `agent_brain_ready` |
| 19 | Dockerfile: hornear grupo A+B pinned (cada pin verificado por build CI, incremental) |
| 20 | Doc de cierre + actualización del `EngineRegistry`/health para los nuevos motores |

---

## Guardrails (intactos en todo)
Cost cap repartido por shot · redacción de stderr/secretos · loopback-only · `STUDIO_DYNAMIC`
gobierna submotores/modelos/descubrimiento dinámico (nunca los motores horneados) ·
`StoryboardGate` como única puerta al gasto pesado · validación en Blackwell con pesos
reales diferida hasta que el usuario autorice GPU/vast.ai.

## El estudio, en una línea
Con M6 el agente deja de ser "12 herramientas sueltas": tiene un **cerebro** (memoria,
contexto, recuperación), una **librería amplia** sobre la que razona sin visión de túnel,
y un **lazo adaptativo** (LangGraph) que planifica jerárquicamente, ejecuta con feedback y
replanifica escalonadamente. Idea → plan → borrador aprobado → producción con auto-corrección → reel.
