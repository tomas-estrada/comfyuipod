# Investigación competitiva limpia y selección de funciones — 2026-11-04

**Fecha de consulta de todas las fuentes: 2026-11-04 (UTC).** Metodo: análisis de
producto limpio (documentación pública oficial, páginas de precios, model cards,
repositorios públicos y papers). **No** se usaron endpoints no documentados, código
privado, pesos propietarios, prompts internos, datos de usuarios, scraping de cuentas
privadas ni mecanismos para evadir suscripciones, créditos o controles de acceso.

Objetivo: identificar funciones premium de herramientas comerciales de creación
audiovisual que resuelvan problemas creativos concretos, localizar alternativas open
source equivalentes con licencia verificable, e integrar las mejores como funciones
nativas de ComfyUIPod. Resultado integrado: el motor **`ad_studio`** (estudio de
anuncios: brief → guion → storyboard → variantes de formato con QA), ver
[`../estudio_anuncios.md`](../estudio_anuncios.md).

---

## 1. Productos y funciones investigados

### 1.1 Kling AI (Kuaishou) — `kling.ai` / `klingai.com`
Fuentes primarias: página oficial de exploración/precios `kling.ai/explore/kling_ai_pricing`,
release notes oficiales (Kling 4.0 GA desde 2026-10-15) y guías oficiales de usuario,
verificadas el 2026-11-04 (citadas también por reviews de terceros fechados en octubre 2026).

| Función | Entradas / salidas / controles visibles | Pago / créditos |
|---|---|---|
| T2V / I2V (Kling 4.0, 3–30 s nativo; 4.0 Flash hasta 20 s) | texto o imagen → MP4; selector de modelo, resolución, duración | planes desde ~$10/mes; créditos por generación; Ultra+ (~$4.457 el primer año) da acceso early a 4.0 Flash |
| Keyframes (hasta 10 imágenes) + start/end frame | imágenes que anclan momentos del timeline | de pago |
| **Omni Reference** (hasta 15 entradas: imágenes, vídeo, sujetos, muestras de voz) | combina apariencia, movimiento y sonido de referencias | de pago (Kling 4.0) |
| Camera control | trayectoria/movimiento de cámara dirigido | de pago |
| Lip-sync + audio nativo (2 canales, 5 idiomas) | vídeo con diálogo sincronizado labialmente | de pago (2.6+) |
| Extend (hasta ~3 min) y multi-shot/storyboard (hasta 6 planos conectados en 3.0) | continuación y guion visual por plano | de pago |
| Motion Brush, Modify Video (edición por prompt) | pintado de movimiento; edición localizada | de pago |
| API REST (t2v, i2v, extend, lipsync) | automatización | API de pago por uso |

### 1.2 Higgsfield — `higgsfield.ai`
Fuentes: páginas oficiales de features/precios (planes Starter $15, Plus $49, Ultra $129
verificados octubre 2026) y guías oficiales; MCP connector propio.

| Función | Entradas / salidas / controles | Pago |
|---|---|---|
| Cinema Studio: 70–120+ presets de cámara (dolly, crash zoom, bullet time, FPV, órbita 360) | clip con movimiento de cámara aplicado | créditos por generación |
| Soul / Soul 2.0 (imagen) + **Soul ID** (identidad de personaje desde 5–80 fotos) | consistencia de sujeto entre generaciones | de pago |
| **Speak / Lipsync** (avatar que habla con audio) | imagen/personaje + audio → vídeo con labios sincronizados | de pago |
| Inpaint (brocha), face swap, try-on, 80+ apps | edición localizada de imagen/vídeo | de pago |
| **Marketing Studio / URL-to-video**: URL o fotos de producto → anuncio en varios formatos | brief/URL de producto → variantes de anuncio | de pago (créditos) |
| Acceso multi-modelo (Kling 3.0, Veo 3.1, Sora 2, Seedance 2.0, Wan 2.6…) bajo un login | selector de modelo | suscripción |

### 1.3 Suno — `suno.com`
Fuentes: `suno.com`, `about.suno.com/release-notes`, página de precios Pro $8–10/mes
(2.500 créditos ≈ 500 canciones, derechos comerciales) y Premier $24–30/mes (10.000
créditos + Suno Studio), verificadas 2026-11-04.

| Función | Entradas / salidas / controles | Pago |
|---|---|---|
| Generación de canción completa con voz y letra (v5.5, 2026-03-26) desde prompt o voz; Personas, Custom Models, Voices | texto/voz → canción; estilos, letra, idioma | gratis personal (v4.5); Pro de pago para v5.5 y uso comercial |
| **Stem separation** (vocales/instrumental; Studio: hasta 12 stems generativos, «Advanced Stem Separation» junio 2026) | canción → stems WAV alineados | Pro/Premier |
| **Suno Studio (DAW navegador, solo Premier)**: multitrack, EQ, Warp Markers, Remove FX, remaster con v5, export WAV/MIDI | edición multipista post-generación | solo Premier |
| Continue/Extend, Cover, Replace section | continuar o reemplazar secciones de un clip | créditos |

### 1.4 Candidatos llamados “Omni” (identificados explícitamente)
No existe un único “Omni”; se distinguen tres candidatos razonables:

| Producto | Qué es | Estado de pesos |
|---|---|---|
| **ByteDance OmniHuman-1.5** (paper arXiv 2508.19209, ago 2025) y **OmniHuman 2.0** | imagen única + audio → avatar parlante/cantante con lip-sync, gestos y emoción semántica; hasta 30 s (1.5) / 5 min (2.0) | **CERRADOS**: solo vía BytePlus/Volcano Engine, CapCut/Dreamina y APIs de pago (Replicate, WaveSpeed, eachlabs). No hay pesos descargables |
| **Kling “Omni” / “Omni Reference”** (Kuaishou, Kling 3.0/4.0) | generación de vídeo guiada por hasta 15 referencias (imagen/vídeo/sujeto/voz) | cerrado (servicio) |
| **BAAI OmniGen2** (`VectorSpaceLab/OmniGen2`, arXiv 2506.18871, jun 2025) | modelo multimodal abierto unificado: T2I, edición por instrucción e in-context; pesos en HuggingFace `BAAI/OmniGen2` / `OmniGen2/OmniGen2` | **abierto**: código Apache-2.0; tarjeta HF del repo (verificar licencia de pesos en la model card antes de uso comercial); ~17 GB nativos |

Equivalentes abiertos para avatar audio-driven (OmniHuman): **Hallo3**
(`fudan-generative-vision/hallo3`, CVPR 2025), **EchoMimicV3** (`antgroup/echomimic_v3`,
AAAI 2026, 1.3B, 12–16 GB VRAM, con nodos ComfyUI) y **LatentSync** (Apache-2.0, **ya
integrado** en el pod vía `ComfyUI-LatentSyncWrapper` y plantilla `latentsync`).
Licencias de Hallo3/EchoMimicV3: repositorios públicos con licencia abierta (Apache-2.0
según sus README; ⚠ verificar el archivo LICENSE exacto antes de redistribuir pesos).

### 1.5 Otras herramientas investigadas (más de cinco)
Fuentes oficiales verificadas 2026-11-04 (runwayml.com + help center, pika.art,
developers.googleblog.com + docs.cloud.google.com, lumalabs.ai/news, elevenlabs.io/pricing
y /pricing/api, heygen.com, synthesia.io, opus.pro, capcut.com, invideo.ai, fliki.ai,
pictory.ai, descript.com; precios también contrastados con reviews fechados 2026).

| Producto | Funciones relevantes | Pago |
|---|---|---|
| **Runway** Gen-4/4.5 | I2V (imagen obligatoria), T2V 16 s, References (consistencia), Camera Controls, Act-Two (transferencia de actuación, 5 cr/s), Motion Brush, Aleph (editar vídeo real), inpaint, extend, upscale 4K | Standard $15/mes (~625 cr); Gen-4 Turbo 5 cr/s; API $0.01/crédito |
| **Pika** 2.5 | T2V/I2V 5–10 s; **Pikaframes** (2–5 keyframes → hasta 25 s); Pikascenes (ingredientes → escena); Pikadditions/Pikaswaps (insertar/reemplazar elementos); Pikaformance (lip-sync); solo SFX (no música/voz) | desde $10/mes; 10–65 créditos por superficie |
| **Google Veo 3.1** | clips 4/6/8 s con **audio nativo** (diálogo/SFX), Ingredients to Video (hasta 3 imágenes de referencia), first/last frame, **extend** (>60 s encadenable), 9:16 nativo, insert/remove de objetos; SynthID invisible | API $0.40/s (720p–1080p), $0.60/s 4K; Fast $0.10–0.15/s; Lite $0.05/s |
| **Luma Ray3.2** (Dream Machine) | T2V/I2V, modify (V2V), extend, reframe, hasta 16 keyframes, tracking facial (8 caras), HDR/EXR, 20 s 1080p | Lite $9.99; Plus $29.99 con derechos comerciales |
| **ElevenLabs** | TTS v3/v4 (70+ idiomas, audio tags), clonación instantánea/profesional, **Dubbing Studio** (traducción con lip-sync, $0.33–0.50/min), Scribe v2 STT ($0.22/h), Sound Effects, Eleven Music ($0.15/min), Voice Isolator | gratis 10k créditos (sin licencia comercial); Starter $6/mes con licencia comercial; API $0.04–0.10 por 1k caracteres |
| **HeyGen / Synthesia** | avatares (500+ stock en HeyGen), lip-sync, traducción/localización con redoblado sincronizado | HeyGen Creator $29/mes; Synthesia Starter $29/mes (120 min/año) |
| **InVideo / Fliki / Pictory** | brief/URL/blog → vídeo narrado con stock, voz y plantillas; Fliki $28 Basic/$66 Standard | de pago por créditos/minutos |
| **CapCut** | edición, auto-captions y **auto-reframe a 9:16/1:1/16:9** | gratis; Pro $7.99/mes |
| **Opus Clip / Descript** | long→shorts (Opus desde $15/mes); edición por transcripción, Overdub, green screen (Descript desde ~$16/mes) | de pago |
| **ClipLoft** | **lote de variantes 9:16 de anuncio UGC desde un brief**, exportación a specs de Meta/TikTok | Pro $99/mes (~$3.52/vídeo a volumen) |

---

## 2. Matriz de capacidades comerciales → alternativas abiertas (por función)

Licencias verificadas el 2026-11-04 en los repositorios/model cards citados. Se
distingue **código / pesos / datos**.

| Capacidad comercial (producto) | Mejor alternativa open source | Licencias (código / pesos / datos) | Hardware real | ¿Integrada ya en ComfyUIPod? |
|---|---|---|---|---|
| Keyframes / first-last frame / FLF2V (Kling 4.0, Pika Pikaframes, Veo 3.1) | **Wan 2.2 FLF2V** | Apache-2.0 / Apache-2.0 / n/d público | 14B MoE: ~16–24 GB VRAM con FP8+offload | ✅ plantilla `wan_flf2v` |
| Referencias multi-elemento / consistencia de sujeto (Kling Omni Ref, Veo Ingredients, Runway References, Higgsfield Soul ID, Pika Pikascenes) | **Wan2.1-VACE R2V/MV2V**, IP-Adapter SDXL | Apache-2.0 / Apache-2.0 (pesos FaceID excluidos: NC) | igual que Wan base | ✅ `vace_*`, `sdxl_ipadapter`, `wan_animate` |
| Edición localizada / reemplazo de elementos / inpaint en vídeo (Higgsfield Inpaint, Pika Pikaswaps/Pikadditions, Runway Aleph, Kling Modify) | **VACE MV2V (swap/inpaint/expand)** | Apache-2.0 / Apache-2.0 | igual que Wan base | ✅ `vace_inpaint`, `vace_swap`, `vace_restyle` |
| Control de cámara (Higgsfield Cinema Studio, Runway Camera Controls, Kling camera control) | **VideoX-Fun Wan2.2-Fun-Control-Camera** (trayectorias) + CameraCtrl (AnimateDiff/SVD) | Apache-2.0 / Apache-2.0 (pesos `alibaba-pai/Wan2.2-Fun-A14B-Control-Camera`); CameraCtrl ⚠ verificar Apache-2.0 | Wan 14B + ~2 GB | ❌ no (requiere nodo/ modelo nuevo; ver §4) |
| Extensión de vídeo (Kling extend, Runway extend, Veo extend) | Wan FLF2V encadenado + ensamblador por ventanas (`edl/windowed.py`) | Apache-2.0 | según base | ✅ parcial (`assemble_long_video`, `wan_flf2v`) |
| Lip-sync / avatar por voz (Higgsfield Speak, Kling lipsync, HeyGen, OmniHuman) | **LatentSync** (ya en pod); Hallo3; EchoMimicV3 | Apache-2.0 / Apache-2.0 (Hallo3, EchoMimicV3: ⚠ LICENSE) | LatentSync ~6–8 GB; EchoMimicV3 12–16 GB | ✅ `latentsync` + motor `lipsync` |
| Música con voz/letra + retake de sección + remix (Suno v5.5, Studio) | **ACE-Step 1.5** (Retake = reemplazar sección; Flow-Edit/remix; letra en 50+ idiomas) | **MIT código y pesos** (v1 era Apache-2.0); model card HF: «You can strictly use the generated music for commercial purposes»; datos: licencias declaradas por el proyecto | <4 GB VRAM (base 3.5B); XL 4B ≥12 GB | ❌ no; el motor `music_gen` existe pero apunta a MusicGen/Stable Audio (ver §3 ⚠) |
| Stem separation (Suno stems) | Demucs (htdemucs) | código **MIT**, pero **pesos de licencia no clara** (MUSDB solo investigación + 800 canciones internas Meta; htdemucs_6s probablemente CC-NC) | CPU 5–10× más lento; GPU 4 GB+ | ❌ **excluido a propósito** (§3) |
| TTS / clonación de voz / STT (ElevenLabs) | Kokoro (TTS es, Apache-2.0, ya horneado), IndexTTS-2.5 (clonación zero-shot, ya en pod), faster-whisper (STT) | Apache-2.0 / Apache-2.0 (IndexTTS) / MIT | Kokoro CPU; IndexTTS ~5 GB | ✅ motores `audio`, `indextts`, `voice_clone` |
| Brief/producto → anuncio con variantes de formato (Higgsfield Marketing Studio/URL-to-video, ClipLoft, CapCut auto-reframe, InVideo/Fliki) | **composición propia**: guion determinista + storyboard JSON + montaje ffmpeg multi-plano + variantes de aspect + voz/música + QA | sin modelo nuevo (usa los ya integrados) | CPU (ffmpeg) + motores existentes | ✅ **NUEVO: motor `ad_studio`** (este PR) |
| Evaluación automática del resultado | **QA nivel 1 FFmpeg** (black/freeze/flicker/loudness) + **VBench nivel 2** en venv aislado | VBench: ver licencia del repo (MIT según proyecto; ya integrado) | CPU nivel 1; VBench según modelos | ✅ motor `qa` |
| Storyboard desde guion (Kling storyboard/multi-shot, Boords) | esquema JSON propio + puerta de aprobación (`storyboard_gate`) | n/a (código propio) | CPU | ✅ parcial (puerta); ✅ completo con `ad_studio` |

---

## 3. Criterio open source aplicado (licencias honestas)

Verificado por separado código / pesos / datos / restricciones comerciales:

- **Wan 2.1/2.2, VACE, VideoX-Fun, LatentSync, IndexTTS-2.5, Kokoro**: Apache-2.0
  (código y pesos). Ya dentro del pod vía manifiesto controlado.
- **ACE-Step 1.5** (`ace-step/ACE-Step-1.5`, HF `ACE-Step/Ace-Step1.5`): MIT en código
  y pesos de v1.5 (v1.3.5B era Apache-2.0), con grant explícito de uso comercial en la
  model card. Recomendado como sustituto compatible comercialmente del motor
  `music_gen` actual.
- ⚠ **MusicGen/AudioCraft (Meta)**: código MIT pero **pesos CC-BY-NC-4.0**
  (no comercial). El motor `music_gen` existente referencia MusicGen/Stable Audio:
  etiquetado aquí como **no comercial**; para uso comercial del producto se recomienda
  migrar a ACE-Step 1.5 (MIT) o Stable Audio Open (ver abajo).
- ⚠ **Stable Audio Open 1.0**: Stability AI Community License — comercial con registro
  bajo $1M de ingresos anuales; no es Apache/MIT puro.
- ⚠ **Demucs (Meta)**: código MIT, **pesos de licencia no clara** (MUSDB es solo
  investigación; htdemucs_6s probablemente CC-NC). **Excluido** de funciones activas
  del producto por licencia ambigua (regla del repo: no tratar como libre).
- **OmniHuman-1.5/2.0 (ByteDance)**: pesos cerrados; no se integran ni se clonan.
- **OmniGen2 (BAAI)**: código Apache-2.0; pesos en HF (licencia según model card —
  ⚠ verificar antes de uso comercial).
- **Hallo3 / EchoMimicV3**: repos abiertos (Apache-2.0 según README; ⚠ verificar LICENSE
  exacto). No se han horneado en este PR.
- No se añadió ningún componente con licencia ambigua como si fuera libre; no se
  descargaron modelos nuevos en este trabajo (los pesos nuevos, si se aprueban, entran
  por el descargador controlado `scripts/download-models.sh` + `config/models-studio.json`).

## 4. Selección: paquete integrado en este PR

Matriz priorizada (novedad, utilidad, calidad posible, compatibilidad, costo,
licencia, hardware):

| Oportunidad | Por qué / por qué no |
|---|---|
| **A. Estudio de anuncios (elegida)**: brief → guion → storyboard → variantes 9:16/1:1/16:9 con voz, música y QA automático | Combina con assets, EDL/ffmpeg, TTS, música y QA ya existentes; 100% CPU-verificable en CI; licencia limpia (sin modelos nuevos); resuelve el caso “brief/producto → anuncio con variantes” de Higgsfield Marketing Studio, ClipLoft y CapCut auto-reframe |
| B. Control de cámara (VideoX-Fun Wan-Fun-Camera) | Alta calidad, Apache-2.0, pero requiere nodo ComfyUI + pesos nuevos (~14B) y GPU para validar; queda para el worker de infraestructura (descarga controlada). No verificable aquí |
| C. Stem separation (Demucs) | Licencia de pesos ambigua/NC → descartada por el criterio del repo |
| D. Música con retake de sección (ACE-Step 1.5) | MIT y comercial, pero requiere pesos nuevos (~5–10 GB) vía manifiesto; pendiente del worker de infraestructura. Se documenta como sustituto NC de MusicGen |
| E. Avatar avanzado (Hallo3/EchoMimicV3) | Ya existe lipsync (LatentSync) en el pod; añadir otro avatar es duplicado hoy |

**Integrado: A** como motor `ad_studio` (tier WARM) con tres ops (`script`,
`storyboard`, `variants`), rutas `/studio/api/projects/{id}/ads/*`, tres herramientas
de agente (`ad_script`, `ad_storyboard`, `build_ad_variants`), vista «Anuncios» del
panel y QA por variante con reintento acotado. Detalle de uso:
[`../estudio_anuncios.md`](../estudio_anuncios.md).

## 5. Diseño de la alternativa abierta integrada

- `comfy_studio/ads/schema.py`: `AdBrief` → `AdScript` (composer **determinista**, 4
  escenas hook/problema/solución/CTA, duración cuantizada) → `AdStoryboard` (planos con
  prompt visual, texto en pantalla, línea de voz y asset asignado round-robin).
- `comfy_studio/ads/pipeline.py`: `run_ad_variants` monta cada aspect ratio con un
  único `ffmpeg` (scale-cover + crop centrado + concat + `amix` de voz/música
  opcionales), verifica con `ffprobe` (duración y vídeo presentes), registra el MP4
  como asset `outputs`, ejecuta el motor `qa` por variante, publica progreso SSE y
  respeta la cancelación sticky del job entre variantes. Persistencia en
  `project_meta` (`ads:index`, `ad:<id>`).
- `comfy_studio/engines/ad_studio.py`: contrato de 4 verbos; stub
  `STUDIO_STUB_AD_STUDIO=1` (artefactos mínimos, `simulated: true`).
- Sin ffmpeg ni stub: fallo tipado (no finge).

## 6. Hardware, costos y limitaciones

- **CPU suficiente** para variantes con ffmpeg (RTX 5090/4090 no necesarias para el
  montaje); voz (Kokoro, CPU) y música (music_gen) usan los motores ya horneados; QA
  nivel 1 es CPU (FFmpeg) y el nivel 2 (VBench) queda en su venv aislado opcional.
- Coste: $0 en licencias y $0 en servicios externos; todo local.
- Limitaciones honestas: el guion es determinista por plantilla (el LLM puede refinarlo
  por chat); la generación de planos (Wan/VACE) sigue siendo opcional vía `enqueue_render`
  y no se ejecuta dentro de `variants` (montaje de assets existentes); la validación en
  GPU real de QA nivel 2 y de TTS/música queda pendiente de un pod con pesos (aquí no hay
  GPU ni pesos descargados); el test de render real con ffmpeg se ejecuta solo si
  `ffmpeg` está en el PATH (en CI de la imagen sí lo está).

## 7. Validación ejecutada (resultados exactos, 2026-11-04)

```
ruff check studio panel scripts config/probes        -> All checks passed!
pytest studio/tests -q                               -> 602 passed, 23 failed, 10 skipped
python -m py_compile config/probes/*.py scripts/check_models_manifest.py -> OK
python -m comfy_studio.templates                     -> OK: 17 plantillas validadas
python scripts/check_models_manifest.py --fixture --offline -> OK [fixture]: 4/4
```

- Los **23 fallos son preexistentes en este sandbox** (faltan `sqlite-vec` y `ffmpeg`;
  fallan igual en `HEAD` limpio sin este PR — comprobado con `git stash`).
- De los tests nuevos (`tests/test_m7_ad_studio.py`, 24 tests): **23 pasan y 1 se salta**
  (`test_run_variants_real_ffmpeg_renders_and_verifies`, requiere `ffmpeg`/`ffprobe`;
  en la imagen de producción, donde sí existen, valida render real 1280x720, duración
  verificada con ffprobe y QA). La ruta, el contrato, el stub, la cancelación, el
  tool-belt y la persistencia están cubiertos por pruebas deterministas ejecutadas aquí.
- Calidad medible: cada variante queda con `qa.passed` (criterios `max_black_s`/`max_freeze_s`)
  y `simulated`; el criterio de aceptación por defecto es `max_black_s=0.5`,
  `max_freeze_s=1.0` (nivel 1 FFmpeg), sobre la línea base de “sin QA”.
