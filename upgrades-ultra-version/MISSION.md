# Misión extendida — ComfyUIPod Ultra Research

Usa el runner disponible según runner-guide.md; CrewAI y Kiro Crew son distintos. Actúa como responsable técnico de una misión extensa de implementación en https://github.com/tomas-estrada/comfyuipod. Usa kirocrew-conductor cuando la versión instalada lo soporte, workers con trabajo independiente y el work ledger para comprobar sus entregas. Kiro Crew desarrolla el estudio; no sustituye su agente de producción.

## Resultado que debes producir

Convierte el repo en un laboratorio audiovisual personal, autónomo y reproducible que transforme un brief ambiguo o preciso y assets heterogéneos en producciones profesionales editables y reels/Shorts terminados en 4K. Debe comprender propósito, público colombiano, identidad, espacio, acciones, montaje y sonido; buscar inspiración autorizada, elaborar hipótesis, ejecutar herramientas, evaluar resultados y reparar solo lo necesario sin pedir instrucciones a cada paso.

Implementa las 34 capacidades C01–C34 y los 14 programas P01–P14 descritos en los anexos. Resuelve explícitamente las 30 alternativas N01–N30: son herramientas candidatas, no 30 obligaciones de instalar duplicados. Cada capacidad obligatoria necesita una ruta funcional; rechazar una alternativa no elimina el requisito. La superioridad se acreditará en tareas y métricas propias frente a baselines reproducibles; no afirmes superar universalmente a plataformas comerciales ni garantizar viralidad.

La misión incluye implementación, verificación y validación progresiva. W26 ya ejecutó una demo SDXL acotada (USD 0.59) en el digest `sha-f5c29dc`; el estado es GPU L3 parcial, no “Ultra validada”. No alquiles otro pod ni uses APIs facturadas sin autorización específica vigente. Resta probar los fixes operativos en una imagen actual y completar las recetas, perfiles, baselines y holdouts pendientes.

## Paquete obligatorio y precedencia

Lee primero AGENTS.md y reglas aplicables del checkout; después upgrades-ultra-version/README.md, requirements.md, tasks.md y el mapa de backlog.json. Consulta design.md, research.md y acceptance.md para las reglas globales; utiliza programs.md, capabilities.json, engines.json y sources.json por los IDs de cada lote. contexto/Marco_Anterior.md es referencia histórica, no lectura obligatoria repetida. Esta misión extendida y los anexos actuales corrigen premisas antiguas. No inyectes todos los anexos en cada worker: entrega solo el contexto pertinente y los invariantes globales. No existe un límite global de 90 minutos para Ultra; el límite de un objetivo anterior no aplica.

Si falta un anexo, busca primero su versión en la rama/PR de esta carpeta y el remoto autorizado; solo si no existe registra un único bloqueo con la ruta exacta. No improvises una implementación reducida ni declares terminada la misión con un catálogo o una portada.

## Arquitectura que debes respetar

1. Un usuario, un pod Linux y una GPU RTX PRO 6000 Blackwell de 96 GB. Perfiles de RAM host 128 y 256 GB separados, limitados por memoria y cgroups reales. macOS/Windows/Linux son clientes de terminal y navegador; no recrees un helper exclusivo de macOS.
2. Una imagen de producción del mismo repo en su GHCR. Motores seleccionados, dependencias, extensiones, fuentes, binarios, adaptadores y recursos de interfaz preparados durante build. En despliegue el agente no instala ni cablea motores. Puedes emplear etapas/imágenes intermedias de build, sin exigir otros contenedores runtime.
3. Pesos, LoRA, VAE, encoders y otros componentes se adquieren bajo demanda mediante manifiestos con versión, archivos exactos, integridad, licencia, tamaño y dependencias. Descarga lo necesario para una receta, no el repositorio completo ni todos los modelos del catálogo. El bootstrap no incluye una biblioteca de pesos “por si acaso”.
4. El atlas base del Aburrá debe viajar físicamente en esa misma imagen: catálogo, medios autorizados, atribuciones, hashes y pruebas offline. Releases del mismo repo pueden ser entradas inmutables de build. Ningún sandbox, enlace externo o segundo repo constituye la entrega.
5. 180/200 GB no son techo ni mínimo fijo. Calcula tamaño instalado y comprimido, staging de descargas, modelos elegidos, caché, medios, temporales y reserva. Comprueba temprano que la infraestructura de build autorizada puede construir la imagen; no presupongas disco infinito en GitHub Actions.
6. Un stage GPU pesado a la vez, con lease global entre procesos y motores, admisión de RAM/VRAM, descarga y liberación comprobables, timeout/cancelación y recuperación tras fallo. Cuantización y offload de componentes/CPU son válidos; streaming por capas será una receta excepcional explícita. No inventes que todos los modelos o entrenamientos caben.
7. Procesa proxies y HD/FHD cuando convenga, conserva fuentes aprobadas y termina cada entrega con upscale/acabado a la resolución 4K adecuada al formato. Más píxeles no equivalen a recuperar detalle factual perdido.
8. Conserva y amplía el backend/API/orquestación existentes. LangGraph debe usar persistencia real en producción; un fallback de pruebas no puede hacerse pasar por el runtime validado. Una sola autoridad de estado, presupuestos y cancelación, sin dos directores que compitan.
9. Proveedores LLM/VLM intercambiables. Configuración y secretos se introducen en runtime y se redactan en errores/SSE/logs/checkpoints. La clave Vast permanece en el cliente operador. Medios personales solo en rutas aprobadas; nunca en Git, imagen pública o artifacts de CI.
10. Sin servicios comerciales obligatorios ni costes nuevos. Distingue licencia de código, pesos, datos, redistribución de imagen, acceso y territorio. Research no autoriza redistribuir cualquier material. Componentes restringidos requieren una ruta permitida o un bloqueo concreto, jamás un “done” ficticio.

## Comienza por hechos y reparaciones

Dentro de los primeros 10 minutos activos entrega un resumen breve con SHA local/remoto, árbol de trabajo, PRs existentes relevantes, último CI y próxima entrega. La referencia `b54195f` y el contexto de PR #63 son históricos. El checkout revisado para esta actualización es `main` en `5850191`; revalida estado, pruebas e imagen en el momento de trabajar. No vuelvas a imponer la revisión inicial sobre cambios posteriores ni uses `docs/ultra/status.json` como estado vivo: aún apunta a `f5c29dc`.

Reproduce y corrige primero, si siguen presentes:
- Caché: un archivo corrupto no vacío se acepta como adquirido y disponible sin verificar tamaño/hash.
- Descarga HF: la ruta CLI omite --revision aunque el manifiesto declare una.
- Movimiento entre filesystems: el fallback EXDEV escribe sobre el destino final y deja parciales o destruye la versión anterior.
- Recetas: mediciones simuladas pasan a “measured”; se mezclan resoluciones/GPU y no se filtra por perfil.
- Sizing/strict check/helper siguen sumando todo el catálogo y bootstrap antiguo aunque el arranque real no los descargue.
- Perfiles nominales no limitados por NVML/cgroups y ausencia de prueba de exclusión GPU entre motores.
- Atlas: comprobar la cámara de la vista solicitada explícitamente; distancia, rumbo o calibración desconocidos no acreditan exactitud. Comparar metadatos con imágenes y comprobar que el fondo seleccionado llega al grafo ejecutado.

Añade pruebas de regresión que fallen antes y pasen después. No reabras un problema ya corregido; conserva el caso de prueba y su evidencia.

## Implementación científica y de producto

Construye un contrato compartido de producción que una assets con tiempos reales, brief, hipótesis, escena, cámaras, geometría, rig, capas, acciones, iluminación, sonidos, montaje, procedencia y medidas. Usa contratos versionados y transformaciones explícitas, OpenUSD/OTIO/OCIO cuando aporten, DAG con invalidación selectiva y checkpoints durables. Cambiar un título no debe regenerar actores; cambiar iluminación no debe recalcular transcripción.

Entrega rutas completas para:
- Archivo semántico audiovisual, deduplicación, transcripción/diarización, OCR, análisis temporal, comprensión de producto/evento/trend y memoria de preferencias.
- Investigación de referencias accesibles y autorizadas, análisis de ritmo/movimiento/gancho y transformación en receta editable, adaptada a assets propios. La autonomía no depende de publicar ni de saltarse accesos de plataformas.
- Dirección cinematográfica por restricciones y animatics: cámara, focal, trayectoria, duración, legibilidad, identidad, oclusiones y continuidad.
- Atlas verificable y reconstrucción multivista; productos 3D/PBR, partes, rigging y ajustes inversos de geometría, materiales y luces.
- Movimiento humano, coreografías, transferencia de actuación, trayectorias de objetos, contactos, dinámica de vehículos y productos articulados.
- Actuación facial/voz persistentes, actores hablantes, memoria entre planos, reconstrucción 4D y escenarios explorables con cobertura declarada.
- Edición causal, omnimattes y capas RGBA, iluminación temporal, reparación local y composición que conserve las partes aprobadas.
- Foley dirigido por eventos/referencias, ambientes, acústica cuando aporte, música con estructura y voz en español con pronunciación local.
- HyperFrames local, Remotion donde su licencia lo permita, Blender/FFmpeg, exportación editable, subtítulos, audio y acabado temporal 4K.
- Interfaz agéntica apoyada en componentes MIT auditados de OpenCode, con un StudioClient propio sobre la API del estudio: conversación, archivos, previews, timeline, progreso de herramientas, pausas/cancelación, reanudación, evidencia y controles. No supongas que su SDK ya habla LangGraph.
- Investigador experimental que formule hipótesis medibles, compare recetas con holdouts y controles, acumule una frontera Pareto y solo promueva cambios con evidencia. LoRA y aprendizaje son selectivos y respetan licencias y datos; no modifican el código de producción en caliente.

Las rutas y sustitutos están resueltos en engines.json y programs.md. Prioriza LTX-2.5 y Wan para una base audiovisual local; MiniMax-H3 es condicional por alcance publicado y licencia. No invoques sus etapas hospedadas como si fueran abiertas. HyperFrames local es útil, pero sus fuentes/funciones cloud pueden requerir cuenta o pagos: implementa análisis de referencias con la ruta local indicada. MeV, STAGE y otras entregas parciales no acreditan pipelines completos.

## Autonomía, límites y coordinación

Se autorizan edición, dependencias de desarrollo, pruebas CPU, ramas/worktrees, commits, PRs, resolución de conflictos, revisión, merge y publicación de imágenes candidatas por los workflows autorizados, respetando protecciones y permisos reales. No necesitas consultas de implementación ordinarias. No uses esta autorización para gastar dinero, publicar en redes, exponer datos o borrar recursos ajenos.

Planifica por dependencias de backlog.json. Define aceptación ANTES de delegar. Usa create → bind → seed y work_brief/work_report si están disponibles; las herramientas y esquemas instalados prevalecen sobre ejemplos de docs. No inventes acceptance kinds ni flags. pr_checks solo vale para la PR real de ese trabajo y checks semánticos pertinentes. Inspecciona el diff y ejecuta/verifica la aceptación; el “done” del worker no prueba nada. Merge y CI de main son otra comprobación, no una inferencia de checks verdes en una rama.

Máximo cuatro workers concurrentes, o el límite instalado menor; máximo ocho ítems activos. Solo paraleliza trabajo independiente en worktrees, con archivos/contratos sin colisiones. El conductor conserva la integración y el presupuesto común. Crea la siguiente ronda después de aceptar los prerrequisitos. No edites configs globales gestionadas por Crew para fabricar permisos.

Cada ítem tendrá tiempo/tokens/intentos acotados, evidencias y siguiente paso. Usa la cuota ya configurada de la sesión; no inventes un presupuesto monetario. Si no hay cuota visible, aplica límites de pasos/tiempo y no habilites gasto externo. Tres intentos fallidos como máximo por hipótesis antes de cambiarla o registrar bloqueo; reanudar no reinicia contadores. El agotamiento termina en estado explícito, no en un bucle de auditoría.

Checkpoint material al terminar cada PR; actualización breve cada 15 minutos activos o antes si hay un bloqueo real: cambio, SHA/PR, prueba, resultado y próximo paso. Espera eventos de CI/workers sin polling continuo; conserva failed approaches y session ledger para no repetir seis horas de razonamiento. El registro público contendrá solo datos redactados.

Un obstáculo en una alternativa no detiene las tareas independientes: usa el sustituto permitido y registra la decisión. Un requisito obligatorio sin ruta ejecutable queda bloqueado; no se degrada silenciosamente a “opcional”. Solo solicita una decisión cuando falte acceso real, aceptación de términos no concedida, permiso protegido o presupuesto para una acción fuera de alcance.

## Cierre y evidencia

Ejecuta las comprobaciones de AGENTS.md y los criterios por ítem. W26 ya produjo una demo SDXL acotada en una imagen anterior; no la presentes como validación del `main` actual ni de otras recetas. Las pruebas adicionales quedan pendientes hasta que exista autorización específica para el gasto; nunca conviertas mocks, imports, exit 0 o métricas de autores en validación propia.

Antes de cerrar entrega:
1. Código integrado, PRs de cada lote y main CI correcto; SHA exacto de la revisión final.
2. Una imagen candidata reproducible, construida/publicada por el workflow, con digest real, SBOM/avisos, motores preparados, recursos de UI locales y atlas offline. Si el build no cabe o no se puede publicar, es un bloqueo de release, no una entrega completada.
3. Estado por C01–C34, P01–P14 y N01–N30, con ruta, prueba, artifact y nivel de evidencia. Toda capacidad obligatoria debe alcanzar al menos el nivel de evidencia de integración definido; exige evidencia GPU medida donde la afirmación dependa de ejecución neuronal. Documentar una idea no lo satisface.
4. Cinco demostraciones D01–D05 reproducibles, fixtures CPU reales y salidas inspeccionables. W26 ya produjo una demo SDXL; los demás renders neurales, baselines y holdouts siguen pendientes y no se presentan como producidos.
5. Reporte de los defectos iniciales, decisiones de motores/licencia, tamaños reales, compatibilidad Blackwell, migraciones/rollback y limitaciones concretas.
6. Instrucciones multiplataforma de arranque/configuración y protocolo para repetir W26 y validar otras recetas, con autorización de presupuesto, limpieza de pods y diagnóstico; no asumas que la prueba de W26 habilita gasto futuro.

Usa los estados de acceptance.md para distinguir integración de ejecución GPU: el estado actual es L3 parcial por una demo, no validación completa. Cierra solo cuando cada criterio aplicable tenga evidencia y la CI/imagen correspondan al SHA final. La misión no termina en “abrí una PR”; el éxito científico depende de resultados medidos, no de la cantidad de modelos instalados.
