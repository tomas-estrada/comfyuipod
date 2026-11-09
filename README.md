# ComfyUIPod — ComfyUI + Comfy Studio para Vast.ai (RTX PRO 6000 Blackwell, 96 GB)

ComfyUI y Comfy Studio para un pod Linux de un solo usuario, con Vast.ai como ruta de despliegue documentada. El perfil de hardware predeterminado es una **NVIDIA RTX PRO 6000 Blackwell (96 GB de VRAM)** con **128 GB de RAM de host**; también existe la variante de 256 GB y se conserva un perfil heredado de GPU de consumo (~32 GB). El perfil es el objetivo, no prueba de que todos los workflows o modelos estén validados en GPU. La interfaz web funciona desde Windows, macOS o Linux; el helper Bash es opcional y funciona en macOS/Linux o WSL2.

> Empieza por el [índice de documentación](docs/README.md) y la [guía para contribuir](CONTRIBUTING.md). La [política de recursos](docs/POLITICA_ARQUITECTURA_RECURSOS.md) y `studio/comfy_studio/hardware_profile.py` definen el hardware y el espacio actuales; la [guía de despliegue](docs/GUIA_PASO_A_PRODUCCION.md) explica cómo ponerlo en marcha.

> **Plataformas:** la imagen de producción corre en Linux; el panel se usa desde un navegador en Windows, macOS o Linux. El helper `tools/mac/comfy` es opcional, requiere una shell Bash compatible y se soporta en macOS/Linux (en Windows, mediante WSL2); las instrucciones de navegador no requieren ese helper.

---

## 1. Características Principales

1. **Objetivo actual: RTX PRO 6000 Blackwell (96 GB de VRAM), 128/256 GB de RAM de host**; el perfil de consumo de ~32 GB se conserva por compatibilidad, no como base de la arquitectura.
   - Basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` con la terna **PyTorch `2.9.1+cu128`** fijada y **`onnxruntime-gpu`** + **`opencv-python-headless`** protegidos mediante [`config/constraints.txt`](config/constraints.txt) y [`config/nodes.lock`](config/nodes.lock), más registro de librerías CUDA/cuDNN en `ldconfig`.
   - Conserva `build-essential` (`gcc`/`g++`) y `python3-dev` en la imagen para que `torch.compile` (Triton) y los custom nodes con extensiones C/C++ instalados desde ComfyUI-Manager funcionen en tiempo de ejecución.
2. **Separación segura `/opt/ComfyUI` (código) y `/workspace/comfyui-data` (datos)**:
   - ComfyUI y los 12 custom nodes están instalados en `/opt/ComfyUI`, mientras que `models/` (incluyendo `models/configs/*.yaml`), `input/`, `output/` y `user/` residen en `/workspace/comfyui-data`. Si montas un volumen persistente en `/workspace` en Vast.ai, **no se oculta la instalación** y los modelos descargados se reutilizan entre sesiones.
3. **Acceso directo por navegador con HTTP Basic Auth, Portal `/home`, Comfy Studio (`/studio/`) y Observabilidad (`/status`, `/report.md`)**:
   - ComfyUI escucha únicamente en loopback (`127.0.0.1:18188`) y **Nginx** (`user www-data;`) protege el puerto público `8188` con usuario/contraseña (`COMFY_USER` y `COMFY_PASS`), pantalla de espera auto-recargable (`_starting.html`) durante las comprobaciones de arranque, soporte WebSockets (`/ws` sin rate-limit), subida de vídeos sin límite (`client_max_body_size 0;`), `/healthz` (`HEAD /system_stats` sin exponer metadatos), cabeceras de seguridad en el servidor y en todas las locations (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`), compresión `gzip` (CSS/JS/JSON/SVG, mínimo 1024 bytes, `Vary: Accept-Encoding`) y un stack completo de observabilidad en el mismo puerto:
      - **`/home`**: Portal de inicio rápido (`<= 40 KB`, cero CDN externo, contraste WCAG AA) con acceso en 1 clic a ComfyUI, panel `/status`, informe `/report.md`, autodiagnóstico `/diag`, logs y terminal web opcional.
      - **`/studio/` y `/studio/api/*`**: Estudio web de producción audiovisual (**Comfy Studio**, activo en `MODEL_PROFILE=studio` en `127.0.0.1:18190`) con gestión de proyectos y sesiones, ingesta de assets, 17 plantillas curadas de vídeo, imagen, 3D y lipsync (ver [`docs/plantillas.md`](docs/plantillas.md)), percepción visual (`ffmpeg` keyframes + índice vectorial en `sqlite-vec` `index.db` + índice facial con búsqueda combinada `texto ∩ persona`) y transcripción/diarización, agente conversacional multimodal con streaming SSE, ejecución paralela de herramientas, planificador persistente, memoria resumida y 54 herramientas (`enqueue_render`, `search_assets`, `propose_edl`, `extract_keyframes`, `index_media`, `find_shots_text`, `find_shots_face`, `transcribe`, `diarize`, `build_edl`, `list_templates`, `job_status`, `job_cancel`, `tag_asset`, `system_status`, `export_edl`, `export_subtitles`, `update_plan`, `delegate_role`, `download_model`, `list_review_pins`, `resolve_review_pin`, `ad_script`, `ad_storyboard`, `build_ad_variants`, `understand_project`, `create_creative_brief`, `update_creative_brief`, `review_output`, `produce_video`, `plan_long_render`, `assemble_long_video`, `generate_music`, `generate_speech`, `lipsync_video`, `upscale_asset`, `ab_compare_models`, `vision_check`, `register_identity`, `list_identities`, `list_engines`, `install_engine`, `list_capabilities`, `request_capability`, `local_llm_chat`), recuperación durable de jobs interrumpidos tras reinicio (`STUDIO_RESUME_JOBS`), webhooks salientes firmados con HMAC-SHA256 (`STUDIO_WEBHOOK_URL`/`STUDIO_WEBHOOK_SECRET`), galería con descarga directa y ejecutor determinista de EDL con `ffmpeg` (`16:9` y `9:16`)., y **estudio de anuncios** nativo (motor `ad_studio`: brief → guion → storyboard → variantes 9:16/1:1/16:9/4:5 con voz, música opcional y QA automático por variante; ver [`docs/estudio_anuncios.md`](docs/estudio_anuncios.md)). Sobre esa base se añaden: **brief creativo** versionado con comprensión multimodal y evidencia (`/studio/api/projects/{id}/brief/*`, herramientas `understand_project`, `create_creative_brief`, `update_creative_brief`, `review_output`), **montaje asistido desde brief** (`POST /studio/api/projects/{id}/montage/from-brief`, que devuelve un EDL `comfy_studio.edl.v1` con timecodes verificados por `ffprobe`), **pins de revisión** anclados a timecode del EDL (`/pins`), **producciones** con plan editable, aprobación humana, cancelación y progreso (`/productions`, expuestas también por MCP salvo `approve_plan`, que el agente nunca puede invocar), un **catálogo de 16 motores** (`comfyui`, `blender`, `editor`, `ffmpeg`, `heavy_video`, `audio`, `voice_clone`, `indextts`, `music_gen`, `lipsync`, `upscaler`, `capture3d`, `compositor`, `qa`, `roto`, `ad_studio`) con herramientas `list_engines`/`install_engine` y una capa de orquestación con checkpoints en SQLite.
        > **Modo de inferencia actual (importante)**: los backends ML de percepción/ASR operan en modo `auto` (`STUDIO_PERCEPTION_BACKEND`, ver tabla de variables abajo): sondean los pesos reales al arranque (`ml/backends.py`) y usan backend **real** si el probe pasa; si no, caen a la **simulación determinista local** y cada respuesta de API lo marca con `simulated: true` + `simulation_notice` (el panel muestra un aviso persistente). Con `STUDIO_PERCEPTION_BACKEND=real` no hay fallback: responde 503 si el probe falla. El índice CLIP del panel (`perception/clip_index.py`) sigue siendo el extractor determinista local etiquetado como tal. **No evalúes calidad de modelo con salidas marcadas `simulated: true`.**
      - **`/status` y `/status/api`**: Panel de estado en vivo y API JSON servidos por **`comfyctl`** (binario nativo en Rust en `127.0.0.1:18189`) con métricas de GPU/VRAM, RAM/Swap, disco, estado del arranque y de las adquisiciones de modelos (bootstrap y tareas), motor de triaje automático (17 patrones con causa y remediación) y visor de logs por pestañas.
      - **`/report.md`**: Informe Markdown autocontenido (hasta 400 KB, con secretos redactados automáticamente por `comfyctl`) diseñado para copiar y pegar directamente al agente IA para depuración sin acceso SSH.
4. **Supervisor en Rust (`comfyctl supervise`) y modo de arranque estricto (`STRICT_START=1`)**:
   - `comfyctl` valida el host antes de adquirir modelos e iniciar ComfyUI. Los mínimos genéricos de runtime (driver ≥ 570, VRAM ≥ 20 GB, RAM ≥ 16 GB) son distintos del perfil predeterminado del helper (96 GB de VRAM / 128 GB de RAM de host). S5 lee el gate de disco de `storage-requirement.json`, generado en la imagen (hoy 11 GB con bootstrap vacío); el volumen total por perfil se calcula aparte con `scripts/storage_requirement.py`. Studio añade el diagnóstico del perfil Blackwell. Ante un `FATAL` estricto, Nginx y el diagnóstico siguen disponibles durante `STRICT_FATAL_GRACE` (600 s); después el proceso sale con código 20.
   - Supervisa `main.py`, escribe atómicamente `/run/comfyui-supervisor.json`, clasifica salidas (`137` OOM, `134`/`139` crash nativo CUDA/C++, `1` excepción Python, `143` SIGTERM), resetea el contador tras `COMFY_HEALTHY_UPTIME` (`600s`) y propaga `SIGTERM` limpiamente a sus subprocesos.
5. **ComfyUI-Manager preinstalado y desbloqueado**:
   - Instalado desde `Comfy-Org/ComfyUI-Manager` con `security_level = weak` (seguro porque ComfyUI escucha solo en `127.0.0.1`) para instalar cualquier custom node o modelo faltante con **un clic** desde la UI.
6. **`ffmpeg` completo de sistema (`libx264` + `h264_nvenc`) y autodiagnóstico `comfyctl diag` con timeouts**:
   - Fija `VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg`, `VHS_USE_IMAGEIO_FFMPEG=0` y `NVIDIA_DRIVER_CAPABILITIES=all`; `comfyctl diag` comprueba GPU, PyTorch, ONNX Runtime y codecs antes de iniciar ComfyUI. Es una prueba básica de disponibilidad, no una validación de calidad de todas las recetas.
7. **Catálogo y adquisición de modelos**: los manifiestos organizan los pesos por perfil y conservan las fases como grupos de inventario. El arranque normal ejecuta `--bootstrap`, que solo adquiere los activos marcados `bootstrap`; hoy esa clase está vacía y no se descargan pesos al iniciar. ComfyUI y el portal pueden arrancar sin bajar el catálogo. Los pesos de tarea se adquieren cuando se solicitan desde capacidades de Studio o herramientas de modelos. Los modos `--phase1/2/3/all` siguen disponibles para operadores que quieran una descarga masiva explícita. Las tablas siguientes describen el inventario, no lo que se descarga automáticamente al arrancar.

---

## 2. Catálogo de modelos y custom nodes (63.35 GiB de inventario core)

> Los tamaños y grupos de fase describen el catálogo disponible. El arranque actual descarga cero modelos por defecto; los pesos de tarea se adquieren bajo demanda. El total del catálogo no equivale a una descarga obligatoria ni al espacio mínimo para iniciar.

### Fase 1 — Modelos críticos ligeros (2.79 GiB)

| Categoría | Archivo en ComfyUI | Tamaño real | Repo HuggingFace (`Gated: False`) | Carpeta destino |
|---|---|---:|---|---|
| **InsightFace Pack *(licencia NC)*** | `buffalo_l` (`det_10g.onnx`, `w600k_r50.onnx`, etc.) | 0.27 GiB | `Gourieff/ReActor` (`dataset`) | `models/insightface/models/buffalo_l/` |
| **FaceSwap (ReActor)** | `inswapper_128.onnx` | 0.52 GiB | `Gourieff/ReActor` (`dataset`) | `models/insightface/` |
| **Restauración Facial** | `codeformer-v0.1.0.pth` | 0.35 GiB | `Gourieff/ReActor` (`dataset`) | `models/facerestore_models/` |
| **Restauración Facial** | `GFPGANv1.4.pth` | 0.32 GiB | `Gourieff/ReActor` (`dataset`) | `models/facerestore_models/` |
| **Detección Facial (`facexlib`)** | `detection_Resnet50_Final.pth` | 0.10 GiB | `kg-09/face_detection_models` | `models/facedetection/` |
| **Parsing Facial (`facexlib`)** | `parsing_parsenet.pth` | 0.08 GiB | `kg-09/face_detection_models` | `models/facedetection/` |
| **YOLO Face (Impact-Pack)** | `face_yolov8m.pt` | 0.05 GiB | `Gourieff/ReActor` (`dataset`) | `models/ultralytics/bbox/` |
| **Segment Anything (SAM)** | `sam_vit_b_01ec64.pth` | 0.35 GiB | `Gourieff/ReActor` (`dataset`) | `models/sams/` |
| **VAE SDXL (FP16 Fix)** | `sdxl.vae.safetensors` | 0.31 GiB | `madebyollin/sdxl-vae-fp16-fix` | `models/vae/` |
| **VAE SD 1.5** | `vae-ft-mse-840000-ema-pruned.safetensors` | 0.31 GiB | `stabilityai/sd-vae-ft-mse-original` | `models/vae/` |
| **Upscaler General** | `4x-UltraSharp.pth` | 0.06 GiB | `Kim2091/UltraSharp` | `models/upscale_models/` |
| **Upscaler Anime** | `4x-AnimeSharp.pth` | 0.06 GiB | `Kim2091/AnimeSharp` | `models/upscale_models/` |

### Fase 2 — Checkpoints, ControlNet e IP-Adapter (60.56 GiB en perfil `all`)

| Categoría | Archivo en ComfyUI | Tamaño real | Perfiles (`MODEL_PROFILE`) | Repo HuggingFace | Carpeta destino |
|---|---|---:|---|---|---|
| **General / Fotorrealismo SDXL** | `Juggernaut-XI-byRunDiffusion.safetensors` | 6.62 GiB | `all`, `general`, `realistic`, `studio` | `RunDiffusion/Juggernaut-XI-v11` | `models/checkpoints/` |
| **Fotorrealismo SDXL** | `RealVisXL_V5.0_fp16.safetensors` | 6.46 GiB | `all`, `realistic` | `SG161222/RealVisXL_V5.0` | `models/checkpoints/` |
| **General / Vídeo SD 1.5** | `Realistic_Vision_V6.0_NV_B1_fp16.safetensors` | 1.99 GiB | `all`, `general`, `realistic` | `SG161222/Realistic_Vision_V6.0_B1_noVAE` | `models/checkpoints/` |
| **Ilustración / Anime SDXL** | `waiIllustriousSDXL_v160.safetensors` | 6.46 GiB | `all`, `general`, `anime` | `Ine007/waiIllustriousSDXL_v160` | `models/checkpoints/` |
| **Ilustración / Anime SDXL** | `Illustrious-XL-v2.0.safetensors` | 6.46 GiB | `all`, `anime` | `OnomaAIResearch/Illustrious-XL-v2.0` | `models/checkpoints/` |
| **Ilustración / Anime SDXL** | `NoobAI-XL-v1.1.safetensors` | 6.62 GiB | `all`, `anime` | `Laxhar/noobai-XL-1.1` | `models/checkpoints/` |
| **Next-Gen SOTA (FP8)** | `flux1-dev-fp8.safetensors` | 16.06 GiB | `all` | `Comfy-Org/flux1-dev` | `models/checkpoints/` |
| **ControlNet Union ProMax** | `controlnet-union-sdxl-promax.safetensors` | 2.34 GiB | `all`, `general`, `realistic`, `anime`, `studio` | `xinsir/controlnet-union-sdxl-1.0` | `models/controlnet/` |
| **IP-Adapter SDXL (x2)** | `ip-adapter-plus_sdxl_vit-h.safetensors`, `ip-adapter-plus-face_sdxl_vit-h.safetensors` | 1.58 GiB | `all`, `general`, `realistic`, `anime`, `studio` | `h94/IP-Adapter` | `models/ipadapter/` |
| **IP-Adapter SD 1.5 (x2)** | `ip-adapter-plus_sd15.safetensors`, `ip-adapter-plus-face_sd15.safetensors` | 0.18 GiB | `all`, `general`, `realistic`, `anime`, `studio` | `h94/IP-Adapter` | `models/ipadapter/` |
| **CLIP Vision ViT-H** | `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | 2.35 GiB | `all`, `general`, `realistic`, `anime`, `studio` | `h94/IP-Adapter` | `models/clip_vision/` |
| **CLIP Vision ViT-bigG** | `CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors` | 3.44 GiB | `all`, `general`, `realistic`, `anime`, `studio` | `h94/IP-Adapter` | `models/clip_vision/` |

### Fase 3 — Inventario de Estudio de Vídeo y Voz (`MODEL_PROFILE=studio`, [`config/models-studio.json`](config/models-studio.json))

Todos los tamaños han sido verificados contra la cabecera `x-linked-size` de HuggingFace y se validan en cada ejecución de CI mediante [`scripts/check_models_manifest.py`](scripts/check_models_manifest.py) sin descargar los pesos.

| Categoría | Archivos | Tamaño exacto (bytes) | Tamaño (GB / GiB) | Licencia | Destino |
|---|---:|---:|---:|---|---|
| **`wan_core`** (Wan 2.2 `umt5-xxl fp8`, VAEs 2.1/2.2, `ti2v_5B_fp16`, pares `t2v`/`i2v` 14B fp8 high/low, `s2v` 14B fp8_scaled, LoRAs Lightning 4-step, Relight LoRA, `clip_vision_h`, encoder de audio `wav2vec2` ES, `Comfy-Org/Wan-2.2` y `Comfy-Org/Wan-2.1`) | 16 | `100,841,841,876` | 100.84 GB / 93.92 GiB | Apache-2.0 | `models/{text_encoders,vae,diffusion_models,loras,clip_vision,audio_encoders}/` |
| **`vace_fp8`** (Wan 2.2 Fun VACE 14B `high_noise` + `low_noise` fp8_scaled, `Comfy-Org/Wan-2.2`) | 2 | `34,692,112,208` | 34.69 GB / 32.31 GiB | Apache-2.0 | `models/diffusion_models/` |
| **`lora_turbo_5b`** (`Wan2.2-TI2V-5B-Turbo-lora.safetensors`, `Kijai/WanVideo_comfy`) | 1 | `332,348,584` | 0.33 GB / 0.31 GiB | Apache-2.0 | `models/loras/` |
| **`latentsync`** (LatentSync 1.6 UNet + SyncNet + Whisper tiny + `sfd_face` + VAE SD-FT-MSE, `ByteDance/LatentSync-1.6`) | 12 | `9,970,426,719` | 9.97 GB / 9.29 GiB | `openrail++` / MIT | `custom_nodes/ComfyUI-LatentSyncWrapper/checkpoints/` |
| **`liveportrait`** (`Kijai/LivePortrait_safetensors`: appearance, motion, warping, spade, stitching, landmark) | 6 | `635,366,851` | 0.64 GB / 0.59 GiB | MIT | `models/liveportrait/` |
| **`indextts`** (IndexTTS-2.5: `gpt.pth`, `s2mel.pth`, `qwen0.6bemo4-merge`, `feat1.pt`, `feat2.pt`, `wav2vec2bert_stats.pt`, BPE/configs, `IndexTeam/IndexTTS-2.5`) | 22 | `5,491,182,363` | 5.49 GB / 5.11 GiB | ULA bilibili (avisar en usos comerciales) | `${COMFY_DATA_DIR}/indextts/checkpoints/` |
| **`faster_whisper`** (`Systran/faster-whisper-large-v3`: `model.bin` + tokenizador) | 5 | `3,090,835,702` | 3.09 GB / 2.88 GiB | MIT | `models/whisper/faster-whisper-large-v3/` |
| **`wav2vec2_aligners`** (`jonatasgrosman/wav2vec2-large-xlsr-53-{english,spanish}`) | 2 | `2,524,171,054` | 2.52 GB / 2.35 GiB | Apache-2.0 | `models/whisper/aligners/` |
| **`clip_vit_l14`** (`openai/clip-vit-large-patch14`: `pytorch_model.bin` + tokenizador/configs) | 7 | `1,712,163,490` | 1.71 GB / 1.59 GiB | MIT (con caveats de model card) | `models/clip/clip-vit-large-patch14/` |
| **`ipadapter_sdxl`** (`h94/IP-Adapter`: `ip-adapter_sdxl_vit-h`, `ip-adapter-plus_sdxl_vit-h` + `CLIP-ViT-H-14`, **sin FaceID**) | 3 | `4,388,305,741` | 4.39 GB / 4.09 GiB | Apache-2.0 | `models/ipadapter/` y `models/clip_vision/` |
| **`campplus`** (`funasr/campplus`: `campplus_cn_common.bin` + configs) | 3 | `28,037,453` | 0.03 GB / 0.03 GiB | Apache-2.0 | `models/funasr/campplus/` |
| **TOTAL FASE 3 POR DEFECTO (`MODEL_PROFILE=studio`)** | **79** | **`163,706,792,041`** | **163.71 GB / 152.46 GiB** | — | — |
| **`animate_opt_in`** (`wan2.2_animate_14B_int8_convrot.safetensors`, solo con `STUDIO_INCLUDE_ANIMATE=1`) | +1 | `+18,413,068,672` | **+18.41 GB / +17.15 GiB** | Apache-2.0 | `models/diffusion_models/` |
| **`three_d_opt_in`** (`hunyuan_3d_v2.1.safetensors`, con `STUDIO_INCLUDE_3D=1` o `MODEL_PROFILE=unreal`) | +1 | `+7,365,943,290` | **+7.37 GB / +6.86 GiB** | Tencent Hunyuan (uso no comercial) | `models/checkpoints/` |
| **`minimax_h3_opt_in`** (MiniMax H3 / Hailuo 3.0 Ref2VA: DiT ref2va + Singularity v2, LoRA turbo 4-step, Qwen3-VL 32B NVFP4, VAEs de vídeo y audio, solo con `STUDIO_INCLUDE_MINIMAX=1`) | +6 | `+62,997,682,615` | **+63.00 GB / +58.67 GiB** | MiniMax (revisar licencia) | `models/{diffusion_models,loras,text_encoders,vae}/` |

> **Nota sobre los grupos opt-in de Fase 3**: la base por defecto son 79 archivos (163.71 GB). `STUDIO_INCLUDE_ANIMATE=1` añade el modelo base de 14B de Wan 2.2 Animate (`wan2.2_animate_14B_int8_convrot.safetensors`, 18.41 GB; su LoRA relight ya viene por defecto en `wan_core`), `STUDIO_INCLUDE_3D=1` añade Hunyuan3D 2.1 (7.37 GB, implícito en `MODEL_PROFILE=unreal`) y `STUDIO_INCLUDE_MINIMAX=1` añade MiniMax H3 (6 archivos, 63.00 GB). Con `STUDIO_INCLUDE_ANIMATE=1` el total son 80 archivos / 182.12 GB y hace falta alquilar **250 GB** de disco; con MiniMax H3 el total de modelos sube a 245.71 GB; el volumen mínimo derivado es 294 GB. Recalcula los opt-ins combinados y deja espacio para outputs.

### Resumen de Tamaños y Tiempos por Perfil (`MODEL_PROFILE`)

| Perfil (`MODEL_PROFILE`) | Fase 1 | Fase 2 | Fase 3 | Total Modelos | Tiempo a 500 Mbps | Tiempo a 1 Gbps |
|---|---:|---:|---:|---:|---:|---:|
| **`all`** *(por defecto)* | 2.79 GiB | 60.56 GiB | 0.00 GiB | **63.35 GiB** | ~19 min | ~9.5 min |
| **`general`** | 2.79 GiB | 24.96 GiB | 0.00 GiB | **27.75 GiB** | ~8 min | ~4 min |
| **`realistic`** | 2.79 GiB | 24.95 GiB | 0.00 GiB | **27.74 GiB** | ~8 min | ~4 min |
| **`anime`** | 2.79 GiB | 29.43 GiB | 0.00 GiB | **32.22 GiB** | ~9.5 min | ~4.7 min |
| **`minimal`** | 2.79 GiB | 0.00 GiB | 0.00 GiB | **2.79 GiB** | 0 s | 0 s |
| **`studio`** | 2.79 GiB | 13.22 GiB | 152.46 GiB *(163.71 GB)* | **≈ 168.47 GiB** *(≈180.90 GB en disco)* | ~48 min | ~24 min |
| **`unreal`** | 2.79 GiB | 13.22 GiB | 152.46 GiB + 6.86 GiB *(Hunyuan3D implícito)* | **≈ 175.33 GiB** *(≈188.27 GB en disco)* | ~50 min | ~25 min |

### Custom Nodes Preinstalados (12 Nodos Fijados por Commit SHA en [`config/nodes.lock`](config/nodes.lock))

- [`Comfy-Org/ComfyUI-Manager`](https://github.com/Comfy-Org/ComfyUI-Manager)
- [`Kosinkadink/ComfyUI-VideoHelperSuite`](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite)
- [`Gourieff/ComfyUI-ReActor`](https://github.com/Gourieff/ComfyUI-ReActor)
- [`ltdrdata/ComfyUI-Impact-Pack`](https://github.com/ltdrdata/ComfyUI-Impact-Pack) + [`ComfyUI-Impact-Subpack`](https://github.com/ltdrdata/ComfyUI-Impact-Subpack) (con `sam2` fijado por SHA)
- [`Fannovel16/comfyui_controlnet_aux`](https://github.com/Fannovel16/comfyui_controlnet_aux)
- [`cubiq/ComfyUI_IPAdapter_plus`](https://github.com/cubiq/ComfyUI_IPAdapter_plus)
- [`Fannovel16/ComfyUI-Frame-Interpolation`](https://github.com/Fannovel16/ComfyUI-Frame-Interpolation)
- [`rgthree/rgthree-comfy`](https://github.com/rgthree/rgthree-comfy)
- [`kijai/ComfyUI-KJNodes`](https://github.com/kijai/ComfyUI-KJNodes)
- [`ShmuelRonen/ComfyUI-LatentSyncWrapper`](https://github.com/ShmuelRonen/ComfyUI-LatentSyncWrapper) (`360d5283d7276aee68b4237b1387e594e4ce640e`)
- [`kijai/ComfyUI-LivePortraitKJ`](https://github.com/kijai/ComfyUI-LivePortraitKJ) (`4d9dc6205b793ffd0fb319816136d9b8c0dbfdff`)

---

## 3. Variables de Entorno Soportadas

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `OPEN_BUTTON_PORT` | `8188` | Puerto que abre el botón azul **Open** en la consola web de Vast.ai. |
| `COMFY_USER` | `admin` | Usuario para HTTP Basic Auth en Nginx (puerto 8188). |
| `COMFY_PASS` | *(Autogenerada de 24 hex chars si se omite)* | Contraseña para HTTP Basic Auth. Si la defines, **no se imprime en los logs**; si la omites, se genera una clave aleatoria única con `openssl rand -hex 12` y se muestra una vez en el log de arranque (y nunca se expone en `/status/api`, `/report.md` ni `/diag`). |
| `STRICT_START` | `1` | Ejecuta las comprobaciones de host antes de iniciar ComfyUI o adquirir modelos. En Studio/Unreal hay validaciones adicionales, incluido el diagnóstico del perfil Blackwell. El disco de arranque usa el valor derivado y horneado en `storage-requirement.json` (actualmente 11 GB con bootstrap vacío); los mínimos de volumen total se calculan por perfil y no son ese gate. `STRICT_FATAL_GRACE` conserva portal y diagnóstico durante 600 s antes de salir con código 20. |
| `STRICT_MIN_VRAM_MB` / `STRICT_WARN_VRAM_MB` | `20000` / `22000` | Umbrales de VRAM en MiB (`FATAL` si `< STRICT_MIN_VRAM_MB`, `WARN` si `< STRICT_WARN_VRAM_MB`). |
| `STRICT_MIN_RAM_MB` / `STRICT_WARN_RAM_MB` | `16384` / `32768` | Umbrales de RAM en MiB (`FATAL` si `< STRICT_MIN_RAM_MB * 0.95`, `WARN` si `< STRICT_WARN_RAM_MB`). |
| `STRICT_MIN_DISK_GB` | Derivado de la imagen/perfil; override explícito disponible | Espacio libre mínimo en `COMFY_DATA_DIR` para el arranque. La imagen actual deriva 11 GB con bootstrap vacío; consulta `scripts/storage_requirement.py` para el volumen total del perfil y no confundas ambas cifras. |
| `STRICT_MIN_DRIVER` | `570` | Versión mayor mínima del driver NVIDIA (CUDA 12.8) exigida por la comprobación `S2` de `strict-check`. |
| `STRICT_FATAL_GRACE` | `600` | Segundos de gracia tras un `FATAL` con `STRICT_START=1` durante los que se mantienen vivos Nginx, `/home`, `/status` y `/report.md` antes de salir con código `20`. |
| `STRICT_REQUIRE_GPU` | `1` (auto: `0` si `COMFYUI_EXTRA_ARGS` incluye `--cpu`) | Fuerza (`1`) u omite (`0`) la sonda `S10` `cuda_torch` de `strict-check`. |
| `STRICT_REQUIRE_HTPASSWD` | `1` en `comfyctl supervise` (`0` en subcomandos aislados) | Controla si `S7` exige `.htpasswd` ya presente (`fatal` si falta) o lo degrada a `warn`. |
| `MODEL_PROFILE` | `all` | Selecciona el inventario que puede adquirirse. Las cifras `all`, `general`, `realistic`, `anime`, `minimal`, `studio` y `unreal` que siguen son tamaños de catálogo, no descargas de arranque. Pesos de tarea se adquieren bajo demanda; las opciones de capacidad y los grupos opt-in controlan qué se solicita. |
| `STUDIO_INCLUDE_ANIMATE` | `0` | En `MODEL_PROFILE=studio`, pon `1` para incluir el modelo base de Wan 2.2 Animate (`wan2.2_animate_14B_int8_convrot.safetensors`, +18.41 GB / +17.15 GiB, total Fase 3 = 80 archivos / 182.12 GB). Requiere alquilar al menos 250 GB de disco en Vast.ai. |
| `STUDIO_INCLUDE_3D` | `0` | Pon `1` para incluir los pesos de Hunyuan3D 2.1 (`hunyuan_3d_v2.1.safetensors`, +7.37 GB / +6.86 GiB) y habilitar la plantilla `hunyuan3d_image_to_3d`. Implícito (y siempre activo) con `MODEL_PROFILE=unreal`. |
| `STUDIO_INCLUDE_MINIMAX` | `0` | Pon `1` para incluir MiniMax H3 / Hailuo 3.0 Ref2VA (6 archivos, +63.00 GB / +58.67 GiB) y habilitar la plantilla `minimax_r2v`. El mínimo derivado actual es 294 GB; vuelve a calcularlo si combinas opt-ins o cambia tu carga de trabajo. |
| `STUDIO_LLM_KEY` / `STUDIO_LLM_BASE_URL` / `STUDIO_LLM_MODEL` | *(Vacía)* / `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` / `qwen3-vl-plus` | Configuración del proveedor LLM primario compatible-OpenAI para el agente de Comfy Studio (`MODEL_PROFILE=studio`). `STUDIO_LLM_KEY` se redacta siempre en `/status/api`, `/studio/api/*` y `/report.md`. |
| `STUDIO_LLM_SUPPORTS_TOOLS` / `STUDIO_LLM_SUPPORTS_STREAMING` | `1` / `1` | Declaran si el perfil primario admite tool calling y SSE. Con tools desactivado no se envía `tools`; con streaming desactivado la respuesta JSON se entrega al callback en fragmentos de texto después de completarse. |
| `STUDIO_LLM_FALLBACK_KEY` / `STUDIO_LLM_FALLBACK_BASE_URL` / `STUDIO_LLM_FALLBACK_MODEL` | *(Vacía)* / `https://api.deepseek.com` / `deepseek-chat` | Proveedor LLM secundario (failover automático ante errores 4xx/5xx/timeout del primario). `STUDIO_LLM_FALLBACK_KEY` es independiente de `STUDIO_LLM_KEY` (fail-closed: nunca reenvía la key primaria a otro proveedor) y se redacta en todas las salidas. |
| `STUDIO_LLM_FALLBACK_SUPPORTS_TOOLS` / `STUDIO_LLM_FALLBACK_SUPPORTS_STREAMING` | `1` / `1` | Declaran por separado el contrato de tools y SSE del perfil fallback. Ambos perfiles permiten model IDs y capacidades diferentes sin editar código. |
| `STUDIO_SERVER_PORT` | `18190` | Puerto loopback (`127.0.0.1`) donde `comfy_studio.server` expone `/studio/api/*` cuando `MODEL_PROFILE=studio`. |
| `STUDIO_SERVER_WORKERS` | `1` | Único valor admitido (M7-OPS-004): el servidor usa un `ProgressBroker` SSE en memoria y estado por proceso, así que con varios workers los eventos no llegarían a todos los clientes. Cualquier otro valor aborta el arranque con error claro (`exit 2`). |
| `STUDIO_ORCH_REAL_ENGINES` | Automático por perfil | En `MODEL_PROFILE=studio` o `unreal`, los motores reales y la admisión por recursos se activan por defecto. Pon `0`/`simulated` solo para CI o desarrollo sin GPU. `1`/`real` fuerza el modo real. Si falta hardware verificado o falla el cableado, el Studio no arranca; nunca cae silenciosamente a stubs. |
| `STUDIO_MAX_CONCURRENT_RENDERS` | `2` | Máximo de jobs de render de plantilla ejecutándose concurrentemente en segundo plano. ComfyUI serializa la ejecución; este límite acota tareas y sondeos en vuelo. |
| `STUDIO_JSON_LOGS` | `0` | Pon `1` para que el studio-server emita logs JSON de una línea (`ts`, `level`, `logger`, `request_id`, `msg`) en stdout. La redacción de secretos se mantiene (los filtros de handler redactan antes de formatear). Cada petición lleva `X-Request-Id` (se acepta uno entrante seguro o se genera `uuid4`), se refleja en la respuesta y se propaga a los logs (M7-OPS-005). |
| `STUDIO_STUB_LLM` / `STUDIO_STUB_COMFY` | `0` / `0` | Flags de prueba para ejecutar Comfy Studio con cliente LLM determinista in-process (`STUDIO_STUB_LLM=1`) y cola ComfyUI simulada (`STUDIO_STUB_COMFY=1`) en tests y smoke CI sin llamadas de red externas. |
| `STUDIO_RESUME_JOBS` / `STUDIO_RESUME_DELAY_S` | `1` / `30` | Recuperación durable de jobs (M3-B): al arrancar, los jobs huérfanos en vuelo (`queued`/`running`/`cancelling`/`fetching`) se marcan `interrupted` y, con `STUDIO_RESUME_JOBS=1`, los jobs `render` elegibles se reencolan **una sola vez** (`interrupt_retries ≤ 1`) tras `STUDIO_RESUME_DELAY_S` segundos (espera el arranque de ComfyUI). Pon `0` para desactivar por completo la recuperación: es **inerte**, no altera el estado de ningún job (la reconciliación queda en tus manos). |
| `STUDIO_RECOVERY_DRAIN_S` | `10` | Segundos que el apagado espera a que termine una recuperación en curso antes de cancelarla. Si expira, los jobs en vuelo se marcan `interrupted` explícitamente para que no queden sin estado terminal. |
| `STUDIO_FFMPEG_TIMEOUT_S` | `60` | Timeout base por subprocess `ffmpeg`. En EDL el límite de cada corte se escala con su duración: `max(base, min(1800, 4 × duración + base))`. Súbelo si renderizas clips largos en hosts con CPU lenta. |
| `STUDIO_FFPROBE_TIMEOUT_S` | `20` | Timeout por sonda `ffprobe` (medición de duración, dimensiones y streams). |
| `STUDIO_FFMPEG_BIN` / `STUDIO_FFPROBE_BIN` | *(Vacías; búsqueda en `PATH`)* | Rutas o nombres de ejecutable para la toolchain multimedia de Studio. Todos los consumidores de FFmpeg, percepción, EDL, ASR, QA y el health check comparten el mismo resolver; si no defines `STUDIO_FFPROBE_BIN`, se busca `ffprobe` junto a `ffmpeg` y luego en `PATH`. Compatible con rutas de Windows, macOS y Linux. |
| `STUDIO_UPLOAD_MAX_TOTAL_MB` | `1024` | Techo del **total** de bytes de archivo de una petición multipart, además del límite por archivo (`STUDIO_MAX_UPLOAD_BYTES`, 512 MiB). Evita que muchas partes bajo el límite por archivo agoten memoria o disco temporal. |
| `STUDIO_WEBHOOK_URL` / `STUDIO_WEBHOOK_SECRET` | *(Vacías)* | Webhooks salientes (M3-B): con `STUDIO_WEBHOOK_URL` definida (URL aportada por el operador, nunca por usuarios del panel), Comfy Studio notifica `job_finished`, `job_failed`, `job_cancelled`, `job_interrupted` y `agent_turn_done` con timeout de 10 s, 3 reintentos con backoff y backlog acotado (128). Una cancelación se emite como `job_cancelled`, **no** como fallo. Si `STUDIO_WEBHOOK_SECRET` está definido, cada petición firma el body con HMAC-SHA256 en la cabecera `X-Studio-Signature`. Ambos valores se redactan en `/status/api`, `/report.md` y en los propios payloads. La URL se valida contra loopback/redes internas, incluida la IP de metadatos `169.254.169.254` (anti-SSRF). |
| `STUDIO_ENABLE_MCP` | `1` | Activa el servidor MCP (Model Context Protocol, M3-C) en `/mcp` (JSON-RPC 2.0 → `studio-server`): agentes externos (Kilo, clientes LLM) pueden listar proyectos/assets, listar plantillas, encolar renders, consultar jobs y exportar EDL. Health en `/mcp/health` y sonda `S12 mcp_health` de `strict-check`. Pon `0` para desactivarlo. |
| `STUDIO_PERCEPTION_BACKEND` | `auto` | Backend de percepción ML (M3): `auto` hace un `probe` al arranque y usa `real` si pasa, `sim` en caso contrario (fallback visible con `simulated: true`); `real` exige backends reales y responde 503 si el probe falla (sin fallback); `sim` usa la simulación determinista sin tocar los pesos. |
| `STUDIO_ML_PYTHON_BIN` | `python3` | Intérprete Python para los workers de ML en subprocesso (M3.1). Por defecto `sys.executable`; en producción apuntar a `/usr/bin/python3` (el intérprete de ComfyUI con `torch`, `insightface`, etc.). |
| `STUDIO_ML_TIMEOUT_S` | `120` | Timeout por defecto (segundos) para llamadas al worker ML. Se ajusta por subcomando: `probe=30s`, `clip_embed*=120s`, `faces_detect=120s`, `asr_transcribe=300s`, `diarize=120s`. |
| `STUDIO_API_TOKEN` | *(Autogenerado si MCP activo)* | Token para clientes máquina del endpoint `/mcp`: cabecera `X-Studio-Token: <token>` (o `Authorization: Bearer <token>` en loopback), **además** del Basic Auth de borde. Si lo dejas vacío, `start.sh` genera uno con `openssl rand -hex 24`, lo imprime una vez en el banner de arranque (`start.log`, 0600) y lo persiste en `/run/comfyui-api-token` (0600) para que `comfyctl` lo lea, borre y redacte en todas las salidas. |
| `COMFY_TLS` | `0` | Pon `1` para servir el puerto público 8188 con TLS usando un certificado autofirmado generado en el arranque (RSA 2048, 365 días, `/etc/nginx/tls/`). Sin dominio propio el navegador mostrará aviso de certificado; accede por `https://<IP_VAST>:<PUERTO>`. Útil para no enviar `COMFY_PASS`/`STUDIO_API_TOKEN` en claro por la red de Vast.ai. |
| `SKIP_MODEL_DOWNLOAD` | `0` | Pon `1` para omitir por completo la descarga automática de modelos al arrancar. |
| `HF_TOKEN` | *(Vacío)* | Token opcional de HuggingFace (leído automáticamente del entorno por `hf` sin exponerse en argumentos de proceso ni en `/report.md`). |
| `COMFY_DATA_DIR` | `/workspace/comfyui-data` | Ruta donde se almacenan `models/`, `input/`, `output/` y `user/` (actualiza automáticamente `extra_model_paths.yaml`). |
| `COMFY_VOLUME_WAIT_TRIES` / `COMFY_VOLUME_WAIT_SLEEP_S` | `12` / `5` | Guardia de montaje (M7-OPS-002): si `COMFY_DATA_DIR` está bajo `/workspace`, `start.sh` espera hasta `TRIES × SLEEP_S` (60 s por defecto) a que `/workspace` sea un punto de montaje real (no el overlay del contenedor) antes de escribir datos. En RunPod los Network Volumes tardan 30-60 s en montar; en Vast.ai no hay espera. Pon `COMFY_VOLUME_WAIT_TRIES=0` para no esperar. |
| `COMFY_REQUIRE_PERSISTENT_VOLUME` | `0` | Pon `1` para abortar el arranque (`exit 1`) si tras la espera no hay volumen montado en `/workspace`: evita que modelos/outputs se escriban en el disco efímero del contenedor y se pierdan al detener el pod. Recomendado en RunPod. |
| `COMFYUI_EXTRA_ARGS` | *(Vacío)* | Flags adicionales para `python3 main.py` de ComfyUI. Ejemplo de diagnóstico para errores `HostBuffer` / OOM en GPUs Blackwell: `--disable-pinned-memory` (ver sección 5.7). |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` (fijado en la imagen) | Config del allocator de CUDA de PyTorch aplicada por defecto a ComfyUI y al studio-server (M7-OPS-001): mitiga los OOM por fragmentación en sesiones largas con cargas mixtas (sección 5.7). Sobrescribe con `-e PYTORCH_CUDA_ALLOC_CONF=...`; pon `-e PYTORCH_CUDA_ALLOC_CONF=` (vacío) para desactivarla. |
| `COMFY_MAX_RESTARTS` | `5` | Número máximo de reinicios automáticos de ComfyUI ante caídas antes de pasar a estado `exhausted` (manteniendo Nginx, `/home` y `/status` activos para diagnóstico). |
| `COMFY_RESTART_DELAY` | `5` | Segundos de espera entre caídas de ComfyUI antes de que el supervisor relance `main.py`. |
| `COMFY_HEALTHY_UPTIME` | `600` | Segundos de funcionamiento continuo tras los cuales el supervisor resetea `restarts` a `0` e incrementa `healthy_resets`. |
| `LOG_MAX_MB` | `200` | Tamaño máximo en MB por fichero de log en `/var/log` antes de truncar conservando los últimos `50 MB`. Cubre `comfyui.log`, los logs `access`/`error` de nginx, `download-models.log`, `start.log`, `supervisor.log`, `ttyd.log` y `diag.txt`. Los logs sensibles (`start.log` —contiene el banner con la contraseña de arranque—, `supervisor.log`, `ttyd.log`, `diag.txt`) se crean con permisos `600` (solo root); `comfyui.log` y `download-models.log` son `644` porque los sirve nginx. |
| `HF_FETCH_TIMEOUT` | `3600` | Timeout máximo en segundos por intento de descarga individual en `download-models.sh`. |
| `HF_HUB_DISABLE_XET` | *(Vacío)* | Pon `1` para que `hf download` use HTTP clásico en lugar del backend `hf-xet`. Útil si la Fase 2 se queda colgada, devuelve muchos 503 o el host tiene poca RAM (ver sección 5.8). |
| `ENABLE_WEB_TERMINAL` | `0` | Pon `1` para activar `ttyd` en `127.0.0.1:7681`, accesible tras Basic Auth en `/terminal/`. Desactivado por defecto por seguridad. |
| `STATUS_SERVER_PORT` | `18189` | Puerto loopback (`127.0.0.1`) donde `comfyctl serve` expone `/status`, `/status/api`, `/report.md`, `/diag` y `/logs/<nombre>`. Controla de verdad el bind por defecto del servidor (`127.0.0.1:{port}`). |
| **Rutas internas (avanzado)** | — | `COMFY_APP_DIR` (`/opt/ComfyUI`), `STUDIO_DATA_DIR` (`${COMFY_DATA_DIR}/studio`), `STUDIO_COMFYUI_URL` (`http://127.0.0.1:18188`), `COMFY_SUPERVISOR_JSON` (`/run/comfyui-supervisor.json`), `COMFY_STRICT_JSON` (`/run/comfyui-strict.json`), `COMFY_STARTED_AT_FILE` (`/run/comfyui-started-at`), `COMFY_IMAGE_JSON` (`/etc/comfyui/image.json`), `COMFY_HTPASSWD_PATH` (`/etc/nginx/.htpasswd`) y `COMFY_NVIDIA_SMI_BIN` (`nvidia-smi`). Todas tienen defaults correctos y raramente se tocan (útiles sobre todo para tests). |
| **Variables de plataforma Vast.ai (solo lectura)** | `local` / `8188` | `VAST_CONTAINERLABEL` y `VAST_TCP_PORT_8188` las inyecta Vast.ai al alquilar la instancia; `comfyctl` las lee ([`comfyctl/src/config.rs`](comfyctl/src/config.rs) `L352-L353`) para exponer `instance.vast_instance_id` y `instance.external_port_8188` en `/status/api` y `/report.md` (`collect/mod.rs` `L123-L124`). Fuera de Vast.ai usan los valores por defecto. Del mismo grupo, `COMFY_PASS_AUTOGENERATED` (`0`/`1`) es un marcador interno que escribe [`scripts/start.sh`](scripts/start.sh) (`L242-L254`) cuando autogenera la contraseña, para que `comfyctl` muestre `<autogenerada — ver logs del contenedor>` en lugar de la clave. |

### Copias portátiles de Studio

Studio puede empaquetar sus bases de datos y archivos de proyecto en un snapshot verificable, portable entre Windows, macOS y Linux. El snapshot usa la API de backup online de SQLite (incluye datos aún en WAL), empaqueta `studio.db`, `index.db` si existe y `projects/`, y registra tamaño/SHA-256 por archivo. Excluye pesos de modelos, configuración y secretos. Ejecútalo desde el entorno Python de Studio:

```bash
python -m comfy_studio.backup create /ruta/segura/studio-backup.zip
python -m comfy_studio.backup verify /ruta/segura/studio-backup.zip
```

`create` resuelve `STUDIO_DATA_DIR` (o `COMFY_DATA_DIR/studio`) y exige que el ZIP de salida quede fuera del directorio de datos. Para restaurar manualmente, detén Studio, conserva una copia del destino actual, verifica el ZIP, extrae `studio.db`, `index.db` (si está presente) y `projects/` dentro del `STUDIO_DATA_DIR` configurado, y vuelve a iniciar. El comando `verify` valida el manifiesto y hashes; no modifica archivos ni instala pesos.

---

## 4. Guía de Uso (Navegador en Windows, macOS o Linux)

La guía está dividida en dos partes: **A. Primera vez** (configuración que se hace una sola vez) y **B. Cada sesión** (lo que repites cada vez que alquilas una GPU). Los tiempos citados provienen de ejecuciones reales del workflow de CI (runner `ubuntu-24.04` con `buildcache`).

### A. Primera vez

#### A1. Publicar la imagen en GitHub Container Registry (gratis, sin Docker local)

1. Crea un repositorio en tu cuenta de GitHub (por ejemplo `comfyui-vast`) y sube el contenido de este proyecto a la rama `main`.
2. El workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) valida scripts, SHAs y las 26 URLs de HuggingFace, construye una imagen candidata `sha-*`, ejecuta el smoke test (incluido `--quick-test-for-ci` de ComfyUI) y, solo si pasa, la promueve a:
   ```text
   ghcr.io/<tu-usuario-github>/<tu-repo>:latest
   ```
   Medido en CI: el job de build tarda **≈ 10.5 min** reutilizando la caché `buildcache`; la primera ejecución sin caché tarda bastante más (compila la imagen completa y descarga las capas base, ~198 s solo las capas base en el runner).
3. Cuando el workflow esté en verde: perfil de GitHub → **Packages** → el paquete → **Package settings** → **Change visibility → Public**. Sin este paso Vast.ai no puede descargar la imagen. (En GHCR verás también un tag `buildcache` y una entrada de plataforma `unknown/unknown`: ambos son normales.)
4. Anota la ruta exacta de la imagen. GitHub la publica **en minúsculas** aunque tu usuario o repo tengan mayúsculas.

#### A2. Cuenta en Vast.ai

5. Crea la cuenta en [cloud.vast.ai](https://cloud.vast.ai/) y verifica el correo.
6. **Billing → Add Credit**: consulta el precio vigente de la oferta, almacenamiento y transferencia antes de cargar saldo. El coste depende del hardware disponible y del tiempo que mantengas la instancia.
7. Opcional pero recomendado: **Account → API Keys → New** para poder usar el CLI `vastai` desde la Terminal de la Mac (ver A4).

#### A3. Crear la plantilla en Vast.ai

8. Entra en [cloud.vast.ai/templates](https://cloud.vast.ai/templates/) → **New Template** y rellena:

   | Campo | Valor |
   |---|---|
   | **Image Path:Tag** | `ghcr.io/<tu-usuario>/<tu-repo>:latest` |
   | **Docker Options** | `-p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e COMFY_USER=admin -e MODEL_PROFILE=studio` y, si quieres contraseña fija, `-e COMFY_PASS=TuClaveLargaUnica123` |
   | **Launch Mode** | **Docker ENTRYPOINT** |
   | **Disk Space** | Mínimo derivado para el perfil elegido (231 GB para Studio base); consulta `scripts/storage_requirement.py` y deja margen para outputs y opt-ins. |

9. Por qué **ENTRYPOINT**: en los modos *SSH* y *Jupyter*, Vast.ai sobrescribe el `ENTRYPOINT` de la imagen para instalar su propio servidor SSH/Jupyter. En esos modos tendrías que escribir `bash /start.sh` en el campo **On-start Script** para que el contenedor arranque. En modo ENTRYPOINT `tini → /start.sh` corre tal cual y el `HEALTHCHECK` funciona.
10. Guarda la plantilla como **privada**. Si la guardas como pública con `COMFY_PASS` en Docker Options, cualquiera podría leer tu contraseña.

> **Notas de seguridad (SEC-05 / BAJA-15)**
> - Los puertos directos de Vast.ai van por HTTP plano. Usa una contraseña única por sesión en `COMFY_PASS`, o déjala vacía para que el contenedor genere una aleatoria en cada arranque.
> - Como las imágenes estándar de Vast.ai, el contenedor corre como `root` para evitar problemas de permisos con volúmenes en `/workspace`.

#### A4. Preparar la Mac y el CLI `comfy` (opcional, recomendado)

11. Instala el CLI de Vast.ai: `pipx install vastai` (o `pip3 install --user vastai`) y configura la clave: `vastai set api-key <tu-clave>`.
12. Instala el helper [`tools/mac/comfy`](tools/mac/comfy) (ver [`tools/mac/README.md`](tools/mac/README.md)) y exporta `COMFY_IMAGE=ghcr.io/<tu-usuario>/<tu-repo>:latest` en tu `~/.zshrc` para usar `comfy rent`:
    ```bash
    mkdir -p ~/.local/bin && cp tools/mac/comfy ~/.local/bin/comfy && chmod +x ~/.local/bin/comfy
    ```
    Con él puedes abrir el portal (`comfy`), abrir Comfy Studio (`comfy studio`), ver el estado (`comfy open status`), extraer la contraseña autogenerada (`comfy pass`), descargar y copiar al portapapeles el informe completo para el agente IA (`comfy report` o `comfy studio report`), monitorizar en vivo (`comfy watch`), alquilar (`comfy rent` / `comfy rent studio`), rescatar tus renders antes de destruir (`comfy fetch-outputs [id] [dest-dir]`, vía `vastai ssh` + `tar`/`base64`, re-ejecutable y con skip de archivos ya descargados; rechaza cualquier ruta con `..` tanto en el destino como dentro del `tar` recibido) o destruir (`comfy destroy <id>`).

#### A5. Sesión de aceptación (solo la primera instancia real)

13. En una aceptación real, registra commit e imagen exactos, GPU/driver, tiempos de arranque, tareas/modelos adquiridos y resultados reproducibles. La checklist V5 es histórica; una instancia aceptada no valida todas las plantillas ni sustituye mediciones por receta.

---

### B. Cada sesión

#### B1. Buscar y alquilar la GPU (5 minutos)

1. En **Search**, con tu plantilla seleccionada, aplica estos filtros:

   | Filtro | Valor | Por qué |
   |---|---|---|
   | GPU | Una RTX PRO 6000 Blackwell de 96 GB para el perfil predeterminado; 1 GPU | El helper deriva los filtros del perfil de hardware configurado |
   | **CUDA** (`cuda_vers`) | **≥ 12.8** | Versión máxima de CUDA soportada por el host (campo documentado de la API); con menos, `torch` cu128 no inicializa la GPU |
   | **GPU RAM** | ≥ 96 GB para el perfil Blackwell predeterminado | El helper valida la VRAM anunciada contra el perfil; no infiere la capacidad solo por el nombre de la oferta |
   | **CPU RAM** | ≥ 128 GB por defecto; 256 GB si seleccionas esa variante | La RAM de host y la VRAM son presupuestos separados; una no sustituye a la otra |
   | **Disk Space** | Mínimo derivado por perfil: 231 GB para Studio base; vuelve a calcularlo para opt-ins y añade el espacio de outputs. | `comfy rent studio` lo calcula; consulta `scripts/storage_requirement.py` antes de alquilar |
   | **Internet Download** (`inet_down`) | ≥ 5000 Mbps (5 Gbps), como exige el helper `comfy rent studio` | El tiempo depende de los activos que solicites, el tamaño y el rendimiento real del host; no todo el catálogo se descarga al iniciar |
   | **Reliability** | ≥ 0.98, *verified* | Menos cortes a mitad de sesión |

   > Los puertos van **proxy-mapeados** por Vast.ai (no se requieren *direct ports*): Vast asigna un puerto del host y lo redirige al 8188 del contenedor. Es lo que hace `comfy rent` con `--args` (runtype args, que respeta el ENTRYPOINT de la imagen).

2. **Lee la tarjeta de la oferta antes de pulsar Rent**, no solo el $/h:
   - **RAM del host**: respeta la RAM de host del perfil seleccionado: Studio requiere 128 GB por defecto (o 256 GB en esa variante). En perfiles menores, el uso real depende de la receta; no asumas que 32 GB bastan para cargas grandes.
   - **$/GB/mes de almacenamiento** y **$/TB de tráfico**: si una tarea descarga muchos modelos, el tráfico puede costar más que la primera hora de GPU; el arranque por defecto no descarga el catálogo.
   - **On-demand** para sesiones de trabajo; *interruptible* es más barato pero otro usuario puede quitarte la máquina.
3. Pulsa **Rent** (o ejecuta `comfy rent studio` para el perfil de referencia) solo después de revisar la oferta completa. El helper muestra el precio vigente antes de confirmar; no uses cifras históricas como presupuesto actual.

*(Referencia operativa: el helper aplica el perfil actual y evita filtros GPU obsoletos)*:
```bash
comfy rent studio --dry-run
scripts/storage_requirement.py --profile studio --check
```
> `--args` fija el runtype `args` (respeta el ENTRYPOINT de la imagen) y los puertos van proxy-mapeados; no se usa `--direct`. En `--env`, el entrecomillado es **solo del valor** (`-e CLAVE="valor"`): el parser de vast-cli limpia comillas del valor, nunca de la clave.

#### B2. Esperar el arranque (de 5 a 15 minutos la primera vez en un host)

4. En **Instances**, la tarjeta pasa por *Loading / Creating* mientras el host descarga la imagen (~7-8 min medidos en el runner de CI para la imagen de 15.6 GB: ~218 s el pull de la candidata + ~198 s las capas base; en el host de Vast depende de su red). Si el host ya tenía la imagen en caché de una sesión anterior, es casi inmediato.
5. Cuando pase a **Running**, aparece el botón **Open** y la dirección `IP:PUERTO` (o ejecuta `comfy` en tu Mac para abrir directamente `/home`). **El puerto externo no es 8188**: Vast.ai asigna un puerto aleatorio del host y lo redirige al 8188 del contenedor. Usa siempre la dirección que muestra la tarjeta.
6. Cómo saber en qué punto está el arranque:
   - Botón **Logs** de la tarjeta (o `comfy logs` / `comfy pass`): si está vacío, la imagen aún se está descargando; si ves `[1/7] Verificando instalación de ffmpeg…`, `start.sh` ya corre. Ahí aparece también la contraseña si dejaste que se autogenere.
   - Al pulsar **Open**, introduce las credenciales HTTP Basic Auth. El portal `/home` y `/status` están disponibles durante el arranque; el bootstrap actual no baja pesos por defecto.
   - Cuando ComfyUI esté listo, la misma pestaña muestra la interfaz. Si una tarea requiere pesos que no están en disco, solicítalos desde la capacidad de Studio o instala/descarga el modelo de forma explícita; el catálogo completo no se descarga en segundo plano al iniciar.

#### B3. Correr el primer workflow

7. **Elige y adquiere el checkpoint correcto.** El workflow por defecto de ComfyUI apunta a `v1-5-pruned-emaonly.safetensors`, que no está en este catálogo. Elige un checkpoint del perfil y descárgalo explícitamente si falta; por ejemplo, `Realistic_Vision_V6.0_NV_B1_fp16` (SD 1.5) o `Juggernaut-XI-byRunDiffusion` (SDXL). El catálogo ya no se descarga automáticamente por fases al iniciar.
8. Orden recomendado de pruebas en una sesión nueva: (a) checkpoint + KSampler 512×512, 20 pasos, para confirmar CUDA y VAE; (b) añadir **ReActor** sobre el resultado, para confirmar `inswapper` y ONNX Runtime en GPU; (c) `VHS Load Video` + `Video Combine` con formato `video/h264-mp4`, para confirmar `h264_nvenc`; (d) **FLUX fp8** solo cuando `/status` muestre que terminó (16 GB, es lo último que baja).
9. Para cargar tus propios workflows: arrastra el `.json` o `.png` desde el Finder sobre el lienzo. Las plantillas oficiales están en **Workflow → Browse Templates**; si una pide un modelo que falta, ComfyUI muestra el diálogo *Missing models* con botón de descarga.
10. Sube vídeos con `VHS_LoadVideo`; no hay límite de tamaño en el proxy.

#### B3.1 Runbook S3 — Percepción Visual, Transcripción/Diarización y EDL en Comfy Studio (`MODEL_PROFILE=studio`)

Cuando alquilas con `comfy rent studio` y abres `comfy studio` (`/studio/`), puedes ejecutar el flujo completo de indexación y montaje automático desde la pestaña **Percepción & EDL** o pidiéndoselo al agente conversacional:

> **Modo de inferencia actual**: los pasos 1–4 funcionan de extremo a extremo (assets, keyframes reales con `ffmpeg`, índice `sqlite-vec`, transcripciones/EDL y render EDL real con `ffmpeg` que **falla** si `ffprobe` no verifica el MP4). Los backends ML de percepción/ASR arrancan en `auto`: usan backend real si el probe de capacidades pasa; si no, la simulación determinista local se declara con `simulated: true` y `simulation_notice`. `real` exige los modelos reales y falla de forma explícita si no están disponibles; `sim` fuerza la simulación. El índice visual CLIP del panel sigue siendo determinista y no debe interpretarse como calidad de un modelo CLIP real. Controla el modo con `STUDIO_PERCEPTION_BACKEND=auto|real|sim` y revisa el campo `simulated` de cada respuesta antes de evaluar resultados.

1. **Subir brutos e indexar keyframes (`sqlite-vec`)**:
   - Arrastra tus clips MP4/MOV/WAV o fotos de referencia a la barra lateral **Assets del Proyecto**.
   - En la pestaña **Percepción & EDL**, selecciona el vídeo y pulsa **Extraer Keyframes** (`POST /studio/api/projects/{id}/keyframes/extract`) e **Indexar** (`POST /studio/api/projects/{id}/perception/index`), o escribe en el chat *"extrae los keyframes e indexa los medios del proyecto"* (`extract_keyframes`, `index_media`). Las miniaturas con timestamps exactos aparecen en la cuadrícula de keyframes y sus embeddings quedan almacenados en `STUDIO_DATA_DIR/index.db`. Reindexar reutiliza keyframes y embeddings cuando no cambiaron la fuente, los parámetros de extracción, los archivos derivados ni los inputs del embedding; las modificaciones pertinentes invalidan esa caché. Si `ffmpeg` no puede extraer fotogramas de un clip, el marcador de posición se guarda con `synthetic: true` y **se excluye del índice**.
2. **Detectar rostros, nombrar identidades y buscar planos (`texto`, `cara` o `texto ∩ persona`)**:
   - Pulsa **Detectar Caras** (`POST /studio/api/projects/{id}/faces/index`) sobre los vídeos o fotos de referencia y registra/renombra identidades (`POST /studio/api/projects/{id}/identities`, `PATCH /studio/api/projects/{id}/identities/{identity_id}`).
   - Busca planos por descripción textual (`find_shots_text`), por identidad/foto de referencia (`find_shots_face`) o combinando ambos (`POST /studio/api/projects/{id}/search/combined`, p. ej. *"Persona A hablando frente a pizarra"*).
   - > **Aviso de licencia no comercial (`InsightFace buffalo_l`)**: cuando se integren los pesos reales del paquete `InsightFace buffalo_l` (`det_10g.onnx`, `w600k_r50.onnx`), su licencia **no comercial (NC)** restringe el uso del índice facial a investigación y proyectos no comerciales salvo acuerdo comercial directo con el titular (los pesos `IP-Adapter-FaceID` siguen excluidos del catálogo Fase 3 por el mismo motivo). El aviso acompaña ya a todas las respuestas faciales.
3. **Transcribir y diarizar**:
   - Pulsa **Transcribir Audio** (`POST /studio/api/projects/{id}/transcribe`, herramienta `transcribe`) para obtener palabras con timestamps (`start_sec`, `end_sec`) y detección de muletillas en ES/EN. Comprueba `simulated` y `simulation_notice`: en `auto`, `true` indica que se usó el fallback determinista; con backend real disponible puede devolver inferencia real.
   - Pulsa **Diarizar Hablantes** (`POST /studio/api/projects/{id}/diarize`, herramienta `diarize`) para etiquetar cada segmento y palabra por hablante (`SPEAKER_00`, `SPEAKER_01`, ...). La ruta simulada no requiere `pyannote` ni `HF_TOKEN`; verifica de nuevo el campo `simulated` de la respuesta.
4. **Generar EDL de Highlights y renderizar entregables `16:9` y `9:16` con `ffmpeg` (real)**:
   - Pulsa **Generar Highlights EDL** (`POST /studio/api/projects/{id}/edl/highlights`, o pide al agente `propose_edl`) para crear una nueva versión de EDL (`comfy_studio.edl.v1`) que recorta pausas (`>= min_pause_sec`) y muletillas (`remove_fillers=true`).
   - Revisa o ajusta los cortes (`in_sec`, `out_sec`, `speaker`, `reason`) en el timeline editable de la pestaña **Percepción & EDL**, elige el formato de entrega (`16:9` `1280x720` o `9:16` `720x1280`) y pulsa **Renderizar MP4** (`POST /studio/api/projects/{id}/edl/{version}/render`, herramienta `build_edl`). El MP4 concatenado se verifica con `ffprobe` y solo entonces se guarda en la **Galería**; si la verificación falla, el render devuelve error en lugar de un entregable con metadatos inventados. Los timeouts de `ffmpeg`/`ffprobe` son configurables (`STUDIO_FFMPEG_TIMEOUT_S`, `STUDIO_FFPROBE_TIMEOUT_S`).

#### Envío de plantillas y seguimiento de jobs

`POST /studio/api/templates/{template_id}/submit` y su ruta por proyecto responden `202 Accepted` con un `job_id` por defecto: la respuesta confirma que el render fue aceptado, **no** que haya terminado. Consulta `GET /studio/api/jobs/{job_id}` o escucha progreso en `/studio/api/progress` (SSE). Los clientes que necesiten esperar dentro de la petición pueden usar `?wait=1` para obtener la respuesta síncrona; se recomienda el modo asíncrono para renders largos. El límite inicial de trabajos concurrentes es `STUDIO_MAX_CONCURRENT_RENDERS=2`.

#### Diagnóstico de herramientas multimedia

Abre `/studio/api/health` y revisa `media_tools.ffmpeg_available` / `media_tools.ffprobe_available`. Si una herramienta aparece ausente, comprueba que el ejecutable esté en `PATH` o define `STUDIO_FFMPEG_BIN` y `STUDIO_FFPROBE_BIN` con su ruta completa; todas las rutas Studio de media, percepción, EDL, ASR y QA comparten este resolver. En solicitudes con error, conserva la cabecera `X-Request-Id` de la respuesta para correlacionarla con logs; `STUDIO_JSON_LOGS=1` activa líneas JSON con `request_id`.

#### B4. Si algo falla durante la sesión (Observabilidad y Diagnóstico IA)

11. **Panel `/status` y Portal `/home`**: Abre `http://<IP>:<PUERTO>/status` (o `http://<IP>:<PUERTO>/home`) para ver en vivo el estado del supervisor de ComfyUI, uso de VRAM/RAM/Disco, progreso de descargas, alertas automáticas de triaje con remediación paso a paso y pestañas de logs (`comfyui`, `start`, `download-models`, `supervisor`, `diag`, `nginx-error`).
12. **Informe para el Agente IA (`/report.md`)**: Pulsa **"Copiar informe para IA (`report.md`)"** en `/home` o `/status` (o ejecuta `comfy report` en tu Mac) y pega el Markdown resultante en el chat del agente. Incluye autodiagnóstico de GPU/PyTorch/ONNX/NVENC, alertas detectadas, deriva de paquetes Python respecto a `requirements.lock`, commits de `custom_nodes` vs `nodes.lock`, inventario de modelos en disco y colas de todos los logs (con secretos redactados).
13. Consulta también la sección 5 (OOM con modelos grandes, descargas de tareas, `IMPORT FAILED`) y registra evidencia de la GPU concreta usada.

#### B5. Cerrar la sesión

14. **Descarga tus resultados** desde la UI: clic derecho sobre la salida de `Video Combine` / `Save Image` (o en **Queue → History**) → **Save**. Todo lo que quede en `/workspace/comfyui-data/output` se pierde con la instancia.
15. Pulsa **Destroy** (icono de papelera), **no Stop**. El almacenamiento puede seguir facturándose al detener, según la oferta. En un pod efímero, el disco local se pierde; los modelos de tarea no se vuelven a bajar por defecto al iniciar. Destruye la instancia cuando hayas rescatado los outputs y respaldado los datos que quieras conservar.
16. Si en el futuro quieres conservar los modelos entre sesiones, Vast.ai ofrece **Volumes** persistentes que podrían montarse en `/workspace/comfyui-data`; la imagen ya está preparada para ello (los datos viven fuera de `/opt/ComfyUI`). Es una decisión pendiente (OPS-04), no una función activa.

#### B6. La misma imagen en un RunPod GPU Pod (M7-OPS-002)

La imagen es agnóstica de proveedor: sirve igual en un **RunPod GPU Pod** persistente:

1. Crea una **Pod Template** con la imagen publicada en GHCR (`ghcr.io/tomas-estrada/comfyuipod:<tag>`), **HTTP port 8188** expuesto y un **Network Volume** (o Volume Disk) montado en **`/workspace`** — `COMFY_DATA_DIR` ya vale `/workspace/comfyui-data` por defecto, así que modelos, outputs y estado de Studio persisten entre pods.
2. Variables recomendadas: `COMFY_PASS=<valor fuerte>` (o déjala autogenerar), `MODEL_PROFILE=studio|all|...`, y `COMFY_REQUIRE_PERSISTENT_VOLUME=1` para que el arranque aborte en lugar de escribir en el disco efímero si el volumen aún no montó (los Network Volumes de RunPod pueden tardar 30-60 s; `start.sh` ya espera ese montaje, ver `COMFY_VOLUME_WAIT_TRIES`).
3. Accede por `https://<pod-id>-8188.proxy.runpod.net/` (HTTPS automático de RunPod) o con `COMFY_TLS=1`. El health check del pod es `GET /healthz` (sin auth, HEAD a ComfyUI).
4. Al terminar, **Stop** (conserva el volumen, no paga GPU) o **Terminate** (borra el Volume Disk; el Network Volume sobrevive si lo mantienes).

> **Advertencia de seguridad: Basic Auth sobre HTTP plano (sin TLS)**
> En la IP pública de Vast.ai el portal va por HTTP sin TLS (sin dominio propio no hay certificado), así que el usuario y la contraseña de Basic Auth viajan **en claro** por la red. Para uso sensible, accede por **túnel SSH**: `vastai ssh <id>` con un forward local del 8188 (p. ej. `ssh -L 8188:127.0.0.1:8188`) y abre `http://127.0.0.1:8188` en tu Mac. Para sesiones cortas con contraseña autogenerada de un solo uso, el riesgo es aceptable y está documentado. **`ENABLE_WEB_TERMINAL=1` (shell root) actívalo únicamente a través de un túnel SSH**, nunca directo sobre la IP pública.

---

## 5. Lecciones Aprendidas y Solución de Problemas

### 1. `OSError: [Errno 22] Invalid argument` en `VHS_VideoCombine`
- **Causa original**: `VideoHelperSuite` usaba un binario incompleto de `imageio-ffmpeg` o un pipe roto sin `libx264`/`nvenc`.
- **Solución aplicada**: [`Dockerfile`](Dockerfile) instala `ffmpeg` de Ubuntu 24.04 (`apt`), fija `VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg` y `VHS_USE_IMAGEIO_FFMPEG=0`, y [`scripts/start.sh`](scripts/start.sh) verifica `libx264` y `h264_nvenc` al arrancar.

### 2. Workflows antiguos con widgets desalineados (ej. error en `pix_fmt`)
- **Síntoma**: Al cargar un workflow `.json` antiguo, `VHS_VideoCombine` o `KSampler` falla indicando un valor inválido en `pix_fmt` (p. ej. un número donde va `"yuv420p"`).
- **Solución rápida en la UI**: Elimina el nodo que da error, haz doble clic en el lienzo, busca el nodo de nuevo (ej. `Video Combine 🎥🅥🅗🅢`) y reconecta sus entradas.

### 3. Cruce de carpetas en `extra_model_paths.yaml` y `models/configs`
- **Cómo funciona**: [`config/extra_model_paths.yaml`](config/extra_model_paths.yaml) cruza `checkpoints`, `diffusion_models` y `unet` (y `clip` con `text_encoders`) sin pre-registrar claves de custom nodes para no vaciar sus conjuntos de extensiones. Además, los archivos YAML de arquitectura (`v1-inference.yaml`, etc.) se conservan en `/workspace/comfyui-data/models/configs`.

### 4. Perfil GPU y rendimiento

El perfil de referencia es RTX PRO 6000 Blackwell de 96 GB de VRAM (94 GB utilizables tras la reserva operativa) con 128 GB de RAM host. Esa capacidad no demuestra que todos los grafos, resoluciones y modelos estén validados. La campaña W26 contiene una ejecución SDXL acotada; consulta [sus resultados y límites](docs/ultra/W26_GPU_RESULTS.md) y usa recetas medidas por combinación de modelo, configuración y hardware.

### 5. Modelos de detección de InsightFace (`buffalo_l`, `inswapper_128.onnx`)
- **Solución aplicada**: La ejecución explícita de `scripts/download-models.sh --phase1` descarga y descomprime `buffalo_l.zip` en `/workspace/comfyui-data/models/insightface/models/buffalo_l/` y crea el enlace en `/root/.insightface/models/buffalo_l`, junto con `inswapper_128.onnx`, `codeformer-v0.1.0.pth`, `GFPGANv1.4.pth`, `detection_Resnet50_Final.pth` y `parsing_parsenet.pth`.

### 6. Actualizaciones de ComfyUI y `PIP_CONSTRAINT` (BAJA-09 / BAJA-12)
- **Versiones congeladas**: ComfyUI y los 12 custom nodes preinstalados se clonan en commits verificados en [`config/nodes.lock`](config/nodes.lock). Para actualizar ComfyUI core a una nueva versión, actualiza el SHA en `config/nodes.lock` y ejecuta el workflow de GitHub Actions en lugar de usar "Update ComfyUI" desde la UI.
- **Protección `PIP_CONSTRAINT`**: La variable `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` está activa en runtime para impedir que un custom node instalado desde ComfyUI-Manager degrade `torch` (`2.9.1+cu128`) o rompa `opencv-python-headless` / `onnxruntime-gpu`. Si algún nodo experimental requiere levantar esa restricción temporalmente, puedes pasar `-e PIP_CONSTRAINT=""` en las opciones del contenedor.
- **No uses "Update ComfyUI" desde Manager**: `/opt/ComfyUI` es un clon superficial fijado por SHA; el botón de actualización del core puede fallar o dejar el frontend (`comfyui-frontend-package`) desalineado con el servidor. Instalar o actualizar *custom nodes* desde Manager sí está soportado.

### 7. OOM o `HostBuffer` con modelos grandes
- **Síntoma**: un modelo o combinación de tareas supera la memoria disponible, aunque quede VRAM libre o la carga sea prolongada. El resultado depende de la receta y del hardware; no generalices a partir de otra GPU.
- **Primer intento**: revisa `/status`, el perfil y los requisitos de la receta; prueba flags como `--disable-pinned-memory` solo si la evidencia del error lo justifica y vuelve a medir.
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` viene activado por defecto para reducir fragmentación; comprueba que no se haya desactivado.
- El `--vram-headroom` / `--reserve-vram` documentado por el SHA fijado debe elegirse con medición y dejar margen para procesos externos.
- El código de salida 137 también puede indicar falta de RAM del sistema. Escoge el perfil de host apropiado y evita adquisiciones/cargas que compitan por memoria.

### 8. La Fase 2 se queda colgada o devuelve muchos errores 503
- **Causa probable**: el backend `hf-xet` que usa `hf download` por defecto dimensiona sus buffers con constantes fijas, no según la RAM disponible, y en contenedores con poca memoria puede provocar OOM, más errores 503 o descargas detenidas (huggingface_hub #3300, xet-core #927 y #581).
- **Solución**: añade `-e HF_HUB_DISABLE_XET=1` a las Docker Options para forzar HTTP clásico. Es algo más lento pero estable. Los modelos ya descargados se conservan (`hf_fetch` salta los archivos existentes) y los parciales se reanudan en el siguiente arranque.

### 9. Un nodo aparece con `IMPORT FAILED` o `no kernel image is available for sm_120`
- **Si pasa en la imagen recién arrancada**: no debería; el build falla si cualquiera de los 12 nodos no importa. Reporta el log completo.
- **Si pasa tras instalar algo desde Manager**: casi siempre un nodo arrastró su propio `torch` o `opencv` y pisó los fijados. Compara `pip3 freeze` con `/etc/comfyui/requirements.lock` (el artefacto `requirements-lock` del workflow es idéntico) para ver qué cambió, y reinstala la terna con `pip3 install --force-reinstall torch==2.9.1+cu128 torchvision==0.24.1+cu128 torchaudio==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128`. Mientras `PIP_CONSTRAINT` siga activo esto no debería ocurrir; ocurre si se desactivó con `-e PIP_CONSTRAINT=""`.
