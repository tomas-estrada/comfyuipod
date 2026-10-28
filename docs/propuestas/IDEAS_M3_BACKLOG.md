# Backlog de Ideas — Hito M3 "Perfect Pod"

**Fecha:** 2026-10-28 · **Autor:** sesión de búsqueda de ideas (goal: "search for ideas. Document everything.")
**Estado del repo al momento de redacción:** 28 archivos modificados sin commitear (+3.350/−181), 12 rutas nuevas sin seguimiento (módulos `asr/`, `edl/`, `perception/`, `recovery.py`, `webhooks.py`, tests M3, `AGENTS.md`, `AUDITORIA_AGENTE_V9.md`). Rama de trabajo documentada en `docs/decisiones.md` como `m3-percepcion-edl`.

Este documento consolida ideas de mejora, hallazgos y riesgos detectados al inspeccionar el batch M3 en curso. Convención de IDs: `M3-IDEA-nnn` (mejoras de producto/ingeniería), `M3-SEC-nnn` (seguridad), `M3-OPS-nnn` (operaciones/gobernanza), alineada con `AGENTS.md`.

---

## 1. Hallazgo transversal: ML simulado presentado como real

Los cuatro subsistemas de percepción/transcripción del batch M3 son **simulaciones deterministas**, no integran los modelos que la documentación declara:

| Módulo | Documentación declara | Implementación real | Evidencia |
|---|---|---|---|
| `studio/comfy_studio/asr/whisperx.py` | whisperx + faster-whisper con cadena de fallback sm_120 (5 pasos) | Transcripciones sintetizadas desde ventanas acústicas RMS/ZCR + frases enlatadas `_DEFAULT_UTTERANCES_ES`, scores fabricados 0.94/0.78, timestamps equiespaciados. whisperx/faster-whisper nunca se invoca ni se carga | `whisperx.py:355-396`, `:382`, `FASTER_WHISPER_MODEL_REL :27-29` (constantes muertas) |
| `studio/comfy_studio/asr/diarize.py` | FunASR cam++ | Clustering 1-D de ZCR/cubetas de frecuencia; `campplus_cn_common.bin` referenciado pero nunca cargado | `diarize.py:23`, `:26-93` |
| `studio/comfy_studio/perception/clip_index.py` | CLIP ViT-L/14 | Hashing bag-of-words a 64 dims con ejes conceptuales; imágenes mapeadas por estadísticas RGB | `clip_index.py:20-195`, `:209-305` |
| `studio/comfy_studio/perception/face_index.py` | InsightFace buffalo_l | `detect_faces_in_image` devuelve siempre 1 cara sintética (bbox `[0.22,0.14,0.78,0.86]`, score 0.96) salvo metadatos previos | `face_index.py:88`, `:110-142` |

`docs/decisiones.md:187-196` y `README.md:19` / `docs/walkthrough.md:13` describen estos componentes como modelos reales. Los tests de aceptación `VAL-PERC-002..005` validan la simulación, no la calidad real. **No hay ningún TODO/FIXME que señale la divergencia.**

### Ideas derivadas

- **M3-IDEA-001 (alta)** — Integrar faster-whisper/whisperx real detrás de la fachada existente (`resolve_asr_backend_plan` ya modela la cadena de fallback; conectarla a ejecución real). Mantener la ruta determinista solo para tests, activada por fixture/monkeypatch explícito, nunca por defecto en producción.
- **M3-IDEA-002 (alta)** — Integrar CLIP ViT-L/14 real (open_clip, `onnxruntime` ya presente en las sondas `config/probes/ort_cuda.py`) para `embed_text_clip`/`embed_image_clip`. La dimensión 64 está fija en `store.py:16-17`; un embedding real (768 dims) requiere migración de esquema o proyección documentada.
- **M3-IDEA-003 (media)** — Integrar InsightFace buffalo_l real respetando el aviso de licencia NC ya exportado (`face_index.py:19-23`); la cara sintética actual hace inútil `find_shots_face` en producción.
- **M3-IDEA-004 (media)** — Integrar FunASR cam++ real para diarización (ya se declara `requires_hf_token: False`, compatible con la restricción de no depender de pyannote/HF token).
- **M3-IDEA-005 (alta, barata)** — Mientras no exista ML real: añadir bandera explícita de modo simulación (p. ej. `STUDIO_PERCEPTION_SIM=1`) y **etiquetar las respuestas de API y la UI del panel** con `simulated: true`. Alinear `README.md`, `walkthrough.md` y `decisiones.md` con el estado real. Esto elimina el riesgo de claims falsos de calidad en producción.
- **M3-IDEA-006 (baja)** — Añadir un smoke test que falle si un backend declarado como "real" está en modo simulación en un perfil de producción (`MODEL_PROFILE=studio`), para prevenir regresiones de honestidad.

## 2. Seguridad

- **M3-SEC-001 (alta)** — `STUDIO_WEBHOOK_URL` se usa sin validar (`webhooks.py:217-224`): no hay chequeo anti-SSRF/loopback pese a la regla 5 de `AGENTS.md`. Rechazar URLs que resuelvan a 127.0.0.0/8, ::1, RFC1918 y metadata (169.254.169.254) al crear el notifier. Riesgo medio-bajo (configurado por operador) pero la regla existe y no tiene guarda.
- **M3-SEC-002 (alta)** — Cambio de postura de auth en `app.py` (hunk @:539-545): con `require_auth=True` pero sin `COMFY_PASS`, los endpoints `/studio` se sirven **sin autenticación** en vez de 401 (fail-open silencioso). Mitigado solo por binding 127.0.0.1 + nginx. Idea: fail-closed con degradación visible (health `degraded` + banner en status server + log WARN en arranque), o exigir `STUDIO_ALLOW_NOAUTH=1` explícito para el modo sin contraseña.
- **M3-SEC-003 (media)** — `_BoundedMultiPartParser` eliminó el techo de bytes totales de la petición (se conservó el techo por parte). Restaurar un límite total configurable (p. ej. `STUDIO_UPLOAD_MAX_TOTAL_MB`) para evitar presión de memoria/disco con muchas partes.

## 3. Robustez / ingeniería

- **M3-IDEA-010 (alta)** — `probe_media_info` fabrica metadatos (320x180/h264/has_video=True) cuando ffprobe falta o falla (`keyframes.py:43-90`); la verificación EDL (`ffmpeg_edl.py:314`, `:356-364`) puede pasar con números falsos, y la extracción de keyframes cae en un PNG sintético de color sólido (`keyframes.py:380-385`). Idea: devolver `probe_error` explícito y hacer que la verificación EDL falle duro si el probe no es real.
- **M3-IDEA-011 (media)** — Timeouts de subprocess ffmpeg/ffprobe hardcodeados (60 s/20 s/10 s en `ffmpeg_edl.py:264`, `:308`; `keyframes.py:68`, `:167`, `:355`): clips largos fallarán el render EDL. Hacerlos configurables (`STUDIO_FFMPEG_TIMEOUT_S`) o proporcionales a la duración del timeline.
- **M3-IDEA-012 (baja)** — `webhooks.py:38` mapea jobs `cancelled` a evento `job_failed`: desajuste semántico para consumidores. Añadir evento `job_cancelled`.
- **M3-IDEA-013 (baja)** — La tarea de recovery se cancela en shutdown (`app.py` lifespan `finally`): un resume en vuelo puede interrumpirse a mitad de `execute_prompt_job`. Idea: drain con timeout o marcar el job antes de cancelar.
- **M3-IDEA-014 (baja)** — Agotar el `CallBudget` reporta `stop_reason="max_iterations"` (`loop.py`): telemetría engañosa. Usar `call_budget_exhausted`.
- **M3-IDEA-015 (baja)** — `_maybe_summarize_history` consume presupuesto de llamadas LLM antes de cualquier llamada orientada al usuario; contabilizarla aparte o ejecutarla con presupuesto propio.
- **M3-IDEA-016 (media)** — Herramientas mutantes (`tag_asset`, `propose_edl`, `transcribe`, `diarize`, `index_media`, `extract_keyframes`) son `parallel_safe=True` por defecto (`tools.py:1344`): escrituras SQLite concurrentes dependen de WAL. Idea: marcarlas no-paralelas, o verificar `PRAGMA journal_mode=WAL` + `busy_timeout` en `store.py`/`projects.py` y añadir test de contención.
- **M3-IDEA-017 (media)** — `store.py:14` hace `import sqlite_vec` duro: si el paquete falta, la app no arranca. Idea: import perezoso con degradación (búsqueda deshabilitada + health `degraded`), y test de la ruta sin `sqlite_vec`.
- **M3-IDEA-018 (baja)** — Techo de salida EDL de 200 MB hardcodeado (`ffmpeg_edl.py:325`): configurable o documentado en la guía de producción.

## 4. Tests faltantes

- **M3-IDEA-020 (media)** — Cobertura a nivel endpoint para: `identities` PATCH/DELETE, `POST faces/index`, `POST search/combined` (hoy solo se ejercitan indirectamente vía store/tools).
- **M3-IDEA-021 (media)** — Tests de: comportamiento EDL con timeout >60 s, guarda SSRF del webhook (tras M3-SEC-001), arranque sin `sqlite_vec`, y modo no-auth explícito (tras M3-SEC-002).
- **M3-IDEA-022 (baja)** — Test de regresión del race corregido en `comfyui_client.py:897-916` (WS `node=None` ya no marca `done` prematuramente): simular `execution_complete` antes de persistencia de outputs y verificar que el job no se cierra temprano.

## 5. Operaciones / gobernanza

- **M3-OPS-001 (alta)** — **Cron externo descontrolado**: un job programado externo está generando un archivo placeholder de auditoría por minuto (`AUDITORIA_AGENTE_V10..V159.md`, ~407-409 B cada uno, registrados en `docs/auditorias/LONG_RUN_AUDIT_TRACKER.md` entre 02:13 y 04:42 UTC del 2026-10-28). Viola la regla de cadencia de `AGENTS.md` ("máx. una auditoría por día o por PR, sin ciclos minuto a minuto; auditorías vacías o duplicadas son inválidas"). Acción propuesta: desactivar la tarea programada externa, eliminar los placeholders V41-V159 (conservando V9 real) y podar el tracker. La auditoría V9 ya había limpiado una tanda anterior de estos archivos.
- **M3-OPS-002 (alta)** — Commitear el batch M3 en la rama `m3-percepcion-edl` tras pasar la validación completa de `AGENTS.md` (ruff, pytest, py_compile, `python -m comfy_studio.templates`, `check_models_manifest.py`, cargo fmt/clippy/test, `nginx -t`). Incluir `AGENTS.md` y `AUDITORIA_AGENTE_V9.md`, hoy sin seguimiento. Los `__pycache__/` dentro de `asr/`, `edl/`, `perception/` no deben commitearse (verificar `.gitignore`).
- **M3-OPS-003 (media)** — Unificar esquemas de IDs: el batch M3 usa `VAL-PERC-*`/`VAL-PROD-*` en `decisiones.md` mientras `AGENTS.md` prescribe `Mx-SCR/AGT/OPS-nnn`. Documentar el mapeo o adoptar la convención única.
- **M3-OPS-004 (baja)** — Documentar las nuevas variables de entorno (`STUDIO_WEBHOOK_URL`, `STUDIO_WEBHOOK_SECRET`, `STUDIO_RESUME_JOBS`, `STUDIO_RESUME_DELAY_S`) en `README.md` y `docs/GUIA_PASO_A_PRODUCCION.md` con su semántica de seguridad (el secret ya pasa por `SecretRedactor` y se enmascara en `public_dict`, `config.py:116-125`, `:480-486`).

## 6. Ideas de producto (nuevas capacidades)

- **M3-IDEA-030** — Caja de búsqueda semántica en el panel sobre `clip_index.find_shots_text` (la API `GET/POST /studio/api/.../search` ya existe en `app.py:1318`, `:1372`; falta UI dedicada con resultados en cuadrícula de keyframes).
- **M3-IDEA-031** — Timeline EDL interactivo: reordenar/ajustar cortes con drag & drop sobre los segmentos ya renderizados en la vista Timeline (`panel/app.js:1543+`), regenerando `edl.vN+1` vía `POST edl/build`.
- **M3-IDEA-032** — Exportación de subtítulos SRT/VTT desde `transcriptions` + EDL (los datos de segmentos y palabras ya se persisten en `projects.py:1043-1172`); útil tanto con ML real como simulado, y barato de implementar.
- **M3-IDEA-033** — Auto-highlights híbridos: combinar energía acústica (ya calculada en `analyze_wav_acoustic_windows`, `whisperx.py:233`) con cambios de escena de keyframes para proponer cortes sin depender de fillers de texto.
- **M3-IDEA-034** — Eventos de webhook para `transcription_done` y `edl_rendered` (el dispatcher `BrokerWebhookDispatcher` solo cubre jobs de render y turns de agente, `webhooks.py:42-69`).
- **M3-IDEA-035** — Extender el health check S11 `studio_health` con una sonda de percepción: disponibilidad real de ffprobe/ffmpeg, `sqlite_vec` importable, y modo simulación activo — visible en el status server para detectar pods degradados antes de usarlos.
- **M3-IDEA-036** — Render por lotes multi-aspecto: un solo `POST edl/{version}/render` con lista de presets (`ASPECT_DIMENSIONS` ya define 16:9, 9:16, 1:1, 4:5 en `ffmpeg_edl.py:22`) encolando N jobs, con eventos de webhook por variante.
- **M3-IDEA-037** — Superficie de `delegate_role` en el panel: mostrar qué sub-agente (director/editor/artist3d) ejecutó cada herramienta en el stream de eventos del chat (los `ROLE_TOOL_SUBSETS` ya existen en `tools.py:602`).

## 7. Priorización sugerida

| Prioridad | IDs |
|---|---|
| **P0 (antes de merge M3)** | M3-OPS-001, M3-OPS-002, M3-IDEA-005, M3-SEC-001, M3-SEC-002 |
| **P1 (corto plazo)** | M3-IDEA-001, M3-IDEA-010, M3-SEC-003, M3-IDEA-016, M3-IDEA-017, M3-IDEA-020/021 |
| **P2 (siguiente hito)** | M3-IDEA-002, M3-IDEA-003, M3-IDEA-004, M3-IDEA-030..037 |
| **P3 (higiene)** | M3-IDEA-011..015, M3-IDEA-018, M3-OPS-003, M3-OPS-004, M3-IDEA-006, M3-IDEA-022 |
