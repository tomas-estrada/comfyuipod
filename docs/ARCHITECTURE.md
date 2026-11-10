# Arquitectura actual

Esta página resume la implementación que acompaña a `main`. Para valores exactos y opciones operativas, consulta el [`README.md`](../README.md) y la [guía de producción](GUIA_PASO_A_PRODUCCION.md); el código indicado es la fuente de verdad. El [índice de documentación](README.md) explica el alcance de los demás documentos.

## 1. Alcance y forma de despliegue

ComfyUIPod se distribuye como una imagen Docker para un host Linux con GPU NVIDIA. El usuario controla el sistema desde un navegador en Windows, macOS o Linux; el servicio y ComfyUI corren dentro del contenedor Linux. No es una aplicación de escritorio ni una promesa de instalación nativa Windows. Studio sí resuelve `ffmpeg`/`ffprobe` mediante rutas configuradas o búsqueda de ejecutables compatible con Windows, macOS y Linux; eso no cambia el sistema operativo de la imagen de producción.

El supervisor nativo Rust `comfyctl` es el proceso de control. Nginx es la única entrada pública prevista (puerto 8188, con Basic Auth). ComfyUI, el servidor Studio y el servidor de estado se enlazan a loopback. Los procesos auxiliares se lanzan y supervisan dentro del contenedor; sus puertos internos no deben publicarse directamente.

## 2. Componentes y fuentes de verdad

| Componente | Responsabilidad principal | Implementación |
|---|---|---|
| Supervisor y diagnósticos | Validaciones previas, fases de arranque, estado, triaje, logs y ciclo de vida de ComfyUI | `comfyctl/`, `scripts/start.sh`, `scripts/download-models.sh` |
| Frontal y autenticación | Entrada web, proxy de rutas, WebSockets/SSE, límites y cabeceras de seguridad | `config/nginx.conf` |
| Studio API | Configuración, ciclo de vida de app, auth de borde, observabilidad, conexión con ComfyUI | `studio/comfy_studio/app.py`, `server.py`, `routers/`, `webutil.py` |
| Studio UI | Panel de proyectos, chat del agente, jobs, plantillas, galería, timeline EDL, anuncios y eventos en vivo | `panel/` |
| Motores y orquestación | Contrato uniforme de 21 motores (`list`/`install`/`verify`/`run`), puerta de instalación y grafo de producción con checkpoints | `studio/comfy_studio/engines/`, `orchestration/`, `ads/`, `montage/`, `creative/` |
| Catálogo de plantillas | Validación de parámetros y creación/encolado de workflows | `studio/comfy_studio/templates/` |
| Percepción y media | Keyframes, metadata/probe multimedia, búsqueda visual/facial, ASR/diarización y EDL | `studio/comfy_studio/perception/`, `asr/`, `edl/`, `media_bins.py` |
| Herramientas ML | Descubrimiento de capacidades y llamadas aisladas a backends reales; cada operación ML se declara simulada cuando corresponde | `studio/comfy_studio/ml/` |
| Agente | Herramientas, memoria, planificación, presupuestos, roles y llamadas LLM | `studio/comfy_studio/agent/` |
| Persistencia | Base Studio y archivos de proyecto; índice vectorial separado | `studio/comfy_studio/projects.py`, `perception/store.py`, `STUDIO_DATA_DIR` |
| Supervisión/estado web | Estado y autodiagnóstico para operadores | `comfyctl/src/`, rutas `/status`, `/status/api`, `/report.md`, `/diag` |

Los grupos anteriores indican límites de responsabilidad, no procesos independientes en todos los casos. La configuración completa y defaults vigentes están en el README y en `comfyctl/src/config.rs` / `studio/comfy_studio/config.py`.

## 3. Rutas y flujo de una operación Studio

1. El navegador solicita `/studio/` o `/studio/api/...` a Nginx. La autenticación Basic Auth se aplica en el borde; Nginx proxy-pasa a `127.0.0.1:18190` y habilita el transporte necesario para WebSocket y progreso SSE.
2. `comfy_studio.server` crea la aplicación FastAPI. Los routers organizan endpoints por área; `app.py` conserva el ensamblado, lifespan, estado y middlewares. `STUDIO_SERVER_WORKERS` debe ser `1`: el estado de progreso/broker es local al proceso y varios workers no son compatibles.
3. Una petición que ejecuta una plantilla valida parámetros, persiste el job y responde `202 Accepted` con `job_id` por defecto. La tarea sigue en segundo plano; el cliente consulta `/studio/api/jobs/{job_id}` o escucha `/studio/api/progress`. `?wait=1` conserva el modo síncrono para integraciones que lo necesiten.
4. La ejecución envía el workflow a ComfyUI en loopback. El progreso y los cambios de estado llegan al panel por SSE; resultados, assets y metadatos se registran en el store del proyecto.
5. Ante reinicio, la capa de recuperación reconcilia estados de jobs según su política y configuración. Webhooks salientes opcionales notifican eventos y, si hay secreto, firman el cuerpo con HMAC-SHA256.

No se debe tratar el `202` como resultado final: es una confirmación de aceptación/encolado. El identificador devuelto es la clave para seguir el trabajo.

La orquestación de producción informa `phase=simulated` si usa uno o más colaboradores stub; ese estado no publica `final_output_path` ni assets en la galería. `phase=done` se reserva para una ejecución completamente cableada cuyo archivo final no vacío fue verificado por `ffprobe`. Los checkpoints antiguos `done` sin prueba de artefacto se migran a `simulated` antes de mostrarse.

## 4. Datos, persistencia y backups

`STUDIO_DATA_DIR` contiene la base de metadatos `studio.db`, el almacén de proyectos y assets bajo `projects/`, y el índice vectorial `index.db` cuando se inicializa. El diseño evita guardar los pesos de modelos en el snapshot de Studio: los modelos pertenecen a las rutas de datos de ComfyUI y se descargan según `MODEL_PROFILE`.

La CLI `python -m comfy_studio.backup create <archivo.zip>` genera un snapshot consistente mediante la API online de SQLite y agrega manifiesto con tamaños y SHA-256; `verify` comprueba integridad. El backup incluye las bases pertinentes y archivos de proyecto; no incluye pesos, configuración ni secretos. Para restaurar: detener Studio, preservar copia de destino, verificar el archivo, extraer sus datos al `STUDIO_DATA_DIR` correcto y reiniciar. Ver detalles y comandos en [Copias portátiles de Studio](../README.md#copias-portátiles-de-studio).

## 5. Estado real, simulación y cachés de medios

`STUDIO_PERCEPTION_BACKEND=auto` hace un sondeo de capacidades al arrancar y elige backend real cuando pasa; si no, cae a la simulación determinista, que se declara en los resultados con `simulated: true` y aviso. `real` exige backend real y falla de forma explícita si no está disponible; `sim` no intenta cargar los modelos. Algunas rutas del índice CLIP continúan siendo deterministas aunque otras operaciones usen modelos reales. Comprueba siempre el estado de la respuesta antes de interpretar calidad ML.

Las operaciones multimedia comparten el resolver de `ffmpeg`/`ffprobe`: `STUDIO_FFMPEG_BIN` y `STUDIO_FFPROBE_BIN` permiten indicar ejecutables; en ausencia de overrides se usan los defaults de resolución documentados en README. `/studio/api/health` informa sobre disponibilidad de medios para ayudar a diagnosticar configuración.

La indexación de video reutiliza keyframes si la versión del archivo fuente, los parámetros de extracción, el ejecutable resuelto y los archivos derivados siguen coincidiendo. El índice CLIP reutiliza una embedding si coinciden bytes del keyframe e inputs semánticos/backend/modelo. Cambiar la fuente, parámetros, archivos derivados, caption/tags u otros inputs pertinentes invalida el resultado y fuerza trabajo nuevo; no borres ni edites manualmente los metadatos de cache. Es una optimización de reindexado, no una caché global que evite la primera extracción.

## 6. Observabilidad y diagnóstico

El middleware de Studio asigna o valida un `X-Request-Id`, lo refleja en la respuesta y lo propaga al logging. Con `STUDIO_JSON_LOGS=1`, cada línea estructurada incluye `request_id`; usa el valor de la respuesta para correlacionar el fallo en `studio-server` logs. El handler de logs conserva la redacción de secretos.

`GET /studio/api/health` ofrece estado del servidor y capacidades relevantes. `/status` y `/status/api` presentan estado de host/supervisor; `/report.md` y `/diag` son ayudas para soporte, no un sustituto de revisar/ocultar secretos antes de compartirlos. El workflow de GitHub Actions valida los componentes del proyecto; su resultado actual debe verificarse en el PR, no extrapolarse desde un informe de auditoría antiguo.

## 7. Límites de seguridad

- Solo Nginx publica la superficie web prevista; servidores internos permanecen en loopback.
- No pasar URLs controladas por usuario a fetchers internos; las integraciones externas deben conservar la validación anti-SSRF.
- Las salidas públicas deben pasar por redacción de secretos; no copiar API keys, contraseñas ni secretos de webhook a documentación o logs.
- Mantener un solo worker Studio; SSE y estado en memoria no tienen coordinación multi-proceso.
- Los backups pueden contener contenido privado de proyectos. Guardarlos fuera del directorio de datos, verificar hashes y proteger el destino según la política de datos del operador.

## 8. Pruebas y cambios

Las convenciones y comandos obligatorios están en [`AGENTS.md`](../AGENTS.md). Las pruebas principales se organizan en `studio/tests/` y `comfyctl/tests/`; los nombres específicos de API, media, backup y recuperación se encuentran en el README y el árbol de tests. Para cambios de documentación, validar enlaces locales, generar de nuevo la versión PDF si se modifica la guía publicada, y confirmar que los checks del PR estén verdes antes de integrar.
