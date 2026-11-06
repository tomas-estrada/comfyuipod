# Ejecución del backlog

El goal es portable entre agentes. Leer runner-guide.md: los detalles de work ledger siguientes solo se aplican a Kiro Crew; CrewAI debe usar los mecanismos reales de su versión. No cambiar por ello el backend de producción.

## Antes del primer dispatch

1. Leer GOAL y todos los anexos, después contrastar con reglas/estado actuales del repo.
2. Verificar instalación real de Kiro Crew y nombres de agente/herramientas. Preferir el chat `kirocrew-conductor` con work ledger.
3. Comprobar repo, GitHub/GHCR y permisos ya concedidos mediante acciones de lectura. El prompt no concede permisos de herramienta ausentes.
4. Registrar el objetivo y el punto de partida. No crear tareas automáticas recurrentes ni mensajes en Slack/email para esta misión.
5. Conservar presupuesto configurado de la sesión y estimar el siguiente lote. Si falta un dato de gasto externo, esa acción queda fuera de alcance y el resto continúa.
6. Obtener evidencia de W00 y crear pruebas de los P0 antes de ampliar el stack.

El goal no necesita convertirse en un único contexto gigante. El conductor mantiene el mapa; cada worker recibe las secciones pertinentes, invariantes, prerequisitos ya aceptados y criterios exactos.

## Cómo interpretar backlog.json

Hay 27 paquetes W00–W26. W00–W25 registran el desarrollo integrado; W26 ya tiene una demo GPU SDXL limitada, documentada en `docs/ultra/W26_GPU_RESULTS.md`. D01–D05 completas, holdouts y una repetición en el `main` actual siguen pendientes. Las dependencias son del plan del proyecto, no órdenes de despachar todos los ítems a la vez.

Un paquete grande puede necesitar varias PRs secuenciales. Antes de despacharlo, dividirlo en trabajo independiente/afirmable de 30–90 minutos activos. Si dos pasos dependen de la salida del anterior, mantenerlos en el mismo ítem o en rondas sucesivas. Nunca usar ocho workers sobre el mismo contrato central.

Orden de rondas orientativo, subordinado al DAG real:
- Baseline → integridad → recursos.
- Scheduler y contratos; después fábrica de engines/build representativo.
- Ingesta/evaluación, generación base, atlas y activos geométricos según dependencias.
- Referencias, cámara, física, movimiento, audio, capas y luz.
- Cara, memoria/4D y acabado; después UI y director integrado.
- Investigador y demostraciones finales; release candidato con estado de evidencia explícito. W26 ya ejecutó una demo SDXL; faltan D01–D05, holdouts y repetición en una imagen del `main` actual.

W24 se construye temprano para que los equipos no inventen sus propios criterios después de implementar. El conductor integra cambios de Docker/locks/shared registries de forma serial aunque las recetas por engine se desarrollen en paralelo.

## Work ledger real

Usar las herramientas instaladas, inspeccionando su esquema:
- Conductor: work_ledger_record / work_ledger_read.
- Worker: work_brief / work_report.
- Dispatch: create con aceptación → bind de sesión → seed del brief.
- Un worker puede informar progreso/done/blocked; no aceptar su propio trabajo.
- Conductor inspecciona diff, artifacts y checks semánticos de la PR real antes de verificar.

Kinds documentados en la revisión: pr_checks, pr_merged, edition_check, file, human_approval. No inventar un kind “command” ni una combinación JSON no admitida. Poner las pruebas pertinentes en los checks de la PR o usar un checker instalado documentado. Un archivo de status, por sí solo, no demuestra que sus afirmaciones sean ciertas.

Para PR nueva con número aún desconocido, usar el mecanismo admitido por la edición; el conductor asocia el número real informado tras verificar repo/branch/commits. Una PR verde ajena no puede satisfacer el ítem. Draft/checks desconocidos se resuelven antes de evaluar.

Pr_checks prueba una condición de la PR, no que ya esté en main. Registrar merge y comprobar CI del SHA posterior como paso de integración/release explícito. Si una protección requiere aprobación de otra persona y no existe, conservar ese bloqueo real y continuar trabajo independiente sin eludirla.

Si no existe work ledger en la edición instalada, no fabricar llamadas. Se puede ejecutar trabajo secuencial con task runner/checkpoints reales y el registro del repo, conservando aceptación antes del trabajo y revisión independiente disponible. No afirmar que se utilizó una característica que no existe.

## Presupuesto, intentos y cierre de bloqueos

- Máximo cuatro leaf workers o el límite menor instalado. Task Runner tiene sus propios límites; no extrapolar los del conductor.
- Máximo ocho ítems activos. Cerrar/reconciliar antes de añadir otros.
- Máximo tres fallos de aceptación por hipótesis. Conservar tries/fails, tiempo activo y consumo visible en resume.
- Cambiar de hipótesis requiere registrar qué falló y qué cambio concreto resolverá la causa; no renombrar un ítem para resetear límites.
- No abrir trabajo fuera de este backlog mientras haya requisitos obligatorios sin resolver. Ideas nuevas van a una cola futura con motivo, sin secuestrar la misión.
- Si agota cuota o termina en bloqueo real, dejar checkpoint y estado terminal honesto; no seguir auditando el repo cada minuto.
- Un criterio fallido obligatorio no desaparece por cerrar el ítem como rejected/abandoned. El resultado global sigue bloqueado hasta resolverlo.

No se impone un límite global de 90 minutos a toda Ultra. Es una misión extensa, con entregas pequeñas verificables, no una promesa de tener todo listo en una tarde.

## Registro persistente en el repo

Crear/actualizar un registro reducido, por ejemplo docs/ultra/status.json y docs/ultra/PROGRESS.md, mediante commits materiales:
- goal revision y SHA de repo;
- W/C/P/N y estado;
- PR/commit y aceptación;
- artifacts de CI/build/digest;
- nivel L0–L4, GPU pendiente y motivo;
- bloqueos/rechazos con fuente;
- siguiente lote;
- contadores de intentos y failed approaches redactados.

No copiar transcripts completos, secrets, URLs firmadas ni rutas/metadata de archivos personales. El work/session ledger de Crew conserva contexto operativo; el repo conserva lo necesario para que otra sesión reproduzca y retome el trabajo. El resultado no depende de un sandbox efímero.

Actualizar tras cada PR o cambio material. Informar al usuario cada 15 minutos activos o al producir un bloqueo significativo. Esperar eventos/CI sin loops de polling de pocos segundos. Las convenciones de auditoría del repo limitan auditorías, no impiden progreso de implementación.

## Contrato breve para cada worker

El conductor prepara un brief con:
- ID y problema concreto;
- prerequisitos aceptados y contratos/archivos de referencia;
- permisos dentro del scope y rutas de su worktree;
- entregables/ruta API/planner/UI que debe conectar;
- pruebas de acceptance.md y nuevas regresiones necesarias;
- límites de tiempo/intentos/recursos;
- qué evidencia entregar y qué no puede declarar validado.

El worker implementa, prueba y entrega PR revisable. Conductor integra, verifica y actualiza estado. No asignar al worker “descubre todo lo que no entiendes”: la selección y las hipótesis están en este paquete; la verificación actual de versiones/permisos es ingeniería necesaria.

## Release y etapa GPU

No detenerse en PR abierta. Verifica el `main`, CI, imagen/digest y smoke actuales; no uses el cierre W25 ni `status.json` como estado vivo. W26 ejecutó una sola receta en un digest anterior y dejó una validación L3 parcial.

No repitas alquiler ni ejecutes APIs facturadas sin autorización específica vigente. Antes de declarar cerrados los hallazgos W26, repite el arranque y la adquisición de modelos en una imagen actual; después completa D01–D05, mediciones por perfil, baselines y holdouts según el presupuesto que se autorice.
