# Informe de Implementación: Observabilidad, Portal `/home` y Resiliencia (`OBSERVABILIDAD_Y_PORTAL.md`)

**Fecha**: `2026-10-16`
**Especificación base**: [`docs/propuestas/OBSERVABILIDAD_Y_PORTAL.md`](../propuestas/OBSERVABILIDAD_Y_PORTAL.md)
**Commit base previo**: `4842937` (cierre de `AUDITORIA_AGENTE_V1.md` a `AUDITORIA_AGENTE_V5.md`)

---

## 1. Investigación Previa Obligatoria (§14) — Resultados Verificados

Antes de escribir código se verificaron contra fuentes primarias los 4 puntos exigidos por la sección 14 de la propuesta:

1. **Disponibilidad y flags de `ttyd` en Ubuntu 24.04 (`noble`)**:
   - Consultada la API oficial de Launchpad (`getPublishedBinaries` para `binary_name=ttyd` en `ubuntu/noble`): el paquete oficial es **`ttyd 1.7.4-1build2`** en el componente `universe` (habilitado por defecto en `nvidia/cuda:12.8.0-runtime-ubuntu24.04`).
   - Verificado en el código fuente upstream [`tsl0922/ttyd@1.7.4` (`src/server.c` y `src/http.c`)](https://github.com/tsl0922/ttyd/tree/1.7.4/src):
     - Flags soportados: `-i` / `--interface`, `-p` / `--port`, `-b` / `--base-path`, `-W` / `--writable`.
     - Con `--base-path /terminal`, `ttyd` registra internamente los endpoints `/terminal/`, `/terminal/token` y `/terminal/ws`.
     - Por tanto, en [`config/nginx.conf`](../../config/nginx.conf), `location /terminal/` usa `proxy_pass http://127.0.0.1:7681;` (sin barra final en `proxy_pass`) para reenviar el path `/terminal/...` sin recortar el prefijo, y `location = /terminal` redirige (`301`) a `/terminal/`.
2. **Disponibilidad del paquete Python `onnx` para el test de `InferenceSession` en `scripts/diag.sh`**:
   - Verificado en los metadatos de PyPI de `insightface` (`https://pypi.org/pypi/insightface/json`): `requires_dist` declara explícitamente `'onnx>=1.13'`.
   - En [`scripts/diag.sh`](../../scripts/diag.sh), el check `ort` construye un grafo ONNX mínimo en memoria (`onnx.helper.make_node("Identity", ...)` con tensor `1x4 float32`) y ejecuta `ort.InferenceSession(..., providers=["CUDAExecutionProvider", "CPUExecutionProvider"]).run(...)`, comprobando que `sess.get_providers()[0] == "CUDAExecutionProvider"` (con fallback a `det_10g.onnx` si estuviera en disco).
3. **Rutas de `ComfyUI-Manager` en el SHA fijado (`4d3bed4b569205124a52444caf25de1719fae0de`)**:
   - Verificado `prestartup_script.py` en el commit fijado en [`config/nodes.lock`](../../config/nodes.lock): utiliza `folder_paths.get_system_user_directory()` (`user/__manager/config.ini`) o el fallback legacy (`user/default/ComfyUI-Manager/config.ini`). Ambos directorios se mantienen sincronizados y limpios tras el warm-up en [`Dockerfile`](../../Dockerfile) y [`scripts/start.sh`](../../scripts/start.sh).
4. **Esquema JSON de `vastai show instances --raw` y comportamiento de `vastai logs`**:
   - Inspeccionado el wheel oficial de `vastai` en PyPI (`vastai/cli/commands/instances.py` y `vastai/api/instances.py`):
     - En `vastai` 1.x, `vastai show instances --raw` devuelve un objeto JSON `{"instances": [...], "next_token": ..., "total_instances": ...}` (mientras que en `vastai` 0.x devolvía una lista `[...]`).
     - [`tools/mac/comfy`](../../tools/mac/comfy) soporta ambos esquemas (`parsed.get("instances", parsed)`), extrae el puerto público desde `ports["8188/tcp"][0]["HostPort"]` y en `comfy logs` soporta tanto el texto directo devuelto por `vastai logs` como el fallback `curl -fsSL` si alguna versión antigua devuelve una URL S3.

---

## 2. Trazabilidad Sección por Sección (`OBSERVABILIDAD_Y_PORTAL.md`)

| Sección | Archivos creados / modificados | Detalles de implementación y decisiones de ingeniería |
|---|---|---|
| **§2 Arquitectura y Mapa de Rutas** | [`config/nginx.conf`](../../config/nginx.conf), [`config/_starting.html`](../../config/_starting.html) | Único puerto público `8188/tcp`. `/healthz` sin Basic Auth (`HEAD /system_stats`); `/favicon.ico` devuelve `204` sin Basic Auth para evitar ruido en logs; `/home`, `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/*` y `/terminal/` protegidos por Basic Auth. `/download-status` mantenido por compatibilidad. Doble `access_log` / `error_log` hacia `/dev/std*` y `/var/log/nginx/*.log`. |
| **§3 Portal Web `/home`** | [`config/portal/index.html`](../../config/portal/index.html) | HTML5 + CSS + JS inline (`23.3 KB <= 40 KB`), `0` recursos externos (sin Google Fonts ni CDNs), `0` enlaces `http://`, paleta dark/light automática con ratios de contraste WCAG AA documentados en comentarios CSS (`15.4:1` texto primario, `7.6:1` secundario, `>= 5.8:1` semánticos), 7 tarjetas verificadas en vivo contra `/status/api` y modal `<textarea>` de respaldo cuando `navigator.clipboard` es bloqueado por el navegador en HTTP plano. |
| **§4 Panel `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/<nombre>`** | [`scripts/status-server.py`](../../scripts/status-server.py) | Servidor `ThreadingHTTPServer` (solo stdlib Python 3.12) en `127.0.0.1:18189`. Caché de 3s en `/status/api` y de 30s para `pip3 freeze` / `git rev-parse` en `/report.md`. Devuelve `405 Method Not Allowed` en métodos distintos de `GET`/`HEAD` y `404` en logs fuera de la lista blanca (`comfyui`, `nginx-error`, `nginx-access`, `download`/`download-models`, `supervisor`, `status-server`, `diag`, `container`/`start`, `ttyd`). Límite estricto `<= 400 KB` en `/report.md`. |
| **§5 Supervisor de ComfyUI y de `status-server.py`** | [`scripts/start.sh`](../../scripts/start.sh) | Escritura atómica de `/run/comfyui-supervisor.json` (`tmp` + `os.replace`). Clasificación de salidas (`137` OOM, `134`/`139` crash nativo, `1` excepción Python, `143` SIGTERM, otros), volcado de `free -m`, `nvidia-smi` y últimas 40 líneas de `comfyui.log` a `/var/log/supervisor.log`. Respeta `COMFY_MAX_RESTARTS` (default `5`) y `COMFY_RESTART_DELAY` (default `5`), mantiene Nginx y `/status` vivos en estado `exhausted` y captura `SIGTERM`/`SIGINT` para apagado limpio (`ExitCode=0`). Único `tail -n 0 -F /var/log/comfyui.log &` para no acumular procesos `tee`. |
| **§6 Motor de Triaje Automático** | [`scripts/triage_patterns.json`](../../scripts/triage_patterns.json), [`scripts/status-server.py`](../../scripts/status-server.py) | Implementadas las 17 reglas del catálogo §6.3 (`oom-rc137`, `hostbuffer`, `sm120`, `disk-full`, `cuda-oom`, `import-failed`, `frontend-mismatch`, `manager-500`, `nginx-502`, `hf-503`, `hf-stuck`, `nvenc-missing`, `ort-cpu`, `value-not-in-list`, `nginx-429`, `nginx-413`, `hf-timeout`) más alertas dinámicas por disco libre (`< 10 GiB` / `< 3 GiB`) y RAM disponible (`< 2 GiB`). |
| **§7 Autodiagnóstico `diag.sh` y Metadatos `/etc/comfyui/image.json`** | [`scripts/diag.sh`](../../scripts/diag.sh), [`Dockerfile`](../../Dockerfile) | 10 pruebas no bloqueantes con `timeout <= 15s` cada una (`gpu`, `torch` con `matmul` 1024×1024 en GPU, `ort` con `InferenceSession` real en memoria, `nvenc`, `libx264`, `nginx` workers `www-data`, `disk`/`ram`/`cpu`, `python-imports`, `ldconfig`, `env` enmascarado). `/etc/comfyui/image.json` generado en Step 6 del `Dockerfile` con `IMAGE_TAG` y `BUILD_DATE`. |
| **§8 Terminal Web Opcional (`ttyd`) y Redacción de Secretos** | [`Dockerfile`](../../Dockerfile), [`scripts/start.sh`](../../scripts/start.sh), [`scripts/status-server.py`](../../scripts/status-server.py) | `ttyd` instalado vía `apt` en `Dockerfile`, desactivado por defecto (`ENABLE_WEB_TERMINAL=0`). Si se activa con contraseña autogenerada, `start.sh` imprime advertencia explícita. `status-server.py` aplica `redact_secrets()` sobre todas las salidas (`/status/api`, `/report.md`, `/diag`, `/logs/*`) para garantizar que `COMFY_PASS` y `HF_TOKEN` nunca se filtren por HTTP. |
| **§9 Smoke Test Full-Stack en CI** | [`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) | Ampliado el job `validate` (`ShellCheck` de los 4 scripts Bash, `py_compile` de `status-server.py`, validación de esquema y regex de `triage_patterns.json`, tamaño `<= 40 KB` y 0 CDNs en HTML) y el job `build-and-push` con los 10 pasos E2E de §9.2 (`/healthz`, `401` sin auth, `/home`, `/status/api`, `/report.md` sin secretos, `/diag`, `404`/`405`, `kill -9` supervisor recovery y `docker stop -t 30` con `ExitCode=0`). |
| **§10 CLI `comfy` para macOS** | [`tools/mac/comfy`](../../tools/mac/comfy), [`tools/mac/README.md`](../../tools/mac/README.md) | Implementados todos los subcomandos (`open`, `url`, `all`, `logs`, `pass`, `report`, `watch`, `rent`, `destroy`), limpio bajo `ShellCheck`. |
| **§11 Endurecimiento de `download-models.sh`** | [`scripts/download-models.sh`](../../scripts/download-models.sh) | Añadido `timeout "${HF_FETCH_TIMEOUT:-3600}"` por intento en `hf_fetch`, líneas estructuradas `PLAN FASE 1/2`, `INICIO: ...` y `TIMEOUT: ...`, y documentación de `HF_HUB_DISABLE_XET=1`. |

---

## 3. Checklist de Criterios de Aceptación (§15)

- [x] Todos los scripts Bash (`scripts/start.sh`, `scripts/download-models.sh`, `scripts/diag.sh`, `tools/mac/comfy`) pasan `bash -n` y `ShellCheck` sin errores.
- [x] `scripts/status-server.py` compila con `python3 -m py_compile` usando únicamente la librería estándar de Python 3.12.
- [x] `scripts/triage_patterns.json` contiene los 17 patrones válidos y sus expresiones regulares compilan sin errores.
- [x] `config/portal/index.html` pesa `23.3 KB` (`<= 40 KB`), no contiene referencias `http://` ni CDNs externos, y documenta los ratios de contraste WCAG AA en comentarios CSS.
- [x] Ninguna restricción existente (`PIP_CONSTRAINT`, `nodes.lock`, `torch==2.9.1+cu128`, reinstalación limpia de `cv2` y `onnxruntime-gpu`) fue debilitada.
- [x] Todos los archivos del repositorio están normalizados a finales de línea `LF` (`0` archivos con `CRLF`).
