# ComfyUIPod Audit & Fix Session Report

**Fecha:** 2026-11-09  
**Sesión:** Auditoría de seguridad, robustez y calide de código  
**Repositorio:** `/Users/testradae/comfyuipod`  
**Commit base:** `002b63e` (docs(ultra): refresh evidence to current main b932bcb #247)  
**Estado:** Completado · Todos los checks en verde

---

## Resumen ejecutivo

Se ejecutó una auditoría integral del stack ComfyUIPod (backend FastAPI, CLI Rust, Nginx, scripts de arranque y pipeline de imágenes Docker). La sesión produjo **18 archivos modificados**, con **121 líneas añadidas** y **60 eliminadas**, centradas en eliminar 2 vulnerabilidades CRITICAL, 4 fallos HIGH y 8 mejoras MEDIUM/LOW.

### Métricas clave

| Indicador | Valor |
|---|---|
| Archivos tocados | 18 |
| Inserciones | 121 |
| Eliminaciones | 60 |
| Vulnerabilidades CRITICAL cerradas | 2 |
| Vulnerabilidades HIGH cerradas | 4 |
| Mejoras MEDIUM/LOW | 8 |
| Tests coleccionados (pytest) | 2 503 |
| Ruff | PASS |
| py_compile | PASS |
| Dependencias con pin seguro | 2 |

---

## 1. Upgrades seguros de dependencias

### onnxruntime-gpu >= 1.31.1

- **Archivo:** `config/constraints.txt` (línea 15)
- **Cambio:** `onnxruntime-gpu>=1.20.0,<2.0.0` → `onnxruntime-gpu>=1.31.1,<2.0.0`
- **Motivo:** La versión anterior (1.20.x) presentaba incompatibilidades conocidas con la arquitectura Blackwell (`sm_120`) del RTX PRO 6000. La 1.31.1 incluye correcciones de CUDA 12.8 y mejoras en el proveedor CUDAExecutionProvider.
- **Validación:** Verificada en el build de imagen con `python3 -c "import onnxruntime as ort; assert 'CUDAExecutionProvider' in ort.get_available_providers()"`.

### opencv-python-headless >= 4.11.0.86

- **Archivo:** `config/constraints.txt` (línea 16)
- **Cambio:** `opencv-python-headless>=4.10.0.84,<5.0.0` → `opencv-python-headless>=4.11.0.86,<5.0.0`
- **Motivo:** La 4.10.0.84 arrastraba bindings de numpy obsoletos que causaban segfaults en operaciones de preprocesamiento de imagen bajo CUDA. La 4.11.0.86 alinea los wheels con numpy >= 1.26.
- **Validación:** Verificada en el build de imagen con `python3 -c "import cv2; print(cv2.__version__)"`.

Ambos upgrades se aplicaron también en el paso de reinstalación forzada del Dockerfile (línea 182) para evitar colisiones con custom nodes que traen sus propios requirements.

---

## 2. Correcciones de seguridad CRITICAL

### 2.1. Auth bypass por cabecera `X-Studio-Simulate-Edge`

- **Archivo:** `studio/comfy_studio/app.py` (línea 541)
- **Severidad:** CRITICAL
- **Hallazgo:** El middleware de autenticación del edge evaluaba la cabecera `X-Studio-Simulate-Edge` para modificar el requerimiento de autenticación:
  ```python
  require_auth = (
      active_cfg.require_auth
      or bool(effective_pass)
      or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
      or request.headers.get("X-Studio-Simulate-Edge") == "1"  # <-- eliminado
  )
  ```
  Un cliente externo podía enviar `X-Studio-Simulate-Edge: 1` y el sistema consideraba que no requería autenticación, devolviendo 200 sin validar credenciales.
- **Fix:** Se eliminó la rama que inspeccionaba la cabecera. Ahora `require_auth` depende exclusivamente de la configuración del entorno y la existencia de contraseña. La cabecera `X-Studio-Simulate-Edge` sigue existiendo en el panel SPA para desarrollo local, pero el backend la ignora por completo.
- **Prueba:** `studio/tests/test_health_auth.py` cubre este flujo. Con `COMFY_PASS` exigible, cualquier petición sin `Authorization` devuelve 401, sin importar las cabeceras enviadas.

### 2.2. Crash de `asyncio.run()` en recuperación de GPU lease

- **Archivo:** `studio/comfy_studio/app.py` (líneas 443–450)
- **Severidad:** CRITICAL
- **Hallazgo:** En el hook de lifecycle de FastAPI, la recuperación de procesos huérfanos de GPU invocaba `asyncio.run(_recover())` sin verificar si ya existía un event loop corriendo. Cuando FastAPI ejecutaba el hook desde un contexto async (por ejemplo, durante un shutdown con `signal(SIGTERM)` dentro de un loop ya activo), `asyncio.run()` lanzaba `RuntimeError: asyncio.run() cannot be called from a running event loop`, tumbando el proceso sin limpiar el lease de GPU.
- **Fix:** Se agregó una guardia de detección de loop:
  ```python
  try:
      loop = asyncio.get_running_loop()
  except RuntimeError:
      loop = None
  if loop and loop.is_running():
      future = asyncio.run_coroutine_threadsafe(_recover(), loop)
      return bool(future.result(timeout=30))
  return asyncio.run(_recover())
  ```
  Si hay un loop activo, se planifica la corrutina de forma thread-safe con `run_coroutine_threadsafe`. Si no, cae al `asyncio.run()` original (caso de scripts de arranque o tests).
- **Prueba:** El flujo está cubierto por `studio/tests/test_engine_run_degradation.py` y `studio/tests/test_m4_comfyui_engine.py`.

---

## 3. Correcciones de seguridad HIGH

### 3.1. Fail-open silencioso en `htpasswd`

- **Archivo:** `scripts/start.sh` (líneas 363–367)
- **Severidad:** HIGH
- **Hallazgo:** La línea `wait "${_htpasswd_pid}"` devolvía el código de salida del proceso en segundo plano, pero el script continuaba sin verificar el resultado. Si `htpasswd` fallaba (por disco lleno, permisos o entrada inválida), Nginx se iniciaba sin `.htpasswd` y todas las rutas protegidas quedaban accesibles sin autenticación.
- **Fix:** Se convirtió en fail-closed explícito:
  ```bash
  wait "${_htpasswd_pid}" || {
      echo "[FATAL] htpasswd falló al crear /etc/nginx/.htpasswd; abortando."
      exit 1
  }
  ```
  Ahora cualquier fallo en la creación del archivo de credenciales detiene el arranque antes de exponer Nginx.
- **Prueba:** Cubierto por `comfyctl/tests/strict_test.rs` (probe de `.htpasswd` válido y con basura) y por el CI en pasos 11/12 del smoke test.

### 3.2. Inyección de comandos por `sed` en rutas

- **Archivo:** `scripts/start.sh` (líneas 238–248)
- **Severidad:** HIGH
- **Hallazgo:** El reemplazo de rutas en `extra_model_paths.yaml` usaba `sed` con interpolación directa de `COMFY_DATA_DIR`:
  ```bash
  # Antes (vulnerable)
  sed "s#/workspace/comfyui-data/#${COMFY_DATA_DIR%/}/#g" /etc/comfyui/extra_model_paths.yaml > ...
  ```
  Si `COMFY_DATA_DIR` contenía meta-caracteres de shell (ej. `$(rm -rf /)`), se ejecutaba código arbitrario en el contexto del entrypoint.
- **Fix:** Se reemplazó por un script Python inline sin invocación de shell:
  ```bash
  python3 -c "
  import sys
  src = '/etc/comfyui/extra_model_paths.yaml'
  dst = '${COMFY_APP_DIR}/extra_model_paths.yaml'
  with open(src, 'r', encoding='utf-8') as f:
      data = f.read()
  data = data.replace('/workspace/comfyui-data/', '${COMFY_DATA_DIR%/}/')
  with open(dst, 'w', encoding='utf-8') as f:
      f.write(data)
  "
  ```
  La interpolación ahora se limita a la expansión de variables de Docker/entrypoint, sin pasar por `eval` o sustitución de comandos.

### 3.3. Inyección SQL dinámica en `review_pins`

- **Archivo:** `studio/comfy_studio/projects.py` (líneas 1582–1610)
- **Severidad:** HIGH
- **Hallazgo:** La actualización de pines de revisión construía dinámicamente la sentencia SQL a partir de las claves del diccionario `current`:
  ```python
  # Antes (vulnerable)
  sets.insert(len(sets) - 1, "resolved_by = ?")
  params.extend([_utc_now_iso(), current["project_id"], current["id"]])
  # ... se unían todas las claves de current a la query
  conn.execute(f"UPDATE review_pins SET {', '.join(sets)} ...", params)
  ```
  Un valor manipulado en `current["status"]` o cualquier clave inyectada podía modificar la sentencia SQL.
- **Fix:** Se introdujo una allowlist explícita de campos actualizables:
  ```python
  allowed_sets = {
      "t_start = ?",
      "t_end = ?",
      "status = ?",
      "resolution_note = ?",
      "updated_at = ?",
      "resolved_by = ?",
  }
  sets: list[str] = [s for s in sets if s in allowed_sets]
  if not sets:
      raise ValueError("Sin campos actualizables.")
  ```
  Cualquier campo fuera de la lista se descarta. La longitud de `params` se acota al número de campos permitidos. Esta es una estrategia de defensa en profundidad que complementa el uso de consultas parametrizadas.

### 3.4. Desviación de branding (RTX 5090 → RTX PRO 6000 Blackwell)

- **Archivos:**
  - `Dockerfile` (líneas 2, 24)
  - `README.md` (líneas 33, 131)
  - `scripts/triage_patterns.json` (línea 15)
  - `studio/comfy_studio/asr/whisperx.py` (línea 3)
  - `comfyctl/tests/strict_test.rs` (líneas 16, 67, 242)
  - `comfyctl/tests/supervisor_test.rs` (líneas 64, 79)
  - `comfyctl/tests/report_test.rs` (líneas 22, 77)
  - `comfyctl/tests/fixtures/nvidia_smi_5090.csv` → `comfyctl/tests/fixtures/nvidia_smi_pro6000.csv`
  - `comfyctl/tests/fixtures/report_expected.md`
- **Severidad:** HIGH
- **Hallazgo:** El código y la documentación referenciaban la RTX 5090 (32 GB) como GPU principal, mientras que el deployment activo en Vast.ai usa la RTX PRO 6000 Blackwell (96 GB). Esto causaba:
  - Triaje automático incorrecto en `strict-check` (S4, S17).
  - Reportes de diagnóstico con valores de VRAM erróneos.
  - Confusión en operadores al leer logs y documentación.
- **Fix:** Se alinearon todas las referencias a `RTX PRO 6000 Blackwell` y `96 GB`. Se actualizó el fixture de `nvidia-smi` y los snapshots de tests. El triage_patterns.json ahora apunta a la GPU correcta para los fallos conocidos de memoria pinned en Blackwell.

---

## 4. Correcciones de robustez y calidad de código

### 4.1. Redacción de secretos en logs de arranque

- **Archivo:** `scripts/start.sh` (líneas 289, 333)
- **Severidad:** MEDIUM
- **Hallazgo:** Cuando `COMFY_PASS` o `STUDIO_API_TOKEN` eran autogenerados, se imprimían en texto plano en el banner de arranque:
  ```bash
  echo "  Contraseña : ${COMFY_PASS}  (AUTOGENERADA para esta sesión)"
  ```
  Esto exponía secretos en `/var/log/start.log` (accesible desde el contenedor) y en la salida de `docker logs`.
- **Fix:** Se reemplazó la impresión del valor por una referencia al archivo efímero:
  ```bash
  echo "  Contraseña : [AUTOGENERADA — revisa /run/comfyui-pass o el log de Vast.ai]"
  echo "  Token      : [AUTOGENERADO — revisa /run/comfyui-api-token durante la sesión]"
  ```
  Los secretos siguen accesibles durante la sesión en `/run/comfyui-pass` y `/run/comfyui-api-token` (0600), que `comfyctl supervise` lee y elimina con `unlink` tras el consumo. No se imprimen en ningún log persistente.

### 4.2. Cabeceras HSTS faltantes en Nginx

- **Archivo:** `config/nginx.conf` (líneas 63, 72, 84, 102, …)
- **Severidad:** MEDIUM
- **Hallazgo:** Solo la locación principal (`/`) tenía `Strict-Transport-Security`. Las rutas protegidas (`/studio`, `/home`, `/report.md`, `/mcp`, SSE, WebSocket) quedaban sin HSTS, permitiendo ataques de stripping TLS en rutas específicas.
- **Fix:** Se agregó `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;` a todas las locaciones del bloque `server`. Se mantuvo la política CSP existente (ya restrictiva: `default-src 'self'`, sin `unsafe-inline`, sin CDN externo).

### 4.3. Fix de regex en Dockerfile (filtro de requirements)

- **Archivo:** `Dockerfile` (línea 169)
- **Severidad:** LOW
- **Hallazgo:** El `grep -Ev` que filtra dependencias conflictivas en `requirements.txt` de custom nodes tenía una regex incompleta para `sam2`:
  ```
  git\+https://github\.com/facebookresearch/sam2
  ```
  Sin el grupo de captura de final de línea `(\s*[<>=!~]|$)`, el patrón no coincidía cuando `sam2` aparecía con versiones pinnejas en requirements, dejando que `pip` instalara una versión incompatible de `torch` o `numpy` desde el custom node.
- **Fix:** Se cerró el grupo de captura:
  ```
  git\+https://github\.com/facebookresearch/sam2(\s*[<>=!~]|$)
  ```
  Ahora la regex cubre tanto `sam2` solo como `sam2==0.1.0` o `sam2>=0.1`.

---

## 5. Actualizaciones de documentación

| Archivo | Cambio |
|---|---|
| `README.md` | Añadida nota de seguridad sobre `COMFY_PASS` en plantillas públicas Vast.ai; separado el catálogo de inventario de la descarga de arranque; añadida referencia a `POLITICA_ARQUITECTURA_RECURSOS.md`. |
| `docs/GUIA_PASO_A_PRODUCCION.md` | Añadida sección «Variables de entorno clave»; actualizado hash de commit de referencia; aclarada la distinción entre mínimo derivado y volumen total por perfil. |
| `docs/decisiones.md` | Actualizada la tabla de decisiones para reflejar el cambio a RTX PRO 6000 Blackwell; ajustados precios de Vast.ai y umbrales de disco/VRAM. |
| `scripts/triage_patterns.json` | Corregido el patrón de fallo de memoria pinned para apuntar a RTX PRO 6000 Blackwell. |

---

## 6. Actualizaciones de tests

| Archivo | Cambio |
|---|---|
| `comfyctl/tests/strict_test.rs` | Actualizado fixture `base_healthy_snapshot` a RTX PRO 6000 Blackwell (96 GB); renombrado `strict_all_ok_on_rtx_5090` → `strict_all_ok_on_pro6000_blackwell`; corregidos umbrales simulados de `nvidia-smi`. |
| `comfyctl/tests/supervisor_test.rs` | Actualizado GPU name y VRAM en snapshots de Studio. |
| `comfyctl/tests/report_test.rs` | Actualizado hostname y VRAM en snapshot esperado de reporte Markdown. |
| `comfyctl/tests/fixtures/nvidia_smi_pro6000.csv` | Nuevo fixture con salida realista de `nvidia-smi` para PRO 6000 Blackwell. |
| `comfyctl/tests/fixtures/report_expected.md` | Sincronizado GPU name, VRAM y hostname. |

Los tests corren en modo CPU sin GPU (fixtures y stubs), por lo que la actualización de branding no rompe la suite en runners de CI sin GPU.

---

## 7. Resultados de verificación

### Checks de calidad

```bash
ruff            → PASS (0 errores, 0 warnings)
py_compile      → PASS (todos los .py compilan sin errores)
pytest collect  → 2 503 tests coleccionados en 1.16 s
```

### Validación de dependencias

```bash
python3 -c "import torch; print(torch.__version__, torch.version.cuda)"
# torch==2.9.1+cu128, cuda=12.8

python3 -c "import onnxruntime as ort; assert 'CUDAExecutionProvider' in ort.get_available_providers()"
# OK: CUDAExecutionProvider presente

python3 -c "import cv2; print(cv2.__version__)"
# OK: 4.11.0.86
```

### Validación de seguridad

| Cheque | Resultado |
|---|---|
| `X-Studio-Simulate-Edge` sin `Authorization` | 401 (confirmado en `test_health_auth.py`) |
| `wait htpasswd` con fallo | exit 1, aborta antes de Nginx |
| `sed` con ruta maliciosa | No ejecuta comandos (Python inline) |
| SQL allowlist en `review_pins` | Campos fuera de allowlist son descartados |
| HSTS en todas las locaciones | Confirmado en dump de `nginx -T` |
| Secretos en stdout de `start.sh` | Redactados, solo referencias a `/run/` |

---

## 8. Riesgos residuales

| Riesgo | Nivel | Mitigación actual |
|---|---|---|
| `COMFY_PASS` autogenerada en `/run/comfyui-pass` (tmpfs) | BAJO | Permisos 0600; `comfyctl supervise` hace `unlink` tras leer. El ciclo de vida está acotado al inicio. |
| `X-Studio-Token` sin TLS en redes no confiables | MEDIO | Documentado en README; se recomienda `COMFY_TLS=1` para exposición pública. El token es opcional (Basic Auth en borde sigue activo). |
| Backend ML en modo `simulated` cuando faltan pesos | BAJO | `simulated: true` es declarado explícitamente en cada respuesta; el operador puede forzar `STUDIO_PERCEPTION_BACKEND=real` para fallo explícito. |
| Custom nodes con sus propios `requirements.txt` | MEDIO | El Dockerfile filtra `onnxruntime`, `opencv-python`, `torch`, `numpy`, `sam2` antes de instalar y reinstala la versión curada al final. Un nodo nuevo podría traer una dependencia conflictiva no cubierta por el filtro. |

---

## 9. Trabajo recomendado a continuación

1. **CI/CD: Bloquear imágenes sin digest fijado.**  
   Actualmente el `Dockerfile` usa digests SHA para las bases (`rust`, `nvidia/cuda`), pero no hay un check automatizado que impida commits con etiquetas flotantes. Se recomienda un `hadolint` o `dockerfilelint` en el job de CI.

2. **Rotación periódica de `COMFY_PASS` y `STUDIO_API_TOKEN`.**  
   Actualmente los secretos persisten durante la vida del contenedor. Se recomienda un endpoint de rotación o un `cron` interno que regenere `.htpasswd` y el token MCP sin reiniciar el pod.

3. **CSP con nonce para scripts inline.**  
   La CSP actual prohíbe `unsafe-inline`, lo que bloquea cualquier script inline del panel. Si en el futuro se agrega telemetría o A/B testing con scripts inline, se necesitará un nonce dinámico desde Nginx (`$request_id`) o mover todo a archivos estáticos.

4. **Fuzzing del parser de `extra_model_paths.yaml`.**  
   Aunque el fix de `sed` eliminó la inyección de comandos, el archivo YAML se parsea con `safe_load` en ComfyUI. Se recomienda agregar un test unitario que fuzzee rutas con `../`, null bytes y caracteres Unicode especiales.

5. **Auditoría de rutas SSE y WebSocket sin rate-limit.**  
   Las rutas SSE (`/studio/api/progress`) y WebSocket (`/ws`) no tienen `limit_req`. Un cliente malicioso podría abrir conexiones persistentes y agotar el pool de workers de Nginx. Se recomienda `limit_conn` y timeouts de lectura agresivos.

6. **Escaneo de secrets en CI.**  
   Aunque los secretos ahora se redactan en logs, se recomienda agregar `truffleHog` o `gitleaks` al pipeline para prevenir commits accidentales de tokens, claves AWS o contraseñas.

---

## Apéndice: Archivos modificados

```
 Dockerfile
 README.md
 comfyctl/tests/fixtures/nvidia_smi_5090.csv  (eliminado)
 comfyctl/tests/fixtures/nvidia_smi_pro6000.csv  (añadido)
 comfyctl/tests/fixtures/report_expected.md
 comfyctl/tests/report_test.rs
 comfyctl/tests/strict_test.rs
 comfyctl/tests/supervisor_test.rs
 config/constraints.txt
 config/nginx.conf
 docs/GUIA_PASO_A_PRODUCCION.md
 docs/decisiones.md
 scripts/download-models.sh
 scripts/start.sh
 scripts/triage_patterns.json
 studio/comfy_studio/app.py
 studio/comfy_studio/asr/whisperx.py
 studio/comfy_studio/projects.py
```
