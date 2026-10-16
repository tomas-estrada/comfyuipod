# Investigación Web Verificada y Decisiones de Arquitectura

**Fecha de consulta y verificación**: `2026-10-16`

---

## 1. Investigación Web Requerida (Sección 2 del Spec)

| Tema | Estado verificado (2026-10-16) | Decisión adoptada |
|---|---|---|
| **API / CLI de Vast.ai (RTX 5090 / 4090)** | `vastai search offers 'gpu_name=RTX_5090 num_gpus=1 gpu_ram>=32 cuda_max_good>=12.8 inet_down>=500 disk_space>=150 reliability>0.97 verified=true rentable=true' -o 'dph_total+'` y `vastai create instance <ID> --image <IMG> --disk 150 --env '-p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e COMFY_USER=admin -e COMFY_PASS=...' --direct`. Precios **RTX 5090 (32 GB GDDR7)**: **$0.45–$0.95/h** on-demand y **$0.25–$0.45/h** interruptible; **RTX 4090 (24 GB)**: **$0.30–$0.55/h** on-demand. Fuente: [Vast.ai CLI Docs](https://docs.vast.ai/cli/commands). | Uso 100% Web mediante Plantilla en la consola de Vast.ai apuntando a **RTX 5090 (32 GB)** como GPU principal (por solicitud explícita del usuario) y manteniendo soporte completo para **RTX 4090 (24 GB)** con la misma imagen `cu128`. |
| **Imagen base Docker y Blackwell (`sm_120`)** | `Comfy-Org` no mantiene una imagen `ghcr.io/comfyanonymous/comfyui:latest` oficial con nodos de vídeo y restauración facial precompilados. El repositorio oficial de ComfyUI reside ahora en `Comfy-Org/ComfyUI` (redirección 301 desde `comfyanonymous/ComfyUI`). La arquitectura Blackwell (`sm_120`, RTX 5090) requiere **CUDA 12.8+** y PyTorch compilado con `cu128`. | Imagen propia basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` + terna fijada `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128` + `onnxruntime-gpu` + registro de librerías CUDA/cuDNN en `/etc/ld.so.conf.d/pytorch-cuda.conf`, compilada con **GitHub Actions** en `ghcr.io`. |
| **ComfyUI-Manager** | Repositorio oficial trasladado a [`Comfy-Org/ComfyUI-Manager`](https://github.com/Comfy-Org/ComfyUI-Manager). En versiones recientes (v3.38+), la configuración reside en `user/__manager/config.ini` (y `user/default/ComfyUI-Manager/config.ini` en versiones previas). Manager permite `security_level = weak` cuando ComfyUI escucha en loopback (`127.0.0.1`). | Preinstalado en `/opt/ComfyUI/custom_nodes/ComfyUI-Manager` con `channel_url` apuntando a `Comfy-Org/ComfyUI-Manager` y `security_level = weak`. |
| **Model Manager nativo** | Los diálogos de ComfyUI Desktop no descargan automáticamente modelos arbitrarios de terceros (como `inswapper_128.onnx` o `buffalo_l`) en contenedores Docker headless. | Pre-descarga automática en 2 fases en [`scripts/download-models.sh`](../scripts/download-models.sh) + `Model Manager` de `ComfyUI-Manager` activo en la UI web. |
| **Modelos por defecto en HuggingFace** | Los 26 archivos fueron verificados mediante `HEAD` contra HuggingFace (`HTTP 302`, `Gated: False`, con tamaño exacto medido vía `x-linked-size`: **2.79 GiB** en Fase 1 y **60.56 GiB** en Fase 2 = **63.35 GiB** total). `huggingface-hub >= 1.0` (actual `2.2.0`) provee el binario `hf` y utiliza `hf-xet` por defecto en `x86_64`. | Catálogo completo por defecto (`MODEL_PROFILE=all`) más perfiles validados (`general` 27.75 GiB, `realistic` 27.74 GiB, `anime` 32.22 GiB, `minimal` 2.79 GiB). |
| **`ffmpeg` en contenedor** | El paquete `ffmpeg` de Ubuntu 24.04 (`apt`) soporta `--enable-libx264` (CPU) y `--enable-nvenc` (`h264_nvenc` / `hevc_nvenc` en GPU). Requiere `NVIDIA_DRIVER_CAPABILITIES=all` para exponer `libnvidia-encode.so`. | Instalado vía `apt`, forzado en VideoHelperSuite con `VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg` y verificado sin `SIGPIPE` al inicio en [`scripts/start.sh`](../scripts/start.sh). |

---

## 2. Decisiones Tomadas a partir de las Auditorías V1, V2 y V3

### Ronda V1 ([`docs/auditorias/AUDITORIA_AGENTE_V1.md`](auditorias/AUDITORIA_AGENTE_V1.md))

| ID V1 | Decisión implementada |
|---|---|
| **SEC-01 / SEC-02 / SEC-04** | Si el usuario define `COMFY_PASS`, **nunca se imprime en los logs**. Si no la define, se autogenera una contraseña aleatoria de 24 caracteres hexadecimales con `openssl rand -hex 12` (sin tuberías ni riesgo de `SIGPIPE`, `BAJA-01`) y se muestra una única vez. Se pasa a `htpasswd -ci` por `stdin` y `HF_TOKEN` se lee del entorno sin usar `--token` en CLI. |
| **SEC-03 / SEC-05 / SEC-06** | Eliminado `--enable-cors-header` en ComfyUI. Añadido rate-limiting (`limit_req_zone`) en [`config/nginx.conf`](../config/nginx.conf) y documentado el uso de HTTP en puertos directos de Vast.ai. |
| **REL-01 / REL-06 / REL-07** | Añadido `user www-data;` al inicio de [`config/nginx.conf`](../config/nginx.conf) para evitar errores HTTP 500 al leer `.htpasswd`. Logs de Nginx enviados a `/dev/stdout` y `/dev/stderr`. `/healthz` verifica `http://127.0.0.1:18188/system_stats` mediante `proxy_method HEAD` (`BAJA-03`). |
| **REL-02 / REL-03 / REL-05** | Salida de `ffmpeg -encoders` capturada en variable para evitar `SIGPIPE` (`141`) con `pipefail`. Nginx se inicia **antes** de la Fase 1 y la Fase 1 se ejecuta con `timeout 600` no fatal. `tini` configurado como PID 1 (`ENTRYPOINT ["/usr/bin/tini", "--", "/start.sh"]`). |
| **REL-04 / DOC-02** | `STAGE_DIR` movido al mismo filesystem (`${MODELS_DIR}/.hf_stage`) para que `mv` sea un `rename()` atómico. Añadidos 3 reintentos con backoff y resumen de fallos. Tamaños reales (`2.79 GiB` Fase 1 + `60.56 GiB` Fase 2 = `63.35 GiB`) documentados con precisión. |
| **CFG-01 / CFG-03 / CFG-04 / OPS-02** | Código de ComfyUI y custom nodes instalado en `/opt/ComfyUI` y datos en `/workspace/comfyui-data/{models,input,output,user}` enlazados por symlinks. [`config/extra_model_paths.yaml`](../config/extra_model_paths.yaml) reducido únicamente a los cruces nativos (`checkpoints`/`diffusion_models`/`unet` y `clip`/`text_encoders`). |

### Ronda V2 ([`docs/auditorias/AUDITORIA_AGENTE_V2.md`](auditorias/AUDITORIA_AGENTE_V2.md))

| ID V2 | Decisión implementada |
|---|---|
| **CRIT-01 (SHA de `hadolint-action`)** | Actualizado a `hadolint/hadolint-action@54c9adbab1582c2ef04b2016b760714a4bfde3cf # v3.1.0` (verificado con `git ls-remote`). |
| **CRIT-02 (Colisión `cv2` / `onnxruntime` en `Dockerfile`)** | Tras instalar todos los custom nodes, se desinstalan todas las variantes (`opencv-python`, `opencv-contrib-python`, `opencv-python-headless`, `opencv-contrib-python-headless`, `onnxruntime`, `onnxruntime-gpu`) y se reinstalan con `pip3 install --no-deps --force-reinstall "opencv-python-headless>=4.10.0.84,<5.0.0" "onnxruntime-gpu>=1.20.0,<2.0.0"`, restaurando íntegramente `cv2/` y verificando `import cv2` en el build. |
| **ALTA-01 / ALTA-02 (Promoción segura a `latest` y tag en minúsculas)** | El workflow construye y publica primero únicamente el tag candidato en minúsculas (`type=sha,prefix=sha-` generado por `docker/metadata-action`), ejecuta el smoke test completo sobre esa imagen y, solo si pasa, promueve a `:latest` / semver con `docker buildx imagetools create`. |
| **ALTA-03 / L-01 (Conservar toolchain C/C++)** | Se instalan y conservan `build-essential`, `g++`, `cmake`, `pkg-config` y `python3-dev` en la capa inicial (`Step 1` con limpieza de `/var/lib/apt/lists/*` en el mismo `RUN`), garantizando que `torch.compile` (Triton) y las instalaciones nativas desde ComfyUI-Manager funcionen en runtime sin capas de purga cruzadas inútiles. |
| **ALTA-04 (Commits de Git)** | Commit inicial en la rama `main` con todos los archivos del proyecto trackeados y los informes de auditoría archivados en `docs/auditorias/`. |
| **ALTA-05 (Fijado de PyTorch `cu128`, `sam2` y `requirements.lock`)** | Fijada la terna `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128` en `Dockerfile` y [`config/constraints.txt`](../config/constraints.txt). `sam2` se clona por SHA fijado en [`config/nodes.lock`](../config/nodes.lock) y se instala con `SAM2_BUILD_CUDA=0 pip3 install --no-build-isolation .`, filtrándose del `requirements.txt` de Impact-Pack. Al final del build se vuelca `pip3 freeze > /etc/comfyui/requirements.lock` y se sube como artefacto de CI. |
| **MED-01 (Ajuste de `limit_req` y `/ws`)** | Ampliado a `rate=50r/s`, `burst=300 nodelay`, `limit_req_status 429` y separado `location /ws` sin `limit_req` en [`config/nginx.conf`](../config/nginx.conf). |
| **MED-02 (GPU objetivo RTX 5090 vs RTX 4090)** | El usuario solicitó expresamente apuntar a **RTX 5090 (32 GB)** tras la entrega inicial; se documenta en `README.md` que tanto RTX 5090 (32 GB) como RTX 4090 (24 GB) son 100% compatibles con la misma imagen `cu128`. |
| **MED-03 (`requirements-no-cupy.txt` + warm-up `--quick-test-for-ci`)** | El bucle del paso 4 procesa tanto `requirements.txt` como `requirements-no-cupy.txt` (`ComfyUI-Frame-Interpolation`) y ejecuta `python3 main.py --cpu --quick-test-for-ci` tanto en el build como en el smoke test de CI, fallando si aparece `IMPORT FAILED`. |
| **MED-04 (Conservar `models/configs/*.yaml`)** | Se respalda `/opt/ComfyUI/models/configs` en `/etc/comfyui/models_configs` antes de crear el symlink a `/workspace/comfyui-data/models`, y [`scripts/start.sh`](../scripts/start.sh) restaura los YAML si el directorio en el volumen está vacío. |
| **MED-05 (`huggingface-hub >= 1.0` y `hf-xet`)** | Verificado en PyPI que `huggingface-hub 2.2.0` instala `hf-xet` por defecto en `x86_64` y provee el comando `hf`. Se verifica `import huggingface_hub, hf_xet` y `hf version` en el smoke test de CI. |
| **BAJA-01 a BAJA-15 & INF-01 / INF-02** | Contraseña con `openssl rand -hex 12` sin tuberías (`BAJA-01`), `HEALTHCHECK --start-period=900s` (`BAJA-02`), `proxy_method HEAD` en `/healthz` (`BAJA-03`), validación de `MODEL_PROFILE` (`BAJA-04`), sustitución dinámica de `COMFY_DATA_DIR` en `extra_model_paths.yaml` (`BAJA-05`), cifras por perfil alineadas (`BAJA-06`), pantalla de espera [`config/_starting.html`](../config/_starting.html) durante Fase 1 (`BAJA-07`), conservación de `STAGE_DIR` si falla algún modelo (`BAJA-08`), `git checkout -B main FETCH_HEAD` (`BAJA-09`), `setsid -f` para Fase 2 (`INF-02` / `L-03`) y creación de [`docs/walkthrough.md`](walkthrough.md) en el repositorio (`INF-01`). |

### Ronda V3 ([`docs/auditorias/AUDITORIA_AGENTE_V3.md`](auditorias/AUDITORIA_AGENTE_V3.md))

| ID V3 | Decisión implementada |
|---|---|
| **H-01 (Referencia fija en minúsculas para caché de BuildKit)** | Añadido el paso `meta_cache` con `docker/metadata-action` (`tags: type=raw,value=buildcache`) en [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml) para que `cache-from` y `cache-to` apunten siempre a `ghcr.io/<owner>/<repo>:buildcache` en minúsculas y reutilicen las capas entre commits. |
| **M-01 (Verificación real de commit SHAs en CI)** | Reemplazado `git ls-remote` por `git init -q /tmp/chk_sha && git -C /tmp/chk_sha fetch -q --depth=1 --filter=tree:0 "$url" "$sha"` sin fallback en el job `validate` de CI (probado localmente contra los 12 SHAs de [`config/nodes.lock`](../config/nodes.lock)). |
| **M-02 (Espacio de disco entre build y pull del candidato)** | Añadido paso `docker buildx prune --all --force && sudo docker system prune --all --force` con reporte `df -h` antes del `docker pull` del smoke test en [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml). |
| **M-03 (Timeout y captura de código de retorno en warm-up)** | En [`Dockerfile`](../Dockerfile) y en el smoke test de CI, el warm-up corre con `timeout 600 python3 main.py --cpu --quick-test-for-ci > /tmp/comfy_warmup.log 2>&1 || rc=$?` y aborta si `[ "$rc" -ne 0 ] || grep -q "IMPORT FAILED"`. |
| **L-01 (Sin `apt-get purge` en capa distinta)** | `cmake` y `pkg-config` se instalan en la capa 1 (que limpia `/var/lib/apt/lists/*` en el mismo `RUN`) y no se purgan en capas posteriores. |
| **L-02 (`--direct` en `vastai create instance`)** | Eliminado `--args` antes de `--direct` en `README.md` y `docs/decisiones.md`. |
| **L-03 (`setsid -f` para evitar zombi en Fase 2)** | Actualizado [`scripts/start.sh`](../scripts/start.sh) a `setsid -f /usr/local/bin/download-models.sh --phase2 >> /var/log/download-models.log 2>&1 < /dev/null`, haciendo fork inmediato para que `tini` (PID 1) adopte y coseche el proceso al terminar. |
| **L-04 (`albumentations` y `albumentationsx`)** | Documentado en la Sección 3 de este archivo por qué coexisten ambos paquetes y la regla de no desinstalar ninguno por separado. |
| **L-05 (Precisión documental en `walkthrough.md` y `decisiones.md`)** | Alineadas todas las afirmaciones de verificación con la implementación real (`git fetch --depth=1 --filter=tree:0` y `setsid -f`). |
| **L-06 (Procedimiento de actualización de SHAs y constraints)** | Documentado en la Sección 3 de este archivo. |
| **L-07 (Limpieza tras el warm-up del build)** | Al terminar el warm-up en el paso 6 del [`Dockerfile`](../Dockerfile), se eliminan `/opt/ComfyUI/temp`, `/workspace/comfyui-data/temp`, `/workspace/comfyui-data/user/default/comfy.settings.json`, `/workspace/comfyui-data/user/__manager`, `/workspace/comfyui-data/user/default/ComfyUI-Manager`, `/root/.config` y `/root/.cache`, y se vuelven a copiar los `config.ini` limpios de ComfyUI-Manager. |
| **L-08 (Repositorio local en OneDrive)** | Riesgo residual aceptado en el entorno local de desarrollo; el árbol git está limpio y normalizado a `LF` mediante `.gitattributes`. |

### Ronda V4 ([`docs/auditorias/AUDITORIA_AGENTE_V4.md`](auditorias/AUDITORIA_AGENTE_V4.md))

| ID V4 | Decisión implementada |
|---|---|
| **N-01 (Unificar `timeout 600` en smoke test)** | Actualizado el timeout del warm-up en el smoke test de [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml) de `300` a `600` segundos para igualar el `timeout 600` del paso 6 en [`Dockerfile`](../Dockerfile). |
| **N-02 (`--phase2` completa pendientes de Fase 1)** | En [`scripts/download-models.sh`](../scripts/download-models.sh), el bloque `--phase2` invoca primero `run_phase1` de forma idempotente (los archivos ya descargados hacen `SKIP` en `<1 ms`) antes de `run_phase2`, garantizando que ningún modelo de Fase 1 quede ausente si se alcanzó el `timeout 600` durante el arranque. |
| **N-03 (`set -euo pipefail` y `::error::` en M-01)** | Añadido `set -euo pipefail` explícito y reporte `::error::SHA $sha no existe en $url ($name)` al paso de verificación de SHAs de `nodes.lock` en [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml). |
| **N-04 (Limpieza e inspección de `user/default/ComfyUI-Manager`)** | Añadido `/workspace/comfyui-data/user/default/ComfyUI-Manager` al `rm -rf` posterior al warm-up en [`Dockerfile`](../Dockerfile) antes de recrear el directorio y copiar `config.ini`, e incorporado `find /workspace/comfyui-data/user -type f` en el smoke test de CI (`V12`). |
| **N-05 (Atestación de procedencia SLSA en `build-push-action` v6)** | Se conserva el comportamiento por defecto (`provenance: mode=min`) para mantener trazabilidad SLSA en GHCR sin impacto funcional en `docker pull` ni en Vast.ai. |
| **N-06 (Limpieza de redundancias menores)** | Eliminado `g++` redundante en [`Dockerfile`](../Dockerfile) (ya incluido por `build-essential`) y `sudo` redundante antes de `docker` en [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml). |

---

## 3. Mantenimiento, Coexistencia de Paquetes (`L-04`) y Procedimiento de Bump (`L-06`)

### 3.1 Coexistencia de `albumentations` y `albumentationsx` (`L-04`)
- `ComfyUI-ReActor` declara `albumentations` en su `requirements.txt`, mientras que `comfyui_controlnet_aux` declara `albumentationsx` (fork drop-in que instala en el mismo namespace `site-packages/albumentations/`).
- Ambos son compatibles a nivel de API, pero **nunca se debe ejecutar `pip uninstall` de uno solo de ellos**, ya que `pip` borraría los archivos compartidos de `site-packages/albumentations/` dejando roto al otro paquete. Si alguna vez se actualiza uno manualmente, reinstalar con `pip3 install --force-reinstall --no-deps`.

### 3.2 Procedimiento para actualizar `config/nodes.lock` y `config/constraints.txt` (`L-06`)
1. **Cuándo actualizar**:
   - Cuando una nueva versión de ComfyUI core o de alguno de los 10 custom nodes sea necesaria para soportar un modelo/nodo nuevo o corregir un bug.
2. **Cómo actualizar un SHA en `config/nodes.lock`**:
   - Consultar el commit objetivo de la rama principal del repositorio upstream:
     ```bash
     git ls-remote <URL_REPO> HEAD
     ```
   - Reemplazar el SHA de 40 caracteres en [`config/nodes.lock`](../config/nodes.lock).
   - Si se actualiza PyTorch o una cota en [`config/constraints.txt`](../config/constraints.txt), actualizar la misma versión en el paso 3 del [`Dockerfile`](../Dockerfile).
3. **Criterio obligatorio de aceptación antes de dar por bueno el bump**:
   - Hacer push en una rama o en `main` y verificar que el workflow de GitHub Actions ([`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)) pase en verde tanto el job `validate` como el `build-and-push` (incluyendo el warm-up `--quick-test-for-ci` sin ningún `IMPORT FAILED` y la promoción a `latest`).
   - Descargar el artefacto `requirements-lock` generado por el workflow para auditar si algún nodo introdujo dependencias nuevas.

