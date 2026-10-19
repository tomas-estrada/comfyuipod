# Walkthrough del Repositorio: ComfyUI "Everything-Ready" para Vast.ai

Este documento detalla la función de cada archivo del repositorio y las verificaciones realizadas tras las rondas de auditoría V1–V6 y la implementación de [`docs/propuestas/OBSERVABILIDAD_Y_PORTAL.md`](propuestas/OBSERVABILIDAD_Y_PORTAL.md).

---

## 1. Mapa de Archivos del Repositorio

| Archivo | Propósito y garantías clave |
|---|---|
| [`Dockerfile`](../Dockerfile) | Imagen basada en `nvidia/cuda:12.8.0-runtime-ubuntu24.04` con `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128`, `ffmpeg` (`libx264` + `h264_nvenc`), `nginx`, `ttyd`, `tini`, `procps` (`O-06`), `build-essential` + `cmake` + `pkg-config` + `python3-dev`, instalación en `/opt/ComfyUI`, reinstalación limpia `--no-deps --force-reinstall` de `opencv-python-headless` y `onnxruntime-gpu`, generación de `/etc/comfyui/image.json`, portal `/home`, `status-server.py`, `diag.sh`, validación de `>= 17` reglas en `triage_patterns.json` (`O-12`) y warm-up `--quick-test-for-ci` con `timeout 600` y limpieza posterior. |
| [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml) | Pipeline CI/CD en dos etapas: (1) `validate` con `ShellCheck` (`start.sh`, `download-models.sh`, `diag.sh`, `tools/mac/comfy`), `py_compile` (`status-server.py`), validación de `triage_patterns.json`, paridad de los 26 modelos entre `download-models.sh` y `EXPECTED_MODELS` (`O-11`), ausencia de CDNs en HTML, `Hadolint`, verificación de los 12 commit SHAs de `nodes.lock` y verificación `HEAD` de los 26 modelos en HuggingFace; (2) `build-and-push` con candidato `sha-*`, smoke test E2E de 10 pasos + checks `O-07` (`0` `IMPORT FAILED`) y `O-05` (`0` `COMFY_PASS=` en `/proc/<pid>/environ`) y promoción a `:latest`. |
| [`scripts/start.sh`](../scripts/start.sh) | Entrypoint ejecutado bajo `tini`: inicializa logs en `/var/log`, persiste `/run/comfyui-started-at` (`O-10`), muestra metadatos de `/etc/comfyui/image.json`, verifica `ffmpeg` sin `SIGPIPE`, inicializa `/workspace/comfyui-data`, configura Basic Auth con `openssl rand -hex 12` por `stdin`, arranca Nginx + `status-server.py` (supervisado) + `ttyd` opcional con `env -u COMFY_PASS`, ejecuta `diag.sh`, limpia `unset COMFY_PASS` (`O-05`), corre Fase 1 con `timeout 600` no fatal, lanza Fase 2 con `setsid -f`, trunca logs si superan `LOG_MAX_MB` (`200 MB`, `O-04`) y supervisa ComfyUI en `127.0.0.1:18188` con clasificación de salidas, reset tras `COMFY_HEALTHY_UPTIME` (`600s`, `O-02`), `/run/comfyui-supervisor.json` y apagado limpio ante `SIGTERM` (`docker stop -t 30`). |
| [`scripts/status-server.py`](../scripts/status-server.py) | Servidor HTTP multi-hilo (`ThreadingHTTPServer`, stdlib Python 3.12) en `127.0.0.1:18189` con hilo recolector en segundo plano cada 60s para `pip3 freeze` y `git` (`O-03`), `tail_lines()` por bloques de 8 KiB desde `SEEK_END` (`O-04`), `/status` (HTML WCAG AA), `/status/api` (JSON cacheado 3s), `/report.md` (informe Markdown <= 400 KB con fechas ISO-8601 UTC en `last_exit.at`), `/diag` y `/logs/<nombre>?n=N`, con redacción automática de secretos (`COMFY_PASS`, `HF_TOKEN`). |
| [`scripts/diag.sh`](../scripts/diag.sh) | Autodiagnóstico no bloqueante al arranque con 10 pruebas acotadas (`<= 15s` c/u): GPU, `torch` CUDA `matmul` 1024×1024, `onnxruntime` `InferenceSession` real en memoria, `h264_nvenc`, `libx264`, workers Nginx `www-data`, disco/RAM/CPU, imports Python, `ldconfig` y variables de entorno enmascaradas. |
| [`scripts/triage_patterns.json`](../scripts/triage_patterns.json) | 17 reglas declarativas de triaje automático sobre logs del contenedor con severidad, causa probable y primer comando sugerido (`hf-503` acotado para evitar falsos positivos con tamaños o puertos, y `nginx-502` suprimido cuando `comfyui.state == "running"`, `O-08`). |
| [`scripts/download-models.sh`](../scripts/download-models.sh) | Descargador en 2 fases con validación de `MODEL_PROFILE`, timeout por intento (`HF_FETCH_TIMEOUT=3600`), trazas estructuradas (`PLAN FASE`, `INICIO`, `OK`, `SKIP`, `TIMEOUT`, `ERROR`), 3 reintentos con backoff exponencial, `STAGE_DIR` en el mismo filesystem para `rename()` atómico y continuación idempotente de pendientes de Fase 1 en `--phase2`. |
| [`config/portal/index.html`](../config/portal/index.html) | Portal `/home` (`<= 40 KB`, cero CDN externo, contraste WCAG AA) con tarjetas verificadas en vivo contra `/status/api`, distinción explícita de `supervisor_state === "exhausted"` (`O-09b`), detección de `[DIAG] resumen:` (`O-09c`), estado `api.services.terminal.running` (`O-09d`) y aviso de error si falla la copia de `/report.md` (`O-09e`). |
| [`config/nginx.conf`](../config/nginx.conf) | Reverse proxy en `0.0.0.0:8188` con `user www-data;`, doble log (`/dev/std*` + `/var/log/nginx/*.log`), `/healthz`, `/favicon.ico` (`204`), `/home`, `/status`, `/status/api`, `location = /report.md` con `proxy_read_timeout 120s;` (`O-03`), `/diag`, `/logs/*`, `/terminal/` (WebSocket `ttyd`) y proxy a ComfyUI (`127.0.0.1:18188`). |
| [`config/_starting.html`](../config/_starting.html) | Página HTML ligera mostrada durante la Fase 1 con auto-recarga cada 5s y enlaces directos al Portal `/home` y al Panel `/status`. |
| [`tools/mac/comfy`](../tools/mac/comfy) y [`tools/mac/README.md`](../tools/mac/README.md) | CLI helper para macOS / Linux (`comfy open`, `url`, `all`, `logs`, `pass`, `report`, `watch`, `rent`, `destroy`) con exigencia explícita de `COMFY_IMAGE` (`O-12`), credenciales a `curl` por `stdin` (`curl -K -`, `O-13`), espera de hasta 10 min en `comfy open` (`O-14`) y tabla de precios + confirmación interactiva + `--dry-run` + `--direct --args` en `comfy rent` (`O-01`). |
| [`config/nodes.lock`](../config/nodes.lock) y [`config/constraints.txt`](../config/constraints.txt) | Registro de commits SHA verificados (ComfyUI, `sam2` y 10 custom nodes) y restricciones `PIP_CONSTRAINT` para proteger `torch==2.9.1+cu128`, `numpy<2.3.0`, `opencv-python-headless<5.0.0` y `onnxruntime-gpu`. |
| [`config/extra_model_paths.yaml`](../config/extra_model_paths.yaml) y [`config/manager-config.ini`](../config/manager-config.ini) | Cruce de directorios nativos de modelos y configuración de `ComfyUI-Manager` con `security_level = weak`. |
| [`docs/decisiones.md`](decisiones.md) y [`README.md`](../README.md) | Registro completo de investigación, decisiones de las auditorías V1–V6 + Observabilidad y Portal, y guía paso a paso de uso. |

---

## 2. Verificaciones Estáticas y de Red Ejecutadas

1. **Sintaxis Bash, ShellCheck, Python, JSON y formato LF**:
   - `shellcheck scripts/start.sh scripts/download-models.sh scripts/diag.sh tools/mac/comfy` → `EXIT 0` (`0` warnings/errors).
   - `bash -n scripts/start.sh scripts/download-models.sh scripts/diag.sh tools/mac/comfy` → `EXIT 0`.
   - `python -m py_compile scripts/status-server.py` → `EXIT 0`.
   - Validación de esquema y compilación de las 17 expresiones regulares en `scripts/triage_patterns.json` → `17/17 OK`.
   - Paridad de los 26 modelos entre `scripts/download-models.sh` y `EXPECTED_MODELS` en `scripts/status-server.py` (`O-11`) → `26/26 OK`.
   - Tamaño de `config/portal/index.html` (`<= 40 KB`) y `0` referencias a CDNs externos en `config/portal/index.html` y `config/_starting.html`.
   - `0` archivos con `CRLF` (`\r\n`) en todo el repositorio.
2. **Verificación activa de red (GitHub, Launchpad, Ubuntu Packages, PyPI, Vast.ai y HuggingFace)**:
   - Disponibilidad de `ttyd 1.7.4-1build2` y dependencia `cmake -> procps` en Ubuntu 24.04 `noble` verificadas contra Launchpad y `packages.ubuntu.com`.
   - Comportamiento de `--direct --args` (`runtype="args"`) verificado contra `vast-ai/vast-cli` (`vast.py`).
   - Los 12 commit SHAs de `config/nodes.lock` y los 26 modelos en HuggingFace (`63.35 GiB`) verificados.

