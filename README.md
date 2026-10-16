# ComfyUI "Everything-Ready" Executor para Vast.ai (RTX 5090 32 GB / RTX 4090 24 GB — 100% Web)

Despliegue efímero de **ComfyUI** en **Vast.ai** optimizado para **NVIDIA RTX 5090 (32 GB GDDR7 VRAM, arquitectura Blackwell `sm_120` con CUDA 12.8)** —y totalmente compatible con **RTX 4090 (24 GB, Ada `sm_89`)**—, diseñado para abrir en el navegador de tu **MacBook**, iniciar sesión con usuario/contraseña (HTTP Basic Auth), arrastrar cualquier workflow (`.json` o `.png`) y ejecutarlo sin tocar rutas ni escribir código.

> Consulta [`docs/decisiones.md`](docs/decisiones.md) para ver la tabla de investigación web verificada (fecha `2026-10-16`) y el registro completo de decisiones de seguridad, reproducibilidad y arquitectura, o [`docs/walkthrough.md`](docs/walkthrough.md) para el desglose archivo por archivo.

---

## 1. Características Principales

1. **Soporte nativo para RTX 5090 (32 GB, Blackwell `sm_120`) y RTX 4090 (24 GB, Ada `sm_89`)**:
   - Basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` con la terna **PyTorch `2.9.1+cu128`** fijada y **`onnxruntime-gpu`** + **`opencv-python-headless`** protegidos mediante [`config/constraints.txt`](config/constraints.txt) y [`config/nodes.lock`](config/nodes.lock), más registro de librerías CUDA/cuDNN en `ldconfig`.
   - Conserva `build-essential` (`gcc`/`g++`) y `python3-dev` en la imagen para que `torch.compile` (Triton) y los custom nodes con extensiones C/C++ instalados desde ComfyUI-Manager funcionen en tiempo de ejecución.
2. **Separación segura `/opt/ComfyUI` (código) y `/workspace/comfyui-data` (datos)**:
   - ComfyUI y los 10 custom nodes están instalados en `/opt/ComfyUI`, mientras que `models/` (incluyendo `models/configs/*.yaml`), `input/`, `output/` y `user/` residen en `/workspace/comfyui-data`. Si montas un volumen persistente en `/workspace` en Vast.ai, **no se oculta la instalación** y los modelos descargados se reutilizan entre sesiones.
3. **Acceso directo por navegador con HTTP Basic Auth (Nginx)**:
   - ComfyUI escucha únicamente en loopback (`127.0.0.1:18188`) y **Nginx** (`user www-data;`) protege el puerto público `8188` con usuario/contraseña (`COMFY_USER` y `COMFY_PASS`), pantalla de espera auto-recargable (`_starting.html`) durante la Fase 1, soporte WebSockets (`/ws` sin rate-limit), subida de vídeos sin límite (`client_max_body_size 0;`), `/healthz` (`HEAD /system_stats` sin exponer metadatos) y visor de descargas en vivo en `/download-status`.
4. **ComfyUI-Manager preinstalado y desbloqueado**:
   - Instalado desde `Comfy-Org/ComfyUI-Manager` con `security_level = weak` (seguro porque ComfyUI escucha solo en `127.0.0.1`) para instalar cualquier custom node o modelo faltante con **un clic** desde la UI.
5. **`ffmpeg` completo de sistema (`libx264` + `h264_nvenc`)**:
   - Fija `VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg`, `VHS_USE_IMAGEIO_FFMPEG=0` y `NVIDIA_DRIVER_CAPABILITIES=all`, verificando encoders al arrancar en [`scripts/start.sh`](scripts/start.sh) para evitar `OSError: [Errno 22] Invalid argument` en `VHS_VideoCombine`.
6. **Descarga automática resiliente en 2 fases (63.35 GiB reales medidos en HuggingFace con `hf` + `hf-xet`)**:
   - **Fase 1 (Síncrona antes de abrir ComfyUI, 2.79 GiB)**: Descarga InsightFace `buffalo_l`, `inswapper_128.onnx`, `CodeFormer`, `GFPGANv1.4`, `facexlib` detectors, `YOLO face`, `SAM`, `SDXL VAE fp16-fix`, `SD1.5 VAE`, `4x-UltraSharp` y `4x-AnimeSharp`. Mientras dura (~25–60s según red), Nginx muestra una pantalla de espera con auto-recarga y enlace a `/download-status`.
   - **Fase 2 (Segundo plano, 60.56 GiB en perfil `all`)**: Descarga todos los checkpoints populares, ControlNet Union ProMax e IP-Adapter con 3 reintentos automáticos y `rename()` atómico en el mismo filesystem mientras ya usas ComfyUI. Pulsa **`R`** en el navegador para refrescar los desplegables.

---

## 2. Catálogo de Modelos Verificados (63.35 GiB Total) y Custom Nodes

### Fase 1 — Modelos críticos ligeros (2.79 GiB)

| Categoría | Archivo en ComfyUI | Tamaño real | Repo HuggingFace (`Gated: False`) | Carpeta destino |
|---|---|---:|---|---|
| **InsightFace Pack** | `buffalo_l` (`det_10g.onnx`, `w600k_r50.onnx`, etc.) | 0.27 GiB | `Gourieff/ReActor` (`dataset`) | `models/insightface/models/buffalo_l/` |
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
| **General / Fotorrealismo SDXL** | `Juggernaut-XI-byRunDiffusion.safetensors` | 6.62 GiB | `all`, `general`, `realistic` | `RunDiffusion/Juggernaut-XI-v11` | `models/checkpoints/` |
| **Fotorrealismo SDXL** | `RealVisXL_V5.0_fp16.safetensors` | 6.46 GiB | `all`, `realistic` | `SG161222/RealVisXL_V5.0` | `models/checkpoints/` |
| **General / Vídeo SD 1.5** | `Realistic_Vision_V6.0_NV_B1_fp16.safetensors` | 1.99 GiB | `all`, `general`, `realistic` | `SG161222/Realistic_Vision_V6.0_B1_noVAE` | `models/checkpoints/` |
| **Ilustración / Anime SDXL** | `waiIllustriousSDXL_v160.safetensors` | 6.46 GiB | `all`, `general`, `anime` | `Ine007/waiIllustriousSDXL_v160` | `models/checkpoints/` |
| **Ilustración / Anime SDXL** | `Illustrious-XL-v2.0.safetensors` | 6.46 GiB | `all`, `anime` | `OnomaAIResearch/Illustrious-XL-v2.0` | `models/checkpoints/` |
| **Ilustración / Anime SDXL** | `NoobAI-XL-v1.1.safetensors` | 6.62 GiB | `all`, `anime` | `Laxhar/noobai-XL-1.1` | `models/checkpoints/` |
| **Next-Gen SOTA (FP8)** | `flux1-dev-fp8.safetensors` | 16.06 GiB | `all` | `Comfy-Org/flux1-dev` | `models/checkpoints/` |
| **ControlNet Union ProMax** | `controlnet-union-sdxl-promax.safetensors` | 2.34 GiB | `all`, `general`, `realistic`, `anime` | `xinsir/controlnet-union-sdxl-1.0` | `models/controlnet/` |
| **IP-Adapter SDXL (x2)** | `ip-adapter-plus_sdxl_vit-h.safetensors`, `ip-adapter-plus-face_sdxl_vit-h.safetensors` | 1.58 GiB | `all`, `general`, `realistic`, `anime` | `h94/IP-Adapter` | `models/ipadapter/` |
| **IP-Adapter SD 1.5 (x2)** | `ip-adapter-plus_sd15.safetensors`, `ip-adapter-plus-face_sd15.safetensors` | 0.18 GiB | `all`, `general`, `realistic`, `anime` | `h94/IP-Adapter` | `models/ipadapter/` |
| **CLIP Vision ViT-H** | `CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | 2.35 GiB | `all`, `general`, `realistic`, `anime` | `h94/IP-Adapter` | `models/clip_vision/` |
| **CLIP Vision ViT-bigG** | `CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors` | 3.44 GiB | `all`, `general`, `realistic`, `anime` | `h94/IP-Adapter` | `models/clip_vision/` |

### Resumen de Tamaños y Tiempos por Perfil (`MODEL_PROFILE`)

| Perfil (`MODEL_PROFILE`) | Fase 1 | Fase 2 | Total Modelos | Tiempo Fase 2 a 500 Mbps | Tiempo Fase 2 a 1 Gbps |
|---|---:|---:|---:|---:|---:|
| **`all`** *(por defecto)* | 2.79 GiB | 60.56 GiB | **63.35 GiB** | ~19 min | ~9.5 min |
| **`general`** | 2.79 GiB | 24.96 GiB | **27.75 GiB** | ~8 min | ~4 min |
| **`realistic`** | 2.79 GiB | 24.95 GiB | **27.74 GiB** | ~8 min | ~4 min |
| **`anime`** | 2.79 GiB | 29.43 GiB | **32.22 GiB** | ~9.5 min | ~4.7 min |
| **`minimal`** | 2.79 GiB | 0.00 GiB | **2.79 GiB** | 0 s | 0 s |

### Custom Nodes Preinstalados (Fijados por Commit SHA en [`config/nodes.lock`](config/nodes.lock))

- [`Comfy-Org/ComfyUI-Manager`](https://github.com/Comfy-Org/ComfyUI-Manager)
- [`Kosinkadink/ComfyUI-VideoHelperSuite`](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite)
- [`Gourieff/ComfyUI-ReActor`](https://github.com/Gourieff/ComfyUI-ReActor)
- [`ltdrdata/ComfyUI-Impact-Pack`](https://github.com/ltdrdata/ComfyUI-Impact-Pack) + [`ComfyUI-Impact-Subpack`](https://github.com/ltdrdata/ComfyUI-Impact-Subpack) (con `sam2` fijado por SHA)
- [`Fannovel16/comfyui_controlnet_aux`](https://github.com/Fannovel16/comfyui_controlnet_aux)
- [`cubiq/ComfyUI_IPAdapter_plus`](https://github.com/cubiq/ComfyUI_IPAdapter_plus)
- [`Fannovel16/ComfyUI-Frame-Interpolation`](https://github.com/Fannovel16/ComfyUI-Frame-Interpolation)
- [`rgthree/rgthree-comfy`](https://github.com/rgthree/rgthree-comfy)
- [`kijai/ComfyUI-KJNodes`](https://github.com/kijai/ComfyUI-KJNodes)

---

## 3. Variables de Entorno Soportadas

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `OPEN_BUTTON_PORT` | `8188` | Puerto que abre el botón azul **Open** en la consola web de Vast.ai. |
| `COMFY_USER` | `admin` | Usuario para HTTP Basic Auth en Nginx (puerto 8188). |
| `COMFY_PASS` | *(Autogenerada de 24 hex chars si se omite)* | Contraseña para HTTP Basic Auth. Si la defines, **no se imprime en los logs**; si la omites, se genera una clave aleatoria única con `openssl rand -hex 12` y se muestra una vez en el log de arranque. |
| `MODEL_PROFILE` | `all` | Perfil de modelos: `all` (63.35 GiB), `general` (27.75 GiB), `realistic` (27.74 GiB), `anime` (32.22 GiB) o `minimal` (2.79 GiB). Si se introduce un valor desconocido, usa `all` con advertencia. |
| `SKIP_MODEL_DOWNLOAD` | `0` | Pon `1` para omitir por completo la descarga automática de modelos al arrancar. |
| `HF_TOKEN` | *(Vacío)* | Token opcional de HuggingFace (leído automáticamente del entorno por `hf` sin exponerse en argumentos de proceso). |
| `COMFY_DATA_DIR` | `/workspace/comfyui-data` | Ruta donde se almacenan `models/`, `input/`, `output/` y `user/` (actualiza automáticamente `extra_model_paths.yaml`). |
| `COMFYUI_EXTRA_ARGS` | *(Vacío)* | Flags adicionales para `python3 main.py` de ComfyUI. |

---

## 4. Guía Paso a Paso (Desde tu MacBook, 100% Web)

### Paso 1: Publicar tu imagen en GitHub Container Registry (gratis, sin usar Docker en tu Mac)

1. Crea un repositorio nuevo en tu cuenta de GitHub (por ejemplo `comfyui-vast`) y sube el contenido de este proyecto.
2. Al hacer push a la rama `main`, el workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) validará los scripts con ShellCheck, comprobará los SHAs y las 26 URLs de HuggingFace, construirá una imagen candidata `sha-*`, ejecutará el **smoke test** (incluyendo `--quick-test-for-ci` de ComfyUI) y, solo si pasa, la promoverá a `:latest`:
   ```text
   ghcr.io/<tu-usuario-github>/<tu-repo>:latest
   ```
3. Cuando termine el workflow en verde, entra en tu perfil de GitHub → **Packages** → selecciona el paquete → **Package settings** → cambia la visibilidad a **Public** (para que Vast.ai pueda descargarlo sin pedir credenciales).

---

### Paso 2: Configurar la Plantilla en la Consola Web de Vast.ai

Entra en [cloud.vast.ai/templates/](https://cloud.vast.ai/templates/) desde el navegador de tu MacBook y haz clic en **New Template**:

- **Image Path**: `ghcr.io/<tu-usuario-github>/<tu-repo>:latest`
- **Docker Options (Environment / Port Mapping)**:
  ```text
  -p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e COMFY_USER=admin -e COMFY_PASS=TuClaveLargaUnica123 -e MODEL_PROFILE=all
  ```
- **Launch Mode**: **`Docker Entrypoint`** *(recomendado para que `tini` actúe como PID 1 y funcione `HEALTHCHECK`; si prefieres el modo interactivo SSH de Vast.ai, pon `/start.sh` en el campo On-start Script).*
- **Recommended Disk Space**: **`150` GB** (para alojar la imagen + los 63.35 GiB de modelos del perfil `all` + vídeos temporales y outputs).

> **Notas de seguridad (SEC-05 / BAJA-15)**:
> - Los puertos directos en Vast.ai funcionan sobre HTTP plano. Utiliza siempre una contraseña única para tus sesiones efímeras en `COMFY_PASS` (o deja `COMFY_PASS` vacío para que el contenedor genere una clave aleatoria en cada arranque).
> - Al igual que las imágenes estándar de Vast.ai, el contenedor se ejecuta como `root` para garantizar compatibilidad de permisos con volúmenes montados en `/workspace`.

---

### Paso 3: Elegir y Alquilar la GPU en Vast.ai (RTX 5090 32 GB o RTX 4090 24 GB)

En la pestaña **Search** de Vast.ai, aplica estos filtros:

- **GPU Primaria**: **RTX 5090** (**32 GB GDDR7 VRAM**, ~$0.45–$0.95/h on-demand o ~$0.25–$0.45/h interruptible). Ideal para workflows pesados de vídeo que combinan FLUX/SDXL + ControlNet + ReActor + CodeFormer en VRAM sin offload.
- **GPU Alternativa Económica**: **RTX 4090** (**24 GB VRAM**, ~$0.30–$0.55/h on-demand). Totalmente soportada por la misma imagen (`cu128`).
- **Max CUDA Version (`cuda_max_good`)**: **>= 12.8** *(imprescindible para que el driver del host soporte CUDA 12.8)*.
- **Disk Space**: **>= 150 GB** (si usas `MODEL_PROFILE=all`; con `MODEL_PROFILE=general` bastan 100 GB).
- **Internet Download Speed (`inet_down`)**: **>= 500 Mbps** (recomendado **>= 1000 Mbps**).
- **Direct Ports**: **>= 1**

*(Referencia CLI por si algún día usas `vastai` en terminal)*:
```bash
vastai search offers "gpu_name=RTX_5090 num_gpus=1 gpu_ram>=32 cuda_max_good>=12.8 inet_down>=500 disk_space>=150 direct_port_count>=1 reliability>0.97 verified=true rentable=true" -o "dph_total+"

vastai create instance <OFFER_ID> \
  --image ghcr.io/<tu-usuario>/<tu-repo>:latest \
  --disk 150 \
  --env "-p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e COMFY_USER=admin -e COMFY_PASS=TuClaveLargaUnica123 -e MODEL_PROFILE=all" \
  --args --direct
```

---

### Paso 4: Usar ComfyUI y Descargar tus Resultados antes de Destruir la Instancia

1. **Abrir desde el segundo 1**:
   - Nginx arranca antes de la Fase 1. Al pulsar **Open** en Vast.ai e introducir tu usuario y contraseña:
     - Mientras corre la Fase 1 (~25–60s), verás una pantalla de estado que se recarga sola cada 8 segundos con enlace directo a `/download-status`.
     - Apenas termina la Fase 1, se abre automáticamente la interfaz de ComfyUI mientras la Fase 2 descarga los checkpoints en segundo plano. Pulsa **`R`** en el navegador para refrescar los desplegables.
2. **Subir Workflows, Imágenes y Vídeos**:
   - Arrastra cualquier `.json` o `.png` desde el Finder de tu Mac sobre el lienzo de ComfyUI, y sube vídeos en `VHS_LoadVideo` sin límite de tamaño.
3. **Descargar tus vídeos e imágenes generados**:
   - Haz clic derecho sobre el resultado en `VHS_VideoCombine` / `Save Image` (o en el panel **Queue / History**) y elige **Save Video** / **Save Image** para guardarlo en tu MacBook.
4. **IMPORTANTE — Destruir la instancia al terminar**:
   - En Vast.ai, si solo pulsas **Stop**, dejas de pagar la GPU pero **sigues pagando por cada hora de almacenamiento en disco (150 GB)** y tus archivos en `output/` se borrarán al destruirla. Descarga siempre tus vídeos desde la UI al terminar tu sesión y pulsa el icono de papelera (**Destroy**) en la consola de Vast.ai.

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
- **Versiones congeladas**: ComfyUI y los 10 custom nodes preinstalados se clonan en commits verificados en [`config/nodes.lock`](config/nodes.lock). Para actualizar ComfyUI core a una nueva versión, actualiza el SHA en `config/nodes.lock` y ejecuta el workflow de GitHub Actions en lugar de usar "Update ComfyUI" desde la UI.
- **Protección `PIP_CONSTRAINT`**: La variable `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` está activa en runtime para impedir que un custom node instalado desde ComfyUI-Manager degrade `torch` (`2.9.1+cu128`) o rompa `opencv-python-headless` / `onnxruntime-gpu`. Si algún nodo experimental requiere levantar esa restricción temporalmente, puedes pasar `-e PIP_CONSTRAINT=""` en las opciones del contenedor.
