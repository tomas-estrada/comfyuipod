# Auditoría de Agente — Versión 3

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha | 2026-10-16 |
| Alcance | Re-auditoría tras aplicar `AUDITORIA_AGENTE_V2.md`. Lectura completa de los 19 archivos del repo + verificación activa contra red (GitHub API, `git ls-remote`, índice de wheels de PyTorch, PyPI JSON, `raw.githubusercontent.com`). **No se construyó la imagen ni se ejecutó el contenedor.** Nadie lo ha hecho todavía: el commit inicial existe pero el workflow nunca ha corrido. |
| Documentos leídos | `docs/walkthrough.md` (ahora sí existe), `docs/decisiones.md`, `docs/auditorias/AUDITORIA_AGENTE_V1.md`, `docs/auditorias/AUDITORIA_AGENTE_V2.md`. |
| Destinatario | Agente implementador y dueño del proyecto. |

---

## 0. Veredicto

**Estáticamente, el repositorio está en buen estado y sin defectos bloqueantes.** Los dos críticos y los cinco altos de V2 están corregidos de forma verificable. No encontré ningún error que impida que el build arranque o que el contenedor levante.

**Pero "OK para producción" todavía no se puede afirmar**, por dos razones que no dependen de la calidad del código:

1. **Hay una regresión alta en el CI** (H-01): la caché de capas apunta a un tag distinto en cada commit, así que nunca se reutiliza. Cada push reconstruye la imagen desde cero (60-120 min) y crea un tag `-buildcache` nuevo en GHCR. No rompe nada, pero convierte el pipeline en algo lento y sucio, y es exactamente lo que CI-02 pretendía evitar.
2. **La imagen nunca se ha construido ni ejecutado.** Todas las verificaciones que en V1 y V2 quedaron como "requieren ejecutar" (sección 5) siguen abiertas. Una revisión estática, por exhaustiva que sea, no sustituye al primer build verde ni a una sesión real en Vast.ai. Los puntos con más probabilidad de aparecer en ese primer build son M-02 (disco del runner) y M-03 (warm-up sin timeout).

Mi recomendación: corregir H-01, M-01, M-02 y M-03 (menos de una hora en total), hacer el primer push, y tratar el resultado de ese workflow más una sesión de 30 minutos en una RTX 4090/5090 como el verdadero criterio de aceptación. Después de eso, si no aparece nada nuevo, el proyecto está listo.

---

## 1. Evidencia de verificación activa (esta ronda)

| Comprobación | Resultado |
|---|---|
| `hadolint/hadolint-action@54c9adbab…` (CRIT-01) | ✅ Coincide con el tag `v3.1.0`. |
| `actions/upload-artifact@b4b15b8c…` (nuevo) | ✅ Coincide con `v4.4.3`. Las otras 5 acciones ya estaban verificadas en V2. |
| 12 SHAs de `config/nodes.lock` (incluido `facebookresearch/sam2@2b90b9f5…`) | ✅ 12/12 existen. La URL de ComfyUI ya apunta a `Comfy-Org/ComfyUI` (BAJA-14 resuelta). |
| `torch==2.9.1+cu128`, `torchvision==0.24.1+cu128`, `torchaudio==2.9.1+cu128` para `cp312` `manylinux_2_28_x86_64` | ✅ Las tres ruedas existen en `download.pytorch.org/whl/cu128`. |
| `huggingface-hub` 2.2.0 (PyPI) | ✅ Es la última. `hf-xet` y `click` son dependencias núcleo en x86_64, así que el comando `hf` funciona sin extras. El `hf version` del smoke test lo cubre. |
| `comfy-aimdo==0.5.6`, `comfy-kitchen==0.2.37`, `comfy-angle` (nuevas deps de ComfyUI HEAD) | ✅ Tienen ruedas `abi3`/`py3` y no declaran requisito de versión de torch que choque con 2.9.1. |
| `sam2/setup.py` en el SHA fijado | ✅ Respeta `SAM2_BUILD_CUDA=0`; `install_requires` compatible con la terna fijada. |
| `ComfyUI-Frame-Interpolation/requirements-no-cupy.txt` | ✅ Existe. Contiene `torch`, `torchvision` y `opencv-contrib-python`, que la regex del paso 4 filtra correctamente. |
| Estado git | ✅ 1 commit en `main` (`15a6b77`), árbol limpio. Sigue dentro de OneDrive (riesgo residual aceptado por el dueño, ver L-08). |

---

## 2. Estado de los 28 hallazgos de V2

| ID V2 | Estado | Verificación |
|---|---|---|
| CRIT-01 SHA hadolint | ✅ | Red. |
| CRIT-02 cv2/onnxruntime | ✅ | `Dockerfile:123-124`: desinstala las 6 variantes y reinstala con `--no-deps --force-reinstall`. El `import cv2` de la línea 129 sigue como guardia. |
| ALTA-01 publicar antes del smoke | ✅ | Solo se empuja `sha-*`; promoción con `imagetools create` tras el smoke. |
| ALTA-02 mayúsculas en tag | ✅ | Tag derivado de `metadata-action` (`fromJSON(...).tags[0]`). |
| ALTA-03 toolchain purgado | ✅ | `build-essential`, `g++`, `python3-dev` conservados; solo se purgan `cmake` y `pkg-config`. Ver L-01 por dónde se purgan. |
| ALTA-04 sin commits | ✅ | 1 commit, árbol limpio. |
| ALTA-05 reproducibilidad | ✅ | Terna torch fijada en `Dockerfile` y `constraints.txt`; `sam2` por SHA con `--no-build-isolation`; `pip freeze` a `requirements.lock` subido como artefacto. |
| MED-01 `limit_req` | ✅ | `rate=50r/s`, `burst=300`, `429`, `/ws` fuera del límite. Pendiente V5 en runtime. |
| MED-02 GPU objetivo | ✅ | Documentado como decisión del dueño; README cubre 5090 y 4090. |
| MED-03 `install.py` / warm-up | ✅ | `requirements-no-cupy.txt` procesado; warm-up `--quick-test-for-ci` en build (`Dockerfile:187-194`) y en smoke test con `grep IMPORT FAILED`. Ver M-03. |
| MED-04 `models/configs` | ✅ | Respaldo en `/etc/comfyui/models_configs` y restauración con `cp -rn` en `start.sh`. |
| MED-05 `huggingface_hub` 1.x | ✅ | Pin `>=1.0.0,<3.0.0`; `hf_transfer` eliminado de ENV, scripts y README; `hf_xet` importado en smoke. |
| BAJA-01 `openssl rand` | ✅ | `-hex 12`. |
| BAJA-02 `start-period` | ✅ | 900 s. |
| BAJA-03 `/healthz` | ✅ | `proxy_method HEAD`. aiohttp responde HEAD en rutas `GET` por defecto, así que funciona. |
| BAJA-04 `MODEL_PROFILE` | ✅ | Validado con `case`. |
| BAJA-05 `COMFY_DATA_DIR` | ✅ | `sed` sobre `base_path` en cada arranque. |
| BAJA-06 cifras por perfil | ✅ | Script y README coinciden (24.96 / 24.95 / 29.43 GiB). |
| BAJA-07 502 en Fase 1 | ✅ | `_starting.html` con `error_page 502 =503` e `internal`. |
| BAJA-08 `.incomplete` | ✅ | `rm -rf STAGE_DIR` solo si `FAILED_MODELS` está vacío. |
| BAJA-09 HEAD desligado | ✅ | `git checkout -B main FETCH_HEAD` + README 5.6. |
| BAJA-10 modo SSH | ✅ | README recomienda "Docker Entrypoint". |
| BAJA-11 auditorías en raíz | ✅ | Movidas a `docs/auditorias/`. |
| BAJA-12 `PIP_CONSTRAINT` runtime | ✅ | README 5.6. |
| BAJA-13 aserción ORT | ✅ Informativo | Sin cambio (correcto); V3 sigue pendiente. |
| BAJA-14 URL ComfyUI | ✅ | `Comfy-Org/ComfyUI`. |
| BAJA-15 root | ✅ | README paso 2, nota de seguridad. |
| INF-01 `walkthrough.md` | ✅ | Existe en `docs/`. |
| INF-02 zombi Fase 2 | ⚠️ No resuelto realmente | `setsid` sin `-f` no cambia el padre del proceso; sigue colgando de ComfyUI tras el `exec`. Ver L-03. Cosmético. |

---

## 3. Hallazgos nuevos

### 3.1 Altos

#### H-01 — La caché de BuildKit usa un tag distinto por commit: nunca se reutiliza · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:135-136`
- **Evidencia**:
  ```yaml
  cache-from: type=registry,ref=${{ fromJSON(steps.meta_candidate.outputs.json).tags[0] }}-buildcache
  cache-to:   type=registry,ref=${{ fromJSON(steps.meta_candidate.outputs.json).tags[0] }}-buildcache,mode=max
  ```
  `tags[0]` es `ghcr.io/<owner>/<repo>:sha-<7 chars>`, así que la referencia resultante es `…:sha-abc1234-buildcache`. En cada commit nuevo, `cache-from` apunta a un tag que todavía no existe y `cache-to` crea otro. Resultado: build completo desde cero en cada push (60-120 min con la compilación de `insightface`, `sam2` y ~60 paquetes) y un tag `-buildcache` huérfano por commit en GHCR.
- **Por qué pasó**: al corregir ALTA-02 (minúsculas) se reutilizó el único valor en minúsculas disponible, que era el tag del candidato. El objetivo (ref en minúsculas) era correcto; la fuente elegida no.
- **Acción**: Generar una referencia fija en minúsculas con el propio `metadata-action`:
  ```yaml
  - id: meta_cache
    uses: docker/metadata-action@902fa8ec7d6ecbf8d84d538b9b233a880e428804 # v5.7.0
    with:
      images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
      tags: type=raw,value=buildcache
  ```
  y usar `ref=${{ fromJSON(steps.meta_cache.outputs.json).tags[0] }}` en `cache-from` y `cache-to`. Opcional: añadir un paso de limpieza con `actions/delete-package-versions` para los tags `sha-*` antiguos.

### 3.2 Medios

#### M-01 — La comprobación de SHAs de `nodes.lock` en CI no detecta un SHA inválido · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:44-49`
- **Evidencia**:
  ```bash
  git ls-remote "$url" | grep -q "^${sha::12}" || git ls-remote "$url" HEAD >/dev/null
  ```
  `git ls-remote` solo lista puntas de ramas y tags. Hoy los 12 SHAs coinciden con `HEAD` y el `grep` pasa; mañana, cuando cualquier repo reciba un commit, el `grep` fallará y entrará el fallback, que solo comprueba que el repo existe. Un SHA inventado o borrado pasaría la validación. `docs/walkthrough.md` describe este paso como "comprobación de los 12 SHAs"; en la práctica comprueba 12 URLs.
- **Acción**: Verificar el commit de verdad, que es lo mismo que hará el `Dockerfile`:
  ```bash
  git init -q /tmp/chk && git -C /tmp/chk fetch -q --depth=1 "$url" "$sha" && rm -rf /tmp/chk
  ```
  o, más barato, `curl -fsS "https://api.github.com/repos/$owner_repo/commits/$sha" >/dev/null`.

#### M-02 — Riesgo real de quedarse sin disco en el runner entre el build y el smoke test · Decisión: Aplicar
- **Ubicación**: `.github/workflows/docker-publish.yml:128-142`
- **Razonamiento**: Tras liberar ~30 GB, el runner `ubuntu-latest` queda con ~45 GB libres. El build con el driver `docker-container` deja la caché de BuildKit (todas las capas, ~15-20 GB) en ese mismo disco; después, `docker pull` del candidato descarga y descomprime la misma imagen otra vez para `dockerd` (~15-20 GB más). Es muy probable terminar en `no space left on device` justo en el smoke test, que es el paso que protege `latest`.
- **Acción**: Entre el build y el `docker pull`, añadir `docker buildx prune --all --force` (la caché ya está en el registro, no se pierde nada) y un `df -h` para dejar constancia. Si aun así falta espacio, mover `data-root` de Docker a `/mnt` (tiene ~60 GB extra en los runners de GitHub).

#### M-03 — El warm-up de build no tiene `timeout` ni `pipefail`: puede colgar el build o pasar en falso · Decisión: Aplicar
- **Ubicación**: `Dockerfile:187-194`
- **Razonamiento**:
  - Si un nodo se queda bloqueado al importar (ComfyUI-Manager esperando red, un `install.py` interactivo), el `RUN` no termina nunca y el job muere a los 180 min sin mensaje útil. El smoke test del CI sí usa `timeout 300`; el build no.
  - El `RUN` corre con `/bin/sh` (dash), sin `pipefail`. Si `python3 main.py` muere antes de llegar a la tabla de importación (error de sintaxis, dependencia rota de ComfyUI core), `tee` devuelve 0, el `grep "IMPORT FAILED"` no encuentra nada y el build **pasa**.
- **Acción**:
  ```dockerfile
  timeout 600 python3 main.py --cpu --quick-test-for-ci > /tmp/comfy_warmup.log 2>&1; rc=$?; \
  cat /tmp/comfy_warmup.log; \
  if [ "$rc" -ne 0 ] || grep -q "IMPORT FAILED" /tmp/comfy_warmup.log; then ...; exit 1; fi
  ```
  Esto captura el código de salida real de Python y evita la tubería.

### 3.3 Bajos

#### L-01 — `apt-get purge` de `cmake`/`pkg-config` en una capa distinta a la de instalación no reduce la imagen · Aplicar
- `Dockerfile:57` los instala en la capa del paso 3 (y no limpia `/var/lib/apt/lists/*`); `Dockerfile:121-122` los purga en la capa del paso 4. Los ficheros siguen en la capa anterior; se pagan ~100-150 MB de imagen por nada. Si se quieren purgar, debe ser al final del mismo `RUN` que los instala (es decir, moverlos a la capa del paso 4 o instalarlos y purgarlos ambos en el paso 3). Alternativa más simple: no purgarlos y quitar las dos líneas.

#### L-02 — `vastai create instance … --args --direct` es incorrecto · Aplicar
- `README.md:153`, `docs/decisiones.md:11`. `--args` captura todo lo que le sigue como argumentos del `ENTRYPOINT`, así que `--direct` se pasaría a `/start.sh` como `$1` (ignorado) y la instancia no tendría puerto directo. La forma correcta es `--direct` antes de `--args`, o directamente sin `--args` si no hay argumentos: `vastai create instance <ID> --image … --disk 150 --env "…" --direct`. Afecta solo a la referencia CLI; la guía web no se ve afectada.

#### L-03 — `setsid` sin `-f` no evita el zombi (INF-02 sigue abierto) · Aplicar o cerrar como aceptado
- `scripts/start.sh:164`. `setsid` crea una sesión nueva pero no cambia el padre; tras el `exec python3`, ComfyUI hereda el hijo y no lo cosecha. `setsid -f` hace el fork intermedio y deja el proceso colgando de `tini`, que sí lo cosecha. Es cosmético (un zombi, cero recursos); si no se cambia, actualizar `docs/decisiones.md` para no afirmar que está resuelto.

#### L-04 — `albumentations` y `albumentationsx` instalados a la vez · Documentar
- `ComfyUI-ReActor` pide `albumentations`; `comfyui_controlnet_aux` pide `albumentationsx` (fork drop-in con el mismo nombre de import). Ambos escriben en el mismo paquete `albumentations/`; el último instalado gana. Funciona, pero si en el futuro alguien desinstala uno de los dos, rompe el otro. Vale con una línea en `docs/decisiones.md`; no hay acción segura sin tocar los `requirements.txt` upstream.

#### L-05 — El cuadro de "verificaciones ejecutadas" de `walkthrough.md` sobrevende dos puntos · Aplicar
- Afirma "comprobación de los 12 SHAs" (ver M-01: comprueba URLs) y `docs/decisiones.md` afirma que `setsid` resuelve INF-02 (ver L-03). Son detalles, pero este tipo de documento pierde valor en cuanto una afirmación no se sostiene. Corregir ambas frases.

#### L-06 — Los 12 SHAs son el `HEAD` de hoy, no versiones probadas · Documentar / Consultar
- Ya señalado en V2. Es coherente con "congelar lo que funcione en el primer build verde", pero exige un procedimiento de bump: cuándo se actualiza, quién, y que el criterio sea "el workflow pasa con el nuevo SHA". Falta ese párrafo en `docs/decisiones.md` o en un `CONTRIBUTING.md` corto.

#### L-07 — `/opt/ComfyUI/temp` y los ficheros que crea el warm-up quedan en la imagen · Aplicar
- El `--quick-test-for-ci` del paso 6 crea `temp/`, `user/default/comfy.settings.json`, cachés de Manager en `user/__manager/` y posiblemente `/root/.config/Ultralytics`. Son pocos MB, pero `user/` acaba en `/workspace/comfyui-data/user`, que es lo que el usuario verá como "su" configuración inicial. Añadir `rm -rf /opt/ComfyUI/temp /root/.config /root/.cache` al final del `RUN` del paso 6 y revisar qué deja Manager en `user/__manager/`.

#### L-08 — Repo dentro de OneDrive · Riesgo residual
- Ya hay commit y el árbol está limpio, así que el daño potencial es menor que en V1/V2. Sigue siendo recomendable trabajar desde una copia fuera de OneDrive o excluir `.git/` de la sincronización. Lo dejo registrado como aceptado si el dueño no quiere moverlo.

---

## 4. Verificaciones que solo cierra el primer build y una sesión real

Sin cambios de fondo respecto a V2; las agrupo por el momento en que se cierran.

**Al primer workflow verde:**
- V1 El `Dockerfile` construye de principio a fin (cubre CRIT-02, ALTA-03, M-03 y cualquier sdist sin rueda).
- V2 Tamaño final de la imagen (`docker images`) y si el runner aguanta el `pull` (M-02).
- V6 Ningún nodo con `IMPORT FAILED` en el warm-up.
- V7 `hf version` y `import hf_xet` en el smoke test.
- Artefacto `requirements.lock` generado: guardarlo como línea base del entorno.

**En una sesión en Vast.ai (RTX 4090 o 5090, 30 minutos):**
- V3 `onnxruntime` crea una `InferenceSession` con `CUDAExecutionProvider` (ReActor realmente usa GPU).
- V4 Workers de Nginx como `www-data`; `curl -u` devuelve 200, no 500.
- V5 Carga de la UI sin 429 en la pestaña Network; `/ws` estable 10 minutos.
- V8 Desplegables de IPAdapter, ReActor e Impact con ficheros tras la Fase 1.
- V9 Subida de un vídeo >1 GB y un workflow de >10 min sin cortes del proxy.
- V10 `VHS_VideoCombine` con `h264_nvenc` produce un MP4 válido (es el motivo original del proyecto).
- `nvidia-smi` dentro del contenedor muestra la GPU y `torch.cuda.is_available()` es `True`.

---

## 5. Lo que está bien y no debe tocarse

- La estructura de dos jobs del workflow (validar → construir candidato → smoke → promover) es correcta y es el patrón que hay que conservar. Solo necesita las tres correcciones de CI de esta ronda.
- La regex del paso 4 del `Dockerfile`, ahora con `opencv-contrib-python` y la línea `git+…sam2`, hace exactamente lo que debe. Verificado contra los `requirements.txt` reales de los 10 nodos.
- La secuencia de limpieza/reinstalación de `cv2` y `onnxruntime` (`Dockerfile:123-124`) es la correcta. No simplificarla.
- `constraints.txt` con la terna `+cu128` fijada y cotas superiores en `numpy`, `opencv`, `onnxruntime-gpu` y `huggingface-hub`: es el equilibrio adecuado entre reproducibilidad y margen para Manager.
- `start.sh` completo: orden de arranque, contraseña, `sed` del YAML, restauración de `configs`, `timeout` no fatal. No hay nada que corregir salvo `setsid -f`.
- `nginx.conf`: `/ws` separado, `limit_req_status 429`, página de espera `internal`, `proxy_method HEAD` en `/healthz`. Correcto.
- `download-models.sh`: validación de perfil, reintentos, conservación de parciales, resúmenes. Correcto.
- `docs/decisiones.md` y `docs/walkthrough.md` existen, están enlazados desde el README y son trazables por ID de auditoría. Solo hay que corregir las dos frases de L-05.

---

## 6. Plan de cierre

### Antes del primer push (≤ 1 h)
1. H-01: ref de caché fija vía `meta_cache`.
2. M-01: verificación real de SHAs (`git fetch --depth=1` o API).
3. M-02: `docker buildx prune --all --force` + `df -h` antes del `pull`.
4. M-03: `timeout 600` y captura de `rc` en el warm-up del `Dockerfile`.
5. L-01, L-02, L-03, L-05, L-07: cambios de una línea cada uno.

### Primer push
6. Observar el workflow completo. Guardar el artefacto `requirements.lock` y anotar en `docs/decisiones.md` el tamaño de la imagen y la duración del build.
7. Segundo push trivial (por ejemplo, un cambio en README) para confirmar que la caché se reutiliza y el build baja a minutos.

### Sesión de aceptación en Vast.ai
8. Recorrer la lista de la sección 4. Si todo pasa, registrar fecha, GPU, driver y resultado en `docs/decisiones.md` y dar el proyecto por listo.
9. L-06: escribir el procedimiento de bump de `nodes.lock` y `constraints.txt`.

### Criterio de "listo para producción"
- [ ] Workflow verde con promoción a `latest` y segundo build usando caché.
- [ ] Sesión de aceptación en Vast.ai con los 8 puntos de la sección 4 superados.
- [ ] `docs/decisiones.md` sin ninguna afirmación que el código o el CI no cumplan.
