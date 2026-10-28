# ComfyUI "Everything-Ready" Executor para Vast.ai (RTX 5090 32 GB / RTX 4090 24 GB — 100% Web)

Despliegue efímero de **ComfyUI** en **Vast.ai** optimizado para **NVIDIA RTX 5090 (32 GB GDDR7 VRAM, arquitectura Blackwell `sm_120` con CUDA 12.8)** —y totalmente compatible con **RTX 4090 (24 GB, Ada `sm_89`)**—, diseñado para abrir en el navegador de tu **MacBook**, iniciar sesión con usuario/contraseña (HTTP Basic Auth), arrastrar cualquier workflow (`.json` o `.png`) y ejecutarlo sin tocar rutas ni escribir código.

> Consulta [`docs/decisiones.md`](docs/decisiones.md) para ver la tabla de investigación web verificada (fecha `2026-10-16`) y el registro completo de decisiones de seguridad, reproducibilidad y arquitectura, o [`docs/walkthrough.md`](docs/walkthrough.md) para el desglose archivo por archivo.

---

## 1. Características Principales

1. **Soporte nativo para RTX 5090 (32 GB, Blackwell `sm_120`) y RTX 4090 (24 GB, Ada `sm_89`)**:
   - Basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` con la terna **PyTorch `2.9.1+cu128`** fijada y **`onnxruntime-gpu`** + **`opencv-python-headless`** protegidos mediante [`config/constraints.txt`](config/constraints.txt) y [`config/nodes.lock`](config/nodes.lock), más registro de librerías CUDA/cuDNN en `ldconfig`.
   - Conserva `build-essential` (`gcc`/`g++`) y `python3-dev` en la imagen para que `torch.compile` (Triton) y los custom nodes con extensiones C/C++ instalados desde ComfyUI-Manager funcionen en tiempo de ejecución.
2. **Separación segura `/opt/ComfyUI` (código) y `/workspace/comfyui-data` (datos)**:
   - ComfyUI y los 12 custom nodes están instalados en `/opt/ComfyUI`, mientras que `models/` (incluyendo `models/configs/*.yaml`), `input/`, `output/` y `user/` residen en `/workspace/comfyui-data`. Si montas un volumen persistente en `/workspace` en Vast.ai, **no se oculta la instalación** y los modelos descargados se reutilizan entre sesiones.
3. **Acceso directo por navegador con HTTP Basic Auth, Portal `/home`, Comfy Studio (`/studio/`) y Observabilidad (`/status`, `/report.md`)**:
   - ComfyUI escucha únicamente en loopback (`127.0.0.1:18188`) y **Nginx** (`user www-data;`) protege el puerto público `8188` con usuario/contraseña (`COMFY_USER` y `COMFY_PASS`), pantalla de espera auto-recargable (`_starting.html`) durante la Fase 1, soporte WebSockets (`/ws` sin rate-limit), subida de vídeos sin límite (`client_max_body_size 0;`), `/healthz` (`HEAD /system_stats` sin exponer metadatos), cabeceras de seguridad en el servidor y en todas las locations (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`), compresión `gzip` (CSS/JS/JSON/SVG, mínimo 1024 bytes, `Vary: Accept-Encoding`) y un stack completo de observabilidad en el mismo puerto:
      - **`/home`**: Portal de inicio rápido (`<= 40 KB`, cero CDN externo, contraste WCAG AA) con acceso en 1 clic a ComfyUI, panel `/status`, informe `/report.md`, autodiagnóstico `/diag`, logs y terminal web opcional.
      - **`/studio/` y `/studio/api/*`**: Estudio web de producción audiovisual (**Comfy Studio**, activo en `MODEL_PROFILE=studio` en `127.0.0.1:18190`) con gestión de proyectos y sesiones, ingesta de assets, 7 plantillas curadas de vídeo, imagen y 3D (ver [`docs/plantillas.md`](docs/plantillas.md)), percepción visual (`ffmpeg` keyframes + índice vectorial en `sqlite-vec` `index.db` + índice facial con búsqueda combinada `texto ∩ persona`) y transcripción/diarización, agente conversacional multimodal con streaming SSE, ejecución paralela de herramientas, planificador persistente, memoria resumida y 19 herramientas (`enqueue_render`, `search_assets`, `propose_edl`, `extract_keyframes`, `index_media`, `find_shots_text`, `find_shots_face`, `transcribe`, `diarize`, `build_edl`, `list_templates`, `job_status`, `job_cancel`, `tag_asset`, `system_status`, `export_edl`, `export_subtitles`, `update_plan`, `delegate_role`), recuperación durable de jobs interrumpidos tras reinicio (`STUDIO_RESUME_JOBS`), webhooks salientes firmados con HMAC-SHA256 (`STUDIO_WEBHOOK_URL`/`STUDIO_WEBHOOK_SECRET`), galería con descarga directa y ejecutor determinista de EDL con `ffmpeg` (`16:9` y `9:16`).
        > **Modo de inferencia actual (importante)**: los cuatro subsistemas de percepción/ASR son **simulaciones deterministas locales** — no cargan `CLIP ViT-L/14`, `faster-whisper`/`whisperx`, `FunASR cam++` ni `InsightFace buffalo_l`. Cada respuesta de API incluye `simulated: true` y `simulation_notice`, y el panel muestra un aviso persistente. La integración de los backends reales está planificada en el hito M3 (ver [`docs/propuestas/IDEAS_M3_BACKLOG.md`](docs/propuestas/IDEAS_M3_BACKLOG.md), `M3-IDEA-001..004`). **No evalúes calidad de modelo con estas salidas.**
      - **`/status` y `/status/api`**: Panel de estado en vivo y API JSON servidos por **`comfyctl`** (binario nativo en Rust en `127.0.0.1:18189`) con métricas de GPU/VRAM, RAM/Swap, disco, progreso de descargas Fase 1/2/3, motor de triaje automático (17 patrones con causa y remediación) y visor de logs por pestañas.
      - **`/report.md`**: Informe Markdown autocontenido (hasta 400 KB, con secretos redactados automáticamente por `comfyctl`) diseñado para copiar y pegar directamente al agente IA para depuración sin acceso SSH.
4. **Supervisor en Rust (`comfyctl supervise`) y modo de arranque estricto (`STRICT_START=1`)**:
   - **`comfyctl`** ejecuta `strict-check` (`S1..S10` en perfiles estándar y `S1..S13` en `MODEL_PROFILE=studio`, fuente de verdad en [`comfyctl/src/config.rs`](comfyctl/src/config.rs) `L281-L289`, con `default_min_disk_gb` en `L42-L52`) antes de iniciar descargas: verifica GPU/driver (`>= 570` para CUDA 12.8), VRAM (`>= 20000 MiB` `FATAL`, `< 22000 MiB` `WARN`), RAM (`>= 16384 MiB` con margen del 5% `15564 MiB` para reserva del kernel `FATAL`, `< 32768 MiB` `WARN`), espacio en disco libre por perfil (`180 GB` en `studio`, `75 GB` en `all`, `45 GB` en `general`/`realistic`/`anime`, `10 GB` en `minimal`, `5 GB` con `SKIP_MODEL_DOWNLOAD=1`), escritura en `/workspace/comfyui-data`, `nginx -t` + `.htpasswd`, `ffmpeg` (`libx264`), `/etc/comfyui/nodes.lock`, sonda real `torch.cuda` con timeout de 20s (`S10`), salud de `studio-server` en `127.0.0.1:18190/studio/api/health` (`S11`), salud de MCP (`S12`) y percepción vectorial degradable (`S13`, cuando `MODEL_PROFILE=studio`). Si detecta un host incompatible con `STRICT_START=1`, omite descargas y ComfyUI pero mantiene vivos Nginx, `/home`, `/status` y `/report.md` durante `STRICT_FATAL_GRACE` (`600s`, saliendo después con código `20`) con un banner rojo `FATAL` indicando ejecutar `comfy destroy <id>` para no pagar GPU/disco en una instancia rota.
   - Supervisa `main.py`, escribe atómicamente `/run/comfyui-supervisor.json`, clasifica salidas (`137` OOM, `134`/`139` crash nativo CUDA/C++, `1` excepción Python, `143` SIGTERM), resetea el contador tras `COMFY_HEALTHY_UPTIME` (`600s`) y propaga `SIGTERM` limpiamente a sus subprocesos.
5. **ComfyUI-Manager preinstalado y desbloqueado**:
   - Instalado desde `Comfy-Org/ComfyUI-Manager` con `security_level = weak` (seguro porque ComfyUI escucha solo en `127.0.0.1`) para instalar cualquier custom node o modelo faltante con **un clic** desde la UI.
6. **`ffmpeg` completo de sistema (`libx264` + `h264_nvenc`) y autodiagnóstico `comfyctl diag` con timeouts**:
   - Fija `VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg`, `VHS_USE_IMAGEIO_FFMPEG=0` y `NVIDIA_DRIVER_CAPABILITIES=all`, y ejecuta `comfyctl diag` de forma síncrona antes de la Fase 1 (con timeouts duros por sonda vía `sys::run_command_with_timeout`) para probar GPU, kernel CUDA `matmul` en PyTorch, sesión real `InferenceSession` en `onnxruntime-gpu`, `h264_nvenc` y `libx264`.
7. **Descarga automática resiliente en hasta 3 fases (medidas vía `x-linked-size` en HuggingFace con `hf` + `hf-xet`)**:
   - **Fase 1 (Síncrona antes de abrir ComfyUI, 2.79 GiB)**: Descarga InsightFace `buffalo_l`, `inswapper_128.onnx`, `CodeFormer`, `GFPGANv1.4`, `facexlib` detectors, `YOLO face`, `SAM`, `SDXL VAE fp16-fix`, `SD1.5 VAE`, `4x-UltraSharp` y `4x-AnimeSharp`. Empieza tras el autodiagnóstico `comfyctl diag` (≤ 90 s en el peor caso, suma de timeouts de sonda; normalmente < 15 s) y, mientras dura (1-3 min según red, tope 10 min), Nginx muestra una pantalla de espera con enlaces a `/home` y `/status`.
   - **Fase 2 (Segundo plano, 60.56 GiB en perfil `all`; 13.22 GiB en `studio`)**: Descarga todos los checkpoints populares, ControlNet Union ProMax e IP-Adapter con timeout por intento (`HF_FETCH_TIMEOUT=3600`), 3 reintentos automáticos y `rename()` atómico en el mismo filesystem mientras ya usas ComfyUI. Pulsa **`R`** en el navegador para refrescar los desplegables.
   - **Fase 3 (Segundo plano en `MODEL_PROFILE=studio`, 146.05 GB / 136.02 GiB base en 77 archivos)**: Lee [`config/models-studio.json`](config/models-studio.json) y descarga de forma idempotente y reanudable (con directorio de staging propio `${MODELS_DIR}/.hf_stage_phase3` aislado de la limpieza de Fase 2) el inventario completo del estudio de vídeo y voz: Wan 2.2 (`Comfy-Org/Wan-2.2` + `Comfy-Org/Wan-2.1` para `clip_vision_h` + LoRAs Lightning/Turbo/Relight + VACE 14B fp8_scaled), LatentSync 1.6 (`custom_nodes/ComfyUI-LatentSyncWrapper/checkpoints/`), LivePortrait (`models/liveportrait/`), IndexTTS-2.5 (`${COMFY_DATA_DIR}/indextts/checkpoints/`), `faster-whisper-large-v3` + aligners `wav2vec2` ES/EN, `funasr/campplus`, `CLIP ViT-L/14` e `IP-Adapter SDXL` (`h94/IP-Adapter`, sin pesos FaceID no comerciales). El modelo base de Wan 2.2 Animate (`wan2.2_animate_14B_int8_convrot.safetensors`, +18.41 GB / +17.15 GiB) es opt-in vía `STUDIO_INCLUDE_ANIMATE=1`.

---

## 2. Catálogo de Modelos Verificados (63.35 GiB Total) y Custom Nodes

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
| **`wan_core`** (Wan 2.2 `umt5-xxl fp8`, VAEs 2.1/2.2, `ti2v_5B_fp16`, pares `t2v`/`i2v` 14B fp8 high/low, LoRAs Lightning 4-step, Relight LoRA, `clip_vision_h`, `Comfy-Org/Wan-2.2` y `Comfy-Org/Wan-2.1`) | 14 | `83,184,917,475` | 83.18 GB / 77.47 GiB | Apache-2.0 | `models/{text_encoders,vae,diffusion_models,loras,clip_vision}/` |
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
| **TOTAL FASE 3 POR DEFECTO (`MODEL_PROFILE=studio`)** | **77** | **`146,049,867,640`** | **146.05 GB / 136.02 GiB** | — | — |
| **`animate_opt_in`** (`wan2.2_animate_14B_int8_convrot.safetensors`, solo con `STUDIO_INCLUDE_ANIMATE=1`) | +1 | `+18,413,068,672` | **+18.41 GB / +17.15 GiB** | Apache-2.0 | `models/diffusion_models/` |

> **Nota sobre `STUDIO_INCLUDE_ANIMATE=1`**: El LoRA de relight de Wan 2.2 Animate (`wan2.2_animate_14B_relight_lora_bf16.safetensors`, 1.48 GB) viene incluido por defecto en `wan_core`, pero el modelo base de 14B (`wan2.2_animate_14B_int8_convrot.safetensors`, 18.41 GB) es **opt-in** mediante `-e STUDIO_INCLUDE_ANIMATE=1` para mantener el perfil `studio` por defecto dentro de un alquiler de disco de 220 GB. Si activas `STUDIO_INCLUDE_ANIMATE=1`, alquila al menos **250 GB** de disco en Vast.ai.

### Resumen de Tamaños y Tiempos por Perfil (`MODEL_PROFILE`)

| Perfil (`MODEL_PROFILE`) | Fase 1 | Fase 2 | Fase 3 | Total Modelos | Tiempo a 500 Mbps | Tiempo a 1 Gbps |
|---|---:|---:|---:|---:|---:|---:|
| **`all`** *(por defecto)* | 2.79 GiB | 60.56 GiB | 0.00 GiB | **63.35 GiB** | ~19 min | ~9.5 min |
| **`general`** | 2.79 GiB | 24.96 GiB | 0.00 GiB | **27.75 GiB** | ~8 min | ~4 min |
| **`realistic`** | 2.79 GiB | 24.95 GiB | 0.00 GiB | **27.74 GiB** | ~8 min | ~4 min |
| **`anime`** | 2.79 GiB | 29.43 GiB | 0.00 GiB | **32.22 GiB** | ~9.5 min | ~4.7 min |
| **`minimal`** | 2.79 GiB | 0.00 GiB | 0.00 GiB | **2.79 GiB** | 0 s | 0 s |
| **`studio`** | 2.79 GiB | 13.22 GiB | 136.02 GiB *(146.05 GB)* | **≈ 152.03 GiB** *(≈163.24 GB en disco)* | ~44 min | ~22 min |

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
| `STRICT_START` | `1` | Modo de arranque estricto (`comfyctl strict-check`, fuente de verdad en [`comfyctl/src/config.rs`](comfyctl/src/config.rs) `L281-L289`, con `default_min_disk_gb` en `L42-L52`): valida `S1..S10` (y `S11` `studio_health` en `MODEL_PROFILE=studio`) antes de descargar modelos: GPU visible, driver NVIDIA `>= 570`, VRAM `>= 20000 MiB` (`< 22000 MiB` `WARN`), RAM `>= 16384 MiB` con margen del 5% `15564 MiB` (`< 32768 MiB` `WARN`), disco libre por perfil (`180 GB` en `studio`, `75 GB` en `all`, `45 GB` en `general`/`realistic`/`anime`, `10 GB` en `minimal`, `5 GB` si `SKIP_MODEL_DOWNLOAD=1`), escritura en `/workspace/comfyui-data`, `nginx -t` + `.htpasswd`, `ffmpeg` (`libx264`), `nodes.lock`, sonda `torch.cuda` (`20s` timeout) y `/studio/api/health` (`S11` en `studio`). Si falla con `STRICT_START=1`, aborta descargas y ComfyUI pero mantiene vivos `/home`, `/status` y `/report.md` durante `STRICT_FATAL_GRACE` (`600s`, código de salida `20`). Pon `0` para degradar fallos a `WARN`. |
| `STRICT_MIN_VRAM_MB` / `STRICT_WARN_VRAM_MB` | `20000` / `22000` | Umbrales de VRAM en MiB (`FATAL` si `< STRICT_MIN_VRAM_MB`, `WARN` si `< STRICT_WARN_VRAM_MB`). |
| `STRICT_MIN_RAM_MB` / `STRICT_WARN_RAM_MB` | `16384` / `32768` | Umbrales de RAM en MiB (`FATAL` si `< STRICT_MIN_RAM_MB * 0.95`, `WARN` si `< STRICT_WARN_RAM_MB`). |
| `STRICT_MIN_DISK_GB` | `180` (`studio`), `75` (`all`), `45` (`general`/`realistic`/`anime`), `10` (`minimal`), `5` (`SKIP_MODEL_DOWNLOAD=1`) | Espacio libre mínimo en GiB requerido en `COMFY_DATA_DIR`. |
| `STRICT_MIN_DRIVER` | `570` | Versión mayor mínima del driver NVIDIA (CUDA 12.8) exigida por la comprobación `S2` de `strict-check`. |
| `STRICT_FATAL_GRACE` | `600` | Segundos de gracia tras un `FATAL` con `STRICT_START=1` durante los que se mantienen vivos Nginx, `/home`, `/status` y `/report.md` antes de salir con código `20`. |
| `STRICT_REQUIRE_GPU` | `1` (auto: `0` si `COMFYUI_EXTRA_ARGS` incluye `--cpu`) | Fuerza (`1`) u omite (`0`) la sonda `S10` `cuda_torch` de `strict-check`. |
| `STRICT_REQUIRE_HTPASSWD` | `1` en `comfyctl supervise` (`0` en subcomandos aislados) | Controla si `S7` exige `.htpasswd` ya presente (`fatal` si falta) o lo degrada a `warn`. |
| `MODEL_PROFILE` | `all` | Perfil de modelos: `all` (63.35 GiB), `general` (27.75 GiB), `realistic` (27.74 GiB), `anime` (32.22 GiB), `minimal` (2.79 GiB) o `studio` (Fase 1 2.79 GiB + Fase 2 SDXL/ControlNet 13.22 GiB + Fase 3 136.02 GiB / 146.05 GB ≈ 152.03 GiB / 163.24 GB en disco). Si se introduce un valor desconocido, usa `all` con advertencia. |
| `STUDIO_INCLUDE_ANIMATE` | `0` | En `MODEL_PROFILE=studio`, pon `1` para incluir el modelo base de Wan 2.2 Animate (`wan2.2_animate_14B_int8_convrot.safetensors`, +18.41 GB / +17.15 GiB, total Fase 3 = 78 archivos / 164.46 GB). Requiere alquilar al menos 250 GB de disco en Vast.ai. |
| `STUDIO_LLM_KEY` / `STUDIO_LLM_BASE_URL` / `STUDIO_LLM_MODEL` | *(Vacía)* / `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` / `qwen3-vl-plus` | Configuración del proveedor LLM primario compatible-OpenAI para el agente de Comfy Studio (`MODEL_PROFILE=studio`). `STUDIO_LLM_KEY` se redacta siempre en `/status/api`, `/studio/api/*` y `/report.md`. |
| `STUDIO_LLM_FALLBACK_KEY` / `STUDIO_LLM_FALLBACK_BASE_URL` / `STUDIO_LLM_FALLBACK_MODEL` | *(Vacía)* / `https://api.deepseek.com` / `deepseek-chat` | Proveedor LLM secundario (failover automático ante errores 4xx/5xx/timeout del primario). `STUDIO_LLM_FALLBACK_KEY` es independiente de `STUDIO_LLM_KEY` (fail-closed: nunca reenvía la key primaria a otro proveedor) y se redacta en todas las salidas. |
| `STUDIO_SERVER_PORT` | `18190` | Puerto loopback (`127.0.0.1`) donde `comfy_studio.server` expone `/studio/api/*` cuando `MODEL_PROFILE=studio`. |
| `STUDIO_STUB_LLM` / `STUDIO_STUB_COMFY` | `0` / `0` | Flags de prueba para ejecutar Comfy Studio con cliente LLM determinista in-process (`STUDIO_STUB_LLM=1`) y cola ComfyUI simulada (`STUDIO_STUB_COMFY=1`) en tests y smoke CI sin llamadas de red externas. |
| `STUDIO_RESUME_JOBS` / `STUDIO_RESUME_DELAY_S` | `1` / `30` | Recuperación durable de jobs (M3-B): al arrancar, los jobs huérfanos en vuelo (`queued`/`running`/`cancelling`/`fetching`) se marcan `interrupted` y, con `STUDIO_RESUME_JOBS=1`, los jobs `render` elegibles se reencolan **una sola vez** (`interrupt_retries ≤ 1`) tras `STUDIO_RESUME_DELAY_S` segundos (espera el arranque de ComfyUI). Pon `0` para desactivar por completo la recuperación: es **inerte**, no altera el estado de ningún job (la reconciliación queda en tus manos). |
| `STUDIO_RECOVERY_DRAIN_S` | `10` | Segundos que el apagado espera a que termine una recuperación en curso antes de cancelarla. Si expira, los jobs en vuelo se marcan `interrupted` explícitamente para que no queden sin estado terminal. |
| `STUDIO_FFMPEG_TIMEOUT_S` | `60` | Timeout base por subprocess `ffmpeg`. En EDL el límite de cada corte se escala con su duración: `max(base, min(1800, 4 × duración + base))`. Súbelo si renderizas clips largos en hosts con CPU lenta. |
| `STUDIO_FFPROBE_TIMEOUT_S` | `20` | Timeout por sonda `ffprobe` (medición de duración, dimensiones y streams). |
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
| `COMFYUI_EXTRA_ARGS` | *(Vacío)* | Flags adicionales para `python3 main.py` de ComfyUI. Ejemplo útil en RTX 5090 ante errores `HostBuffer` / OOM inesperados: `--disable-pinned-memory` (ver sección 5.7). |
| `COMFY_MAX_RESTARTS` | `5` | Número máximo de reinicios automáticos de ComfyUI ante caídas antes de pasar a estado `exhausted` (manteniendo Nginx, `/home` y `/status` activos para diagnóstico). |
| `COMFY_RESTART_DELAY` | `5` | Segundos de espera entre caídas de ComfyUI antes de que el supervisor relance `main.py`. |
| `COMFY_HEALTHY_UPTIME` | `600` | Segundos de funcionamiento continuo tras los cuales el supervisor resetea `restarts` a `0` e incrementa `healthy_resets`. |
| `LOG_MAX_MB` | `200` | Tamaño máximo en MB por fichero de log en `/var/log` antes de truncar conservando los últimos `50 MB`. Cubre `comfyui.log`, los logs `access`/`error` de nginx, `download-models.log`, `start.log`, `supervisor.log`, `ttyd.log` y `diag.txt`. Los logs sensibles (`start.log` —contiene el banner con la contraseña de arranque—, `supervisor.log`, `ttyd.log`, `diag.txt`) se crean con permisos `600` (solo root); `comfyui.log` y `download-models.log` son `644` porque los sirve nginx. |
| `HF_FETCH_TIMEOUT` | `3600` | Timeout máximo en segundos por intento de descarga individual en `download-models.sh`. |
| `HF_HUB_DISABLE_XET` | *(Vacío)* | Pon `1` para que `hf download` use HTTP clásico en lugar del backend `hf-xet`. Útil si la Fase 2 se queda colgada, devuelve muchos 503 o el host tiene poca RAM (ver sección 5.8). |
| `ENABLE_WEB_TERMINAL` | `0` | Pon `1` para activar `ttyd` en `127.0.0.1:7681`, accesible tras Basic Auth en `/terminal/`. Desactivado por defecto por seguridad. |
| `STATUS_SERVER_PORT` | `18189` | Puerto loopback (`127.0.0.1`) donde `comfyctl serve` expone `/status`, `/status/api`, `/report.md`, `/diag` y `/logs/<nombre>`. Controla de verdad el bind por defecto del servidor (`127.0.0.1:{port}`). |
| **Rutas internas (avanzado)** | — | `COMFY_APP_DIR` (`/opt/ComfyUI`), `STUDIO_DATA_DIR` (`${COMFY_DATA_DIR}/studio`), `STUDIO_COMFYUI_URL` (`http://127.0.0.1:18188`), `COMFY_SUPERVISOR_JSON` (`/run/comfyui-supervisor.json`), `COMFY_STRICT_JSON` (`/run/comfyui-strict.json`), `COMFY_STARTED_AT_FILE` (`/run/comfyui-started-at`), `COMFY_IMAGE_JSON` (`/etc/comfyui/image.json`), `COMFY_HTPASSWD_PATH` (`/etc/nginx/.htpasswd`) y `COMFY_NVIDIA_SMI_BIN` (`nvidia-smi`). Todas tienen defaults correctos y raramente se tocan (útiles sobre todo para tests). |
| **Variables de plataforma Vast.ai (solo lectura)** | `local` / `8188` | `VAST_CONTAINERLABEL` y `VAST_TCP_PORT_8188` las inyecta Vast.ai al alquilar la instancia; `comfyctl` las lee (`config.rs` `L308-L309`) para exponer `instance.vast_instance_id` y `instance.external_port_8188` en `/status/api` y `/report.md` (`collect/mod.rs` `L123-L124`). Fuera de Vast.ai usan los valores por defecto. Del mismo grupo, `COMFY_PASS_AUTOGENERATED` (`0`/`1`) es un marcador interno que escribe `start.sh` (`L163-L175`) cuando autogenera la contraseña, para que `comfyctl` muestre `<autogenerada — ver logs del contenedor>` en lugar de la clave. |

---

## 4. Guía de Uso (Desde tu MacBook, 100% Web)

La guía está dividida en dos partes: **A. Primera vez** (configuración que se hace una sola vez) y **B. Cada sesión** (lo que repites cada vez que alquilas una GPU). Los tiempos citados provienen de ejecuciones reales del workflow de CI (runner `ubuntu-24.04` con `buildcache`).

### A. Primera vez

#### A1. Publicar la imagen en GitHub Container Registry (gratis, sin Docker en tu Mac)

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
6. **Billing → Add Credit**: Vast.ai cobra contra saldo prepagado (tarjeta o cripto). Para una sesión de pruebas de 2-3 horas con una RTX 5090 bastan 5-10 USD.
7. Opcional pero recomendado: **Account → API Keys → New** para poder usar el CLI `vastai` desde la Terminal de la Mac (ver A4).

#### A3. Crear la plantilla en Vast.ai

8. Entra en [cloud.vast.ai/templates](https://cloud.vast.ai/templates/) → **New Template** y rellena:

   | Campo | Valor |
   |---|---|
   | **Image Path:Tag** | `ghcr.io/<tu-usuario>/<tu-repo>:latest` |
   | **Docker Options** | `-p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e COMFY_USER=admin -e MODEL_PROFILE=all` y, si quieres contraseña fija, `-e COMFY_PASS=TuClaveLargaUnica123` |
   | **Launch Mode** | **Docker ENTRYPOINT** |
   | **Disk Space** | `120` GB para `MODEL_PROFILE=all` (suficiente: 63 GiB de modelos + ~16 GB de imagen; `strict-check` S5 pasa) o `220` GB para `MODEL_PROFILE=studio` (`250` GB con `STUDIO_INCLUDE_ANIMATE=1`). Si acumulas mucho output local (vídeos/upscales), alquila a mano con `--disk 150+`. Para `general`/`realistic`/`anime` bastan 80 GB; 40 GB para `minimal`. |

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

13. La primera vez que levantes una instancia, recorre la lista de verificaciones de [`docs/auditorias/AUDITORIA_AGENTE_V5.md`](docs/auditorias/AUDITORIA_AGENTE_V5.md) (sección 3) y anota en [`docs/decisiones.md`](docs/decisiones.md): GPU, driver, tiempo de descarga de la imagen, duración de la Fase 1 y de la Fase 2, y resultado de cada punto. A partir de ahí, el proyecto se considera en producción.

---

### B. Cada sesión

#### B1. Buscar y alquilar la GPU (5 minutos)

1. En **Search**, con tu plantilla seleccionada, aplica estos filtros:

   | Filtro | Valor | Por qué |
   |---|---|---|
   | GPU | **RTX 5090** (32 GB) o **RTX 4090** (24 GB), 1 GPU | Misma imagen `cu128` para ambas |
   | **CUDA** (`cuda_vers`) | **≥ 12.8** | Versión máxima de CUDA soportada por el host (campo documentado de la API); con menos, `torch` cu128 no inicializa la GPU |
   | **GPU RAM** | ≥ 24 GB | La imagen exige ≥ 20000 MiB (`strict-check` `S3`); el helper filtra `gpu_ram>=24` |
   | **CPU RAM** | ≥ 32 GB | Las descargas `hf-xet` y la carga de FLUX fp8 consumen RAM; el helper filtra `cpu_ram>=32` |
   | **Disk Space** | ≥ 120 GB | El disco se fija al alquilar; no se amplía después. 120 GB cubren `all` (63 GiB modelos + ~16 GB imagen) |
   | **Internet Download** (`inet_down`) | ≥ 500 Mbps (ideal ≥ 1 Gbps) | 63 GB de modelos: ~19 min a 500 Mbps, ~1 h a 150 Mbps |
   | **Reliability** | ≥ 0.98, *verified* | Menos cortes a mitad de sesión |

   > Los puertos van **proxy-mapeados** por Vast.ai (no se requieren *direct ports*): Vast asigna un puerto del host y lo redirige al 8188 del contenedor. Es lo que hace `comfy rent` con `--args` (runtype args, que respeta el ENTRYPOINT de la imagen).

2. **Lee la tarjeta de la oferta antes de pulsar Rent**, no solo el $/h:
   - **RAM del host**: evita hosts con menos de 32 GB si usas `MODEL_PROFILE=all` (las descargas con `hf-xet` y la carga de FLUX fp8 de 16 GB consumen RAM; un host justo puede matar ComfyUI por falta de memoria).
   - **$/GB/mes de almacenamiento** y **$/TB de tráfico**: con 63 GB descargados por sesión, en hosts caros el tráfico puede costar más que la primera hora de GPU.
   - **On-demand** para sesiones de trabajo; *interruptible* es más barato pero otro usuario puede quitarte la máquina.
3. Pulsa **Rent** (o ejecuta `comfy rent all` desde tu Mac). Precios orientativos (2026-10): RTX 5090 ~$0.45–$0.95/h on-demand; RTX 4090 ~$0.30–$0.55/h. El precio exacto se ve en `comfy rent` antes de confirmar.

*(Referencia CLI equivalente — los mismos filtros que usa el helper [`tools/mac/comfy`](tools/mac/comfy))*:
```bash
vastai search offers "gpu_name=RTX_5090 num_gpus=1 gpu_ram>=24 cpu_ram>=32 cuda_vers>=12.8 inet_down>=500 disk_space>=120 reliability>=0.98 verified=true rentable=true" -o "dph_total+"

vastai create instance <OFFER_ID> \
  --image ghcr.io/<tu-usuario>/<tu-repo>:latest \
  --disk 120 \
  --label comfy \
  --env '-p 8188:8188 -e MODEL_PROFILE=all -e COMFY_USER=admin -e COMFY_PASS=TuClaveLargaUnica123' \
  --args
```
> `--args` fija el runtype `args` (respeta el ENTRYPOINT de la imagen) y los puertos van proxy-mapeados; no se usa `--direct`. En `--env`, el entrecomillado es **solo del valor** (`-e CLAVE="valor"`): el parser de vast-cli limpia comillas del valor, nunca de la clave.

#### B2. Esperar el arranque (de 5 a 15 minutos la primera vez en un host)

4. En **Instances**, la tarjeta pasa por *Loading / Creating* mientras el host descarga la imagen (~7-8 min medidos en el runner de CI para la imagen de 15.6 GB: ~218 s el pull de la candidata + ~198 s las capas base; en el host de Vast depende de su red). Si el host ya tenía la imagen en caché de una sesión anterior, es casi inmediato.
5. Cuando pase a **Running**, aparece el botón **Open** y la dirección `IP:PUERTO` (o ejecuta `comfy` en tu Mac para abrir directamente `/home`). **El puerto externo no es 8188**: Vast.ai asigna un puerto aleatorio del host y lo redirige al 8188 del contenedor. Usa siempre la dirección que muestra la tarjeta.
6. Cómo saber en qué punto está el arranque:
   - Botón **Logs** de la tarjeta (o `comfy logs` / `comfy pass`): si está vacío, la imagen aún se está descargando; si ves `[1/7] Verificando instalación de ffmpeg…`, `start.sh` ya corre. Ahí aparece también la contraseña si dejaste que se autogenere.
   - Al pulsar **Open**, el navegador pide usuario y contraseña (HTTP Basic Auth). Tras introducirlos verás la pantalla de espera `_starting.html` mientras corre la Fase 1 (1-3 minutos; tope 10) con enlaces directos al **Portal (`/home`)** y al **Panel de Estado (`/status`)**.
   - En cuanto ComfyUI abre su puerto, la misma pestaña muestra la interfaz. La Fase 2 (60 GB) sigue descargando en segundo plano; síguela en `/status` y pulsa **R** en ComfyUI para refrescar los desplegables.

#### B3. Correr el primer workflow

7. **Elige el checkpoint correcto.** El workflow por defecto de ComfyUI apunta a `v1-5-pruned-emaonly.safetensors`, que **no está en este catálogo**. En `Load Checkpoint` selecciona `Realistic_Vision_V6.0_NV_B1_fp16` (SD 1.5, es de los primeros en llegar en la Fase 2) o `Juggernaut-XI-byRunDiffusion` (SDXL). Si pulsas Queue sin cambiarlo verás `value not in list`.
8. Orden recomendado de pruebas en una sesión nueva: (a) checkpoint + KSampler 512×512, 20 pasos, para confirmar CUDA y VAE; (b) añadir **ReActor** sobre el resultado, para confirmar `inswapper` y ONNX Runtime en GPU; (c) `VHS Load Video` + `Video Combine` con formato `video/h264-mp4`, para confirmar `h264_nvenc`; (d) **FLUX fp8** solo cuando `/status` muestre que terminó (16 GB, es lo último que baja).
9. Para cargar tus propios workflows: arrastra el `.json` o `.png` desde el Finder sobre el lienzo. Las plantillas oficiales están en **Workflow → Browse Templates**; si una pide un modelo que falta, ComfyUI muestra el diálogo *Missing models* con botón de descarga.
10. Sube vídeos con `VHS_LoadVideo`; no hay límite de tamaño en el proxy.

#### B3.1 Runbook S3 — Percepción Visual, Transcripción/Diarización y EDL en Comfy Studio (`MODEL_PROFILE=studio`)

Cuando alquilas con `comfy rent studio` y abres `comfy studio` (`/studio/`), puedes ejecutar el flujo completo de indexación y montaje automático desde la pestaña **Percepción & EDL** o pidiéndoselo al agente conversacional:

> **Modo de inferencia actual**: los pasos 1–3 funcionan de extremo a extremo (assets, keyframes reales con `ffmpeg`, índice `sqlite-vec`, identidades, transcripciones y EDL), pero los **modelos** de percepción/ASR **no se cargan**: los embeddings, detecciones faciales, textos y turnos de hablante son simulaciones deterministas locales. El paso 4 (**render EDL**) sí es real: recorta y concatena con `ffmpeg`, y **falla** si `ffprobe` no puede verificar el MP4 resultante.

1. **Subir brutos e indexar keyframes (`sqlite-vec`)**:
   - Arrastra tus clips MP4/MOV/WAV o fotos de referencia a la barra lateral **Assets del Proyecto**.
   - En la pestaña **Percepción & EDL**, selecciona el vídeo y pulsa **Extraer Keyframes** (`POST /studio/api/projects/{id}/keyframes/extract`) e **Indexar** (`POST /studio/api/projects/{id}/perception/index`), o escribe en el chat *"extrae los keyframes e indexa los medios del proyecto"* (`extract_keyframes`, `index_media`). Las miniaturas con timestamps exactos aparecen en la cuadrícula de keyframes y sus embeddings quedan almacenados en `STUDIO_DATA_DIR/index.db`. Si `ffmpeg` no puede extraer fotogramas de un clip, el marcador de posición se guarda con `synthetic: true` y **se excluye del índice**.
2. **Detectar rostros, nombrar identidades y buscar planos (`texto`, `cara` o `texto ∩ persona`)**:
   - Pulsa **Detectar Caras** (`POST /studio/api/projects/{id}/faces/index`) sobre los vídeos o fotos de referencia y registra/renombra identidades (`POST /studio/api/projects/{id}/identities`, `PATCH /studio/api/projects/{id}/identities/{identity_id}`).
   - Busca planos por descripción textual (`find_shots_text`), por identidad/foto de referencia (`find_shots_face`) o combinando ambos (`POST /studio/api/projects/{id}/search/combined`, p. ej. *"Persona A hablando frente a pizarra"*).
   - > **Aviso de licencia no comercial (`InsightFace buffalo_l`)**: cuando se integren los pesos reales del paquete `InsightFace buffalo_l` (`det_10g.onnx`, `w600k_r50.onnx`), su licencia **no comercial (NC)** restringe el uso del índice facial a investigación y proyectos no comerciales salvo acuerdo comercial directo con el titular (los pesos `IP-Adapter-FaceID` siguen excluidos del catálogo Fase 3 por el mismo motivo). El aviso acompaña ya a todas las respuestas faciales.
3. **Transcribir y diarizar (simulación determinista; sin `HF_TOKEN`)**:
   - Pulsa **Transcribir Audio** (`POST /studio/api/projects/{id}/transcribe`, herramienta `transcribe`) para obtener palabras con timestamps (`start_sec`, `end_sec`) y detección de muletillas en ES/EN. La respuesta incluye `simulated: true`.
   - Pulsa **Diarizar Hablantes** (`POST /studio/api/projects/{id}/diarize`, herramienta `diarize`) para etiquetar cada segmento y palabra por hablante (`SPEAKER_00`, `SPEAKER_01`, ...). No requiere `pyannote` **ni `HF_TOKEN`**, y la respuesta también incluye `simulated: true`.
4. **Generar EDL de Highlights y renderizar entregables `16:9` y `9:16` con `ffmpeg` (real)**:
   - Pulsa **Generar Highlights EDL** (`POST /studio/api/projects/{id}/edl/highlights`, o pide al agente `propose_edl`) para crear una nueva versión de EDL (`comfy_studio.edl.v1`) que recorta pausas (`>= min_pause_sec`) y muletillas (`remove_fillers=true`).
   - Revisa o ajusta los cortes (`in_sec`, `out_sec`, `speaker`, `reason`) en el timeline editable de la pestaña **Percepción & EDL**, elige el formato de entrega (`16:9` `1280x720` o `9:16` `720x1280`) y pulsa **Renderizar MP4** (`POST /studio/api/projects/{id}/edl/{version}/render`, herramienta `build_edl`). El MP4 concatenado se verifica con `ffprobe` y solo entonces se guarda en la **Galería**; si la verificación falla, el render devuelve error en lugar de un entregable con metadatos inventados. Los timeouts de `ffmpeg`/`ffprobe` son configurables (`STUDIO_FFMPEG_TIMEOUT_S`, `STUDIO_FFPROBE_TIMEOUT_S`).

#### B4. Si algo falla durante la sesión (Observabilidad y Diagnóstico IA)

11. **Panel `/status` y Portal `/home`**: Abre `http://<IP>:<PUERTO>/status` (o `http://<IP>:<PUERTO>/home`) para ver en vivo el estado del supervisor de ComfyUI, uso de VRAM/RAM/Disco, progreso de descargas, alertas automáticas de triaje con remediación paso a paso y pestañas de logs (`comfyui`, `start`, `download-models`, `supervisor`, `diag`, `nginx-error`).
12. **Informe para el Agente IA (`/report.md`)**: Pulsa **"Copiar informe para IA (`report.md`)"** en `/home` o `/status` (o ejecuta `comfy report` en tu Mac) y pega el Markdown resultante en el chat del agente. Incluye autodiagnóstico de GPU/PyTorch/ONNX/NVENC, alertas detectadas, deriva de paquetes Python respecto a `requirements.lock`, commits de `custom_nodes` vs `nodes.lock`, inventario de modelos en disco y colas de todos los logs (con secretos redactados).
13. Consulta también la sección 5 (problemas conocidos: OOM en RTX 5090, descargas colgadas, `IMPORT FAILED`).

#### B5. Cerrar la sesión

14. **Descarga tus resultados** desde la UI: clic derecho sobre la salida de `Video Combine` / `Save Image` (o en **Queue → History**) → **Save**. Todo lo que quede en `/workspace/comfyui-data/output` se pierde con la instancia.
15. Pulsa **Destroy** (icono de papelera), **no Stop**. Con *Stop* sigues pagando el almacenamiento (120 GB) y, si el host alquila esa GPU a otro usuario mientras tanto, no podrás reanudar la instancia. Este proyecto está diseñado para descargar los modelos en cada arranque, así que *Destroy* es el cierre normal.
16. Si en el futuro quieres conservar los modelos entre sesiones, Vast.ai ofrece **Volumes** persistentes que podrían montarse en `/workspace/comfyui-data`; la imagen ya está preparada para ello (los datos viven fuera de `/opt/ComfyUI`). Es una decisión pendiente (OPS-04), no una función activa.

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

### 4. Rendimiento con `ReActor` + `CodeFormer` + `FLUX` en vídeo (RTX 5090 32 GB vs RTX 4090 24 GB)
- **Ventaja de los 32 GB GDDR7**: Combinar difusión pesada (SDXL / FLUX.1-dev FP8 de 16.06 GiB) + `ControlNet Union` + `ReActor` + `CodeFormer` cabe simultáneamente en los **32 GB de VRAM de la RTX 5090** sin intercambiar modelos con la RAM del sistema (en RTX 4090 de 24 GB también funciona a gran velocidad para SDXL/SD1.5 y mediante gestión automática de VRAM de ComfyUI para FLUX).

### 5. Modelos de detección de InsightFace (`buffalo_l`, `inswapper_128.onnx`)
- **Solución aplicada**: La Fase 1 de [`scripts/download-models.sh`](scripts/download-models.sh) descarga y descomprime `buffalo_l.zip` en `/workspace/comfyui-data/models/insightface/models/buffalo_l/` y crea el enlace en `/root/.insightface/models/buffalo_l`, junto con `inswapper_128.onnx`, `codeformer-v0.1.0.pth`, `GFPGANv1.4.pth`, `detection_Resnet50_Final.pth` y `parsing_parsenet.pth`.

### 6. Actualizaciones de ComfyUI y `PIP_CONSTRAINT` (BAJA-09 / BAJA-12)
- **Versiones congeladas**: ComfyUI y los 12 custom nodes preinstalados se clonan en commits verificados en [`config/nodes.lock`](config/nodes.lock). Para actualizar ComfyUI core a una nueva versión, actualiza el SHA en `config/nodes.lock` y ejecuta el workflow de GitHub Actions en lugar de usar "Update ComfyUI" desde la UI.
- **Protección `PIP_CONSTRAINT`**: La variable `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` está activa en runtime para impedir que un custom node instalado desde ComfyUI-Manager degrade `torch` (`2.9.1+cu128`) o rompa `opencv-python-headless` / `onnxruntime-gpu`. Si algún nodo experimental requiere levantar esa restricción temporalmente, puedes pasar `-e PIP_CONSTRAINT=""` en las opciones del contenedor.
- **No uses "Update ComfyUI" desde Manager**: `/opt/ComfyUI` es un clon superficial fijado por SHA; el botón de actualización del core puede fallar o dejar el frontend (`comfyui-frontend-package`) desalineado con el servidor. Instalar o actualizar *custom nodes* desde Manager sí está soportado.

### 7. OOM o `HostBuffer.read_file_slice failed` en RTX 5090 con modelos grandes
- **Síntoma**: Con Wan, FLUX.2 u otros modelos pesados, ComfyUI falla en todas las generaciones con un error de CUDA que menciona `HostBuffer`, o se queda sin memoria aunque la VRAM no esté llena. Son fallos conocidos de la gestión dinámica de VRAM y de la memoria *pinned* en Blackwell (ComfyUI issues #15255 y #14250, 2026), reportados a NVIDIA.
- **Primer intento**: añade `-e COMFYUI_EXTRA_ARGS=--disable-pinned-memory` a las Docker Options de la plantilla y reinicia la instancia. Si tienes varias GPU en el host, `--cuda-device 0` también está documentado como alternativa.
- **Sesiones largas en RTX 5090 con cargas mixtas** (imagen + vídeo + upscaling seguidos): la VRAM se fragmenta con el tiempo. Puedes añadir `-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` a las Docker Options para mitigar la fragmentación del allocator de CUDA (opcional; no afecta a cargas cortas).
- **Si el contenedor se reinicia o se apaga solo**: es un OOM de **RAM del sistema** (el kernel mata a `python3` con código 137). Elige un host con más RAM (≥ 32 GB con `MODEL_PROFILE=all`), o usa un perfil menor para que la Fase 2 no coincida con la carga de FLUX.

### 8. La Fase 2 se queda colgada o devuelve muchos errores 503
- **Causa probable**: el backend `hf-xet` que usa `hf download` por defecto dimensiona sus buffers con constantes fijas, no según la RAM disponible, y en contenedores con poca memoria puede provocar OOM, más errores 503 o descargas detenidas (huggingface_hub #3300, xet-core #927 y #581).
- **Solución**: añade `-e HF_HUB_DISABLE_XET=1` a las Docker Options para forzar HTTP clásico. Es algo más lento pero estable. Los modelos ya descargados se conservan (`hf_fetch` salta los archivos existentes) y los parciales se reanudan en el siguiente arranque.

### 9. Un nodo aparece con `IMPORT FAILED` o `no kernel image is available for sm_120`
- **Si pasa en la imagen recién arrancada**: no debería; el build falla si cualquiera de los 12 nodos no importa. Reporta el log completo.
- **Si pasa tras instalar algo desde Manager**: casi siempre un nodo arrastró su propio `torch` o `opencv` y pisó los fijados. Compara `pip3 freeze` con `/etc/comfyui/requirements.lock` (el artefacto `requirements-lock` del workflow es idéntico) para ver qué cambió, y reinstala la terna con `pip3 install --force-reinstall torch==2.9.1+cu128 torchvision==0.24.1+cu128 torchaudio==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128`. Mientras `PIP_CONSTRAINT` siga activo esto no debería ocurrir; ocurre si se desactivó con `-e PIP_CONSTRAINT=""`.
