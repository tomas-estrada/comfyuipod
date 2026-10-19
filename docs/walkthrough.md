# Walkthrough del Repositorio: ComfyUI "Everything-Ready" para Vast.ai

Este documento detalla la función de cada archivo del repositorio y las verificaciones realizadas tras las rondas de auditoría V1–V5 y la implementación de [`docs/propuestas/OBSERVABILIDAD_Y_PORTAL.md`](propuestas/OBSERVABILIDAD_Y_PORTAL.md).

---

## 1. Mapa de Archivos del Repositorio

| Archivo | Propósito y garantías clave |
|---|---|
| [`Dockerfile`](../Dockerfile) | Imagen basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` con `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128`, `ffmpeg` (`libx264` + `h264_nvenc`), `nginx`, `ttyd`, `tini`, `build-essential` + `cmake` + `pkg-config` + `python3-dev`, instalación en `/opt/ComfyUI`, reinstalación limpia `--no-deps --force-reinstall` de `opencv-python-headless` y `onnxruntime-gpu`, generación de `/etc/comfyui/image.json`, portal `/home`, `status-server.py`, `diag.sh` y warm-up `--quick-test-for-ci` con `timeout 600` y limpieza posterior. |
| [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml) | Pipeline CI/CD en dos etapas: (1) `validate` con `ShellCheck` (`start.sh`, `download-models.sh`, `diag.sh`, `tools/mac/comfy`), `py_compile` (`status-server.py`), validación de `triage_patterns.json` y ausencia de CDNs en HTML, `Hadolint`, verificación de los 12 commit SHAs de `nodes.lock` y verificación `HEAD` de los 26 modelos en HuggingFace; (2) `build-and-push` con candidato `sha-*`, smoke test E2E de 10 pasos (Nginx + `/healthz` + `401` + `/home` + `/status/api` + `/report.md` + `/diag` + `404`/`405` + kill -9 supervisor + `docker stop -t 15` SIGTERM) y promoción a `:latest`. |
| [`scripts/start.sh`](../scripts/start.sh) | Entrypoint ejecutado bajo `tini`: inicializa logs en `/var/log`, muestra metadatos de `/etc/comfyui/image.json`, verifica `ffmpeg` sin `SIGPIPE`, inicializa `/workspace/comfyui-data`, configura Basic Auth con `openssl rand -hex 12` por `stdin`, arranca Nginx + `status-server.py` (supervisado) + `ttyd` opcional (`ENABLE_WEB_TERMINAL=1`), ejecuta `diag.sh`, corre Fase 1 con `timeout 600` no fatal, lanza Fase 2 con `setsid -f` y supervisa ComfyUI en `127.0.0.1:18188` con clasificación de salidas, `/run/comfyui-supervisor.json` y apagado limpio ante `SIGTERM`. |
| [`scripts/status-server.py`](../scripts/status-server.py) | Servidor HTTP multi-hilo (`ThreadingHTTPServer`, stdlib Python 3.12) en `127.0.0.1:18189` que sirve `/status` (HTML WCAG AA con pausa, progreso en vivo y pestañas de logs), `/status/api` (JSON cacheado 3s), `/report.md` (informe Markdown <= 400 KB para IA con deriva de paquetes/nodos e inventario de modelos), `/diag` y `/logs/<nombre>?n=N`, con redacción automática de secretos (`COMFY_PASS`, `HF_TOKEN`). |
| [`scripts/diag.sh`](../scripts/diag.sh) | Autodiagnóstico no bloqueante al arranque con 10 pruebas acotadas (`<= 15s` c/u): GPU, `torch` CUDA `matmul` 1024×1024, `onnxruntime` `InferenceSession` real en memoria, `h264_nvenc`, `libx264`, workers Nginx `www-data`, disco/RAM/CPU, imports Python, `ldconfig` y variables de entorno enmascaradas. |
| [`scripts/triage_patterns.json`](../scripts/triage_patterns.json) | 17 reglas declarativas de triaje automático sobre logs del contenedor con severidad, título, causa probable y remediación accionable. |
| [`scripts/download-models.sh`](../scripts/download-models.sh) | Descargador en 2 fases con validación de `MODEL_PROFILE`, timeout por intento (`HF_FETCH_TIMEOUT=3600`), trazas estructuradas (`PLAN FASE`, `INICIO`, `OK`, `SKIP`, `TIMEOUT`, `ERROR`), 3 reintentos con backoff exponencial, `STAGE_DIR` en el mismo filesystem para `rename()` atómico y continuación idempotente de pendientes de Fase 1 en `--phase2`. |
| [`config/portal/index.html`](../config/portal/index.html) | Portal `/home` (`23.3 KB <= 40 KB`, cero CDN externo, contraste WCAG AA) con tarjetas de acceso rápido verificadas en vivo contra `/status/api` y botón de copia de `/report.md` con fallback `<textarea>`. |
| [`config/nginx.conf`](../config/nginx.conf) | Reverse proxy en `0.0.0.0:8188` con `user www-data;`, doble log (`/dev/std*` + `/var/log/nginx/*.log`), `/healthz`, `/favicon.ico` (`204`), `/home`, `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/*`, `/terminal/` (WebSocket `ttyd`) y proxy a ComfyUI (`127.0.0.1:18188`). |
| [`config/_starting.html`](../config/_starting.html) | Página HTML ligera mostrada durante la Fase 1 con auto-recarga cada 5s y enlaces directos al Portal `/home` y al Panel `/status`. |
| [`tools/mac/comfy`](../tools/mac/comfy) y [`tools/mac/README.md`](../tools/mac/README.md) | CLI helper para macOS / Linux (`comfy open`, `url`, `all`, `logs`, `pass`, `report`, `watch`, `rent`, `destroy`) compatible con el JSON de `vastai` 0.x y 1.x. |
| [`config/nodes.lock`](../config/nodes.lock) y [`config/constraints.txt`](../config/constraints.txt) | Registro de commits SHA verificados (ComfyUI, `sam2` y 10 custom nodes) y restricciones `PIP_CONSTRAINT` para proteger `torch==2.9.1+cu128`, `numpy<2.3.0`, `opencv-python-headless<5.0.0` y `onnxruntime-gpu`. |
| [`config/extra_model_paths.yaml`](../config/extra_model_paths.yaml) y [`config/manager-config.ini`](../config/manager-config.ini) | Cruce de directorios nativos de modelos y configuración de `ComfyUI-Manager` con `security_level = weak`. |
| [`docs/decisiones.md`](decisiones.md) y [`README.md`](../README.md) | Registro completo de investigación, decisiones de las auditorías V1–V5 + Observabilidad y Portal, y guía paso a paso de uso. |

---

## 2. Verificaciones Estáticas y de Red Ejecutadas

1. **Sintaxis Bash, Python, JSON y formato LF**:
   - `bash -n scripts/start.sh scripts/download-models.sh scripts/diag.sh tools/mac/comfy` → `EXIT 0`.
   - `python -m py_compile scripts/status-server.py` → `EXIT 0`.
   - Validación de esquema y compilación de las 17 expresiones regulares en `scripts/triage_patterns.json` → `17/17 OK`.
   - Tamaño de `config/portal/index.html` (`23.3 KB <= 40 KB`) y `0` referencias a CDNs externos en `config/portal/index.html` y `config/_starting.html`.
   - `0` archivos con `CRLF` (`\r\n`) en todo el repositorio.
2. **Verificación activa de red (GitHub, Launchpad, PyPI, PyTorch y HuggingFace)**:
   - Disponibilidad de `ttyd 1.7.4-1build2` en Ubuntu 24.04 `noble` (`universe`) verificada contra la API de Launchpad y código fuente de `tsl0922/ttyd@1.7.4`.
   - Dependencia transitiva `onnx>=1.13` en `insightface` y esquema JSON de `vastai` CLI verificados contra PyPI.
   - Los 12 commit SHAs de `config/nodes.lock` y los 26 modelos en HuggingFace (`63.35 GiB`) verificados.
