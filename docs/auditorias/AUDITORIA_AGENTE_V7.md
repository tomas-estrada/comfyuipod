# Auditoría V7 — Correcciones V6 y migración a `comfyctl` (Rust)

**Alcance**: commits `9ac6d71` (fixes V6) → `309d4c3` (contratos) → `512e485` (comfyctl fase 1) → `98867a6` (comfyctl fase 2, HEAD), más el informe del ejecutor `docs/auditorias/IMPLEMENTACION_V6_Y_RUST.md`.
**Método**: lectura estática completa del árbol en HEAD y de los diffs por commit; contraste con `AUDITORIA_AGENTE_V6.md` (O-01..O-15), `docs/propuestas/IMPLEMENTACION_RUST.md` (§4–§11) y con cada afirmación del informe del ejecutor. El auditor **no ejecutó** `cargo`, `docker` ni CI; todo lo que exige ejecución se lista en §7 como V21+.
**Convenciones**: `[Repo]` verificado en código · `[Docs]` verificado en documentación oficial en rondas previas · `[Conocimiento]` conocimiento general no verificado en esta ronda · `[Verificar]` requiere ejecución o consulta.

---

## 1. Veredicto

**No apto para primer workflow ni para sesión Vast.ai en su estado actual.** La migración a Rust es amplia, está bien estructurada y respeta las reglas de código de §4.4, pero contiene **tres defectos bloqueantes** que el ejecutor no pudo detectar porque no ejecutó el contenedor:

1. El parser de fases de descarga no reconoce el formato real de `download-models.sh` (regresión funcional respecto a `status-server.py`; el test que lo cubre usa un fixture inventado).
2. El paso 11 del smoke test falla de forma determinista (`strict-check` sin `.htpasswd` → FATAL → rc 20 ≠ 0) y, con alta probabilidad, también el paso 1 (umbral de RAM 16384 MB en un runner de 16 GB).
3. Las tres interfaces que muestran el banner FATAL (portal, `/status`, `comfy watch`) leen campos que no existen en el JSON de `strict`.

Además, **el informe del ejecutor contiene un número elevado de afirmaciones no respaldadas por el código** (§4). Debe tratarse como no fiable hasta que se reescriba citando únicamente lo que existe.

Recuento: **3 altos, 7 medios, 6 bajos** (§5). Los cierres de V6 (O-01..O-14) son correctos salvo los matices indicados en §2.

---

## 2. Estado de los hallazgos V6 (O-01..O-15)

| ID | Estado | Evidencia `[Repo]` | Matiz |
|---|---|---|---|
| O-01 `comfy rent` | Cerrado | `tools/mac/comfy`: tabla top-5, confirmación `[y/N]`, `--yes`, `--dry-run`, `--direct --args`, reenvío de `COMFYUI_EXTRA_ARGS`/`HF_HUB_DISABLE_XET`/`ENABLE_WEB_TERMINAL`, filtros `cpu_ram>=32 cuda_max_good>=12.8` | El informe menciona `--max-dph` y `--gpu`; **no existen** en el código. `COMFYUI_EXTRA_ARGS` con espacios rompe la cadena `--env` (R-14). |
| O-02 rc=143 / reset | Cerrado | `supervisor.rs::on_child_exit`, `healthy_resets`, `COMFY_HEALTHY_UPTIME` | — |
| O-03 `/report.md` lento | Cerrado | `nginx.conf` `location = /report.md` 120 s; `http.rs` caché pesada con hilo de refresco 60 s | Caché pesada sin TTL ni vigilancia del hilo (R-16). |
| O-04 `tail_lines` / rotación | Cerrado | `logtail.rs` bloques 8 KiB desde `SeekFrom::End`; `supervisor.rs::truncate_logs_if_needed` | `phases.rs` sigue leyendo `download-models.log` completo (pequeño; aceptable). |
| O-05 `COMFY_PASS` en entorno | Cerrado | `start.sh` `unset COMFY_PASS` + `/run/comfyui-pass` 0600; `supervisor.rs` `env_remove("COMFY_PASS")`; `env -u COMFY_PASS ttyd`; smoke paso 6 | Ver R-11 (secreto no disponible para `comfyctl report` manual). |
| O-06 `procps` | Cerrado | `Dockerfile` | — |
| O-07 `IMPORT FAILED` en smoke | Cerrado | workflow, tras `/healthz` | — |
| O-08 falsos positivos | Cerrado | `triage.rs` (supresión `nginx-502` con ComfyUI running, `\bERROR\b` sensible a mayúsculas, dedupe); `triage_patterns.json` `hf-503` con `\b503\b` | — |
| O-09 UX portal | Cerrado con defecto | `config/portal/index.html` | El banner FATAL nuevo lee `c.message` inexistente (R-04). Resto no verificado visualmente (V21+). |
| O-10 uptime por `mtime` | Cerrado | `/run/comfyui-started-at` + `procfs.rs::get_container_uptime_s`; uptime de proceso por `started_at` del supervisor | — |
| O-11 catálogo duplicado | Cerrado | CI compara `hf_fetch` de `download-models.sh` con `models.rs::EXPECTED_MODELS` (26/26) | Catálogo ahora vive en Rust; la paridad la garantiza CI. |
| O-12 docs inconsistentes | **Reabierto** | `COMFY_IMAGE` obligatorio ✔; pero README y `decisiones.md` describen umbrales STRICT y esquema que no coinciden con el código (R-05) | — |
| O-13 `curl -u` en `ps` | Cerrado | `curl_auth` con `-K -` por stdin | — |
| O-14 `open` en Linux | Cerrado | `open_browser` por `uname -s` y `COMFY_BROWSER` | — |
| O-15 CSP | Consultar | Sin cambios | Decisión del dueño pendiente. |

---

## 3. Trazabilidad con `IMPLEMENTACION_RUST.md`

| Sección | Estado | Observación |
|---|---|---|
| §4.2 Contratos congelados | Parcial | Esquemas creados y validados en CI (`additionalProperties: true`, `healthy_resets`, `supervisor_state` con `fatal`). **No incluyen el bloque `strict`** aunque el informe y `decisiones.md` L127 dicen que sí. `$id` apunta a un dominio de Comfy-Org que no es del proyecto. |
| §4.3 Dependencias | Parcial | 26 paquetes en `Cargo.lock` (≤ 40 ✔). `chrono` en lugar de `time` declarado con motivo (RUSTSEC-2026-0009, MSRV) `[Verificar]`. **Binario glibc (bookworm), no musl estático; desviación no declarada** (R-09). |
| §4.4 Reglas de código | Cumple | `Cargo.toml` lints, `clippy.toml`, `deny.toml`, `rust-toolchain.toml` 1.86.0, `--locked`, `unsafe` solo en `sys.rs`. `sys.rs` usa `libc::signal`, no `sigaction` (válido; el informe miente). |
| §5 Subcomandos | Cumple | `version`, `strict-check`, `supervise [--external-status-server]`, `serve [--bind]`, `report`, `diag`. `--external-status-server` ya no lo usa nadie (R-12). |
| §6 STRICT_START | **Parcial** | 9 checks S1..S9. Faltan `cuda.torch`, `compute_cap`, `nvenc`, `net.hf`, `ports.loopback`; sin banda WARN; umbrales distintos a los fijados; código de salida 20 en lugar de 3 (R-06). |
| §7 `diag` | Parcial | Formato `[DIAG][OK|WARN|FAIL]` y resumen conservados; 10 comprobaciones; sondas en `/etc/comfyui/probes/`. **Sin `timeout`** (R-07). |
| §8 Dockerfile / CI | Parcial | Stage `builder` por digest, `COPY --from`, job `rust` con acciones por SHA, smoke 11–14. Pasos 11 y 1 con fallos previsibles (R-02, R-03). |
| §9 `start.sh` | Cumple con residuos | `/run/comfyui-pass`, `unset`, `exec comfyctl supervise`. Cabecera y logs huérfanos (R-12). |
| §10 Migración por fases | Cumple | Commits separados por fase; `status-server.py` y `diag.sh` eliminados en fase 2. |
| §11 Aceptación | Pendiente | Requiere ejecución (V21+). |

---

## 4. Fiabilidad del informe del ejecutor

Afirmaciones de `IMPLEMENTACION_V6_Y_RUST.md` sin respaldo en HEAD `[Repo]`:

| Afirmación | Realidad |
|---|---|
| `sys.rs`: `sigaction`, `SIGCHLD`, `setpgid`, `kill(-pgid)`, `waitpid` | Solo `libc::signal`, `kill(pid, sig)`, `kill(pid, 0)`, `statvfs`. Ninguna de las demás existe. |
| `strict.rs`: "8 comprobaciones", "sonda `torch.cuda`", "RAM ≥ 24 GiB", "disco 100/55/20" | 9 comprobaciones, sin sonda torch, `STRICT_MIN_RAM_MB=16384`, disco 80/45/15 (5 con SKIP). |
| Esquema `status-api` con "bloque opcional `strict`" | Ausente en `docs/contratos/status-api.schema.json`. |
| `http.rs` sirve `/download-status` | No; lo sirve nginx por `alias`. |
| `comfy rent` con `--max-dph` y `--gpu` | Solo `--dry-run`, `--yes`, perfil. |
| O-02 "protección de `wait` bajo `set -e`" | Ya existía antes de `9ac6d71`. |
| O-06 "`phase2.total` por perfil" y O-07 "`ldconfig`" en `9ac6d71` | No están en ese diff (el primero llegó en `98867a6` vía `models.rs`). |
| `## 9. Colas de logs` | `report.rs` escribe `## 9. Modelos` (correcto en código; el informe está mal). |
| Referencias `archivo:Lx-Ly` | Varias no coinciden con los ficheros (p. ej. `strict.rs` tiene 579 líneas, no 423). |

Afirmaciones coherentes con el código: 13 tests (`strict` 5, `supervisor` 2, resto 1 cada uno), 26 paquetes, lints, digest del builder y SHAs de acciones presentes en Dockerfile/workflow (su validez en red no ha sido comprobada por el auditor).

**Acción**: el ejecutor debe reescribir el informe citando solo lo que existe, y el auditor volverá a cotejarlo en V8. Hasta entonces, cualquier afirmación del informe sin verificación directa cuenta como no verificada.

---

## 5. Hallazgos nuevos

Formato: ID · severidad · ubicación · evidencia · acción · decisión.

### Altos

**R-01 · ALTA · `comfyctl/src/collect/phases.rs` + `tests/fixtures/download_models.log`**
El parser busca `Iniciando FASE 1`, `FASE 1 completada en Ns: a/b exitosos, c fallidos`, `Omitiendo Fase 2`, `\]\s*(OK|SKIP|ERROR):\s*(\S+)`. `scripts/download-models.sh` escribe `INICIANDO FASE 1 (2.79 GiB): …`, `PLAN FASE 1: 12 archivos`, `FASE 1 COMPLETADA.`, `RESUMEN FASE 1: …`, `PLAN FASE 2: N archivos (perfil)`, `omitiendo Fase 2` (minúscula), `SKIP (ya existe): nombre`, `OK: /ruta/completa (tamaño)`, `ERROR: Falló la descarga de nombre …`. `String::contains` es sensible a mayúsculas: ninguna fase se detectará, `done/failed` nunca avanzan, `phase_banner` dirá "ComfyUI listo" durante toda la Fase 2 y `## 5` del informe quedará en `pending`. `status-server.py` (eliminado) sí reconocía `INICIANDO FASE`, `COMPLETADA`, `PLAN FASE 2`, `OK:`/`SKIP (ya existe):`, `ERROR: Falló la descarga de`. El fixture del test reproduce el formato inventado, por lo que `cargo test` verde no prueba nada.
Acción: reescribir el parser contra el formato real (`PLAN FASE n: (\d+) archivos` para `total`; `INICIANDO FASE n`; `FASE n COMPLETADA`; `OK:`/`SKIP (ya existe):`/`ERROR: Falló la descarga de` para contadores; `INICIO:` para `current_file`, comparando por nombre de fichero, no por ruta). Regenerar el fixture a partir de una ejecución real de `download-models.sh` (p. ej. con `HF_CLI` falso en CI) y añadir en CI un test que ejecute el script en modo seco y compare con el parser. Mantener `elapsed_s` si se puede derivar de los timestamps del log.
Decisión: obligatoria antes de cualquier sesión Vast.ai (V13–V20 dependen de ver el progreso de Fase 2).

**R-02 · ALTA · `.github/workflows/docker-publish.yml` paso 11 (y 12)**
Paso 11 ejecuta `comfyctl strict-check` con `docker run --entrypoint sh` (sin `start.sh`). S7 `nginx_and_htpasswd` exige que `/etc/nginx/.htpasswd` exista y tenga ≥ 1 byte; el Dockerfile no lo crea. Con `STRICT_START=1` → `fatal` → rc 20; el paso exige rc 0 → **el workflow fallará de forma determinista**. Además, S5 exige 80 GB libres (perfil `all`, sin `SKIP_MODEL_DOWNLOAD`) y S4 16384 MB de RAM, ambos dudosos en un runner de GitHub. Paso 12 pasa por casualidad (espera 20 en la primera ejecución y `warn` en la segunda).
Acción: (a) en pasos 11 y 12 crear un `.htpasswd` mínimo o fijar `COMFY_HTPASSWD_PATH` a un fichero de prueba, y pasar `SKIP_MODEL_DOWNLOAD=1` (o `STRICT_MIN_DISK_GB=1`) y `STRICT_MIN_RAM_MB` acorde; (b) mejor aún, que S7 solo compruebe `.htpasswd` cuando `comfyctl` corre dentro de `supervise` (el check tiene sentido tras `start.sh`), y que `strict-check` aislado lo marque `warn`. Añadir un test que ejecute `evaluate_strict_checks` con un `SystemSnapshot` "runner CI" y compruebe `result != fatal`.
Decisión: obligatoria; bloquea el primer workflow.

**R-03 · ALTA `[Verificar]` · `comfyctl/src/config.rs` L175 + smoke paso 1**
`STRICT_MIN_RAM_MB=16384` y S4 compara `MemTotal/1024 >= 16384` sin margen. En un runner `ubuntu-latest` de "16 GB", `/proc/meminfo` suele reportar menos de 16384 MiB porque el kernel reserva memoria `[Conocimiento]`. El contenedor del smoke no fija `STRICT_START=0`, así que S4 sería `fatal`, `comfyctl supervise` entraría en gracia de 600 s sin arrancar ComfyUI, `/healthz` (proxy a `/system_stats`) devolvería 502 y el paso 1 agotaría sus 600 s. Mismo riesgo en hosts Vast.ai anunciados con `cpu_ram=16`. Además, en contenedor `/proc/meminfo` muestra la RAM del host, no el límite del cgroup, así que el check mide la máquina, no la instancia `[Conocimiento]`.
Acción: aplicar margen (por ejemplo umbral efectivo = `STRICT_MIN_RAM_MB × 0.95`, o default 15360) y documentarlo; en el smoke fijar explícitamente `STRICT_MIN_RAM_MB` y `STRICT_MIN_DISK_GB` para que la prueba no dependa del runner; si se quiere medir el límite real, leer `/sys/fs/cgroup/memory.max` cuando exista. El `fallback` de 32768 MB cuando no se puede leer `/proc/meminfo` debe ser `warn`, no un pase silencioso.
Decisión: obligatoria antes del primer workflow.

### Medios

**R-04 · MEDIA · `config/portal/index.html` L527, `comfyctl/src/status_html.html` L194, `tools/mac/comfy` (`cmd_watch`)**
Las tres leen `c.message`, `c.observed`, `c.expected`. El JSON real (`strict.rs::StrictCheckItem`) tiene `id`, `name`, `status`, `detail`, `remediation`. El banner FATAL mostrará `undefined`/`None` justo cuando más importa. Causa raíz: el esquema no describe `strict` (R-13), así que CI no pudo detectarlo.
Acción: usar `detail` y `remediation` en las tres vistas; añadir `strict` al esquema; en el smoke paso 12 validar el JSON contra ese bloque.
Decisión: obligatoria.

**R-05 · MEDIA · `README.md` L22 y L101, `docs/decisiones.md` L127, informe del ejecutor**
La documentación describe VRAM ≥ 20 GB, RAM ≥ 24 GB, disco 100/55/20, bloque `strict` en el esquema y flags de `comfy rent` que no existen. El código tiene 16000 MB, 16384 MB, 80/45/15. Es la misma clase de problema que O-12, ahora en el componente más crítico.
Acción: una sola fuente de verdad para umbrales (tabla en README generada o copiada desde `config.rs` con referencia a línea) y corregir las tres piezas tras decidir R-06.
Decisión: obligatoria junto con R-06.

**R-06 · MEDIA · `comfyctl/src/strict.rs`, `config.rs` frente a `IMPLEMENTACION_RUST.md` §6.2**
El dueño aprobó §6.2 "tal cual" (turno 16). Desviaciones no declaradas: (1) falta la sonda `cuda.torch` (`torch.cuda.is_available()` con timeout), la única que detecta "driver presente pero CUDA inutilizable"; (2) faltan `compute_cap`, `nvenc` (WARN), `net.hf` (WARN) y `ports.loopback`; (3) no hay banda WARN (RAM 16–32 GB debía ser WARN; VRAM entre 20000 y el mínimo debía avisar); (4) umbrales: VRAM 16000 frente a 20000, RAM 16384 frente a 32768 OK / 16384 WARN, disco 80/45/15 frente a 75/45/10; (5) código de salida 20 frente a 3.
Acción: o restaurar §6.2, o documentar cada desviación con motivo en `decisiones.md` y pedir aprobación. Recomendación del auditor: añadir S10 `cuda_torch` (fatal si `strict_require_gpu`, con `timeout` de 20 s y `COMFY_PROBE_PYTHON` sobreescribible para tests), añadir banda WARN para RAM, mantener los umbrales de VRAM/disco de §6.2, y **conservar el código 20** (ya está en CI, `comfy` y portal); el auditor actualizará §6 de la propuesta a 20 cuando el ejecutor confirme.
Decisión: Consultar al dueño solo si el ejecutor prefiere mantener los umbrales actuales; en caso contrario aplicar la recomendación.

**R-07 · MEDIA · `comfyctl/src/diag.rs`, `supervisor.rs` L285**
`diag.sh` envolvía cada sonda con `timeout 10/15`. `diag.rs` usa `Command::output()`/`status()` sin límite: una sonda colgada (import de `torch` con driver roto, `ffmpeg h264_nvenc` sin NVENC) bloquea el hilo para siempre, `/diag` queda vacío y `## 0`/`## 2` del informe dicen "aún en curso". Además, `diag` corre en un hilo concurrente con Fase 1 y con el arranque de ComfyUI (matmul y sesión ORT compitiendo por la GPU), no en el orden status-server → diag → Fase 1 de §12 de la propuesta de observabilidad.
Acción: implementar `run_with_timeout(cmd, secs)` en `sys.rs` (spawn + `try_wait` + `kill`) y usarlo en todas las sondas; opcionalmente anteponer `diag` a Fase 1 con un tope global de 60 s. Añadir test con una sonda que duerme.
Decisión: obligatoria.

**R-08 · MEDIA · `comfyctl/tests/*` (cobertura de contratos reales)**
Los 13 tests pasan pero varios validan formatos inventados (R-01) o no cubren los defectos de esta ronda (R-02, R-04). Un test que parsee el log real, otro que compruebe que los campos usados por el portal existen en `StrictCheckItem`, y uno que evalúe `strict` con un snapshot "CI" habrían bastado.
Acción: añadir esos tres tests; en CI, extraer de `index.html`/`status_html.html`/`comfy` los nombres de campos `strict.checks[].x` usados y comprobar que están en el esquema.
Decisión: obligatoria con R-01/R-04.

**R-09 · MEDIA · `Dockerfile` stage `builder`**
(1) El binario se compila para `x86_64-unknown-linux-gnu` contra glibc 2.36 de bookworm y se ejecuta sobre glibc 2.39 de Ubuntu 24.04; funciona por compatibilidad hacia delante `[Conocimiento]`, pero §4.3 pedía musl estático y la desviación no está declarada ni justificada (el paso 13 del smoke solo mira `librustc*`). (2) Se copia `comfyctl/rust-toolchain.toml` con `components = ["rustfmt", "clippy"]` al builder; la imagen `rust:slim` instala el perfil `minimal`, así que rustup intentará descargar esos componentes durante `docker build` `[Verificar]`: dependencia de red adicional y punto de fallo que la caché de BuildKit no cubre.
Acción: no copiar `rust-toolchain.toml` al builder (o fijar `RUSTUP_TOOLCHAIN=1.86.0` y quitar `components` del fichero); decidir musl (añadir target `x86_64-unknown-linux-musl` y `--target`) o documentar glibc como decisión con el par de versiones; en el paso 13 comprobar `ldd` esperado explícitamente.
Decisión: obligatoria la parte (2); la (1) a criterio del dueño.

**R-10 · MEDIA · `tools/mac/comfy` `cmd_open` + `nginx.conf` `/healthz`**
`/healthz` hace proxy a `/system_stats` de ComfyUI. Durante la gracia FATAL (600 s) devuelve 502, así que `comfy open` espera 10 min y luego muere con "Timeout esperando IP/puerto" sin mencionar el FATAL; solo detecta `exited/stopped` cuando el contenedor ya salió. El `HEALTHCHECK` de Docker no se ve afectado (start-period 900 s > 600 s).
Acción: en el bucle de `cmd_open`, cuando `/healthz` falle, consultar `/home` (200 implica nginx vivo) y, si hay credenciales resolubles, `/status/api` → `strict.result`; abrir el portal igualmente y avisar.
Decisión: recomendada.

### Bajos

**R-11 · BAJA · `comfyctl/src/config.rs::from_env` (`read_and_unlink_secret`)**
Cualquier subcomando borra `/run/comfyui-pass`. Tras `supervise`, un `comfyctl report`/`diag` lanzado a mano desde ttyd no conoce la clave autogenerada y no la redacta. El servidor interno sí la conoce y redacta `/logs/*` (`http.rs` L239), incluido `/logs/start`, cuyo banner imprime la clave autogenerada; por eso el impacto se limita a ejecuciones manuales del binario.
Acción: unlink solo en `supervise`; para los demás subcomandos, leer sin borrar si existe (o aceptar la limitación y documentarla en `tools/mac/README.md`).

**R-12 · BAJA · `scripts/start.sh` L5–L11, L43/L50, `main.rs` `--external-status-server`**
Cabecera de comentarios obsoleta (menciona `status-server.py`, `diag.sh`, 9 pasos), sigue creando `/var/log/status-server.log`, y el flag `--external-status-server` es código muerto de la fase 1.
Acción: limpiar cabecera y log huérfano; eliminar el flag o documentarlo como modo de desarrollo.

**R-13 · BAJA · `docs/contratos/status-api.schema.json`**
Sin bloque `strict` (debe añadirse como propiedad opcional con `result`, `strict_start`, `checked_at`, `checks[]{id,name,status,detail,remediation}`); `$id` con dominio ajeno. `phases.phase1.failed` pasó de lista (Python) a entero (Rust): comprobar que el esquema lo refleja y que el portal no itera sobre él `[Verificar]`.

**R-14 · BAJA · `tools/mac/comfy` `cmd_rent`**
`--env "-p 8188:8188 -e COMFYUI_EXTRA_ARGS=${COMFYUI_EXTRA_ARGS}"`: un valor con espacios (`--cpu --fast`) rompe la cadena que Vast.ai divide por espacios `[Verificar]`. Documentar la limitación o comillar según la sintaxis que acepte `vastai`.

**R-15 · BAJA · `comfyctl/src/report.rs`**
Texto fijo "Los 10 custom nodes coinciden" (debe contar las entradas de `nodes.lock`); truncado a 395 000 caracteres (no bytes) por debajo del límite declarado de 400 KB (inofensivo).

**R-16 · BAJA · `comfyctl/src/http.rs`, `procfs.rs`**
Caché pesada sin TTL: si el hilo de refresco muere, `/report.md` sirve datos viejos indefinidamente sin indicarlo. `services.status_server.running` es siempre `true` (trivial, pero el campo pierde sentido). Añadir `heavy_refreshed_at` al informe y marcar "datos de hace N s" si N > 300.

---

## 6. Lo que no tocar

- Cabeceras `## 0.`–`## 10.` de `/report.md` y el formato `[DIAG][…]`/`[DIAG] resumen:` son contrato (CI paso 14).
- Esquemas JSON: solo cambios aditivos (añadir `strict` es aditivo).
- Reglas de `Cargo.toml` (`lints`), `deny.toml`, `rust-toolchain.toml` 1.86.0 y `--locked`.
- Política O-02 del supervisor (rc=143 no consume presupuesto; reset tras uptime sano).
- Código de salida 20 para FATAL (queda fijado; la propuesta se alineará).

---

## 7. Verificaciones que requieren ejecución (V21+)

| ID | Qué | Criterio |
|---|---|---|
| V21 | Primer workflow completo | Jobs `validate`, `rust`, `build-and-push` verdes; smoke 1–14 sin saltarse pasos. |
| V22 | `docker build` del stage `builder` | Sin descargas de componentes rustup (R-09); tiempo de compilación anotado. |
| V23 | Sesión Vast.ai normal (perfil `all`) | `/status/api.phases` avanza durante Fase 2 (R-01); `phase_banner` muestra `N/14`. |
| V24 | FATAL simulado (`-e STRICT_MIN_VRAM_MB=99999`) | Banner rojo con `detail` legible en `/home`, `/status`, `comfy watch` (R-04); contenedor sale con 20 tras 600 s; `comfy open` informa en lugar de esperar 10 min (R-10). |
| V25 | `comfyctl diag` en frío | 10 líneas + resumen en < 60 s; con una sonda bloqueada artificialmente, termina por timeout (R-07). |
| V26 | Redacción | `/report.md`, `/diag` y `/logs/start` sin la clave autogenerada ni `HF_TOKEN` (R-11). |
| V27 | `comfy rent --dry-run` y alquiler real | Tabla, confirmación, `--direct --args` respeta `ENTRYPOINT`; variables reenviadas visibles en `env_masked`. |

V13–V20 de V6 siguen vigentes y se ejecutan tras V21.

---

## 8. Plan de corrección (orden de riesgo)

1. **R-02, R-03**: desbloquear CI (fixtures de `.htpasswd`, umbrales explícitos en smoke, margen en S4).
2. **R-01, R-08**: parser de fases contra el log real + fixture real + test.
3. **R-04, R-13**: campos `detail`/`remediation` en las tres vistas; `strict` en el esquema; validación en smoke paso 12.
4. **R-06, R-05**: decidir umbrales y sondas; alinear README, `decisiones.md`, informe.
5. **R-07**: timeouts en `diag`.
6. **R-09 (2)**: toolchain en builder; decidir musl.
7. **R-10, R-11, R-12, R-14, R-15, R-16**.
8. Reescribir `IMPLEMENTACION_V6_Y_RUST.md` (§4) o sustituirlo por `IMPLEMENTACION_V7.md` con referencias reales.

Commit sugerido: `fix(comfyctl): resolve Audit V7 findings (R-01..R-16)`. Sin push hasta V8.

---

## 9. Criterio de producción

Apto para primera sesión Vast.ai cuando: R-01..R-09 cerrados y verificados en V8 por lectura; V21 verde; V23 y V24 ejecutados por el dueño con capturas de `/status/api` y `docker logs`. Hasta entonces, la versión `f0fd82d` + `9ac6d71` (Python) era funcionalmente más completa que HEAD en lo relativo a progreso de descargas, y conviene tenerlo presente si se necesita una sesión urgente: `STRICT_START=0` y aceptar `phases` ciego, o revertir temporalmente a `9ac6d71`.
