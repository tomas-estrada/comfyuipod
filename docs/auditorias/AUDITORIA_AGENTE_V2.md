# Auditoría de Agente — Versión 2

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha | 2026-10-16 |
| Alcance | Re-auditoría tras aplicar `AUDITORIA_AGENTE_V1.md`. Revisión estática de los 15 archivos del repo **más verificación activa contra la red** (GitHub API, `git ls-remote`, `raw.githubusercontent.com`, HuggingFace `HEAD`). **No se construyó la imagen Docker ni se ejecutó el contenedor.** |
| Documentos de referencia | `docs/decisiones.md` (leído). `walkthrough.md` **no existe en el repositorio** (ver INF-01). |
| Destinatario | Agente implementador. Las acciones marcadas `Aplicar` no necesitan consulta; las marcadas `Consultar` sí. |

---

## 0. Veredicto

**El repositorio NO está listo para publicar.** La mejora respecto a V1 es sustancial (22 de 29 hallazgos resueltos o resueltos con matices), pero los cambios introdujeron **dos defectos bloqueantes nuevos** que impiden que el pipeline llegue a producir una imagen:

1. **CRIT-01** — El SHA fijado para `hadolint/hadolint-action` no existe (GitHub API devuelve 422). El job `validate` falla al resolver la acción, y como `build-and-push` tiene `needs: validate`, **nunca se construye nada**.
2. **CRIT-02** — En el `Dockerfile`, `pip3 uninstall -y opencv-python` borra los ficheros de `cv2/` que comparten todas las variantes de OpenCV, y el `pip3 install --upgrade opencv-python-headless` posterior es un no-op si ya está en la última versión. El `python3 -c "import cv2"` de la línea 111 falla y **el build termina en error**. Confirmado que `opencv-python` entra sí o sí por `ultralytics` y `facexlib` (paso 3) y `opencv-contrib-python` por `mediapipe` (paso 4).

Además hay dos defectos de **alta** severidad en el CI que, una vez arreglados los anteriores, harían que la imagen se publique como `latest` **antes** de pasar el smoke test, y que el smoke test falle de todos modos para cualquier repo con mayúsculas en el nombre (este se llama `ComfyUIPod`).

Corregir CRIT-01, CRIT-02, ALTA-01 y ALTA-02 es trabajo de menos de una hora. Después de eso, el repo queda en condiciones de hacer el primer build real, que es el único paso que puede cerrar las verificaciones pendientes de la sección 5.

---

## 1. Evidencia de verificación activa

Lo que se comprobó contra la red en esta auditoría (todo de solo lectura):

| Comprobación | Resultado |
|---|---|
| Los 11 SHAs de `config/nodes.lock` existen en GitHub | **11/11 OK.** Todos coinciden con el `HEAD` actual de cada repo (`git ls-remote`). Es decir, "fijado" = "lo último a fecha 2026-10-16", no una versión estable probada. |
| `comfyanonymous/ComfyUI` | La API devuelve **301** (el repo fue transferido a `Comfy-Org/ComfyUI`). El clon sigue funcionando por redirección, pero conviene actualizar la URL en `nodes.lock` antes de que GitHub deje de redirigir. |
| SHAs de las 6 GitHub Actions vs. tag comentado | **5/6 coinciden.** `hadolint/hadolint-action@f9a9afe9…` **no existe** (422). El SHA real de `v3.1.0` es `54c9adbab1582c2ef04b2016b760714a4bfde3cf`. |
| Las 26 URLs de HuggingFace (`HEAD` sobre `/resolve/main/`) | **26/26 devuelven 302** (existen, no gated). `flux1-dev-fp8.safetensors` reporta `X-Linked-Size: 17246524772` = 16.06 GiB, coincide con el README. |
| `requirements.txt` real de cada nodo en su SHA fijado | Descargados los 10. Hallazgos relevantes en CRIT-02, ALTA-03, MED-03 y MED-05. `ComfyUI_IPAdapter_plus` y `ComfyUI-Frame-Interpolation` **no tienen** `requirements.txt` (404). |
| Estado git local | `git init` hecho, rama `main`, **0 commits**, 100 % de los archivos sin trackear. Sigue dentro de OneDrive. |

---

## 2. Estado de los hallazgos de V1

| ID V1 | Estado | Comentario |
|---|---|---|
| SEC-01 contraseña en logs | ✅ Resuelto | Solo se imprime si es autogenerada. |
| SEC-02 contraseña por defecto | ✅ Resuelto | Opción (b): 20 caracteres aleatorios. Ver BAJA-01 por el método de generación. |
| SEC-03 CORS abierto | ✅ Resuelto | Flag eliminado. |
| SEC-04 secretos en argv | ✅ Resuelto | `htpasswd -i` por stdin; `HF_TOKEN` desde entorno. |
| SEC-05 HTTP plano | ✅ Documentado | Nota en README paso 2. |
| SEC-06 fuerza bruta | ⚠️ Con matices | `limit_req` añadido, pero con parámetros que pueden romper la carga de la UI. Ver MED-01. |
| REL-01 `user www-data;` | ✅ Resuelto | |
| REL-02 SIGPIPE en ffmpeg | ✅ Resuelto | |
| REL-03 Fase 1 fatal | ✅ Resuelto | `timeout 600` + no fatal + Nginx antes. CLI ausente ahora `exit 0`. |
| REL-04 reintentos | ✅ Resuelto | 3 intentos con backoff, resumen de fallos, `aria2` eliminado. |
| REL-05 `tini` | ✅ Resuelto | Ver BAJA-10: solo aplica en modo "Docker Entrypoint" de Vast.ai. |
| REL-06 `/healthz` real | ✅ Resuelto | Ver BAJA-02 y BAJA-03. |
| REL-07 logs a stdout | ✅ Resuelto | |
| REP-01 versiones fijadas | ⚠️ Parcial | Repos git: sí (por SHA). **`torch`/`torchvision`/`torchaudio` siguen sin versión**; `constraints.txt` solo tiene cotas inferiores (`>=`), que no fijan nada; `sam2` se instala desde `git+https://…` sin SHA. Ver ALTA-05. |
| REP-02 `\|\| true` y conflictos | ⚠️ Parcial, con regresión | `\|\| true` eliminado y filtrado de líneas correcto, pero la limpieza final de `cv2`/`onnxruntime` está rota. Ver CRIT-02. |
| REP-03 toolchain en imagen | ⚠️ Con matices | Se purga en la misma capa (equivalente a multi-stage en tamaño), pero se purga **antes** del paso que instala las dependencias de los nodos, y deja la imagen sin compilador en runtime. Ver ALTA-03. |
| REP-04 `install.py` en arranque | ❌ Pendiente | No abordado ni mencionado en `docs/decisiones.md`. Ver MED-03. |
| REP-05 clones superficiales | ⚠️ Documentar | Ahora ComfyUI está en `FETCH_HEAD` desligado sin rama. Ver BAJA-09. |
| CFG-01 `/workspace` | ✅ Resuelto | `/opt/ComfyUI` + `/workspace/comfyui-data`. Ver MED-04 por `models/configs`. |
| CFG-02 `channel_url` | ✅ Resuelto | |
| CFG-03 YAML | ✅ Resuelto | Reducido a los cruces. |
| CFG-04 pisar config Manager | ✅ Resuelto | `cp` condicional. |
| CI-01 validar imagen | ⚠️ Parcial, con regresión | Hay smoke test, pero corre **después** del `push` y usa un tag que falla con mayúsculas. No ejecuta `--quick-test-for-ci`. Ver ALTA-01, ALTA-02, MED-03. |
| CI-02 caché y concurrencia | ✅ Resuelto | Caché en registry, `concurrency`, `timeout-minutes`. |
| CI-03 supply chain | ⚠️ Con regresión | Acciones fijadas por SHA, pero uno es inválido. Ver CRIT-01. Validación de catálogo HF: correcta. |
| DOC-01 rutas locales | ✅ Resuelto | |
| DOC-02 cifras | ✅ Casi | Tamaños reales en README. Quedan incoherencias menores entre cabecera del script y README. Ver BAJA-06. |
| DOC-03 LICENSE, .gitignore, variables | ✅ Resuelto | |
| OPS-01 git / OneDrive | ⚠️ Parcial | `git init` hecho, pero sin commits y aún en OneDrive. Ver ALTA-04. |
| OPS-02 persistencia | ✅ Resuelto | Volumen en `/workspace` soportado. |
| OPS-03 pérdida de `output/` | ✅ Documentado | README paso 4.5. |
| OPS-04 root | ❌ Pendiente | Ni cambiado ni documentado. Ver BAJA-15. |

---

## 3. Hallazgos nuevos

### 3.1 Críticos (bloquean el pipeline)

#### CRIT-01 — SHA inexistente en `hadolint/hadolint-action` · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:38`
- **Evidencia**: `GET /repos/hadolint/hadolint-action/commits/f9a9afe9ce67fb29a4f8ebc061be7fa2f37e262a` → **422**. El tag `v3.1.0` apunta a `54c9adbab1582c2ef04b2016b760714a4bfde3cf`.
- **Impacto**: GitHub Actions falla con "Unable to resolve action" antes de ejecutar ningún paso del job `validate`. `build-and-push` depende de `validate`, así que no se construye ni publica nada. Cualquier push a `main` termina en rojo.
- **Acción**: Sustituir por `hadolint/hadolint-action@54c9adbab1582c2ef04b2016b760714a4bfde3cf # v3.1.0`. Añadir al procedimiento de bump una verificación automática: `git ls-remote https://github.com/<owner>/<action> refs/tags/<tag>` debe coincidir con el SHA fijado (los otros 5 SHAs sí son correctos).

#### CRIT-02 — La limpieza de `cv2`/`onnxruntime` deja `cv2` roto y el build falla · Decisión: Aplicar
- **Ubicación**: `Dockerfile:107-108,111`
- **Cadena de hechos verificada**:
  1. Paso 3 instala `ultralytics` (requiere `opencv-python>=4.6.0`) y `facexlib` (requiere `opencv-python`). Ambos instalan `opencv-python` encima de `opencv-python-headless`. Los ficheros de `cv2/` son los mismos nombres en todas las variantes.
  2. Paso 4: `comfyui_controlnet_aux` requiere `mediapipe>=0.8.0`, que arrastra `opencv-contrib-python`. Tercera variante escribiendo en `cv2/`.
  3. Línea 107: `pip3 uninstall -y onnxruntime opencv-python` elimina todos los ficheros listados en el `RECORD` de `opencv-python`, es decir, todo `cv2/`.
  4. Línea 108: `pip3 install --upgrade opencv-python-headless`. Si la versión instalada ya es la última (lo normal, se instaló horas antes), pip responde "Requirement already satisfied" y **no restaura nada**.
  5. Línea 111: `import cv2` → `ModuleNotFoundError` → el `RUN` falla.
- **Nota**: `onnxruntime` (CPU) no lo instala ningún paquete del árbol actual (`insightface` no lo requiere; `controlnet_aux` pide `onnxruntime-gpu`), así que ese `uninstall` es hoy un no-op. Pero la misma trampa aplicaría si algún nodo lo trajera.
- **Acción**: Sustituir las líneas 107-108 por una limpieza completa y reinstalación forzada sin dependencias:
  ```dockerfile
  && pip3 uninstall -y opencv-python opencv-contrib-python opencv-python-headless opencv-contrib-python-headless onnxruntime onnxruntime-gpu || true \
  && pip3 install --no-deps --force-reinstall opencv-python-headless onnxruntime-gpu \
  ```
  Y mantener el `import cv2` de la línea 111 como guardia (ya lo es; por eso este fallo se detectaría en build y no en producción).

### 3.2 Altos

#### ALTA-01 — El CI publica `latest` antes de ejecutar el smoke test · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:114-131`
- **Impacto**: `build-push-action` con `push: true` sube todos los tags (incluido `latest`) y solo después se ejecuta el smoke test. Si falla, `latest` ya está roto en GHCR y Vast.ai lo descargará. Esto invierte el objetivo de CI-01.
- **Acción** (una de las dos):
  - (a) Construir con `push: false, load: true`, ejecutar el smoke test sobre la imagen local y, si pasa, un segundo `build-push-action` con `push: true` (reutiliza la caché, cuesta segundos). Cuidado con el disco del runner: una imagen de 15-20 GB cargada en el daemon de Docker más la caché de BuildKit se acerca al límite; medir en el primer build.
  - (b) Publicar solo el tag `sha-…` en el primer push, ejecutar el smoke test, y promover a `latest`/semver con `docker buildx imagetools create -t <latest> <sha>`. No duplica disco y es lo recomendable aquí.

#### ALTA-02 — El smoke test usa el nombre del repo sin pasar a minúsculas · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:126`
- **Evidencia**: `IMAGE_TAG="${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:sha-…"` con `IMAGE_NAME: ${{ github.repository }}`. `metadata-action` normaliza a minúsculas los tags que publica, pero esta línea usa el valor crudo. Para `usuario/ComfyUIPod` resulta `ghcr.io/usuario/ComfyUIPod:sha-…` → `docker pull` falla con "repository name must be lowercase".
- **Acción**: `IMAGE_TAG="$(echo '${{ steps.meta.outputs.tags }}' | grep ':sha-' | head -n1)"` o, más limpio, `${{ fromJSON(steps.meta.outputs.json).tags[1] }}` tras comprobar el orden. Cualquier solución que no derive el nombre a mano.

#### ALTA-03 — El toolchain se purga antes de instalar las dependencias de los nodos, y la imagen queda sin compilador · Decisión: Consultar
- **Ubicación**: `Dockerfile:84` (purga) vs. `Dockerfile:91-106` (instalación de requirements de nodos) y `Dockerfile:68` (`sam2` desde git vía Impact-Pack)
- **Problema 1 (orden)**: El paso 4 instala ~40 paquetes con sus transitivas (`mediapipe`, `rtree`, `trimesh`, `albumentationsx`, `scikit-image`, `matrix-nio`, `PyGithub`, `colour-science`, `git+sam2`, …) ya sin `gcc`/`g++`/`python3-dev`. Hoy todos parecen tener wheel para Python 3.12 x86_64, pero cualquier bump futuro que caiga en un sdist rompe el build de forma no obvia. Lo coherente es purgar al final del paso 4, no al final del paso 3.
- **Problema 2 (runtime)**: Sin `gcc` ni `Python.h`, fallan en tiempo de ejecución:
  - `torch.compile` / Triton (necesita un compilador C para el launcher). Afecta a nodos de KJNodes (`TorchCompileModel*`) y a cualquier workflow que use compile.
  - Instalaciones desde ComfyUI-Manager de nodos cuyas dependencias no tienen wheel (`sageattention`, `dlib`, `pycocotools` en algunas versiones, `xformers` compilado, etc.). El usuario verá un error críptico de pip en la UI.
- **Coste de mantenerlos**: `gcc` + `python3-dev` ≈ 150-200 MB. `g++`+`cmake` ≈ 100 MB más. Frente a una imagen de 15-20 GB es ruido.
- **Recomendación**: mover la purga al final del paso 4 y purgar solo `cmake` y `pkg-config`; conservar `build-essential` y `python3-dev`. Si el dueño prioriza tamaño, al menos documentar en README que `torch.compile` y nodos con compilación nativa no funcionan.

#### ALTA-04 — Repositorio sin commits, todo sin trackear, dentro de OneDrive · Decisión: Consultar
- **Evidencia**: `git status --short` muestra `??` en todos los archivos; `git log` → "does not have any commits yet".
- **Impacto**: No hay punto de retorno. Si OneDrive sincroniza mal `.git/` (ocurre con archivos de pack y locks), el repo se corrompe. Ninguna de las mejoras de esta ronda está protegida.
- **Acción**: Decidir dónde vive la fuente de verdad, hacer el primer commit (sin los `AUDITORIA_*.md` si se quieren mantener fuera, o moverlos a `docs/auditorias/`), y mover la carpeta de trabajo fuera de OneDrive (o excluir `.git/` de la sincronización).

#### ALTA-05 — La reproducibilidad declarada es mayor que la real · Decisión: Consultar (política de versiones)
- **Ubicación**: `Dockerfile:59-60,68,70-83`; `config/constraints.txt`; `docs/decisiones.md:30`
- **Hechos**:
  - `pip3 install --upgrade pip setuptools wheel` y `pip3 install torch torchvision torchaudio` **sin versión**. Un build mañana puede traer torch 2.x+1 con comportamiento distinto en Blackwell.
  - `constraints.txt` usa solo `>=`. Un constraint con cota inferior no evita derivas; solo evita downgrades por debajo de ese valor.
  - `ComfyUI-Impact-Pack` requiere `git+https://github.com/facebookresearch/sam2` sin SHA: cada build clona el HEAD de sam2. Además, sam2 declara `torch` como dependencia de **build** (`build-system.requires`), así que pip, con aislamiento de build activo, descarga un torch de PyPI (~2.5 GB con sus paquetes `nvidia-*`) solo para construirlo. No contamina el entorno final, pero duplica tiempo y disco del runner.
  - Los 11 SHAs están en el HEAD de hoy. Es un "congelado", no una "versión estable". ComfyUI-Manager y KJNodes tienen commits de hace horas.
  - `docs/decisiones.md` afirma "todos los repositorios fijados" y "restricciones de versión". Es cierto para git y falso para pip.
- **Acción**:
  - Fijar `torch==`, `torchvision==`, `torchaudio==` (la terna debe ser coherente; tomar la que publique `download.pytorch.org/whl/cu128` en el momento del primer build verde) y volcar el resto del entorno con `pip freeze` a un `config/requirements.lock` generado en CI como artefacto, para que el siguiente bump sea comparable.
  - Para sam2: `PIP_NO_BUILD_ISOLATION=1` solo en esa línea, o preinstalar `sam2` fijado por SHA antes de los nodos y filtrar la línea `git+` del `requirements.txt` de Impact-Pack con la misma regex del paso 4.
  - Corregir la redacción de `docs/decisiones.md` para que describa lo que realmente está fijado.

### 3.3 Medios

#### MED-01 — `limit_req` puede devolver 503 durante la carga inicial de la UI · Decisión: Aplicar
- **Ubicación**: `config/nginx.conf:21,62`
- **Razonamiento**: La frontend de ComfyUI (`comfyui-frontend-package 1.55`) hace decenas de peticiones en los primeros segundos (chunks JS, CSS, fuentes, `/object_info`, `/settings`, `/userdata/*`, plantillas). Con `rate=30r/s burst=60 nodelay`, la petición 61 que llegue dentro del mismo segundo recibe **503** y el chunk JS correspondiente no carga: UI parcialmente rota, sin mensaje claro. Varias personas detrás de la misma IP (NAT de oficina) comparten el cubo.
- **Además**: 30 intentos/segundo = 2,6 millones de intentos/día. Como protección contra fuerza bruta es cosmética; la protección real es la contraseña de 20 caracteres aleatorios.
- **Acción**: `rate=50r/s burst=300 nodelay; limit_req_status 429; limit_req_log_level warn;`. Opcional: arrancar con `limit_req_dry_run on;` una sesión para medir el pico real antes de ajustar. No aplicar el límite a `/ws`.

#### MED-02 — Cambio de público objetivo de RTX 4090 a RTX 5090 no solicitado · Decisión: Consultar
- **Ubicación**: `README.md:1-3,133-141`, `Dockerfile:2,6`, `docs/decisiones.md:11`
- **Razonamiento**: V1 no recomendó cambiar la GPU objetivo. El README original estaba diseñado para RTX 4090 (más barata, ~$0.30-0.55/h) y ahora dirige al usuario a RTX 5090 (~$0.45-0.95/h) con filtro `cuda_max_good>=12.8`. La imagen funciona en ambas (cu128 cubre `sm_89` y `sm_120`), pero es una decisión de producto y de coste que debe tomar el dueño, no el agente.
- **Acción**: Confirmar con el dueño. Si se mantiene 5090, añadir una línea en README diciendo que RTX 4090 sigue soportada y cuándo elegirla. Si se vuelve a 4090, revertir textos y filtros (y el comentario de VRAM en la sección 5.4).

#### MED-03 — Dependencias de nodos sin `requirements.txt` y warm-up de `install.py` siguen sin cubrir · Decisión: Aplicar
- **Ubicación**: `Dockerfile:98` (solo mira `requirements.txt`); `.github/workflows/docker-publish.yml:124-131`
- **Hechos**: `ComfyUI-Frame-Interpolation` no tiene `requirements.txt`; tiene `requirements-no-cupy.txt` y `requirements-with-cupy.txt` (este último trae `cupy-cuda12x`, necesario para varios modelos de interpolación) y un `install.py`. `ComfyUI_IPAdapter_plus` no tiene ninguno (sus deps ya están). Impact-Pack y controlnet_aux ejecutan lógica de instalación en el primer import.
- **Impacto**: En cada arranque de instancia efímera se dispara instalación/descarga con red, o el nodo falla al importar sin que nadie lo vea hasta abrir un workflow.
- **Acción**:
  1. En el bucle del paso 4, tratar también `requirements-no-cupy.txt` (y decidir si se quiere `cupy-cuda12x`, ~100 MB).
  2. Añadir al final del paso 4 un warm-up: `cd /opt/ComfyUI && python3 main.py --cpu --quick-test-for-ci 2>&1 | tee /tmp/boot.log && ! grep -q "IMPORT FAILED" /tmp/boot.log`. Esto ejecuta los `install.py`, cachea lo que se pueda y **hace fallar el build si algún nodo no importa**.
  3. Repetir ese mismo comando en el smoke test del CI (ahora solo prueba `import torch, cv2…`, que no detecta nodos rotos).

#### MED-04 — `rm -rf /opt/ComfyUI/models` borra `models/configs/*.yaml` · Decisión: Aplicar
- **Ubicación**: `Dockerfile:140`
- **Razonamiento**: ComfyUI distribuye en `models/configs/` los YAML de arquitectura (`v1-inference.yaml`, `v2-inference-v.yaml`, `anything_v3.yaml`, etc.) que usa el nodo `CheckpointLoader` (el legado, con selector de config) y algunos custom nodes. Al borrar el directorio y enlazarlo a `/workspace/comfyui-data/models`, esa carpeta desaparece y el desplegable queda vacío.
- **Acción**: Antes del `rm -rf`, `cp -a /opt/ComfyUI/models/configs /workspace/comfyui-data/models/configs`. Añadir `models/configs` a la lista de `mkdir -p` de `start.sh` no basta: hay que copiar el contenido.

#### MED-05 — Compatibilidad con `huggingface_hub >= 1.0` no verificada · Decisión: Verificar
- **Ubicación**: `Dockerfile:70`, `config/constraints.txt:6`, `scripts/download-models.sh:29`
- **Razonamiento**: `huggingface_hub[cli,hf_transfer]>=0.30.0` resolverá a la serie 1.x (publicada en 2025). En 1.x el comando `huggingface-cli` desaparece (solo `hf`, que el script ya contempla) y el soporte de `hf_transfer` quedó deprecado a favor de `hf_xet`, que es el backend por defecto. Es posible que el extra `[hf_transfer]` no exista (pip solo avisa) y que `HF_HUB_ENABLE_HF_TRANSFER=1` sea ignorado. No rompe nada, pero la documentación promete una aceleración que puede no estar activa.
- **Acción**: En el smoke test, ejecutar `hf version` (ya está) y `python3 -c "import huggingface_hub as h, hf_xet; print(h.__version__)"`. Si es 1.x, quitar `hf_transfer` de extras, variables y README, y mencionar `hf_xet`.

### 3.4 Bajos

#### BAJA-01 — Generación de contraseña con `head -c` bajo `pipefail` + `errexit` · Aplicar
- `scripts/start.sh:117`. `head -c 20` cierra la tubería; si `tr` todavía escribe, recibe SIGPIPE, la sustitución de comando devuelve 141 y, al estar en una asignación bajo `set -e`, el script muere sin mensaje. La ventana es muy pequeña (32 bytes caben en un `write`), pero existe, y la solución es más simple: `COMFY_PASS="$(openssl rand -hex 16)"` (32 caracteres, sin filtrado, sin tubería).

#### BAJA-02 — `HEALTHCHECK --start-period=120s` menor que el `timeout 600` de Fase 1 · Aplicar
- `Dockerfile:166`. ComfyUI no arranca hasta que Fase 1 termina o agota 600 s. Con `start-period=120s`, `interval=30s`, `retries=3`, el contenedor se marca `unhealthy` a los ~210 s en cualquier red lenta. Vast.ai no actúa sobre el healthcheck de Docker, así que el daño es solo informativo, pero el valor es incoherente. Subir a `--start-period=900s`.

#### BAJA-03 — `/healthz` expone `/system_stats` sin autenticación · Consultar
- `config/nginx.conf:44-49`. La respuesta incluye modelo de GPU, VRAM, versión de Python/PyTorch/ComfyUI y SO. Es información útil para un atacante que esté enumerando instancias. Alternativa que mantiene el healthcheck real: `proxy_pass` a `/system_stats` pero con `proxy_method HEAD`, o `return 200` condicionado a un `proxy_pass` interno con `error_page`. Si se acepta el riesgo, documentarlo.

#### BAJA-04 — `MODEL_PROFILE` no se valida · Aplicar
- `scripts/download-models.sh:28`. `MODEL_PROFILE=photo` (typo) descarga solo ControlNet + IP-Adapter en silencio. Añadir al inicio: `case "$MODEL_PROFILE" in all|general|realistic|anime|minimal) ;; *) log "WARN: MODEL_PROFILE='$MODEL_PROFILE' desconocido; usando 'all'"; MODEL_PROFILE=all ;; esac`.

#### BAJA-05 — `COMFY_DATA_DIR` se anuncia configurable pero el YAML está fijo · Aplicar
- `README.md:95` vs `config/extra_model_paths.yaml:10` (`base_path: /workspace/comfyui-data/`) y `Dockerfile:116-145`. Si el usuario cambia `COMFY_DATA_DIR`, `start.sh` re-enlaza `/opt/ComfyUI/{models,…}` correctamente, pero el YAML sigue apuntando a la ruta antigua. Generar el YAML en `start.sh` con `sed "s#/workspace/comfyui-data/#${COMFY_DATA_DIR%/}/#"` o quitar la variable del README.

#### BAJA-06 — Tamaños por perfil incoherentes · Aplicar
- `scripts/download-models.sh:15-17` dice `general ~26.5`, `realistic ~26.4`, `anime ~31.0` GiB. Sumando la tabla del README: `general` = 24.96, `realistic` = 24.96, `anime` = 29.43 GiB (Fase 2). `README.md:92` da `anime` = 32.39 GiB total; la suma real es 32.22. Alinear los tres sitios (o mejor: que la cabecera del script no repita cifras y remita al README).

#### BAJA-07 — Durante Fase 1, `/` devuelve un 502 crudo · Aplicar
- README paso 4.1 dice que el botón **Open** mostrará el progreso, pero **Open** abre `/`, no `/download-status`, y durante Fase 1 Nginx responde "502 Bad Gateway". Añadir en `location /` un `error_page 502 =503 /_starting.html;` con una página estática mínima (`<meta http-equiv="refresh" content="10">`, enlace a `/download-status`). Diez líneas y mejora mucho la primera impresión.

#### BAJA-08 — `rm -rf "${STAGE_DIR}"` al final de Fase 2 borra los `.incomplete` de descargas fallidas · Aplicar
- `scripts/download-models.sh:256`. Si un modelo falló tras 3 intentos, su parcial en `.hf_stage/.cache/huggingface/` permitiría reanudar en el siguiente arranque con volumen persistente. Borrar solo si `${#FAILED_MODELS[@]} -eq 0`.

#### BAJA-09 — ComfyUI en `FETCH_HEAD` desligado: "Update ComfyUI" desde Manager fallará · Documentar
- `Dockerfile:61-67`. No hay rama local ni `origin/main`. El botón de actualizar de Manager intentará `git pull` sobre HEAD desligado. Coherente con un entorno efímero y `update_policy = stable-comfyui`, pero debe decirse en README ("para actualizar ComfyUI, cambia el SHA en `nodes.lock` y reconstruye").

#### BAJA-10 — En modo "SSH" de Vast.ai no actúan `tini` ni `HEALTHCHECK` · Documentar
- `README.md:122`. En ese modo Vast.ai sustituye el entrypoint y ejecuta `/start.sh` como on-start script. Recomendar "Docker Entrypoint" como modo principal y dejar SSH como alternativa.

#### BAJA-11 — README enlaza a `AUDITORIA_AGENTE_V1.md` en la raíz · Aplicar
- `README.md:5`. Los informes de auditoría van a quedar obsoletos en cada iteración. Mover `AUDITORIA_AGENTE_V*.md` a `docs/auditorias/` y enlazar solo a `docs/decisiones.md`.

#### BAJA-12 — `PIP_CONSTRAINT` persiste en runtime · Documentar
- `Dockerfile:13`. Es una decisión razonable (protege el entorno cuando Manager instala nodos), pero tiene un efecto visible: si un nodo pide algo fuera de los constraints, la instalación desde la UI falla. Añadir una frase en la sección de solución de problemas.

#### BAJA-13 — La aserción sobre `CUDAExecutionProvider` no prueba lo que parece · Informativo
- `Dockerfile:111`, workflow línea 131. `ort.get_available_providers()` lista los providers **compilados**, no los que pueden inicializarse. Pasa en un runner sin GPU aunque cuDNN no se encuentre. Es útil como guardia de "instalé el paquete GPU y no el CPU", nada más. La verificación real (crear una `InferenceSession` con CUDA EP) solo puede hacerse en la instancia; sigue abierta (sección 5, V3).

#### BAJA-14 — `nodes.lock` apunta a `comfyanonymous/ComfyUI`, que ahora redirige · Aplicar
- La API devuelve 301 hacia `Comfy-Org/ComfyUI`. Actualizar la URL. Los clones seguirán funcionando mientras GitHub mantenga la redirección, pero no conviene depender de ello.

#### BAJA-15 — Todo corre como root y no está documentado · Documentar
- Sin cambios respecto a V1 (OPS-04). Añadir una línea en README como riesgo aceptado.

### 3.5 Informativos

#### INF-01 — `walkthrough.md` no existe
- La instrucción de esta ronda remite a `walkthrough.md` para "el detalle de todos los cambios aplicados". No está en el repositorio ni en ninguna subcarpeta. Si se generó, no se guardó. `docs/decisiones.md` cubre parcialmente ese papel, pero no enumera los cambios archivo a archivo ni explica el cambio de GPU objetivo (MED-02) ni la ausencia de REP-04.

#### INF-02 — Proceso zombi de Fase 2
- Tras `exec python3`, el `nohup download-models.sh --phase2 &` queda como hijo de ComfyUI, que no lo cosecha al terminar. Un zombi, sin consumo. `tini` solo cosecha huérfanos. Cosmético; si molesta, lanzar Fase 2 con `setsid -f` para que cuelgue de `tini`.

---

## 4. Decisiones que requieren al dueño

1. **ALTA-03**: ¿Conservar `gcc`/`python3-dev` en la imagen (+~200 MB) para que `torch.compile` y las instalaciones nativas desde Manager funcionen, o priorizar tamaño y documentar la limitación?
2. **ALTA-04**: ¿Dónde vive la fuente de verdad del repo y cuándo se hace el primer commit?
3. **ALTA-05**: Política de versiones de pip: ¿`==` estricto para la terna torch + `requirements.lock` generado en CI, o se acepta deriva en pip y solo se fija git?
4. **MED-02**: ¿RTX 5090 o RTX 4090 como GPU recomendada? Afecta a precio por hora, README y filtros de búsqueda.
5. **BAJA-03**: ¿Aceptar que `/healthz` exponga `/system_stats` sin autenticación?

---

## 5. Verificaciones que siguen pendientes (requieren construir o ejecutar)

| # | Qué | Cómo | Cierra |
|---|---|---|---|
| V1 | El `Dockerfile` construye de principio a fin tras aplicar CRIT-02 | Primer `docker build` (local o CI) | CRIT-02, ALTA-03 (problema 1) |
| V2 | Tamaño final de la imagen y tiempo de pull en Vast.ai | `docker images`; cronometrar | README "~15 GB" |
| V3 | `onnxruntime-gpu` inicializa CUDA EP con el cuDNN de pip vía `ldconfig` | En GPU: `python3 -c "import torch, onnxruntime as o; o.InferenceSession(..., providers=['CUDAExecutionProvider'])"` | BAJA-13 |
| V4 | Workers de Nginx como `www-data` y `.htpasswd` legible | `ps -o user,cmd -C nginx`; `curl -u` → 200 | REL-01 (V1) |
| V5 | Carga completa de la UI sin 503 con los límites actuales | Abrir UI con DevTools → pestaña Network, filtrar 503 | MED-01 |
| V6 | Ningún nodo marca `IMPORT FAILED` en el arranque | Log de ComfyUI al primer arranque, o warm-up de MED-03 | MED-03 |
| V7 | Versión real de `huggingface_hub` y backend de descarga activo | `hf version`; `pip show huggingface_hub hf_xet hf_transfer` | MED-05 |
| V8 | Desplegables de IPAdapter / ReActor / Impact con ficheros | Arranque con `SKIP_MODEL_DOWNLOAD=1` + ficheros dummy | CFG-03 (V1) |
| V9 | `limit_req` no afecta a `/ws` ni a subidas largas | Subir un vídeo de >1 GB y ejecutar un workflow de 10 min | MED-01 |
| V10 | El on-start en modo SSH de Vast.ai ejecuta `/start.sh` correctamente sin `tini` | Instancia de prueba en ese modo | BAJA-10 |

---

## 6. Lo que está bien y no debe tocarse (actualizado)

- Separación `/opt/ComfyUI` (código) y `/workspace/comfyui-data` (datos) con symlinks recreados en `start.sh`. Correcta y robusta ante volúmenes.
- Orden de arranque: Nginx → Fase 1 con `timeout` no fatal → Fase 2 en background → `exec` ComfyUI. Es el orden correcto.
- `hf_fetch`: staging en el mismo filesystem, `mv` atómico, reintentos, conservación de `.incomplete` entre intentos, resumen de fallos. Bien hecho.
- `nginx.conf`: `user www-data;`, logs a stdout/stderr, WebSocket, `client_max_body_size 0`, `proxy_buffering off`. Mantener.
- Filtro de `requirements.txt` del paso 4: la regex hace exactamente lo que debe (deja pasar `onnxruntime-gpu`, `opencv-python-headless`, y filtra `torch`, `torchvision`, `torchaudio`, `opencv-python`, `onnxruntime` con o sin especificador). El problema no está en el filtro sino en la limpieza posterior (CRIT-02).
- Validación del catálogo HF en CI: la regex captura los 26 `hf_fetch`, distingue `dataset` de `model` y falla en gated/404. Correcta.
- `manager-config.ini`: comentario explicando por qué `security_level = weak` es seguro aquí. Es exactamente el tipo de comentario que debe existir.
- Las 5 acciones de GitHub con SHA correcto y versión comentada: patrón a mantener.

---

## 7. Plan de ejecución sugerido

### Fase A — Desbloquear el pipeline (≤ 1 h, todo `Aplicar`)
1. CRIT-01: SHA correcto de hadolint.
2. CRIT-02: limpieza + `--force-reinstall --no-deps` de `opencv-python-headless` y `onnxruntime-gpu`.
3. ALTA-02: derivar `IMAGE_TAG` de `steps.meta.outputs`.
4. ALTA-01: publicar solo `sha-*`, smoke test, promover con `imagetools create`.
5. BAJA-14: URL de ComfyUI a `Comfy-Org/ComfyUI` en `nodes.lock`.
6. Primer push → primer build real. Con el log del build, cerrar V1 y V2.

### Fase B — Robustez (medio día)
1. ALTA-03 (tras decisión): mover la purga al final del paso 4 y conservar `gcc`/`python3-dev`.
2. MED-03: `requirements-no-cupy.txt` + warm-up `--quick-test-for-ci` en build y en smoke test.
3. MED-04: conservar `models/configs`.
4. MED-01: ajustar `limit_req`.
5. BAJA-01, BAJA-02, BAJA-04, BAJA-05, BAJA-06, BAJA-07, BAJA-08.
6. ALTA-05 (tras decisión): fijar terna torch y generar `requirements.lock` en CI.

### Fase C — Higiene y documentación
1. ALTA-04: primer commit, salir de OneDrive.
2. MED-02 (tras decisión): texto de GPU objetivo.
3. MED-05: confirmar `huggingface_hub` 1.x y limpiar referencias a `hf_transfer` si procede.
4. BAJA-03, BAJA-09, BAJA-10, BAJA-11, BAJA-12, BAJA-15: documentación.
5. Corregir `docs/decisiones.md` para que refleje el estado real de REP-01/REP-02/REP-04 y recoja la decisión de MED-02.

### Checklist de salida
- [ ] Workflow en verde de principio a fin, con `latest` publicado solo tras smoke test.
- [ ] `docker run --rm --entrypoint python3 <img> -c "import cv2, onnxruntime, insightface, torch"` OK.
- [ ] `--quick-test-for-ci` sin `IMPORT FAILED`.
- [ ] Arranque en Vast.ai: `/download-status` responde en <10 s, UI carga sin 503 en Network, Fase 2 progresa, `VHS_VideoCombine` con `h264_nvenc` funciona, ReActor encuentra `buffalo_l`.
- [ ] `git log` tiene al menos un commit y el repo está fuera de OneDrive.
- [ ] `docs/decisiones.md` no afirma nada que el código no haga.
