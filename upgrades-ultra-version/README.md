# ComfyUIPod Ultra — inicio para el agente

Esta carpeta es la extensión persistente del goal. Contiene investigación, decisiones, contratos, prioridades y aceptación para desarrollar Ultra en este repositorio. No es por sí sola una implementación terminada ni un catálogo de modelos para descargar. Para recibir código y onboarding, empieza por el [README raíz](../README.md) y [CONTRIBUTING.md](../CONTRIBUTING.md).

## Goal corto

Usar el texto de [GOAL.md](GOAL.md). Tiene aproximadamente 1.200 caracteres. [MISSION.md](MISSION.md) conserva el mandato extendido para consulta; no hace falta pegarlo entero al agente.

## Primera lectura y siguiente acción

1. Leer el AGENTS.md vigente, este README y [MISSION.md](MISSION.md).
2. Revisar [requirements.md](requirements.md), [tasks.md](tasks.md), el mapa de [backlog.json](backlog.json) y los P0 en [research.md](research.md).
3. Antes de proponer trabajo, comparar el backlog con `main` y sus pruebas; W00–W25 ya tienen entregas integradas y no se deben reabrir sin evidencia de regresión.
4. Para cada lote, consultar únicamente sus C/P/engines y los checks A indicados; el conductor conserva las reglas globales. No releer toda la biblioteca en cada worker.
5. El reporte W26 registra una demo GPU SDXL acotada. D01–D05 completas, baselines/holdouts y una repetición sobre una imagen del `main` actual aún están pendientes.

## Mapa para leer por necesidad

| Archivo | Para qué sirve |
| --- | --- |
| [GOAL.md](GOAL.md) | Mandato breve para el agente |
| [MISSION.md](MISSION.md) | Autorización/scope, principios y cierre |
| [requirements.md](requirements.md) | Qué debe hacer el estudio y qué no cuenta como terminado |
| [design.md](design.md) | Contratos, DAG, scheduler, motores, atlas, UI, datos y memoria |
| [research.md](research.md) | Evidencia actual del repo, defectos y decisiones técnicas |
| [capabilities.json](capabilities.json) | C01–C34: función, ruta, prueba y relación con trabajo/programas |
| [programs.md](programs.md) | P01–P14: hipótesis, input/output, experimentos y límites |
| [engines.json](engines.json) | Selección de rutas, alternativas N01–N30, licencias y compatibilidad |
| [backlog.json](backlog.json) | W00–W26: dependencias, entregables, aceptación y budget por ítem |
| [tasks.md](tasks.md) | Cómo avanzar, coordinar, reanudar y evitar iteraciones sin cierre |
| [acceptance.md](acceptance.md) | A00–A29 y cinco demostraciones D01–D05 |
| [runner-guide.md](runner-guide.md) | Adaptación a CrewAI o Kiro Crew sin confundirlos con el runtime del estudio |
| [sources.md](sources.md) / [sources.json](sources.json) | Fuentes primarias, revisiones y alcance real de lo verificado |
| [snapshot.json](snapshot.json) | SHA/PR/CI consultados; volver a verificar antes de editar |
| [contexto/Marco_Anterior.md](contexto/Marco_Anterior.md) | Marco previo completo como referencia histórica |

La misión contiene 34 objetivos funcionales, 14 programas y 30 opciones adicionales. No son 64 motores distintos obligatorios. engines.json incluye además plataformas/servicios propios y alternativas; sus números no se convierten en downloads al boot.

Para que una ruta marcada `build_selected` cuente como preparada para A07,
`scripts/check_engines_manifest.py` reporta como bloqueos los SHA de código
inmutables, hashes SHA-256 de locks, decisión explícita de redistribución,
revisión de notices, estado de implementación del adapter y rutas versionadas
de evidencia para su entrypoint y prueba. El checker verifica que esas rutas
existan dentro del repositorio y estén versionadas; la revisión humana confirma
que demuestran el comportamiento reclamado. Ese informe evalúa declaraciones
del manifiesto; inventario de la imagen, boot offline y probes de runtime
requieren evidencia separada de CI. Un manifiesto coherente no implica que el
release esté listo.

## Reglas de lectura y ejecución

- GOAL/MISSION y anexos actuales prevalecen sobre premisas antiguas del marco histórico.
- Un usuario, una imagen/pod Linux, perfil RTX PRO 6000 Blackwell 96 GB y perfiles host RAM128/256 separados; la compatibilidad de una GPU no equivale a validación de todas las recetas.
- Motores preparados en build; pesos/LoRA/VAE/encoders bajo demanda. Atlas base físicamente dentro de la misma imagen GHCR.
- Disco derivado de componentes/uso reales; 180/200GB no es cap ni mínimo universal.
- Una autoridad de producción y lease global de GPU; extender la API/orquestación existente.
- Proxies para trabajar, entregables finales4K y proyectos editables.
- Proveedores LLM/VLM configurables, secretos runtime y medios personales fuera de Git/GHCR/CI.
- Investigación gratuita y permisos por componente; no servicios comerciales obligatorios.
- Cada función conecta planner→API/tool→engine→artifact→QA/UI. Un wrapper o un mock no la completa.
- PRs, merge y publicación de candidatas autorizados dentro de permisos/protecciones existentes.
- W26 ya ejecutó una demo aprobada de USD 0.59 en el digest `sha-f5c29dc`. Esa autorización no se extiende a nuevas campañas: antes de cualquier otro gasto o API facturada, se necesita autorización específica vigente.
- El resultado es **GPU L3 parcial**, no “Ultra validada”: una receta SDXL fue medida; las demás recetas, D01–D05, baselines y holdouts siguen pendientes.

## Seguimiento y reanudación

Crear un registro material y redactado en docs/ultra/ al implementar, con W/C/P/N, PR/SHA, pruebas, artifacts, nivel L0–L4, intentos, bloqueos y siguiente lote. El estado del goal debe poder retomarse desde repo/CI/imagen y los checkpoints reales del runner.

No crear auditorías periódicas, mensajes en redes o una búsqueda interminable de novedades. Esta carpeta define un trabajo extenso pero finito y demostrable. Una opción técnica inviable se sustituye por una ruta permitida equivalente; una capacidad obligatoria sin solución queda bloqueada y visible.

## Comprobar consistencia del paquete

Desde la raíz del repo:

```sh
python3 upgrades-ultra-version/validate_spec.py
```

En Windows, si Python se invoca mediante el launcher, usar `py upgrades-ultra-version/validate_spec.py`. Este validador comprueba IDs/referencias/DAG y contenido del plan; no certifica implementación ni resultados GPU.

Paquete de investigación preparado el 2026-11-05 UTC. La demo W26 es un evento posterior y separado; el registro y el snapshot de esta carpeta no sustituyen la evidencia del [reporte W26](../docs/ultra/W26_GPU_RESULTS.md). `docs/ultra/status.json` todavía apunta a `f5c29dc` y no refleja el `main` actual (`5850191`).
