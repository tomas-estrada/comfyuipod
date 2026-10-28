# Auditoría V159 — Revisión de mejoras del trabajo M3 sin confirmar (04:45 UTC)

## Alcance
Revisión de solo-lectura de los módulos M3 nuevos sin confirmar (`recovery.py`,
`webhooks.py`, `asr/`, `edl/`, `perception/`, ~3.350 líneas) más su integración
en `app.py` y `agent/tools.py`. Validación ejecutada: `ruff check` (limpio) y
`pytest studio/tests -q` (139 pasan, 116 s).

## Hallazgos de producción (M3-SCR)

- **M3-SCR-001 (alta)** — Endpoints `async` llaman directamente pipelines
  síncronos de medios que lanzan decenas de subprocess ffmpeg:
  `app.py:1204,1235,1306,1337,1403,1435,1556,1581,1658,1699`. Un build EDL con
  N cortes puede congelar el event loop minutos enteros (health checks, SSE y
  todo request detenido). Fix: `anyio.to_thread.run_sync` / encolar como job.
- **M3-SCR-002 (alta)** — Resultados ASR/diarización/caras fabricados se
  presentan como salida real de modelo: `asr/whisperx.py:391-396,490-536`
  (utterances enlatadas con scores falsos), `asr/diarize.py:96-133`
  (`backend: "funasr_campplus"` cuando es heurística ZCR),
  `perception/face_index.py:88-142` (una cara sintética siempre). Los paquetes
  reales (`whisperx`, `funasr`, `insightface`) nunca se importan. Fix: importar
  backend real con lazy-import + sonda, o marcar `"simulated": true` y backend
  honesto.
- **M3-SCR-003 (media)** — `subprocess.TimeoutExpired` no capturado → HTTP 500:
  `edl/ffmpeg_edl.py:264,308`, `perception/keyframes.py:355-361`; los endpoints
  solo capturan `ValueError`/`RuntimeError` (`app.py:1212,1284,1709`).
- **M3-SCR-004 (media)** — Vídeo renderizado cargado entero en RAM y tope de
  tamaño inutilizado por construcción: `edl/ffmpeg_edl.py:313,325`
  (`max_upload_bytes=max(200MB, len+1024)` nunca rechaza); mismo patrón en
  `keyframes.py:437`. Fix: copia en disco con tope duro real.
- **M3-SCR-005 (media)** — Lista de cortes sin límite → cadena ilimitada de
  subprocess: `EdlBuildRequest.cuts`/`EdlCreateRequest.cuts` sin `max_length`
  (`app.py:152-177`), bucle sin cota en `edl/ffmpeg_edl.py:148-282`. Fix:
  `max_length=200` + chequeo defensivo.
- **M3-SCR-006 (media)** — `probe_media_info` fabrica metadatos plausible
  (`320x180 h264 has_video=True`) cuando ffprobe falta o falla:
  `perception/keyframes.py:44-90`; la "verificación" EDL
  (`ffmpeg_edl.py:314,356-364`) reporta dimensiones falsas como verificadas.
  Fix: `probe_ok: False` y no verificar con sonda fallida.
- **M3-SCR-007 (media)** — Keyframe sintético (PNG azul 64x36) se guarda e
  indexa como plano real cuando ffmpeg falla: `keyframes.py:355-385` no comprueba
  `returncode`; el asset queda captionado "Plano 1 de …" y entra en el índice
  CLIP (`clip_index.py:447`). Fix: comprobar returncode o marcar `"synthetic"` y
  excluir del índice.
- **M3-SCR-008 (media)** — Endpoints M3 nuevos devuelven dicts crudos y
  `detail=str(exc)` con stderr de ffmpeg (rutas de servidor) sin pasar
  `SecretRedactor`, violando la regla 3 de AGENTS.md:
  `app.py:1214-1220,1256-1285,1310-1316,1347-1370,1412-1420,1445-1454,1488-1493,1565-1569,1590-1594,1683,1723`
  vs. el patrón existente (`app.py:591,628,955,1123,1139`).
- **M3-SCR-009 (media)** — `perception/store.py:138-150`: conexión sqlite nueva
  por operación con recarga de extensión sqlite-vec; `with conn` no cierra
  (solo commitea). Upserts SELECT-luego-INSERT con carrera → `IntegrityError`
  no capturado (`store.py:181-239,356-402`); el agente crea su propio store por
  tool call (`agent/tools.py:700-703`). Fix: `ON CONFLICT DO UPDATE`,
  `contextlib.closing`, instancia compartida.
- **M3-SCR-010 (media)** — Secuencias multi-DB sin compensación:
  `face_index.py:194-236` escribe studio.db y luego index.db sin rollback;
  `keyframes.py:318-323` borra keyframes antes de extraer → fallo a mitad deja
  `clip_shots.keyframe_asset_id` colgando.
- **M3-SCR-011 (media)** — Import duro en arranque: `store.py:14`
  (`import sqlite_vec`), `enable_load_extension` (no disponible en todos los
  builds) y construcción en `create_app` (`app.py:35-41,500`). Si falla, cae
  TODA la app, no solo percepción. Fix: construcción lazy en lifespan +
  `app.state.perception_available` + 503 en endpoints degradados.
- **M3-SCR-012 (baja)** — `assert` para invariantes de producción
  (`diarize.py:294`, `store.py:245,408,525`): se elimina con `python -O` y
  produce 500 sin capturar. Fix: `RuntimeError`/`LookupError` explícitos.
- **M3-SCR-013 (baja)** — KNN de sqlite-vec decorativo: tablas vec0 sin
  `project_id`, búsqueda global O(N) recalculada en Python
  (`store.py:291-336,552-594`); `search_face_detections` ejecutado dos veces
  (`face_index.py:437-451`); N+1 `get_asset` por hit (`face_index.py:455-481`,
  `clip_index.py:544-551`).
- **M3-SCR-014 (baja)** — Duplicación con riesgo de deriva: cadena de fallback
  Blackwell escrita dos veces (`whisperx.py:82-193` vs `551-660`),
  `_l2_normalize` duplicado (`clip_index.py:219-226`, `face_index.py:26-33`),
  pipeline tempdir→WAV→ventanas duplicado (`whisperx.py:448-451`,
  `diarize.py:107-110`), ensamblado de doc EDL triplicado (`app.py:1622-1633`,
  `tools.py:786-802`, `ffmpeg_edl.py:106-121`).
- **M3-SCR-015 (baja)** — `webhooks.py:151-161`: reintenta también en HTTP 4xx
  (400/401/413 no se arreglan reintentando); desperdicia hasta 3 envíos por
  evento mal configurado. Fix: no reintentar en 4xx excepto 408/429.
- **M3-SCR-016 (baja)** — `recovery.py:144-151`: la reanudación espera
  `execute_prompt_job` secuencialmente por job; con varios jobs huérfanos la
  recuperación total es la suma de todas las esperas. Fix: `asyncio.gather`
  acotado o reencolar y dejar que el worker normal ejecute.
- **M3-SCR-017 (baja)** — Timestamps negativos en fallbacks degenerados
  (`whisperx.py:509-517`, `diarize.py:119`): `min(duration-0.2, …)` negativo si
  `duration < 0.2`. Fix: `max(0.0, …)`.
- **M3-SCR-018 (baja)** — `_resolve_source_video_path` hace escaneo completo de
  assets por cada corte (`ffmpeg_edl.py:155-165`). Fix: memoizar por build.
- **M3-SCR-019 (baja)** — `highlights.py:55-67`: `removed_pauses` cuenta pausas
  de segmentos luego descartados por `speaker_filter`; el informe editorial
  miente. Fix: contar solo sobre segmentos que sobreviven.
- **M3-SCR-020 (baja)** — Código muerto/solo para tests:
  `run_with_blackwell_fallback` y `WhisperXTranscriber` (`whisperx.py:663-764`),
  `CamPlusDiarizer` (`diarize.py:303-335`), alias `FaceIndex` y wrapper
  `combined_search` (`face_index.py:530-558`). Fix: eliminar o cablear.
- **M3-SCR-021 (baja)** — `asr/whisperx.py` colisiona por nombre con el paquete
  pip `whisperx` (confusión de auto-import; hoy no rompe por imports absolutos).
  Fix: renombrar a `transcribe.py`.

## Hallazgos de gobernanza (M3-AGT)

- **M3-AGT-001 (alta)** — El tracker de auditorías registra ~149 notas
  (V10→V158) creadas una por minuto entre 02:13 y 04:41 UTC del 28/10, en
  violación directa de la cadencia de AGENTS.md ("máx. una auditoría por día o
  por PR, sin ciclos automáticos minuto a minuto"). La mayoría son vacías
  ("Estado git sin cambios… Resumen publicado en el tablero (ALL)"), que la
  convención declara inválidas. Fix: detener el ciclo automático externo,
  purgar/consolidar las notas vacías y exigir hallazgos con ID único por nota.

## Verificado como correcto (no señalar)

Sin `shell=True`; binaries vía `shutil.which`; rutas validadas con
`is_relative_to` (sin path traversal); timeouts en todo subprocess; SQL
parametrizado; modelos pydantic acotados (`max_frames≤200`, `limit≤100`,
`extra="forbid"`); herramientas del agente con lazy imports y
`ValueError→ToolExecutionError`; WAL + busy timeout en la DB vectorial;
embeddings excluidos de resultados de búsqueda; recovery/webhooks aislados del
arranque; `.gitignore` cubre `__pycache__/` a cualquier profundidad (solo los
11 `.py` nuevos se añadirían).

## Estado de validación
- `ruff check studio panel scripts config/probes` → limpio.
- `pytest studio/tests -q` → 139 pasan (116 s).
- Sin cambios de código en esta auditoría (solo-lectura).
