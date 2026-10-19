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
3. **Acceso directo por navegador con HTTP Basic Auth, Portal `/home` y Observabilidad (`/status`, `/report.md`)**:
   - ComfyUI escucha únicamente en loopback (`127.0.0.1:18188`) y **Nginx** (`user www-data;`) protege el puerto público `8188` con usuario/contraseña (`COMFY_USER` y `COMFY_PASS`), pantalla de espera auto-recargable (`_starting.html`) durante la Fase 1, soporte WebSockets (`/ws` sin rate-limit), subida de vídeos sin límite (`client_max_body_size 0;`), `/healthz` (`HEAD /system_stats` sin exponer metadatos) y un stack completo de observabilidad en el mismo puerto:
      - **`/home`**: Portal de inicio rápido (`<= 40 KB`, cero CDN externo, contraste WCAG AA) con acceso en 1 clic a ComfyUI, panel `/status`, informe `/report.md`, autodiagnóstico `/diag`, logs y terminal web opcional.
      - **`/status` y `/status/api`**: Panel de estado en vivo y API JSON servidos por **`comfyctl`** (binario nativo en Rust en `127.0.0.1:18189`) con métricas de GPU/VRAM, RAM/Swap, disco, progreso de descargas Fase 1/2, motor de triaje automático (17 patrones con causa y remediación) y visor de logs por pestañas.
      - **`/report.md`**: Informe Markdown autocontenido (hasta 400 KB, con secretos redactados automáticamente por `comfyctl`) diseñado para copiar y pegar directamente al agente IA para depuración sin acceso SSH.
4. **Supervisor en Rust (`comfyctl supervise`) y modo de arranque estricto (`STRICT_START=1`)**:
   - **`comfyctl`** ejecuta `strict-check` (`S1..S10`, fuente de verdad en [`comfyctl/src/config.rs`](comfyctl/src/config.rs) `L165-L214`) antes de iniciar descargas: verifica GPU/driver (`>= 570` para CUDA 12.8), VRAM (`>= 20000 MiB` `FATAL`, `< 22000 MiB` `WARN`), RAM (`>= 16384 MiB` con margen del 5% `15564 MiB` para reserva del kernel `FATAL`, `< 32768 MiB` `WARN`), espacio en disco libre por perfil (`75 GB` en `all`, `45 GB` en `general`/`realistic`/`anime`, `10 GB` en `minimal`, `5 GB` con `SKIP_MODEL_DOWNLOAD=1`), escritura en `/workspace/comfyui-data`, `nginx -t` + `.htpasswd`, `ffmpeg` (`libx264`), `/etc/comfyui/nodes.lock` y sonda real `torch.cuda` con timeout de 20s (`S10`). Si detecta un host incompatible con `STRICT_START=1`, omite descargas y ComfyUI pero mantiene vivos Nginx, `/home`, `/status` y `/report.md` durante `COMFY_STRICT_GRACE_SECONDS` (`600s`, saliendo después con código `20`) con un banner rojo `FATAL` indicando ejecutar `comfy destroy <id>` para no pagar GPU/disco en una instancia rota.
   - Supervisa `main.py`, escribe atómicamente `/run/comfyui-supervisor.json`, clasifica salidas (`137` OOM, `134`/`139` crash nativo CUDA/C++, `1` excepción Python, `143` SIGTERM), resetea el contador tras `COMFY_HEALTHY_UPTIME` (`600s`) y propaga `SIGTERM` limpiamente a sus subprocesos.
5. **ComfyUI-Manager preinstalado y desbloqueado**:
   - Instalado desde `Comfy-Org/ComfyUI-Manager` con `security_level = weak` (seguro porque ComfyUI escucha solo en `127.0.0.1`) para instalar cualquier custom node o modelo faltante con **un clic** desde la UI.
6. **`ffmpeg` completo de sistema (`libx264` + `h264_nvenc`) y autodiagnóstico `comfyctl diag` con timeouts**:
   - Fija `VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg`, `VHS_USE_IMAGEIO_FFMPEG=0` y `NVIDIA_DRIVER_CAPABILITIES=all`, y ejecuta `comfyctl diag` de forma síncrona antes de la Fase 1 (con timeouts duros por sonda vía `sys::run_command_with_timeout`) para probar GPU, kernel CUDA `matmul` en PyTorch, sesión real `InferenceSession` en `onnxruntime-gpu`, `h264_nvenc` y `libx264`.
7. **Descarga automática resiliente en 2 fases (63.35 GiB reales medidos en HuggingFace con `hf` + `hf-xet`)**:
   - **Fase 1 (Síncrona antes de abrir ComfyUI, 2.79 GiB)**: Descarga InsightFace `buffalo_l`, `inswapper_128.onnx`, `CodeFormer`, `GFPGANv1.4`, `facexlib` detectors, `YOLO face`, `SAM`, `SDXL VAE fp16-fix`, `SD1.5 VAE`, `4x-UltraSharp` y `4x-AnimeSharp`. Mientras dura (~25–60s según red), Nginx muestra una pantalla de espera con enlaces a `/home` y `/status`.
   - **Fase 2 (Segundo plano, 60.56 GiB en perfil `all`)**: Descarga todos los checkpoints populares, ControlNet Union ProMax e IP-Adapter con timeout por intento (`HF_FETCH_TIMEOUT=3600`), 3 reintentos automáticos y `rename()` atómico en el mismo filesystem mientras ya usas ComfyUI. Pulsa **`R`** en el navegador para refrescar los desplegables.

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
| `COMFY_PASS` | *(Autogenerada de 24 hex chars si se omite)* | Contraseña para HTTP Basic Auth. Si la defines, **no se imprime en los logs**; si la omites, se genera una clave aleatoria única con `openssl rand -hex 12` y se muestra una vez en el log de arranque (y nunca se expone en `/status/api`, `/report.md` ni `/diag`). |
| `STRICT_START` | `1` | Modo de arranque estricto (`comfyctl strict-check`, fuente de verdad en [`comfyctl/src/config.rs`](comfyctl/src/config.rs) `L165-L214`): valida 10 comprobaciones (`S1..S10`) antes de descargar modelos: GPU visible, driver NVIDIA `>= 570`, VRAM `>= 20000 MiB` (`< 22000 MiB` `WARN`), RAM `>= 16384 MiB` con margen del 5% `15564 MiB` (`< 32768 MiB` `WARN`), disco libre por perfil (`75 GB` en `all`, `45 GB` en `general`/`realistic`/`anime`, `10 GB` en `minimal`, `5 GB` si `SKIP_MODEL_DOWNLOAD=1`), escritura en `/workspace/comfyui-data`, `nginx -t` + `.htpasswd`, `ffmpeg` (`libx264`), `nodes.lock` y sonda `torch.cuda` (`20s` timeout). Si falla con `STRICT_START=1`, aborta descargas y ComfyUI pero mantiene vivos `/home`, `/status` y `/report.md` durante `COMFY_STRICT_GRACE_SECONDS` (`600s`, código de salida `20`). Pon `0` para degradar fallos a `WARN`. |
| `STRICT_MIN_VRAM_MB` / `STRICT_WARN_VRAM_MB` | `20000` / `22000` | Umbrales de VRAM en MiB (`FATAL` si `< STRICT_MIN_VRAM_MB`, `WARN` si `< STRICT_WARN_VRAM_MB`). |
| `STRICT_MIN_RAM_MB` / `STRICT_WARN_RAM_MB` | `16384` / `32768` | Umbrales de RAM en MiB (`FATAL` si `< STRICT_MIN_RAM_MB * 0.95`, `WARN` si `< STRICT_WARN_RAM_MB`). |
| `STRICT_MIN_DISK_GB` | `75` (`all`), `45` (`general`/`realistic`/`anime`), `10` (`minimal`), `5` (`SKIP_MODEL_DOWNLOAD=1`) | Espacio libre mínimo en GiB requerido en `COMFY_DATA_DIR`. |
| `MODEL_PROFILE` | `all` | Perfil de modelos: `all` (63.35 GiB), `general` (27.75 GiB), `realistic` (27.74 GiB), `anime` (32.22 GiB) o `minimal` (2.79 GiB). Si se introduce un valor desconocido, usa `all` con advertencia. |
| `SKIP_MODEL_DOWNLOAD` | `0` | Pon `1` para omitir por completo la descarga automática de modelos al arrancar. |
| `HF_TOKEN` | *(Vacío)* | Token opcional de HuggingFace (leído automáticamente del entorno por `hf` sin exponerse en argumentos de proceso ni en `/report.md`). |
| `COMFY_DATA_DIR` | `/workspace/comfyui-data` | Ruta donde se almacenan `models/`, `input/`, `output/` y `user/` (actualiza automáticamente `extra_model_paths.yaml`). |
| `COMFYUI_EXTRA_ARGS` | *(Vacío)* | Flags adicionales para `python3 main.py` de ComfyUI. Ejemplo útil en RTX 5090 ante errores `HostBuffer` / OOM inesperados: `--disable-pinned-memory` (ver sección 5.7). |
| `COMFY_MAX_RESTARTS` | `5` | Número máximo de reinicios automáticos de ComfyUI ante caídas antes de pasar a estado `exhausted` (manteniendo Nginx, `/home` y `/status` activos para diagnóstico). |
| `COMFY_RESTART_DELAY` | `5` | Segundos de espera entre caídas de ComfyUI antes de que el supervisor relance `main.py`. |
| `COMFY_HEALTHY_UPTIME` | `600` | Segundos de funcionamiento continuo tras los cuales el supervisor resetea `restarts` a `0` e incrementa `healthy_resets`. |
| `LOG_MAX_MB` | `200` | Tamaño máximo en MB por fichero de log en `/var/log` antes de truncar conservando los últimos `50 MB`. |
| `HF_FETCH_TIMEOUT` | `3600` | Timeout máximo en segundos por intento de descarga individual en `download-models.sh`. |
| `HF_HUB_DISABLE_XET` | *(Vacío)* | Pon `1` para que `hf download` use HTTP clásico en lugar del backend `hf-xet`. Útil si la Fase 2 se queda colgada, devuelve muchos 503 o el host tiene poca RAM (ver sección 5.8). |
| `ENABLE_WEB_TERMINAL` | `0` | Pon `1` para activar `ttyd` en `127.0.0.1:7681`, accesible tras Basic Auth en `/terminal/`. Desactivado por defecto por seguridad. |

---

## 4. Guía de Uso (Desde tu MacBook, 100% Web)

La guía está dividida en dos partes: **A. Primera vez** (configuración que se hace una sola vez) y **B. Cada sesión** (lo que repites cada vez que alquilas una GPU). Los tiempos marcados como *pendiente de medir* se completarán tras el primer build y la primera sesión real.

### A. Primera vez

#### A1. Publicar la imagen en GitHub Container Registry (gratis, sin Docker en tu Mac)

1. Crea un repositorio en tu cuenta de GitHub (por ejemplo `comfyui-vast`) y sube el contenido de este proyecto a la rama `main`.
2. El workflow [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) valida scripts, SHAs y las 26 URLs de HuggingFace, construye una imagen candidata `sha-*`, ejecuta el smoke test (incluido `--quick-test-for-ci` de ComfyUI) y, solo si pasa, la promueve a:
   ```text
   ghcr.io/<tu-usuario-github>/<tu-repo>:latest
   ```
   El primer build tarda entre 60 y 120 minutos (*pendiente de medir*). Los siguientes reutilizan la caché `buildcache` y tardan minutos.
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
   | **Disk Space** | `150` GB para `MODEL_PROFILE=all` (100 GB bastan para `general`/`realistic`/`anime`; 40 GB para `minimal`) |

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
    Con él puedes abrir el portal (`comfy`), ver el estado (`comfy open status`), extraer la contraseña autogenerada (`comfy pass`), descargar y copiar al portapapeles el informe completo para el agente IA (`comfy report`), monitorizar en vivo (`comfy watch`), alquilar (`comfy rent`) o destruir (`comfy destroy <id>`).

#### A5. Sesión de aceptación (solo la primera instancia real)

13. La primera vez que levantes una instancia, recorre la lista de verificaciones de [`docs/auditorias/AUDITORIA_AGENTE_V5.md`](docs/auditorias/AUDITORIA_AGENTE_V5.md) (sección 3) y anota en [`docs/decisiones.md`](docs/decisiones.md): GPU, driver, tiempo de descarga de la imagen, duración de la Fase 1 y de la Fase 2, y resultado de cada punto. A partir de ahí, el proyecto se considera en producción.

---

### B. Cada sesión

#### B1. Buscar y alquilar la GPU (5 minutos)

1. En **Search**, con tu plantilla seleccionada, aplica estos filtros:

   | Filtro | Valor | Por qué |
   |---|---|---|
   | GPU | **RTX 5090** (32 GB) o **RTX 4090** (24 GB), 1 GPU | Misma imagen `cu128` para ambas |
   | **Max CUDA** (`cuda_max_good`) | **≥ 12.8** | Es el driver del host; con menos, `torch` cu128 no inicializa la GPU |
   | **Disk Space** | ≥ 150 GB (`all`) / ≥ 100 GB (otros perfiles) | El disco se fija al alquilar; no se amplía después |
   | **Internet Download** (`inet_down`) | ≥ 500 Mbps (ideal ≥ 1 Gbps) | 63 GB de modelos: ~19 min a 500 Mbps, ~1 h a 150 Mbps |
   | **Direct Ports** | ≥ 1 | Necesario para que el botón **Open** apunte directo a Nginx |
   | **Reliability** | > 0.97, *verified* | Menos cortes a mitad de sesión |

2. **Lee la tarjeta de la oferta antes de pulsar Rent**, no solo el $/h:
   - **RAM del host**: evita hosts con menos de 32 GB si usas `MODEL_PROFILE=all` (las descargas con `hf-xet` y la carga de FLUX fp8 de 16 GB consumen RAM; un host justo puede matar ComfyUI por falta de memoria).
   - **$/GB/mes de almacenamiento** y **$/TB de tráfico**: con 63 GB descargados por sesión, en hosts caros el tráfico puede costar más que la primera hora de GPU.
   - **On-demand** para sesiones de trabajo; *interruptible* es más barato pero otro usuario puede quitarte la máquina.
3. Pulsa **Rent** (o ejecuta `comfy rent all` desde tu Mac). Precios orientativos (2026-10): RTX 5090 ~$0.45–$0.95/h on-demand; RTX 4090 ~$0.30–$0.55/h.

*(Referencia CLI equivalente)*:
```bash
vastai search offers "gpu_name=RTX_5090 num_gpus=1 gpu_ram>=32 cuda_max_good>=12.8 inet_down>=500 disk_space>=150 direct_port_count>=1 reliability>0.97 verified=true rentable=true" -o "dph_total+"

vastai create instance <OFFER_ID> \
  --image ghcr.io/<tu-usuario>/<tu-repo>:latest \
  --disk 150 \
  --label comfy \
  --env "-p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e COMFY_USER=admin -e COMFY_PASS=TuClaveLargaUnica123 -e MODEL_PROFILE=all" \
  --direct
```

#### B2. Esperar el arranque (de 5 a 15 minutos la primera vez en un host)

4. En **Instances**, la tarjeta pasa por *Loading / Creating* mientras el host descarga la imagen (*tiempo pendiente de medir*; depende del tamaño de la imagen y de la red del host). Si el host ya tenía la imagen en caché de una sesión anterior, es casi inmediato.
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

#### B4. Si algo falla durante la sesión (Observabilidad y Diagnóstico IA)

11. **Panel `/status` y Portal `/home`**: Abre `http://<IP>:<PUERTO>/status` (o `http://<IP>:<PUERTO>/home`) para ver en vivo el estado del supervisor de ComfyUI, uso de VRAM/RAM/Disco, progreso de descargas, alertas automáticas de triaje con remediación paso a paso y pestañas de logs (`comfyui`, `start`, `download-models`, `supervisor`, `diag`, `nginx-error`).
12. **Informe para el Agente IA (`/report.md`)**: Pulsa **"Copiar informe para IA (`report.md`)"** en `/home` o `/status` (o ejecuta `comfy report` en tu Mac) y pega el Markdown resultante en el chat del agente. Incluye autodiagnóstico de GPU/PyTorch/ONNX/NVENC, alertas detectadas, deriva de paquetes Python respecto a `requirements.lock`, commits de `custom_nodes` vs `nodes.lock`, inventario de modelos en disco y colas de todos los logs (con secretos redactados).
13. Consulta también la sección 5 (problemas conocidos: OOM en RTX 5090, descargas colgadas, `IMPORT FAILED`).

#### B5. Cerrar la sesión

14. **Descarga tus resultados** desde la UI: clic derecho sobre la salida de `Video Combine` / `Save Image` (o en **Queue → History**) → **Save**. Todo lo que quede en `/workspace/comfyui-data/output` se pierde con la instancia.
15. Pulsa **Destroy** (icono de papelera), **no Stop**. Con *Stop* sigues pagando el almacenamiento (150 GB) y, si el host alquila esa GPU a otro usuario mientras tanto, no podrás reanudar la instancia. Este proyecto está diseñado para descargar los modelos en cada arranque, así que *Destroy* es el cierre normal.
16. Si en el futuro quieres conservar los modelos entre sesiones, Vast.ai ofrece **Volumes** persistentes que podrían montarse en `/workspace/comfyui-data`; la imagen ya está preparada para ello (los datos viven fuera de `/opt/ComfyUI`). Es una decisión pendiente (OPS-04), no una función activa.

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
- **No uses "Update ComfyUI" desde Manager**: `/opt/ComfyUI` es un clon superficial fijado por SHA; el botón de actualización del core puede fallar o dejar el frontend (`comfyui-frontend-package`) desalineado con el servidor. Instalar o actualizar *custom nodes* desde Manager sí está soportado.

### 7. OOM o `HostBuffer.read_file_slice failed` en RTX 5090 con modelos grandes
- **Síntoma**: Con Wan, FLUX.2 u otros modelos pesados, ComfyUI falla en todas las generaciones con un error de CUDA que menciona `HostBuffer`, o se queda sin memoria aunque la VRAM no esté llena. Son fallos conocidos de la gestión dinámica de VRAM y de la memoria *pinned* en Blackwell (ComfyUI issues #15255 y #14250, 2026), reportados a NVIDIA.
- **Primer intento**: añade `-e COMFYUI_EXTRA_ARGS=--disable-pinned-memory` a las Docker Options de la plantilla y reinicia la instancia. Si tienes varias GPU en el host, `--cuda-device 0` también está documentado como alternativa.
- **Si el contenedor se reinicia o se apaga solo**: es un OOM de **RAM del sistema** (el kernel mata a `python3` con código 137). Elige un host con más RAM (≥ 32 GB con `MODEL_PROFILE=all`), o usa un perfil menor para que la Fase 2 no coincida con la carga de FLUX.

### 8. La Fase 2 se queda colgada o devuelve muchos errores 503
- **Causa probable**: el backend `hf-xet` que usa `hf download` por defecto dimensiona sus buffers con constantes fijas, no según la RAM disponible, y en contenedores con poca memoria puede provocar OOM, más errores 503 o descargas detenidas (huggingface_hub #3300, xet-core #927 y #581).
- **Solución**: añade `-e HF_HUB_DISABLE_XET=1` a las Docker Options para forzar HTTP clásico. Es algo más lento pero estable. Los modelos ya descargados se conservan (`hf_fetch` salta los archivos existentes) y los parciales se reanudan en el siguiente arranque.

### 9. Un nodo aparece con `IMPORT FAILED` o `no kernel image is available for sm_120`
- **Si pasa en la imagen recién arrancada**: no debería; el build falla si cualquiera de los 10 nodos no importa. Reporta el log completo.
- **Si pasa tras instalar algo desde Manager**: casi siempre un nodo arrastró su propio `torch` o `opencv` y pisó los fijados. Compara `pip3 freeze` con `/etc/comfyui/requirements.lock` (el artefacto `requirements-lock` del workflow es idéntico) para ver qué cambió, y reinstala la terna con `pip3 install --force-reinstall torch==2.9.1+cu128 torchvision==0.24.1+cu128 torchaudio==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128`. Mientras `PIP_CONSTRAINT` siga activo esto no debería ocurrir; ocurre si se desactivó con `-e PIP_CONSTRAINT=""`.
