# Diseño técnico de Ultra Research

Este es un diseño a implementar, no una descripción de código ya entregado. Reutilizar los módulos existentes y migrar de forma incremental. Los nombres nuevos son propuestas de organización; no crear duplicados si el repo ya resuelve el contrato.

## Una autoridad de producción

La API existente mantiene proyectos, assets, ejecuciones y autenticación. El estado de producción se extiende desde ProductionState y el checkpointer existentes. LangGraph se incluye y prueba en el perfil de producción; el fallback ligero solo sirve al entorno que lo declara explícitamente. El agente de producción no depende de instalar Kiro Crew.

Ruta de una misión:
ingesta → comprensión/evidencia → hipótesis/brief → plan con constraints → animatic → adquisición de componentes → stages de motores → QA dimensional → reparación local → mezcla/acabado → entrega.

Cada transición persiste estado y referencias a artifacts. Una API de cancelación llega a todos los subprocess groups y descargas; no se limita a ocultar progreso en la UI. Reanudar evita repetir efectos externos o trabajos ya verificados mediante claves idempotentes.

## Contratos de datos

Preferir esquemas JSON versionados y tipos compatibles con el repo. No forzar una migración integral de Pydantic/dataclasses. Agregar migraciones, validadores y ejemplos mínimos antes de nuevos engines.

| Entidad | Campos y semántica esenciales |
| --- | --- |
| Mission | id/version, project, intent hypotheses/evidence, público, restricciones, presupuesto de pasos/tiempo/coste autorizado, deadline opcional, estado/stop_reason |
| Asset | content hash, original/private path, mime/codec, owner/license/consent, orientación/color, duración/fps, proxy, segment index y anomalías |
| Evidence | origen, asset/segment/timestamp o place/view, observado/inferido, método/model revision, confianza calibrable y limitación |
| Scene | unidades SI, sistema de ejes/handedness, world origin, transforms y coordenadas locales derivadas de WGS84 |
| Camera | intrinsics, distortion, focal/sensor, poses temporalmente indexadas, near/far, estado de calibración y provenance |
| Actor/Object | ID estable, geometría/escala, materiales, rig/body model, bind pose, pivotes/articulaciones, identidad y zonas inferidas |
| Action | actor/target, start/end racionales, path/contact constraints, fuerzas/dinámica cuando existe, seed y procedencia |
| Layer/AOV | RGB/alpha/depth/normals/flow/albedo/light/roughness/etc, timebase, espacio/color, premultiplicación, unidades y masks de validez |
| SoundEvent | causa/actor, onset/duración, posición opcional, texto/voz/referencia, stem, gain, licencia y sincronización |
| Shot | escena/cámara, intervalos/assets, requisitos de identidad/lugar, constraints y condiciones de aceptación; receta seleccionada |
| Recipe | engine+code+weights revisions, quant/precision, resolución/frames/contexto, offload/tiling, hardware/driver/kernel, schema y measurements |
| Artifact | content hash, tipo, path, parents, producer invocation, seeds, input/output schema, calidad/evidence y retención |
| Experiment | hipótesis previa, baseline/treatment, dataset split, variables, métricas/tolerancias, presupuesto, resultados/fallos y decisión |

El reloj de producción usa índices de frame y fracciones exactas. Mantener audio con sample rate y conversiones explícitas. Tratar video VFR y rotación antes del análisis. No redondear tiempos repetidamente ni usar duración estimada como verdad.

Transformaciones físicas:
- WGS84→origen local de escena con transform explícito.
- Poses de OpenCV/OpenGL/Blender/USD con convención declarada y pruebas de un objeto asimétrico.
- Profundidad en metros frente a profundidad relativa con flags distintos.
- Rig/retargeting con correspondencias, escala, root motion y contacto; no asumir SOMA/SMPL/FLAME equivalentes.
- OpenPBR/mapping de materiales explícito; un AOV faltante es desconocido, no cero.
- OCIO para lineal/display, rango, primarias y alpha; no duplicar transform de display en el encode.
- OTIO/EDL preservan montaje soportado; efectos no traducibles quedan referenciados al proyecto fuente, no descartados silenciosamente.

## DAG y conservación de trabajo

Cada nodo depende de hashes de inputs, código, pesos, parámetros y contratos. Una caché de artifacts no usa solo template_id. Cambiar luz invalida relight/composición y sus métricas, no ASR; cambiar texto de un título invalida el título/comp/encode, no geometría o actuación.

La reparación parte de capas/pases/fuentes sin pérdida conservados. Comparar invariantes antes del encode de distribución: H.264/HEVC pueden cambiar bytes o frames al reencode sin que se haya regenerado contenido. No exigir identidad binaria de dos MP4 completos como única prueba de conservación.

Un resultado de QA identifica dimensión/región/intervalo y severidad. Replanificar con escalones limitados: misma receta → alternativa compatible → cambio de shot/plan. Los contadores se guardan; resumir contexto no los borra.

## Protocolo uniforme de motores

Extender el contrato actual de engines; nombres ilustrativos:
- describe/probe: versión, capacidad, esquemas, componentes faltantes, hardware/driver/ABI.
- prepare/admit: recipe exacta, espacio/recursos y lease; no instala código ni dependencias.
- acquire: componentes de modelo por manifest, con descarga e integridad transaccionales.
- execute: input/output tipados y subprocess/job ID, eventos y artifacts reales.
- cancel: terminación cooperativa y límite para kill de grupo.
- unload/stop: libera pesos/procesos/VRAM y devuelve prueba de estado.
- verify: tests reales de entorno/contrato; evidencia GPU solo si hubo ejecución y telemetría.

Un exit 0 no basta: validar existencia, tipo, contenido, dimensiones/timebase, hash y relación con inputs. Si el motor requiere otro dato, devolver input_missing con el campo concreto. Nunca un silent pass-through como “fallback”.

El health expone dimensiones independientes:
code_prepared, dependencies_prepared, integration_registered, weights_state, hardware_eligibility, loaded, execution_validation.
weights_state diferencia missing/staging/corrupt/verified; hardware_eligibility diferencia unknown/estimated/measured/supported_by_recipe. Un mock no cambia execution_validation a real.

## Entornos y build de una sola imagen

Usar rutas de código/venv/binaries de solo lectura, por ejemplo /opt/engines, y workspaces/cachés de datos separados. Agrupar engines que sean compatibles sobre un lock compartido, y aislar solo incompatibilidades reales. No meter 57 copias completas de CUDA por tener 57 componentes seleccionados en el spec.

Fijar commits/hashes y construir wheels/extensiones nativas durante build. Usar etapas de build y wheelhouse/cache cuando el permiso de redistribución lo permita. La compilación especializada necesaria para los kernels debe estar resuelta para la arquitectura prevista; JIT normal de un backend se declara y cachea, no se confunde con instalar un motor completo.

El primer build representativo y el presupuesto completo preceden a la expansión. Medir:
bytes de layer comprimidos, tamaño instalado por env/engine, temporales máximos del builder y descarga/pull del pod. Reconciliar bases/layers compartidas para no sumar varias veces el mismo coste.

Si el runner gratuito no alcanza, documentar el recurso faltante y usar solo infraestructura ya disponible/autorizada que lo tenga. No alquilar build hosts ni mover la preparación de engines al pod para evitar el problema. Un build-only OCI/cache/Release no implica otro runtime ni otro repo.

La imagen final contiene binarios, frontend compilado, navegador/fonts necesarios, scripts/model manifests, instrucciones de licencia, engine locks y atlas base. Los pesos de tareas no se incluyen salvo un componente mínimo indispensable explícitamente justificado. El arranque comprueba code/env/assets y reporta missing weights, sin descargar catálogos ni hacer pip/npm/git clone.

## Integridad y adquisición de modelos

Una receta declara exactamente componentes y revisiones. El planificador conoce el conjunto de archivos necesario antes de descargarlo. El CLI HF y la ruta Python deben usar la misma revisión fija. Para HF LFS, el digest del contenido no se confunde con el SHA del blob Git.

Protocolo de adquisición:
1. Resolver manifest/consentimiento/territorio y locks por componente.
2. Verificar la entrada cacheada contra tamaño/digest/revisión; un archivo no vacío no es suficiente.
3. Reservar disco de destino y staging considerando filesystem, paralelismo, duplicados y reserva.
4. Descargar a temporal identificable; soportar reanudación y cancelación sin publicarlo como válido.
5. Si hay EXDEV, copiar a un temporal en el filesystem de destino, comprobar, fsync y hacer os.replace. Mantener el destino anterior hasta esa operación.
6. Publicar metadatos de integridad y liberar locks; limpiar temporales con política acotada.
7. Ante mismatch, marcar corrupt y no ejecutar; reintentar solo según presupuesto.

No guardar URLs firmadas/tokens como provenance pública. Versiones actualizables de manifest se producen por PR; el pod usa una versión efectiva precisa.

## Admisión y lease global GPU

Un scheduler de recursos arbitra todos los engines, incluidos ComfyUI, Blender GPU, audio, VLM y jueces. Un mutex de thread no sirve entre procesos. Puede usarse una primitiva OS/SQLite transaccional con owner token y fencing; demostrar su corrección en tests multiproceso.

El owner tiene identidad de proceso/grupo y nacimiento, UUID de GPU, recipe/job y estado. Una lease caducada no se concede a otro mientras el proceso anterior sigue usando la GPU. Recovery verifica y termina el propietario huérfano cuando es seguro, comprueba liberación y solo entonces concede otra lease. Una liberación tardía de un owner antiguo no revoca al nuevo.

Reservar memoria real disponible menos margen para drivers/runtime/OS; no usar “94 GB” como hecho físico. Admisión de RAM incluye VAE/encoder CPU, caché y trabajos en paralelo. Si no cabe: cambiar precisión/tiling/CPU/duración/resolución bajo restricciones, o rechazar esa receta con razón. El budget de una producción no se reinicia porque un engine haya hecho OOM.

Telemetría por stage: peak VRAM, host/cgroup peak, carga/unload, setup, inferencia, QA, descarga, tiempo total, tamaño outputs y estado de cleanup. La medida simulada se etiqueta y se excluye de la selección real.

## Archivo e inspiración

Reutilizar percepción/ASR/sqlite-vec. Añadir segmentos, eventos, OCR, hashes perceptuales cuando aporte y relaciones asset→intención. Procesar por lotes y con backpressure; un archivo corrupto no derriba toda la misión.

El brief se compila desde evidencia: “se escucha anuncio de evento” no autoriza inventar patrocinador, fecha o precio. Guardar hipótesis y su selección. El agente puede producir una interpretación conservadora viable sin preguntar constantemente.

Conectores de inspiración:
- Biblioteca propia y referencias autorizadas como ruta siempre disponible.
- Fuentes públicas con API/feed permitido y cuota gratuita, opcionales según credenciales/términos.
- Metadatos de rendimiento con timestamp, denominador, antigüedad y confianza; views de plataformas distintas no se suman como una métrica común.
- Descargar/analizar medios solo cuando el acceso y su uso estén permitidos. Tener una URL pública no acredita licencia de reutilización.
- El API de YouTube no permite descargar subtítulos de cualquier video por ser público; manejar permisos reales. No prometer transcripción universal por endpoint oficial.
- Chromium se usa para fuentes permitidas que requieren render, con timeout/caché/reintentos y sin eludir bloqueos. No es un mecanismo para evadir límites.
- Extraer receta (hook, duración, beats, movimientos) y adaptarla al brief propio, no clonar el archivo ni atribuir éxito causal demostrado.

No se publican contenidos ni se mandan mensajes automáticamente. El entregable incluye copy/hashtags como hipótesis basadas en contexto, con fuentes/fecha si hay datos recientes, no una garantía de alcance.

## Atlas y geometría

Mantener el bundle base como entrada inmutable de build del mismo repo. Conservar los medios reales, metadatos, licencia, hashes, thumbnails, índice, calibration reports, transforms y coverage atlas. Si un dominio desaparece, el bundle publicado sigue funcionando offline.

Asignar a cada región/vista coverage: catalog_only, observed_single, observed_multiview, calibrated, reconstructed_validated, inferred. Incluir fecha, exactitud/posición/rumbo desconocidos y regiones ocluidas. Unknown no se rellena con heading=0 ni timestamp actual.

Selección:
- Lugar explícito en parámetros o en el prompt tiene prioridad sobre selección previa.
- La vista explícita pasa exactamente los mismos checks que la seleccionada automáticamente.
- Municipio/nombre/coords y semejanza visual se cotejan; radio cercano no prueba que el fondo corresponda a la fachada.
- strict_exact aplica límites de posición/cámara/calibración y coverage del shot completo.
- Sin cobertura, elegir shot restringido a evidencia, adquirir una vista útil de fuente permitida o terminar esa variante como insuficiente. Si el lugar fue fijado, no sustituirlo por otro.

Reconstruir con MapAnything/DA3 y/o SfM que se seleccione por evidencia; guardar imágenes de entrada, poses, solape y evaluación con vistas excluidas. Un solo depth map sirve para un proxy 2.5D, no certifica geometría oculta.

La adquisición activa propone la siguiente zona/ángulo que reduzca incertidumbre y busca evidencia autorizada. Solo una actualización de bundle validada convierte nuevos archivos en baseline público. Un pod puede mantener overlays privados de trabajo con procedencia, sin publicar automáticamente assets personales.

## Edición causal, luz y simulación

Causa→acción→contacto→efecto→sonido son relaciones explícitas, no solo embeddings. El grafo permite eliminar un actor y recalcular sombras, reflejos, oclusiones o sonidos afectados. La edición neural se compara con rerender físico sintético y luego se evalúa en escenas reales con incertidumbre.

Los AOVs de V-RGBX/UniRelight/DiffusionRenderer requieren mapping: no llamar roughness a una salida con otra interpretación. Alpha y máscaras de efectos conservan timebase y color. Objetos/sombras/transparencias pueden necesitar capas distintas.

Chrono y Blender generan estados de vehículos/articulaciones y pases. La transferencia a video neural se acepta solo si conserva controles relevantes. Sombra plausible no prueba fuerza/masa recuperadas; distinguir parámetros observados, asumidos y optimizados.

## Voz, música y sonido

Timeline de eventos común con onset/duración, stems separados y ducking. ControlFoley es ruta dirigida; MMAudio baseline. ACE-Step crea música/variantes según estructura; Qwen3-TTS/IndexTTS se comparan en español. Cada hablante conserva texto/voz/identidad separados.

Audio2Face necesita correspondencia de blendshapes al rig del avatar; no hay compatibilidad universal automática con MARCUS/FLAME/Gaussians. Conservar la actuación editable además del MP4 de la cara.

Mezcla/final: sample rate, loudness/true peak definidos por preset, sincronía, colas/loops y ausencia de clipping. No confundir música conjunta de un modelo con stems limpios independientes.

## Interfaz y cliente

Usar componentes MIT auditados de OpenCode y un StudioClient generado/tipado para las rutas propias. Adaptar semántica del estudio; no emular una API de coding entera por estética. Mantener licencias y local assets. Los proyectos/editables son del repo del estudio.

Vistas mínimas: conversación y brief, archivos/segmentos, storyboard/timeline, escena/cámara/cobertura, herramientas y recursos, comparador de variantes/QA, exports y procedencia. El detalle técnico aparece cuando ayuda a decidir, no como una pared de logs para el usuario.

SSE/WebSocket según protocolo existente: reconnect/replay, event IDs y estado de snapshot para no duplicar trabajos. Cancelar/pausar/reanudar y parámetros simples de calidad/presupuesto tienen efecto real. Pruebas Playwright sobre backend determinista y algunas rutas reales CPU, sin red externa.

Cliente común de terminal en Python/Rust según la base actual: status, upload, submit, cancel, logs redactados y download. No depender de Bash/osascript/keychain para Windows/Linux. Paths con espacios, unicode, CRLF, SSH y entornos de variables tienen pruebas por plataforma.

## Investigación y mejora

P14 diferencia dos flujos:
- Producción: ejecuta herramientas/recetas preparadas y conserva observaciones.
- Desarrollo/investigación: propone cambios de código/receta en branch, verifica, hace PR y promueve por CI; no instala código nuevo en el pod de producción.

Priorizar optimización de parámetros, componentes y recuperación antes de training. Un experimento define la mínima mejora relevante y cómo contará coste de fallos. Comparar bajo mismo input/seed cuando sea útil y seeds/escenas heldout. Los jueces no deben leer qué opción se pretende favorecer. Reportar distribuciones y límites, no solo la mejor muestra.

Un resultado negativo es conocimiento. No forzar una “mejora” por gastar el presupuesto. Promover solo si cumple restricciones, no empeora dimensiones críticas y aporta mejora en el criterio predefinido; guardar rollback.

