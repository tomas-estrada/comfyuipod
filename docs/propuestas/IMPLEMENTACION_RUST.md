# Propuesta: `comfyctl` en Rust, arranque estricto y `comfy` para la Mac

| Campo | Valor |
|---|---|
| Fecha | 2026-10-16 |
| Estado | **Propuesta. No implementar hasta que el dueño decida D6 (§12) y se cumplan los prerrequisitos de §10.0.** |
| Punto de partida | Commit `f0fd82d` auditado en [`AUDITORIA_AGENTE_V6.md`](../auditorias/AUDITORIA_AGENTE_V6.md). La implementación Python/Bash de [`OBSERVABILIDAD_Y_PORTAL.md`](OBSERVABILIDAD_Y_PORTAL.md) es la **especificación de comportamiento** de todo lo que aquí se propone reescribir. |
| Destinatario | Agente ejecutor. El auditor revisa; no ejecuta. |
| Convenciones | [Docs] = documentación oficial consultada; [Conocimiento] = conocimiento general, verificar en la primera ejecución; [Repo] = respaldado por el código del repositorio. |

---

## 0. Resumen ejecutivo

**Qué se propone.** Un binario estático `comfyctl` (Rust, `x86_64-unknown-linux-musl`, compilado en CI dentro del propio `Dockerfile`) que sustituya a tres piezas de `f0fd82d`: el bucle supervisor de `start.sh`, `scripts/status-server.py` y la orquestación de `scripts/diag.sh`. Añade una capacidad que hoy no existe: **arranque estricto** (`STRICT_START`), que detecta en los primeros 60–120 s si la instancia alquilada no sirve (sin CUDA, driver viejo, VRAM o disco insuficientes) y lo hace visible de forma inequívoca para que el usuario la destruya en lugar de seguir pagando. Opcionalmente, en una fase posterior, un `comfy` para la Mac también en Rust.

**Qué no cambia.** Portal `/home` (HTML estático), Nginx, `download-models.sh`, `triage_patterns.json`, el esquema de `/status/api`, `/run/comfyui-supervisor.json`, las secciones de `/report.md`, el `Dockerfile` de ComfyUI y todas las decisiones de las rondas V1–V6.

**Por qué ahora no.** La implementación Python funciona en CI sin GPU y todavía no se ha ejecutado ni un workflow ni una sesión real. Reescribir antes de tener logs reales es optimizar sin datos. Prerrequisito: V13–V20 de la auditoría V6 completadas.

**Recomendación.** Opción **B (híbrida)** de §12: `comfyctl` en Rust para supervisor, servidor de estado, informe, diagnóstico y arranque estricto; Bash solo como "pegamento" de 60 líneas; `comfy` de la Mac en Bash hasta que haya una razón concreta para migrarlo.

---

## 1. Qué problema resuelve Rust aquí y qué no

### 1.1 Lo que sí aporta

| Problema real observado | Cómo lo resuelve `comfyctl` |
|---|---|
| El diagnóstico depende del mismo intérprete Python que se intenta diagnosticar. Si `pip` pisa `torch`, `numpy` u `onnxruntime` (hallazgos V2, patrones `sm120`/`import-failed`), `status-server.py` sigue vivo porque solo usa stdlib, pero `diag.sh` y `write_supervisor_state` ya no son fiables. | Binario estático sin dependencia de `/usr/bin/python3` ni de `libpython`. Diagnostica aunque el entorno Python esté roto. |
| O-02, O-03, O-04 de V6: política de reinicios, latencia de `/report.md`, lectura completa de logs cada pocos segundos. | Hilos nativos, `seek` desde el final, caché con `RwLock`, tipos para la máquina de estados del supervisor. Son problemas que en Python se resuelven con cuidado y en Rust con el diseño por defecto. |
| El supervisor es Bash: `wait`, `trap`, `setsid`, códigos de salida y JSON escritos vía `python3 -`. Difícil de probar en CI sin levantar un contenedor. | `comfyctl supervise` es un proceso con `waitpid`, señales y máquina de estados testeable con `cargo test` (procesos simulados que salen con 137/143/1). |
| No existe "fail-fast": una instancia sin CUDA arranca igual, muestra ComfyUI en modo CPU y factura. | `comfyctl strict-check` (§6) con política explícita, salida ≠ 0 y marca visible en el portal y en `comfy`. |
| Una sola fuente de verdad para patrones, esquema JSON e informe. | `serde` + tipos compartidos entre `serve`, `report` y los tests; `schemars` opcional para publicar `status-api.schema.json`. |

### 1.2 Lo que no aporta (para no venderlo mal)

- **No evita los fallos de entorno que han ocurrido hasta ahora.** Esos fallos (`sm_120`, `IMPORT FAILED`, `HostBuffer`, hf-xet colgado, OOM) están en PyTorch, ComfyUI, nodos y descargas. `comfyctl` los detecta y explica mejor; no los previene.
- **La compilación ocurre en CI, no en la instancia.** "Si compila, sigue facturando; si no, cancelo" no aplica: el binario llega ya compilado dentro de la imagen. La propiedad que se busca es la de §6 (fail-fast en arranque), que es independiente del lenguaje; Rust solo la hace más fiable.
- **Coste.** Multi-stage build (+3–6 min la primera vez, cacheado después), un `Cargo.lock` que auditar, un conjunto de crates (§4.3) que entra en la cadena de suministro, y tiempo de desarrollo estimado en 1.3–1.5× el de la versión Python.

---

## 2. Alcance

### 2.1 Dentro

1. `comfyctl` (crate único, binario único) con subcomandos `strict-check`, `supervise`, `serve`, `report`, `diag`, `version`.
2. `start.sh` reducido a: variables, `htpasswd`, Nginx, `exec comfyctl supervise` (que a su vez lanza `serve`, `diag` y las fases de descarga).
3. Política `STRICT_START`.
4. Cambios en `Dockerfile` (stage `builder`) y en el workflow (job `rust`, build-arg, smoke test ampliado).
5. Documentación: README §3 (variables nuevas), `decisiones.md` (D6 y política de arranque estricto), `walkthrough.md`.

### 2.2 Fuera (se mantiene como está)

- `config/portal/index.html`, `config/_starting.html`, `config/nginx.conf` (salvo `proxy_read_timeout` de `/report.md` si O-03 no se aplicó antes).
- `scripts/download-models.sh`: sigue en Bash porque orquesta el CLI `hf` y su valor está en los reintentos y el `STAGE_DIR`, ya auditados. `comfyctl` lo lanza y lo observa, no lo reemplaza.
- `scripts/triage_patterns.json`: mismo fichero, misma semántica; lo lee `comfyctl`.
- Los *probes* Python de `diag.sh` (matmul, ORT, imports) **siguen siendo Python**: deben ejecutarse con el intérprete real para que la prueba tenga sentido. Pasan a `/etc/comfyui/probes/*.py` y `comfyctl diag` los ejecuta con timeout y clasifica su salida. Las pruebas no Python (`nvidia-smi`, `ffmpeg`, `nginx -t`, disco, RAM, `ldconfig`, env) se hacen en Rust.
- `tools/mac/comfy`: Bash hasta la Fase 3 opcional (§10.3).

---

## 3. Arquitectura de procesos (objetivo)

```
tini(1)
└── /start.sh                     # ~60 líneas: env, htpasswd, nginx -t && nginx
    └── exec comfyctl supervise   # PID estable, trap TERM/INT, reaper de hijos
        ├── [hilo] HTTP 127.0.0.1:18189   (= comfyctl serve, mismo proceso)
        ├── [hilo] colector: GPU/mem/disco/logs cada 3 s; secciones pesadas cada 60 s
        ├── [hilo] rotación de logs (comfyui.log, access.log) por tamaño
        ├── comfyctl strict-check (fase de arranque, §6)             → fatal ⇒ modo "fatal"
        ├── comfyctl diag → /var/log/diag.txt                        (no bloqueante, 90 s)
        ├── download-models.sh --phase1 (timeout 600, no fatal)
        ├── download-models.sh --phase2 (setsid, background)
        ├── ttyd (opcional, ENABLE_WEB_TERMINAL=1)
        └── python3 main.py --listen 127.0.0.1 --port 18188 … (supervisado)
```

Diferencias con `f0fd82d`:
- Un solo proceso supervisor (hoy: `start.sh` + `bash -c` para status-server + `python3` para el JSON). `status-server.py` desaparece; `serve` vive como hilo del supervisor. Si `serve` falla, el supervisor lo reinicia internamente (misma política de 10 fallos/60 s).
- `comfyctl supervise --standalone-http` permite ejecutar solo el servidor (útil en desarrollo y en tests).
- `exec` desde `start.sh`: `comfyctl` es hijo directo de `tini`; recibe `SIGTERM` sin intermediarios. Sigue imprimiendo a stdout (para `docker logs`) y a `/var/log/start.log`.

---

## 4. Diseño del crate `comfyctl`

### 4.1 Estructura

```
comfyctl/
├── Cargo.toml
├── Cargo.lock                 # versionado, obligatorio
├── rust-toolchain.toml        # channel fijado (p. ej. "1.86.0"), components = ["clippy","rustfmt"]
├── deny.toml                  # cargo-deny: licencias, advisories, sources
├── clippy.toml
├── src/
│   ├── main.rs                # parseo de argumentos (sin clap: pico-args), dispatch
│   ├── config.rs              # Env → Config tipada (COMFY_*, STRICT_*, rutas)
│   ├── paths.rs               # constantes de rutas (/var/log/…, /run/…, /etc/comfyui/…)
│   ├── supervisor/
│   │   ├── mod.rs             # máquina de estados: Starting → Running → Down → Running | Exhausted | Fatal | ShuttingDown
│   │   ├── child.rs           # spawn, waitpid, clasificación de rc, kill escalonado
│   │   ├── state_file.rs      # /run/comfyui-supervisor.json (escritura atómica)
│   │   └── policy.rs          # límite de reinicios, reset tras uptime sano, rc=143 no cuenta (O-02)
│   ├── http/
│   │   ├── mod.rs             # servidor (tiny_http), router, 404/405/400, HEAD
│   │   ├── status_api.rs      # /status/api
│   │   ├── status_html.rs     # /status (HTML embebido con include_str!)
│   │   ├── report.rs          # /report.md
│   │   └── logs.rs            # /logs/<nombre>?n=
│   ├── collect/
│   │   ├── gpu.rs             # nvidia-smi --query-gpu / --query-compute-apps
│   │   ├── mem_disk.rs        # /proc/meminfo, statvfs
│   │   ├── procs.rs           # /proc/*/stat,status,cmdline (sin `ps`)
│   │   ├── phases.rs          # parseo de download-models.log (PLAN/INICIO/OK/SKIP/TIMEOUT/ERROR)
│   │   ├── drift.rs           # pip freeze vs requirements.lock, git rev-parse vs nodes.lock
│   │   └── models.rs          # inventario y ausentes por perfil (manifiesto único, O-11)
│   ├── triage.rs              # carga triage_patterns.json, evalúa fuentes, ordena por severidad
│   ├── logtail.rs             # tail eficiente por bloques desde el final (O-04)
│   ├── redact.rs              # redacción de COMFY_PASS / HF_TOKEN en todas las salidas
│   ├── strict.rs              # §6: comprobaciones fatales
│   ├── diag.rs                # §7: orquestación de probes
│   └── sys.rs                 # único módulo con `unsafe`/libc: setsid, kill, waitpid, statvfs
├── assets/
│   ├── status.html            # mismo HTML de status-server.py (include_str!)
│   └── probes/*.py            # copiados a /etc/comfyui/probes/ en build
└── tests/
    ├── fixtures/              # logs reales anonimizados: comfyui_oom.log, download_timeout.log, access_503.log, supervisor_137.log, diag_cpu.txt
    ├── triage_test.rs         # cada patrón dispara con su fixture y no con los demás
    ├── phases_test.rs         # PLAN/INICIO/OK → done/total/current_file
    ├── report_test.rs         # snapshot de /report.md con fixtures (secciones y orden)
    ├── supervisor_test.rs     # hijo simulado (`sh -c 'exit 137'`, `kill -TERM $$`) → transiciones y JSON
    ├── http_test.rs           # 401 no aplica (es Nginx); 404 fuera de lista; 405 POST; HEAD sin cuerpo
    └── redact_test.rs         # secreto en log/JSON/Markdown → `[REDACTED]`
```

### 4.2 Contratos que se conservan (pruebas de paridad)

| Contrato | Fuente de verdad | Prueba |
|---|---|---|
| JSON de `/status/api` | `status-server.py:build_status_api` de `f0fd82d` → congelar como `docs/contratos/status-api.schema.json` **antes** de empezar (campos y tipos; se permiten campos nuevos, nunca renombrar ni quitar). | Test que valida la salida de `comfyctl` contra el esquema; el portal y `comfy watch` no cambian. |
| `/run/comfyui-supervisor.json` | `start.sh:write_supervisor_state`. Campos nuevos aditivos: `healthy_resets`, `fatal` (objeto o `null`). | Test de snapshot. |
| Secciones de `/report.md` | `## 0. Triage automático` … `## 9. Modelos`. Se añade `## 10. Arranque estricto` solo cuando `STRICT_START` produjo WARN o FATAL. | Test de snapshot; el smoke test de CI ya hace `grep` de `## 0.` y `## 8.`. |
| `triage_patterns.json` | Mismo fichero y claves. | `tests/triage_test.rs`. |
| Rutas de logs y lista blanca de `/logs/` | `LOG_WHITELIST` de `status-server.py`. | Test. |
| Formato de líneas de `diag` | `[DIAG][OK|WARN|FAIL] clave: detalle` y `[DIAG] resumen: n ok, n warn, n fail`. | Test; los patrones `nvenc-missing`/`ort-cpu` dependen de ello. |

### 4.3 Dependencias (mínimas y justificadas)

| Crate | Para qué | Alternativa descartada |
|---|---|---|
| `serde`, `serde_json` | JSON de estado, patrones, `image.json`. | Escribir JSON a mano: frágil. |
| `regex` | Patrones de triage (sintaxis compatible con los 17 actuales; verificar que no usan *lookaround*, que `regex` no soporta: hoy no lo hacen [Repo]). | `fancy-regex` solo si algún patrón futuro lo exige. |
| `tiny_http` | Servidor HTTP mínimo con hilos, `HEAD`, sin async. | `hyper`/`axum`: arrastran `tokio`, +100 crates. Servidor manual sobre `std::net`: parser HTTP propio, riesgo innecesario. |
| `libc` | `setsid`, `kill`, `waitpid`, `statvfs`, `sysconf(_SC_CLK_TCK)`. Aislado en `sys.rs`. | `nix`: más cómodo pero más superficie. Elegir uno; no ambos. |
| `pico-args` | Argumentos de línea de comandos (6 subcomandos, pocas opciones). | `clap`: excelente, pero 20+ crates para 6 subcomandos. |
| `time` o `jiff` (uno) | ISO-8601 UTC en informes (O-09a). | `chrono` también vale; elegir uno y fijarlo. |
| *(dev)* `insta` | Snapshots de informe y JSON. | Comparación manual de cadenas. |
| *(opcional)* `schemars` | Generar `status-api.schema.json` desde los tipos. | Mantener el esquema a mano. |

Objetivo: **≤ 40 crates transitivos** en `cargo tree`, todos con licencia MIT/Apache-2.0/BSD/ISC/Zlib/Unicode (lista cerrada en `deny.toml`). Si una dependencia supera ese presupuesto, se justifica en `decisiones.md` o no entra.

### 4.4 Reglas de calidad (no negociables, verificadas en CI)

```toml
# Cargo.toml
[lints.rust]
unsafe_code = "deny"            # solo sys.rs lo permite con #[allow] local y comentario
missing_docs = "warn"
[lints.clippy]
all = "deny"
pedantic = "warn"
unwrap_used = "deny"
expect_used = "deny"
panic = "deny"
indexing_slicing = "deny"
unwrap_in_result = "deny"
[profile.release]
opt-level = "z"
lto = true
codegen-units = 1
panic = "abort"
strip = true
```

- `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`, `cargo deny check` en el job `validate` (nuevo paso `rust`). Cualquier fallo bloquea el build de la imagen.
- `rust-toolchain.toml` fija canal y versión exacta; `Cargo.lock` versionado; `cargo build --locked` en el `Dockerfile`.
- Todo error es `Result<_, Error>` con un `enum Error` por módulo; `main` traduce a código de salida (§6.4) y mensaje `[FATAL]`/`[ERROR]` con el mismo prefijo que hoy.
- Ningún comando externo recibe cadenas construidas desde parámetros HTTP (misma regla que §8.3.3 de la propuesta original). El router solo acepta rutas de una lista cerrada; `n` se parsea a `u16` con cota 2000.
- El binario **nunca se descarga**: se compila en el stage `builder` del `Dockerfile` con la misma imagen `rust:<versión>-slim-bookworm` fijada por digest.

---

## 5. Subcomandos

| Subcomando | Qué hace | Sustituye a |
|---|---|---|
| `comfyctl version` | Imprime versión del binario, `image.json` y toolchain con que se compiló. | — |
| `comfyctl strict-check [--json]` | Ejecuta las comprobaciones de §6, imprime `[STRICT][OK|WARN|FATAL] clave: detalle`, escribe `/run/comfyui-strict.json`, sale con 0 (OK/WARN) o 3 (FATAL). | Nada (nuevo). |
| `comfyctl diag [--timeout 90]` | Orquesta los 10 checks de §7 (probes Python con timeout + checks nativos), escribe `/var/log/diag.txt`. Siempre sale con 0. | `scripts/diag.sh`. |
| `comfyctl serve [--bind 127.0.0.1:18189]` | Servidor HTTP: `/status`, `/status/api`, `/report.md`, `/diag`, `/logs/<nombre>`. | `scripts/status-server.py`. |
| `comfyctl report [--out FILE]` | Genera `/report.md` a stdout o fichero sin servidor (útil desde `ttyd` o CI). | `curl /report.md`. |
| `comfyctl supervise` | Proceso principal: `strict-check` → `diag` → Fase 1 → Fase 2 → ComfyUI supervisado; hilos `serve`, colector y rotación. Trap TERM/INT. | Pasos 5–7 de `start.sh`. |

Variables de entorno (todas con el mismo nombre que hoy; nuevas marcadas con ✚):

| Variable | Default | Uso |
|---|---|---|
| `COMFY_MAX_RESTARTS` | `5` | Igual. |
| `COMFY_RESTART_DELAY` | `5` | Igual. |
| ✚ `COMFY_HEALTHY_UPTIME` | `600` | Segundos de vida del proceso tras los cuales `restarts` vuelve a 0 (O-02). |
| ✚ `STRICT_START` | `1` | `1` = las comprobaciones FATAL de §6 detienen el arranque; `0` = solo avisan. |
| ✚ `STRICT_MIN_VRAM_MB` | `20000` | Umbral de VRAM. |
| ✚ `STRICT_MIN_DISK_GB` | según perfil (§6.2) | Umbral de disco libre en `/workspace`. |
| ✚ `STRICT_MIN_DRIVER` | `570` | Versión mayor mínima del driver NVIDIA. |
| ✚ `STRICT_REQUIRE_NVENC` | `0` | `1` = la ausencia de `h264_nvenc` es FATAL (por defecto WARN). |
| ✚ `STRICT_FATAL_GRACE` | `600` | Segundos que Nginx y `/status` siguen vivos tras un FATAL antes de salir. |
| ✚ `LOG_MAX_MB` | `200` | Tamaño a partir del cual se truncan `comfyui.log` y `access.log` conservando la cola (O-04). |

---

## 6. Política de arranque estricto (`STRICT_START`)

### 6.1 Objetivo

Que una instancia inservible se declare como tal **antes** de que el usuario invierta tiempo en ella, y que la señal sea imposible de pasar por alto: en `docker logs`, en `/home`, en `/status`, en `comfy` y en el estado de la instancia en Vast.ai.

### 6.2 Comprobaciones

Se ejecutan tras Nginx (para que `/home` ya responda) y antes de la Fase 1. Cada una tiene timeout de 20 s. Si `COMFYUI_EXTRA_ARGS` contiene `--cpu`, las comprobaciones de GPU pasan a WARN automáticamente (modo CPU deliberado, como en el smoke test).

| Clave | Método (sin Python) | OK | WARN | FATAL |
|---|---|---|---|---|
| `gpu.present` | `nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader` | 1+ GPU | — | sin GPU o `nvidia-smi` falla (`NVIDIA_DRIVER_CAPABILITIES`, `--gpus` ausente) |
| `gpu.driver` | `driver_version` ≥ `STRICT_MIN_DRIVER` | ≥ 570 | — | < 570. [Conocimiento] CUDA 12.8 requiere driver ≥ 570.x en Linux; confirmar contra la tabla de compatibilidad de NVIDIA y anotar en `decisiones.md`. |
| `gpu.vram` | `memory.total` ≥ `STRICT_MIN_VRAM_MB` | ≥ 20000 | 16000–19999 | < 16000 |
| `gpu.compute_cap` | `compute_cap` ∈ {8.9, 12.0} (Ada, Blackwell: las dos arquitecturas para las que se compiló la terna cu128 [Repo]) | sí | otra ≥ 7.5 (funcionará, no probado) | < 7.5 |
| `cuda.torch` | Probe Python `probes/cuda.py` (`torch.cuda.is_available()` + matmul 1024²) | True | — | False o excepción (es la única comprobación que usa Python; si Python está roto, el FATAL lo dice explícitamente: "intérprete/torch no utilizable") |
| `disk.workspace` | `statvfs("/workspace")` libre ≥ `STRICT_MIN_DISK_GB` (`all`: 75; `general`/`realistic`/`anime`: 45; `minimal`: 10) | ≥ umbral | ≥ 50 % del umbral | < 50 % del umbral |
| `ram.total` | `/proc/meminfo` MemTotal | ≥ 32 GB | 16–32 GB (riesgo `rc=137` con perfil `all`, README §5.7) | < 16 GB |
| `nvenc` | `ffmpeg -f lavfi -i testsrc=duration=1 -c:v h264_nvenc -f null -` | ok | falla (`libx264` de respaldo) | solo si `STRICT_REQUIRE_NVENC=1` |
| `net.hf` | `HEAD https://huggingface.co` con timeout 10 s (solo si `SKIP_MODEL_DOWNLOAD != 1`) | 2xx/3xx | timeout o 5xx (la Fase 1 fallará; se avisa) | — |
| `ports.loopback` | `18188`, `18189`, `7681` libres antes de arrancar | libres | — | ocupados (instancia reutilizada con procesos zombis) |

### 6.3 Comportamiento ante FATAL (`STRICT_START=1`)

1. Imprime un bloque inconfundible en stdout (llega a `docker logs` y a la consola de Vast.ai):
   ```
   ==================================================================
   [FATAL] ESTA INSTANCIA NO SIRVE PARA ESTA IMAGEN
     gpu.driver : 550.54 < 570 (CUDA 12.8 requiere >= 570)
     disk.workspace : 38 GB libres < 75 GB (perfil all)
   Acción recomendada: DESTRUIR la instancia y alquilar otra que cumpla:
     cuda_max_good>=12.8 disk_space>=120 gpu_ram>=24
   Nginx y /status permanecen activos 600 s para que puedas leer /report.md.
   El contenedor saldrá con código 3.
   ==================================================================
   ```
2. Escribe `/run/comfyui-strict.json` (`{"result":"fatal","checks":[…],"at":…}`) y `fatal` en `/run/comfyui-supervisor.json`. `/status/api` expone `strict: {result, checks}`; el portal muestra una franja roja fija con el texto anterior y deshabilita la tarjeta ComfyUI con el motivo. `/report.md` añade `## 10. Arranque estricto` al principio del triage con severidad `alta`.
3. **No** arranca Fase 1, Fase 2 ni ComfyUI (nada de descargar 60 GB a una instancia que se va a destruir).
4. Espera `STRICT_FATAL_GRACE` segundos (interrumpible por `SIGTERM`) y sale con **código 3**. `tini` propaga el código; el contenedor termina.
5. En Vast.ai, un contenedor que termina deja la instancia en un estado distinto de `running` [Conocimiento: `actual_status` pasa a `exited`; **verificar** en la primera prueba y anotar también si la facturación de GPU continúa en ese estado, que es lo que motiva el paso 6].
6. `comfy` (Mac) en `open`, `watch` y `all`: si `actual_status != running` o si `/status/api.strict.result == "fatal"`, imprime el bloque `[FATAL]` recuperado de `vastai logs` y ofrece `comfy destroy <id>` con confirmación. Nunca destruye solo.

Con `STRICT_START=0` todo lo anterior se degrada a WARN: se imprime el bloque con `[WARN]`, se sigue arrancando y el triage lo muestra. Es el modo para depurar en una instancia "mala" a propósito.

### 6.4 Códigos de salida de `comfyctl supervise`

| Código | Significado |
|---|---|
| `0` | Apagado limpio por `SIGTERM`/`SIGINT` (`docker stop`). |
| `3` | FATAL de arranque estricto tras el periodo de gracia. |
| `4` | Error de configuración (variable inválida, fichero obligatorio ausente, puerto ocupado por otro servicio). |
| `5` | Nginx no arrancó (`nginx -t` falló). |
| *(nunca)* | Agotar `COMFY_MAX_RESTARTS` **no** termina el proceso: estado `exhausted` con portal vivo, igual que hoy. |

El `HEALTHCHECK` del `Dockerfile` no cambia (`/healthz`); en estado `fatal`/`exhausted` el contenedor aparece `unhealthy`, que es correcto.

---

## 7. `comfyctl diag` (reemplazo de `diag.sh`)

- Los 10 checks actuales se conservan con los mismos nombres de clave (`gpu`, `torch`, `ort`, `nvenc`, `libx264`, `nginx`, `disk`, `ram`, `cpu`, `python-imports`, `ldconfig`, `env`) y el mismo formato de línea, para no romper `triage_patterns.json`.
- Nativos en Rust: `gpu`, `nvenc`, `libx264`, `nginx` (lee `/proc/*/status` para el usuario de los workers: sin `ps`), `disk`, `ram`, `cpu`, `ldconfig`, `env`.
- Probes Python (`/etc/comfyui/probes/torch_matmul.py`, `ort_session.py`, `imports.py`): el mismo código que hoy está embebido en `diag.sh`, extraído a ficheros. `comfyctl` los ejecuta con `timeout` propio (no el binario `timeout`), captura stdout/stderr, redacta y clasifica por el prefijo `OK|`/`WARN|`/`FAIL|`.
- Las comprobaciones de `strict-check` y `diag` comparten colectores (`collect/gpu.rs`, etc.) para no ejecutar `nvidia-smi` dos veces.

---

## 8. Cambios en `Dockerfile` y CI

### 8.1 `Dockerfile` (multi-stage)

```dockerfile
# syntax=docker/dockerfile:1.7
FROM rust:1.86.0-slim-bookworm@sha256:<digest> AS builder
RUN rustup target add x86_64-unknown-linux-musl && apt-get update && apt-get install -y --no-install-recommends musl-tools && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY comfyctl/Cargo.toml comfyctl/Cargo.lock comfyctl/rust-toolchain.toml ./
RUN mkdir src && echo 'fn main(){}' > src/main.rs && cargo build --release --locked --target x86_64-unknown-linux-musl && rm -rf src   # caché de dependencias
COPY comfyctl/ ./
RUN cargo build --release --locked --target x86_64-unknown-linux-musl \
 && ./target/x86_64-unknown-linux-musl/release/comfyctl version

FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04   # stage actual, sin cambios hasta aquí
…
COPY --from=builder /src/target/x86_64-unknown-linux-musl/release/comfyctl /usr/local/bin/comfyctl
COPY comfyctl/assets/probes/ /etc/comfyui/probes/
RUN comfyctl version && comfyctl strict-check --help >/dev/null
```

- `hadolint` ya está en CI; la imagen `rust` se fija por digest igual que las acciones de GitHub (criterio CRIT-01).
- Tamaño esperado del binario: 2–4 MB (`opt-level = "z"`, `strip`). Sin impacto en la imagen de 20+ GB.
- `python3-dev`, `build-essential` y `cmake` siguen en la imagen final por ALTA-03 (nodos con extensiones C++); nada de eso se usa para `comfyctl`.

### 8.2 Workflow

- Job `validate`: nuevo paso `rust` con `dtolnay/rust-toolchain@<sha>` (fijar por SHA como las demás acciones) leyendo `rust-toolchain.toml`; `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test --locked`, `cargo deny check`. Caché de `~/.cargo` y `target/` con `actions/cache@<sha>` por hash de `Cargo.lock`.
- Smoke test full-stack (§9.2 de la propuesta original): se mantienen los 10 pasos y se añaden:
  - **11.** `docker run --rm --entrypoint comfyctl $TAG strict-check --json` en el runner (sin GPU) → `result == "fatal"` con `gpu.present` FATAL y código 3: prueba el camino fatal real.
  - **12.** Contenedor con `-e STRICT_START=0 -e COMFYUI_EXTRA_ARGS=--cpu` (el actual) → `strict.result == "warn"`, ComfyUI `running`.
  - **13.** Contenedor con `-e STRICT_START=1 -e STRICT_FATAL_GRACE=20` sin `--cpu` → `/home` responde durante la gracia con la franja `[FATAL]`, `/report.md` contiene `## 10. Arranque estricto`, y `docker wait` devuelve `3` en < 60 s.
  - **14.** Paridad: `comfyctl report` dentro del contenedor y `curl /report.md` producen las mismas cabeceras `## N.`.
- `shellcheck` deja de analizar `diag.sh` (eliminado) y sigue con `start.sh`, `download-models.sh`, `tools/mac/comfy`.

---

## 9. `start.sh` resultante (esqueleto)

```bash
#!/usr/bin/env bash
set -euo pipefail
export COMFY_APP_DIR=/opt/ComfyUI COMFY_DATA_DIR=/workspace/comfyui-data …
mkdir -p /var/log/nginx; : > /var/log/start.log; date +%s > /run/comfyui-started-at
exec > >(tee -a /var/log/start.log) 2>&1
comfyctl version
# datos, extra_model_paths.yaml, htpasswd y mensaje de credenciales: igual que hoy (pasos 1–3)
…
nginx -t && nginx
# A partir de aquí todo lo gestiona comfyctl. COMFY_PASS solo viaja por fichero 0600 (O-05).
umask 077; printf '%s' "${COMFY_PASS}" > /run/comfyui-pass; umask 022
unset COMFY_PASS
exec comfyctl supervise
```

`comfyctl` lee `/run/comfyui-pass` una vez para `redact`, lo borra del disco (`unlink`) y lo conserva solo en memoria; `env_masked` sigue mostrando `<autogenerada>`/`<definida por el usuario>` a partir de `COMFY_PASS_AUTOGENERATED`. Así ni ComfyUI, ni la Fase 2, ni la shell de `ttyd` heredan la contraseña.

---

## 10. Plan de migración

### 10.0 Prerrequisitos (sin excepciones)

- [ ] Primer workflow verde con `f0fd82d` + correcciones V6 (`O-01..O-14`).
- [ ] Sesión de aceptación en Vast.ai con V13–V20 anotadas en `decisiones.md`. En particular V15 (latencia real de `/report.md`) y V20 (crecimiento de logs) cuantifican el beneficio de O-03/O-04 y, por tanto, de esta migración.
- [ ] Esquema `docs/contratos/status-api.schema.json` y `supervisor-state.schema.json` generados a partir de la implementación Python y validados en el smoke test actual (así la paridad se mide contra algo fijo).
- [ ] Decisión D6 del dueño (§12).

### 10.1 Fase 1 — `strict-check` + `supervise` (sin tocar el servidor HTTP)

- Entregables: crate con `strict`, `supervisor`, `sys`, `config`, tests de supervisor y de strict con `nvidia-smi` simulado (binario falso en `PATH` que imprime CSV).
- `start.sh` pasa a `exec comfyctl supervise --external-status-server` (sigue lanzando `status-server.py` como hoy).
- Criterio de salida: smoke pasos 1–13 verdes; `docker stop` → 0; `kill -9` → `restarts == 1`; `pkill` (143) → `restarts` sin cambio; estado `fatal` visible en `/status/api` (campo aditivo que `status-server.py` lee de `/run/comfyui-strict.json` con un cambio de 15 líneas).
- Auditoría V7 sobre este commit.

### 10.2 Fase 2 — `serve` + `report` + `diag`

- Entregables: `http/*`, `collect/*`, `triage`, `logtail`, `redact`, `diag`, fixtures y snapshots; `status-server.py` y `diag.sh` eliminados; `Dockerfile` sin `COPY` de ambos.
- Criterio: paso 14 de paridad; portal sin cambios funciona contra el nuevo servidor; `/report.md` en < 2 s con caché fría en el runner; `tail` de un log de 500 MB sintético en < 50 ms (test con fichero generado).
- Auditoría V8.

### 10.3 Fase 3 (opcional) — `comfy` para la Mac en Rust

Solo si tras usar el Bash actual aparece una necesidad concreta (p. ej. parsear la API REST de Vast directamente en vez de depender del formato de `vastai show instances --raw`, que ya cambió entre 0.x y 1.x [Repo: `decisiones.md`]). Diseño:

- Crate separado `comfy-cli/` (misma política de calidad), target `aarch64-apple-darwin` y `x86_64-apple-darwin`, compilado en un job `macos-14` del workflow, publicado como *asset* de GitHub Release con `SHA256SUMS` firmado por `cosign` (keyless, OIDC de Actions) o, como mínimo, con suma publicada en el README.
- Habla con `https://console.vast.ai/api/v0/` [Docs: la API REST de Vast.ai está documentada en `docs.vast.ai`; **verificar** endpoints `GET /instances/`, `PUT /instances/{id}/` (destroy) y `PUT /asks/{id}/` (crear) antes de implementar] usando la API key de `~/.vast_api_key` (la misma que usa el CLI oficial). Credenciales nunca por argumento.
- Mismos subcomandos y mismo comportamiento que §10.2 de la propuesta original más el flujo FATAL de §6.3.6.
- Hasta entonces, el Bash de `tools/mac/comfy` con las correcciones O-01/O-13/O-14 es suficiente.

---

## 11. Criterios de aceptación (por fase)

**Fase 1**
- [ ] `cargo clippy -D warnings`, `cargo test`, `cargo deny` verdes; `cargo tree | wc -l` ≤ 40.
- [ ] Smoke 1–13 verdes; `docker wait` devuelve 3 en el escenario FATAL y 0 en `docker stop`.
- [ ] `/proc/<pid_comfy>/environ` sin `COMFY_PASS`.
- [ ] Con `kill -9` ×6 seguidos → `exhausted`, portal vivo; con un `kill -9`, esperar `COMFY_HEALTHY_UPTIME` y otro `kill -9` → `restarts == 1` (reset funcionó).
- [ ] En GPU real: `strict-check` OK en la RTX 5090 y en una RTX 4090 del README; FATAL simulado con `STRICT_MIN_VRAM_MB=99999`.

**Fase 2**
- [ ] Paridad de esquema JSON y de secciones de `/report.md`.
- [ ] `/report.md` con caché fría < 2 s en el runner; `tail` de 500 MB < 50 ms.
- [ ] Los 17 patrones disparan con sus fixtures y ninguno con las de los demás (matriz 17×17 en test).
- [ ] Portal, `comfy watch` y el smoke test sin cambios.

**Fase 3**
- [ ] Binario macOS verificable por suma; `comfy open` abre `/home` de una instancia real; flujo FATAL ofrece `destroy` y no destruye sin confirmación.

---

## 12. Decisión D6 para el dueño

| Opción | Qué implica | Cuándo tiene sentido |
|---|---|---|
| **A. Quedarse en Python/Bash** | Aplicar O-01..O-14 y añadir `STRICT_START` en Bash (`strict-check.sh` con `nvidia-smi`, `statvfs` vía `df`, probes Python). Funcional, pero el diagnóstico sigue dependiendo de Python y el supervisor de Bash. | Si tras V13–V20 el sistema es estable y el uso es personal y esporádico. |
| **B. Híbrida (recomendada)** | `comfyctl` para supervisor, estado, informe, diag y arranque estricto (Fases 1–2). Bash como pegamento; `comfy` Mac en Bash. | Si se va a usar la imagen con regularidad, con dinero real por hora, y se quiere que el diagnóstico sobreviva a un entorno Python roto. |
| **C. Todo en Rust** | B + Fase 3 (`comfy` Mac). | Solo si el CLI oficial `vastai` vuelve a cambiar su formato de salida o se quiere distribuir `comfy` a otras personas. |

Decisiones secundarias que hay que fijar al elegir B o C:
- `STRICT_START` por defecto `1` (recomendado: la imagen es específica de GPU Ada/Blackwell y la propuesta entera va de no pagar instancias inútiles) o `0`.
- Umbrales de §6.2 (VRAM 20 GB, disco por perfil, driver 570) — son propuestas; el dueño decide con la tabla de compatibilidad de NVIDIA a la vista.
- `tiny_http` vs servidor sobre `std::net` (recomendado `tiny_http`).

---

## 13. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| El stage `builder` falla por red (crates.io) y bloquea el build de una imagen que no cambió en Rust | Caché de BuildKit por capa (`Cargo.lock` primero) y `cargo vendor` opcional en una fase posterior si la tasa de fallos lo justifica. |
| Divergencia entre lo que `status-server.py` hacía y lo que `comfyctl` hace | Esquemas congelados (§10.0) y tests de paridad (paso 14). |
| Falsos FATAL que impiden usar una instancia válida | `STRICT_START=0` como escape documentado en README §5; umbrales por variable; WARN en vez de FATAL para todo lo que no sea "no hay CUDA", "driver incompatible", "disco < 50 % del umbral". |
| Más código que auditar | Reglas de §4.4; cada fase es un commit y una ronda de auditoría. |
| Deuda de dos lenguajes | Bash queda en ~60 líneas de `start.sh` y en `download-models.sh`; nada de lógica duplicada entre Bash y Rust. |

---

## 14. Lo que el ejecutor debe tener antes de escribir la primera línea

1. Aprobación de D6 (y de los defaults de §12) por el dueño.
2. V13–V20 completadas y anotadas.
3. `docs/contratos/*.schema.json` generados y validados contra la implementación actual.
4. Un fixture real por patrón de triage, obtenido de la sesión de aceptación o fabricado y marcado como sintético.
5. Confirmación en Vast.ai de qué ocurre con el estado y la facturación de una instancia cuyo contenedor sale con código ≠ 0 (§6.3.5). De ello depende cuánto énfasis poner en §6.3.6.
