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



---

## Respuesta a la auditoría profesional M6 (2026-10-30)

Un auditor externo (sin todo el contexto, con un validador adversario) revisó la
rama `a427b8f` y abrió 12 hallazgos. Cruzados contra `main` tras el merge, el
dictamen y las acciones:

### Arreglados en código (bugs reales, verificables sin GPU)

| ID | Qué era | Fix |
|---|---|---|
| **M6-SCR-001** | Una dep INEXISTENTE (id alucinado por el LLM) nunca entra en `accepted` ni `failed` → el shot se saltaba para siempre → el grafo giraba hasta `max_steps`. (Mi fix previo solo cubría deps `FAILED`.) | `execute_next_shot` detecta deps huérfanas (`dep not in known`) y marca el shot `FAILED` (`shot_unreachable`). |
| **M6-SCR-004** | Si el ensamblador falla, no marca `done` → el router devolvía `ASSEMBLE` eternamente → `max_steps`. | Contador `assemble_attempts` en `state.metadata`; tras 2 fallos marca `phase="failed"`. El router (`_route`) ahora termina en `done` **o** `failed`. |
| **M6-SCR-002** | `StoryboardGate` vivía solo en RAM (`self._gates`); tras reiniciar el pod, `approve_plan` lanzaba `ProductionNotApprovedError` para siempre. | El estado de la puerta se persiste en `state.metadata["storyboard_gate"]` (va en el checkpoint). `approve_plan` la hidrata del checkpoint si la RAM está vacía. Test de "reinicio del pod". |
| **M6-SCR-006** | Ruta `/workspace` hardcodeada (rompía en Windows/macOS) y sin WAL. | El dir deriva de `_default_studio_data_dir` (portable); `_connect` aplica `PRAGMA journal_mode=WAL` + `busy_timeout`. |
| **M6-OPS-003** | `health.py` reportaba `degraded` siempre (LangGraph no se hornea por diseño), generando ruido. | Reporta `ready` con `engine: stdlib|langgraph` en los metadatos; `degraded`/`down` quedan para fallos reales. (El validador ya había confirmado que S15 en Rust no avisaba por esto; esto limpia la semántica JSON.) |
| **M6-AGT-002** | `TemplateSelector`, símbolos del selector y `orchestration_health_report` sin exportar en `__init__.__all__`. | Exportados; simetría con `engines`/`agent_brain`. |
| **M6-OPS-001** | `cargo fmt --check` fallaba. | Ya resuelto antes de leer la auditoría (commit `eee9690`). |

### Diferido a la integración de servidor (con autorización GPU/vast.ai)

No son bugs del esqueleto; son el cableado real que el plan maestro ya tenía como
fase de servidor. Se dejan escritos como invariantes ANTES de cablear:

- **M6-AGT-001 — gobernanza de gasto (CRÍTICO de diseño).** `approve_plan` **NUNCA**
  se expondrá al agente LLM: es la puerta de gasto humano. Al cablear: exponer al
  agente/MCP solo `plan_production`, `get_plan`, `production_status`; `approve_plan`
  va exclusivamente en la API pública, accionado por el humano, y despachado como
  job de fondo (`ProjectStore.enqueue_job`) para no bloquear Nginx (504).
- **M6-SCR-003 — replan global focalizado.** Hoy el escalón GLOBAL no reescribe el
  shot (reejecuta igual hasta agotar → `FAILED`, sin bucle gracias a SCR-001/004).
  Al integrar el LLM real: `replan_shot_with_llm(shot, state, planner)` que reescribe
  SOLO ese shot, sin destruir los shots ya aceptados (objeción correcta del validador).
- **M6-SCR-005 — conectar `TemplateSelector` al escalón de plantilla.** Hoy
  `_default_reselect` es no-op (`changed=False`). El adaptador real tomará el
  `ShotIntent` del shot y aplicará otra plantilla viable vía `TemplateSelector.select`.
- **M6-OPS-002 — 6 GET redundantes a `/studio/api/health` en Rust.** Optimización:
  una sola petición en `SystemSnapshot::gather()` alimentando las 6 sondas.
- **M6-SCR-007 — tipo `updated_at` en `agent_memory`.** Higiene SQLite menor.

### Veredicto propio
El auditor acertó en los 3 críticos/altos de robustez del lazo (deadlock por deps,
bucle del ensamblador, pérdida del gate) — todos arreglados y con test de regresión.
Los diferidos son cableado de servidor, no defectos del esqueleto. Gate tras los
fixes: ruff limpio, **499 tests** del studio verdes, Rust fmt+clippy`-D`+test `--locked`
verdes.
