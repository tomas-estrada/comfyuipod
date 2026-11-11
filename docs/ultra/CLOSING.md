# ComfyUIPod Ultra — Informe de cierre pre-GPU

> **Cierre histórico pre-GPU.** Este estado corresponde al corte indicado abajo,
> antes de la demo W26; no describe el estado actual. W26 midió una generación
> SDXL real y acotada, y dos hallazgos de operaciones recibieron fixes posteriores
> en `main`. Consulta [el reporte W26](W26_GPU_RESULTS.md); falta repetir la
> verificación en una imagen actual y completar las demás recetas.

Estado al cierre de ese corte: **BLOCKED_PRE_GPU** (entrega parcial sólida; el
resto del backlog de capacidades quedó pendiente y visible).

Fecha: 2026-11-05 UTC. Revisión final de main: `1cbfb48`.

## Qué significa este estado

El plan Ultra (`upgrades-ultra-version/`) define una misión extensa: 34
capacidades (C01–C34), 14 programas (P01–P14) y 27 paquetes de trabajo
(W00–W26). Esta sesión completó e integró la **columna vertebral de
infraestructura y orquestación**, la **primera capa de capacidades** y una
**imagen candidata reproducible con el atlas offline incluido**. El grueso de
las capacidades creativas sigue pendiente y se declara con precisión abajo.

No se alquiló GPU ni se usaron APIs facturadas. Todo lo verificado es a nivel de
**contrato en CPU (L1 / L2-parcial)**; ninguna capacidad neuronal se validó en
GPU (eso es L3/L4 → W26).

## Integrado en main (verificado, sin mocks)

| W | Paquete | PRs | Evidencia | Nivel |
|---|---------|-----|-----------|-------|
| W00 | Baseline + reconciliación de PRs | — | #58, #64 integrados; #59 diferida | L1 |
| W01 | Integridad transaccional de modelos | #65 | caché corrupta, `--revision`, EXDEV transaccional + regresiones | L1 |
| W02 | Hardware, sizing y recetas | #66, #67 | `hardware_detect` (NVML/cgroup), recetas honestas (no simulado→medido), start_gate sin Fase-3 | L1 |
| W03 | Lease global de GPU | #68, #71, #75 | árbitro entre procesos (fencing/huérfanos/admisión), ensayo multiproceso, enganche al executor | L1 |
| W04 | Contratos, persistencia y DAG | #69 | lineage con invalidación selectiva (A05), migraciones de schema | L1 |
| W05 | Archivo semántico e ingesta | #72 | dedup, aislamiento de corruptos, segmentos con evidencia (A08) | L1 |
| W07 | Release gate de engines | #70 | validador de `engines.json` (coherencia vs. bloqueos de release) | L1 |
| W08 | Base AV, recetas y selección | #74, #76 | catálogo LTX/Wan/Qwen/SeedVR2 + selector por VRAM real + enlace planner→receta | L1 |
| W09 | Atlas offline del Valle de Aburrá | #63, #78 | 1230 imágenes georreferenciadas + atribuciones/hashes **físicamente en la imagen** | L1/L2 |

Cadena conectada y probada end-to-end en CPU:
`planner → bind_recipes (receta por VRAM real) → template ejecutable →
executor (orden topológico) → LeasedEngineRunner (lease GPU) → engine`.

~138 tests nuevos. Suite completa verde en CI Linux (837→913+ tests).

## Imagen candidata (W25 parcial)

- **Tag:** `ghcr.io/tomas-estrada/comfyuipod:sha-1cbfb48`
- **Digest `:latest`:** `sha256:6d0d84e06af370b9ebfc7a103ca6e243a62f743fba842428d71ecda2c7cd1ebf`
- **Build:** run 41991452471, verde (16m31s), construida y publicada por el
  workflow GHCR.
- **Smoke:** unitario + full-stack E2E (nginx+status+portal+supervisor+SIGTERM)
  + `MODEL_PROFILE=studio` E2E.
- **Contiene:** W00–W09 + atlas offline físico + cadena de orquestación.
- `requirements.lock` subido como artefacto de reproducibilidad.

Es una imagen candidata reproducible con digest real. **No** es "Ultra validada":
no se ejecutó generación neuronal en GPU.

## Bloqueos y pendientes (visibles, no degradados)

### Capacidades del backlog aún no implementadas
- **W06** Inspiración activa → receta (C02).
- **W10** Activos 3D, partes, ajuste inverso (C09, C10) — Trellis/PartCrafter/Mitsuba.
- **W11** Cámaras, reconstrucción, ensayo de rodaje (C05, C07, C08, C20).
- **W12** Física, vehículos, productos articulados (C10, C15, C18, C19).
- **W13** Movimiento humano, rigging, actuación (C11–C15).
- **W14** Avatar facial editable (C16).
- **W15** Edición causal, matting, capas (C24, C28, C29).
- **W16** Reiluminación, materiales, restilización (C21, C22).
- **W17** Memoria espacial, multishot, 4D (C06, C17).
- **W18/W19** Foley/separación/sonido causal y música/voz español (C16, C25–C27).
- **W20** Montaje, composición, color, entregables 4K (C02, C19, C24, C30–C32).
- **W21** Frontend agéntico + cliente multiplataforma (C12).
- **W22** Director autónomo integrado (C03, C04, C08, C20).
- **W23** Investigador autónomo y aprendizaje selectivo (C33, C34).

### Sub-items de los paquetes integrados
- Completar la evidencia A07 de las 58 rutas `build_selected` en `engines.json`;
  el verificador ahora enumera faltantes de pin, locks, redistribución, notices
  e implementación. Verificar de nuevo el conteo al cerrar cada familia.
- Protocolo uniforme `describe`/`execute`/`cancel`/`unload` sobre `Engine` base.
- LangGraph real ejecutando la ruta efectiva en la imagen (hoy con fallback).
- Provider profiles LLM/VLM intercambiables con contract server (A06).
- Enganche del lineage al replan del executor.
- Extracción semántica de ingesta con los backends reales (perception/asr) y
  pesos.

### Fuera de alcance de esta fase (por diseño)
- **W26 / validación GPU (L3-L4):** requiere la campaña GPU autorizada —
  alquiler de pod, descarga de pesos, medición real de VRAM y calidad. No se
  ejecuta ahora. Los tests GPU deben quedar seleccionables y saltados
  explícitamente, nunca convertidos en un pass con mocks.

## Protocolo de validación GPU (preparado, no ejecutado)

Cuando se autorice la campaña GPU (W26):
1. Rentar un pod con la imagen `sha-1cbfb48` (o la vigente), GPU RTX PRO 6000
   Blackwell 96 GB, perfil RAM 128/256, disco derivado por
   `scripts/storage_requirement.py`.
2. Fijar presupuesto, reglas de teardown y límite de gasto antes de arrancar.
3. Verificar presencia real de pesos vía ComfyUI `/object_info` antes de
   producir (no inferir readiness de `/healthz`).
4. Medir el pico real de VRAM/RAM por receta y sustituir las estimaciones de
   `av_recipes` por mediciones (`recipe_for_template`).
5. Ejecutar las demostraciones D01–D05 como escenarios reproducibles.
6. Destruir el pod al terminar y verificar 0 instancias.

## Honestidad final

Esta entrega acredita **implementación preparada** de la base y una capa de
capacidades, con imagen candidata reproducible. **No** acredita capacidad
neuronal validada. El éxito científico posterior se decide por resultados
medidos en GPU, no por cantidad de módulos. Las capacidades obligatorias no
implementadas quedan **bloqueadas y visibles**, no etiquetadas como completas.

---

## Actualización — capa de capacidades completa a nivel contrato (2026-11-05)

Tras el informe inicial, se completó e integró TODA la capa de capacidades del
backlog (W05–W23) a nivel **contrato L1**, con tests reales y cero mocks. Cada
capacidad entró como un módulo verificable con su acceptance-check, respetando
el orden de dependencias y la distinción preparado (L1/L2) vs validado-GPU (L3).

### Capacidades integradas (módulo + acceptance)

| W | Módulo | Acceptance | Capacidades |
|---|--------|-----------|-------------|
| W05 | `ingest.py` | A08 | C01 |
| W06 | `inspiration.py` | A09 | C02 |
| W08 | `av_recipes.py` + `recipe_binding.py` | A10 | C23, C28, C30 |
| W09 | `atlas/` (PR #63) | A11 | C07 |
| W10 | `assets3d.py` | A12 | C09, C10 |
| W11 | `cinematography.py` | A13 | C05, C07, C08, C20 |
| W12 | `physics.py` | A14 | C10, C15, C18, C19 |
| W13 | `motion.py` | A15 | C11–C15 |
| W14 | `face.py` | A16 | C16 |
| W15 | `layers.py` | A17 | C24, C28, C29 |
| W16 | `relight.py` | A18 | C21, C22 |
| W17 | `spatial_memory.py` | A19 | C06, C17 |
| W18 | `audio.py` | A20 | C25 |
| W19 | `voice.py` | A21 | C16, C26, C27 |
| W20 | `delivery_4k.py` | A22 | C02, C19, C24, C30–C32 |
| W21 | `studio_client.py` | A23 | C12 |
| W22 | `director.py` | A24 | C03, C04, C08, C20 |
| W23 | `researcher.py` | A26 | C33, C34 |

Más la infraestructura W00–W04, W07 y la cadena conectada
`planner → bind_recipes → executor → LeasedEngineRunner → lease`.

### Qué significa "a nivel contrato L1"

Cada módulo implementa el CONTRATO verificable de su capacidad: tipos, estados
honestos, invariantes del plan (procedencia observed/inferred, causalidad
selectiva, presupuestos, idempotencia, no-promoción-desde-mocks, licencias que
viajan con el proyecto, etc.), probados en CPU. La ejecución neuronal real
(calidad de generación, kernels CUDA, medición de VRAM) es **L3 y pertenece a la
campaña GPU (W26)**, que sigue fuera de esta fase por diseño.

### Lo que todavía queda para `ACCEPTED_PRE_GPU` pleno

1. **Sub-items de los paquetes** (no capacidades nuevas, sino profundizar los
   contratos hasta L2 con los engines reales):
   - fijar `code_revision`/lock por familia en `engines.json` (57 release-blockers);
   - protocolo uniforme `describe`/`execute`/`cancel`/`unload` sobre `Engine`;
   - LangGraph real ejecutando la ruta efectiva en la imagen;
   - provider profiles LLM/VLM intercambiables (A06);
   - enganchar cada contrato nuevo a su engine real en el executor.
2. **W24** (banco de evaluación / demostraciones D01–D05 reproducibles).
3. **W25** (release: la imagen candidata ya existe con digest; falta el reporte
   final completo por C/P/N y las matrices).
4. **W26 / validación GPU (L3-L4)**: campaña GPU autorizada.

El estado honesto sigue siendo **BLOCKED_PRE_GPU**: la arquitectura y los
contratos de las 34 capacidades existen y se prueban, con imagen candidata
reproducible, pero la validación de calidad neuronal y el cierre de los
sub-items de integración con engines reales siguen pendientes. Nada se etiqueta
como validado-GPU sin medición.
