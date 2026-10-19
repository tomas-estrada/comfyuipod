# Auditoría V8 — Revisión del commit `863025a` (correcciones V7, R-01..R-16)

**Alcance**: commit `863025a` `fix(comfyctl): resolve Audit V7 findings (R-01..R-16)` (32 archivos, +1554/−480) sobre `98867a6`, y el informe reescrito `docs/auditorias/IMPLEMENTACION_V6_Y_RUST.md`.
**Método**: lectura estática del diff completo y del árbol en HEAD; contraste con `AUDITORIA_AGENTE_V7.md`, `scripts/download-models.sh` (formato real de log) y el workflow. El auditor **no ejecutó** `cargo`, `docker` ni CI. Convenciones como en V7: `[Repo]`, `[Conocimiento]`, `[Verificar]`.

---

## 1. Veredicto

**Mejora sustancial; queda un bloqueante nuevo en CI y dos puntos de honestidad del informe.** Los tres altos de V7 están cerrados en código: el parser de fases reconoce el formato real, `strict-check` aislado ya no falla por `.htpasswd`, S4 tiene margen y límite de cgroup, las vistas FATAL usan `detail`/`remediation`, `diag` tiene timeouts y corre antes de Fase 1, y los umbrales vuelven a §6.2 con S10 `cuda_torch`.

Sin embargo, el nuevo paso de CI "download-models.sh en seco" **falla de forma determinista** por el mismo motivo que motivó R-01: está escrito contra un formato imaginado del script y además el script ignora la variable `HF_CLI` que el paso intenta inyectar. Como ese paso vive en el job `validate`, del que dependen `rust` y `build-and-push`, el primer workflow volverá a quedar en rojo.

Recuento: **1 alto, 3 medios, 5 bajos**. Tras cerrar S-01..S-03 el repo queda apto para V21 (primer workflow) y, si V21 es verde, para la primera sesión Vast.ai (V23/V24).

---

## 2. Estado de los hallazgos V7

| ID | Estado | Evidencia `[Repo]` |
|---|---|---|
| R-01 parser de fases | **Cerrado en código**, test no fiel (S-02) | `phases.rs`: `INICIANDO FASE n`, `PLAN FASE n: N archivos`, `FASE n COMPLETADA`, `OMITIENDO FASE 2`, `SKIP (ya existe):`, `OK: /ruta`, `ERROR: Falló la descarga de`, comparación por basename, `elapsed_s` por timestamps, reset en el segundo `run_phase1` de `--phase2`, exclusión de `OK: InsightFace buffalo_l listo en`. |
| R-02 paso 11 | Cerrado | `require_htpasswd` (`from_env` false salvo `STRICT_REQUIRE_HTPASSWD=1`; `from_env_supervise` true); S7 `warn` si falta y no se exige; pasos 11/12 con `SKIP_MODEL_DOWNLOAD=1 STRICT_MIN_RAM_MB=4096 STRICT_MIN_DISK_GB=1` y prueba con/sin `.htpasswd`. |
| R-03 RAM | Cerrado | `probe_mem_total_mb` → `(ok, mb)` con cgroup v2/v1; S4 margen 5 %; fallback ilegible → `warn`; smoke paso 1 con umbrales explícitos. |
| R-04 vistas FATAL | Cerrado | Portal L527, `status_html.html` L194, `comfy` `cmd_watch`/`cmd_open` usan `id/name/detail/remediation`; test `strict_contract_fields_match_portal_status_and_cli_views`; CI rechaza `c.message/observed/expected`. |
| R-05 docs | Cerrado | README L22/L101–L104, `decisiones.md` L124–L160, `walkthrough.md` alineados con `config.rs`. |
| R-06 §6.2 | Cerrado con decisión | VRAM 20000/22000, RAM 16384/32768 (banda WARN), disco 75/45/10/5, S10 `cuda_torch` (20 s, `COMFY_PROBE_PYTHON`, `COMFY_PROBES_DIR`), código 20 conservado. Siguen fuera `compute_cap`, `nvenc`, `net.hf`, `ports.loopback` (aceptable: `nvenc` lo cubre `diag`; los otros son WARN informativos). Ver S-08. |
| R-07 timeouts diag | Cerrado | `sys::run_command_with_timeout`; `diag.rs` 10/15/5 s con `COMFY_DIAG_TIMEOUT_SECS`; `supervisor.rs` ejecuta `diag` síncrono tras `strict-check` y antes de Fase 1. Ver S-06, S-09. |
| R-08 tests | Parcial | 15 tests; `strict_test` cubre snapshot CI 16 GB y campos del contrato; `phases_test` usa fixture sintético (S-02); paso CI en seco roto (S-01). |
| R-09 builder | Cerrado | `rust-toolchain.toml` fuera del `COPY`; glibc documentado en `decisiones.md` L129; paso 13 con lista blanca de `ldd`. |
| R-10 `comfy open` | Cerrado | Consulta `/home` y `/status/api` durante 502; imprime checks y abre `/home` con rc 20. |
| R-11 secreto | Cerrado | `read_secret_file(path, unlink)`; solo `supervise` borra. |
| R-12 start.sh | Cerrado | Cabecera 0–5, sin `status-server.log`, flag eliminado. |
| R-13 esquema | Cerrado con reservas (S-03) | Bloque `strict` añadido; `$id` como URN; `failed` pasa a `["integer","array"]`; `gpu.processes` añadido como `[]`. |
| R-14 `comfy rent` | Cerrado `[Verificar]` | `-e "K=V"` entrecomillado; depende de cómo `vastai` divida `--env` (V27). |
| R-15 report | Cerrado | `nodes_lock_count`; truncado en frontera UTF-8. |
| R-16 caché | Cerrado con defecto lógico (S-05) | TTL 300 s, `cache_age_s`, `status_server.running` por puerto. |

---

## 3. Hallazgos nuevos

### Alto

**S-01 · ALTA · `.github/workflows/docker-publish.yml` L96–L132 (job `validate`, paso "download-models.sh en seco")**
El paso fallará siempre, por dos motivos independientes:
1. `scripts/download-models.sh` L51–L58 asigna `HF_CLI` por autodetección (`command -v hf` → `hf`; si no, `huggingface-cli`; si no, `echo "[WARN] …Omitiendo descargas." && exit 0`). **Ignora `HF_CLI` del entorno.** En el runner no hay `hf`, así que el script sale con rc 0 sin escribir ninguna traza `[MODELS]`, y el primer `grep -q "INICIANDO FASE 1 (2.79 GiB):"` falla. Si el runner tuviera `hf`, el paso intentaría descargas reales de 27 GiB.
2. Cuatro de los ocho `grep` buscan cadenas que el script no emite: `INICIANDO FASE 2 (Segundo plano` (real: `Segundo Plano`, L221), `PLAN FASE 2: 8 archivos (MODEL_PROFILE=general)` (real: `PLAN FASE 2: 10 archivos (general)`, L226 con `phase2_planned=10`), `SKIP (ya existe): det_10g.onnx` (real: `SKIP (ya existe): InsightFace buffalo_l`, L158), `FASE 2 COMPLETADA.` (real: `FASE 2 COMPLETADA (Perfil: general).`, L295).
Es la misma clase de error que R-01: se escribe contra un formato supuesto en lugar de leer el script. Como `validate` es prerrequisito de `rust` y `build-and-push`, bloquea todo el workflow.
Acción: (a) en `download-models.sh`, respetar `HF_CLI` si viene definido y es ejecutable (`HF_CLI="${HF_CLI:-}"`; autodetectar solo si está vacío); (b) corregir los `grep` con las cadenas literales del script, o mejor, sustituirlos por una comprobación del parser real: guardar `dl.log` como artefacto y, en el job `rust`, ejecutar un test que lea `COMFYCTL_PHASES_LOG` si existe y exija `phase1.state == done`, `phase1.done == 12`, `phase2.state == done`, `phase2.done == 10`; (c) **commitear ese mismo `dl.log` como `tests/fixtures/download_models.log`** (cierra S-02).
Decisión: obligatoria; bloquea V21.

### Medios

**S-02 · MEDIA · `comfyctl/tests/fixtures/download_models.log`, `phases_test.rs`, informe R-01**
El fixture se describe como "real" pero es sintético: nombres que no existen en el catálogo (`sdxl_vae.safetensors`, `RealESRGAN_x4plus_anime_6B.pth`, `hand_yolov8n.pt`, `animagine-xl-4.0-opt.safetensors`, `noobaiXLNAIXL_vPred10Version.safetensors`, repo `iahispano/applio`), cabeceras que el script no produce (`INICIANDO FASE 1 (Síncrona — Modelos base indispensables)`, `PLAN FASE 2: 3 archivos para MODEL_PROFILE=anime`, `RESUMEN FASE 1: 0 fallo(s) de 12 archivos.`, `INICIANDO FASE 2 (Asíncrona en segundo plano — Perfil: anime)`). El parser tolera ambos formatos, así que R-01 está cerrado, pero el test sigue sin probar el contrato real y el informe lo afirma.
Acción: reemplazar por la salida literal del dry-run de S-01 (perfil `general`, `--phase1` + `--phase2`) y ajustar las aserciones (`phase2.total == 10`, `current_file == None` al terminar). Mantener el segundo test sintético solo para el caso `ERROR`/`minimal`.
Decisión: obligatoria junto con S-01.

**S-03 · MEDIA · Defectos latentes de `98867a6` corregidos sin declararlos; contrato adaptado a la implementación**
Tres cosas que en `98867a6` habrían roto CI y que `863025a` arregla de paso sin mencionarlo como defecto:
1. `comfyctl/tests/fixtures/*.log` no estaban versionados (`.gitignore` `*.log`); `include_str!` habría impedido compilar `cargo test` en el job `rust`. El "árbol limpio" de V7 era por ficheros ignorados.
2. `gpu.processes` era `required` en el esquema congelado y Rust no lo emitía → el paso 4 del smoke habría fallado. Se resuelve añadiendo `processes: []` siempre (regresión funcional silenciosa frente a Python, que listaba procesos de GPU).
3. `phases.*.failed` era `array` en el esquema y Rust emite entero → mismo fallo. Se resuelve relajando el esquema a `["integer","array"]`.
Los puntos 2 y 3 adaptan el contrato "congelado" a la implementación en lugar de al revés. El parser ya captura los nombres de los ficheros fallidos (`rx_err`), así que emitir `failed` como lista de nombres era trivial y más útil para el agente de diagnóstico.
Acción: emitir `failed` como array de nombres (y devolver el esquema a `array`), o declarar explícitamente la ruptura y fijar `integer`; rellenar `processes` con `nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits` (con timeout) o marcarlo `deprecated` en el esquema; anotar los tres defectos en `decisiones.md` como "detectados en V8, origen `98867a6`".
Decisión: obligatoria la anotación; la elección `array`/`integer` a criterio del dueño (recomendación: array).

**S-04 · MEDIA · `IMPLEMENTACION_V6_Y_RUST.md` (reescrito)**
Mucho mejor que la versión anterior, pero persisten afirmaciones no localizables: `$id` "actualizado a `https://comfyuipod.local/schemas/...`" (real: `urn:comfyuipod:schema:status-api:v1`); `is_tcp_port_listening(status_port)` (real: `check_tcp_port(cfg.status_server_port, 100)`); R-14 "se rechazan comillas dobles literales en `COMFYUI_EXTRA_ARGS`" (no existe tal validación en `tools/mac/comfy`); "cabecera de `start.sh` a 7 pasos" (son 0–5); fixture "real" (S-02); paso CI en seco presentado como funcional (S-01).
Acción: corregir; regla operativa para el ejecutor: **cada afirmación del informe debe poder confirmarse con un `grep` literal en HEAD**, y el informe debe declarar los defectos propios que descubre al corregir (S-03).
Decisión: obligatoria.

### Bajos

**S-05 · BAJA · `http.rs::get_heavy_cached` + `report.rs` `## 8`**
Si la caché pesada tiene más de 300 s se recalcula de forma síncrona en la petición (reintroduce la latencia de O-03 cuando el hilo de refresco muere) y, como tras recalcular `cache_age_s = 0`, el `[WARN]` "antigüedad > 300 s" de `## 8` no puede dispararse nunca. Elegir una semántica: servir la caché vieja con el aviso (recomendado) o recalcular; no ambas.

**S-06 · BAJA · `sys::run_command_with_timeout`**
Tras `kill()` del hijo se hace `join()` de los hilos lectores; si un nieto heredó los pipes (p. ej. una sonda que lance `bash -c` o un `python3` que haga `subprocess`), `read_to_end` no termina y el timeout no protege. Con las sondas actuales (python3 directo, ffmpeg, nginx -t, ps, ldconfig, git, pip3) es improbable. Documentar la limitación en el doc-comment; si se añaden sondas con shell, usar `setsid` y matar el grupo.

**S-07 · BAJA `[Verificar]` · `tools/mac/comfy` `cmd_rent` L527–L543**
El entrecomillado `-e "K=V"` dentro de la cadena `--env` solo funciona si `vastai` divide con semántica de shell; si divide por espacios literalmente, las comillas acabarán dentro del valor. Verificar en V27 con `--dry-run` y una creación real con `COMFYUI_EXTRA_ARGS="--fast --cpu"` inspeccionando `env_masked` en `/status/api`.

**S-08 · BAJA · `strict.rs` S3/S4**
La banda WARN solo se evalúa si `STRICT_MIN_VRAM_MB >= 20000` y `STRICT_MIN_RAM_MB >= 16384`. Si el dueño baja el mínimo (p. ej. para una 4090 de 24 GB no cambia nada, pero para pruebas con 16000 sí), la banda desaparece sin aviso. Documentar el gate en README L102–L103 o eliminarlo (la banda tiene sentido siempre que `warn > min`).

**S-09 · BAJA · `supervisor.rs` L286–L290**
`diag` síncrono antes de Fase 1 añade al arranque, en el peor caso, la suma de timeouts (≈ 75 s: 10 + 20 + 15 + 15 + 15 + 5 + 5 + 5). Aceptable y preferible a la concurrencia anterior; anotar en README (sección de tiempos de arranque) que "Fase 1 empieza tras el autodiagnóstico (≤ 90 s)".

---

## 4. Lo que no tocar

Igual que V7 §6, más: `require_htpasswd` por modo de invocación, margen 5 % en S4, código 20, orden `strict-check → diag → Fase 1`.

---

## 5. Verificaciones que requieren ejecución

| ID | Qué | Criterio |
|---|---|---|
| V21 | Primer workflow completo tras S-01 | `validate` (incluido dry-run), `rust`, smoke 1–14 verdes. Adjuntar el `dl.log` del dry-run como artefacto. |
| V22 | `docker build` del stage `builder` | Sin descargas de rustup; anotar tiempo. |
| V23 | Sesión Vast.ai perfil `all` | `/status/api.phases.phase2.done` avanza; `phase_banner` `N/14 (fichero)`; `## 5` del informe con `done/total` correctos. |
| V24 | FATAL simulado `-e STRICT_MIN_VRAM_MB=99999` | Banner con `S3 (vram_min): … — Remediación …` legible en `/home`, `/status`, `comfy watch`, `comfy open` (rc 20 sin esperar 10 min); salida 20 tras 600 s. |
| V25 | `comfyctl diag` en frío | 10 líneas + resumen; con `COMFY_DIAG_TIMEOUT_SECS=1` y GPU, las sondas lentas terminan como `FAIL|timeout`. |
| V26 | Redacción | `/report.md`, `/diag`, `/logs/start` sin clave ni `HF_TOKEN`. |
| V27 | `comfy rent` | `--dry-run` y alquiler real con `COMFYUI_EXTRA_ARGS` de dos palabras; comprobar `env_masked` (S-07). |
| V28 | S10 real | En Vast con GPU sana: `S10 ok` con `matmul_1024x1024=…ms`; con `COMFY_PROBE_PYTHON=/bin/false`: `S10 fatal`. |

V13–V20 (V6) siguen vigentes tras V21.

---

## 6. Plan de corrección

1. **S-01**: `HF_CLI` respetado en `download-models.sh`; `grep` literales o verificación por parser; artefacto `dl.log`.
2. **S-02**: fixture = salida literal del dry-run; aserciones ajustadas.
3. **S-03**: decidir `failed` (recomendado array de nombres) y `processes`; anotar defectos latentes en `decisiones.md`.
4. **S-04**: corregir el informe; aplicar la regla "localizable por `grep`".
5. **S-05, S-08, S-09**: ajustes pequeños de lógica/documentación.
6. **S-06, S-07**: documentar; verificar en V27.

Commit sugerido: `fix(ci): resolve Audit V8 findings (S-01..S-09)`. Sin push. El auditor revisa en V9 solo S-01..S-04 por lectura y, si están cerrados, autoriza V21.

---

## 7. Criterio de producción

Apto para primer workflow (V21) cuando S-01..S-04 estén cerrados. Apto para primera sesión Vast.ai cuando V21 sea verde y el dueño ejecute V23 y V24 con capturas de `/status/api`, `docker logs` y `comfy watch`.
