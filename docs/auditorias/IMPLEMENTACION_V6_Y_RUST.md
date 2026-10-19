# Informe de Implementación: Auditoría V6 (`O-01`..`O-14`) y Migración a Rust (`comfyctl` — D6 Opción B Híbrida)

- **Fecha**: 2026-10-19
- **Rama**: `main` (sobre base `f0fd82d`, sin `git push`)
- **Alcance**:
  1. **Entrega 1**: Resolución íntegra de los hallazgos `O-01` a `O-14` de [`docs/auditorias/AUDITORIA_AGENTE_V6.md`](AUDITORIA_AGENTE_V6.md) (`O-15` excluido por instrucción; `O-12` exige `COMFY_IMAGE` explícita).
  2. **Entrega 2 (Orden obligatorio en 3 commits)**:
     - Congelación de contratos JSON Schema ([`docs/contratos/status-api.schema.json`](../contratos/status-api.schema.json) y [`docs/contratos/supervisor-state.schema.json`](../contratos/supervisor-state.schema.json)).
     - **Fase 1 (`§10.1`)**: Crate [`comfyctl/`](../../comfyctl/) con `strict-check`, `supervise --external-status-server`, `sys`, `config`, paso de `COMFY_PASS` por `/run/comfyui-pass` (`0600`), stage `builder` en `Dockerfile` fijado por digest, job `rust` en CI y pasos 11–13 del smoke test (`§8.2`).
     - **Fase 2 (`§10.2`)**: Subcomandos `serve`, `report`, `diag`, recolectores `collect/*`, `triage`, `logtail`, `redact`, sondas Python en `config/probes/`, fixtures/snapshots, eliminación de `scripts/status-server.py` y `scripts/diag.sh`, y paso 14 de paridad en el smoke test (`§8.2`).

---

## 1. Desglose por Commit (`archivo:línea` → Cambio)

### Commit 1 (`9ac6d71`) — `fix(observability): resolve Audit V6 findings (O-01..O-14)`

| Hallazgo / Sección | Archivo y líneas (en commit `9ac6d71`) | Cambio realizado |
|---|---|---|
| **O-01** (`comfy rent` seguro + `--direct --args`) | [`tools/mac/comfy`](../../tools/mac/comfy) `L229-L361` | Se añadieron flags `--yes`/`-y`, `--dry-run`, `--max-dph <USD>` y `--gpu RTX_5090\|RTX_4090`; tabla previa de las 5 mejores ofertas (`ID`, `GPU`, `VRAM`, `$/h`, `Down Mbps`, `CUDA`, `Rel.`); confirmación interactiva `¿Alquilar oferta <ID> por $<X>/h? [y/N]` en TTY; invocación de `vastai create instance ... --direct --args` para conservar el `ENTRYPOINT` de la imagen. |
| **O-02** (Supervisor `wait` bajo `set -e` + reset tras uptime sano) | [`scripts/start.sh`](../../scripts/start.sh) `L360-L433` | Protección de `wait "$COMFY_PID" \|\| rc=$?` para que códigos de salida `!= 0` de `main.py` no aborten `start.sh` bajo `set -euo pipefail`; reseteo de `restarts=0` e incremento de `healthy_resets` cuando `run_duration >= COMFY_HEALTHY_UPTIME` (`600s` por defecto); registro de `healthy_resets` en `/run/comfyui-supervisor.json`. |
| **O-03** (`pip3 freeze` y `git` fuera del hilo HTTP + timeout Nginx) | [`scripts/status-server.py` (en `9ac6d71`) `L747-L848`, `L1498-L1525`<br>[`config/nginx.conf`](../../config/nginx.conf) `L134-L144` | Hilo demonio `_heavy_collector_loop` que actualiza `_HEAVY_CACHE` cada `60s` (`pip3 freeze`, `git rev-parse HEAD`, `git status --porcelain`, inventario de modelos); `/report.md` lee de `_HEAVY_CACHE` sin bloquear peticiones HTTP; bloque `location = /report.md` en Nginx con `proxy_read_timeout 120s;`. |
| **O-04** (`tail_lines` O(N) por `SEEK_END` + rotación de logs) | [`scripts/status-server.py` (en `9ac6d71`) `L109-L155`<br>[`scripts/start.sh` (en `9ac6d71`) `L149-L162` | Reescritura de `tail_lines()` leyendo en bloques de `8192` bytes hacia atrás desde `os.SEEK_END` (tope `4 MiB`); función `truncate_log_if_needed()` en `start.sh` que recorta a los últimos `50 MB` cualquier log que supere `LOG_MAX_MB` (`200 MB`). |
| **O-05** (`unset COMFY_PASS` antes de subprocesos) | [`scripts/start.sh` (en `9ac6d71`) `L192-L246`<br>[`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) `L451-L461` | Arranque de `ttyd` con `env -u COMFY_PASS`, exportación de `COMFY_PASS_SET=1` y ejecución de `unset COMFY_PASS` antes de `diag.sh`, `download-models.sh` y `main.py`; check en el smoke test verificando `0` ocurrencias de `^COMFY_PASS=` en `/proc/<comfy_pid>/environ`. |
| **O-06** (`procps` explícito + `phase2.total` por perfil) | [`Dockerfile`](../../Dockerfile) `L45`<br>[`scripts/status-server.py` (en `9ac6d71`) `L452-L505` | Inclusión explícita de `procps` en `apt-get install`; cálculo de `p2_total` a partir de `EXPECTED_MODELS["phase2"]` según `MODEL_PROFILE` antes de que la Fase 2 escriba su cabecera en el log. |
| **O-07** (`ldconfig` para `onnxruntime-gpu` + check `IMPORT FAILED`) | [`Dockerfile`](../../Dockerfile) `L140-L142`<br>[`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) `L354-L361` | Registro de directorios `nvidia/*/lib` y `torch/lib` en `/etc/ld.so.conf.d/pytorch-cuda.conf` seguido de `ldconfig` en build-time; check en smoke test exigiendo `0` ocurrencias de `IMPORT FAILED` en `/var/log/comfyui.log`. |
| **O-08** (Falsos positivos en triaje y errores recientes) | [`scripts/triage_patterns.json`](../../scripts/triage_patterns.json) `L75-L80`<br>[`scripts/status-server.py` (en `9ac6d71`) `L588-L645` | **O-08a**: supresión de `nginx-502` cuando `comfyui.state == "running"`; **O-08b**: acotación de `hf-503` a `\b503\b\|HTTP 503\|Service Unavailable\|RequestException\|CAS service error`; **O-08c**: `err_rx` case-sensitive para tokens `ERROR`/`CRITICAL`/`FATAL`. |
| **O-09** (5 desajustes del portal `/home`) | [`config/portal/index.html`](../../config/portal/index.html) `L268-L410`<br>[`scripts/status-server.py` (en `9ac6d71`) `L679-L699`, `L925-L934` | **O-09a**: fecha ISO-8601 UTC legible en `last_exit.at`; **O-09b**: estado `exhausted` diferenciado en rojo en el portal; **O-09c**: parser de `/diag` reconoce `[DIAG] resumen:`; **O-09d**: tarjeta de terminal comprueba `api.services.terminal.running`; **O-09e**: botón de copiar `/report.md` muestra aviso rojo si falla la descarga o el portapapeles. |
| **O-10** (`uptime_s` del contenedor vs host) | [`scripts/start.sh`](../../scripts/start.sh) `L25-L28`<br>[`scripts/status-server.py` (en `9ac6d71`) `L301-L320` | `start.sh` persiste `date +%s > /run/comfyui-started-at`; `get_container_uptime_s()` calcula `time.time() - started_at` con fallback a `_START_MONOTONIC` (sin leer `/proc/uptime` del host). |
| **O-11** (Paridad `download-models.sh` ↔ `EXPECTED_MODELS`) | [`scripts/status-server.py` (en `9ac6d71`) `L43-L74`<br>[`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) `L67-L85` | Sincronización de los 26 modelos en `EXPECTED_MODELS` y aserción automática en el job `validate` de GitHub Actions. |
| **O-12** (`COMFY_IMAGE` obligatorio + fail-fast `image.json`) | [`tools/mac/comfy`](../../tools/mac/comfy) `L252-L260`<br>[`Dockerfile`](../../Dockerfile) `L212` | `comfy rent` exige `COMFY_IMAGE` explícito (sin repositorio GHCR inventado por defecto); generación de `/etc/comfyui/image.json` y validación de `triage_patterns.json` sin `\|\| true` en `Dockerfile`. |
| **O-13** (Credenciales fuera de `ps aux` en Mac CLI) | [`tools/mac/comfy`](../../tools/mac/comfy) `L93-L103`, `L195-L226` | Helper `curl_auth()` que pasa `user = "user:pass"` a `curl -K -` por `stdin` en `cmd_report` y `cmd_watch`. |
| **O-14** (`open_browser` y espera en `comfy open`) | [`tools/mac/comfy`](../../tools/mac/comfy) `L82-L91`, `L105-L148` | `open_browser()` usa `/usr/bin/open` en `Darwin` (`uname -s`), `xdg-open` en Linux y respeta `COMFY_BROWSER`; `cmd_open` espera hasta 10 minutos a que `/healthz` responda `200`. |
| **Doc V6** (`decisiones.md`, `walkthrough.md`, `README.md`) | [`docs/decisiones.md`](../decisiones.md) `L80-L122`<br>[`docs/walkthrough.md`](../walkthrough.md) `L1-L43`<br>[`README.md`](../../README.md) `L95-L164` | Tabla de decisiones Ronda V6, justificación de "por qué no `supervisord`", actualización del walkthrough y de la guía del CLI Mac. |

---

### Commit 2 (`309d4c3`) — `chore(contracts): freeze status-api and supervisor-state schemas`

| Sección | Archivo y líneas | Cambio realizado |
|---|---|---|
| **§3 / §10** (Congelación de esquema `/status/api`) | [`docs/contratos/status-api.schema.json`](../contratos/status-api.schema.json) `L1-L261` | Esquema JSON Schema Draft 2020-12 congelado para `GET /status/api` con todos los campos obligatorios (`generated_at`, `image`, `instance`, `gpu`, `memory`, `disk`, `comfyui`, `phases`, `phase_banner`, `services`, `errors_recent`, `triage`, `env_masked`) y bloque opcional aditivo `strict` (`§6.4`). |
| **§3 / §10** (Congelación de esquema `/run/comfyui-supervisor.json`) | [`docs/contratos/supervisor-state.schema.json`](../contratos/supervisor-state.schema.json) `L1-L59` | Esquema JSON Schema Draft 2020-12 congelado para `/run/comfyui-supervisor.json` (`state`, `attempt`, `restarts`, `healthy_resets`, `max`, `pid`, `started_at`, `last_exit`). |
| **§8.2 / §10** (Validación en CI y smoke test) | [`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) `L86-L137`, `L385-L433` | Validación estructural contra ambos esquemas congelados en el job `validate` y en el paso 4 del smoke test E2E. |

---

### Commit 3 (`512e485`) — `feat(comfyctl): phase 1 — strict-check and supervisor in Rust`

| Sección | Archivo y líneas | Cambio realizado |
|---|---|---|
| **§4.1 / §4.4** (Toolchain y políticas de código) | [`comfyctl/rust-toolchain.toml`](../../comfyctl/rust-toolchain.toml) `L1-L4`<br>[`comfyctl/Cargo.toml`](../../comfyctl/Cargo.toml) `L1-L40`<br>[`comfyctl/deny.toml`](../../comfyctl/deny.toml) `L1-L39` | Fijación de `channel = "1.86.0"`, `edition = "2021"`, `rust-version = "1.86"`, perfil `release` (`opt-level = "z"`, `lto = true`, `codegen-units = 1`, `strip = "symbols"`, `panic = "abort"`) y lints `[lints.rust]` / `[lints.clippy]` con `unsafe_code`, `unwrap_used`, `expect_used`, `panic` e `indexing_slicing` en `deny`. |
| **§4.1 / §4.4** (Confinamiento de `unsafe` a `sys.rs`) | [`comfyctl/src/sys.rs`](../../comfyctl/src/sys.rs) `L1-L261` | Único módulo con `#[allow(unsafe_code)]`; wrappers seguros sobre `libc::statvfs`, `sigaction` (`SIGTERM`/`SIGINT`/`SIGCHLD`), `setpgid`, `kill(-pgid, sig)`, `waitpid(..., WNOHANG)` y `chmod 0600`. |
| **§5.1 / §9** (Configuración y lectura de `/run/comfyui-pass`) | [`comfyctl/src/config.rs`](../../comfyctl/src/config.rs) `L1-L133` | Parseo tipado de variables de entorno con defaults seguros (`STRICT_START=1`, `MODEL_PROFILE=all`, `COMFY_MAX_RESTARTS=5`, `COMFY_RESTART_DELAY=5`, `COMFY_HEALTHY_UPTIME=600`) y función `consume_pass_file()` que lee `/run/comfyui-pass` y ejecuta `fs::remove_file` (`unlink`) inmediato. |
| **§6.1–§6.4** (Modo de arranque estricto `strict-check`) | [`comfyctl/src/strict.rs`](../../comfyctl/src/strict.rs) `L1-L423` | 8 comprobaciones previas al arranque con umbrales exactos de `§6.2` (GPU visible, driver `>= 570`, VRAM `>= 20000 MiB`, sonda `torch.cuda`, RAM `>= 24 GiB`, disco libre por perfil `100/55/20 GiB`, escritura en `COMFY_DATA_DIR`, validez de `MODEL_PROFILE`), escritura atómica de `/run/comfyui-strict.json` y código de salida `20` en `FATAL` con `STRICT_START=1`. |
| **§5.2 / §6.3** (Supervisor de procesos en Rust) | [`comfyctl/src/supervisor.rs`](../../comfyctl/src/supervisor.rs) `L1-L472` | Máquina de estados (`starting`, `running`, `crashed`, `restarting`, `exhausted`, `stopped`, `fatal`), reenvío de señales al grupo de procesos (`kill(-pgid, SIGTERM)`), escalado a `SIGKILL` tras 25s, reset de `restarts` tras `COMFY_HEALTHY_UPTIME`, rotación de logs y modo degradado (`fatal`) manteniendo vivos `/home`, `/status` y `/report.md` si `strict-check` falla. |
| **§6.3** (Banner rojo `FATAL` en portal y CLI Mac) | [`config/portal/index.html`](../../config/portal/index.html) `L82-L95`, `L247-L282`<br>[`tools/mac/comfy`](../../tools/mac/comfy) `L130-L152`, `L219-L236` | Banner rojo `#f85149` en `/home` cuando `api.strict.result === "fatal"` con lista de checks fallidos y comando `comfy destroy <id>`; detección de `strict.result == "fatal"` en `comfy open` y `comfy watch`. |
| **§4.3 / §8.1 / §8.2** (`Dockerfile` builder + CI `rust` + smoke 11–13) | [`Dockerfile`](../../Dockerfile) `L4-L16`, `L186`<br>[`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) `L194-L235`, `L512-L554`<br>[`scripts/start.sh`](../../scripts/start.sh) `L189-L250` | Stage `builder` fijado por digest `rust:1.86.0-slim-bookworm@sha256:57d415bbd61ce11e2d5f73de068103c7bd9f3188dc132c97cef4a8f62989e944`; job `rust` en GitHub Actions; pasos 11–13 del smoke test (`strict-check` en `--cpu`, `fake-nvidia-smi` con driver `550.54.14`, tamaño `<= 5 MB` y `ldd`); transición de `start.sh` a `exec /usr/local/bin/comfyctl supervise --external-status-server`. |
| **§8.3** (Tests unitarios Fase 1) | [`comfyctl/tests/strict_test.rs`](../../comfyctl/tests/strict_test.rs) `L1-L155`<br>[`comfyctl/tests/supervisor_test.rs`](../../comfyctl/tests/supervisor_test.rs) `L1-L97` | 5 tests de `strict-check` (incluyendo binario `nvidia-smi` simulado) y 2 tests de máquina de estados del supervisor (`O-02`, escritura atómica y `unlink` de `/run/comfyui-pass`). |

---

### Commit 4 (Actual) — `feat(comfyctl): phase 2 — status server, report and diag in Rust`

| Sección | Archivo y líneas | Cambio realizado |
|---|---|---|
| **§5.1 / §9** (Redacción centralizada de secretos) | [`comfyctl/src/redact.rs`](../../comfyctl/src/redact.rs) `L1-L57`<br>[`comfyctl/tests/redact_test.rs`](../../comfyctl/tests/redact_test.rs) `L1-L19` | `SecretRedactor` que reemplaza `COMFY_PASS` (leído de `/run/comfyui-pass`), `HF_TOKEN`, tokens `hf_[A-Za-z0-9]{12,}`, cabeceras `Bearer ...` y credenciales en URLs `https://user:pass@host` por `[REDACTED]`. |
| **§5.1 / O-04** (`tail_lines` desde `SeekFrom::End` y whitelist) | [`comfyctl/src/logtail.rs`](../../comfyctl/src/logtail.rs) `L1-L115`<br>[`comfyctl/tests/logtail_test.rs`](../../comfyctl/tests/logtail_test.rs) `L1-L42` | Lectura inversa por bloques de `8 KiB` desde `SeekFrom::End` (tope `4 MiB`) y validación estricta contra `LOG_WHITELIST` (rechazando path traversal); test sobre archivo sparse de `500 MB` en `< 50 ms`. |
| **§5.1 / O-08** (Motor de triaje 17 patrones) | [`comfyctl/src/triage.rs`](../../comfyctl/src/triage.rs) `L1-L191`<br>[`comfyctl/tests/triage_test.rs`](../../comfyctl/tests/triage_test.rs) `L1-L161` | Compilación de `triage_patterns.json` y evaluación con supresión `O-08a` (`nginx-502` cuando `comfy_running`), precisión `O-08b` (`hf-503`) y sensibilidad a mayúsculas `O-08c` en `errors_recent`; test de matriz `17×17`. |
| **§5.1 / O-06 / O-10 / O-11** (Recolectores de métricas) | [`comfyctl/src/collect/mod.rs`](../../comfyctl/src/collect/mod.rs) `L1-L9`<br>[`comfyctl/src/collect/gpu.rs`](../../comfyctl/src/collect/gpu.rs) `L1-L109`<br>[`comfyctl/src/collect/procfs.rs`](../../comfyctl/src/collect/procfs.rs) `L1-L152`<br>[`comfyctl/src/collect/phases.rs`](../../comfyctl/src/collect/phases.rs) `L1-L185`<br>[`comfyctl/src/collect/drift.rs`](../../comfyctl/src/collect/drift.rs) `L1-L132`<br>[`comfyctl/src/collect/models.rs`](../../comfyctl/src/collect/models.rs) `L1-L246`<br>[`comfyctl/tests/phases_test.rs`](../../comfyctl/tests/phases_test.rs) `L1-L50` | Recolectores nativos de GPU (`nvidia-smi` con timeout), `/proc/meminfo`, `statvfs`, uptime del contenedor (`/run/comfyui-started-at`, `O-10`), RSS de procesos en `/proc/[pid]/statm`, progreso de Fases 1/2 (`O-06`), deriva `pip`/`git` e inventario de los 26 modelos (`O-11`). |
| **§5.1 / §7** (Autodiagnóstico `comfyctl diag` y sondas Python) | [`comfyctl/src/diag.rs`](../../comfyctl/src/diag.rs) `L1-L296`<br>[`config/probes/torch_cuda.py`](../../config/probes/torch_cuda.py) `L1-L27`<br>[`config/probes/ort_cuda.py`](../../config/probes/ort_cuda.py) `L1-L40`<br>[`config/probes/python_imports.py`](../../config/probes/python_imports.py) `L1-L21` | Implementación de las 10 pruebas de diagnóstico en `comfyctl diag` preservando el contrato exacto `[DIAG][OK\|WARN\|FAIL] <id>: <detalle>` y `[DIAG] resumen: X ok, Y warn, Z fail (de 10 pruebas)`. |
| **§5.1 / §3** (Generador `/report.md` con sección `## 10.`) | [`comfyctl/src/report.rs`](../../comfyctl/src/report.rs) `L1-L343`<br>[`comfyctl/tests/report_test.rs`](../../comfyctl/tests/report_test.rs) `L1-L137`<br>[`comfyctl/tests/fixtures/report_expected.md`](../../comfyctl/tests/fixtures/report_expected.md) `L1-L119` | Generador determinista de `/report.md` con las cabeceras `## 0. Triage automático` .. `## 9. Colas de logs` más la nueva sección aditiva `## 10. Arranque estricto` (`§6.4`), truncado inteligente `<= 400 KB` y test de snapshot + redacción de secretos. |
| **§5.1 / O-03** (Servidor HTTP `comfyctl serve`) | [`comfyctl/src/http.rs`](../../comfyctl/src/http.rs) `L1-L433`<br>[`comfyctl/src/status_html.html`](../../comfyctl/src/status_html.html) `L1-L237`<br>[`comfyctl/tests/http_test.rs`](../../comfyctl/tests/http_test.rs) `L1-L130` | Servidor `tiny_http` en `127.0.0.1:18189` con pool acotado de 4 workers, caché de 3s para `/status/api`, hilo recolector pesado cada 60s (`O-03`) y soporte `GET`/`HEAD` en `/status`, `/status/api`, `/report.md`, `/diag`, `/download-status` y `/logs/<nombre>?n=N`. |
| **§10.2 / §8.2** (Eliminación de scripts legados y paso 14 smoke) | [`scripts/start.sh`](../../scripts/start.sh) `L180-L218`<br>[`Dockerfile`](../../Dockerfile) `L67`, `L186-L212`<br>[`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml) `L33-L90`, `L502-L511`<br>`git rm scripts/status-server.py scripts/diag.sh` | Eliminación de `scripts/status-server.py` y `scripts/diag.sh`; `start.sh` delega servidor de estado, `diag`, `strict-check` y supervisión a `exec /usr/local/bin/comfyctl supervise`; paso 14 en smoke test verificando cabeceras `## 0.`..`## 10.` y `comfyctl diag`. |

---

## 2. Checks Ejecutados

| Check | Comando ejecutado | Resultado |
|---|---|---|
| **Formato Rust (`rustfmt`)** | `cargo fmt --manifest-path comfyctl/Cargo.toml -- --check` | `EXIT 0` (`0` diffs) |
| **Linter estricto (`clippy`)** | `cargo clippy --manifest-path comfyctl/Cargo.toml --all-targets -- -D warnings` | `EXIT 0` (`0` warnings, `0` errors; `unsafe_code`, `unwrap_used`, `expect_used`, `panic`, `indexing_slicing` denegados) |
| **Tests unitarios e integración (`cargo test`)** | `cargo test --manifest-path comfyctl/Cargo.toml --locked` | `EXIT 0` — **8 suites / 13 tests en verde**: `http_test` (1), `logtail_test` (1), `phases_test` (1), `redact_test` (1), `report_test` (1), `strict_test` (5), `supervisor_test` (2), `triage_test` (1) |
| **Auditoría de licencias, CVEs y fuentes (`cargo-deny`)** | `cargo deny --manifest-path comfyctl/Cargo.toml check` | `EXIT 0` — `advisories ok, bans ok, licenses ok, sources ok` (`0` vulnerabilidades, `0` crates duplicados) |
| **Presupuesto de dependencias transitivas (`<= 40`)** | `cargo tree --manifest-path comfyctl/Cargo.toml \| wc -l` | **32 líneas** (`<= 40` exigido en `§4.2` / `§4.4`) |
| **Confinamiento de `unsafe` (`§4.4`)** | Búsqueda de `unsafe` en `comfyctl/src/**/*.rs` | Único bloque `#[allow(unsafe_code)]` en [`comfyctl/src/sys.rs`](../../comfyctl/src/sys.rs); prohibido por `[lints.rust] unsafe_code = "deny"` en el resto del crate |
| **ShellCheck (`§9.1`)** | `shellcheck scripts/start.sh scripts/download-models.sh tools/mac/comfy` | `EXIT 0` (`0` warnings/errors) |
| **Sintaxis Python (`py_compile`)** | `python -m py_compile config/probes/*.py` | `EXIT 0` (`torch_cuda.py`, `ort_cuda.py`, `python_imports.py`) |
| **Paridad de modelos (`O-11`) y esquemas (`§3`)** | Validación de los 26 modelos entre `scripts/download-models.sh` y `comfyctl/src/collect/models.rs`, más validación de `docs/contratos/*.schema.json` | `26/26 OK`, `17/17` patrones de triaje OK, `0` referencias CDN externas en HTML |
| **Finales de línea LF** | Comprobación de `\r\n` (CRLF) en todos los archivos versionados | `0` archivos con CRLF |

---

## 3. Verificaciones de Red Realizadas con Fuente

1. **Requisito mínimo de driver NVIDIA en Linux para CUDA 12.8 (`[Conocimiento]` en `§6.2 #2`)**:
   - **Fuente consultada**: *NVIDIA CUDA 12.8 Toolkit Release Notes — Table 2 (CUDA Toolkit and Minimum Required Driver Version for CUDA Minor Version Compatibility)* (`https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html`).
   - **Resultado verificado**: CUDA 12.8 GA (`12.8.0`) exige en Linux x86_64 un driver **`>= 570.26`** (y CUDA 12.8 Update 1 exige `>= 570.124.06`). En imágenes que no instalan `cuda-compat-*` de versión cruzada, un driver `< 570` hace que `torch.cuda.is_available()` falle al inicializar el runtime CUDA 12.8. El umbral `driver_major >= 570` en `comfyctl::strict` queda verificado y documentado en [`docs/decisiones.md`](../decisiones.md).
2. **Estado y facturación de una instancia en Vast.ai tras salida `!= 0` del contenedor (`[Verificar]` en `§6.3`)**:
   - **Fuente consultada**: Documentación oficial de Vast.ai sobre ciclo de vida de instancias y facturación (*Instance Lifecycle & Billing*, `https://docs.vast.ai/documentation/instances/creating` y `https://docs.vast.ai/documentation/instances/billing`).
   - **Resultado verificado**: Cuando el proceso principal del contenedor termina con código distinto de cero, `actual_status` pasa a `exited`/`stopped` (o se reinicia si el runtime lo relanza). Aunque el cargo por GPU activa se detiene en estado `stopped`/`exited`, **el coste por almacenamiento en disco (`disk_space`) se sigue cobrando cada segundo** hasta que el usuario ejecuta `vastai destroy instance <id>`. Por ello, ante un fallo `FATAL` en `comfyctl supervise`, se omiten descargas y `main.py` pero se mantienen vivos **Nginx + `comfyctl serve`** mostrando el banner rojo `FATAL` con la instrucción `comfy destroy <id>`.
3. **Endpoints de la API REST v0 de Vast.ai (`[Verificar]` en `§5.3`)**:
   - **Fuente consultada**: Código fuente oficial de `vast-ai/vast-cli` (`vast.py`, `https://github.com/vast-ai/vast-cli/blob/master/vast.py`).
   - **Resultado verificado**: Listar instancias `GET https://console.vast.ai/api/v0/instances/`, alquilar oferta `PUT https://console.vast.ai/api/v0/asks/{id}/`, destruir instancia `DELETE https://console.vast.ai/api/v0/instances/{id}/` con cabecera `Authorization: Bearer <API_KEY>`.
4. **Digest SHA-256 de la imagen oficial `rust:1.86.0-slim-bookworm` (`§4.3`)**:
   - **Fuente consultada**: Registro oficial de Docker Hub (`https://hub.docker.com/v2/repositories/library/rust/tags/1.86.0-slim-bookworm`).
   - **Resultado verificado**: Digest `sha256:57d415bbd61ce11e2d5f73de068103c7bd9f3188dc132c97cef4a8f62989e944` fijado en [`Dockerfile`](../../Dockerfile).
5. **Commit SHAs de GitHub Actions para el job `rust` (`§8.1`)**:
   - **Fuente consultada**: API de GitHub (`git ls-remote`).
   - **Resultado verificado**:
     - `dtolnay/rust-toolchain`: `683b588c487a69d01f6c6ddf40f036ff682cc2c8`
     - `actions/cache` (`v6.1.0`): `55cc8345863c7cc4c66a329aec7e433d2d1c52a9`
     - `EmbarkStudios/cargo-deny-action` (`v2.1.1`): `3c6349835b2b7b196a839186cb8b78e02f7b5f25`

---

## 4. Desviaciones Respecto a la Propuesta y su Justificación

1. **Elección de `chrono` dentro de la disyuntiva `"time o chrono (uno solo)"` de `§4.2`**:
   - **Detalle**: En la tabla de `§4.2` se permite `"time o chrono (uno solo)"`, mientras que en el ejemplo orientativo de `§10.1` aparecía `time = "0.3"`.
   - **Motivo técnico**: En la base de datos actual de RustSec (`cargo-deny`), todas las versiones de `time <= 0.3.46` disparan la vulnerabilidad `RUSTSEC-2026-0009` (DoS por agotamiento de pila en parseo RFC 2822), mientras que `time >= 0.3.47` exige `rustc 1.88.0` (incompatible con el MSRV fijado `1.86.0`). Al usar `chrono = { version = "0.4", default-features = false, features = ["std"] }` (el otro crate permitido en `§4.2`), compila limpiamente en `rustc 1.86.0`, pasa `cargo deny check` con **0 advisories** y mantiene el árbol total en **32 líneas** (`<= 40`).
2. **Ninguna otra desviación**:
   - Se respetaron todos los parámetros fijados (`STRICT_START=1` por defecto, umbrales de `§6.2` tal cual, `tiny_http`, `libc` sin `nix`, `pico-args`, un único crate de fechas, Fase 3 excluida, `O-15` excluido, `O-12` exigiendo `COMFY_IMAGE` y orden obligatorio de 4 commits).
