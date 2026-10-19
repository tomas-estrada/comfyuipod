# Propuesta: Portal `/home`, observabilidad sin SSH y herramienta `comfy` para la Mac

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha | 2026-10-16 |
| Autor | Agente auditor (rondas V1–V5) |
| Destinatario | Agente ejecutor. Este documento es la especificación completa; no hace falta leer las auditorías para implementarlo, pero sí respetar sus decisiones (resumidas en §1.3). |
| Estado | Autorizado por el dueño del proyecto. Pendiente de implementación. |
| Base | Commit `4842937` de `main`. |

---

## 0. Resumen ejecutivo

Hoy, si ComfyUI muere, si Manager no abre o si la Fase 2 se cuelga, la única herramienta del usuario es el botón **Logs** de Vast.ai, y como `start.sh` termina en `exec python3`, una caída de ComfyUI tumba el contenedor entero (incluido Nginx y `/download-status`). El usuario trabaja desde un MacBook, 100% web, con instancias efímeras que cuestan dinero por minuto.

Esta propuesta añade, **sin cambiar nada de lo que ya funciona**:

1. **Portal `/home`**: una página de inicio legible y usable con tarjetas hacia cada servicio activo (ComfyUI, estado, informe, descargas, diagnóstico, terminal), con estado real de cada uno. Es la única URL que el usuario necesita recordar.
2. **Servidor de estado** (`/status`, `/status/api`, `/report.md`, `/diag`, `/logs/*`): solo lectura, comandos fijos, misma Basic Auth. `/report.md` genera un Markdown pre-clasificado (tabla de triage con patrones conocidos) pensado para pegarlo a un agente de IA en la Mac, que propone el comando de corrección.
3. **Supervisor de ComfyUI** en `start.sh` en lugar de `exec`: reinicios con límite y registro de cada caída (código, hora, causa probable), de modo que el portal, el informe y las descargas sobrevivan a un OOM.
4. **Terminal web opcional** (`ttyd`) bajo `/terminal/`, desactivada por defecto, para ejecutar lo que el agente proponga sin salir del navegador.
5. **Herramienta `comfy` para la Mac**: un comando que localiza la instancia con el CLI de Vast.ai, espera a que Nginx responda y abre Chrome en `/home`. Subcomandos para logs, contraseña, alquiler y destrucción.
6. **Endurecimiento de la Fase 2** (timeout por archivo y paso de `HF_HUB_DISABLE_XET`), derivado de issues reales de `hf-xet`.
7. **CI de pila completa**: el smoke test levanta el contenedor en CPU y comprueba con `curl` cada endpoint, la autenticación, el usuario de los workers de Nginx y el supervisor. Cierra en CI varias verificaciones que hoy solo se podían hacer en Vast.ai.

Coste estimado en imagen: < 3 MB. Coste en runtime: un proceso Python en reposo (< 40 MB RSS) y, si se activa, `ttyd` (< 10 MB).

---

## 1. Alcance, principios y restricciones

### 1.1 En alcance
Todo lo enumerado en §0, más las actualizaciones de `README.md`, `docs/decisiones.md` y `docs/walkthrough.md` descritas en §10.

### 1.2 Fuera de alcance
- Volúmenes persistentes de Vast.ai (OPS-04). Se menciona en README como decisión pendiente; no se implementa.
- Cualquier endpoint que acepte comandos arbitrarios por HTTP. El único camino para ejecutar comandos es `ttyd` (shell real, opt-in) o SSH.
- Métricas históricas, bases de datos, Prometheus. Todo es "estado actual + últimas N líneas".
- Cambiar el modo de autenticación (sigue siendo Basic Auth de Nginx).

### 1.3 Decisiones previas que no se pueden revertir
- ComfyUI escucha solo en `127.0.0.1:18188`; Nginx es el único proceso en `0.0.0.0:8188`. Todo servicio nuevo escucha en loopback y se publica vía Nginx.
- `tini` es PID 1 y `/start.sh` su único hijo directo.
- `PIP_CONSTRAINT`, `nodes.lock`, la terna `torch==2.9.1+cu128` y la secuencia de limpieza de `cv2`/`onnxruntime` del `Dockerfile` no se tocan.
- `/healthz` sigue siendo `HEAD /system_stats` sin auth; debe devolver no-2xx cuando ComfyUI está caído (el supervisor no debe "falsear" salud).
- Ningún secreto (`COMFY_PASS`, `HF_TOKEN`, `.htpasswd`) se imprime en logs, páginas ni informes. Ver §8.

### 1.4 Principios de diseño
- **Solo lectura por defecto.** El servidor de estado ejecuta una lista cerrada de comandos sin parámetros del cliente. Los únicos parámetros aceptados son un nombre de log de una lista blanca y un número de líneas acotado.
- **Nada muerto en la UI.** Cada enlace del portal se comprueba en vivo; si un servicio no está disponible, la tarjeta se muestra deshabilitada con el motivo, nunca como un enlace roto.
- **Sin dependencias externas en el navegador.** Nada de CDN, fuentes web ni librerías; el portal debe funcionar aunque la instancia no tenga salida a Internet. HTML + CSS + JS vanilla en un solo archivo.
- **Legible.** Contraste mínimo WCAG AA (4.5:1 en texto), tema oscuro y claro según `prefers-color-scheme`, tamaños de fuente ≥ 14 px, funciona en una ventana de 390 px de ancho (iPhone) y en 1440 px.
- **Barato.** Lecturas de log con `tail` acotado, caché de 3 s para comandos, timeouts en todo subproceso.

---

## 2. Arquitectura

```
Internet ──► :8188 Nginx (Basic Auth, limit_req)
               ├── /                 → 127.0.0.1:18188  ComfyUI (sin cambios)
               ├── /ws               → 127.0.0.1:18188  (sin cambios)
               ├── /healthz          → HEAD /system_stats (sin cambios, sin auth)
               ├── /download-status  → alias log (sin cambios)
               ├── /home             → /etc/comfyui/portal/index.html   [NUEVO, estático]
               ├── /status           → 127.0.0.1:18189  status-server    [NUEVO]
               ├── /status/api       → 127.0.0.1:18189                   [NUEVO, JSON]
               ├── /report.md        → 127.0.0.1:18189                   [NUEVO, text/markdown]
               ├── /diag             → 127.0.0.1:18189                   [NUEVO, text/plain]
               ├── /logs/<nombre>    → 127.0.0.1:18189                   [NUEVO, text/plain]
               └── /terminal/        → 127.0.0.1:7681   ttyd (WebSocket) [NUEVO, opt-in]

tini (PID 1)
 └── /start.sh
      ├── nginx (daemon)
      ├── status-server.py            (siempre)
      ├── ttyd                        (solo si ENABLE_WEB_TERMINAL=1)
      ├── download-models.sh --phase2 (setsid -f, como hoy)
      └── bucle supervisor ──► python3 main.py  (reinicia con límite)
```

Puertos nuevos, todos en loopback: `18189` (status-server), `7681` (ttyd). Ninguno se expone con `EXPOSE`.

---

## 3. Portal `/home`

### 3.1 Archivo y servicio
- `config/portal/index.html` (un solo archivo, ≤ 40 KB, sin recursos externos). Copiado a `/etc/comfyui/portal/index.html`.
- Nginx: `location = /home { alias /etc/comfyui/portal/index.html; default_type text/html; add_header Cache-Control "no-store"; }` dentro del bloque autenticado, con `limit_req zone=auth_limit burst=60 nodelay`.
- Opcional: `location = /favicon.ico` que devuelva un SVG inline o 204, para que Chrome no registre 404 en el log.

### 3.2 Contenido y comportamiento

**Cabecera** (datos de `GET /status/api`, refresco cada 15 s):
- Nombre del proyecto, tag de imagen (`IMAGE_TAG` horneado en build, ver §7.2), GPU y driver, uptime del contenedor.
- Línea de fase: `Arrancando` / `Fase 1: descargando (x/12)` / `ComfyUI listo · Fase 2: 7/14 modelos` / `Fase 2 completada` / `ComfyUI caído (reinicio 2/5)`. El texto proviene del servidor, el portal solo lo pinta.
- Si `COMFY_PASS` fue autogenerada: aviso "la contraseña está en los logs del contenedor (`vastai logs <id>` o botón Logs)". Nunca la contraseña.

**Tarjetas** (grid responsive, 1 columna < 640 px, 2 columnas < 1100 px, 3 columnas en escritorio). Cada tarjeta: icono SVG inline, título, descripción de una línea, estado (punto de color + texto) y acción.

| Tarjeta | Ruta | Comprobación de estado | Si no está disponible |
|---|---|---|---|
| ComfyUI | `/` | `HEAD /system_stats` → 200 | "ComfyUI arrancando" (503) o "ComfyUI caído, el supervisor lo está reiniciando" (según `/status/api`) |
| Estado del sistema | `/status` | `HEAD /status` → 200 | "Servidor de estado no disponible" |
| Informe para agente de IA | `/report.md` | igual que Estado | igual |
| Descargas de modelos | `/download-status` | `HEAD` → 200 | "Aún no hay log de descargas" |
| Diagnóstico de arranque | `/diag` | `HEAD /diag` → 200 | "Diagnóstico aún no ejecutado" |
| Terminal web | `/terminal/` | `/status/api` → `terminal.enabled` y `HEAD /terminal/` → 200 | "Desactivada. Arranca la instancia con `-e ENABLE_WEB_TERMINAL=1` para activarla" |
| ComfyUI Manager | `/` + nota | igual que ComfyUI | Nota fija: "Se abre desde el botón Manager dentro de ComfyUI" |

Reglas:
- Las tarjetas deshabilitadas no son enlaces (`aria-disabled="true"`, sin `href`), tienen opacidad reducida pero texto legible, y muestran el motivo.
- "Informe para agente" tiene dos acciones: **Abrir** y **Copiar al portapapeles** (hace `fetch('/report.md')` y `navigator.clipboard.writeText`). Si el portapapeles falla (HTTP plano en algunos navegadores restringe `clipboard`), mostrar el texto en un `<textarea>` seleccionable con botón "Seleccionar todo". No puede fallar en silencio.
- Enlaces abren en la misma pestaña salvo ComfyUI y Terminal, que abren en pestaña nueva (`target="_blank" rel="noopener"`).
- Un pie con: ruta de datos, perfil de modelos activo, enlace a la documentación del repo y versión del portal.

### 3.3 Requisitos visuales (criterios de aceptación de UX)
- Tema oscuro por defecto si el sistema lo pide; claro en caso contrario. Variables CSS para colores; nada de imágenes de fondo.
- Contraste texto/fondo ≥ 4.5:1 en ambos temas (verificar con cualquier herramienta de contraste; anotar los pares de colores elegidos en un comentario del CSS).
- Estados con color **y** texto (no solo color): verde "Activo", ámbar "Arrancando", gris "No disponible", rojo "Caído".
- Fuente del sistema (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`), base 16 px, títulos de tarjeta 18 px.
- Navegable con teclado (orden de tabulación lógico, `:focus-visible` visible).
- Sin layout shift al cargar estados: las tarjetas reservan el espacio del indicador.
- Carga en < 200 ms sobre LAN; sin peticiones periódicas más frecuentes que cada 15 s.

### 3.4 Lo que el portal NO hace
- No ejecuta nada. No tiene formularios. No guarda nada en el navegador salvo la preferencia de tema (opcional, `localStorage`).

---

## 4. Servidor de estado (`scripts/status-server.py`)

### 4.1 Requisitos generales
- Python 3.12, **solo biblioteca estándar** (`http.server`, `json`, `subprocess`, `collections`, `re`, `time`, `pathlib`).
- Escucha en `127.0.0.1:18189`. `ThreadingHTTPServer` con un pool implícito; cada petición debe terminar en < 3 s.
- Arranca desde `start.sh` justo después de Nginx y antes de la Fase 1, con `setsid -f` y log en `/var/log/status-server.log`.
- Si muere, el supervisor de §5 lo relanza (es el segundo proceso supervisado); si no se puede relanzar, el portal lo indica, pero ComfyUI no se ve afectado.
- Todo subproceso con `timeout` (5 s por defecto; `nvidia-smi` 5 s; `pip3 freeze` 20 s y solo bajo demanda del informe).
- Caché en memoria de 3 s para `/status/api` y de 30 s para las secciones caras del informe (`pip freeze`, listado de modelos).

### 4.2 Endpoints

| Ruta | Tipo | Contenido |
|---|---|---|
| `GET /status` | `text/html` | Página de estado legible (misma hoja de estilo que el portal, embebida), auto-refresco cada 10 s mediante `fetch` de `/status/api`, no recarga completa. Secciones de §4.3. |
| `GET /status/api` | `application/json` | Objeto de §4.3. Es la fuente de verdad para el portal y para la página de estado. |
| `GET /report.md` | `text/markdown; charset=utf-8` | Informe de §6. Cabecera `Content-Disposition: inline; filename="comfyui-report-<timestamp>.md"`. |
| `GET /diag` | `text/plain` | Contenido de `/var/log/diag.txt` (ver §7.1) o `Diagnóstico aún no ejecutado` con 404. |
| `GET /logs/<nombre>?n=<N>` | `text/plain` | Últimas N líneas (por defecto 200, máximo 2000) de un log de la lista blanca: `comfyui`, `nginx-error`, `nginx-access`, `download`, `supervisor`, `status-server`, `diag`, `container` (= `/var/log/start.log`, ver §5). Cualquier otro nombre → 404. `n` no numérico → 400. |
| cualquier otra | | 404 en texto plano. Sin listado de directorios. Solo `GET` y `HEAD`. |

### 4.3 Contenido de `/status/api` (y secciones de `/status`)

```json
{
  "generated_at": "2026-10-16T18:42:11Z",
  "image": {"tag": "sha-4842937", "built_at": "...", "comfyui_sha": "bdc63e72…"},
  "instance": {"hostname": "...", "uptime_s": 2472, "vast_instance_id": "<de VAST_CONTAINERLABEL si existe>", "external_port_8188": "<VAST_TCP_PORT_8188 si existe>"},
  "gpu": {"available": true, "name": "NVIDIA GeForce RTX 5090", "driver": "580.xx", "vram_total_mb": 32607, "vram_used_mb": 1180, "util_pct": 0, "temp_c": 41, "processes": [{"pid": 1234, "name": "python3", "used_mb": 1100}]},
  "memory": {"total_mb": 64221, "available_mb": 51200, "swap_used_mb": 0},
  "disk": {"path": "/workspace", "total_gb": 150, "used_gb": 71, "free_gb": 79},
  "comfyui": {"state": "running|starting|down|disabled", "pid": 1234, "rss_mb": 6200, "uptime_s": 2400, "port_open": true, "restarts": 0, "last_exit": null, "max_restarts": 5},
  "phases": {"phase1": {"state": "done|running|timeout|skipped", "failed": []}, "phase2": {"state": "running", "done": 7, "total": 14, "failed": [], "current_file": "flux1-dev-fp8.safetensors", "hf_pid": 2345, "hf_rss_mb": 900}},
  "services": {"nginx": {"running": true, "workers_user": "www-data"}, "status_server": {"running": true}, "terminal": {"enabled": false, "running": false}},
  "errors_recent": [{"ts": "...", "source": "comfyui", "line": "torch.OutOfMemoryError: CUDA out of memory…"}],
  "triage": [{"severity": "alta", "pattern_id": "oom-rc137", "summary": "...", "first_command": "..."}],
  "env_masked": {"MODEL_PROFILE": "all", "COMFY_USER": "admin", "COMFY_PASS": "<definida por el usuario>", "HF_TOKEN": "<no definido>", "COMFYUI_EXTRA_ARGS": "", "HF_HUB_DISABLE_XET": "<no definido>", "ENABLE_WEB_TERMINAL": "0"}
}
```

Fuentes de cada campo:
- `comfyui.*` y `restarts/last_exit`: fichero de estado del supervisor `/run/comfyui-supervisor.json` (§5.3) + `ss -ltn` o `/proc/net/tcp` para `port_open` + `/proc/<pid>/status` para RSS.
- `phases.*`: parseo de `/var/log/download-models.log` (banners `INICIANDO FASE`, líneas `OK:`, `SKIP`, `ERROR:` y `RESUMEN`). Añadir en `download-models.sh` una línea de máquina al inicio de cada fase: `[MODELS] PLAN FASE 2: 14 archivos` para que el total sea exacto por perfil.
- `gpu.*`: `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` y `--query-compute-apps`. Si `nvidia-smi` no existe o falla, `available: false` y motivo.
- `errors_recent`: últimas 20 líneas de `comfyui`, `nginx-error` y `download` que casen con la lista de patrones de §6.3, con marca de tiempo si la línea la tiene.
- `env_masked`: solo las claves listadas, con valores sustituidos para `COMFY_PASS` y `HF_TOKEN` (`<definida por el usuario>` / `<no definido>`), nunca el valor.

### 4.4 Página `/status` (HTML)
- Misma estética que el portal (copiar el bloque CSS; no se comparte archivo para que cada página sea autocontenida).
- Secciones en este orden: **Triage** (si hay), **ComfyUI** (estado, PID, RSS, reinicios, última caída), **GPU**, **Memoria y disco**, **Descargas** (barra de progreso de Fase 2 con nombre del archivo actual), **Errores recientes** (lista con origen y hora), **Servicios** (nginx/status/terminal), **Logs** (pestañas: ComfyUI, Nginx, Descargas, Supervisor; cada una carga `/logs/<nombre>?n=200` con botón "más líneas" hasta 2000 y botón "copiar"), **Entorno** (enmascarado).
- Botón "Pausar actualización" para poder leer logs sin que se muevan.
- Todo texto de log en `<pre>` con `white-space: pre-wrap`, fuente monoespaciada 13 px, líneas que casan con patrones de error resaltadas (fondo tenue, no solo color de texto).

---

## 5. Supervisor de ComfyUI en `scripts/start.sh`

### 5.1 Comportamiento
Sustituir el `exec python3 main.py …` final por un bucle:

```bash
# Pseudocódigo normativo; el implementador escribe la versión final con shellcheck limpio.
COMFY_MAX_RESTARTS="${COMFY_MAX_RESTARTS:-5}"
COMFY_RESTART_DELAY="${COMFY_RESTART_DELAY:-5}"
attempt=0; comfy_pid=""
shutdown() { [ -n "$comfy_pid" ] && kill -TERM "$comfy_pid" 2>/dev/null; wait "$comfy_pid" 2>/dev/null; nginx -s quit 2>/dev/null; exit 0; }
trap shutdown TERM INT
while :; do
    attempt=$((attempt + 1))
    write_state running "$attempt" ""           # /run/comfyui-supervisor.json
    python3 main.py --listen 127.0.0.1 --port 18188 --extra-model-paths-config … ${COMFYUI_EXTRA_ARGS:-} \
        >> /var/log/comfyui.log 2>&1 &
    comfy_pid=$!
    wait "$comfy_pid"; rc=$?
    classify "$rc"                                # 137 → OOM RAM probable; 134/139 → fallo nativo; 1 → excepción Python; 124 → n/a
    write_state down "$attempt" "$rc"
    log "[SUPERVISOR] ComfyUI terminó rc=$rc (${reason}) intento ${attempt}/${COMFY_MAX_RESTARTS}"
    if [ "$attempt" -ge "$COMFY_MAX_RESTARTS" ]; then
        log "[SUPERVISOR] Límite alcanzado. Nginx, /home, /status y /terminal siguen activos para depurar. Reinicia la instancia cuando corrijas la causa."
        write_state exhausted "$attempt" "$rc"
        wait                                      # bloquea para siempre; tini sigue siendo PID 1
    fi
    sleep "$COMFY_RESTART_DELAY"
done
```

Reglas:
- El log de ComfyUI va a `/var/log/comfyui.log` **y** a la salida del contenedor. Implementación: al principio de `start.sh`, `tail -n 0 -F /var/log/comfyui.log &` (un solo `tail`, arrancado una vez). Así el botón Logs de Vast.ai y `vastai logs` siguen mostrando todo.
- Toda la salida de `start.sh` se duplica también a `/var/log/start.log` (`exec > >(tee -a /var/log/start.log) 2>&1` al inicio) para que `/logs/container` exista. Verificar que esto no reintroduce problemas de SIGPIPE ni cambia el PID 1.
- Antes de cada reinicio, guardar las 40 últimas líneas de `/var/log/comfyui.log` en `/var/log/supervisor.log` junto con `rc`, hora, `free -m` resumido y `nvidia-smi` resumido. Ese fichero alimenta la sección "Supervisor" del informe.
- `docker stop` debe seguir siendo limpio: el `trap` reenvía TERM a ComfyUI, espera y cierra Nginx. Probar en CI (§9).
- Caso `SKIP_MODEL_DOWNLOAD` no cambia. Caso `COMFY_MAX_RESTARTS=0`: comportamiento equivalente al actual (un solo intento, sin reinicio) pero sin `exec`, para que el portal siga vivo.

### 5.2 Clasificación de códigos de salida (`classify`)

| `rc` | Motivo mostrado | Pista en informe |
|---|---|---|
| 137 | `SIGKILL: casi seguro OOM de RAM del sistema` | Mostrar `free -m` y RSS de `hf` en el momento; sugerir host con más RAM, `MODEL_PROFILE` menor, `HF_HUB_DISABLE_XET=1` |
| 134 / 139 | `Abort / segfault en código nativo` | Sugerir revisar último nodo ejecutado y `--disable-pinned-memory` |
| 1 | `Excepción Python no capturada` | Mostrar último traceback |
| 143 | `SIGTERM (parada ordenada)` | No contar como fallo si vino del `trap` |
| otro | `Código ${rc}` | |

### 5.3 Fichero de estado `/run/comfyui-supervisor.json`
`{"state": "running|down|exhausted", "attempt": 2, "max": 5, "pid": 1234, "started_at": "...", "last_exit": {"rc": 137, "at": "...", "reason": "..."}}`. Escritura atómica (`tmp` + `mv`). Lo lee el servidor de estado.

### 5.4 Segundo proceso supervisado: `status-server.py`
Bucle mucho más simple: relanzar siempre con `sleep 2`, sin límite, en un subshell con `setsid -f`. Si falla 10 veces en 1 minuto, dejar de intentarlo y escribirlo en `/var/log/start.log`.

---

## 6. `/report.md`: informe para un agente de IA

### 6.1 Objetivo
Que el usuario pueda copiarlo y pegarlo a un agente de código en su Mac (o leerlo él) y que el agente tenga, sin preguntar nada: contexto de la arquitectura, estado actual, errores completos, qué ha cambiado respecto a la imagen original y una primera hipótesis.

### 6.2 Estructura (orden fijo; secciones vacías se omiten con una línea "sin datos")

```markdown
# ComfyUI Everything-Ready · Informe de diagnóstico
Generado: <ISO-8601 UTC> · Imagen: <tag> · ComfyUI: <sha corto> · Uptime: hh:mm:ss
GPU: <nombre> <VRAM> · Driver <ver> · torch <ver> · cuda.is_available=<bool>
RAM: <total> / <libre> · Disco /workspace: <total> / <libre> · Perfil: <MODEL_PROFILE>

## 0. Triage automático
| Sev. | Patrón | Evidencia (línea y origen) | Causa probable | Primer comando sugerido |
(ordenado por severidad; máximo 10 filas; "Sin coincidencias" si vacío)

## 1. Contexto fijo para el agente
- Rutas: app /opt/ComfyUI · datos /workspace/comfyui-data · configs /etc/comfyui · logs /var/log/{comfyui,start,supervisor,download-models,nginx/*}.log
- Procesos: tini(1) → start.sh → {nginx, status-server, ttyd?, download-models --phase2, python3 main.py (supervisado, máx N reinicios)}
- Puertos: 8188 público (nginx) → 18188 ComfyUI · 18189 status · 7681 ttyd (todos loopback salvo 8188)
- Cómo ejecutar comandos: terminal web en /terminal/ (si ENABLE_WEB_TERMINAL=1) · SSH si la instancia se creó en modo SSH · `vastai execute <id> 'ls|du|rm …'` para lo básico
- Cómo reiniciar ComfyUI sin reiniciar la instancia: `kill <pid>`; el supervisor lo relanza en 5 s
- Cómo reinstalar la terna PyTorch: <comando exacto con versiones>
- Restricciones: PIP_CONSTRAINT activo · no usar "Update ComfyUI" desde Manager · secretos no incluidos en este informe

## 2. Diagnóstico de arranque ([DIAG])
<contenido de /var/log/diag.txt>

## 3. ComfyUI y supervisor
estado, pid, rss, reinicios; tabla de caídas (hora, rc, motivo); últimas 40 líneas previas a cada caída (máx 3 caídas)

## 4. GPU, memoria y disco
nvidia-smi resumido + procesos en GPU; free -m; df -h /workspace; los 10 ficheros más grandes de output/ y temp/ si el disco está > 85 %

## 5. Errores de ComfyUI
Todos los tracebacks completos de las últimas 500 líneas (bloques `Traceback … Error:`), deduplicados, con hora; luego las últimas 150 líneas del log tal cual

## 6. Descargas
Estado Fase 1/2, archivo en curso con PID y RSS de hf, lista FAILED_MODELS, últimas 30 líneas del log

## 7. Nginx
Últimas 50 líneas de error.log; recuento de 429/502/413/401 en las últimas 500 líneas de access.log

## 8. Drift del entorno
- pip freeze vs /etc/comfyui/requirements.lock: añadidos / eliminados / cambiados (resaltar torch*, opencv*, onnxruntime*, numpy, huggingface-hub)
- custom_nodes/ vs /etc/comfyui/nodes.lock: SHA actual por nodo (git rev-parse HEAD), nodos no listados (instalados desde Manager), nodos con working tree sucio
- config.ini efectivo de Manager (security_level, update_policy, channel_url)
- Variables de entorno relevantes (enmascaradas)

## 9. Modelos
Por carpeta: número de archivos y tamaño; lista de los esperados por el perfil que faltan
```

Límite de tamaño: 400 KB. Si se supera, recortar primero §5 (líneas crudas), luego §4.

### 6.3 Tabla de triage (patrones iniciales)

| id | Dónde buscar | Patrón (regex, case-insensitive) | Sev. | Causa probable | Primer comando / acción |
|---|---|---|---|---|---|
| `oom-rc137` | supervisor | `rc=137` | alta | OOM de RAM del sistema (kernel mató a python3) | Ver `free -m` y RSS de `hf`; `MODEL_PROFILE` menor o host con más RAM; `HF_HUB_DISABLE_XET=1` |
| `cuda-oom` | comfyui | `CUDA out of memory\|torch\.OutOfMemoryError` | media | VRAM insuficiente para el workflow | Reducir resolución/batch; `--reserve-vram 2` o `--lowvram` en `COMFYUI_EXTRA_ARGS` |
| `hostbuffer` | comfyui | `HostBuffer\|read_file_slice failed\|pinned` | alta | Bug conocido de VRAM dinámica/memoria pinned en Blackwell (ComfyUI #15255/#14250) | `-e COMFYUI_EXTRA_ARGS=--disable-pinned-memory` y reiniciar |
| `sm120` | comfyui | `no kernel image is available\|sm_120` | alta | `torch` sin soporte Blackwell: alguien pisó la terna cu128 | Ver §8 drift; `pip3 install --force-reinstall torch==2.9.1+cu128 …` |
| `import-failed` | comfyui | `IMPORT FAILED` | media | Dependencia rota o nodo nuevo incompatible | Leer el traceback justo encima; `pip3 install -r custom_nodes/<nodo>/requirements.txt` |
| `frontend-mismatch` | comfyui | `comfyui-frontend-package.*(not installed\|outdated)` | media | Core actualizado sin el paquete pip del frontend | `pip3 install -r /opt/ComfyUI/requirements.txt`; no usar "Update ComfyUI" |
| `manager-500` | nginx-error, comfyui | `/api/manager.* 500\|Server got itself in trouble` | media | Excepción en backend de Manager | Buscar traceback en §5; revisar `security_level`; reiniciar ComfyUI |
| `value-not-in-list` | comfyui | `value not in list` | baja | Workflow referencia un modelo que no existe en el catálogo | Cambiar el checkpoint en el nodo; ver §9 modelos faltantes |
| `nginx-429` | nginx-access | ` 429 ` | baja | `limit_req` demasiado agresivo | Revisar `rate`/`burst` en `nginx.conf` |
| `nginx-502` | nginx-access | ` 502 \| 503 ` (fuera de arranque) | media | ComfyUI caído o sin responder | Ver §3 supervisor |
| `nginx-413` | nginx-access | ` 413 ` | baja | Límite de cuerpo (no debería, `client_max_body_size 0`) | Revisar config |
| `hf-503` | download | `503\|Service Unavailable` repetido | media | Inestabilidad de xet/HF | `HF_HUB_DISABLE_XET=1`; reintentar fase |
| `hf-stuck` | download, procesos | proceso `hf` con > 30 min sin avance en el log | media | Descarga colgada (hf_hub #4520) | `kill <pid hf>`; el bucle reintenta; `HF_HUB_DISABLE_XET=1` |
| `hf-timeout` | download | `ERROR: Falló la descarga` | baja | 3 intentos fallidos | Ver archivo y repo; reintentar con `download-models.sh --phase2` desde la terminal |
| `nvenc-missing` | diag | `NVENC FAIL\|h264_nvenc no detectado` | media | Driver sin NVENC o `NVIDIA_DRIVER_CAPABILITIES` incorrecto | Comprobar `nvidia-smi`; VHS caerá a `libx264` |
| `ort-cpu` | diag | `ORT providers activos: \['CPUExecutionProvider'\]` | media | onnxruntime-gpu sin CUDA EP real | Ver `ldconfig -p \| grep cudnn`; drift de `onnxruntime` |
| `disk-full` | api | `disk.free_gb < 10` | alta | Disco de la instancia lleno | Borrar `output/` o `temp/`; perfil menor |

La tabla vive en `scripts/triage_patterns.json` para que se pueda ampliar sin tocar el código. El servidor la carga al arrancar.

---

## 7. Diagnóstico de arranque y metadatos de imagen

### 7.1 `scripts/diag.sh`
- Lo lanza `start.sh` después de arrancar Nginx y el servidor de estado, **antes** de la Fase 1, con `timeout 90`. Escribe a stdout (log del contenedor) y a `/var/log/diag.txt`. Nunca aborta el arranque.
- Formato de cada línea: `[DIAG][OK|WARN|FAIL] <clave>: <detalle>`. Al final, `[DIAG] resumen: N ok, N warn, N fail`.
- Comprobaciones:
  1. `gpu`: `nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader`.
  2. `torch`: versión, `torch.version.cuda`, `torch.cuda.is_available()`, nombre del dispositivo, prueba de un `matmul` 1024×1024 en GPU con tiempo.
  3. `ort`: `InferenceSession` sobre un modelo `Identity` construido con `onnx.helper` en memoria, con `providers=['CUDAExecutionProvider','CPUExecutionProvider']`; `OK` solo si `get_providers()[0]` es CUDA. Si `onnx` no está instalado, usar `det_10g.onnx` si ya existe; si no, `WARN` con motivo.
  4. `nvenc`: `ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc=duration=1:size=640x360:rate=30 -c:v h264_nvenc -f null -`.
  5. `libx264`: lo mismo con `-c:v libx264`.
  6. `nginx`: `nginx -t` y usuario de los workers (`ps -o user= -C nginx` debe incluir `www-data`).
  7. `disk`, `ram`, `cpu`: `df -h /workspace`, `free -m`, `nproc`.
  8. `python-imports`: `import cv2, insightface, huggingface_hub, hf_xet` con versiones.
  9. `ldconfig`: `ldconfig -p | grep -c -E 'libcudnn|libcublas'` > 0.
  10. `env`: lista enmascarada (misma política que §4.3).
- Cada comprobación con su propio `timeout` (15 s máximo); un fallo no impide las siguientes.

### 7.2 Metadatos horneados en build
- `Dockerfile`: `ARG IMAGE_TAG=dev` y `ARG BUILD_DATE`; escribir `/etc/comfyui/image.json` con `{"tag": "...", "built_at": "...", "comfyui_sha": "<de nodes.lock>"}`. El workflow pasa `build-args: IMAGE_TAG=${{ fromJSON(steps.meta_candidate.outputs.json).tags[0] }}` y la fecha.
- `start.sh` imprime estos datos en la cabecera de arranque.

---

## 8. Terminal web (`ttyd`), Nginx y seguridad

### 8.1 `ttyd`
- `Dockerfile`: añadir `ttyd` al `apt-get install` de la capa 1 (paquete en `universe` de Ubuntu 24.04; si `apt` no lo encuentra, el build falla y se decide entonces si se descarga el binario de GitHub por SHA256).
- `start.sh`: si `ENABLE_WEB_TERMINAL=1`, lanzar `setsid -f ttyd --interface 127.0.0.1 --port 7681 --base-path /terminal --writable bash -l >> /var/log/ttyd.log 2>&1 < /dev/null`. Verificar en la versión instalada que `--base-path` existe (en 1.7.x es `-b, --base-path`); si no, servir en `/` y ajustar el `proxy_pass`.
- `start.sh`: si `ENABLE_WEB_TERMINAL=1` y `COMFY_PASS` no fue definida por el usuario, imprimir aviso destacado: "Terminal web activa con contraseña autogenerada. Si otra persona ve este log, tiene shell root".
- Nginx:
  ```nginx
  location /terminal/ {
      auth_basic "ComfyUI Vast.ai Access";
      auth_basic_user_file /etc/nginx/.htpasswd;
      proxy_pass http://127.0.0.1:7681;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Host $http_host;
      proxy_read_timeout 86400s;
      proxy_buffering off;
  }
  ```
  Si `ttyd` no está en ejecución, Nginx devuelve 502; el portal lo muestra como "Desactivada" gracias a `/status/api` (no se usa `error_page` aquí).

### 8.2 Resto de `location` nuevos en `nginx.conf`
- `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/`: `proxy_pass http://127.0.0.1:18189;` con auth, `limit_req zone=auth_limit burst=60 nodelay`, `proxy_read_timeout 30s`, `add_header Cache-Control "no-store" always`.
- `/home`: §3.1.
- Nada nuevo sin `auth_basic` salvo `/healthz` (ya existente).

### 8.3 Reglas de seguridad (criterios de aceptación)
1. Ningún endpoint nuevo responde sin credenciales (probar en CI: 401 sin auth en `/home`, `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/comfyui`, `/terminal/`).
2. `grep` del valor de `COMFY_PASS` y de un `HF_TOKEN` de prueba sobre `docker logs`, `/report.md`, `/status/api`, `/diag` y `/logs/*` → cero coincidencias (probar en CI con valores conocidos).
3. El servidor de estado rechaza cualquier método distinto de `GET`/`HEAD` (405) y cualquier ruta fuera de la lista (404). No hay interpolación de parámetros en comandos.
4. `ttyd` desactivado por defecto; documentado como "activar para una sesión de depuración y desactivar después".
5. Los puertos 18189 y 7681 no aparecen en `EXPOSE` ni escuchan fuera de loopback (probar con `ss -ltn` en el smoke test).

---

## 9. Cambios en CI (`.github/workflows/docker-publish.yml`)

### 9.1 Job `validate`
- `shellcheck` también sobre `scripts/diag.sh` y la parte Bash de `tools/mac/comfy`.
- `python3 -m py_compile scripts/status-server.py` y `python3 -c "import json; json.load(open('scripts/triage_patterns.json'))"`.
- Comprobación de que `config/portal/index.html` no contiene `http://` ni `https://` hacia recursos (solo enlaces de documentación permitidos en una lista explícita) para garantizar "sin CDN".

### 9.2 Smoke test de pila completa (nuevo paso, después del smoke actual)
```bash
docker run -d --name smoke -p 8188:8188 \
  -e SKIP_MODEL_DOWNLOAD=1 -e COMFYUI_EXTRA_ARGS=--cpu -e COMFY_PASS=ci-secret-9f8e7d \
  -e HF_TOKEN=hf_fake_token_for_ci_only -e ENABLE_WEB_TERMINAL=1 -e COMFY_MAX_RESTARTS=3 "$CANDIDATE_TAG"
# 1. esperar /healthz 200 (máx 600 s), mostrando docker logs si falla
# 2. sin auth → 401 en cada ruta nueva; /healthz → 200
# 3. con auth → 200 en /, /home, /status, /status/api, /report.md, /diag, /download-status; /terminal/ → 200
# 4. /home contiene "ComfyUI" y no contiene "http://" externos; /status/api es JSON con comfyui.state == "running"
# 5. /report.md contiene "## 0. Triage" y "## 8. Drift" y NO contiene "ci-secret-9f8e7d" ni "hf_fake_token"
# 6. docker logs smoke NO contiene "ci-secret-9f8e7d"
# 7. docker exec smoke ps -o user= -C nginx | grep -q www-data
# 8. docker exec smoke ss -ltn → 18189 y 7681 solo en 127.0.0.1
# 9. supervisor: docker exec smoke pkill -f "main.py" ; esperar ≤ 60 s a que /healthz vuelva a 200 ; /status/api .comfyui.restarts == 1
# 10. docker stop -t 30 smoke → código de salida 0 y sin procesos huérfanos (docker inspect .State.ExitCode)
docker rm -f smoke
```
Este paso cierra en CI, sin GPU: V4 (nginx como `www-data`, 401/200), la parte de proxy de V5, el supervisor y la política de secretos. Lo que sigue requiriendo GPU: V3, V8, V9, V10 y CUDA.

---

## 10. Herramienta para la Mac: `tools/mac/comfy`

### 10.1 Entregables
- `tools/mac/comfy` (Bash, `#!/usr/bin/env bash`, `set -euo pipefail`, pasa `shellcheck`), sin dependencias más allá de `vastai`, `python3` (el de macOS vale), `curl` y `open`.
- `tools/mac/README.md`: instalación (`pipx install vastai`, `vastai set api-key`, copiar a `~/bin`, `chmod +x`), verificación inicial, uso.

### 10.2 Subcomandos

| Comando | Qué hace |
|---|---|
| `comfy` / `comfy open` | Localiza la instancia `running` con etiqueta `comfy` (o la única `running`), resuelve `public_ipaddr` y `ports["8188/tcp"][0].HostPort`, espera hasta 10 min a que Nginx responda (200/401/503), abre Chrome (`open -a "Google Chrome"`, configurable con `COMFY_BROWSER`) en `http://IP:PUERTO/home`. |
| `comfy url` | Imprime la URL base. |
| `comfy all` | Abre `/`, `/status` y `/download-status` en pestañas. |
| `comfy logs [N]` | `vastai logs <id> --tail N` (por defecto 300). |
| `comfy pass` | Extrae usuario y contraseña autogenerada del log; si fue definida por el usuario, lo dice. |
| `comfy report` | `curl -u` con credenciales pedidas por `read -s` (no por argumento) → guarda `~/Downloads/comfyui-report-<ts>.md` y lo abre, o lo imprime con `--stdout` para encadenarlo a un agente. |
| `comfy watch` | `vastai show instances` cada 15 s. |
| `comfy rent [--gpu 5090\|4090] [--profile all\|general\|…] [--disk 150]` | Ejecuta la búsqueda del README, muestra las 5 ofertas más baratas con `$/h`, RAM del host, `$/GB·mes`, `$/TB`, `inet_down`, `cuda_max_good`; pide confirmación; crea la instancia con `--label comfy --direct` y la plantilla/imagen configuradas en `~/.config/comfy/config` (imagen, perfil, opciones). Nunca escribe `COMFY_PASS` en el historial: lo pide con `read -s` o lo deja autogenerar. |
| `comfy destroy` | Pide confirmación mostrando id, GPU y coste acumulado, y ejecuta `vastai destroy instance <id>`. |

### 10.3 Verificaciones obligatorias antes de dar por bueno
- Ejecutar `vastai show instances --raw` una vez y **confirmar los nombres de campo** (`actual_status`, `public_ipaddr`, `ports`, `label`). El auditor no pudo verificar la referencia completa del CLI; si difieren, ajustar y documentar.
- Confirmar que `vastai logs` imprime el log en stdout (y no solo una URL de S3); si solo da URL, hacer `curl` de esa URL.
- `comfy open` con la instancia en *Loading* debe decir claramente "aún no hay puerto mapeado" y salir con código 1, no colgarse.

---

## 11. Endurecimiento de descargas (`scripts/download-models.sh`)

1. `hf_fetch`: envolver cada intento en `timeout "${HF_FETCH_TIMEOUT:-3600}"` (segundos por intento; 16 GB a 50 Mbps ≈ 45 min). Un `rc=124` cuenta como intento fallido y se registra como `TIMEOUT`.
2. Línea de plan al inicio de cada fase para que el servidor de estado calcule progreso: `log "PLAN FASE 2: ${n} archivos (${perfil})"`, donde `n` se calcula contando las llamadas que el perfil ejecutará (función que devuelve la lista antes de descargar).
3. Antes de cada `hf download`, `log "INICIO: ${local_filename} (${repo_id}/${remote_path})"` para que `current_file` sea exacto.
4. Documentar en la cabecera del script que `HF_HUB_DISABLE_XET=1` lo lee `huggingface_hub` directamente (sin código nuevo).
5. Sin cambios en la semántica de reintentos ni en `STAGE_DIR`.

---

## 12. Cambios por archivo (lista de control para el ejecutor)

| Archivo | Cambio |
|---|---|
| `config/portal/index.html` | **Nuevo.** Portal §3. |
| `scripts/status-server.py` | **Nuevo.** §4 y §6. |
| `scripts/triage_patterns.json` | **Nuevo.** §6.3. |
| `scripts/diag.sh` | **Nuevo.** §7.1. |
| `scripts/start.sh` | Cabecera con `image.json`; `tee` a `/var/log/start.log`; arranque de status-server (supervisado), `diag.sh`, `ttyd` opcional; supervisor de ComfyUI §5. Mantener orden actual: ffmpeg → datos → auth → nginx → **status-server → diag** → Fase 1 → Fase 2 → ComfyUI. |
| `scripts/download-models.sh` | §11. |
| `config/nginx.conf` | `location` de §3.1, §8.1 y §8.2. |
| `Dockerfile` | `ttyd` en capa 1; `ARG IMAGE_TAG/BUILD_DATE` + `/etc/comfyui/image.json`; `COPY` de los nuevos archivos a `/etc/comfyui/portal/`, `/usr/local/bin/status-server.py`, `/usr/local/bin/diag.sh`, `/etc/comfyui/triage_patterns.json`; `sed` de CRLF y `chmod +x` como el resto; `nginx -t` ya cubre la config nueva. |
| `.github/workflows/docker-publish.yml` | §9. `build-args` para `IMAGE_TAG`/`BUILD_DATE`. |
| `tools/mac/comfy`, `tools/mac/README.md` | **Nuevos.** §10. |
| `README.md` | Sección 3: variables nuevas (`ENABLE_WEB_TERMINAL`, `COMFY_MAX_RESTARTS`, `COMFY_RESTART_DELAY`, `HF_FETCH_TIMEOUT`). Sección 4.B: sustituir "Botón Logs" por "`/home` → Estado" en B4; añadir `comfy` en A4 y B. Sección 1: punto 7 "Portal y diagnóstico sin SSH". Sección 5: entrada "Cómo pedir ayuda a un agente de IA con `/report.md`". |
| `docs/decisiones.md` | Nueva sección "Observabilidad": decisiones de §1.3/§1.4, `ttyd` opt-in, límite de reinicios, por qué no `supervisord` (un proceso más, config adicional, y los bucles de reinicio sin límite de ai-dock #8/#58 son justo lo que se quiere evitar). |
| `docs/walkthrough.md` | Filas para los archivos nuevos. |

---

## 13. Criterios de aceptación

### Funcionales
- [ ] Con `SKIP_MODEL_DOWNLOAD=1` y `--cpu`, `/home` muestra ComfyUI "Activo", Estado "Activo", Informe "Activo", Descargas "No disponible: omitidas", Diagnóstico "Activo", Terminal según `ENABLE_WEB_TERMINAL`.
- [ ] Matar `python3 main.py` a mano: en ≤ 10 s el portal muestra "Caído, reiniciando (1/5)"; en ≤ 60 s vuelve a "Activo"; `/status/api.comfyui.restarts == 1`; `/report.md` §3 muestra la caída con hora y `rc`.
- [ ] Con `COMFY_MAX_RESTARTS=1` y ComfyUI roto a propósito (`COMFYUI_EXTRA_ARGS=--port-invalido`), el contenedor **no se apaga**: `/home`, `/status`, `/report.md` siguen respondiendo y el triage lo explica.
- [ ] `docker stop` termina limpio en < 30 s.
- [ ] `/report.md` pegado tal cual a un agente de IA le basta para proponer un comando en los tres escenarios de prueba: `rc=137` simulado (`kill -9`), `IMPORT FAILED` simulado (romper un `requirements` de un nodo en un contenedor de prueba) y `value not in list`.
- [ ] `comfy open` desde una Mac abre Chrome en `/home` de una instancia real; `comfy pass` muestra la contraseña autogenerada; `comfy destroy` destruye.

### UX
- [ ] Portal legible en tema claro y oscuro, contraste ≥ 4.5:1 verificado, sin scroll horizontal a 390 px.
- [ ] Ninguna tarjeta enlaza a algo que devuelva 404/502; las no disponibles están deshabilitadas con motivo.
- [ ] "Copiar informe" funciona en Chrome y Safari sobre HTTP plano, o muestra el `<textarea>` alternativo.
- [ ] Sin peticiones a dominios externos (pestaña Network vacía de terceros).

### Seguridad
- [ ] Los 5 puntos de §8.3 verificados en CI.

### CI y documentación
- [ ] Workflow en verde con el smoke test de §9.2 incluido.
- [ ] README, decisiones y walkthrough actualizados; ninguna afirmación sin respaldo en el código (estándar de las auditorías).

---

## 14. Orden de implementación sugerido

1. `image.json` + supervisor + `start.log` (§5, §7.2). Es el cambio con más riesgo; probar `docker stop` y reinicio antes de seguir.
2. `status-server.py` con `/status/api` y `/logs/*` (§4). Luego `/status` HTML.
3. `diag.sh` (§7.1) y `/diag`.
4. `triage_patterns.json` + `/report.md` (§6).
5. Portal `/home` (§3) y `location` de Nginx (§8.2).
6. `ttyd` (§8.1).
7. Endurecimiento de descargas (§11).
8. Smoke test de pila completa (§9).
9. `tools/mac/comfy` (§10), probado contra una instancia real.
10. Documentación (§12) y entrega para auditoría V6.

Puntos que el ejecutor debe **verificar antes de asumir**: flag `--base-path` de la versión de `ttyd` instalada; nombres de campo de `vastai show instances --raw`; rutas de caché de Manager en el SHA fijado (para la limpieza de L-07 y la sección 8 del informe); si `onnx` queda instalado como dependencia transitiva (para la prueba ORT de §7.1).

---

## 15. Decisiones que necesita el dueño antes de empezar

| # | Decisión | Recomendación del auditor |
|---|---|---|
| D1 | `ttyd` incluido en la imagen (opt-in por variable) o excluido por completo | Incluido, desactivado por defecto. Es la pieza que convierte el informe en acción sin SSH. |
| D2 | `COMFY_MAX_RESTARTS` por defecto | 5, con 5 s de espera. Suficiente para sobrevivir a un OOM puntual sin enmascarar un fallo permanente. |
| D3 | ¿El informe incluye `pip freeze` completo o solo el diff contra `requirements.lock`? | Solo el diff (más corto y es lo que importa); el completo está en `/logs/` si hace falta. |
| D4 | ¿`comfy rent` crea instancias automáticamente o solo muestra ofertas? | Muestra y pide confirmación explícita antes de crear. Nunca sin confirmación. |
| D5 | Idioma de la UI del portal e informe | Español en el portal (usuario final); informe con cabeceras en español y comandos/nombres técnicos sin traducir. |
