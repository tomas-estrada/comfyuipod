# Adaptar el goal al agente disponible

CrewAI y Kiro Crew son productos distintos. El goal y los anexos del estudio son portables; el mecanismo de ejecución debe corresponder a lo que esté instalado. Ninguno pasa a ser por eso una dependencia del pod.

## Si el agente es CrewAI

Asignar GOAL.md como objetivo y darle herramientas reales para leer/editar el checkout, ejecutar checks y operar GitHub/GHCR según permisos existentes. El texto del goal no habilita herramientas, autenticación ni acceso de red que falten.

La planificación sigue backlog.json. Las tareas devuelven cambios/PRs/artifacts y evidencia verificable; un resumen bonito no satisface su expected output. Verificar en la versión instalada los mecanismos de tareas, ejecución jerárquica/secuencial, persistencia/checkpoints y límites. No asumir que una API de un tutorial antiguo sigue vigente ni generar agents.yaml/JSONC global sin comprobar la instalación.

Leer archivos por ruta es suficiente. Si se usa knowledge/RAG, elegir almacenamiento/embeddings locales o ya autorizados; no habilitar por defecto un servicio de embeddings facturado para indexar esta carpeta. Los índices son ayuda de recuperación, no sustituto del texto normativo de aceptación.

Persistir IDs de trabajo, estado de requisitos, presupuesto, intentos y SHA. Un framework con memoria no sabe automáticamente qué PR fue integrada o qué imagen contiene un cambio: hay que comprobarlo. Sin delegación disponible, realizar trabajo secuencial; no necesita un equipo para completar el objetivo.

**No invocar work_ledger_record, work_brief o kirocrew run como si fueran APIs de CrewAI.** Son de otra herramienta.

Fuentes oficiales consultadas: [agentes](https://docs.crewai.com/v1.15.35/en/concepts/agents) y [flows](https://docs.crewai.com/v1.15.35/en/concepts/flows). La versión instalada puede ser distinta.

## Si el agente es Kiro Crew

Preferir un chat del proyecto sobre kirocrew-conductor, si está disponible. La UI Crew Mode corresponde a un conductor y algunas versiones la ocultan bajo Settings → Developer → Feature Previews → Ultra Mode. No es el nombre del release de ComfyUIPod.

Usar work ledger real, create→bind→seed, aceptación antes del dispatch y reporte de workers. La edición y esquemas instalados gobiernan llamadas/kinds. tasks.md detalla coordinación y revisión.

La documentación también admite kirocrew run TASK.md. Es una ruta de Task Runner, cuyo agente se selecciona por sesión/run y cuyos permisos headless deben existir antes. El prompt no puede saltarlos. Si se elige esa ruta, usar verificación de pruebas real y require_verification=true de la edición correspondiente; no --no-test.

No modificar configs globales autogeneradas por Crew para fabricar permisos. Mantener session ledger y esperar eventos de CI/workers, sin polling continuo.

## Comportamiento común

- Leer README/MISSION y avanzar por paquetes, consultando anexos pertinentes.
- Verificar fuentes/versiones al integrar; la investigación ya indica rutas y límites, no impone una rama moving latest.
- Mantener máximo cuatro workers concurrentes o límite menor instalado; no paralelizar archivos/contratos compartidos.
- Fallos e intentos persisten tras resume. No reabrir infinito con distinto nombre.
- Si falta un permiso real, registrar el bloqueo específico y continuar tareas independientes.
- Toda petición de budget GPU/APIs de prueba queda para la fase posterior; no gastar para satisfacer un check pre-GPU.
- El backend del estudio sigue la API/orquestación existente/LangGraph. No sustituirlo por el framework usado para desarrollar el repo.

