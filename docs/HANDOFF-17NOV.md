# HANDOFF-17NOV — estado y continuación del proyecto

**Captura:** 2026-11-17 UTC  
**Repositorio:** [`tomas-estrada/comfyuipod`](https://github.com/tomas-estrada/comfyuipod)  
**Baseline de aplicación verificado:** `b612fbe7369dd20d66d8933095f23882ade7a2af`  
**Estado:** **no listo para producción**. Este handoff es una captura fechada; antes de ejecutar trabajo nuevo, verifica el SHA de `main`, PRs abiertas, CI y reglas de protección.

## Lectura rápida

El producto es un contenedor Linux de un solo usuario con ComfyUI y Comfy Studio, gestionado principalmente desde navegador. El objetivo de producto está en [`upgrades-ultra-version/GOAL.md`](../upgrades-ultra-version/GOAL.md), con la misión extendida en [`upgrades-ultra-version/MISSION.md`](../upgrades-ultra-version/MISSION.md). La misión no está cumplida: no presentes el proyecto como production-ready ni como si solo faltaran mediciones GPU.

`main` en `b612fbe` tiene una candidata verificada, no promovida:

| Evidencia | Valor comprobado |
|---|---|
| Workflow de main | [44033581865](https://github.com/tomas-estrada/comfyuipod/actions/runs/44033581865), `success` |
| Checks | Rust lint/tests/dependency audit; CI-01/CI-03 y Studio/panel; candidate build, full-stack smoke y Studio-profile smoke — los tres jobs pasaron |
| Imagen candidata | `ghcr.io/tomas-estrada/comfyuipod:sha-b612fbe` |
| Manifest digest | `sha256:e321fab21694bceb5b39a2027598a7daf77f300e51322c1ae739e8cd454ff996` |
| Config digest | `sha256:ef07b50043e03cdb14ff6200c59f2e675d1895bfcd02240935f5b1c965ae152c` |
| Promoción | No llegó a `latest`/semver; el gate A07 lo bloqueó correctamente |
| A07 | 88 entradas, 57 seleccionadas; **228 blockers en 46/57 engines**; manifiesto coherente, `release_ready=false` |

El resultado A07 se volvió a derivar localmente con `python3 scripts/check_engines_manifest.py --json`. La CI de main reportó el mismo conteo y usó historial completo. El control cuenta evidencia declarada y rutas rastreadas; el cierre exige también comprobar que cada lock, licencia, notice, adapter, inventario de imagen y probe de ejecución corresponde al engine real. No reduzcas `build_selected` ni llenes datos para hacer pasar el gate.

> La ejecución de GitHub API confirmó que `main` seguía en `b612fbe` al crear este handoff. `git fetch origin main` falló en el checkout por resolución DNS de `github.com`; el SHA remoto se verificó por la conexión GitHub. Tras el merge de este PR, el SHA de `main` cambiará por commits documentales; el baseline de aplicación descrito aquí sigue siendo `b612fbe` salvo nuevos cambios de código.

## Cómo entender el repo

| Tema | Fuente que manda | Código/evidencia que hay que inspeccionar |
|---|---|---|
| Inicio, perfiles, variables y despliegue | [`README.md`](../README.md), [`docs/GUIA_PASO_A_PRODUCCION.md`](GUIA_PASO_A_PRODUCCION.md), [`docs/POLITICA_ARQUITECTURA_RECURSOS.md`](POLITICA_ARQUITECTURA_RECURSOS.md) | `comfyctl/src/config.rs`, `studio/comfy_studio/config.py`, `studio/comfy_studio/hardware_profile.py`, `studio/comfy_studio/hardware_detect.py`, `scripts/storage_requirement.py` |
| Convenciones, harness, secretos y checks | [`AGENTS.md`](../AGENTS.md), [`CONTRIBUTING.md`](../CONTRIBUTING.md), [`docs/agent-harness-contract.md`](agent-harness-contract.md) | `.codex/hooks.json`, `.codex/agent-harness.json`, `scripts/codex_agent_harness.py`, `scripts/cmd-agent`, `.opencode/`, `.kilo/`, `.commandcode/` |
| API y UI de Studio | [`docs/ARCHITECTURE.md`](ARCHITECTURE.md), [`docs/ARCHITECTURE_CANONICAL.md`](ARCHITECTURE_CANONICAL.md) | `studio/comfy_studio/app.py`, `studio/comfy_studio/routers/`, `studio/comfy_studio/agent/`, `panel/` |
| Engines del runtime | [`docs/engines/README.md`](engines/README.md) | `studio/comfy_studio/engines/registry.py`, `base.py`, `preparedness.py`, adapters específicos y sus tests. `wired=true` no equivale a engine preparado o ejecución validada. |
| Misión y aceptación | [`upgrades-ultra-version/README.md`](../upgrades-ultra-version/README.md), [`acceptance.md`](../upgrades-ultra-version/acceptance.md), [`backlog.json`](../upgrades-ultra-version/backlog.json), [`capabilities.json`](../upgrades-ultra-version/capabilities.json) | Tests y runtime correspondientes; ejecutar `python3 upgrades-ultra-version/validate_spec.py` para validar el paquete, no el producto |
| Investigación local consultable | [`docs/research/README.md`](research/README.md) | `python3 scripts/research_rag.py "<pregunta concreta>"`; los resultados son pistas, verifica fuente primaria y código |
| Estado y cierre | Este documento, [`docs/ultra/status.json`](ultra/status.json), [`docs/ultra/AGENT_CLOSURE_AUDIT.md`](ultra/AGENT_CLOSURE_AUDIT.md), [`docs/ultra/CAPABILITY_EVIDENCE.md`](ultra/CAPABILITY_EVIDENCE.md) | A fecha de captura, main/CI/imágenes y `scripts/check_engines_manifest.py` |

El registro del runtime tiene **20** motores y el catálogo de workflows expone **19** plantillas en el snapshot. Son recuentos de código, no afirmaciones de capacidad; consulta los registries dinámicos. La misión declara 33 capacidades activas, 14 programas y 26 work items. En `acceptance.md` los IDs van de A10 a A12: no hay criterio A11 en ese archivo.

## Pendientes que impiden decir “solo falta GPU”

La matriz íntegra y normativa es [`upgrades-ultra-version/acceptance.md`](../upgrades-ultra-version/acceptance.md). La siguiente tabla agrupa lo que todavía no está probado por evidencia de main; no convierte una prueba estrecha en cierre global:

| Aceptación | Estado para quien retoma |
|---|---|
| A00 | Este snapshot reconcilia SHA, CI e imagen. Repetir baseline al iniciar el siguiente lote y después de integrar. |
| A01–A04 | Hay implementaciones y regresiones de adquisición, sizing, recursos y lease en main. Revalidar contra cada bullet de acceptance; las medidas CUDA y ciertos casos de hardware siguen pendientes. |
| A05 | Existe prueba de integración con LangGraph instalado, colaboradores compuestos y SQLite. Reanudar/cancelar, idempotencia, invalidación selectiva y semántica de director necesitan completar su matriz; tests de fallback no bastan. |
| A06 | Hay contract tests simulados para perfiles/error/redacción. No prueban conectividad/configuración real ni comportamiento de modelo; no enviar un secreto ni hacer llamadas facturadas sin un límite vigente y runtime-secret handling. |
| A07 | **Abierto: 228 bloqueos en 46 engines seleccionados.** Faltan diferentes combinaciones de SHA upstream, locks reproducibles y rastreados, revisión legal/notices, adapter/entrypoint y prueba real. Mantén el roster y clasifica cualquier restricción con evidencia. |
| A08–A10 | Ingesta/índice, análisis de referencias y generación tienen rutas y fixtures; falta aceptación con corpus multimedia heterogéneo, casos autorizados, salida de motores reales, integridad por formato/tiempo y holdouts según cada bullet. No usar solo el demo sintético como prueba de producto. |
| A12–A19 | Geometría, cámaras, física, movimiento, cara, capas, luz y memoria 4D tienen evidencia desigual/parcial. Completar outputs reabribles, medidas, dependencias/invalidación y holdouts que pide cada sección; proxy CPU no es el solver ni la inferencia neural que nombra. |
| A20–A22 | Sonido, español/voz y entrega final requieren validar mediciones de audio, pronunciación, sync, editabilidad, aspecto/color/4K y notices/créditos en los flujos de producto. |
| A23–A24 | Panel/cliente, MCP/harness y producción autónoma tienen tests de API/JS/servicio. Falta completar recorrido browser con backend CPU real, reconnect/cancel/resume y restricciones persistentes de punta a punta. El control de escritorio de Codex no estuvo disponible; eso no impide pruebas browser headless en CI. |
| A25 | D01–D05 cuentan con runners/fixtures y algunos artifacts sintéticos. No todos son campañas completas con inputs autorizados, ejecución real, ground truth independiente y manifest que permita reproducir sin duplicar/falsear outputs. |
| A26 | El registry y contratos de experimentos no cierran un programa científico: faltan baselines, splits, datasets, múltiples seeds, resultados/fallos/costes y criterios de promoción reproducibles. |
| A27 | Los tres jobs del workflow de `b612fbe` pasaron. Eso acredita ese SHA; rerun de CI requerido para cualquier PR y workflow de main tras merge. |
| A28 | Aceptación pre-GPU histórica, actualizada en el paquete para distinguirla de W26; no la uses sola como cierre. |
| A29 | W26 midió una sola receta SDXL en `sha-f5c29dc`, imagen anterior. No hay revalidación GPU de `b612fbe`, no valida el resto de recetas y no cierra requisitos no-GPU. No iniciar nuevo gasto sin la autorización específica que exige la misión. |

**Trabajo prioritario:** cerrar A07 en lotes de engines realmente implementables, en paralelo con brechas CPU de A05/A08/A09/A12–A26. Para cada engine, verificar fuente exacta, licencia de código y pesos, condiciones de redistribución, dependencias transitivas, notice instalado en imagen, adapter que ejecuta trabajo útil, test CPU y estado honesto de hardware/pesos. Si un requisito no puede resolverse legal o técnicamente, registrar el bloqueo y una alternativa que conserve la capacidad; no convertirlo en `done` por un wrapper.

## Restricciones que no se deben repetir

- **No usar Modal.** Los intentos anteriores terminaron con HTTP 429, coste reportado de USD 19.40, informe incompleto y cuenta/billing bloqueados. No reintentar ni desplegar otro contenedor Modal.
- **No alquilar GPU, descargar pesos grandes ni activar APIs facturadas** como parte del handoff. La campaña W26 es de un digest anterior. `MISSION.md` y A29 requieren autorización de presupuesto vigente para otra campaña.
- Una clave DeepSeek se compartió en la conversación para un tope de prueba de USD 2; no se incluyó en Git ni se envió durante este handoff. Si esa prueba vuelve a ser necesaria, no copies la clave desde documentos/logs; inyecta el secreto en runtime, redáctalo y nunca superes ese tope.
- El acceso de control al escritorio/navegadores no estaba concedido en esta sesión y Chrome no se usó. Para A23, agrega o ejecuta prueba browser headless; no afirmes inspección visual humana basándote solo en HTTP o DOM simulado.
- El primer lote de Command Code con modelo gratuito falló por conexión al API. El lote posterior con `z-ai/glm-5.3-flash` dejó registros de harness pero no cambió código ni produjo hallazgos auditables completos. No cuentes esos runs como revisión o cierre; si se relanza, exige un informe persistido y revísalo en el diff.
- No subir medios personales, weights ni credenciales a Git, GHCR o artifacts de CI. Ver reglas de [`AGENTS.md`](../AGENTS.md) y [`SECURITY.md`](../SECURITY.md).

## Forma recomendada de continuar

1. Desde una máquina con acceso GitHub, verifica `git status`, `git fetch origin`, `main`, PRs abiertas, checks requeridos y release candidate. Este checkout no logró hacer fetch por DNS, aunque el conector GitHub confirmó el SHA al capturar el documento.
2. Usa `docs/ultra/AGENT_CLOSURE_AUDIT.md` y `upgrades-ultra-version/acceptance.md` para descomponer lotes por archivos/contratos sin colisiones. Agrupa fixes relacionados y valida el lote completo antes de abrir PR; no mandes una PR por cada fallo aislado.
3. Antes de trabajo sobre harness/MCP, Blender/3D, multimodal o contexto, consulta el RAG local según `AGENTS.md`; verifica después la fuente primaria y la implementación del repo.
4. Ejecuta los comandos actuales de `AGENTS.md` y pruebas pertinentes. Para cambios de producto, añade regresiones; no borres ni rebajes gates. Para documentación, `git diff --check`, enlace local y render de diagramas si toca `.puml`.
5. Abre PR desde rama al día con `main`. La regla entregada por el dueño exige PR, checks CI-01/CI-03 y Rust, rama actualizada, sin force-push ni borrado. Espera el resultado, corrige la tanda completa, integra y verifica el SHA posterior y su workflow.
6. Actualiza este handoff, `docs/ultra/status.json`, `CAPABILITY_EVIDENCE.md` y `snapshot.json` solo desde la evidencia del commit/imagen integrado. Si solo cambian docs, deja claro el baseline de código del que proceden.

## Auditoría de Markdown, carpeta por carpeta

Se revisaron los Markdown rastreados para ubicarlos como guía activa, instrucción de runtime, investigación/propuesta fechada, informe histórico, fixture o documentación de tercero. Un script comprobó los **126 enlaces Markdown locales existentes antes de crear este handoff: 0 rutas faltantes**. Después de añadir este documento el PR contiene una página más. Las fuentes fechadas se conservaron como registros y se les añadió un puntero cuando podían confundirse con estado actual; las guías de entrada desfasadas se actualizaron. El inventario individual está en el apéndice.

Cambios de documentación vigentes en esta tanda:

- `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/README.md`: handoff enlazado, estado actual visible y conteos/instrucciones no fijados a números viejos.
- `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_CANONICAL.md`, `docs/engines/README.md`, `docs/walkthrough.md`: roster/plantillas revisados con código actual; `wired` separado de preparado/validado.
- `docs/architecture/README.md`, `docs/architecture/00-inventory.md`: snapshots MAAD identificados como históricos.
- `docs/ultra/PROGRESS.md`, `docs/ultra/W25_FINAL_REPORT.md`, `docs/ultra/CLOSING.md`: punteros al estado actual, sin reescribir evidencia histórica.
- `upgrades-ultra-version/README.md`, `MISSION.md`, `research.md`, `VALIDATION.md`: paquete fechado separado del estado vivo; referencias a `main` y CI actualizadas.
- `docs/ultra/AGENT_CLOSURE_AUDIT.md`, `CAPABILITY_EVIDENCE.md` y `status.json`: snapshot de código/imagen/A07 en `b612fbe` y sus límites.

Los informes `docs/auditorias/`, reportes W y campañas GPU, planes `docs/superpowers/`, propuestas e investigación conservan su fecha y conclusiones originales. No son estado vivo y no deben corregirse retroactivamente como si esas pruebas hubieran corrido sobre el código de hoy. Los README dentro de `node_modules` son de un tercero, y `comfyctl/tests/fixtures/report_expected.md` es fixture de test: se preservan tal cual.

### Apéndice: inventario individual de Markdown rastreado al baseline

La clasificación indica la función y el tratamiento de cada archivo. “Revisado/enlazado” significa que se contrastó para este handoff; los resultados históricos o legales se preservan y no se presentan como código actual.

| Archivo | Disposición para retomar |
|---|---|
| [`.commandcode/agents/harnessed-builder.md`](../.commandcode/agents/harnessed-builder.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.kilo/agent/implementer.md`](../.kilo/agent/implementer.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.kilo/agent/reviewer.md`](../.kilo/agent/reviewer.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.kilo/agent/scrutiny-auditor.md`](../.kilo/agent/scrutiny-auditor.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.kilo/skills/release-ci/SKILL.md`](../.kilo/skills/release-ci/SKILL.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.kilo/skills/repo-audit/SKILL.md`](../.kilo/skills/repo-audit/SKILL.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.kilo/skills/unreal-pipeline/SKILL.md`](../.kilo/skills/unreal-pipeline/SKILL.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.opencode/agents/free-builder.md`](../.opencode/agents/free-builder.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`.opencode/agents/harnessed-builder.md`](../.opencode/agents/harnessed-builder.md) | Instrucción/runtime profile del agente; conservar junto a AGENTS y harness, comprobar perfil real antes de delegar. |
| [`AGENTS.md`](../AGENTS.md) | Actualizado: estructura y guía de agentes; el handoff es la referencia de estado. |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Actualizado: enlaza estado 17 nov y separa W26 histórico del baseline actual. |
| [`README.md`](../README.md) | Actualizado: estado visible, A07, enlace al handoff; inventario y cifras se contrastaron con registries. |
| [`SECURITY.md`](../SECURITY.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`comfyctl/tests/fixtures/report_expected.md`](../comfyctl/tests/fixtures/report_expected.md) | Fixture esperado por tests; no es documentación operativa y se conserva por contrato. |
| [`design-qa.md`](../design-qa.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) | Actualizado: conteo de motores y rutas comparados con registry. |
| [`docs/ARCHITECTURE_CANONICAL.md`](../docs/ARCHITECTURE_CANONICAL.md) | Actualizado: 19 plantillas y toolbelt sin conteo obsoleto. |
| [`docs/GUIA_PASO_A_PRODUCCION.md`](../docs/GUIA_PASO_A_PRODUCCION.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/POLITICA_ARQUITECTURA_RECURSOS.md`](../docs/POLITICA_ARQUITECTURA_RECURSOS.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/README.md`](../docs/README.md) | Actualizado: el handoff encabeza la ruta de navegación; eliminado el SHA obsoleto. |
| [`docs/agent-harness-contract.md`](../docs/agent-harness-contract.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/architecture/00-inventory.md`](../docs/architecture/00-inventory.md) | Añadido aviso de snapshot histórico MAAD. |
| [`docs/architecture/03-rules.md`](../docs/architecture/03-rules.md) | Artefacto de auditoría MAAD; fechas/conteos son de su snapshot, no estado vivo. |
| [`docs/architecture/04-atam-report.md`](../docs/architecture/04-atam-report.md) | Artefacto de auditoría MAAD; fechas/conteos son de su snapshot, no estado vivo. |
| [`docs/architecture/05-mismatch-report.md`](../docs/architecture/05-mismatch-report.md) | Artefacto de auditoría MAAD; fechas/conteos son de su snapshot, no estado vivo. |
| [`docs/architecture/README.md`](../docs/architecture/README.md) | Añadido baseline/fecha históricos y puntero al estado actual. |
| [`docs/auditorias/AUDITORIA_AGENTE_V1.md`](../docs/auditorias/AUDITORIA_AGENTE_V1.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V2.md`](../docs/auditorias/AUDITORIA_AGENTE_V2.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V3.md`](../docs/auditorias/AUDITORIA_AGENTE_V3.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V4.md`](../docs/auditorias/AUDITORIA_AGENTE_V4.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V5.md`](../docs/auditorias/AUDITORIA_AGENTE_V5.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V6.md`](../docs/auditorias/AUDITORIA_AGENTE_V6.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V7.md`](../docs/auditorias/AUDITORIA_AGENTE_V7.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V8.md`](../docs/auditorias/AUDITORIA_AGENTE_V8.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_AGENTE_V9.md`](../docs/auditorias/AUDITORIA_AGENTE_V9.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_INTEGRAL_FINAL_M3.md`](../docs/auditorias/AUDITORIA_INTEGRAL_FINAL_M3.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/AUDITORIA_M3_SCR_20261028.md`](../docs/auditorias/AUDITORIA_M3_SCR_20261028.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/CONSULTORIA_AGENTICA_ENGINES_USABILIDAD_MVP_2026-11-15.md`](../docs/auditorias/CONSULTORIA_AGENTICA_ENGINES_USABILIDAD_MVP_2026-11-15.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/IMPLEMENTACION_OBSERVABILIDAD_Y_PORTAL.md`](../docs/auditorias/IMPLEMENTACION_OBSERVABILIDAD_Y_PORTAL.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/IMPLEMENTACION_V6_Y_RUST.md`](../docs/auditorias/IMPLEMENTACION_V6_Y_RUST.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/auditorias/README.md`](../docs/auditorias/README.md) | Informe/índice histórico fechado; conservar hallazgos originales, no usar como estado vivo. |
| [`docs/cinematic-studio.md`](../docs/cinematic-studio.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/decisiones.md`](../docs/decisiones.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/design/STUDIO_UX_CONCEPT.md`](../docs/design/STUDIO_UX_CONCEPT.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/engines/README.md`](../docs/engines/README.md) | Reescrito contra build_default_registry(); distingue wired de readiness/validación. |
| [`docs/engines/ace-step.md`](../docs/engines/ace-step.md) | Guía de engine; contrastar ruta, licencia, adapter y test antes de reclamar soporte. |
| [`docs/engines/chrono.md`](../docs/engines/chrono.md) | Guía de engine; contrastar ruta, licencia, adapter y test antes de reclamar soporte. |
| [`docs/engines/mission-compiler.md`](../docs/engines/mission-compiler.md) | Guía de engine; contrastar ruta, licencia, adapter y test antes de reclamar soporte. |
| [`docs/engines/production-memory.md`](../docs/engines/production-memory.md) | Guía de engine; contrastar ruta, licencia, adapter y test antes de reclamar soporte. |
| [`docs/engines/reference-local.md`](../docs/engines/reference-local.md) | Guía de engine; contrastar ruta, licencia, adapter y test antes de reclamar soporte. |
| [`docs/engines/scene-optimizer.md`](../docs/engines/scene-optimizer.md) | Guía de engine; contrastar ruta, licencia, adapter y test antes de reclamar soporte. |
| [`docs/estudio_anuncios.md`](../docs/estudio_anuncios.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/licenses/blender.md`](../docs/licenses/blender.md) | Guía/notice legal; preservada. Confirmar notice y licencia exactos antes de redistribuir. |
| [`docs/licenses/llama-cpp-notices.md`](../docs/licenses/llama-cpp-notices.md) | Guía/notice legal; preservada. Confirmar notice y licencia exactos antes de redistribuir. |
| [`docs/licenses/mitsuba.md`](../docs/licenses/mitsuba.md) | Guía/notice legal; preservada. Confirmar notice y licencia exactos antes de redistribuir. |
| [`docs/licenses/remotion.md`](../docs/licenses/remotion.md) | Guía/notice legal; preservada. Confirmar notice y licencia exactos antes de redistribuir. |
| [`docs/plantillas.md`](../docs/plantillas.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/propuestas/BRIEF_CREATIVO_M7.md`](../docs/propuestas/BRIEF_CREATIVO_M7.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/CATALOGO_MOTORES_OPENSOURCE_2026.md`](../docs/propuestas/CATALOGO_MOTORES_OPENSOURCE_2026.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/ESTUDIO_AUMENTADO_IA.md`](../docs/propuestas/ESTUDIO_AUMENTADO_IA.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/ESTUDIO_REEL_MOTORES.md`](../docs/propuestas/ESTUDIO_REEL_MOTORES.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/IDEAS_M3_BACKLOG.md`](../docs/propuestas/IDEAS_M3_BACKLOG.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/IMPLEMENTACION_RUST.md`](../docs/propuestas/IMPLEMENTACION_RUST.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md`](../docs/propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/MONTAJE_DESDE_BRIEF.md`](../docs/propuestas/MONTAJE_DESDE_BRIEF.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/OBSERVABILIDAD_Y_PORTAL.md`](../docs/propuestas/OBSERVABILIDAD_Y_PORTAL.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/ORQUESTACION_BLOQUE4_5_CIERRE.md`](../docs/propuestas/ORQUESTACION_BLOQUE4_5_CIERRE.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/PLAN_MAESTRO_ESTUDIO_AGENTE.md`](../docs/propuestas/PLAN_MAESTRO_ESTUDIO_AGENTE.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/propuestas/PROTOTIPO_MODO_AGENTE_WEB.md`](../docs/propuestas/PROTOTIPO_MODO_AGENTE_WEB.md) | Propuesta o auditoría fechada; ideas no implican implementación. Contrastar con código/backlog. |
| [`docs/research/Conduct a comprehensive, evidence-grounded research investigation for the open-s.md`](../docs/research/Conduct a comprehensive, evidence-grounded research investigation for the open-s.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/README.md`](../docs/research/README.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/blender-extensions-integration.md`](../docs/research/blender-extensions-integration.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/blender-physics-cpu.md`](../docs/research/blender-physics-cpu.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/blender-task-agent-harness.md`](../docs/research/blender-task-agent-harness.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/catalogs/comfyui-blender-ecosystem-review-2026-11-12.md`](../docs/research/catalogs/comfyui-blender-ecosystem-review-2026-11-12.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/catalogs/link-catalog-data-quality.md`](../docs/research/catalogs/link-catalog-data-quality.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/catalogs/open-engine-registry-v8.0-data-quality.md`](../docs/research/catalogs/open-engine-registry-v8.0-data-quality.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/datasets/README.md`](../docs/research/datasets/README.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/engine-model-connectors.md`](../docs/research/engine-model-connectors.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/github-integration-scan-2026-11-12.md`](../docs/research/github-integration-scan-2026-11-12.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/langgraph-agent-design.md`](../docs/research/langgraph-agent-design.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/mitsuba-cpu-fixtures.md`](../docs/research/mitsuba-cpu-fixtures.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/research/opencode-context-and-free-models.md`](../docs/research/opencode-context-and-free-models.md) | Corpus/investigación fechada; recuperar leads y validar fuentes primarias al usarla. |
| [`docs/srs.md`](../docs/srs.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/superpowers/plans/2026-11-12-a07-lock-evidence.md`](../docs/superpowers/plans/2026-11-12-a07-lock-evidence.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-12-c02-camera-motion.md`](../docs/superpowers/plans/2026-11-12-c02-camera-motion.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-12-c09-gltf-validation.md`](../docs/superpowers/plans/2026-11-12-c09-gltf-validation.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-12-production-recipe-dispatch.md`](../docs/superpowers/plans/2026-11-12-production-recipe-dispatch.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-12-ultra-non-gpu-closure.md`](../docs/superpowers/plans/2026-11-12-ultra-non-gpu-closure.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-12-ultra-nongpu-closure.md`](../docs/superpowers/plans/2026-11-12-ultra-nongpu-closure.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-13-asset-backed-camera-animatic.md`](../docs/superpowers/plans/2026-11-13-asset-backed-camera-animatic.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-15-c26-ace-step-integration.md`](../docs/superpowers/plans/2026-11-15-c26-ace-step-integration.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-15-d02-media-evidence.md`](../docs/superpowers/plans/2026-11-15-d02-media-evidence.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/plans/2026-11-15-engine-registry-rag.md`](../docs/superpowers/plans/2026-11-15-engine-registry-rag.md) | Plan de implementación fechado; usar solo con diff/CI actuales y marcarlo no canónico para estado. |
| [`docs/superpowers/specs/2026-11-12-ultra-non-gpu-closure-design.md`](../docs/superpowers/specs/2026-11-12-ultra-non-gpu-closure-design.md) | Spec de diseño fechado; contrato no certifica implementación actual. |
| [`docs/superpowers/specs/2026-11-13-asset-backed-camera-animatic-design.md`](../docs/superpowers/specs/2026-11-13-asset-backed-camera-animatic-design.md) | Spec de diseño fechado; contrato no certifica implementación actual. |
| [`docs/ultra/AGENT_CLOSURE_AUDIT.md`](../docs/ultra/AGENT_CLOSURE_AUDIT.md) | Snapshot A07/main vigente al baseline b612fbe; enlazado desde el handoff. |
| [`docs/ultra/CAPABILITY_EVIDENCE.md`](../docs/ultra/CAPABILITY_EVIDENCE.md) | Ledger con portada vigente y filas históricas identificadas; límites preservados. |
| [`docs/ultra/CLOSING.md`](../docs/ultra/CLOSING.md) | Añadido puntero al estado actual; informe de cierre conserva su corte histórico. |
| [`docs/ultra/POST_AUDIT_VERIFICATION_2026-11-09.md`](../docs/ultra/POST_AUDIT_VERIFICATION_2026-11-09.md) | Evidencia/plan W o auditoría fechada; conservar su digest/fecha y no generalizar al main actual. |
| [`docs/ultra/PROGRESS.md`](../docs/ultra/PROGRESS.md) | Añadido estado 17 nov; cuerpo W01 se conserva como registro histórico. |
| [`docs/ultra/SESSION_REPORT_2026-11-09.md`](../docs/ultra/SESSION_REPORT_2026-11-09.md) | Evidencia/plan W o auditoría fechada; conservar su digest/fecha y no generalizar al main actual. |
| [`docs/ultra/W01_ACQUISITION_SLICE.md`](../docs/ultra/W01_ACQUISITION_SLICE.md) | Evidencia/plan W o auditoría fechada; conservar su digest/fecha y no generalizar al main actual. |
| [`docs/ultra/W25_FINAL_REPORT.md`](../docs/ultra/W25_FINAL_REPORT.md) | Corregido enlace a baseline del handoff; cuerpo se conserva como informe fechado. |
| [`docs/ultra/W26_GPU_PLAN.md`](../docs/ultra/W26_GPU_PLAN.md) | Evidencia/plan W o auditoría fechada; conservar su digest/fecha y no generalizar al main actual. |
| [`docs/ultra/W26_GPU_RESULTS.md`](../docs/ultra/W26_GPU_RESULTS.md) | Evidencia/plan W o auditoría fechada; conservar su digest/fecha y no generalizar al main actual. |
| [`docs/ultra/audits/llm-server-llama-cpp-audit.md`](../docs/ultra/audits/llm-server-llama-cpp-audit.md) | Evidencia/plan W o auditoría fechada; conservar su digest/fecha y no generalizar al main actual. |
| [`docs/upgrades/COMFYUI_V0.25.md`](../docs/upgrades/COMFYUI_V0.25.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/upgrades/CUDA13_PYTORCH_2.15.md`](../docs/upgrades/CUDA13_PYTORCH_2.15.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`docs/walkthrough.md`](../docs/walkthrough.md) | Actualizado el catálogo curado; resultados de comandos siguen fechados como histórico. |
| [`studio/gltf-validator/node_modules/gltf-validator/ISSUES.md`](../studio/gltf-validator/node_modules/gltf-validator/ISSUES.md) | README/ISSUES vendorizados del tercero; no editar como documentación del producto. |
| [`studio/gltf-validator/node_modules/gltf-validator/README.md`](../studio/gltf-validator/node_modules/gltf-validator/README.md) | README/ISSUES vendorizados del tercero; no editar como documentación del producto. |
| [`tools/mac/README.md`](../tools/mac/README.md) | Guía operativa/código actual revisada por enlaces y referencias; conservar como fuente junto a implementación y tests. |
| [`upgrades-ultra-version/GOAL.md`](../upgrades-ultra-version/GOAL.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/MISSION.md`](../upgrades-ultra-version/MISSION.md) | Actualizada referencia main obsoleta y enlazado el handoff. |
| [`upgrades-ultra-version/README.md`](../upgrades-ultra-version/README.md) | Añadido handoff y diferencia entre paquete de investigación y estado vivo. |
| [`upgrades-ultra-version/VALIDATION.md`](../upgrades-ultra-version/VALIDATION.md) | Distingue validación del paquete de la CI actual de aplicación. |
| [`upgrades-ultra-version/acceptance.md`](../upgrades-ultra-version/acceptance.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/contexto/Marco_Anterior.md`](../upgrades-ultra-version/contexto/Marco_Anterior.md) | Marco histórico; no prevalece sobre MISSION/GOAL y acceptance actuales. |
| [`upgrades-ultra-version/design.md`](../upgrades-ultra-version/design.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/programs.md`](../upgrades-ultra-version/programs.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/requirements.md`](../upgrades-ultra-version/requirements.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/research.md`](../upgrades-ultra-version/research.md) | Añadido alcance histórico de lectura/research. |
| [`upgrades-ultra-version/runner-guide.md`](../upgrades-ultra-version/runner-guide.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/sources.md`](../upgrades-ultra-version/sources.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
| [`upgrades-ultra-version/tasks.md`](../upgrades-ultra-version/tasks.md) | Spec/plan de misión; estado referido al snapshot de investigación o actualizado arriba. Consultar acceptance y contrastar implementación. |
