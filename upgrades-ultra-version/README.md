# ComfyUIPod Ultra — inicio para el agente

Esta carpeta es la extensión persistente del goal. Contiene investigación, decisiones, razonamiento, contratos, prioridades y aceptación para implementar Ultra en este mismo repositorio. No es una implementación ya terminada ni un catálogo de modelos para descargar.

## Goal corto

Usar el texto de [GOAL.md](GOAL.md). Tiene aproximadamente 1.200 caracteres. [MISSION.md](MISSION.md) conserva el mandato extendido para consulta; no hace falta pegarlo entero al agente.

## Primera lectura y siguiente acción

1. Leer el AGENTS.md vigente, este README y [MISSION.md](MISSION.md).
2. Revisar [requirements.md](requirements.md), [tasks.md](tasks.md), el mapa de [backlog.json](backlog.json) y los P0 en [research.md](research.md).
3. Comenzar W00 y los arreglos de integridad/recursos. Definir aceptación antes de editar/delegar.
4. Para cada lote, consultar únicamente sus C/P/engines y los checks A indicados; el conductor conserva las reglas globales. No releer toda la biblioteca en cada worker.
5. Continuar por dependencias hasta W25. W26 está preparado como etapa GPU futura y no se ejecuta todavía.

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

## Reglas de lectura y ejecución

- GOAL/MISSION y anexos actuales prevalecen sobre premisas antiguas del marco histórico.
- Un usuario, una imagen/pod Linux, RTX PRO 6000 Blackwell96GB y perfiles host RAM128/256 separados.
- Motores preparados en build; pesos/LoRA/VAE/encoders bajo demanda. Atlas base físicamente dentro de la misma imagen GHCR.
- Disco derivado de componentes/uso reales; 180/200GB no es cap ni mínimo universal.
- Una autoridad de producción y lease global de GPU; extender la API/orquestación existente.
- Proxies para trabajar, entregables finales4K y proyectos editables.
- Proveedores LLM/VLM configurables, secretos runtime y medios personales fuera de Git/GHCR/CI.
- Investigación gratuita y permisos por componente; no servicios comerciales obligatorios.
- Cada función conecta planner→API/tool→engine→artifact→QA/UI. Un wrapper o un mock no la completa.
- PRs, merge y publicación de candidatas autorizados dentro de permisos/protecciones existentes.
- No alquilar GPU ni usar APIs facturadas para probar el estudio en esta fase. El runner utiliza únicamente la cuota/herramientas de desarrollo ya autorizadas.
- La entrega actual se llama Ultra pre-GPU. “Ultra validada” requiere ejecución/medidas posteriores, no una CI con stubs.

## Seguimiento y reanudación

Crear un registro material y redactado en docs/ultra/ al implementar, con W/C/P/N, PR/SHA, pruebas, artifacts, nivel L0–L4, intentos, bloqueos y siguiente lote. El estado del goal debe poder retomarse desde repo/CI/imagen y los checkpoints reales del runner.

No crear auditorías periódicas, mensajes en redes o una búsqueda interminable de novedades. Esta carpeta define un trabajo extenso pero finito y demostrable. Una opción técnica inviable se sustituye por una ruta permitida equivalente; una capacidad obligatoria sin solución queda bloqueada y visible.

## Comprobar consistencia del paquete

Desde la raíz del repo:

```sh
python3 upgrades-ultra-version/validate_spec.py
```

En Windows, si Python se invoca mediante el launcher, usar `py upgrades-ultra-version/validate_spec.py`. Este validador comprueba IDs/referencias/DAG y contenido del plan; no certifica implementación ni resultados GPU.

Investigación preparada el 2026-11-05 UTC. No se han ejecutado modelos GPU ni descargado sus pesos para escribir estos documentos.

