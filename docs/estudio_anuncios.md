# Estudio de Anuncios (`ad_studio`)

Motor nativo del estudio que convierte un **brief de producto** en **variantes de
anuncio montadas** (9:16, 1:1, 16:9, 4:5), con guion, storyboard, voz y música
opcionales y evaluación QA automática por variante. Equivalente abierto y local a las
funciones «Marketing Studio / URL-to-video» de Higgsfield, «batch de variantes 9:16» de
ClipLoft y «auto-reframe» de CapCut; la investigación completa con fuentes, licencias,
hardware y limitaciones está en
[`propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md`](propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md).

No descarga modelos nuevos: compone los motores ya presentes en el pod (ffmpeg/EDL,
`audio` TTS, `music_gen`, `qa`). Todo corre en CPU salvo voz/música/QA-nivel-2, que
usan sus backends habituales.

## Flujo (tres ops del motor)

1. **`script`** — brief estructurado → guion JSON (`comfy_studio.ad_script.v1`),
   compuesto por un **composer determinista** (4 escenas: hook, problema, solución,
   CTA; duración objetivo repartida y cuantizada). El agente puede refinarlo por chat.
2. **`storyboard`** — guion → storyboard JSON (`comfy_studio.ad_storyboard.v1`): cada
   plano lleva duración, prompt visual, texto en pantalla, línea de voz y el asset de
   producto asignado (round-robin). El anuncio entra en la puerta de aprobación
   (`gate.state = awaiting_approval`, `engines/storyboard_gate.py`).
3. **`variants`** — storyboard + assets del proyecto → un MP4 por aspect ratio con
   `ffmpeg` (scale-cover + crop centrado + concat; `amix` de voz TTS y/o cama musical
   opcional), verificado con `ffprobe`, evaluado con el motor `qa` y registrado como
   asset `outputs`. Crea un job `ad_variants` con progreso SSE y **cancelación sticky**
   entre variantes.

## API (tras Basic Auth del pod)

```
POST /studio/api/projects/{pid}/ads/script            body: AdBriefRequest -> 201 ad (con guion)
GET  /studio/api/projects/{pid}/ads                   lista de anuncios
GET  /studio/api/projects/{pid}/ads/{ad_id}           detalle
POST /studio/api/projects/{pid}/ads/{ad_id}/storyboard  deriva y persiste el storyboard
POST /studio/api/projects/{pid}/ads/{ad_id}/variants  202 {job_id,...} (async, por defecto)
POST .../variants?wait=1                              200 con el resumen (síncrono)
```

`AdVariantsRequest`: `aspect_ratios` (máx. 4 de `16:9|9:16|1:1|4:5`, por defecto
`["9:16","1:1","16:9"]`), `voiceover`, `voice_id`, `music`, `music_prompt`,
`music_duration_s`, `source_asset_id`, `chunk_asset_ids` (un asset de vídeo por plano),
`fps` (12–60), `max_retries` (0–2), `acceptance` (`max_black_s`, `max_freeze_s`).

Ejemplo:

```bash
curl -u "$COMFY_USER:$COMFY_PASS" -X POST \
  "http://localhost:8188/studio/api/projects/$PID/ads/script" \
  -H 'Content-Type: application/json' \
  -d '{"product_name":"Cafetera Nova","key_message":"El café perfecto en 30 s",
       "audience":"profesionales ocupados","tone":"cercano","target_duration_s":15,
       "cta":"Pruébala hoy con 20% de descuento","product_asset_ids":["<asset_id>"]}'

curl -u "$COMFY_USER:$COMFY_PASS" -X POST \
  "http://localhost:8188/studio/api/projects/$PID/ads/$AD_ID/variants?wait=1" \
  -H 'Content-Type: application/json' \
  -d '{"aspect_ratios":["9:16","1:1","16:9"],"voiceover":true}'
```

## Agente (chat / MCP tool-belt)

Tres herramientas nuevas: `ad_script`, `ad_storyboard`, `build_ad_variants`
(esquemas Pydantic con `extra="forbid"`; `build_ad_variants` crea un job trazable y
cancelable con `job_cancel`).

```
"Crea un anuncio para la Cafetera Nova dirigido a profesionales ocupados,
 con CTA de descuento, y genera sus variantes 9:16 y 16:9 con voz"
```

## Panel

Pestaña **6. Anuncios**: formulario de brief (producto, mensaje, audiencia, tono,
duración, CTA; toma como assets de producto las imágenes/vídeos del proyecto),
listado de anuncios con botones «Seleccionar»/«Storyboard», y generación de variantes
con checkboxes de formato, voz y música. Los resultados aparecen en la lista y en la
Galería (assets `outputs` con metadatos `engine=ad_studio`).

## Modos y honestidad

- `STUDIO_STUB_AD_STUDIO=1`: variantes simuladas (MP4/WAV mínimos) marcadas
  `simulated: true` — solo para tests/CI sin ffmpeg.
- Sin ffmpeg y sin stub: fallo **tipado** (`AdPipelineError`), nunca finge.
- Cada variante informa `qa_passed`, métricas nivel 1 (`black_s`, `freeze_s`) y
  `simulated`; si QA no pasa, la variante queda `status="needs_review"`.
- Persistencia en `project_meta` (`ads:index`, `ad:<id>`); no se borra nada existente.

## Pruebas

`studio/tests/test_m7_ad_studio.py` (24 tests): composición determinista, validación,
persistencia, puerta, variantes stub con assets reales registrados, cancelación
sticky, fallos tipados (sin storyboard / aspect inválido / asset ausente / sin
ffmpeg), contrato del motor, registro en el roster, rutas HTTP (201/200/202/400/404/422)
y flujo completo del tool-belt. Uno requiere `ffmpeg` real (render 1280x720 verificado
con ffprobe + QA) y se salta si no está en el PATH.

Comandos ejecutados (2026-11-04): `ruff check studio panel scripts config/probes` →
limpio; `pytest studio/tests -q` → 602 passed (23 fallos preexistentes del sandbox por
`sqlite-vec`/`ffmpeg` ausentes, reproducibles en `HEAD` limpio), 10 skipped;
`python -m comfy_studio.templates` → 17 plantillas OK;
`check_models_manifest.py --fixture --offline` → 4/4 OK.
