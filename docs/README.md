# Documentación de ComfyUIPod

Este índice orienta la documentación del repositorio y señala cuál es la fuente de verdad para cada tema. Los informes de auditoría son fotografías de una revisión concreta; para el comportamiento actual, prevalecen el código de `main`, el `README.md` raíz y la guía de producción.

## Empieza aquí

| Necesidad | Documento |
|---|---|
| Entender capacidades, perfiles de modelos, variables de entorno y uso general | [`README.md`](../README.md) |
| Desplegar y operar el servicio paso a paso, desde cualquier sistema cliente | [Guía Markdown](GUIA_PASO_A_PRODUCCION.md) · [PDF](GUIA_PASO_A_PRODUCCION.pdf) |
| Entender componentes, límites de confianza, datos y flujos de ejecución | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Consultar plantillas incluidas y su finalidad | [`plantillas.md`](plantillas.md) |
| Seguir el recorrido por carpetas y archivos principales | [`walkthrough.md`](walkthrough.md) |
| Consultar decisiones técnicas y su justificación | [`decisiones.md`](decisiones.md) |

## Por área

- **Despliegue e infraestructura:** el README raíz y la guía de producción cubren perfiles, variables de entorno, requisitos del host, persistencia, backups, operación y diagnóstico. La fuente de configuración de runtime está en `comfyctl/src/config.rs`, `studio/comfy_studio/config.py`, `scripts/` y `config/`.
- **Comfy Studio:** la arquitectura y el README describen la API, el panel, proyectos/assets, trabajos, percepción/ASR, EDL, workers y persistencia. Los contratos concretos se implementan en `studio/comfy_studio/`; los tests de `studio/tests/` definen muchos comportamientos observables.
- **API de estado y supervisor:** la arquitectura, el README y `docs/decisiones.md` acompañan a los contratos JSON de [`contratos/`](contratos/) y a la implementación Rust de `comfyctl/`.
- **Propuestas y backlog:** [`propuestas/`](propuestas/) contiene planes, opciones evaluadas e ideas. Una propuesta no implica que la función esté implementada; comprueba el estado del backlog y el código antes de tratarla como capacidad disponible.
- **Auditorías:** [`auditorias/README.md`](auditorias/README.md) cataloga revisiones históricas. Las cifras de tests, listas de hallazgos y estados incluidos allí corresponden a la fecha de cada informe y no sustituyen la CI actual.

## Guía de contribución y validación

Las instrucciones para agentes y las verificaciones obligatorias están en [`AGENTS.md`](../AGENTS.md). Para cambios del servidor Studio, ejecuta como mínimo la suite Python, Ruff, validación de plantillas y del manifiesto; para Rust, ejecuta `cargo fmt`, Clippy y los tests con `--locked`. Consulta los comandos exactos en `AGENTS.md` y asegúrate de que el workflow de GitHub Actions del PR termine verde antes de integrar.

## Compatibilidad de plataformas

La imagen de producción ejecuta ComfyUI y Comfy Studio en un contenedor Linux. El acceso web se realiza desde un navegador en Windows, macOS o Linux. La resolución de `ffmpeg`/`ffprobe` de Studio acepta rutas configuradas y búsqueda de ejecutables para Windows, macOS y Linux. El helper `tools/mac/comfy` es un script de shell para macOS/Linux; en Windows se puede usar bajo WSL2 o prescindir de él y operar desde el portal web y las herramientas de Vast.ai. No debe confundirse compatibilidad del cliente o del resolver multimedia con soporte para ejecutar nativamente en Windows la imagen Linux o el helper Bash.
