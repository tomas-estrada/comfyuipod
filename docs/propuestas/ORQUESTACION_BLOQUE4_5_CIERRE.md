# M6 Bloques 4–5 — Cierre: la capa de orquestación

Finding IDs: `M6-SCR-030`..`M6-SCR-040`.

Estado: **COMPLETO** (stub-first, gate-verde). Cierra el plan maestro
`PLAN_MAESTRO_ESTUDIO_AGENTE.md` (fases 12–20). Construido sobre M4 (contrato de
motores), M5 (roster de 14 + F7), M6-B1 (cerebro) y M6-B3 (TemplateSelector).

## Qué hace, en una línea
El agente convierte una **idea** en un **reel**: planifica jerárquicamente,
ejecuta con los motores, se **autoevalúa** y **replanifica** escalonadamente, y
ensambla — todo sobre un grafo tipado y reanudable, con la puerta de gasto humana
entre el plan barato y la producción cara.

## Las piezas (`studio/comfy_studio/orchestration/`)
| Fase | Módulo | Qué es |
|---|---|---|
| 12 | `state.py` | `ProductionState` tipado: idea → secuencias → shots → capas; escalones de replan; `to_dict`/`from_dict`. |
| 12 | `checkpointer.py` | `ProductionCheckpointer` SQLite (stdlib): guarda/reanuda por id tras un corte. |
| 12 | `graph.py` | `ProductionGraph`: nodos + router; runner stdlib + compilación LangGraph opcional (mismos nodos), checkpoint por paso. |
| 13 | `planner.py` | Planificación jerárquica (Eje 1): `PlannerLLM` inyectable + stub determinista; reparte cost cap, fija `acceptance`. |
| 14 | `executor.py` | Ejecución adaptativa (Eje 2): `EngineRunner` inyectable; orden topológico por `depends_on`; motores como nodos. |
| 15 | `evaluator.py` | Feedback loop + replan escalonado (Ejes 2+1): evaluador VBench/FFmpeg inyectable; replan **local → plantilla → global**. |
| 16 | `assembler.py` | Ensamblaje (Eje 4): `Assembler` inyectable; shots aceptados → reel final. |
| 17 | `service.py` | `ProductionService`: tools `plan_production` / `get_plan` / `approve_plan` / `production_status`, atados a la `StoryboardGate`. |
| 18 | `health.py` | Reporte de salud para strict-check. |

## Los 4 ejes, realizados
- **Eje 4 (LangGraph):** grafo con estado persistente reanudable. Decisión de
  criterio: LangGraph es biblioteca pura (sin servidor), se usa cuando está
  horneado; el grafo trae **runner stdlib equivalente** para que CI no dependa del
  import — misma disciplina stub-first que los motores.
- **Eje 1 (planificación jerárquica + replan escalonado):** idea → secuencias →
  shots → capas; replan en el orden EXACTO pedido: local (otra seed) → plantilla
  (el `TemplateSelector` del B3) → global (el planner reconsidera).
- **Eje 2 (ejecución adaptativa + feedback):** shots en orden topológico; tras
  cada shot un evaluador lo puntúa contra su `acceptance`; si falla, dispara el
  replan; si pasa, avanza.
- **Eje 3 (TemplateSelector):** se enchufa como el `TemplateReselector` del
  escalón 2 (Bloque 3).

## Puerta de gasto (sin cambios de M4)
`ProductionService.plan_production` deja la producción en `awaiting_approval` vía
la `StoryboardGate` existente. El lazo caro (ejecución + ensamblaje) **solo** corre
tras `approve_plan`. El cost cap total se reparte por shot.

## strict-check (fase 18)
Dos sondas nuevas en `comfyctl/src/strict.rs`, **warn nunca fatal** (como S14):
- **S15 `orchestration_ready`** — lee `orchestration.state` del health. `down`
  (grafo no construible) → warn; `degraded` (sin LangGraph, runner stdlib) y
  `ready` → ok.
- **S16 `agent_brain_ready`** — lee `agent_brain.capabilities`. Una capacidad
  `unavailable` → warn; `stub` es válido en CI.

El perfil studio pasa de 16 a **18 items** de strict-check.

## Colaboradores reales (integración de servidor)
Todo es **inyectable + stub por defecto**, así que CI corre sin GPU/LLM/LangGraph.
El servidor enchufa las implementaciones reales:
`PlannerLLM` → cliente LLM async del estudio · `EngineRunner` → `Engine.run` de los
adaptadores · `ShotEvaluator` → motor `qa` (VBench/FFmpeg) · `TemplateReselector` →
`TemplateSelector` (B3) · `Assembler` → editor (Remotion) + compositor (Natron).

## Guardrails (intactos)
Cost cap por shot · redacción de secretos · loopback-only · `STUDIO_DYNAMIC`
gobierna el descubrimiento dinámico · `StoryboardGate` como única puerta al gasto
pesado · validación en Blackwell con pesos reales diferida hasta autorización de
GPU/vast.ai.

## Gate
ruff limpio; suite del studio 472 verde (34 tests de orquestación); Rust
fmt + clippy `-D warnings` + test `--locked` verde (incl. el test S15/S16 nuevo y
los snapshots actualizados). Cero regresión.
