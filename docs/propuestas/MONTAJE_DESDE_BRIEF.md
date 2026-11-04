# Montaje asistido y verificable desde brief (M7)

Propuesta implementada en `studio/comfy_studio/montage/` + `studio/comfy_studio/routers/montage.py`.

## Idea

Dado un **brief en lenguaje natural** ("quiero los momentos del incendio y el fuego, del segundo 2 al 4"),
el estudio recupera los momentos relevantes del proyecto cruzando las modalidades que ya existen
(imagen vía índice CLIP, diálogo vía transcripciones persistidas, persona vía identidades faciales),
propone **cortes con asset y timecodes de origen**, **persiste un EDL versionado
(`comfy_studio.edl.v1`, `kind="montage"`)** y, solo si se pide `render=true`, produce un **borrador
real con ffmpeg verificado con ffprobe** a través del ejecutor EDL existente. Cada corte trae un
bloque `evidence` (shot_id / keyframe_asset_id / transcription_id / segment_id / identity_id,
términos coincidentes, similitud) para que cada decisión se rastree hasta el material real del
proyecto. Los timecodes se acotan a la duración real del asset medida con ffprobe; si el probe
falla, el corte se marca `timecode_verified: false` en vez de presentarse como verificado.

## API

- `POST /studio/api/projects/{project_id}/montage/from-brief` — body `MontageBriefRequest`
  (`brief`, `target_duration_sec`, `aspect_ratio`, `max_cuts`, `asset_ids`, `person`,
  `time_range_sec`, `render`, `fps`). Devuelve el plan + el EDL guardado (`edl_id`, `version`)
  y, con `render=true`, el resultado verificado del render. Publica eventos SSE `montage`
  (`planned`) y `edl` (`built`).
- El EDL resultante se lista/edita/renderiza con los endpoints de EDL ya existentes
  (`GET .../edl`, `POST .../edl/build`, `POST .../edl/{version}/render`).

## Por qué esta idea

El repositorio ya tenía búsqueda cruzada (`search/combined`), highlights desde transcripción
(`edl/highlights`, con refinamiento híbrido acústico/escenas) y render EDL verificado. Faltaba el
**paso de decisión de montaje**: transformar un brief en cortes relevantes con evidencia, en vez de
tomar todos los segmentos de habla. Esto es exactamente la tarea de *moment retrieval / query-based
video highlights* de la literatura (ver fuentes), pero apoyada en los índices existentes del
proyecto en lugar de un modelo nuevo: no se añaden pesos, descargas ni dependencias.

## Fuentes primarias consultadas

- Lei, Berg, Bansal — "Detecting Moments and Highlights in Videos via Natural Language Queries"
  (Moment-DETR / QVHighlights, NeurIPS 2021): [arXiv:2107.09609](https://arxiv.org/abs/2107.09609),
  código [github.com/jayleicn/moment_detr](https://github.com/jayleicn/moment_detr)
  (código Apache-2.0; el dataset QVHighlights es CC BY-NC-SA 4.0 y **no** se usa aquí).
- Yang et al. — "Vid2Seq: Large-Scale Pretraining of a Visual Language Model for Dense Video
  Captioning" (CVPR 2023): [arXiv:2302.14115](https://arxiv.org/abs/2302.14115),
  código [github.com/antoyang/vid2seq](https://github.com/antoyang/vid2seq) (Apache-2.0).
  Justifica usar la transcripción (ASR) como señal de eventos además de los fotogramas.
- Radford et al. — "Learning Transferable Visual Models From Natural Language Supervision"
  (CLIP, ICML 2021): [arXiv:2103.00020](https://arxiv.org/abs/2103.00020); implementación
  [open_clip (MIT)](https://github.com/mlfoundations/open_clip) ya prevista en el backlog
  (M3-IDEA-002) para el índice visual real.
- Bain et al. — "WhisperX: Time-Accurate Speech Transcription of Long-Form Recordings"
  (arXiv:2303.10248): [github.com/m-bain/whisperX](https://github.com/m-bain/whisperX)
  (BSD-3-Clause), ya dependencia del perfil `asr` del proyecto.
- InsightFace (uso no comercial, aviso ya exportado en `face_index.py`):
  [github.com/deepinsight/insightface](https://github.com/deepinsight/insightface).

## Honestidad sobre simulación

Mientras `STUDIO_PERCEPTION_BACKEND` no esté en modo `real` verificado, las evidencia visual y
facial (y la transcripción) provienen de backends simulados deterministas: la respuesta lleva
`simulated: true` + `simulation_notice` y cada evidencia lo marca. La lógica de montaje
(recuperación por términos idf, fusión, empaquetado con duración objetivo), la verificación de
timecodes con ffprobe y el render del borrador son **reales y deterministas**.

## Medición (tests en `studio/tests/test_m7_montage_brief.py`)

Fixture reproducible: vídeo real de 6 s con 3 escenas (ffmpeg lavfi) + transcripción con
`script_segments` de 3 segmentos. Brief "quiero los momentos del incendio y el fuego" con
`target_duration_sec=2.5`:

- **precisión frente al brief** (fracción de la duración seleccionada que solapa el tramo
  relevante 2–4 s): el montaje obtiene **1.0**; la línea base `edl/highlights` sobre el mismo
  fixture obtiene **≈0.33** (selecciona los 3 segmentos). El montaje supera a la base.
- el 100 % de los cortes trae `evidence` con timecodes dentro de la duración real del asset
  verificada con ffprobe (`cuts_timecode_verified == len(cuts)`);
- con `render=true` el borrador MP4 queda registrado y verificado
  (`verification.probe_ok=true`, duración real ≈ esperada, 1280x720 en 16:9).

## Requisitos, costo y reversión

- Sin modelos nuevos ni descargas: reutiliza `index.db`, transcripciones y EDL existentes.
- El render es opt-in (`render=false` por defecto) y usa los timeouts/cotas ya configurados
  (`STUDIO_FFMPEG_TIMEOUT_S`, `MAX_EDL_CUTS`, `MAX_EDL_OUTPUT_BYTES`).
- Reversión: eliminar `studio/comfy_studio/montage/`, `studio/comfy_studio/routers/montage.py`
  y su entrada en `routers/__init__.py`; los EDL `kind="montage"` quedan como datos de proyecto.
