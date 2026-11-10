# Documentación de ComfyUIPod

Este índice orienta la documentación del repositorio y señala cuál es la fuente de verdad para cada tema. Para el comportamiento actual, contrasta el código de `main` y la CI con el README raíz, la política de recursos y el perfil de hardware; los planes y reportes de campañas son registros fechados, no estado vivo. `docs/ultra/status.json` es un snapshot histórico que todavía apunta a `f5c29dc`; el `main` actual es más reciente.

## Empieza aquí

| Necesidad | Documento |
|---|---|
| Entender capacidades, perfiles de modelos, variables de entorno y uso general | [`README.md`](../README.md) |
| Contribuir, orientarse en el hardware actual y ejecutar las comprobaciones | [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Desplegar y operar el servicio paso a paso, desde cualquier sistema cliente | [Guía Markdown](GUIA_PASO_A_PRODUCCION.md) · [PDF](GUIA_PASO_A_PRODUCCION.pdf) |
| Entender componentes, límites de confianza, datos y flujos de ejecución | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Arquitectura canónica en 5 fases (línea base aprobada, matriz de requisitos y contrato de variables/secretos) | [`ARCHITECTURE_CANONICAL.md`](ARCHITECTURE_CANONICAL.md) |
| Consultar plantillas incluidas y su finalidad | [`plantillas.md`](plantillas.md) |
| Estudio de anuncios (brief → guion → storyboard → variantes con QA) | [`estudio_anuncios.md`](estudio_anuncios.md) |
| Estudio Cinemático (dirección visual → prompt → render SDXL local) | [`cinematic-studio.md`](cinematic-studio.md) |
| Investigación competitiva limpia (Kling, Higgsfield, Suno, “Omni”, +8) y selección open source | [`propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md`](propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md) |
| Seguir el recorrido por carpetas y archivos principales | [`walkthrough.md`](walkthrough.md) |
| Consultar decisiones técnicas y su justificación | [`decisiones.md`](decisiones.md) |
| Saber qué se prepara, qué se descarga y qué hardware alquilar | [`POLITICA_ARQUITECTURA_RECURSOS.md`](POLITICA_ARQUITECTURA_RECURSOS.md) |
| Ver la evidencia GPU acotada y sus límites | [Resultados W26](ultra/W26_GPU_RESULTS.md) |

## Por área

- **Despliegue e infraestructura:** el README raíz y la guía de producción cubren perfiles, variables de entorno, requisitos del host, persistencia, backups, operación y diagnóstico. La fuente de configuración de runtime está en `comfyctl/src/config.rs`, `studio/comfy_studio/config.py`, `scripts/` y `config/`. El almacenamiento **se deriva** con `scripts/storage_requirement.py`; los umbrales codificados a mano deben justificarse contra ese cálculo.
- **Comfy Studio:** la arquitectura y el README describen la API, el panel, proyectos/assets, trabajos, percepción/ASR, EDL, workers y persistencia. Los contratos concretos se implementan en `studio/comfy_studio/`; los tests de `studio/tests/` definen muchos comportamientos observables.
- **API de estado y supervisor:** la arquitectura, el README y `docs/decisiones.md` acompañan a los contratos JSON de [`contratos/`](contratos/) y a la implementación Rust de `comfyctl/`.
- **Propuestas y backlog:** [`propuestas/`](propuestas/) contiene planes, opciones evaluadas e ideas. Una propuesta no implica que la función esté implementada; comprueba el estado del backlog y el código antes de tratarla como capacidad disponible.
- **Auditorías:** [`auditorias/README.md`](auditorias/README.md) cataloga revisiones históricas. Las cifras de tests, listas de hallazgos y estados incluidos allí corresponden a la fecha de cada informe y no sustituyen la CI actual.
- **Migraciones de stack:** [`upgrades/`](upgrades/) documenta cómo ejecutar las migraciones pendientes (CUDA/PyTorch, ComfyUI core). Son procedimientos, no estado: ninguna está aplicada salvo la de Node 22, que ya va en `main`.

## Guía de contribución y validación

La guía para nuevos colaboradores está en [`CONTRIBUTING.md`](../CONTRIBUTING.md); las convenciones y verificaciones obligatorias están en [`AGENTS.md`](../AGENTS.md). Ejecuta los checks pertinentes y confirma que la CI del commit actual termine verde antes de integrar. Una demo GPU de W26 ejercitó una sola receta en un digest anterior; no equivale a validar toda la plataforma.

## Compatibilidad de plataformas

La imagen de producción ejecuta ComfyUI y Comfy Studio en un contenedor Linux. El acceso web se realiza desde un navegador en Windows, macOS o Linux. La resolución de `ffmpeg`/`ffprobe` de Studio acepta rutas configuradas y búsqueda de ejecutables para Windows, macOS y Linux. El helper `tools/mac/comfy` es un script de shell para macOS/Linux; en Windows se puede usar bajo WSL2 o prescindir de él y operar desde el portal web y las herramientas de Vast.ai. No debe confundirse compatibilidad del cliente o del resolver multimedia con soporte para ejecutar nativamente en Windows la imagen Linux o el helper Bash.
