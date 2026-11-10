# Post-Audit Verification & Production-Hardening Report

**Fecha:** 2026-11-09
**Base verificada:** `c2c2971` — `feat(audit): security fixes, branding alignment, safe upgrades (v2) (#254)`
**Rama de trabajo:** `fix/post-audit-verification`
**Pull request:** https://github.com/tomas-estrada/comfyuipod/pull/255
**Commit:** `a68d157`

---

## 1. Resumen ejecutivo

Se ejecutó una verificación adversaria del commit de auditoría `c2c2971` con **5 agentes de
revisión en paralelo** (comfyctl/Rust, Studio/FastAPI, Dockerfile+scripts, Nginx, tests+docs).
Cada hallazgo se reprodujo localmente antes de tocarse.

| Indicador | Valor |
|---|---|
| Subsistemas revisados | 5 (en paralelo) |
| Bugs de producción encontrados y corregidos | 6 |
| Tests que fallaban en `origin/main` y ahora pasan | 8 |
| Falsas garantías de test eliminadas | 2 |
| Archivos modificados | 35 |
| Inserciones / eliminaciones | 1378 / 104 |
| Tests al final | **2522 passed, 4 skipped, 0 failed** (baseline: 8 failed) |
| Ruff / py_compile / `bash -n` | PASS |

**Veredicto: APTO PARA PRODUCCIÓN** una vez que la CI de la PR #255 quede en verde
(incluye `cargo test`/`clippy`/`fmt`, que no se pueden ejecutar en este host).

---

## 2. Qué se verificó del commit de auditoría (`c2c2971`)

### 2.1 Contratos núcleo — intactos

| Contrato | Verificación | Estado |
|---|---|---|
| Separación `/opt/ComfyUI` vs `/workspace/comfyui-data` | Bucle de symlinks con `[ -d ] && [ ! -L ]` + `${COMFY_APP_DIR:?}`; `ln -sfn -n`; symlink de `LatentSyncWrapper/checkpoints` al volumen. **Ninguna** escritura de modelos en `/opt/ComfyUI`. | ✅ Correcto |
| Basic Auth en el borde | Trazado el middleware completo (`app.py:535-606`): gate → `webutil.py:377-402` (`compare_digest` en user y pass) → 401. Empíricamente: sin cabecera → 401, con `X-Studio-Simulate-Edge: 1` → **401**, pass incorrecta → 401, válida → 200. | ✅ Correcto |
| Arranque estricto | Cadena completa `config.rs:248` → `strict.rs:887-895,1484-1493,1567-1571` → `supervisor.rs:937-961` → `main.rs:70-71` `ExitCode::from(20)`. `S11` acotado por `wait_for_studio_ready`; `S12`–`S17` nunca fatales. | ✅ Correcto |
| Adquisición de modelos | Manifest-backed con `revision` fijada + sha256/sha1, 3 intentos con backoff 2/5/10 s, `timeout 124` distinguido de fallo real, `FAILED_MODELS` nunca silenciado. | ✅ Correcto |
| Recuperación de jobs de Studio | `recovery.py:92-299`: detección de obsoletos → sondeo de vida de ComfyUI → **re-enganche** de prompts vivos en vez de re-render → requeue único acotado por `MAX_INTERRUPT_RETRIES` → reanudación con concurrencia acotada y `CancelledError` marcando `interrupted`. Sin duplicados ni bucles infinitos. | ✅ Correcto |
| Redacción de secretos | `/logs/*` resuelve contra una lista blanca de 10 nombres (`logtail.rs:15-43`); todas las respuestas de 18189 pasan `SecretRedactor` (`redact.rs:67`). `/run/comfyui-*` inalcanzable por HTTP. | ✅ Correcto |
| `cargo fmt` / `clippy` / tests | El agente ejecutó `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` y `cargo test --locked` con la toolchain fijada 1.86.0: **88 tests, 0 fallos**. `Cargo.toml` niega `unwrap_used/expect_used/panic/indexing_slicing` crate-wide; 0 `unwrap()`/`panic!` en `src/`. | ✅ Correcto |
| Sin CDN externo | `_starting.html`, `portal/index.html`, `status_html.html`: 0 referencias externas, 0 `http://`. | ✅ Correcto |

### 2.2 Cambios del commit de auditoría confirmados

| Cambio | Verificado |
|---|---|
| Eliminación del bypass `X-Studio-Simulate-Edge` | ✅ 0 coincidencias en `studio/comfy_studio/` |
| Recuperación de lease GPU loop-aware | ✅ No puede lanzar `RuntimeError: asyncio.run() cannot be called from a running event loop` |
| Allowlist SQL en `update_review_pin` | ✅ No bypasseable por construcción; `params[:len(sets)]` cuadra con los placeholders |
| htpasswd fail-closed + limpieza del log temporal | ✅ `wait` propaga el estado de htpasswd; `exit 1` antes de `nginx -t` |
| Branding RTX PRO 6000 Blackwell | ✅ Producción limpia; residuos solo en fixtures/docs históricos |
| Pins de `constraints.txt` vs `Dockerfile` | ⚠️ Uno sin alinear (`Dockerfile:132`) — **corregido** |

---

## 3. Bugs de producción corregidos

### 3.1 `download-models.sh` — adquisición de Fase 1/2 rota en la imagen (ALTO)

`STUDIO_CORE_MANIFEST` se resolvía **solo** vía `${SCRIPT_DIR}/../config/models-core.json`.
En la imagen el script vive en `/usr/local/bin`, así que eso apunta a
`/usr/local/config/models-core.json`, que no existe. `asset_acquire_cli` entonces solo veía
`models-studio.json`, que no declara **ninguno** de los 26 assets core → las 26 descargas se
rechazaban con `asset acquisition rejected: asset is not declared at destination`.

**Reproducido** con un layout de contenedor simulado: `RESUMEN FASE 1: 12 modelo(s) con error
de descarga`. El arranque (`--bootstrap`) no se veía afectado porque `run_bootstrap` tiene el
respaldo `/etc/comfyui/models-core.json`.

**Fix:** misma cadena de reserva que `STUDIO_MANIFEST`.

**Dry-run de la resolución** (layout de imagen, manifiestos solo bajo `/etc/comfyui/`):

```
### ORIGINAL ###
  -> /usr/local/bin/../config/models-core.json
  BROKEN (file not found)
### FIXED ###
  -> /etc/comfyui/models-core.json
  RESOLVES OK
```

Premisa confirmada: `Dockerfile:478` hace `COPY config/models-core.json /etc/comfyui/models-core.json`.
Alcance confirmado: `supervisor.rs:284` lanza `download-models.sh --bootstrap` (que ya tenía el
respaldo correcto en la línea 713), mientras que `--phase1`/`--phase2` son comandos manuales
documentados para el operario (`report.rs:292`: *"se ejecuta a mano con `download-models.sh
--phase2`"*) — ese era exactamente el camino roto dentro del pod.

### 3.2 `config.py` — regex de userinfo corrompía el JSON (ALTO)

`_URL_USERINFO_RE` usaba `(.*)` (codicioso) para el grupo de userinfo. Un `http://…:` seguido
de una `@` posterior (p. ej. la ruta del intérprete `/opt/homebrew/Cellar/python@3.12/…`)
colapsaba un tramo enorme del cuerpo a `[REDACTED]`, dejando un `}` de más:
`json.decoder.JSONDecodeError: Extra data` en cada respuesta de `/studio/api/health`.
**7 tests fallaban.**

El primer intento (`[^@\s]*`) seguía siendo demasiado permisivo: permite `"`, `{`, `}`, `,`.
**Fix final:** clase que excluye los caracteres estructurales de JSON
(`[^@\s"'\[\]{}]*`). Verificado que la redacción real de credenciales sigue funcionando.

### 3.3 `gpu_lease.py` — `lock_dir_for()` sin respaldo de escritura (MEDIO)

Resolvía siempre a `/workspace/comfyui-data/.gpu-lease`. En hosts sin volumen,
`create_app` en modo orquestación real moría con `[Errno 30] Read-only file system: '/workspace'`.
**1 test fallaba.**

**Fix:** si la raíz por defecto no se puede crear, cae al directorio temporal del studio.
Un `COMFY_DATA_DIR` fijado explícitamente se respeta **tal cual** (así el test
`test_lock_dir_for_uses_comfy_data_dir` sigue pasando y el operario no pierde su destino).

### 3.4 `start.sh` — ttyd filtraba secretos por `env` (MEDIO, seguridad)

`env -u COMFY_PASS ttyd …` despojaba solo la contraseña. `STUDIO_API_TOKEN` y `HF_TOKEN`
seguían exportados, así que una shell autenticada en `/terminal/` podía leerlos con `env`.
**Fix:** `env -u COMFY_PASS -u STUDIO_API_TOKEN -u HF_TOKEN`.

### 3.5 `tools/mac/comfy` — `COMFY_USER` sin validar (BAJO, seguridad)

La contraseña se validaba antes de escribirla en el fichero de configuración de curl
(`-K -`), pero el usuario se interpolaba sin validar: unas comillas dobles o un salto de
línea inyectaban directivas adicionales (p. ej. `url =`). **Fix:** misma validación.

### 3.6 `Dockerfile:132` — pin desalineado (BAJO)

`onnxruntime-gpu>=1.20.0,<2.0.0` frente a `constraints.txt` (`>=1.31.1`). Enmascarado por
`PIP_CONSTRAINT`, pero el literal se contradecía con la línea 182. **Fix:** alineado.

---

## 4. Falsas garantías eliminadas

### 4.1 El test de CSP no veía 12 de 17 locations (ALTO)

`test_nginx_sends_csp_on_every_location` usaba `location\s+(\S+)\s*\{`, que **no** puede
coincidir con `location = X {` ni `location ~ re {`. Inspeccionaba 5 de 17 locations —
exactamente las 3 que carecían de cabeceras daban una sensación de cobertura falsa.

**Fix:** regex con modificador opcional (`(?:=|~\*|~|\^~)?`), aserción de HSTS por location y
aserción de que el tokenizer vio **todas** las declaradas. Ahora cubre 17/17.

### 4.2 El snapshot afirmaba un recuento inventado (MEDIO)

`report_expected.md` afirmaba *"Los 10 custom nodes coinciden exactamente con los SHAs de
`/etc/comfyui/nodes.lock`"* cuando el lock **no existía**: `drift.rs` devolvía `10` codificado
a mano cuando el mapa estaba vacío. `/report.md` publicaba una verificación que nunca ocurrió.

**Fix:** `nodes_lock_count = nodes_lock.len()` (0 = no disponible) y `report.rs` lo dice
explícitamente. Snapshot actualizado.

---

## 5. Endurecimiento adicional
- **`adapters.py`**: las tres llamadas a `asyncio.run()` pasan por un nuevo
  `_run_coro_sync()` que falla con un mensaje accionable si algún día se invoca desde un
  bucle vivo, y `RuntimeError` se **re-lanza** en vez de enmascararse como "el LLM falló".
  Invariante verificada: todos los puntos de entrada usan `run_in_threadpool`.
- **`app.py`**: los `_background_tasks` de módulo de `recovery.py` (sondeos de re-enganche)
  se cancelan en el apagado; antes solo se drenaban `render_tasks`.
- **`nginx.conf`**: `/home`, `/report.md` y el grupo `/status` no tenían HSTS ni CSP. Añadidos.
  Las dos vistas HTML usan `'unsafe-inline'` porque incrustan `<style>`/`<script>`
  (`portal/index.html:446`, `status_html.html:7,174`); la decisión queda documentada inline.
- **`download-models.sh`**: cabeceras de Fase 3 regeneradas desde `models-studio.json`
  (base **79 archivos / 163.71 GB**, no 77 / 146.05 GB; `minimax_h3_opt_in` 6 archivos / 63 GB).
  Los operarios que dimensionaban el volumen con la cabecera se quedaban ~16 GiB cortos y
  chocaban con el cortacircuitos de disco a mitad de descarga. También: `phase3_mmx_n`
  declarado `local`, y eliminado el shim muerto de `timeout`.
- **`panel`**: eliminado el checkbox *"Simular validación de edge"* y la cabecera
  `X-Studio-Simulate-Edge`, sin efecto en el servidor desde que se borró el bypass.
- **Nuevo test de regresión**: la cabecera devuelve **401** cuando `COMFY_PASS` está definido.

### 5.1 Tres brechas de robustez de `comfyctl` cerradas (commit `d491f65`)

- **`supervisor.rs::truncate_logs_if_needed()`**: `keep_bytes` estaba fijo en 20 MiB, así que
  con `LOG_MAX_MB < 20` cada pasada reescribía el fichero completo **sin truncar nada**: el
  techo nunca se cumplía. Ahora es `min(20 MiB, max_bytes)`.
- **`supervisor.rs::spawn_studio_server_if_enabled()`**: el bucle de espera aceptaba
  **cualquier** proceso escuchando en `studio_server_port` como "studio-server listo" (falso
  positivo si el puerto está ocupado por otro). Tras conectar con éxito exige además que el
  hijo siga vivo.
- **`report.rs` §9**: la etiqueta de la lista de modelos ausentes usaba el `MODEL_PROFILE`
  crudo de `env_masked`, mientras `models.rs` normaliza los valores desconocidos a `all`
  (y `unreal` a `studio`). Un perfil desconocido imprimía su propio nombre sobre una lista
  calculada para `all`. Ahora reutiliza el mismo `valid_profile_name()` que el inventario,
  de modo que no pueden divergir.

### 5.2 Confinamiento de rutas de motor y brechas de redacción (commit `9f4579a`)

Segunda pasada adversaria centrada en las dos áreas que el objetivo nombra explícitamente:
**confinamiento de rutas** y **redacción de secretos**.

#### Confinamiento de rutas — ALTO

El planner es un LLM: `planner.py:171-179` construye
`Layer(kind=…, engine=…, params=dict(...))` directamente de su JSON **sin esquema**, y
`executor.py:184` hace splat de esos `params` dentro del job del motor. El nombre de motor
ya se validaba contra el mapa registrado, pero los `params` no — y `UpscalerEngine` **sí**
está registrado (`app.py:493`). Una capa como
`{"engine":"upscaler","params":{"input_path":"/etc/shadow","output_path":"/root/x"}}`
llegaba al `Path(job[...])` pelado de `upscaler.py:_parse`, dando lectura arbitraria y
creación arbitraria de ficheros — incluido dentro del `/opt/ComfyUI` inmutable.

Corregido en el **único punto de paso** por el que pasan todos los motores:

- `_confine_job_paths()` normaliza cada clave de ruta del job (`input_path`, `output_path`,
  `audio_path`, `video_path`, `path` y `clips[].path`) al directorio de datos de Studio. Una
  ruta relativa se ancla ahí; una absoluta o con `..` que se salga **conserva solo el nombre
  final**, de modo que el motor nunca escribe fuera del volumen y el fallo es visible en vez
  de silencioso.
- `EngineRunnerAdapter` / `EngineAssembler` aceptan `data_root`; `app.py` pasa
  `cfg.studio_data_dir`, así que el ancla es el volumen real y no el CWD del proceso.
- `EngineAssembler.assemble` ancló su `output_path` del reel (antes el relativo al CWD
  `projects/default/outputs/…`), que rompía en silencio el contrato "todo bajo el volumen".
- `/studio/api/view` rechaza `filename`/`subfolder` con separadores o `..` en lugar de
  reenviarlos tal cual al `/view` de ComfyUI.

#### Redacción — MEDIO

- `SecretStreamRedactor` tenía un patrón de retención en vuelo **más estricto** que
  `_URL_USERINFO_RE`: una contraseña de userinfo con `/` partida entre chunks se filtraba
  completa. Alineados; verificado que el redactor de streaming y el de una sola pasada
  ahora coinciden.
- `STUDIO_HEAVY_VIDEO_KEY` es una credencial real de proveedor que **ningún** literal y
  **ningún** regex de asignación cubría, así que un proveedor que la repetía en su cuerpo de
  error la devolvía tal cual al cliente y al log. Añadida a ambos y a `from_env`/`from_config`.
- `engines/{upscaler,ffmpeg,heavy_video}.py` construían un `SecretRedactor()` **pelado**, sin
  ningún secreto literal: `COMFY_PASS`, `HF_TOKEN` y `STUDIO_API_TOKEN` no se enmascaraban en
  las cadenas de error de los motores. Ahora usan `SecretRedactor.from_env()`, igual que ya
  reciben `ComfyUIClient` y `StudioAgent`.
- `install_log_redaction` se llamaba **dos veces contra el mismo logger** (`comfy_studio`):
  una por el valor por defecto y otra explícita, así que el logger raíz nunca llevaba el
  filtro. `Logger.callHandlers` ignora los filtros de un logger *ancestro* para los registros
  propagados, de modo que `getLogger("comfy_studio.x").warning(...)` llegaba al handler raíz
  (y a `/var/log/studio-server.log`) sin redactar. Reproducido y corregido.
- `ml/runner.py` hacía `redactor or SecretRedactor()`: el respaldo era un redactor **sin
  secretos literales**. Ahora `SecretRedactor.from_env()`.
- Portados desde `comfyctl/src/redact.rs` los patrones que al Python le faltaban:
  `Authorization` con todos los esquemas (no solo `Bearer`), credenciales en URLs de esquema
  genérico (`postgres://`, `ftp://`, …), la forma sin esquema `user:pass@host`, y las claves
  por forma `AKIA|ASIA`, `AIza…`, `ghp_…`, `xox[baprs]-…`.

### 5.3 El checker de paridad de perfiles no veía la guarda de igualdad (commit `95babbf`)

`scripts/check_profile_parity.py` solo entendía la forma
`[[ "${MODEL_PROFILE}" =~ ^(a|b)$ ]]`. La guarda que envuelve la descarga de
FLUX.1-dev es `if [ "${MODEL_PROFILE}" = "all" ]; then` (`download-models.sh:561`),
así que para el checker esa descarga aparecía **sin guarda** y la ensanchaba a
«todos salvo minimal». Casualidad peligrosa: eso coincidía exactamente con la
declaración de 6 perfiles que `config/models-core.json` tenía para
`flux1-dev-fp8.safetensors`, así que **un manifiesto incorrecto y una lectura
incorrecta se validaban entre sí** y el checker daba OK.

Corregido por las dos caras:

- el checker entiende ahora la forma de igualdad simple. Las guardas compuestas
  (`!=`, `&&`, `||`) se dejan **sin parsear** en vez de admitir una conjetura:
  las únicas de igualdad dentro de `run_phase1`/`run_phase2` son el retorno
  temprano de `minimal` (sin `hf_fetch` dentro, sin efecto) y la de FLUX.
- `config/models-core.json` baja `flux1-dev-fp8.safetensors` de 6 perfiles a
  `["all"]`, coincidiendo con `download-models.sh:561` y con
  `comfyctl/src/collect/models.rs:191-196` (`&["all"]`).

Verificado: el checker corregido **reporta la divergencia antes** del cambio de
manifiesto y pasa después; la paridad O-11 entre `download-models.sh` y
`EXPECTED_MODELS` sigue en pie (26/26); el diff del manifiesto es de 1 inserción
y 6 eliminaciones.

### 5.4 Tercera pasada: revisión holística de la rama (commit `0bead88`)

Una revisión adversaria centrada en **la coherencia de la propia rama** (no del repo)
encontró tres problemas autoinfligidos, todos en código que esta rama añadió:

#### ALTO — el redactor de streaming no retenía 7 de los patrones nuevos

La rama había corregido exactamente esta clase de bug para `_URL_USERINFO_RE`, y luego
añadió 7 patrones más para paridad con Rust **sin contrapartida en vuelo**. El buffer
solo retiene lo que coincide con `_INFLIGHT_STREAM_REGEXES`, así que un credencial
partida entre dos chunks de un stream SSE se emitía en claro. Reproducido:
`push("xoxb-1234") + push("567890-abcdefghij")` devolvía el token completo.
Añadidas las entradas ancladas `^…$` correspondientes, de modo que `redact()` y el
stream no puedan volver a divergir.

#### MEDIO — la exención de `mailto:`/`sip:` miraba el grupo equivocado

La comprobación usaba `m.group(0)`, que incluye el delimitador inicial obligatorio
(espacio/comilla/`(`/`[`/`{`/`=`/`,`), así que `startswith("mailto:")` solo era cierto
en la posición 0: **todo** `mailto:user@example.com` incrustado en un valor JSON perdía
su parte local. Ahora se compara `group(2)` contra la misma lista de 9 esquemas que
usa Rust.

#### MEDIO — el confinamiento estaba en 1 de los 3 puntos de entrada de motor

`QaShotEvaluator.evaluate` y `EngineAssembler.assemble` llamaban a `_run_engine_sync`
directamente, así que el `video_path` del qa y los `clips`/`output_path` del editor
quedaban sin confinar. Y `shot.output_path` se restaura **tal cual** del checkpoint de
SQLite, así que no está garantizado que venga de un `run_layer` confinado. Ambos
confina ahora.

Además, confinar solo al dir de Studio era incorrecto: en el pod `comfyui_input_dir` es
`COMFY_DATA_DIR/input` y ComfyUI escribe en `COMFY_DATA_DIR/output`, que son
**hermanos** de `COMFY_DATA_DIR/studio`. Una referencia correcta como
`/workspace/comfyui-data/input/x.png` colapsaba en silencio a `<studio>/x.png`, que no
existe. `_allowed_roots()` admite ahora el volumen entero (el contrato real es «todo
bajo el volumen») y `_confine_one` usa `realpath` para que los prefijos con enlaces
simbólicos comparen de forma coherente.

5 tests de regresión nuevos: retención en vuelo por patrón, `mailto:` incrustado,
preservación de rutas hermanas legítimas, confinamiento del qa sobre una ruta
restaurada del checkpoint, y confinamiento de `clips` del assembler.

### 5.5 Cuarta pasada: smoke local que replica el arranque de CI (commit `def86d0`)

Un smoke local que reproduce el orden de arranque real de `server.py`
(`create_app` → `uvicorn.run`) destapó que la corrección del commit `093eb48`
**no funcionaba**.

uvicorn **no** configura handlers en el logger raíz. Cuando un registro nace en
`comfy_studio.x`, `Logger.callHandlers` sube por la jerarquía, no encuentra ningún
handler y cae en `logging.lastResort` — un `_StderrHandler` a nivel de módulo. Los
filtros de un logger *ancestro* nunca se consultan para un registro originado en un
hijo, así que el secreto llegaba en claro a `/var/log/studio-server.log`:

```
BEFORE (no lastResort) -> 'boot con pass=SUPERSECRETPASS'  LEAKS=True
AFTER  (with lastResort) -> 'boot con pass=[REDACTED]'     LEAKS=False
```

Dos cambios:

- `install_log_redaction()` filtra también `logging.lastResort`, de forma idempotente.
  Es el destino real de los registros de `comfy_studio.*` en el arranque con uvicorn, y
  al ser un singleton de proceso basta filtrarlo una vez.
- El lifespan **reinstala** la redacción cuando uvicorn ya está corriendo, de modo que
  los handlers que uvicorn añade entre `create_app` y el serve también quedan cubiertos
  (en `create_app` la raíz aún no tenía ninguno).

El test de regresión modela el orden real y vacía los handlers de la raíz: si no, el
plugin de logging de pytest añade el suyo y el camino por `lastResort` no se ejercita.

Además se verificó con un smoke local que replica los asserts de CI sobre el
studio-server: 401 sin auth, 200 con auth, JSON válido en `/studio/api/health`,
`/status`, `/templates` y `/projects`, y ningún secreto en ningún cuerpo.

### 5.6 Quinta pasada: el contrato de volumen verificado en los dos sentidos (commit `61ec41b`)

Los tests de confinamiento anteriores solo afirmaban que las rutas hostiles caen
dentro del directorio de datos. Eso deja un agujero: un cambio que **colapsara una
ruta legítima del volumen a su basename** seguiría pasándolos, que es exactamente
el bug que corrigió el refactor multi-raíz.

Verificado contra el layout real del pod (`data_root=/workspace/comfyui-data/studio`,
raíces permitidas `[studio, input, /workspace/comfyui-data]`):

```
PRESERVED  /workspace/comfyui-data/models/checkpoints/flux1-dev-fp8.safetensors
PRESERVED  /workspace/comfyui-data/models/loras/x.safetensors
PRESERVED  /workspace/comfyui-data/input/x.png
PRESERVED  /workspace/comfyui-data/output/y.mp4
PRESERVED  /workspace/comfyui-data/studio/projects/p/assets/a.png
BLOCKED    /opt/ComfyUI/custom_nodes/Foo/x.py     (dir de app inmutable)
BLOCKED    /etc/nginx/.htpasswd
BLOCKED    /etc/shadow
```

Ahora hay un test que fija **las dos direcciones**, para que el contrato «todo bajo
el volumen» no se pueda romper ni por exceso (bloquear lo legítimo) ni por defecto
(dejar pasar lo que no pertenece al volumen).

#### Asimetría importante entre los dos redactores

El redactor de Python corre sobre el **cuerpo JSON serializado**, que el cliente vuelve a
parsear; el de Rust solo corre sobre texto de log. Por eso las clases de caracteres del de
Python **deben** excluir los delimitadores de JSON, mientras que en Rust `[^\s]*` es válido.
El primer intento de esta paridad copió las clases de Rust tal cual y rompió todos los cuerpos
de `/studio/api/health` con `Extra data` (7 fallos de test, capturados por la suite). Ambos
patrones nuevos de URL son ahora JSON-safe, y hay un test que fija el invariante.

---

## 6. Documentación corregida

| Archivo | Corrección |
|---|---|
| `SESSION_REPORT_2026-11-09.md` §4.1 | Afirmaba que los secretos se redactaban del banner de arranque. **Ese cambio se revirtió**: el smoke test de CI exige que el `STUDIO_API_TOKEN` autogenerado sea visible exactamente una vez, y los ficheros `/run/comfyui-*` los consume `comfyctl` (no sirven al operario). Sección reescrita con el estado real y el motivo. |
| `AGENTS.md` | Excepción documentada a *"NEVER print COMFY_PASS / STUDIO_API_TOKEN"*; checks estrictos son S1..S17, no S1..S16. |
| `README.md` | `STUDIO_STUB_COMFY` era fantasma (el real es `STUDIO_STUB_COMFYUI`); `COMFY_VOLUME_WAIT_*` son 60/1, no 12/5; catálogo de motores 16 → **21**; la enumeración de herramientas iba 9 nombres corta respecto a las (correctas) 54. |
| `GUIA_PASO_A_PRODUCCION.md` | Hash `002b63e` → `c2c2971`; `S1–S16` → `S1–S17`. |
| `decisiones.md` | Fila MED-02 (RTX 5090) marcada como histórica. |

---

## 7. Resultados de verificación

| Comprobación | Resultado |
|---|---|
| `ruff check` (`studio/comfy_studio`, `studio/tests`, `scripts`, `config/probes`, `panel`) | **PASS** |
| `py_compile` | **PASS** |
| `bash -n` (`start.sh`, `download-models.sh`, `tools/mac/comfy`) | **PASS** |
| `pytest studio/tests` (desde la raíz del repo) | **2522 passed, 4 skipped, 0 failed** (4 errores, ver abajo) |
| — baseline `origin/main` | **8 failed** (`test_health_auth` ×7, `test_m6_production_wiring` ×1). *Nota:* los 7 de `test_health_auth` solo se reproducen con un intérprete cuya ruta contiene `@` (Python de Homebrew fuera de venv); en un entorno con venv `main` muestra solo el de `test_m6_production_wiring`. |
| `nginx.conf` balance de llaves / cobertura HSTS+CSP | 0 desbalance / **17/17 locations** |
| `cargo fmt --check` / `clippy -D warnings` / `test --locked` | **PASS** (88 tests) — ejecutado por el agente de revisión; **no reproducible en este host** (sin `cargo`) |
| Docker build + smoke tests | ✅ **SUCCESS** en la ejecución `42707644366`, cuyo `headSha` es `552b4c4` — es decir, **el HEAD final de la rama**. Los tres jobs en verde sobre el estado definitivo (también había pasado antes en `42694751058`). |
| Dry-run de resolución de manifiestos | **PASS** — ver §3.1 (original BROKEN / fixed RESOLVES OK) |

Los 4 errores de `test_integration_startup_live.py` requieren un contenedor vivo
(nginx + status-server + upstream reales) y **también fallan en `origin/main`**: son
preexistentes y de entorno, no regresiones.

---

## 8. Riesgos residuales

| Riesgo | Nivel | Mitigación / nota |
|---|---|---|
| Secreto autogenerado impreso una vez en `docker logs` | BAJO | Exigido por el smoke test. Definir `COMFY_PASS`/`STUDIO_API_TOKEN` → el banner muestra `[OCULTO]`. `ttyd` ya no los expone vía `env`. |
| CSP con `'unsafe-inline'` en `/home` y `/status` | MEDIO | Necesario porque ambas vistas incrustan `<style>`/`<script>`. Sigue bloqueando scripts/objetos externos, `frame-ancestors`, `base-uri` y `form-action`. **Recomendado:** externalizar el script del portal y usar nonce/hash. |
| HSTS inerte sin TLS | BAJO | RFC 6797 §7.2: los navegadores ignoran HSTS sobre transporte no seguro. Solo surte efecto con `COMFY_TLS=1`. |
| `routers/productions.py:111` hace `await run_in_threadpool(service.approve_plan, …)` | MEDIO | La petición HTTP (y un worker de anyio) queda retenida durante toda la producción, contradiciendo la intención de "job en fondo sin 504 de Nginx". **No corregido**: arreglarlo cambia el contrato de la API (202 + job-id). Requiere decisión de producto. |
| Cambios Rust sin compilar localmente | BAJO | `cargo` no está disponible en este host. Los edits son mecánicos (snapshot, comentarios, `writeln!`), verificados por lectura; la CI los compila. |
| Fixtures `nvidia_smi_*.csv` huérfanos | INFO | Ningún test hace `include_str!` sobre ellos. Re-brandados por consistencia. |
| `planner.py` sigue aceptando `params` sin esquema por motor | MEDIO | Las **rutas** ya están confinadas en los tres puntos de entrada de motor, así que no hay lectura/escritura fuera del volumen. Un `params` con claves inventadas sigue llegando al motor, que hoy las ignora. **Recomendado:** allowlist de claves por motor. |
| `engines/{ffmpeg,heavy_video}.py` con rutas sin confinar | BAJO | No reachable: `app.py` solo registra `comfyui` y `upscaler`. El confinamiento ya cubre sus claves de ruta si se registran. |
| `_URL_AUTH_RE` mangles URLs con puerto y una `@` posterior | BAJO | `https://cdn.example.com:443/img@2x.png` → `…:[REDACTED]@2x.png`. Inherente a la forma del patrón (el de Rust es peor) y **no afecta a ninguna respuesta actual**. Arreglarlo exigiría excluir `/` de la clave, que reabriría la fuga de contraseñas con `/`. |

### 8.1 Cerrado en esta pasada (no son riesgos residuales)

| Hallazgo | Estado |
|---|---|
| `comfyctl/src/supervisor.rs:511` acepta cualquier listener en el puerto de studio-server | **Corregido**: tras conectar exige además que el hijo siga vivo. |
| `comfyctl/src/supervisor.rs:135-136` `keep_bytes` fijo en 20 MiB | **Corregido**: `min(20 MiB, max_bytes)`. |
| `comfyctl/src/report.rs:437` usa el `MODEL_PROFILE` crudo para la etiqueta | **Corregido**: reutiliza `valid_profile_name()`. |
| `config/models-core.json` (flux) declaraba perfiles más amplios que `models.rs` | **Corregido**: el checker ahora entiende la guarda de igualdad simple y el manifiesto dice `["all"]`. |
| Redactor Python es subconjunto del de Rust | **Corregido**: portados `Authorization`, URL de esquema genérico, `user:pass@host` sin esquema y las claves por forma AKIA/AIza/ghp_/xox. |
| `install_log_redaction` engancha el filtro solo al logger `comfy_studio` | **Corregido**: raíz + `lastResort` + reinstalado en el lifespan (ver §5.5). |
| `ml/runner.py` acepta `redactor=None` en 5 de 7 llamadas | **Corregido**: el respaldo por defecto es `from_env()`, no un redactor pelado. |

---

## 9. Recomendación

### ✅ APTO PARA PRODUCCIÓN

Mergeado en `main` (`9609e5a`) y con la CI completa en verde sobre el HEAD final
(ejecución `42707644366`: los tres jobs, incluido el build de imagen y toda la
suite de smoke). La imagen quedó publicada en
`ghcr.io/tomas-estrada/comfyuipod:latest` (ejecución `42715693828`).

**Los contratos núcleo del proyecto quedan intactos**: separación `/opt` vs
`/workspace`, Basic Auth en el borde, seguridad del arranque estricto y
compatibilidad con el perfil de hardware documentado.

### Hecho después del merge (sobre `main`)

| Commit | Qué |
|---|---|
| `1d00bec` | **CSP sin `'unsafe-inline'`**: `/home` y `/status` usan el hash sha256 de sus bloques inline. `'unsafe-inline'` eliminado de todas las CSP. Guardado por tres vías: `scripts/update_csp_hashes.py --check` en CI, dos tests nuevos, y el propio script. |
| `2311def` | **Corregida una afirmación falsa**: dos comentarios decían que `run_in_threadpool` evitaba el 504 de Nginx. No es así; lo que lo evita es `proxy_read_timeout 3600s`. Documentado el límite real (1 h) y por qué **no** se convirtió `approve` en 202 + job-id. |
| `64a25a2` | **Node 22 LTS** en la imagen (Node 20 estaba en EOL desde abril de 2026) y **corregido un flag de CI que no hacía nada**: el workflow ponía `STUDIO_STUB_COMFY`, que ningún código lee; el real es `STUDIO_STUB_COMFYUI`. |

### Pendiente, con procedimiento escrito

| Migración | Documento | Riesgo | Por qué no está aplicada |
|---|---|---|---|
| CUDA 13.0 + PyTorch 2.15 | [`upgrades/CUDA13_PYTORCH_2.15.md`](../upgrades/CUDA13_PYTORCH_2.15.md) | ALTO | Requiere Docker + GPU. Un fallo solo se manifiesta con `sm_120` presente. |
| ComfyUI core v0.25.x | [`upgrades/COMFYUI_V0.25.md`](../upgrades/COMFYUI_V0.25.md) | MEDIO-ALTO | Requiere build + warm-up + smoke E2E con GPU. |

Ambos documentos enumeran la **superficie completa de pines** (9 puntos para
PyTorch, repartidos entre `constraints.txt`, el `Dockerfile`, los cuatro
ficheros de constraints de custom nodes, `triage_patterns.json` y el snapshot de
tests), el procedimiento de validación y el rollback. Las ramas
`upgrade/cuda-13-torch-2.15` y `upgrade/comfyui-v0.25` siguen vacías a propósito:
son el lugar donde aplicar el procedimiento, no un trabajo a medias.

**Node 22 sí está hecho**: la rama `upgrade/nodejs-22` quedó obsoleta porque el
cambio entró directamente en `main`.

### Decisión de producto pendiente

`POST /studio/api/productions/{id}/approve` es sincrónico a propósito: dos tests
E2E se apoyan en ese contrato para verificar la canalización entera, incluido
que el MP4 final existe y es válido. Convertirlo en 202 + job-id liberaría la
conexión HTTP, pero rompería esa garantía. El análisis completo está en la nota
de gobernanza de `studio/comfy_studio/routers/productions.py`.

---

*Informe de la pasada de verificación original: §1-§8 de este documento.*
