# Auditoría y hoja de trabajo para llegar al MVP utilizable

**Corte:** 2026-11-15, zona `America/Bogota`
**Tipo:** auditoría de código, pruebas y documentos del repositorio; no es una certificación de producto ni de hardware.
**Propósito:** entregar a agentes implementadores tareas concretas, verificables y sin solapamiento.

## Dictamen ejecutivo

**ComfyUIPod todavía no está listo para producción ni para declararse MVP terminado.** Hay avances reales y comprobados en rutas CPU y en CI, pero el manifiesto de motores sigue declarando **228 bloqueos de release**. El resultado GPU W26 demuestra una sola receta SDXL en una imagen histórica, no la calidad general ni la operación del `main` actual. El mayor riesgo de ejecución es que aprobar una producción puede mantener una petición HTTP abierta hasta exponerse a un `504`; el mayor riesgo de reproducibilidad es que la huella de un artefacto no incluye el contenido de sus assets ni la versión resuelta del engine/modelo.

La definición de MVP recomendada para este producto —coherente con la elección del usuario de empezar describiendo un resultado y revisar el plan antes de ejecutarlo— es un flujo vertical completo: **describir → inspeccionar/editar plan → aprobar → ver progreso/errores → inspeccionar artefactos con procedencia → descargar un resultado reproducible**. La plataforma tiene piezas para este flujo, pero no evidencia suficiente de que la ruta completa sea durable, visible y trazable bajo fallos.

## Estado revisado y evidencia

| Área | Estado verificado | Límite |
| --- | --- | --- |
| Main después de PR #405 | `81f5bf39bb048f586ea3c25a045e568af85c07da`; PR #405 squash-merged. | No implica que todos los objetivos del programa estén cerrados. |
| CI de PR #405 | Run `43783405523`: validación del repositorio, Rust y build/smoke full-stack/Studio concluyeron en verde. | Al ser una PR, la promoción a `latest` se omitió; no es nueva validación GPU ni publicación de `main`. |
| PR #405 | Persistencia real de transcripciones WhisperX, exportación EDL a OTIO con lock y smoke de build, y ampliación del límite por agente OpenCode de 12 a 24 turnos. | Sus cambios de implementación no eliminan por sí solos los bloqueos restantes de A07. |
| Manifiesto de motores en el `main` actual | `python scripts/check_engines_manifest.py --check`: 88 motores; 57 seleccionados para build; manifiesto coherente; **228 bloqueos**; `release_ready=false`. | “Coherente” valida declaraciones y estructura; no equivale a adapter ejecutado ni motor listo. |
| Paquete Ultra | `python upgrades-ultra-version/validate_spec.py`: estructura válida; 33 capacidades, 14 programas, 30 candidatos, 26 paquetes y 115 fuentes. | Valida consistencia del plan, no implementación ni resultados. |
| GPU | W26 registró una demo SDXL de 24.6 s, pico medido de 7,684 MB y coste de USD 0.59, ligada a un digest histórico. | W26 declara explícitamente que faltan repetición en imagen actual, D01–D05, baselines/holdouts y mediciones de las demás recetas. Esa autorización histórica no aprueba gasto nuevo. |
| Pruebas locales de esta rama | Ruff completo pasó; 13 pruebas focalizadas del importador/RAG pasaron. La suite completa reportó 3,140 passed, 7 skipped y 4 fallos: tres por ausencia local de LangGraph y uno porque el sparse checkout omitía `.codex/hooks.json`; este último pasó al incluir `.codex` en el checkout. | No se repitió la suite completa después de corregir el sparse checkout. La CI de la nueva PR aún debe ser la autoridad integrada. |
| Workbook de engines | Rama de trabajo `codex/engine-registry-rag-v8`, commit local `0bf028e`: fuente original preservada, exportación con 2,169 filas y digest por fila; consulta RAG recupera por nombre y mantiene la etiqueta `EXTERNAL/UNVERIFIED`. | Esta rama aún no estaba en una PR al redactar el informe. El Excel no es whitelist, ni cambia el manifiesto/ledger de capacidades. |

Los snapshots de capacidad no están alineados con este `main`: `docs/ultra/status.json` reporta `main_sha=d7504cf` y 234 bloqueos para otra rama; `docs/ultra/CAPABILITY_EVIDENCE.md` tiene corte `3d5b93e`; `upgrades-ultra-version/snapshot.json` conserva `b54195f` de noviembre 5. El `main` revisado es `81f5bf3` y el checker actual devuelve 228. Antes de usar esos archivos para asignar trabajo o declarar cierre, deben actualizarse juntos con SHA/PR/CI/digest verificables.

El workbook tiene discrepancias que están documentadas en [`../research/catalogs/open-engine-registry-v8.0-data-quality.md`](../research/catalogs/open-engine-registry-v8.0-data-quality.md): la portada dice 826 registros, pero `Registry` contiene 2,169 filas; 1,064 no tienen URL de descarga; hay cuatro pares de URLs que solo difieren por mayúsculas y nombres distintos; 39 checks tienen fecha futura relativa al corte; y 502 filas tienen una marca sin leyenda. El campo `verified_this_session` es una afirmación de la fuente, no una auditoría independiente. La RAG debe usarlo para **descubrir candidatos** y luego verificar fuentes primarias, dependencias, licencias/pesos y encaje con los adapters existentes.

No encontré referencias a Atlas en Markdown/JSON de `docs/` y `upgrades-ultra-version/` en este corte. El catálogo nuevo no incorpora Atlas como capacidad.

## Hallazgos priorizados para implementación

### P0 — Cerrar el bloqueo del release, no inferir readiness del manifiesto

El checker sigue encontrando 228 bloqueos para 57 engines seleccionados. Entre los primeros reportados aparecen para WhisperX falta de lock con hashes, decisión de redistribución, revisión de notices y declaración A07 de entrypoint implementado, pese a que PR #405 añadió código real de transcripción. Esa diferencia muestra que el trabajo debe **reconciliar código, evidencia y manifest**, no volver a asumir que un merge cierra automáticamente la fila.

**Tarea para agentes:** clasificar los 228 hallazgos por engine y tipo; cerrar en lotes con scopes de IDs disjuntos. Cada fila seleccionada debe tener revisión de código con revisión inmutable, lock hashado, decisión de redistribución, notices revisados, entrypoint y prueba versionada. El agente coordinador consolida cambios compartidos en `upgrades-ultra-version/engines.json` después de recibir evidencia; no permitir que agentes paralelos editen simultáneamente ese mismo manifiesto.

**Aceptación:** `python scripts/check_engines_manifest.py --check` coherente y sin bloqueos no-GPU para los engines requeridos; CI confirma checks obligatorios y smoke de build. Si algo realmente depende de GPU/medición, debe quedar como el único residual identificado, con una razón concreta.

### P0 — Capacidad de hardware dinámica y observada

El audit de código halló defaults de perfil de 96 GB VRAM/128 GB RAM en `studio/comfy_studio/hardware_profile.py`; no son una medición del host ni de sus cgroups. La expectativa del producto es dinámica: un host con 200 GB disponibles no debe tratarse como un perfil fijo 128 o 256 ni provocar OOM o desperdicio silencioso.

**Tarea para agente:** separar `capacity_observed` (RAM/cgroups/GPU telemetry disponible) de `target_profile` o preferencias; calcular admisión a partir de capacidad real y registrar incertidumbre. No convertir memoria total del host en memoria libre para el pod sin descontar sistema y servicios.

**Aceptación:** fixtures para host/cgroup de 128, 200 y 256 GB, límites cgroup menores al host, telemetría ausente/conflictiva y memoria usada por otros servicios; estado y reason legibles en UI/API; ninguna afirmación de capacity si no se midió.

### P0 — Aprobación de producción debe iniciar un trabajo durable

`studio/comfy_studio/routers/productions.py` llama la ejecución dentro de una petición HTTP bloqueante; el propio router documenta riesgo de `504`. El checkpointer existe, pero aprobación no devuelve un handle de job durable.

**Tarea para agente:** convertir la aprobación en una transición idempotente de plan a job/resume, devolver un ID rápidamente y exponer progreso, cancelación y resultado terminal por el flujo de status/eventos existente.

**Aceptación:** pruebas cubren aprobación duplicada, restart con trabajo en curso, cancelación y fallo final; el cliente obtiene un handle antes de que corra el trabajo largo.

### P1 — Procedencia real para invalidar artefactos obsoletos

La huella de shot en `studio/comfy_studio/orchestration/executor.py` incluye descripción, duración y parámetros de layer, pero no hash de contenido de inputs ni identity/version del engine, modelo y runtime. Cambiar una fuente o el motor podría dejar la huella igual.

**Tarea para agente:** añadir hashes de assets de entrada y versiones/configuración resueltas al manifiesto de salida y a la invalidación de descendientes.

**Aceptación:** fixtures demuestran que mismos inputs/config producen misma identidad, y que cambiar media/engine/modelo cambia la huella y marca descendientes obsoletos; la salida registra pruebas/QA y rutas de descarga.

### P1 — Cumplir el flujo de motor multimodal C01

La especificación C01 requiere búsqueda semántica multimodal. El manifest dice que `qwen3-vl` no está implementado y no tiene entrypoint/prueba; el ledger conserva la indexación FFmpeg pero no demuestra pesos ni recuperación neuronal.

**Tarea para agente:** conectar un adapter local/provider detrás de los límites existentes que devuelva evidencia con timestamps, aislar fallos por asset y mantener resultados léxicos disponibles cuando el backend falte.

**Aceptación:** contratos CPU para timestamp, asset corrupto, timeout y backend ausente; el estado nunca llama a la búsqueda “semántica validada” sin ejecución/medición de calidad real.

### P1 — Integrar generación 3D sin confundirla con cargar un GLB

C09 tiene evidencia parcial de cargar/renderizar GLB propiedad del proyecto; el engine requerido TRELLIS.2 figura sin implementar. Esa ruta no prueba generación/reconstrucción ni marca geometría inferida.

**Tarea para agente:** proponer un adapter TRELLIS.2 opcional, con contrato de input/output, ownership del GLB, provenance, límites de memoria y etiquetado de inferido/observado. Mantener pesos bajo demanda.

**Aceptación:** fixtures validan estructura/tamaño/procedencia del GLB y fallback de pesos ausentes; ejecución GPU y calidad visual se registran aparte, sin elevar nivel antes de medir.

### P1 — Completar ruta de audio generado C25

El pipeline de C25 monta audio de muestras del usuario, pero no genera/infiera foley desde la escena. `controlfoley`, `mmaudio` y `sam-audio` permanecen sin implementar.

**Tarea para agente:** conectar un solo engine permitido al flujo de stems, preservando acciones fuente, timebase, duración y ownership del resultado.

**Aceptación:** fixtures confirman onset/timebase y manifest de audio; si faltan pesos/backend, la API lo reporta como no disponible en vez de simular generación real.

### P1 — Corregir feedback de errores de la interfaz

En `panel/app.js`, la carga de lista ignora respuestas HTTP no-OK y puede dejar “Cargando proyectos…” indefinidamente. La ruta preferida del producto empieza por agente/plan, pero un fallo de servicio deja al usuario sin siguiente acción.

**Tarea para agente:** estado de error visible para 401/5xx con retry y guía de autenticación coincidente con “Cuenta y sistema”; manejar fallo de red y dejar accesible el estado.

**Aceptación:** tests del panel para 401, 5xx y error de red; ninguna carga indefinida; texto del botón/destino coincide con navegación actual.

### P1 — Reportar enforcement real del harness

El harness de Codex registra algunas capacidades locales como fuera de cobertura y no puede bloquear ciertos límites/modelos; el contrato indica que esto no equivale a enforcement completo.

**Tarea para agente:** resolver con hook/adapter de despacho efectivo o hacer que `run status`/documentación/CI marque esos runs como parciales/no conformes, sin afirmar que la confianza de hooks da enforcement que no ocurre.

**Aceptación:** test declara qué herramientas, subagentes, límites y checkpoints fueron efectivamente interceptados; evidencia persistida por run; comportamiento no conforme falla cerrado o aparece explícitamente restringido.

### P2 — Durable, configuration-bound engine readiness

`engines/preparedness.py` guarda `execution_validated` en memoria sin timestamp, revisión de engine ni identidad de weights/config. El estado puede perderse con restart o aplicarse a una configuración distinta.

**Aceptación:** evidencia durable indexada por engine revision + modelo/weight hash + perfil; distinguir “se ejecutó una vez” de “validado con esta configuración” y cubrir restart/fallo.

### P2 — No llamar “GPU validado” a una prueba de contrato/fixture

El workflow de CI corre contratos CPU/stubs y full-stack container smoke. Eso es evidencia valiosa pero no es calidad neuronal ni fit de VRAM. Añadir un resumen de evidencia en CI que marque simulated/CPU/stub vs real engine vs hardware medido.

**Aceptación:** artefacto/check de CI lista suite, perfil, image digest, backends simulados/reales y nivel de evidencia; no promocionar claims L3/L4 sin la campaña correspondiente.

### P2 — Accesibilidad de navegación

`switchView()` cambia el contenido/título, pero mantiene el foco en el elemento de navegación anterior.

**Aceptación:** navegación por teclado mueve foco a heading/región principal y comunica el nuevo view a lectores de pantalla; test de regresión y revisión con axe/teclado.

## Plan de ejecución para agentes y coordinador

Ejecutar en lotes con worktrees aislados; cada implementador recibe una sola tarjeta de esta auditoría, base SHA, archivos permitidos, test de aceptación y una instrucción de no tocar manifest/ledger compartidos. Con el límite actual de cuatro slots totales, usar hasta tres agentes más el coordinador a la vez. En lotes de engines, cada agente devuelve su evidencia en un archivo propio y el coordinador integra después las filas comunes.

1. **Lote de bloqueos y seguridad de release:** tres agentes auditan particiones disjuntas de engine IDs y preparan fixes/evidence; integrar una sola vez el manifiesto común. Resolver el caso WhisperX/OTIO en el mismo lote.
2. **Lote de fundamento del flujo:** hardware dinámico, producción durable y lineage de artefactos son scopes independientes; ejecutarlos concurrentemente.
3. **Lote de producto:** feedback/accesibilidad UI, C01 multimodal y C09 3D pueden avanzar en paralelo al lote 2 si los owners de archivos son distintos. C25 audio y C02 editable se asignan después con contratos claros.
4. **Lote de evidencia y cierre:** refrescar `docs/ultra/status.json`, `docs/ultra/CAPABILITY_EVIDENCE.md` y `upgrades-ultra-version/snapshot.json` al SHA real de `main`; no mantener números de blockers ni digest históricos como si fueran actuales. Correr una única validación integrada cuando las correcciones del lote estén juntas.
5. **GPU únicamente al cierre:** usar digest de imagen exacto y autorización de gasto vigente. La misión de Ultra condiciona gasto/API nuevos a autorización específica; W26 no la sustituye. Medir luego D01–D05, perfiles reales y baseline/holdouts conforme a aceptación.

Para reducir fallos de integración, cada PR debe indicar SHA base/head, tests y claims que **no** valida. Tras el merge, revalidar main y digest antes de refrescar el snapshot. No convertir directamente filas del Excel a `build_selected`; el registro es una fuente RAG externa, no una decisión de producto.

## Artefactos revisados

- Manifiesto/release audit: `upgrades-ultra-version/engines.json`, `scripts/check_engines_manifest.py`.
- Scope y acceptance: `upgrades-ultra-version/GOAL.md`, `MISSION.md`, `acceptance.md`, `backlog.json`, `tasks.md`.
- Evidencia declarada: `docs/ultra/CAPABILITY_EVIDENCE.md`, `docs/ultra/status.json`, `upgrades-ultra-version/snapshot.json`.
- RAG de investigación/catálogos: `scripts/research_rag.py`, `docs/research/datasets/`, y el nuevo registro Open Engine Registry v8.0.
- Producto: `panel/app.js`, `panel/index.html`, `studio/comfy_studio/routers/productions.py`, `studio/comfy_studio/orchestration/`, `studio/comfy_studio/hardware_profile.py`, `studio/comfy_studio/engines/preparedness.py`.
- Resultado GPU histórico: `docs/ultra/W26_GPU_RESULTS.md`.
