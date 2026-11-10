# SRS — ComfyUIPod

**Rama:** `docs/maad-architecture-2026-11-10` · **Base:** `main` `b7f21de`
**Fecha:** 2026-11-10 · **Fase:** 3 de 4 (SRS)

## Convenciones

- **ID:** `REQ-<área>-<n>`. Estable; no renumerar al añadir requisitos.
- **Trazabilidad:** `archivo:línea` verificado contra el fichero citado.
- **Confianza:**
  - **confirmado** — el código implementa literalmente lo afirmado.
  - **inferido** — derivado de evidencia indirecta; la derivación se indica.
  - **hueco** — no verificable en este entorno.
- **Prioridad:** MUST / SHOULD / MAY (RFC 2119).

Cada requisito declara **una** condición verificable. Cuando una verificación no
pudo ejecutarse aquí, se marca `hueco` en lugar de afirmar conformidad.

---

## Área A — Superficie de red y autenticación

### REQ-A-01 — Único punto de entrada público — MUST — confirmado
El contenedor expone **un solo** puerto público (8188), servido por Nginx.

- `config/nginx.conf:54` — `listen 8188 default_server;`
- `config/nginx.conf:55` — `listen [::]:8188 default_server;`

### REQ-A-02 — Servicios internos en loopback — MUST — confirmado
ComfyUI, comfyctl, studio-server y ttyd escuchan exclusivamente en `127.0.0.1`.

- `scripts/start.sh:12` — ComfyUI `127.0.0.1:18188`
- `scripts/start.sh:11` — comfyctl `127.0.0.1:18189`
- `comfyctl/src/supervisor.rs:336` — studio-server `127.0.0.1:18190`
- `scripts/start.sh:386` — ttyd `127.0.0.1:7681`

### REQ-A-03 — Basic Auth en toda ruta de contenido — MUST — confirmado
Toda `location` que sirve contenido exige `auth_basic`; solo `/favicon.ico`,
`/healthz` y `/_starting.html` quedan exentos por diseño.

- `config/nginx.conf:109-110`, `:128-129`, `:146-147`, `:169-170`, `:180-181`,
  `:199-200` — `auth_basic` + `auth_basic_user_file`
- Exenciones: `config/nginx.conf:73` (`/favicon.ico`, `auth_basic off`),
  `:86` (`/healthz`), `:95` (`/_starting.html`, `internal`)

### REQ-A-04 — Sin bypass de autenticación por cabecera — MUST — confirmado
`require_auth` se deriva solo de configuración y entorno; ninguna cabecera de
petición puede desactivarlo.

- `studio/comfy_studio/app.py:540-544` — cálculo de `require_auth`
- Regresión cubierta por `studio/tests/test_health_auth.py:152-156`

### REQ-A-05 — 401 sin credenciales válidas — MUST — confirmado
Una petición sin credenciales a una ruta protegida devuelve 401.

- `studio/comfy_studio/app.py:566-572` — 401 en el middleware
- `studio/comfy_studio/webutil.py:377-402` — comparación con `compare_digest`

### REQ-A-06 — 405 en método no permitido — MUST — confirmado
`/status/api` devuelve 405 con `Allow: GET, HEAD` ante cualquier otro método.

- `comfyctl/src/http.rs:152-155` — `405` y cabecera `Allow`

---

## Área B — Cabeceras de seguridad y CSP

### REQ-B-01 — HSTS en toda location — MUST — confirmado
Las 17 `location` declaran `Strict-Transport-Security`.

- Verificado por `studio/tests/test_panel_web.py:49-74`
- `config/nginx.conf:117`, `:138`, `:156`, y 14 más

### REQ-B-02 — CSP en toda location — MUST — confirmado
Las 17 `location` declaran `Content-Security-Policy`.

- Verificado por `studio/tests/test_panel_web.py:49-74`

### REQ-B-03 — Sin `'unsafe-inline'` en `script-src` — MUST — confirmado
Ninguna CSP usa `'unsafe-inline'`; las vistas con bloques inline usan hash sha256.

- `config/nginx.conf:121` — `script-src 'self' 'sha256-…'` (portal `/home`)
- `config/nginx.conf:157` — ídem (grupo `/status`)
- Verificado por `studio/tests/test_panel_web.py` (test
  `test_csp_uses_content_hashes_not_unsafe_inline`)

### REQ-B-04 — Los hashes CSP corresponden al HTML servido — MUST — confirmado
Si el HTML cambia sin regenerar, CI falla.

- `scripts/update_csp_hashes.py --check` invocado en
  `.github/workflows/docker-publish.yml:57`
- Tests: `test_csp_hashes_match_the_served_html` y
  `test_redactor_never_breaks_the_json_body_it_runs_on`

### REQ-B-05 — `server_tokens off` — SHOULD — confirmado
- `config/nginx.conf:12`

---

## Área C — Separación de almacenamiento

### REQ-C-01 — `/opt/ComfyUI` inmutable, datos en el volumen — MUST — confirmado
`models`, `input`, `output` y `user` se sustituyen por symlinks al volumen.

- `scripts/start.sh:215-220` — bucle de symlinks
- `scripts/start.sh:17-18` — `COMFY_APP_DIR`, `COMFY_DATA_DIR`

### REQ-C-02 — Checkpoints de LatentSync en el volumen — MUST — confirmado
- `scripts/start.sh:222-228`

### REQ-C-03 — `/workspace/ComfyUI` apunta al dir de app — SHOULD — confirmado
- `scripts/start.sh:230-232`

### REQ-C-04 — Aviso si el volumen no está montado — MUST — confirmado
Si tras el tiempo de espera `COMFY_DATA_DIR` sigue en el overlay del contenedor,
se avisa; con `COMFY_REQUIRE_PERSISTENT_VOLUME=1` se aborta.

- `scripts/start.sh:160` — aviso
- `scripts/start.sh:161-163` — aborto fail-closed

---

## Área D — Arranque estricto

### REQ-D-01 — `strict-check` evalúa S1–S17 — MUST — confirmado
- `comfyctl/src/strict.rs:1572` (rango de implementación)
- `docs/GUIA_PASO_A_PRODUCCION.md:190` documenta S1–S17

### REQ-D-02 — Fatal con `STRICT_START=1` termina con código 20 — MUST — confirmado
- `comfyctl/src/strict.rs:1568` — `Ok(20)`
- `comfyctl/src/main.rs:113` — `ExitCode::from(code)`

### REQ-D-03 — Ventana de gracia antes de terminar — SHOULD — confirmado
El pod sirve `/status` durante `STRICT_FATAL_GRACE` para inspección.

- `comfyctl/src/supervisor.rs:937-961`

### REQ-D-04 — `studio-server` sano antes de evaluar S11 — MUST — confirmado
La espera es acotada y comprueba que el hijo siga vivo.

- `comfyctl/src/supervisor.rs:342-377`
- `comfyctl/src/supervisor.rs:513-521` — verificación de proceso vivo tras conectar

### REQ-D-05 — Modo degradado con `STRICT_START=0` — SHOULD — confirmado
Un fallo pasa a `warn` en vez de `fatal`.

- `comfyctl/src/strict.rs:887-895`
- Cubierto por `comfyctl/tests/strict_test.rs:90-107`

---

## Área E — Adquisición de modelos

### REQ-E-01 — Adquisición respaldada por manifiesto — MUST — confirmado
Ningún activo se descarga si no está declarado en el manifiesto de destino.

- `studio/comfy_studio/asset_acquire.py:1286` — resolución del necesidad declarada
- Rechazo documentado: mensaje `asset acquisition rejected`

### REQ-E-02 — Verificación de digest antes de usar — MUST — confirmado
- `studio/comfy_studio/asset_acquire.py:1002-1006` — comparación sha256
- `studio/comfy_studio/asset_acquire.py:1085-1087` — selección de algoritmo

### REQ-E-03 — Revisión fijada por manifiesto — MUST — confirmado
- `studio/comfy_studio/asset_acquire.py:76-91` (revision + digest en el CLI)

### REQ-E-04 — Reintentos acotados con backoff — SHOULD — confirmado
Tres intentos con espera 2/5/10 s; `timeout` distinguido de fallo real.

- `studio/comfy_studio/asset_acquire.py` (política de reintentos)

### REQ-E-05 — Cortacircuitos de disco fail-closed — MUST — confirmado
Antes de cada descarga se exige espacio libre ≥ tamaño esperado + margen.

- `scripts/download-models.sh:135-153`

### REQ-E-06 — Paridad perfil bash ↔ manifiesto — MUST — confirmado
- `scripts/check_profile_parity.py:1`
- `config/models-core.json` (26 entradas)

### REQ-E-07 — Paridad catálogo ↔ `EXPECTED_MODELS` — MUST — confirmado
- `.github/workflows/docker-publish.yml:82-91` (check O-11)

### REQ-E-08 — Manifiesto core resolvible dentro de la imagen — MUST — confirmado
- `scripts/download-models.sh:94-108` — cadena de reserva
- `Dockerfile:478` — `COPY config/models-core.json /etc/comfyui/models-core.json`

---

## Área F — Recuperación de trabajos

### REQ-F-01 — Reintento automático acotado — MUST — confirmado
- `studio/comfy_studio/recovery.py:32` — `MAX_INTERRUPT_RETRIES = 1`
- `studio/comfy_studio/recovery.py:174` — condición de reintento

### REQ-F-02 — Re-enganche de prompts vivos en vez de re-render — SHOULD — confirmado
- `studio/comfy_studio/recovery.py:131-168`

### REQ-F-03 — Sondeos de re-enganche cancelados al apagar — SHOULD — confirmado
- `studio/comfy_studio/app.py:186-226` (drenaje en el lifespan)

### REQ-F-04 — Reanudación de producción por CAS — SHOULD — confirmado
- `studio/comfy_studio/orchestration/service.py:329-381` (checkpointer)

---

## Área G — Redacción de secretos

### REQ-G-01 — Respuestas JSON redactadas — MUST — confirmado
- `studio/comfy_studio/app.py:609-623`

### REQ-G-02 — Streams SSE redactados por chunk — MUST — confirmado
- `studio/comfy_studio/agent/llm.py:441` y `:651`
- `studio/comfy_studio/config.py` (`SecretStreamRedactor`)

### REQ-G-03 — Registros de `comfy_studio.*` redactados — MUST — confirmado
- `studio/comfy_studio/config.py` (`install_log_redaction`, cubre `lastResort`)
- `studio/comfy_studio/app.py:130-135` (reinstalado en el lifespan)

### REQ-G-04 — `/logs/*` con lista blanca — MUST — confirmado
- `comfyctl/src/logtail.rs:15-26` — 10 nombres permitidos
- `comfyctl/src/http.rs:231-232`

### REQ-G-05 — Superficie pública de comfyctl redactada — MUST — confirmado
- `comfyctl/src/http.rs:184`, `:220`, `:248`
- `comfyctl/src/report.rs:528`

### REQ-G-06 — Terminal web sin secretos en el entorno — SHOULD — confirmado
- `scripts/start.sh:386` — `env -u COMFY_PASS -u STUDIO_API_TOKEN -u HF_TOKEN`

---

## Área H — Confinamiento de rutas

### REQ-H-01 — Rutas de motor confinadas al volumen — MUST — confirmado
- `studio/comfy_studio/orchestration/adapters.py:229-303`
  (`_resolve_data_root`, `_allowed_roots`, `_confine_one`, `_confine_job_paths`)

### REQ-H-02 — Confinamiento en los tres puntos de entrada — MUST — confirmado
- `run_layer` (`:472`): `studio/comfy_studio/orchestration/adapters.py:490`
- `QaShotEvaluator.evaluate` (`:565`): `studio/comfy_studio/orchestration/adapters.py:568`
- `EngineAssembler.assemble` (`:624`): `studio/comfy_studio/orchestration/adapters.py:650`

### REQ-H-03 — Rutas legítimas del volumen preservadas — MUST — confirmado
- Tests: `test_confinement_preserves_legitimate_sibling_paths_on_the_volume`,
  `test_confinement_preserves_volume_paths_and_blocks_app_and_system_dirs`

### REQ-H-04 — IDs de proyecto validados — MUST — confirmado
- `studio/comfy_studio/projects.py:620-624` — `^[A-Za-z0-9_-]{1,64}$`

### REQ-H-05 — Nombres de fichero saneados en subida — MUST — confirmado
- `studio/comfy_studio/projects.py:160-200`

### REQ-H-06 — `/studio/api/view` rechaza componentes de ruta — MUST — confirmado
- `studio/comfy_studio/routers/assets.py:179-188`

---

## Área I — Gate de gasto humano

### REQ-I-01 — El lazo caro no corre sin aprobación — MUST — confirmado
- `studio/comfy_studio/orchestration/service.py:186` (`awaiting_approval`)
- `studio/comfy_studio/routers/productions.py:3-8` (gobernanza)

### REQ-I-02 — El agente nunca puede aprobar — MUST — confirmado
- `studio/comfy_studio/mcp_server.py:155`, `:174`, `:244`
- Test: `studio/tests/test_mcp_m3.py:78`

### REQ-I-03 — `approve` es sincrónico a propósito — SHOULD — confirmado
- Nota de gobernanza en `studio/comfy_studio/routers/productions.py:13-24`
- Límite de duración: `config/nginx.conf` (`proxy_read_timeout 3600s`)

---

## Área J — Salud de motores

### REQ-J-01 — `assert_production_ready` exige `ready` — MUST — confirmado
- `studio/comfy_studio/engines/health.py:109` (definición)
- `studio/comfy_studio/engines/health.py:122` (rechazo de no-`ready`)
- `studio/comfy_studio/engines/health.py:47` (valores del estado)

### REQ-J-02 — Tests declaran su precondición de stub — MUST — confirmado
- `studio/tests/test_m4_engine_health.py:47` (`delenv` explícito en
  `test_assert_passes_for_ready_engine`)

---

## Matriz de trazabilidad inversa

| Requisito | Verificación automatizada | Confianza |
|---|---|---|
| REQ-A-01/02 | `studio/tests/test_route_map.py` | confirmado |
| REQ-A-03/04/05 | `studio/tests/test_health_auth.py` | confirmado |
| REQ-A-06 | `.github/workflows/docker-publish.yml` (smoke) | confirmado |
| REQ-B-01/02/03/04 | `studio/tests/test_panel_web.py` + `scripts/update_csp_hashes.py` | confirmado |
| REQ-C-01/02/03/04 | smoke E2E en CI | confirmado |
| REQ-D-01/02/05 | `comfyctl/tests/strict_test.rs` | confirmado |
| REQ-D-03/04 | smoke E2E en CI | confirmado |
| REQ-E-01/02/03 | `studio/tests/test_asset_acquire*.py` | confirmado |
| REQ-E-05 | `scripts/download-models.sh --selftest` | confirmado |
| REQ-E-06 | `scripts/check_profile_parity.py` | confirmado |
| REQ-E-07 | `.github/workflows/docker-publish.yml:82-91` | confirmado |
| REQ-E-08 | `scripts/download-models.sh:94-108` | confirmado |
| REQ-F-01/02/04 | `studio/tests/test_recovery_m3.py`, `test_production_restart_recovery.py` | confirmado |
| REQ-F-03 | `studio/tests/test_health_auth.py` (apagado) | confirmado |
| REQ-G-01/02/03 | `studio/tests/test_config_redaction.py` | confirmado |
| REQ-G-04/05 | `comfyctl/tests/` + smoke CI | confirmado |
| REQ-H-01/02/03 | `studio/tests/test_m6_orchestration_adapters.py` | confirmado |
| REQ-H-04/05/06 | `studio/tests/test_projects*.py`, `test_assets*.py` | confirmado |
| REQ-I-01/02 | `studio/tests/test_m6_production_wiring.py`, `test_mcp_m3.py` | confirmado |
| REQ-J-01/02 | `studio/tests/test_m4_engine_health.py` | confirmado |

---

## Huecos

| # | Hueco | Requisitos afectados |
|---|---|---|
| G-01 | Build de imagen y smoke con GPU no ejecutados localmente (sin `docker`/GPU) | REQ-C-01..04, REQ-D-03/04 |
| G-02 | Coste real de arranque de `studio-server` no medido | REQ-D-04 |
| G-03 | Política exacta de reintentos de `asset_acquire` no leída línea a línea | REQ-E-04 |
| G-04 | Versiones de Nginx y ffmpeg no fijadas en el repo | REQ-B-01..05 |
