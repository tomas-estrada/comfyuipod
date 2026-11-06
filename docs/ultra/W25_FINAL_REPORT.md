# W25 — Reporte final de entrega pre-GPU

> **Registro histórico de cierre W25.** Su estado refleja el corte pre-GPU y no
> es el estado actual del proyecto. W26 ejecutó una demo SDXL limitada; el nivel
> global sigue parcial. Consulta [resultados W26](W26_GPU_RESULTS.md) y contrasta
> el `main` actual (`5850191`), ya que `status.json` aún apunta a `f5c29dc`.

Estado al cierre de W25: **BLOCKED_PRE_GPU** (arquitectura y contratos completos;
faltaban integración L2 con engines reales y la campaña GPU W26).

Revisión de main al cerrar: consultar `docs/ultra/status.json` (`main_sha`).
Imagen candidata: ver `candidate_image_current` en `status.json`.

Este reporte consolida el estado por capacidad (C), programa (P) y alternativa
(N) con su nivel de evidencia, como pide el plan (sección "Cierre y evidencia").

## Niveles de evidencia (recordatorio)

- **L0** propuesta/fuente · **L1** código+contrato+unit tests · **L2** engine
  preparado en imagen + adapter + prueba CPU · **L3** ejecución GPU real · **L4**
  reproducido con holdouts.
- Pre-GPU exige L2 por capacidad obligatoria; esta entrega alcanza **L1 en todos
  los contratos** y **L2 parcial** donde el engine ya estaba horneado (atlas,
  percepción, EDL). L3/L4 = campaña GPU (W26).

## Estado por capacidad (C01–C34)

| C | Capacidad | Paquete / módulo | Nivel | Nota |
|---|-----------|------------------|-------|------|
| C01 | Archivo semántico / ingesta | W05 `ingest.py` | L1 | extracción con pesos = L2+ |
| C02 | Inspiración → receta | W06 `inspiration.py`, W20 | L1 | conectores opcionales honestos |
| C03 | Dirección autónoma | W22 `director.py` | L1 | orquesta la cadena |
| C04 | Producción conectada | W22 + chain | L1 | planner→receta→executor→lease |
| C05 | Cámaras | W11 `cinematography.py` | L1 | animatic visible = render GPU |
| C06 | Memoria espacial | W17 `spatial_memory.py` | L1 | — |
| C07 | Atlas verificable | W09 `atlas/` | L2 | medios offline en imagen |
| C08 | Reconstrucción/encuadre | W11 | L1 | — |
| C09 | Activos 3D | W10 `assets3d.py` | L1 | Trellis/PartCrafter = L2+ |
| C10 | Partes / física producto | W10 + W12 `physics.py` | L1 | — |
| C11–C15 | Movimiento/rigging/física | W13 `motion.py`, W12 | L1 | — |
| C16 | Actuación facial/voz | W14 `face.py`, W19 `voice.py` | L1 | pesos faciales NC declarados |
| C17 | 4D / reconstrucción temporal | W17 | L1 | — |
| C18/C19 | Física vehículos/articulados | W12 | L1 | — |
| C20 | Ensayo de rodaje | W11 | L1 | — |
| C21/C22 | Reiluminación / materiales | W16 `relight.py` | L1 | — |
| C23 | Generación base AV | W08 `av_recipes.py` | L1 | render = L3 |
| C24 | Edición causal | W15 `layers.py`, W20 | L1 | — |
| C25 | Foley / sonido causal | W18 `audio.py` | L1 | síntesis = L2+ |
| C26/C27 | Música / voz español | W19 | L1 | lexicon colombiano |
| C28 | Imagen / capas | W08, W15 | L1 | — |
| C29 | Matting / omnimatte | W15 | L1 | — |
| C30 | Upscale / finishing 4K | W08, W20 `delivery_4k.py` | L1 | — |
| C31/C32 | Composición / entregables | W20 | L1 | créditos viajan con el proyecto |
| C33 | Banco de evaluación | W23 `researcher.py`, W24 `demos.py` | L1 | — |
| C34 | Aprendizaje selectivo | W23 | L1 | training = no ejecutado (A26) |

Todas las C01–C34 tienen un contrato ejecutable con tests (L1); ninguna queda
en catálogo/wrapper vacío. La elevación a L2 (adapter+engine real por capacidad)
y L3 (GPU) es el trabajo restante.

## Programas P01–P14

Los programas de investigación están representados por los contratos que los
sostienen (compilador de intención → W22; grafo causal → W04/W15/W16/W18;
adquisición de vista → W09/W17; multi-fidelidad → W08/W02; Pareto → W23). Su
afirmación científica (mejora medida) exige **L4** y por tanto la campaña GPU.

## Alternativas N01–N30

Resueltas en `engines.json` (validado por `scripts/check_engines_manifest.py`):
57 engines `build_selected` con su disposición y licencia (`terms_review`); los
`lock_required_before_build` sin `code_revision` quedan como **release-blockers
declarados** (fijar los locks por familia es trabajo de integración pendiente).

## Infraestructura (W00–W04, W07)

| Área | Módulo | Nivel |
|------|--------|-------|
| Integridad de modelos | `asset_acquire` (3 P0) | L1 |
| Hardware/sizing/recetas | `hardware_detect`, `storage_requirement`, `projects` | L1 |
| Lease global GPU | `gpu_lease` + `leased_runner` (enganchado al executor) | L1 |
| Contratos/DAG | `state` + `lineage` (invalidación selectiva, migraciones) | L1 |
| Release gate engines | `check_engines_manifest` | L1 |

## Imagen candidata

Construida y publicada por el workflow GHCR con digest real, atlas offline
físicamente incluido, smoke E2E + MODEL_PROFILE=studio verde. Ver
`status.json` → `candidate_image_current` para el tag/digest vigentes.

## Bloqueos para `ACCEPTED_PRE_GPU` pleno

1. **Integración L1→L2**: conectar cada contrato a su engine real horneado;
   fijar los 57 engine locks; protocolo `describe/execute/cancel/unload`
   uniforme; LangGraph real ejecutando la ruta efectiva; provider profiles (A06).
2. **W26 / campaña GPU (L3-L4)**: no ejecutada por diseño; `demos.py` deja los
   pasos neurales `pending_gpu` y el protocolo preparado.

## Protocolo GPU preparado (W26, no ejecutado)

Ver `docs/ultra/CLOSING.md` §"Protocolo de validación GPU". Resumen: rentar pod
96 GB con la imagen candidata vigente, fijar presupuesto/teardown, verificar
pesos vía `/object_info`, medir pico VRAM/RAM real por receta, ejecutar D01–D05,
destruir el pod y verificar 0 instancias.

## Veredicto honesto

La arquitectura completa de ComfyUIPod Ultra (34 capacidades, infraestructura,
orquestación, atlas, demos) existe como **contratos verificables con tests
reales y sin mocks**, con una **imagen candidata reproducible con digest**. Esto
es implementación **preparada** (L1/L2), no capacidad **validada** (L3/L4). El
éxito neuronal se decide por medición en GPU, que es la fase siguiente.
