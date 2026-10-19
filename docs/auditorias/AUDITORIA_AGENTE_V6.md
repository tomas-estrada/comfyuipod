# Auditoría de Agente — Versión 6 (implementación de Observabilidad y Portal)

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha | 2026-10-16 |
| Commit auditado | `f0fd82d` — `feat(observability): implement portal, status-server, diag.sh, supervisor, ttyd, and mac comfy CLI` sobre `4842937`, rama `main`, árbol limpio. 18 archivos, +4264/−121. |
| Referencia | [`docs/propuestas/OBSERVABILIDAD_Y_PORTAL.md`](../propuestas/OBSERVABILIDAD_Y_PORTAL.md) (§3–§15) e informe del ejecutor [`IMPLEMENTACION_OBSERVABILIDAD_Y_PORTAL.md`](IMPLEMENTACION_OBSERVABILIDAD_Y_PORTAL.md). |
| Alcance | Lectura completa de los archivos nuevos (`status-server.py` 1435 líneas, `diag.sh`, `triage_patterns.json`, `config/portal/index.html`, `tools/mac/comfy`, `tools/mac/README.md`) y del diff de los modificados (`Dockerfile`, `nginx.conf`, `start.sh`, `download-models.sh`, `_starting.html`, workflow, `README.md`, `decisiones.md`, `walkthrough.md`). **Rol de auditor: no se ejecutó ningún comando de prueba, build ni workflow.** Las afirmaciones del ejecutor sobre verificaciones de red (Launchpad, PyPI, código de `vastai`) se aceptan como declaradas; no se repitieron. |
| Destinatario | Agente ejecutor y dueño del proyecto. Todos los cambios derivados los hará el ejecutor; el auditor los revisará en V7. |

---

## 0. Veredicto

**Implementación completa, fiel a la propuesta en lo esencial y sin bloqueantes para el primer push.** Los 11 componentes de §12 existen, el contrato de endpoints y el esquema de `/status/api` coinciden con §4, el supervisor cumple §5 (incluido el estado `exhausted` sin apagar el contenedor y el apagado limpio por `SIGTERM`), el smoke test de §9.2 está implementado con sus 10 pasos y la política de secretos de §8.3 se prueba en CI.

Hallazgos: **0 altos, 5 medios, 10 bajos**. El más relevante funcionalmente es **O-01**: `comfy rent` crea la instancia (y empieza a facturar) sin mostrar ofertas ni pedir confirmación, en contra de la decisión D4 de la propuesta. El más relevante de higiene es **O-05**: `COMFY_PASS` queda exportada en el entorno del proceso de ComfyUI y de la Fase 2 cuando solo la necesita `status-server.py`.

Ninguno de los hallazgos exige rehacer diseño: son correcciones acotadas sobre lo entregado. La implementación en Python/Bash queda como **comportamiento de referencia** para una eventual migración a Rust (ver §7 y [`docs/propuestas/IMPLEMENTACION_RUST.md`](../propuestas/IMPLEMENTACION_RUST.md)); no se recomienda iniciar esa migración antes del primer workflow verde y una sesión de aceptación en GPU.

---

## 1. Trazabilidad contra la propuesta

| Sección de la propuesta | Estado | Evidencia y desviaciones |
|---|---|---|
| **§3 Portal `/home`** | ✅ | `config/portal/index.html` (7 tarjetas, estados `Activo/Arrancando/Caído/No disponible`, `HEAD` a `/system_stats`, `/status`, `/download-status`, `/diag`, `/terminal/`; copia de `/report.md` con `<textarea>` de respaldo; sin CDN; pares de contraste documentados; `aria-disabled`, `role="status"`). Nginx: `location = /home` con `alias` a fichero, auth y `limit_req`. Desviaciones de UX menores en O-09. |
| **§4 `status-server.py`** | ✅ | stdlib, `ThreadingHTTPServer` en `127.0.0.1:18189`; endpoints `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/<lista blanca>?n=`; 404 fuera de lista, 405 en `POST/PUT/DELETE`, 400 si `n` no numérico; caché 3 s (`api`) y 30 s (secciones pesadas); `redact_secrets()` aplicado a `run_cmd`, `tail_lines`, JSON y Markdown; `Cache-Control: no-store`. Esquema de `/status/api` igual al de §4.3 (`image`, `instance`, `gpu`, `memory`, `disk`, `comfyui`, `phases`, `phase_banner`, `services`, `errors_recent`, `triage`, `env_masked`). Rendimiento: O-03, O-04. |
| **§5 Supervisor** | ✅ | `start.sh:306-428`: bucle `while`, `wait || rc=$?`, clasificación 137/134\|139/1/143/otro, volcado a `/var/log/supervisor.log`, `/run/comfyui-supervisor.json` escrito de forma atómica (`os.replace`) con `state/attempt/restarts/max/pid/started_at/last_exit{rc,code,reason,at}`, `exhausted` con bucle `sleep 5 &`+`wait` interrumpible, `trap shutdown TERM INT` (TERM → 10 s → KILL, `nginx -s quit`, `exit 0`). `status-server.py` con su propio supervisor (10 fallos en 60 s → abandono). Única objeción de política: O-02. |
| **§6 `/report.md` y triage** | ✅ | Secciones 0–9 en el orden de §6.2, cabeceras literales (`## 0. Triage automático`, `## 8. Drift del entorno`). 17 patrones en `triage_patterns.json` con `id/sources/pattern/severity/cause/first_command`; fuentes virtuales `processes` (hf sin avance > 30 min) y `api` (disco < 10 GB) implementadas. Drift: `pip3 freeze` vs `/etc/comfyui/requirements.lock` (solo diff, D3), `git rev-parse` y `status --porcelain` por nodo vs `nodes.lock` (formato `node <dir> <url> <sha>` parseado correctamente), inventario de `models/` y ausentes por perfil. Tracebacks deduplicados (últimos 5). Límite 400 KB. Calidad de patrones: O-08. |
| **§7 `diag.sh` e `image.json`** | ✅ | 10 comprobaciones con `timeout` ≤ 15 s cada una, `set -uo pipefail` sin `-e`, `exit 0` siempre; ORT con `InferenceSession` real (grafo `Identity` vía `onnx.helper`, fallback a `det_10g.onnx`); env enmascarado. `Dockerfile:169-196`: `ARG IMAGE_TAG/BUILD_DATE`, `image.json` con `tag/built_at/comfyui_sha/pytorch/cuda/ubuntu`. Dependencia implícita: O-06. |
| **§8 `ttyd`, Nginx, seguridad** | ✅ | `ttyd` en capa 1 con `ttyd --version`; `setsid -f ttyd --interface 127.0.0.1 --port 7681 --base-path /terminal --writable bash -l` solo con `ENABLE_WEB_TERMINAL=1`; aviso si la clave es autogenerada; `location /terminal/` como en §8.1 más `location = /terminal` → 301. Regex `location ~ ^/(status\|status/api\|report\.md\|diag\|logs/.*)$` con auth, `limit_req`, `proxy_read_timeout 30s`. `EXPOSE 8188` únicamente. Los 5 puntos de §8.3 están cubiertos en CI (punto 5 con `/proc/net/tcp` en lugar de `ss -ltn`, porque `iproute2` no está instalado: sustitución válida). Higiene de entorno: O-05. |
| **§9 CI** | ✅ | `validate`: `shellcheck` sobre 4 scripts, `py_compile`, esquema de los 17 patrones (claves, severidades, `re.compile`, ids únicos), portal < 40 KiB, sin `http://`, sin recursos externos, lista blanca de enlaces. `build-and-push`: `build-args` `IMAGE_TAG`/`BUILD_DATE` desde `meta_candidate`; smoke full-stack de 10 pasos con `set -euo pipefail`, `trap cleanup EXIT`, paso 9 con `kill -9` (rc=137) en lugar de `pkill` (rc=143): equivalente y más representativo del caso OOM; paso 10 `docker stop -t 30` → `ExitCode=0`. Regresión menor: O-07. |
| **§10 `tools/mac/comfy`** | ⚠️ | Subcomandos `open/url/all/logs/pass/report/watch/rent/destroy` presentes; parseo de `{"instances": [...]}` y lista; `ports["8188/tcp"][0].HostPort`; `comfy pass` con regex coherente con `start.sh:224` (`Contraseña : <hex>`); `comfy report` pide la clave con `read -s` si no la deduce. **Desviaciones**: `comfy rent` sin confirmación ni listado de ofertas (O-01); `comfy all` lista instancias en vez de abrir pestañas (aceptable, documentar); `comfy open` no espera a que Nginx responda (spec: hasta 10 min); `COMFY_BROWSER` no implementado. |
| **§11 descargas** | ✅ | `HF_FETCH_TIMEOUT` (3600) por intento, `rc=124` → `TIMEOUT:` y cuenta como intento; `INICIO:` antes de cada descarga; `PLAN FASE 1: 12` y `PLAN FASE 2: 14/10/10/10/0` coherentes con las llamadas `hf_fetch` por perfil (recuento verificado línea a línea); cabecera documenta `HF_HUB_DISABLE_XET`. Semántica de reintentos y `STAGE_DIR` intacta. |
| **§12 docs** | ⚠️ | README (secciones 1, 3, 4, 5), `walkthrough.md` y `decisiones.md` actualizados. Faltan: justificación "por qué no `supervisord`" exigida en §12; errores de cifra en O-12. |
| **§15 decisiones** | ⚠️ | D1 (ttyd opt-in) ✅, D2 (5 reinicios, 5 s) ✅, D3 (solo diff) ✅, D5 (español) ✅. **D4 incumplida** (O-01). |

### Regresiones revisadas sobre lo ya existente

| Punto | Resultado |
|---|---|
| Orden de arranque | ffmpeg → datos → auth → Nginx+status-server+ttyd → diag → Fase 1 → Fase 2 (`setsid -f`, L-03) → supervisor. Igual que §12. |
| `/healthz`, `/ws`, `/download-status`, `_starting.html` | Sin cambios de semántica. `location = /favicon.ico` sin auth devuelve 204 sin tocar backend: inocuo. |
| Prioridad de `location` en Nginx | `=` gana sobre regex; la regex nueva no captura `/ws`, `/api/...`, `/system_stats` ni rutas de Manager (`/api/manager/...`). `location /terminal/` (prefijo) recibe `/terminal/ws` y `/terminal/token` porque la regex no los cubre. Correcto. |
| `PIP_CONSTRAINT`, `nodes.lock`, `constraints.txt`, warm-up con `IMPORT FAILED` | Intactos (`Dockerfile:204-208`). |
| `exec > >(tee -a /var/log/start.log) 2>&1` | `tee` es hijo de `start.sh`, no recibe la señal de `tini`; muere al cerrarse su `stdin` cuando `start.sh` sale. No afecta al `ExitCode`. La clave autogenerada queda en `/var/log/start.log` en claro (644): se sirve por `/logs/start` ya redactada; es el mismo nivel de exposición que `docker logs`, aceptado por diseño. |
| `tail -n 0 -F /var/log/comfyui.log &` | Mantiene la salida de ComfyUI en `docker logs`/`vastai logs`. Se mata en `shutdown`. |
| `set -u` dentro del `bash -c` del supervisor de status-server | El `bash -c` no hereda `set -u`; `${fail_times[@]:-}` es seguro de todos modos. |
| `write_supervisor_state` | Lanza `python3` en cada transición (≤ 3 por reinicio). Coste despreciable. |

---

## 2. Hallazgos nuevos

Formato: ID · severidad · ubicación · evidencia · acción · decisión (`Aplicar` = el ejecutor lo implementa; `Consultar` = requiere al dueño).

### Medios

**O-01 · Media · `tools/mac/comfy:349-396` · `comfy rent` crea la instancia sin confirmación (incumple D4 y §10.2)**
Evidencia: tras `offer_id` se imprime "Alquilando oferta…" y se ejecuta `vastai create instance` directamente. No muestra `$/h`, RAM del host, `$/GB·mes`, `inet_down` ni `cuda_max_good`; no pide confirmación. Además: (a) no pasa `--direct` aunque el filtro exige `direct_port_count>=1` y el README (línea 199) lo usa; (b) no reenvía `COMFYUI_EXTRA_ARGS` (el README recomienda `--disable-pinned-memory` justo para la RTX 5090 que `rent` busca), ni `HF_HUB_DISABLE_XET`, ni `ENABLE_WEB_TERMINAL`; (c) `--onstart-cmd "true"` no está justificado y su efecto en modo Entrypoint no está verificado; (d) `COMFY_PASS` y `HF_TOKEN` viajan en la cadena `--env` (visible en `ps` local durante la llamada; en Vast queda en la configuración de la instancia, como cualquier plantilla).
Acción: listar las 5 ofertas más baratas con las columnas de §10.2, pedir `[y/N]` mostrando precio total estimado (`dph_total` + disco), añadir `--direct`, reenviar las variables anteriores si están definidas, eliminar `--onstart-cmd` o documentar por qué es necesario, y permitir `--dry-run`. Decisión: **Aplicar**.

**O-02 · Media · `start.sh:364-427` y `/report.md` §1 · Los reinicios ordenados por el operador consumen el presupuesto de reinicios**
Evidencia: el informe recomienda "`pkill -f 'main.py'` (el supervisor lo relanza en 5 s)". Ese `pkill` produce `rc=143`, que el bucle trata igual que una caída: `restarts++`. Cinco reinicios manuales (p. ej. tras instalar nodos desde Manager) dejan ComfyUI en `exhausted` hasta reiniciar el contenedor. Tampoco se resetea el contador tras un periodo sano, así que un OOM al día 1 y otro al día 3 siguen sumando.
Acción: (1) no contar `rc=143` como reinicio (`restarts` sin incrementar, pero sí registrar la salida); (2) poner `restarts=0` cuando el proceso anterior vivió más de `COMFY_HEALTHY_UPTIME` (600 s por defecto); (3) reflejar ambas reglas en `/report.md` §1 y en README §3. Decisión: **Aplicar**.

**O-03 · Media · `status-server.py:_compute_heavy_sections` + `nginx.conf` (`proxy_read_timeout 30s`) · La primera llamada a `/report.md` puede superar el timeout del proxy**
Evidencia: con caché fría se ejecutan `pip3 freeze` (hasta 20 s), 20 llamadas `git` (hasta 3 s cada una), `rglob` sobre `models/` (hasta 60 GB de ficheros, barato en recuento pero no gratuito) y después todo lo de `/status/api`. En una instancia con disco lento es plausible superar 30 s → Nginx devuelve 504 y el botón "Copiar al portapapeles" falla en silencio (`catch (_) {}`).
Acción: calcular las secciones pesadas en un hilo de fondo que arranque con el servidor y se refresque cada 60 s (servir siempre el último valor con su `generated_at`); subir `proxy_read_timeout` a 120 s solo para `/report.md`; mostrar el error en el portal. Decisión: **Aplicar**.

**O-04 · Media · `status-server.py:tail_lines` + ausencia de rotación · Lectura completa de ficheros que crecen sin límite**
Evidencia: `collections.deque(fh, maxlen=n)` recorre el fichero entero. `/status/api` (cada 3 s como mínimo, con el panel abierto) lee `comfyui.log` (400 líneas), `nginx-access` (400), `download` (300 + 2000 en `get_phases_status`), etc.; `/logs/comfyui?n=2000` cada 10 s desde `/status`. `comfyui.log` y `/var/log/nginx/access.log` no rotan nunca (ahora además se duplican a disco). En una sesión de 12 h con ComfyUI verboso se leen cientos de MB cada pocos segundos.
Acción: `tail_lines` por bloques desde el final (`seek` hacia atrás hasta reunir `n` saltos de línea); limitar `access.log`/`comfyui.log` con truncado por tamaño en el supervisor (p. ej. al relanzar ComfyUI, si > 200 MB, conservar las últimas 20 MB) o `logrotate` sin cron (`copytruncate` invocado por el propio supervisor cada 30 min). Decisión: **Aplicar**.

**O-05 · Media · `start.sh:211-213` · `COMFY_PASS` exportada al entorno de ComfyUI, Fase 2 y shell `ttyd`**
Evidencia: `export COMFY_PASS` para que `status-server.py` pueda redactarla y `diag.sh` enmascararla. Como efecto colateral, `python3 main.py` (y cualquier custom node, con `security_level = weak`) y `download-models.sh --phase2` ven la contraseña en `os.environ`. Un nodo malicioso ya es root dentro del contenedor, así que el daño adicional es limitado, pero el principio de §1.4 ("ningún secreto donde no hace falta") no se cumple.
Acción: tras arrancar `status-server.py`, `ttyd` y `diag.sh` (paso 5), hacer `unset COMFY_PASS` antes de la Fase 1/2 y del supervisor. `status-server.py` ya tiene la variable en su propio entorno; `COMFY_PASS_AUTOGENERATED` puede seguir exportada. Añadir al smoke test: `docker exec smoke cat /proc/<pid_comfy>/environ | tr '\0' '\n' | grep -c '^COMFY_PASS=' → 0`. Decisión: **Aplicar**.

### Bajos

**O-06 · Baja · `Dockerfile:25-47` · `procps` no está declarado; `ps` y `free` llegan como dependencia transitiva**
Evidencia: `diag.sh` (`ps -o user= -C nginx`, `free -m`), `status-server.py` (`ps -eo pid,rss,args`, `ps -o user= -C nginx`), `start.sh` (`free -m`) y el smoke test paso 7 (`docker exec ps`) usan `procps`. No aparece en el `apt-get install`. [Conocimiento] En Ubuntu 24.04 el paquete `cmake` declara `Depends: procps`, por eso funciona hoy; si alguien retira `cmake` (añadido en L-01), el smoke test falla y el portal muestra Nginx como "none".
Acción: añadir `procps` explícito a la capa 1. Decisión: **Aplicar**.

**O-07 · Baja · workflow · El smoke test ya no comprueba `IMPORT FAILED` en tiempo de ejecución**
Evidencia: se eliminó el paso `--quick-test-for-ci` con `grep -q "IMPORT FAILED"`. El warm-up del `Dockerfile:204-208` sigue haciéndolo durante el build, así que no hay hueco de cobertura, pero el full-stack arranca ComfyUI de todos modos y la comprobación es gratuita.
Acción: tras el paso 1, `docker exec smoke grep -c "IMPORT FAILED" /var/log/comfyui.log` debe ser 0. Decisión: **Aplicar**.

**O-08 · Baja · `triage_patterns.json` · Falsos positivos previsibles**
Evidencia: (a) `nginx-502` (`" (502|503) "` sobre `access.log`) se dispara con los 503 que la propia pantalla de espera produce durante la Fase 1; como el log de acceso inicial es corto, esas líneas permanecen en las últimas 400 durante gran parte de la sesión y el triage muestra una alerta "media" permanente que dice "ComfyUI aún estaba arrancando" cuando ya está `running`. (b) `hf-503` usa `503` sin límites de palabra: coincide con tamaños (`du -h` → `503M`) o fragmentos de hash. (c) `errors_recent` con `ERROR` insensible a mayúsculas captura líneas benignas ("error" dentro de nombres).
Acción: (a) suprimir `nginx-502` cuando `comfyui.state == running` o restringir a las líneas con marca de tiempo de los últimos 10 min; (b) `\b503\b|HTTP 503|Service Unavailable`; (c) `\bERROR\b|\bCRITICAL\b|\bFATAL\b` con sensibilidad a mayúsculas. Decisión: **Aplicar**.

**O-09 · Baja · `portal/index.html`, `status-server.py:build_report_markdown` · Detalles de UX**
Evidencia: (a) `at` de `last_exit` se imprime como epoch (`a las 1760600000`); el criterio de §13 pide "hora". (b) Con `supervisor_state == exhausted` el portal dice "el supervisor lo está reiniciando". (c) La tarjeta Diagnóstico siempre está "Activo" porque `/diag` devuelve 200 aunque esté pendiente. (d) La tarjeta Terminal depende de un `HEAD /terminal/` cuyo soporte en `ttyd` no está verificado; `services.terminal.running` ya da la respuesta. (e) Los `catch (_) {}` del botón de copia ocultan fallos (ver O-03).
Acción: formatear `at` en ISO-8601 UTC; mensaje específico para `exhausted` ("límite de reinicios alcanzado; revisa /report.md"); decidir Diagnóstico por la presencia de `[DIAG] resumen:` en `/diag`; Terminal por `services.terminal.running`; mostrar mensaje de error visible en el portal. Decisión: **Aplicar**.

**O-10 · Baja · `status-server.py:get_proc_rss_and_uptime`, `get_container_uptime_s` · Uptime calculado con `mtime` de `/proc`**
Evidencia: las marcas de tiempo de `/proc/<pid>/status` y `/proc/1` reflejan la instanciación del inodo, no el arranque del proceso; el valor puede ser erróneo en ambos sentidos.
Acción: usar `started_at` de `/run/comfyui-supervisor.json` para ComfyUI y un fichero `/run/comfyui-started-at` escrito por `start.sh` en el paso 0 para el contenedor. Decisión: **Aplicar**.

**O-11 · Baja · `status-server.py:EXPECTED_MODELS` vs `download-models.sh` · Dos fuentes de verdad para el catálogo**
Evidencia: la lista está duplicada a mano. Hoy coincide (12 + 14 ficheros, perfiles verificados uno a uno), pero cualquier alta/baja de modelo exigirá tocar ambos sitios y el CI no lo detecta.
Acción: a corto plazo, un paso en `validate` que compare los nombres de fichero de ambos ficheros; a medio plazo, un manifiesto único (`config/models.tsv`) que lean los dos. Decisión: **Aplicar** (la primera parte).

**O-12 · Baja · Documentación · Afirmaciones sin respaldo o inconsistentes**
Evidencia: `decisiones.md` dice "`docker stop -t 15`" (el workflow usa `-t 30`); el informe del ejecutor cita "§15 criterios" (son §13); `decisiones.md` no incluye la justificación "por qué no `supervisord`" exigida en §12; `comfy all` no hace lo que dice §10.2 sin que se registre la desviación; `Dockerfile:196` exige `>= 15` patrones y el CI `>= 17`; `tools/mac/comfy:19` fija `ghcr.io/essaucr/comfyuipod:latest` mientras el README usa `ghcr.io/<tu-usuario>/<tu-repo>`.
Acción: corregir las cifras, añadir el párrafo de `supervisord`, documentar la desviación de `comfy all`, unificar el umbral en 17 y **confirmar con el dueño** el nombre real del paquete GHCR antes de dejarlo por defecto (o exigir `COMFY_IMAGE`). Decisión: **Aplicar** (cifras/docs) y **Consultar** (nombre de imagen).

**O-13 · Baja · `tools/mac/comfy:283-336` · Contraseña en la línea de comandos de `curl`**
Evidencia: `curl -u "${COMFY_USER}:${pass}"` expone la clave en `ps` de la Mac mientras dura la petición (y cada 3 s en `watch`).
Acción: pasar las credenciales por `stdin` con `curl -K -` (`user = "admin:…"`) o `--netrc-file` temporal con permisos 600. Decisión: **Aplicar**.

**O-14 · Baja · `tools/mac/comfy:31-41` · `open` en Linux**
Evidencia: en algunas distribuciones `open` es un alias de `openvt`; el script lo prefiere a `xdg-open`.
Acción: decidir por `uname -s` (`Darwin` → `open`, resto → `xdg-open`), y honrar `COMFY_BROWSER` como pedía §10.2. Decisión: **Aplicar**.

**O-15 · Baja · `status-server.py`, `portal/index.html` · Sin `Content-Security-Policy`**
Evidencia: ambas páginas usan `<script>` y `<style>` en línea y no envían CSP. Tras Basic Auth y sin contenido de terceros, el riesgo es bajo.
Acción (opcional): `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'` desde Nginx en `/home` y en la regex de estado. Decisión: **Consultar** (coste bajo, beneficio bajo).

---

## 3. Verificaciones que requieren ejecución (se añaden a la checklist V1–V12 de V5)

- [ ] **V13** Smoke full-stack verde en el primer workflow; anotar en `decisiones.md` cuánto tarda `/healthz` en responder en el runner (paso 1) y si el paso 3 devuelve 200 en `/terminal/` (confirma `--base-path` de `ttyd 1.7.4`).
- [ ] **V14** En la sesión Vast.ai: `curl -I -u … /terminal/` → confirmar si `ttyd` responde a `HEAD` (afecta a la tarjeta Terminal hasta que se aplique O-09d).
- [ ] **V15** Primera llamada a `/report.md` con disco de instancia real: medir tiempo (`curl -w '%{time_total}'`) para confirmar o descartar O-03.
- [ ] **V16** `vastai create instance … --env "-p 8188:8188 …"` sin `--ssh/--jupyter`: confirmar que arranca el `ENTRYPOINT` de la imagen y que `--onstart-cmd "true"` no interfiere (O-01c). Ejecutar `vastai show instances --raw` y confirmar `actual_status`, `public_ipaddr`, `ports`, `label`, `dph_total`, `gpu_name` (§10.3).
- [ ] **V17** Escenario §13: `COMFY_MAX_RESTARTS=1` + `COMFYUI_EXTRA_ARGS=--port-invalido` → el contenedor no se apaga; `/home` muestra `exhausted`; `/report.md` §0 explica la causa.
- [ ] **V18** Portal a 390 px de ancho sin scroll horizontal; tema claro y oscuro; pestaña Network sin dominios externos.
- [ ] **V19** `/status/api.phases` durante una Fase 2 real: `done/total` avanza y `current_file` coincide con `INICIO:`.
- [ ] **V20** Tras 2 h de sesión: tamaño de `/var/log/comfyui.log` y `/var/log/nginx/access.log`, y `top` del proceso `status-server.py` con `/status` abierto (dimensiona O-04).

---

## 4. Lo que no debe tocarse al corregir

- La lógica de Fase 1/Fase 2 en `download-models.sh` (reintentos, `STAGE_DIR`, `run_phase1` dentro de `--phase2`), salvo lo descrito en O-11.
- `PIP_CONSTRAINT`, `constraints.txt`, `nodes.lock`, la terna `cu128` y el warm-up del `Dockerfile`.
- La estructura de `location` de Nginx y la política de auth (solo se añade, si acaso, CSP y el `proxy_read_timeout` de `/report.md`).
- El esquema JSON de `/status/api` y el de `/run/comfyui-supervisor.json`: a partir de ahora son un contrato (los consumen el portal, `comfy watch`, el smoke test y la futura `comfyctl`). Cualquier cambio debe ser aditivo.
- Los nombres de las secciones de `/report.md` (`## 0. Triage automático` … `## 9. Modelos`): el smoke test y los agentes dependen de ellos.

---

## 5. Plan para el ejecutor

Un único commit `fix(observability): resolve Audit V6 findings (O-01..O-14)` sobre `f0fd82d`, en este orden de riesgo:

1. O-05 (`unset COMFY_PASS`) y O-06 (`procps`): triviales, afectan al arranque; probar `docker stop`.
2. O-02 (política de reinicios) y O-10 (`started_at`): tocan el supervisor y el JSON (campo nuevo `healthy_resets`, aditivo).
3. O-03 y O-04 (hilo de fondo, `tail` eficiente, truncado de logs): `status-server.py` y `start.sh`.
4. O-07, O-08, O-09, O-11 (CI, patrones, portal).
5. O-01, O-13, O-14 (`tools/mac/comfy`).
6. O-12 (documentación) y, si el dueño lo aprueba, O-15.

Actualizar `decisiones.md` (tabla "Ronda V6"), `walkthrough.md` y el README donde cambie el comportamiento (política de reinicios, `comfy rent` con confirmación). El auditor revisará el diff en V7.

---

## 6. Criterio de producción (sin cambios)

Sigue siendo: primer workflow verde (incluido el smoke full-stack), segundo push con `importing cache manifest` y sesión de aceptación en Vast.ai con las verificaciones V1–V20. Los hallazgos de esta ronda no bloquean el primer push, pero O-01 debe aplicarse **antes** de que alguien use `comfy rent` con dinero real, y O-05 antes de recomendar `ENABLE_WEB_TERMINAL=1` a terceros.

---

## 7. Nota sobre Rust (contexto para la decisión D6)

La implementación de `f0fd82d` demuestra que la funcionalidad cabe en Python stdlib + Bash y que el CI puede probarla sin GPU. Los hallazgos O-02, O-03 y O-04 son exactamente los que una implementación en un binario compilado resolvería "gratis" (hilos, lectura eficiente, tipos), pero ninguno justifica por sí solo reescribir. La propuesta [`IMPLEMENTACION_RUST.md`](../propuestas/IMPLEMENTACION_RUST.md) parte de esta implementación como especificación de comportamiento (mismo JSON, mismos ficheros, mismos nombres de sección) y añade lo que Python no da: un binario estático independiente del intérprete que diagnostica, y una política de arranque estricto (`STRICT_START`) para dejar de pagar instancias que nunca van a funcionar. Recomendación: decidir D6 después de V13–V20, no antes.
