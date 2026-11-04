# Prototipo "Modo Agente" web (experiencia tipo OpenCode para ComfyUIPod)

**Estado**: prototipo funcional implementado (fase 1). **Fecha**: 2026-11-04.

> Aclaración de marca: este modo es un proyecto **independiente** de ComfyUIPod.
> La inspiración en la interacción de OpenCode es solo eso — inspiración. ComfyUIPod
> **no está afiliado, respaldado ni asociado** con el equipo de OpenCode (Anomaly /
> sst). No se copió código, assets ni marcas de OpenCode; ver sección de licencia.

## 1. Objetivo

Una ruta activable (`/studio/agent.html`) que permita: crear/seleccionar una sesión
(proyecto), describir una tarea audiovisual en lenguaje natural, recibir un **plan
visible y editable**, **aprobar** antes de ejecutar, seguir la ejecución con
progreso en tiempo real y revisar los artefactos resultantes — todo consumiendo las
APIs reales de `studio/` (FastAPI), sin sustitutos en producción.

## 2. Opciones comparadas (con evidencia del repositorio)

| Opción | Evidencia del repo | Evaluación |
|---|---|---|
| **A. Bifurcar/adaptar el frontend de OpenCode** | OpenCode (`anomalyco/opencode`, antes `sst/opencode`) es un monorepo TypeScript/Bun con TUI (OpenTUI) y su propio build. El repo impone panel **vanilla sin build** (`studio/tests/test_panel_web.py::test_panel_files_exist_vanilla_without_build_artifacts` exige que `panel/` no tenga `node_modules`) y CSP estricta sin CDNs externos (`config/nginx.conf`: `script-src 'self'`). | Descartada: arrastraría toolchain Bun/npm, build artifacts y dependencias masivas, chocando con reglas explícitas del repo y con AGENTS.md ("sin CDNs externos; assets vendored"). Costo de mantenimiento inaceptable para un prototipo. |
| **B. Integrar paquetes/componentes seleccionados de OpenCode** | Sus componentes UI viven dentro del monorepo TUI (no publicados como paquetes npm reutilizables en vanilla JS); no hay paquetes instalables que encajen sin el build propio. | Descartada por la misma razón: no existen paquetes aislados utilizables bajo CSP `script-src 'self'` sin bundling. |
| **C. Conservar el frontend actual y adoptar los PATRONES de interacción de OpenCode** (sesiones en sidebar, transcripción con actividad de herramientas, puerta de aprobación, progreso vía stream) sobre `panel/` vanilla + las APIs existentes. | El backend YA tiene todo el flujo real: sesiones=proyectos (`routers/projects.py`), chat con agente y SSE (`routers/chat.py`, eventos `token`/`tool_call`/`tool_result`/`done`), plan→aprobación→ejecución→estado con checkpointer SQLite (`routers/productions.py`, `orchestration/`), progreso SSE global (`/studio/api/progress`), galerías/resultados (`routers/assets.py`, `/studio/api/view`). | **Elegida.** Cero dependencias nuevas, reutiliza contratos reales, y el prototipo queda como página estática adicional servida por el mismo `StaticFiles` de `/studio` — reversible y sin tocar el flujo principal (`panel/index.html` solo gana un enlace).

**Decisión: opción C.** Se reutilizan *patrones de interacción* (sesión/conversación/
plan/aprobación/actividad de herramientas/progreso), NO código. Adaptados al dominio
audiovisual: tareas→producciones, motores→shots/capas, resultados→assets de galería.
No se copian conceptos de programación (archivos, diffs, shell) que no aplican a un
estudio multimedia.

## 3. Licencia y atribución de OpenCode

- OpenCode (`anomalyco/opencode`, ex `sst/opencode`) publica su `LICENSE` bajo **MIT**
  (verificado 2026: el archivo LICENSE del repo y el paquete npm `opencode-ai`
  declaran MIT). MIT permite uso, modificación y distribución con conservación del
  aviso de copyright.
- **Decisión de código**: no se copia NINGUNA porción de código, asset ni marca de
  OpenCode en este prototipo (la licencia lo permitiría, pero el stack vanilla+CSP lo
  hace inviable y el valor está en los patrones, no en el código). Por tanto no hay
  avisos de copyright que conservar en el código; se mantiene la aclaración de no
  afiliación en la propia UI (`panel/agent.html`) y en este documento, como piden las
  indicaciones del proyecto OpenCode sobre uso de su nombre.
- Las dependencias añadidas por este prototipo: **ninguna** (backend stdlib/FastAPI ya
  presentes; frontend vanilla). No se importan paquetes de terceros nuevos.

## 4. Evaluación de LangGraph

El repo YA resuelve con menor complejidad lo que LangGraph aportaría:

- `orchestration/graph.py` define un grafo tipado propio (`ProductionGraph`) con
  **checkpoint por paso** vía `ProductionCheckpointer` (SQLite, `checkpointer.py`),
  reanudable tras un corte, y `compile_langgraph()` que compila el MISMO grafo con
  LangGraph **solo si la biblioteca está instalada** (`langgraph_available()`).
- La puerta de aprobación humana (pausa antes de gastar GPU) es la `StoryboardGate`
  + `ProductionService.approve_plan`, expuesta solo en la API pública
  (M6-AGT-001: el agente LLM nunca ve `approve`).
- Reintentos/replan: `REPLAN_ESCALATION` (local→template→global) en
  `orchestration/state.py` + `evaluator.py`.

**Decisión: NO añadir LangGraph como dependencia.** No está en `studio/pyproject.toml`;
incorporarlo sumaría un árbol de dependencias grande para una ganancia de comportamiento
nula (el runner stdlib ya ejecuta el mismo grafo con persistencia por paso). El hook
`compile_langgraph()` queda como puerta de entrada si en el futuro se hornea la lib.

## 5. Contrato mínimo añadido (APIs nuevas, todas reales)

Sobre `routers/productions.py` (mismo patrón de registro que el resto):

- `GET /studio/api/productions?project_id=&limit=` — lista producciones (sesiones de
  trabajo) desde el checkpointer, filtradas por proyecto.
- `PATCH /studio/api/productions/{id}/plan` — edita el plan **solo** en fase
  `awaiting_approval` (idea y shots: descripción, duración, eliminar). 409 si ya se
  aprobó/canceló. Persiste vía checkpointer.
- `POST /studio/api/productions/{id}/cancel` — cancela en `awaiting_approval`
  (termina en `cancelled`) o marca `cancel_requested` durante la ejecución; el router
  del grafo detiene el lazo en el siguiente paso (cancelación cooperativa, con
  checkpoint). 409 si ya es terminal.
- Eventos SSE en `/studio/api/progress`: `production_planned`,
  `production_plan_updated`, `production_approved`, `production_step` (por paso del
  grafo, vía `ProgressBroker.publish_threadsafe`), `production_cancelled` /
  `production_cancel_requested`, `production_finished`. El progreso del lazo de
  producción se expone por SSE (evento por paso persistido) y se refuerza con polling
  de `GET /studio/api/productions/{id}/status` (checkpoint por paso).

Lo que NO se inventa: no hay descarga de modelos bajo demanda en este flujo (eso es
responsabilidad del otro worker con `scripts/download-models.sh`); no hay acceso del
modelo a shell/rutas/red: todas las acciones pasan por las herramientas registradas del
`tool_belt` y la puerta `approve` solo existe en la API pública.

## 6. Frontend

- `panel/agent.html` + `panel/agent.css` + `panel/agent.js` (vanilla, sin build, CSP-safe:
  sin scripts/estilos inline, sin CDNs). Servido por el `StaticFiles` existente en
  `/studio/agent.html`. Enlace de entrada en `panel/index.html`.
- Flujo: sesiones (proyectos) en sidebar → nueva sesión → conversación con stream SSE
  del chat (tokens + `tool_call`/`tool_result` como actividad de herramientas) →
  "Planificar producción" (POST `/studio/api/productions`) → plan editable (PATCH) →
  diálogo de aprobación → ejecución con progreso (SSE `production_step` + polling de
  status) → resultados (assets en `/studio/api/view` y galería del proyecto) con
  estados de error recuperables (reintento vía estado / re-planificar).
- Accesibilidad: `aria-live`, foco visible, teclado (Enter envía, Esc cierra diálogos),
  etiquetas en todos los controles; adaptable a pantallas pequeñas (grid → 1 columna).

## 7. Pruebas

`studio/tests/test_agent_mode_api.py` cubre el contrato nuevo (list/patch/cancel, 404 /
409 / 400, eventos SSE `production_step`, cancel cooperative del grafo, páginas del
panel y CSP). `studio/tests/test_route_map.py` se actualiza con las 3 rutas nuevas.
