# Auditoría de Agente — Versión 1

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha de auditoría | 2026-10-16 |
| Tipo | Revisión estática (lectura de código). **No se construyó la imagen ni se ejecutó ningún script.** |
| Archivos revisados | `Dockerfile`, `scripts/start.sh`, `scripts/download-models.sh`, `config/nginx.conf`, `config/extra_model_paths.yaml`, `config/manager-config.ini`, `.github/workflows/docker-publish.yml`, `README.md`, `.dockerignore`, `.gitattributes` |
| Destinatario | Agente que tomará decisiones de implementación sobre este repo |
| Estado del repo | La carpeta **no es un repositorio git** (`git rev-parse` falla) y vive dentro de OneDrive. Ver OPS-01. |

## 0. Cómo leer este documento

- Cada hallazgo tiene un ID (`SEC`, `REL`, `REP`, `CFG`, `CI`, `DOC`, `OPS`), severidad, ubicación exacta, evidencia, impacto y acción propuesta.
- La columna **Decisión** indica si el agente puede aplicar el cambio directamente (`Aplicar`) o si requiere confirmación del dueño porque cambia comportamiento o alcance (`Consultar`).
- La sección 4 lista lo que **no pude verificar sin ejecutar** y cómo verificarlo.
- La sección 5 lista lo que está bien y **no debe tocarse**.

Severidades: **Crítica** (rompe el arranque o expone credenciales), **Alta** (falla probable en producción o bloquea reproducibilidad), **Media** (degrada fiabilidad/UX), **Baja** (limpieza, deuda).

---

## 1. Resumen ejecutivo

El proyecto está bien pensado para su objetivo (instancia efímera, 100% web, usuario no técnico) y tiene varias decisiones correctas: ComfyUI escucha solo en `127.0.0.1`, Nginx delante con Basic Auth, descarga en dos fases, `/healthz` sin auth, normalización de EOL, `hf` CLI con fallback.

Los problemas principales se agrupan en cuatro ejes:

1. **Seguridad de credenciales**: la contraseña se imprime en los logs del contenedor, hay contraseña por defecto `admin/comfyui` en un puerto público, el token de HF se pasa por argumento de CLI y se habilita CORS abierto sin necesidad.
2. **Reproducibilidad cero**: ningún commit, tag ni versión está fijado (ComfyUI, 10 custom nodes, torch, ~15 paquetes pip). `pip install -r ... || true` oculta fallos y permite que un custom node reemplace torch. Cada build puede producir una imagen distinta o rota sin que nadie cambie el repo.
3. **Fragilidad en el arranque**: un fallo en Fase 1 (red, HF caído, CLI ausente) impide que ComfyUI arranque; `pipefail` + `grep -q` puede producir falsos `WARN`; falta `user www-data;` en Nginx, lo que puede hacer que los workers no lean `.htpasswd`.
4. **Trampa de `/workspace`**: ComfyUI está horneado en `/workspace/ComfyUI`. Si el usuario monta un volumen de Vast.ai en `/workspace` (práctica habitual), el volumen oculta toda la instalación y el contenedor no arranca.

Además el README contiene rutas locales de Windows (`file:///c:/Users/estra/...`), estimaciones de tiempo/tamaño incorrectas (~43 GB declarados vs ~60–65 GB reales) y el CI no valida nada de la imagen.

### Tabla de prioridades

| Prioridad | IDs | Esfuerzo total estimado |
|---|---|---|
| Hacer ya (quick wins, sin riesgo) | SEC-01, SEC-03, SEC-04, REL-02, REL-03, CFG-02, DOC-01 | 1–2 h |
| Siguiente sprint | SEC-02, REL-01, REL-04, REP-01, REP-02, CFG-01, CI-01, CI-02, DOC-02 | 1 día |
| Planificar | REL-05, REP-03, CFG-03, CI-03, OPS-01, OPS-02 | 1–3 días |

---

## 2. Hallazgos detallados

### 2.1 Seguridad (SEC)

#### SEC-01 — Contraseña impresa en texto plano en logs · **Crítica** · Decisión: Aplicar
- **Ubicación**: `scripts/start.sh:63-68`
- **Evidencia**: `echo "  Contraseña : ${COMFY_PASS}"`
- **Impacto**: Los logs del contenedor son visibles en la consola de Vast.ai, en `docker logs`, y en cualquier sistema que los recolecte. Cualquiera con acceso de lectura a logs obtiene la credencial del puerto público.
- **Acción**: Imprimir solo el usuario y un aviso de si la contraseña es la por defecto o no. Nunca el valor.

#### SEC-02 — Credenciales por defecto `admin` / `comfyui` en puerto público · **Alta** · Decisión: Consultar
- **Ubicación**: `scripts/start.sh:52-57`
- **Impacto**: Si el usuario olvida `-e COMFY_PASS=...`, la instancia queda accesible a Internet con credenciales triviales y `security_level = weak` en Manager (que permite instalar y ejecutar código arbitrario vía git). Es ejecución remota de código con contraseña conocida.
- **Opciones para el dueño**:
  - (a) Abortar el arranque si `COMFY_PASS` no está definida (más seguro, peor UX).
  - (b) Generar una contraseña aleatoria (`openssl rand -base64 18`) y mostrarla **una sola vez** en el log con un prefijo claro (compromiso razonable para instancia efímera; contradice parcialmente SEC-01, pero la contraseña aleatoria no es reutilizable en otras instancias).
  - (c) Mantener la por defecto (no recomendado).
- **Recomendación**: (b), y exigir longitud mínima si el usuario la define.

#### SEC-03 — `--enable-cors-header` sin origen = `Access-Control-Allow-Origin: *` · **Media** · Decisión: Aplicar
- **Ubicación**: `scripts/start.sh:119`
- **Impacto**: ComfyUI ya se sirve por el mismo origen vía Nginx; CORS abierto no aporta nada y amplía superficie para páginas de terceros que intenten hablar con la API. No es explotable directamente por Basic Auth (el navegador no envía credenciales a `*`), pero es configuración innecesaria.
- **Acción**: Eliminar el flag. Si en el futuro se necesita, usar `--enable-cors-header https://dominio-concreto`.

#### SEC-04 — Secretos pasados por línea de comandos · **Media** · Decisión: Aplicar
- **Ubicación**: `scripts/start.sh:59` (`htpasswd -b`), `scripts/download-models.sh:57-60` (`--token "${HF_TOKEN}"`)
- **Impacto**: Los argumentos de proceso son visibles en `ps`/`/proc` para cualquier proceso del contenedor (incluidos custom nodes instalados desde la UI).
- **Acción**:
  - `printf '%s' "$COMFY_PASS" | htpasswd -ci /etc/nginx/.htpasswd "$COMFY_USER"`
  - Eliminar `token_args`; el CLI de HF lee `HF_TOKEN` del entorno automáticamente.

#### SEC-05 — Basic Auth sobre HTTP plano sin documentar · **Media** · Decisión: Consultar
- **Ubicación**: `config/nginx.conf:34-35`, `README.md` (no lo menciona)
- **Impacto**: Las credenciales viajan en base64 (equivalente a texto plano) por Internet en cada petición. En Vast.ai los puertos directos son HTTP.
- **Opciones**: documentar el riesgo y recomendar contraseña larga única; o añadir TLS con certificado autofirmado en Nginx (el navegador mostrará advertencia); o documentar el uso de un túnel (Cloudflare Tunnel / Tailscale). Mínimo: documentar.

#### SEC-06 — Sin protección contra fuerza bruta en Basic Auth · **Baja** · Decisión: Aplicar
- **Ubicación**: `config/nginx.conf`
- **Acción**: Añadir `limit_req_zone $binary_remote_addr zone=auth:10m rate=10r/s;` y `limit_req zone=auth burst=20 nodelay;` en `location /`. Barato y reduce el impacto de SEC-02.

### 2.2 Fiabilidad en arranque y runtime (REL)

#### REL-01 — Nginx sin directiva `user`; workers probablemente corren como `nobody` y no pueden leer `.htpasswd` · **Alta (verificar)** · Decisión: Aplicar
- **Ubicación**: `config/nginx.conf:1-3` (falta `user www-data;`), `scripts/start.sh:60-61` (`chmod 640` + `chown root:www-data`)
- **Razonamiento**: El `nginx.conf` reemplaza por completo el de Ubuntu, que sí declara `user www-data;`. Sin esa directiva, nginx usa el usuario compilado por defecto (`nobody` en los paquetes de Debian/Ubuntu). `auth_basic_user_file` lo abren los **workers** en cada petición. Un worker `nobody` no puede leer un archivo `640 root:www-data` → `open() failed (13: Permission denied)` → HTTP 500 en todas las rutas con auth.
- **Cómo verificar**: dentro del contenedor, `ps -o user,cmd -C nginx` y `tail /var/log/nginx/error.log`.
- **Acción**: Añadir `user www-data;` como primera línea de `nginx.conf`. Independientemente del resultado de la verificación, es la configuración correcta.

#### REL-02 — `pipefail` + `grep -q` / `head` producen falsos negativos y posible aborto · **Media** · Decisión: Aplicar
- **Ubicación**: `scripts/start.sh:13,34,36,42`
- **Razonamiento**: `grep -q` termina al primer match y cierra el pipe; `ffmpeg -encoders` (salida > 4 KB) recibe SIGPIPE y sale con 141. Con `pipefail`, la condición del `if` es falsa aunque el encoder exista → imprime `[WARN] libx264 no detectado` falsamente. En la línea 34, `ffmpeg -version | head -n 3` puede abortar todo el script por `set -e` si ffmpeg recibe SIGPIPE (poco probable por el tamaño de salida, pero no cero).
- **Acción**: Capturar la salida una vez en variable y hacer grep sobre la variable:
  ```bash
  encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
  grep -q h264_nvenc <<<"$encoders" && echo OK || echo WARN
  ```

#### REL-03 — Un fallo en Fase 1 impide que ComfyUI arranque · **Media** · Decisión: Aplicar
- **Ubicación**: `scripts/start.sh:92`, `scripts/download-models.sh:28-31`
- **Razonamiento**: `start.sh` tiene `set -e`. Si `download-models.sh` sale con código distinto de 0 (por ejemplo, no encuentra `hf` ni `huggingface-cli`, línea 30), el entrypoint muere y el usuario ve una instancia "Running" sin servicio. Además, Fase 1 es síncrona sin timeout: si HuggingFace está lento, la UI no aparece en minutos y el usuario paga GPU sin usarla.
- **Acción**: `... --phase1 2>&1 | tee -a "$LOG" || echo "[WARN] Fase 1 incompleta, continuando"`; añadir `timeout 600` alrededor de Fase 1; considerar arrancar Nginx **antes** de Fase 1 para que `/download-status` ya responda.

#### REL-04 — Sin reintentos ni verificación en descargas; `aria2` instalado pero no usado · **Media** · Decisión: Aplicar
- **Ubicación**: `scripts/download-models.sh:62-75`, `Dockerfile:29`
- **Razonamiento**: Un corte de red marca `WARN` y el modelo queda ausente hasta el siguiente reinicio del contenedor (que en instancia efímera es "nunca"). No se comprueba tamaño ni hash. Descargas 100% secuenciales (~60 GB uno tras otro).
- **Acción**: Bucle de 3 intentos con backoff en `hf_fetch`; al final de cada fase, imprimir resumen de fallidos en el log (visible en `/download-status`). Decidir si se elimina `aria2` del Dockerfile o se usa para paralelizar (ver REP-03).

#### REL-05 — Proceso en background huérfano y sin `init` · **Baja** · Decisión: Aplicar
- **Ubicación**: `scripts/start.sh:106,115`
- **Razonamiento**: `exec python3` convierte a ComfyUI en PID 1. Cuando Fase 2 termina, su proceso queda zombi porque Python no cosecha hijos. Si ComfyUI muere, Nginx sigue vivo devolviendo 502 y el contenedor **no** se reinicia (porque Nginx es daemon y no es el proceso principal). Si Nginx muere, nadie lo reinicia.
- **Acción**: Instalar `tini` y usar `ENTRYPOINT ["/usr/bin/tini","--","/start.sh"]`. Opcional: `trap` para matar Nginx al salir.

#### REL-06 — `/healthz` siempre devuelve 200 aunque ComfyUI esté caído · **Baja** · Decisión: Aplicar
- **Ubicación**: `config/nginx.conf:39-43`
- **Acción**: `proxy_pass http://127.0.0.1:18188/system_stats;` con `auth_basic off`. Añadir `HEALTHCHECK` al Dockerfile que consulte esa ruta.

#### REL-07 — Logs de Nginx a archivo, no a stdout/stderr · **Baja** · Decisión: Aplicar
- **Ubicación**: `config/nginx.conf:3`
- **Acción**: `error_log /dev/stderr warn;` y `access_log /dev/stdout;` para que aparezcan en los logs de Vast.ai.

### 2.3 Reproducibilidad y build (REP)

#### REP-01 — Nada está fijado a versión · **Alta** · Decisión: Consultar (qué versiones fijar)
- **Ubicación**: `Dockerfile:52-53` (torch sin versión), `57` (ComfyUI `--depth=1` sin tag), `60-74` (15 paquetes sin versión), `78-87` (10 custom nodes sin commit)
- **Impacto**: Dos builds del mismo commit pueden producir imágenes distintas. Un push a `main` de cualquiera de los 11 repos upstream puede romper la imagen `latest` sin cambios en este repo. No hay forma de volver a una versión "que funcionaba".
- **Acción**:
  - ComfyUI: `git clone --depth=1 --branch vX.Y.Z` (tag estable) o `git fetch --depth=1 origin <sha> && git checkout <sha>`.
  - Custom nodes: mismo patrón con SHA; centralizar en un archivo `nodes.txt` (`url sha`) leído por un bucle.
  - torch/torchvision/torchaudio: versión explícita.
  - pip: archivo `requirements-extra.txt` con versiones y un `constraints.txt` (ver REP-02).
  - Proceso: Dependabot/Renovate no cubren esto; documentar un procedimiento manual de "bump" mensual.

#### REP-02 — `pip install -r $req || true` oculta fallos y permite que un nodo reemplace torch · **Alta** · Decisión: Aplicar
- **Ubicación**: `Dockerfile:88-91`
- **Razonamiento**: Es un patrón conocido de fallo: un `requirements.txt` de un nodo pide `torch` o `opencv-python` (no headless) o pinea `numpy<2`, y pip reinstala torch desde PyPI (rueda CUDA distinta) o deja dos `cv2` instalados. El `|| true` hace que el build salga verde aunque la mitad de los nodos no tengan sus dependencias. `ultralytics` ya arrastra `opencv-python` sobre `opencv-python-headless` (`Dockerfile:66,68`).
- **Acción**:
  - Crear `constraints.txt` con torch/torchvision/torchaudio/numpy fijados y usar `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` en todo el Dockerfile.
  - Sustituir `|| true` por un registro de fallos y `exit 1` al final si falló alguno (o al menos escribir `/etc/comfyui/failed-requirements.txt` y mostrarlo en `start.sh`).
  - Tras instalar nodos, verificar: `python3 -c "import torch; assert torch.version.cuda.startswith('12.8')"`.

#### REP-03 — Imagen de una sola etapa con toolchain de compilación · **Media** · Decisión: Consultar
- **Ubicación**: `Dockerfile:33-36,40,27,29`
- **Razonamiento**: `build-essential`, `g++`, `cmake`, `pkg-config`, `python3-venv`, `git-lfs`, `aria2`, `wget` permanecen en la imagen final. Solo se necesitan para compilar `insightface`. Impacto: imagen más pesada (más tiempo de pull en cada instancia nueva, que es el caso de uso), más superficie.
- **Acción**: Etapa `builder` que compila wheels (`pip wheel insightface -w /wheels`) y etapa final que las instala; eliminar paquetes no usados. Nota: `cuda:12.8.0-runtime` no incluye cuDNN; torch trae el suyo por pip y `onnxruntime-gpu` lo encuentra solo si torch se importa antes (ReActor lo hace). Verificar con `python3 -c "import onnxruntime as o; print(o.get_available_providers())"` que `CUDAExecutionProvider` aparece.

#### REP-04 — Custom nodes ejecutan `install.py` en el primer arranque · **Media** · Decisión: Consultar
- **Razonamiento**: Impact-Pack, controlnet_aux, Frame-Interpolation y otros ejecutan instalaciones/descargas en el primer `import`. En una instancia efímera, "primer arranque" es "cada arranque": segundos o minutos extra, y fallos silenciosos si no hay red.
- **Acción**: En el Dockerfile, tras clonar nodos, ejecutar `python3 main.py --cpu --quick-test-for-ci` para disparar los `install.py` en tiempo de build. Medir si merece la pena (puede alargar el build y descargar pesos que ya gestiona Fase 1).

#### REP-05 — Clones `--depth=1` dificultan "Update" desde Manager · **Baja** · Decisión: Consultar
- **Razonamiento**: Manager puede fallar al actualizar/cambiar versión de repos superficiales. Para instancia efímera es aceptable; si se decide persistir `/workspace` (OPS-02), deja de serlo.

### 2.4 Configuración (CFG)

#### CFG-01 — `/workspace/ComfyUI` horneado en la imagen: un volumen en `/workspace` lo oculta · **Alta** · Decisión: Consultar
- **Ubicación**: `Dockerfile:18,57,94`, `config/extra_model_paths.yaml:6`, `scripts/*.sh`
- **Razonamiento**: En Vast.ai (y RunPod) es habitual montar almacenamiento persistente en `/workspace`. Si el usuario lo hace, el montaje tapa la instalación de ComfyUI y el entrypoint falla con `main.py not found`. Además, `COMFY_DIR` existe como variable pero el Dockerfile y el YAML lo tienen hardcodeado, así que no es realmente configurable.
- **Acción propuesta**: Instalar ComfyUI en `/opt/ComfyUI` (inmutable, en imagen) y apuntar `models/`, `input/`, `output/`, `user/` a `/workspace/...` mediante `extra_model_paths.yaml` + symlinks creados en `start.sh`. Esto habilita OPS-02 sin coste adicional.

#### CFG-02 — `channel_url` apunta al repositorio antiguo de Manager · **Baja** · Decisión: Aplicar
- **Ubicación**: `config/manager-config.ini:5`
- **Acción**: `channel_url = https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main`. El propio README (línea 31) reconoce el traslado.

#### CFG-03 — `extra_model_paths.yaml` pre-registra claves de custom nodes con conjunto de extensiones vacío · **Media (verificar)** · Decisión: Consultar
- **Ubicación**: `config/extra_model_paths.yaml:34-41`
- **Razonamiento**: `folder_paths.add_model_folder_path()` de ComfyUI, al recibir una clave desconocida (`ipadapter`, `insightface`, `facerestore_models`, `facedetection`, `ultralytics_bbox`, `sams`...), la crea con `set()` de extensiones. Si el custom node luego la registra con `add_model_folder_path` en vez de asignar `folder_names_and_paths[...]` directamente, las extensiones quedan vacías y el desplegable sale **vacío**. Impact-Pack y ReActor manejan el caso; IPAdapter_plus y otros no está confirmado.
- **Además**: `base_path` es el propio directorio de ComfyUI, por lo que todas las rutas ya son las por defecto; el YAML duplica cada carpeta (doble escaneo, sin beneficio). Lo único que aporta valor es el cruce `checkpoints ↔ diffusion_models ↔ unet` y `clip ↔ text_encoders`.
- **Cómo verificar**: arrancar con `SKIP_MODEL_DOWNLOAD=1`, poner un archivo dummy `.safetensors` en `models/ipadapter` y comprobar que `IPAdapterModelLoader` lo lista.
- **Acción**: Reducir el YAML a los cruces de carpetas y eliminar las claves de custom nodes. Documentar que el cruce hace que `Load Checkpoint` muestre también archivos UNET-only que fallarán al cargar (coste de UX asumido).

#### CFG-04 — `start.sh` sobrescribe la config de Manager en cada arranque · **Baja** · Decisión: Consultar
- **Ubicación**: `scripts/start.sh:80-82`
- **Razonamiento**: Correcto para efímero; si se adopta OPS-02 (persistencia), pisará cambios del usuario. Usar `cp -n` o comprobar existencia.

### 2.5 CI/CD (CI)

#### CI-01 — El workflow no valida la imagen · **Alta** · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml`
- **Razonamiento**: Se publica `latest` sin comprobar que arranca. Combinado con REP-01/REP-02, un build roto llega a producción.
- **Acción**: Añadir job `smoke` tras el build (misma imagen, `load: true` o pull):
  ```bash
  docker run --rm -e SKIP_MODEL_DOWNLOAD=1 --entrypoint python3 $IMG -c \
    "import torch, insightface, onnxruntime, cv2; print(torch.__version__, torch.version.cuda)"
  docker run --rm --entrypoint bash $IMG -c "cd /workspace/ComfyUI && timeout 300 python3 main.py --cpu --quick-test-for-ci"
  docker run --rm --entrypoint nginx $IMG -t
  ```
  Añadir `shellcheck scripts/*.sh` y `hadolint Dockerfile` en un job previo rápido.

#### CI-02 — Caché GHA inútil para esta imagen; builds largos repetidos · **Media** · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:63-64`
- **Razonamiento**: La caché de GitHub Actions tiene límite de 10 GB por repo; las capas de torch + deps lo superan, así que `cache-to: gha,mode=max` se evicta constantemente y cada build parte de cero (30–60 min). Tampoco hay `concurrency` para cancelar builds obsoletos ni `timeout-minutes`.
- **Acción**: `cache-from/to: type=registry,ref=ghcr.io/<repo>:buildcache,mode=max` (sin límite de 10 GB). Añadir:
  ```yaml
  concurrency:
    group: ${{ github.workflow }}-${{ github.ref }}
    cancel-in-progress: true
  ```
  y `timeout-minutes: 180` en el job.

#### CI-03 — Higiene de supply chain · **Baja** · Decisión: Aplicar
- Fijar acciones por SHA (`actions/checkout@<sha>`), no por tag mayor.
- Añadir escaneo de vulnerabilidades (Trivy) en modo informativo (no bloqueante al principio; la imagen tendrá cientos de CVEs de base).
- Añadir job de validación de catálogo: para cada `repo_id/remote_path` del script, `curl -sI https://huggingface.co/<repo>/resolve/main/<path>` y comprobar 200/302 y `x-linked-size`. Esto también permite generar el tamaño real del catálogo para el README (ver DOC-02).

### 2.6 Documentación (DOC)

#### DOC-01 — Rutas locales de Windows en el README · **Alta (privacidad/roto)** · Decisión: Aplicar
- **Ubicación**: `README.md:87,159,175`
- **Evidencia**: `file:///c:/Users/estra/OneDrive/Documents/ComfyUIPod/...`
- **Impacto**: Expone nombre de usuario y estructura de disco del autor; los enlaces no funcionan en GitHub.
- **Acción**: Sustituir por rutas relativas: `[Dockerfile](Dockerfile)`, `[scripts/start.sh](scripts/start.sh)`, etc.

#### DOC-02 — Tamaños y tiempos declarados no cuadran · **Media** · Decisión: Aplicar
- **Ubicación**: `README.md:19-21,110,120,140`; `scripts/download-models.sh:9,150,204`
- **Razonamiento**:
  - Fase 2 se declara "~43 GB". Suma aproximada real: Juggernaut-XI ~7 GB, RealVisXL ~7 GB, RV6 ~2 GB, WAI ~7 GB, Illustrious ~7 GB, NoobAI ~7 GB, FLUX fp8 ~17 GB, ControlNet ProMax ~2.5 GB, IP-Adapter ~2 GB, CLIP Vision ~6 GB → **~60–65 GB**. El propio script dice "~43 GB" al inicio y "~45 GB" al final.
  - "45 GB en 1.5–3 minutos con 500 Mbps": 45 GB a 500 Mbps son ~12 minutos; 60 GB, ~16 minutos. Más el tiempo de escritura en overlayfs.
  - "Fase 1 ~2 GB en ~15 s" requiere >1 Gbps sostenido.
  - Con 120 GB de disco: imagen (~20 GB) + modelos (~62 GB) + temporales de `/tmp/hf_stage` (si `/tmp` y `/workspace` están en sistemas de archivos distintos, `mv` copia y duplica temporalmente el archivo de 17 GB) → ajustado. Recomendar 150 GB o reducir catálogo.
- **Acción**: Calcular tamaños reales vía API de HF (CI-03) y corregir README y mensajes del script. Dar tiempos como rango dependiente de `inet_down`.

#### DOC-03 — Falta documentación operativa básica · **Baja** · Decisión: Aplicar
- No hay `LICENSE`, `CHANGELOG`, `.gitignore` (el `.dockerignore` lo referencia pero no existe), ni sección "Variables de entorno" consolidada (`COMFY_USER`, `COMFY_PASS`, `HF_TOKEN`, `SKIP_MODEL_DOWNLOAD`, `COMFYUI_EXTRA_ARGS`, `COMFY_DIR`).
- La sección 2 del README ("Investigación Web Verificada") mezcla decisiones de diseño con notas de investigación fechadas; mover a `docs/decisiones.md` para que el README sea solo guía de uso.
- Falta aviso de que Vast.ai es facturación por hora y que la instancia debe **destruirse** (no solo pararse) para no pagar almacenamiento.

### 2.7 Operación y repositorio (OPS)

#### OPS-01 — La carpeta no es un repo git y está en OneDrive · **Alta** · Decisión: Consultar
- **Evidencia**: `git rev-parse --abbrev-ref HEAD` → `not a git repository`. Ruta bajo `OneDrive\Documents`.
- **Impacto**: Sin historial, sin posibilidad de revertir, y OneDrive puede provocar bloqueos de archivo y conflictos de sincronización (especialmente con `.git/` si se inicializa dentro). El README asume que el contenido se sube a GitHub, pero no hay rastro de ello aquí.
- **Acción**: `git init` + primer commit, o clonar el repo remoto si ya existe en GitHub y trabajar desde una carpeta fuera de OneDrive. Confirmar con el dueño dónde vive la fuente de verdad.

#### OPS-02 — Sin opción de persistencia: 60 GB se descargan en cada instancia · **Media** · Decisión: Consultar
- **Razonamiento**: Es una decisión de diseño explícita ("despliegue efímero"), pero el coste es real: 10–20 minutos de GPU facturada por arranque solo descargando. Vast.ai ofrece volúmenes persistentes; si CFG-01 se implementa, soportar `/workspace` como volumen es gratis y los `SKIP (ya existe)` del script ya harían el resto.
- **Opciones**: (a) mantener efímero y documentar el coste; (b) soportar volumen opcional; (c) publicar una segunda imagen "con modelos horneados" (imagen de >80 GB; pull lento, no recomendado).

#### OPS-03 — Salida de usuario (`output/`) se pierde al destruir la instancia · **Baja** · Decisión: Consultar
- **Acción opcional**: variable `RCLONE_REMOTE`/`S3_BUCKET` y un `rclone sync` periódico en background; o simplemente documentar con claridad el riesgo.

#### OPS-04 — Todo corre como root · **Baja** · Decisión: Consultar
- Habitual en imágenes de ComfyUI; cambiarlo complica permisos con volúmenes. Documentar como riesgo aceptado o planificar un usuario `comfy` con UID 1000.

---

## 3. Decisiones que requieren al dueño del proyecto

El agente implementador debe obtener respuesta a estas preguntas antes de tocar los puntos marcados `Consultar`:

1. **Contraseña por defecto** (SEC-02): ¿abortar si falta, generar aleatoria, o mantener `comfyui`?
2. **Layout de directorios** (CFG-01 / OPS-02): ¿mover ComfyUI a `/opt/ComfyUI` y dejar `/workspace` para datos, habilitando volúmenes persistentes? Afecta README, YAML, scripts y Dockerfile a la vez; conviene hacerlo en un solo cambio.
3. **Política de versiones** (REP-01): ¿fijar por SHA (máxima reproducibilidad, más mantenimiento) o por tag/rama estable (menos mantenimiento, algo de deriva)? ¿Quién hace el bump y con qué cadencia?
4. **Catálogo de modelos** (DOC-02): con ~62 GB reales, ¿se mantiene todo, se recorta (p. ej., quitar FLUX o un checkpoint anime redundante), o se hace configurable por variable (`MODEL_PROFILE=minimal|photo|anime|all`)?
5. **Multi-stage** (REP-03): ¿vale la pena el trabajo para reducir tamaño de imagen? Depende de cuánto pese hoy la imagen (medir tras el primer build).
6. **Repositorio** (OPS-01): ¿dónde está la fuente de verdad? ¿Se inicializa git aquí o se clona desde GitHub a otra ruta?

---

## 4. Verificaciones pendientes (no realizadas en esta auditoría)

| # | Qué verificar | Cómo | Relacionado |
|---|---|---|---|
| V1 | Usuario real de los workers de Nginx y lectura de `.htpasswd` | `docker run ... /start.sh` con `SKIP_MODEL_DOWNLOAD=1`; `ps -o user,cmd -C nginx`; `curl -u admin:comfyui localhost:8188/` debe dar 200, no 500 | REL-01 |
| V2 | Existencia y tamaño de cada `repo_id/remote_path` en HuggingFace | `curl -sI https://huggingface.co/<repo>/resolve/main/<path>` (añadir `/datasets/` para `repo_type=dataset`) | CI-03, DOC-02 |
| V3 | `onnxruntime-gpu` ve `CUDAExecutionProvider` con cuDNN de torch | `python3 -c "import torch, onnxruntime as o; print(o.get_available_providers())"` en GPU | REP-03 |
| V4 | Desplegables de IPAdapter/ReActor/Impact no vacíos con el YAML actual | Arranque con dummy files, ver CFG-03 | CFG-03 |
| V5 | Qué `requirements.txt` fallan realmente en el bucle `|| true` | Build con `set -x` o quitando `|| true` temporalmente | REP-02 |
| V6 | Tamaño final de la imagen y tiempo de pull en Vast.ai | `docker images`, cronometrar primer arranque | REP-03 |
| V7 | Que `hf` (sin `huggingface-cli`) está disponible con la versión instalada de `huggingface_hub` | `docker run --rm --entrypoint hf $IMG version` | REL-03 |
| V8 | Falsos `[WARN] libx264/h264_nvenc no detectado` en el log de arranque | Revisar log del primer arranque | REL-02 |

---

## 5. Lo que está bien y no debe cambiarse

- ComfyUI escucha solo en `127.0.0.1:18188`; el único puerto expuesto es Nginx. Mantener.
- Manager en `security_level = weak` funciona precisamente **porque** ComfyUI escucha en loopback (Manager considera acceso local). Si alguien cambia `--listen 0.0.0.0`, Manager bloqueará instalaciones. Documentar esta dependencia.
- Config de Manager copiada a las dos rutas (`user/__manager` y `user/default/ComfyUI-Manager`) cubre versiones antiguas y nuevas. Mantener hasta fijar versión de Manager.
- `/healthz` sin auth, `/download-status` con auth y `client_max_body_size 0` + WebSocket upgrade + `proxy_buffering off`: configuración correcta para ComfyUI.
- Normalización de EOL doble (`.gitattributes` + `sed -i 's/\r$//'`): redundante pero defensiva; correcta para un repo editado desde Windows.
- Detección `hf` / `huggingface-cli` con fallback y `SKIP (ya existe)` idempotente en `hf_fetch`.
- `--index-url .../cu128` para torch: correcto para Ada y Blackwell.
- Variables `VHS_FORCE_FFMPEG_PATH` / `VHS_USE_IMAGEIO_FFMPEG=0` / `NVIDIA_DRIVER_CAPABILITIES=all`: resuelven un problema real y documentado.

---

## 6. Plan de ejecución sugerido

### Fase A — Quick wins (sin cambios de comportamiento visibles, ~2 h)
1. SEC-01: quitar contraseña del log.
2. SEC-03: quitar `--enable-cors-header`.
3. SEC-04: `htpasswd -i` por stdin; quitar `--token`.
4. REL-01: `user www-data;` en `nginx.conf`.
5. REL-02: capturar salida de ffmpeg en variable.
6. REL-03: Fase 1 no fatal + `timeout`.
7. CFG-02: `channel_url` a Comfy-Org.
8. DOC-01: enlaces relativos en README.
9. REL-07, SEC-06: logs a stdout, `limit_req`.

### Fase B — Reproducibilidad y CI (~1 día)
1. REP-01 + REP-02: `constraints.txt`, versiones fijadas, `nodes.txt`, sin `|| true`.
2. CI-01: job de smoke test + shellcheck + hadolint.
3. CI-02: caché en registry, `concurrency`, `timeout-minutes`.
4. CI-03: validación de catálogo HF + tamaños reales.
5. DOC-02: corregir cifras con los datos de CI-03.
6. REL-04: reintentos en `hf_fetch` + resumen de fallos.
7. REL-05: `tini`.

### Fase C — Cambios estructurales (requieren decisión, 1–3 días)
1. CFG-01 + OPS-02: layout `/opt/ComfyUI` + `/workspace` para datos + volumen opcional.
2. CFG-03: simplificar `extra_model_paths.yaml` tras verificar V4.
3. SEC-02: política de contraseña.
4. REP-03 / REP-04: multi-stage y warm-up de `install.py` si V6 lo justifica.
5. DOC-03: `LICENSE`, `.gitignore`, tabla de variables, `docs/decisiones.md`.

### Checklist de validación tras cada fase
- [ ] `docker build` completa sin `|| true` silenciando errores.
- [ ] Smoke test de CI-01 en verde.
- [ ] Arranque con `SKIP_MODEL_DOWNLOAD=1`: Nginx responde 401 sin credenciales y 200 con ellas; `/healthz` 200; `/download-status` 200 con auth.
- [ ] Log de arranque sin contraseña y sin falsos `[WARN]` de ffmpeg.
- [ ] Arranque completo en Vast.ai (RTX 4090): Fase 1 termina, UI abre, Fase 2 progresa en `/download-status`, `VHS_VideoCombine` con `h264_nvenc` funciona, ReActor encuentra `buffalo_l` e `inswapper_128.onnx`.
- [ ] README sin rutas locales y con cifras que coinciden con el catálogo real.
