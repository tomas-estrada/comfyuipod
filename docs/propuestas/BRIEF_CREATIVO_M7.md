# Brief creativo persistente + comprensión multimodal con evidencia (M7, Worker 7)

Estado: implementado en `vibe/creative-brief-m7-4b3dbe` (PR abierto como draft).

## Qué se entrega

El flujo pedido: **assets + prompt → comprensión con evidencia → brief creativo
persistente/editable → dirección editorial → (producción con las APIs existentes)
→ revisión del render real → paquete de entrega → feedback**.

Módulos nuevos (sin dependencias nuevas, sin modelos nuevos, sin descargas):

- `studio/comfy_studio/creative/understanding.py` — `build_understanding()`:
  recorre assets (acotables por `asset_ids`), ffprobe real por asset (`probe_ok`
  honesto), hash sha256 con deduplicación, transcripciones con intervalos y
  hablantes, shots CLIP e identidades (si hay índice), EDL existentes. Cada
  hecho conserva `asset_id`, `interval_sec`, `method`, `simulated` y `evidence`
  (transcription_id/segment_id/shot_id/identity_id). Archivos ausentes o no
  verificables se marcan en `invalid` sin romper el proyecto. Topes configurables
  (`STUDIO_BRIEF_MAX_FACTS`, `STUDIO_BRIEF_MAX_HASH_BYTES`,
  `STUDIO_BRIEF_MAX_SEGMENTS`): nada se envía completo al contexto del LLM.
- `studio/comfy_studio/creative/brief.py` — contrato `comfy_studio.brief.v1`:
  propósito, mensaje, tipo de pieza, audiencia (hipótesis explícita con
  `is_hypothesis`), plataforma/idioma/región/duración, concepto/tono/emoción,
  `user_constraints` (solo lo dicho explícitamente; prevalecen), `asset_facts`
  respaldados por evidencia, `assumptions` (decisiones del sistema visibles),
  `pending`, `budget`, `acceptance_criteria` derivados de las restricciones y
  `editorial`. `parse_prompt()` es determinista (sin LLM): extrae duración,
  aspect, plataforma, idioma, música, subtítulos, texto en pantalla, CTA, tono y
  tipo de pieza, cada uno trazado al fragmento original del prompt.
- `studio/comfy_studio/creative/direction.py` — dirección editorial heurística y
  transparente (`method: heuristic-v1`): promesa, hook con evidencia, estructura
  en momentos referenciados a asset+intervalo, selección de material por
  intención (usa/sobra con motivo; duplicados exactos excluidos), plan de
  audio (diálogo original/música/voz/silencio/subtítulos) y CTA. No impone una
  estructura única de planos ni ritmos.
- `studio/comfy_studio/creative/review.py` — `review_output()` sobre el render
  real: mediciones técnicas ffprobe (duración, aspect, audio, codecs,
  integridad) + comprobaciones objetivas contra el brief, separadas de la
  evaluación editorial que queda `requires: human_or_vlm` (no se inventan notas).
  Cada issue trae `correction` localizada (qué tramo/corte tocar). El informe se
  persiste en `project_meta` (`creative_review:{asset_id}`).
- `studio/comfy_studio/creative/delivery.py` — paquete de entrega: video final +
  variantes (EDL renderizadas), previews, portada, subtítulos (SRT/VTT),
  título/caption/hashtags con justificación y `evidence_refs`, EDL editable,
  assets generados con procedencia, decisiones y limitaciones. `trend_data` se
  marca `available: false` con la limitación declarada cuando no hay acceso a
  datos actuales (nunca se inventan métricas ni "probabilidad de viralidad").
  `publishing.published` siempre `false`: preparar ≠ publicar.

Persistencia: tabla nueva `creative_briefs` en `studio.db` (append-only,
`changes_json` con `source: user|system`), más `project_meta` para understanding,
reviews y feedback (`creative_feedback`, que se hereda en briefs nuevos como
`learned_preferences`). Reanudable tras reinicio del pod (test con store nuevo
sobre el mismo data dir).

## API (todas bajo `/studio/api/projects/{project_id}/...`, auth existente)

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/brief/understand` | comprensión multimodal con evidencia (persiste en meta) |
| POST | `/brief` | crea brief v1 desde `prompt` + `asset_ids` + `overrides` (201) |
| GET | `/brief` | brief activo + understanding + preferencias aprendidas |
| GET | `/briefs` | historial de versiones |
| GET | `/brief/{version}` | versión concreta |
| GET | `/brief/delivery` | paquete de entrega del brief activo |
| PATCH | `/brief` | edita campos editables (nueva versión, `source: user`) |
| POST | `/brief/{version}/approve` | aprobación humana (nueva versión `approved`) |
| POST | `/brief/feedback` | feedback → brief + `creative_feedback` (aprendizaje) |
| POST | `/brief/review` | revisión del render real de `asset_id` contra el brief |

Eventos SSE: `brief` (understood/created/updated/approved/feedback) y `review`.

## Agente (modo agente existente)

Nuevas herramientas en el tool-belt (`agent/tools.py`, sin romper MCP:
`BELT_TOOLS_EXPOSED` intacto): `understand_project`, `create_creative_brief`,
`update_creative_brief`, `review_output`. Entran en el subconjunto del rol
`editor` y en `SYSTEM_PROMPT_ES` (items 33–36). Todas con
`parallel_safe=False` (escriben `studio.db`), como exige el test de hardening.

## Pruebas de aceptación (studio/tests/test_m7_creative_brief.py, 12 tests)

1. Un MP4 + "hazme un reel brutal" → brief draft con supuestos visibles, hechos
   con evidencia y selección por intención.
2. Brief específico (15 s, 9:16, sin música, con subtítulos, inglés, TikTok,
   tono épico) → `user_constraints` + criterios de aceptación derivados.
3. Evento con transcripción exacta ("12 de octubre de 2026 en Madrid") → la
   frase se conserva verbatim con transcription_id/hablante/intervalo y
   `simulated: true`; el único dato de fecha del brief proviene de la evidencia.
4. Pieza sin diálogo (solo fotos) → plan de audio sin diálogo/subtítulos.
5. 100 assets mixtos (98 PNG con duplicados por hash + 2 MP4) + 1 subida
   inválida (415) + 1 archivo eliminado → `invalid`, duplicados detectados,
   material sobrante con motivo; el proyecto no se cae.
6. Interrupción/reanudación: store nuevo sobre el mismo dir recupera el brief;
   PATCH → v2, approve → v3 `approved`, historial ordenado.
7. Edición localizada ("menos publicidad, más emoción") → solo ese campo
   cambia; constraints/duración/aspect intactos.
8. Revisión: separa mediciones técnicas (ffprobe) de editorial
   (`human_or_vlm`), correcciones localizadas, informe persistido; con ffmpeg
   real (skip si no hay binario) un render 6 s 16:9 pasa con `passed=true`.
9. Entrega: video/variantes/EDL/subtítulos/provenance presentes; tendencias
   marcadas no disponibles; `published=false`.
10. Feedback queda en el brief y se hereda (`learned_preferences`) en el
    siguiente brief.
11. Tool-belt del agente ejecuta el flujo completo (understand → create →
    update → review) con versiones crecientes.
12. Índice vectorial real (sqlite-vec) alimenta `clip_shot` facts con intervalo
    y `simulated`.

## Verificación ejecutada

- `ruff check studio panel scripts config/probes` — OK.
- `pytest studio/tests -q` — 627 passed, 10 skipped, 8 failed: los 8 fallos son
  pre-existentes y ambientales (este sandbox no tiene `ffmpeg`; CI sí lo tiene).
  Baseline antes del cambio: 615 passed / mismos 8 fallos.
- `python -m py_compile config/probes/*.py scripts/check_models_manifest.py`,
  `python -m comfy_studio.templates` (17 plantillas OK),
  `scripts/check_models_manifest.py --fixture --offline` — OK.
- Smoke real: `python -m comfy_studio.server` en 127.0.0.1 con stubs → health
  200, crear proyecto 201, `POST /brief` 201 (v1, `reel`), `GET /brief` 200,
  `GET /brief/delivery` 200.

**No verificado aquí**: GPU, pesos ML reales (percepción/ASR corren simulados y
se marcan `simulated: true`), ffmpeg (ausente en el sandbox; el test de render
verificado se salta y corre en CI).

## Handoff para el worker 6 (integración)

- No hay cambios en `comfyctl/`, `config/nginx.conf` ni contratos de descarga:
  no aplica cargo/nginx en este PR.
- Puntos de fricción a vigilar al integrar: (1) la tabla `creative_briefs` se
  crea con `CREATE TABLE IF NOT EXISTS` dentro del script V1 — en DBs existentes
  aparece en el siguiente arranque sin migración; (2) las rutas nuevas
  `*/brief/*` ya están en el mapa dorado de `test_route_map.py`; (3) PR #54
  (montaje desde brief) toca `routers/__init__.py` — resolver el import
  adicionando `brief` sin pisar su línea.
- Siguiente paso natural: que `create_creative_brief` alimente directamente el
  montaje desde brief de PR #54 (mismos `asset_facts`/evidence) y que
  `review_output` se enganche tras `execute_edl_ffmpeg` para cerrar el ciclo
  review→corrección localizada→re-render de una variante.
