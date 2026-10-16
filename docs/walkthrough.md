# Walkthrough del Repositorio: ComfyUI "Everything-Ready" para Vast.ai

Este documento detalla la función de cada archivo del repositorio y las verificaciones realizadas tras las rondas de auditoría V1, V2 y V3.

---

## 1. Mapa de Archivos del Repositorio

| Archivo | Propósito y garantías clave |
|---|---|
| [`Dockerfile`](../Dockerfile) | Imagen basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` con `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128`, `ffmpeg` (`libx264` + `h264_nvenc`), `nginx`, `tini`, `build-essential` + `cmake` + `pkg-config` + `python3-dev` (para `torch.compile` y compilaciones nativas), instalación en `/opt/ComfyUI`, reinstalación limpia `--no-deps --force-reinstall` de `opencv-python-headless` y `onnxruntime-gpu`, preservación de `models/configs/*.yaml` y warm-up `--quick-test-for-ci` con `timeout 600`, captura de código de retorno y limpieza posterior de `/opt/ComfyUI/temp` y cachés. |
| [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml) | Pipeline CI/CD en dos etapas: (1) `validate` con `ShellCheck`, `Hadolint` (`54c9adbab1582c2ef04b2016b760714a4bfde3cf`), verificación directa de los 12 commit SHAs de `nodes.lock` vía `git fetch --depth=1 --filter=tree:0` y verificación `HEAD` de los 26 modelos en HuggingFace; (2) `build-and-push` con caché persistente en minúsculas (`type=raw,value=buildcache`), publicación inicial solo del candidato `sha-*`, purga de BuildKit local antes del `docker pull`, smoke test completo (`--quick-test-for-ci` + exportación de `requirements.lock`) y promoción a `:latest` con `docker buildx imagetools create`. |
| [`scripts/start.sh`](../scripts/start.sh) | Entrypoint ejecutado bajo `tini`: verifica `ffmpeg` sin `SIGPIPE`, inicializa `/workspace/comfyui-data` (restaurando `models/configs`), actualiza dinámicamente `extra_model_paths.yaml` según `COMFY_DATA_DIR`, configura Basic Auth con `openssl rand -hex 12` por `stdin`, arranca Nginx antes de la Fase 1, ejecuta Fase 1 con `timeout 600` no fatal, lanza Fase 2 con `setsid -f` (reparentado a `tini` PID 1) y ejecuta ComfyUI en `127.0.0.1:18188`. |
| [`scripts/download-models.sh`](../scripts/download-models.sh) | Descargador en 2 fases con validación de `MODEL_PROFILE` (`all`, `general`, `realistic`, `anime`, `minimal`), 3 reintentos con backoff exponencial, `STAGE_DIR` en el mismo filesystem para `rename()` atómico y conservación de parciales en caso de error. |
| [`config/nginx.conf`](../config/nginx.conf) | Reverse proxy en `0.0.0.0:8188` con `user www-data;`, logs a `/dev/stdout` y `/dev/stderr`, `limit_req` holgado (`rate=50r/s burst=300`), bloque `/ws` dedicado sin rate-limit, `/healthz` mediante `HEAD /system_stats`, `/download-status` y pantalla de espera `_starting.html` para errores 502 durante la Fase 1. |
| [`config/_starting.html`](../config/_starting.html) | Página HTML ligera con auto-recarga cada 8 segundos mostrada mientras la Fase 1 termina y ComfyUI levanta el puerto `18188`. |
| [`config/nodes.lock`](../config/nodes.lock) | Registro de commits SHA verificados para `Comfy-Org/ComfyUI`, `facebookresearch/sam2` y los 10 custom nodes preinstalados. |
| [`config/constraints.txt`](../config/constraints.txt) | Restricciones de versión (`PIP_CONSTRAINT`) para proteger la terna `torch==2.9.1+cu128`, `numpy<2.3.0`, `opencv-python-headless<5.0.0` y `onnxruntime-gpu`. |
| [`config/extra_model_paths.yaml`](../config/extra_model_paths.yaml) | Cruce de directorios nativos (`checkpoints`, `diffusion_models`, `unet`, `clip`, `text_encoders`) sin sobrescribir las extensiones de los custom nodes. |
| [`config/manager-config.ini`](../config/manager-config.ini) | Configuración de `ComfyUI-Manager` apuntando a `Comfy-Org/ComfyUI-Manager` con `security_level = weak`. |
| [`docs/decisiones.md`](decisiones.md) | Tabla de investigación web verificada (`2026-10-16`), registro trazable de todas las decisiones de las auditorías V1, V2 y V3, y procedimiento de actualización de `nodes.lock` / `constraints.txt`. |
| [`README.md`](../README.md) | Guía de usuario paso a paso desde MacBook (100% Web), tabla de variables de entorno, catálogo de modelos con tamaños reales medidos (`63.35 GiB` total) y resolución de problemas. |

---

## 2. Verificaciones Estáticas y de Red Ejecutadas

1. **Sintaxis Bash y formato LF**:
   - `bash -n scripts/start.sh` y `bash -n scripts/download-models.sh` → `EXIT 0`.
   - `0` archivos con `CRLF` (`\r\n`) en todo el repositorio.
2. **Verificación activa de red (GitHub, PyPI, PyTorch y HuggingFace)**:
   - SHA de `hadolint/hadolint-action@v3.1.0` (`54c9adbab1582c2ef04b2016b760714a4bfde3cf`) y `actions/upload-artifact@v4.4.3` (`b4b15b8c7c6ac21ea08fcf65892d2ee8f75cf882`) verificados contra GitHub.
   - Los 12 commit SHAs de `config/nodes.lock` (`Comfy-Org/ComfyUI`, `facebookresearch/sam2` y 10 custom nodes) verificados directamente mediante `git fetch --depth=1 --filter=tree:0 <url> <sha>`.
   - Terna `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128` verificada en `https://download.pytorch.org/whl/cu128/` para `cp312-linux_x86_64`.
   - 26 modelos en `scripts/download-models.sh` verificados mediante `HEAD` contra HuggingFace (`26/26 HTTP 302`, `Gated: False`, `63.35 GiB` totales).
