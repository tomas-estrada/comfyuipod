# Registro de Plantillas de Comfy Studio (`templates-registry`)

Este documento describe las **9 plantillas curadas** (hitos M2 + ampliaciones) incluidas en [`studio/comfy_studio/templates/data/`](../studio/comfy_studio/templates/data/), sus parámetros tipados, los valores por defecto de rendimiento en una **RTX 5090 (32 GB GDDR7)**, el validador estructural y los endpoints HTTP expuestos por `studio-server` (`127.0.0.1:18190`).

---

## 1. Filosofía y Decisiones de Rendimiento (RTX 5090 32 GB)

El registro de plantillas encapsula workflows en **formato API de ComfyUI** (`prompt` graph JSON) listos para producción sin exponer el grafo de nodos a la interfaz de usuario ni al agente conversacional:

1. **Wan 2.2 Fun VACE 14B (`fp8_scaled`) en `480p` + `offload: true`**:
   - Los módulos `wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors` y `wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors` ocupan `32.2 GiB` en disco frente a los `64.6 GiB` de la variante `bf16` (que desbordaría los 32 GB de VRAM de la RTX 5090).
   - Se fija resolución base **`832x480` (`480p` 16:9)**, **`81` fotogramas** (~5 s a 16 fps) y **`offload: true`** para mantener margen térmico y de memoria al encadenar ambos expertos MoE (`high_noise` + `low_noise`).
2. **Wan 2.2 14B MoE + LoRA LightX2V 4-step (`steps=4`, `cfg=1.0`)**:
   - Las plantillas de generación de vídeo (`wan_flf2v`, `wan_i2v`, `wan_t2v`) emplean los modelos **Wan 2.2 14B `fp8_scaled`** combinados con los LoRAs de destilación **LightX2V 4-step** (`high_noise` pasos `0→2`, `low_noise` pasos `2→4`, `cfg=1.0`, `euler` + `simple`), mientras que las plantillas de edición `vace_inpaint` y `vace_restyle` ejecutan los expertos dedicados **Wan 2.2 Fun VACE 14B `fp8_scaled`** en `2+2` pasos (`uni_pc` + `simple`).
   - `wan_t2v_5b` usa el modelo híbrido **Wan 2.2 TI2V 5B `fp16`** con la **LoRA Turbo rank-64** como motor ligero para borradores rápidos a **720p (1280x720) y 24 fps** (cadencia nativa del 5B) con `KSampler` de etapa única; no es el motor principal de producción (sin MoE de doble experto), pero cabe en ~10 GiB de VRAM.
3. **Generación de imagen base con `Juggernaut-XI-byRunDiffusion.safetensors` (`sdxl_image`)**:
   - Para keyframes, storyboards y referencias visuales (que después alimentan `wan_i2v`, `wan_flf2v` o `vace_restyle`), se emplea SDXL Juggernaut XI a **`1024x1024`**, **`25` pasos**, **`cfg=6.0`** con sampler `dpmpp_2m_sde` + `karras`.
   - `sdxl_ipadapter` reutiliza ese mismo pipeline añadiendo **IP-Adapter SDXL ViT-H** para fijar la identidad (cara/personaje/objeto) de una imagen de referencia del proyecto.

---

## 2. Catálogo de Plantillas (M2)

### 2.1 `vace_inpaint` — VACE 14B Inpainting y Reemplazo por Máscara

- **Archivo**: [`studio/comfy_studio/templates/data/vace_inpaint.json`](../studio/comfy_studio/templates/data/vace_inpaint.json)
- **Uso**: Reemplazar o insertar objetos, vestuario o elementos escénicos dentro de un clip de vídeo existente usando una máscara espacial/temporal (`mask_input` / `mask_asset_id`), preservando el movimiento original y la iluminación fuera de la máscara.
- **Modelos requeridos**:
  - `wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors`
  - `wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan_2.1_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción visual del objeto o cambio a introducir en la zona enmascarada.
  - `video_asset_id` / `video_input` (*string*): asset de vídeo base del proyecto (`VHS_LoadVideo`).
  - `mask_asset_id` / `mask_input` (*string*): asset de máscara en blanco y negro (`LoadImage` → `RepeatImageBatch` con `amount={{frames}}` → `ImageToMask` leyendo el canal RGB `"red"` hacia `control_masks` de `WanVaceToVideo`, garantizando la cobertura temporal de todos los fotogramas sin caer en el padding blanco `1.0`).
  - `strength` (*number*, `0.0–1.0`): intensidad del condicionamiento VACE (`WanVaceToVideo`).
- **Defaults de rendimiento**: `832x480` (`480p`), `81` frames (`16` fps), `steps=4` (`2+2`), `cfg=1.0`, `strength=0.85`, `offload=true`, `fp8_scaled`.

---

### 2.2 `vace_restyle` — VACE 14B Restyle y Transferencia de Estilo

- **Archivo**: [`studio/comfy_studio/templates/data/vace_restyle.json`](../studio/comfy_studio/templates/data/vace_restyle.json)
- **Uso**: Transformar la estética, iluminación o acabado visual de un clip de vídeo completo conservando la coreografía, geometría y movimiento de cámara originales, guiado por prompt y una imagen de referencia conectada a `reference_image` en `WanVaceToVideo`.
- **Modelos requeridos**:
  - `wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors`
  - `wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan_2.1_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción del nuevo estilo visual, atmósfera o iluminación.
  - `video_asset_id` / `video_input` (*string*): vídeo fuente cuyo movimiento se preserva (`control_video`).
  - `image_asset_id` / `image_input` (*string*): imagen de referencia estilística conectada a `reference_image` en `WanVaceToVideo`.
  - `strength` (*number*, `0.0–1.0`): peso de adherencia a la estructura del vídeo base (`0.85` por defecto).
- **Defaults de rendimiento**: `832x480` (`480p`), `81` frames (`16` fps), `steps=4`, `cfg=1.0`, `strength=0.85`, `offload=true`, `fp8_scaled`.

---

### 2.3 `vace_swap` — VACE 14B Intercambio de Personaje con Identidad de Referencia

- **Archivo**: [`studio/comfy_studio/templates/data/vace_swap.json`](../studio/comfy_studio/templates/data/vace_swap.json)
- **Uso**: Sustituir al personaje u objeto de un vídeo base por el sujeto concreto de una imagen de referencia (por ejemplo, un niño o un coche específicos) conservando el movimiento, la cámara y el fondo originales. Conecta la imagen de referencia a `reference_image` y una máscara opcional (repetida temporalmente con `RepeatImageBatch` y convertida con `ImageToMask`) a `control_masks` en `WanVaceToVideo` para delimitar la zona editable.
- **Modelos requeridos**:
  - `wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors`
  - `wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan_2.1_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción del sujeto de referencia y del resultado del intercambio.
  - `video_asset_id` / `video_input` (*string*): vídeo fuente cuyo movimiento y escena se preservan (`control_video`).
  - `image_asset_id` / `image_input` (*string*): foto del sujeto concreto a insertar, conectada a `reference_image` en `WanVaceToVideo`.
  - `mask_asset_id` / `mask_input` (*string*, opcional): máscara en blanco y negro que delimita la región editable (`control_masks`).
  - `strength` (*number*, `0.0`–`1.0`): fidelidad de la identidad respecto al vídeo base (`0.95` por defecto).
- **Defaults de rendimiento**: `832x480` (`480p`), `81` frames (`16` fps), `steps=4`, `cfg=1.0`, `strength=0.95`, `offload=true`, `fp8_scaled`.

---

### 2.4 `wan_flf2v` — Wan 2.2 14B First-Last-Frame to Video (FLF2V)

- **Archivo**: [`studio/comfy_studio/templates/data/wan_flf2v.json`](../studio/comfy_studio/templates/data/wan_flf2v.json)
- **Uso**: Generar una transición de vídeo continua y coherente entre un fotograma inicial (`first_frame`) y un fotograma final (`last_frame`) mediante el nodo nativo `WanFirstLastFrameToVideo`. Ideal para unir cortes del montaje o interpolar entre dos keyframes generados con `sdxl_image`.
- **Modelos requeridos**:
  - `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors` + `wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors`
  - `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` + `wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `clip_vision_h.safetensors`, `wan_2.1_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción del movimiento y transición entre ambos extremos.
  - `first_frame_asset_id` / `first_frame_input` (*string*): imagen del primer fotograma.
  - `last_frame_asset_id` / `last_frame_input` (*string*): imagen del último fotograma.
- **Defaults de rendimiento**: `832x480` (`480p`), `81` frames (`16` fps), `steps=4`, `cfg=1.0`, `offload=true`, `fp8_scaled`.

---

### 2.5 `wan_i2v` — Wan 2.2 14B Image-to-Video (I2V Lightning 4-step)

- **Archivo**: [`studio/comfy_studio/templates/data/wan_i2v.json`](../studio/comfy_studio/templates/data/wan_i2v.json)
- **Uso**: Animar un fotograma clave o retrato estático (`image_asset_id` / `image_input`) convirtiéndolo en un plano de vídeo de 5 segundos con preservación de identidad y composición mediante `WanImageToVideo` y `CLIPVisionEncode`.
- **Modelos requeridos**:
  - `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors` + `wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors`
  - `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` + `wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `clip_vision_h.safetensors`, `wan_2.1_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): acción de los sujetos y movimiento de cámara a partir de la imagen inicial.
  - `image_asset_id` / `image_input` (*string*): imagen inicial del plano.
- **Defaults de rendimiento**: `832x480` (`480p`), `81` frames (`16` fps), `steps=4`, `cfg=1.0`, `offload=true`, `fp8_scaled`.

---

### 2.6 `wan_t2v_5b` — Wan 2.2 TI2V 5B Texto a Video Ligero (720p Turbo 4-paso, 24 fps)

- **Archivo**: [`studio/comfy_studio/templates/data/wan_t2v_5b.json`](../studio/comfy_studio/templates/data/wan_t2v_5b.json)
- **Uso**: Borradores rápidos, previews y planos a 720p cuando se itera deprisa o la VRAM es justa: un único modelo TI2V 5B fp16 (~10 GiB) con LoRA Turbo rank-64 en `KSampler` de etapa única (sin par MoE high/low noise).
- **Modelos requeridos**:
  - `wan2.2_ti2v_5B_fp16.safetensors` + `Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan2.2_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción cinematográfica del plano.
  - `frames` (*integer*, `97` por defecto): longitud a 24 fps (cadencia nativa del 5B).
  - `lora_strength` (*number*, `1.0` por defecto): intensidad de la LoRA Turbo.
- **Defaults de rendimiento**: `1280x720` (`720p`), `97` frames (`24` fps), `steps=4`, `cfg=5.0`, sampler `euler` + `simple`.

---

### 2.7 `wan_t2v` — Wan 2.2 14B Text-to-Video (T2V Lightning 4-step)

- **Archivo**: [`studio/comfy_studio/templates/data/wan_t2v.json`](../studio/comfy_studio/templates/data/wan_t2v.json)
- **Uso**: Generar planos de vídeo B-roll, secuencias de establecimiento o escenas completas directamente desde texto (`EmptyHunyuanLatentVideo` + doble `KSamplerAdvanced` MoE).
- **Modelos requeridos**:
  - `wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors` + `wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors`
  - `wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors` + `wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors`
  - `umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `wan_2.1_vae.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción cinematográfica de la escena, sujeto, iluminación y cámara.
  - `negative_prompt` (*string*, opcional): artefactos a evitar.
- **Defaults de rendimiento**: `832x480` (`480p`), `81` frames (`16` fps), `steps=4`, `cfg=1.0`, `offload=true`, `fp8_scaled`.

---

### 2.8 `sdxl_image` — SDXL Juggernaut XI Generación de Imagen y Keyframes

- **Archivo**: [`studio/comfy_studio/templates/data/sdxl_image.json`](../studio/comfy_studio/templates/data/sdxl_image.json)
- **Uso**: Generar imágenes fotorrealistas de alta resolución (`1024x1024`), storyboards, personajes de referencia o primeros/últimos fotogramas para nutrir `wan_i2v`, `wan_flf2v` y `vace_restyle`.
- **Modelos requeridos**:
  - `Juggernaut-XI-byRunDiffusion.safetensors`
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): descripción detallada de la imagen.
  - `negative_prompt` (*string*, opcional): defectos anatómicos o estilísticos a evitar.
  - `width`, `height` (*integer*): resolución (`1024x1024` por defecto).
  - `steps` (*integer*): pasos de muestreo (`25` por defecto).
  - `cfg` (*number*): escala CFG (`6.0` por defecto).
- **Defaults de rendimiento**: `1024x1024`, `steps=25`, `cfg=6.0`, sampler `dpmpp_2m_sde` + scheduler `karras`.

---

### 2.9 `sdxl_ipadapter` — SDXL Juggernaut XI + IP-Adapter (Identidad Fijada por Referencia)

- **Archivo**: [`studio/comfy_studio/templates/data/sdxl_ipadapter.json`](../studio/comfy_studio/templates/data/sdxl_ipadapter.json)
- **Uso**: Generar nuevas tomas o keyframes de un mismo personaje, mascota u objeto a partir de una foto de referencia del proyecto (`image_asset_id` / `image_input`), manteniendo la identidad visual con IP-Adapter SDXL ViT-H (`IPAdapterModelLoader` + `IPAdapterApply` sobre el modelo Juggernaut XI).
- **Modelos requeridos**:
  - `Juggernaut-XI-byRunDiffusion.safetensors`
  - `ip-adapter_sdxl_vit-h.safetensors`, `CLIP-ViT-H-14-laion2B-s32B-b79K` (grupo `ipadapter_sdxl` del perfil studio)
- **Entradas principales (`inputs`)**:
  - `prompt` (*string*, obligatorio): escena deseada; el sujeto es el de la referencia.
  - `image_asset_id` / `image_input` (*string*): foto de referencia con la identidad a conservar.
  - `ipadapter_weight` (*number*, `0.8` por defecto, rango `0.0`–`1.0`): `1.0` fija la cara al máximo, `0.3`–`0.5` solo guía el estilo.
- **Defaults de rendimiento**: `1024x1024`, `steps=25`, `cfg=6.0`, sampler `dpmpp_2m_sde` + `karras`, `ipadapter_weight=0.8`.

---

### 2.10 `hunyuan3d_image_to_3d` — Hunyuan3D 2.1 Imagen a Malla 3D (GLB) *(M3-D)*

- **Archivo**: [`studio/comfy_studio/templates/data/hunyuan3d_image_to_3d.json`](../studio/comfy_studio/templates/data/hunyuan3d_image_to_3d.json)
- **Uso**: Convertir cualquier imagen del proyecto (render `sdxl_image`, keyframe de vídeo o foto subida) en una malla 3D texturizable en formato `GLB`, lista para Blender/Unreal. Pipeline 100% nativo de comfy-core (`ImageOnlyCheckpointLoader` + `ModelSamplingAuraFlow` + `CLIPVisionEncode` + `Hunyuan3Dv2Conditioning` + `KSampler` + `VAEDecodeHunyuan3D` + `VoxelToMesh` + `SaveGLB`), sin custom nodes.
- **Modelos requeridos**:
  - `hunyuan_3d_v2.1.safetensors` (repo `Comfy-Org/Hunyuan-3D-2.1`, licencia Tencent Hunyuan Community)
- **Entradas principales (`inputs`)**:
  - `image_asset_id` (*string*, obligatorio): asset de imagen de partida en el proyecto. Fondo limpio y sujeto centrado producen mejores mallas.
  - `steps` (*integer*): pasos de muestreo del flujo (`30` por defecto; `20` acelera con poca pérdida).
  - `cfg` (*number*): escala CFG (`5.0` por defecto).
  - `octree_resolution` (*integer*): resolución del voxelado (`256` por defecto).
- **Defaults de rendimiento**: `precision=fp16`, `latent_resolution=4096`, `octree_resolution=256`, `steps=30`, `cfg=5.0`, `offload=false`.
- **Salida**: `output_kind=model3d`; el `GLB` queda registrado en la galería del proyecto (`outputs/`).
- **Disponibilidad (gating por pesos)**: solo se descarga con `STUDIO_INCLUDE_3D=1` o `MODEL_PROFILE=unreal`. Sin esos pesos, `GET /studio/api/templates` **no la ofrece**, `GET /studio/api/templates/hunyuan3d_image_to_3d` la devuelve con `available=false` y `unavailable_reason`, y `POST .../submit` responde `422` con el mensaje que indica cómo descargarlos (nunca falla en silencio).

---

## 3. Validador Estructural (`comfy_studio.templates.validator`)

El módulo [`studio/comfy_studio/templates/validator.py`](../studio/comfy_studio/templates/validator.py) verifica todas las plantillas al arrancar `TemplateRegistry` y puede ejecutarse desde CLI o CI:

```bash
python -m comfy_studio.templates
```

### Reglas verificadas por plantilla

1. **Metadatos y documentación en español**: presencia de `id`, `name`, `category`, `output_kind`, `performance_defaults`, `parameters` y `docs` (`uso`, `inputs`, `defaults`, `notas_rendimiento`).
2. **Defaults de rendimiento obligatorios**:
   - `vace_*` y `wan_*` 14B: `height == 480`, `width == 832`, `steps == 4`, `offload == True`, `weight_dtype == "fp8_scaled"`.
   - `wan_t2v_5b`: `width == 1280`, `height == 720`, `steps == 4`, `fp16` + LoRA Turbo.
   - `sdxl_image` y `sdxl_ipadapter`: `width == 1024`, `height == 1024`, `steps == 25`.
3. **Integridad del grafo ComfyUI API-format y contratos pinneados**:
   - Todos los `class_type` pertenecen al catálogo `KNOWN_NODE_SPECS`, declaran todas sus entradas obligatorias (`required_inputs`, incluyendo `pingpong` en `VHS_VideoCombine`) sin valores `null` desconectados y rechazan cualquier entrada no declarada en `required_inputs ∪ optional_inputs` del nodo pinneado (p. ej. `clip_vision_output` o `control_mask` singular en `WanVaceToVideo`, u `offload` en `KSamplerAdvanced`).
   - En `WanVaceToVideo`, `control_masks` no puede alimentarse directamente del slot 1 (canal alfa) de `LoadImage` ni de una máscara espacial única; la imagen cargada por `LoadImage` debe repetirse a la longitud temporal solicitada mediante `RepeatImageBatch` (`amount == length`) antes de extraer el canal RGB con `ImageToMask` (evitando que `WanVaceToVideo` rellene los frames restantes con `1.0` blanco/editable).
   - Todo enlace `["node_id", slot_idx]` apunta a un nodo existente en el mismo workflow con índice de salida entero `>= 0`.
   - Ausencia de ciclos dirigidos (DAG válido).
   - Existencia de al menos un nodo terminal de salida (`VHS_VideoCombine` o `SaveImage`) cuyo `filename_prefix` contiene `{{output_prefix}}` o `{{project_id}}`.
4. **Correspondencia de placeholders `{{param}}`**:
   - Todo placeholder usado en el grafo está declarado en `parameters` (o en el contexto interno `{project_id, job_id, template_id, output_prefix}`).

---

## 4. Endpoints HTTP y Galería de Salidas

| Método y Ruta | Descripción |
|---|---|
| `GET /studio/api/templates` | Lista las 9 plantillas con metadatos, `performance_defaults`, `parameters`, `params_schema` (JSON Schema) y `docs` en español. Acepta `?include_workflow=true`. |
| `GET /studio/api/templates/{template_id}` | Devuelve el detalle completo de una plantilla incluyendo su grafo `workflow` en formato API (`404` si no existe). |
| `POST /studio/api/templates/{template_id}/submit` | Valida `params` contra el schema de la plantilla (`422` con detalle si hay campos inválidos, fuera de rango o desconocidos, sin encolar en ComfyUI), sube a ComfyUI los `*_asset_id` referenciados del proyecto, crea el job en `studio.db`, ejecuta el workflow contra ComfyUI, descarga las salidas vía `/view` y las registra en la galería del proyecto (`category="outputs"`). |
| `GET /studio/api/projects/{project_id}/gallery` | Lista los assets generados en la galería del proyecto enriquecidos con `preview_url`, `download_url` y `view_url`. |
| `GET /studio/api/view` | Sirve previsualización inline o descarga (`?download=1` → `Content-Disposition: attachment`) de un asset de galería (`project_id` + `asset_id`) o salida de ComfyUI (`filename`). |
