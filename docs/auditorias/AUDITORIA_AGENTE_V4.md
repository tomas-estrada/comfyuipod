# Auditoría de Agente — Versión 4

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha | 2026-10-16 |
| Commit auditado | `4e4e3e6` — `fix(ci,docker,runtime): resolve Audit V3 findings (H-01, M-01..M-03, L-01..L-07)` sobre `15a6b77`, rama `main`, árbol limpio. |
| Alcance | Re-auditoría tras aplicar `AUDITORIA_AGENTE_V3.md`. Revisión estática completa del diff `15a6b77..4e4e3e6` (7 archivos) y relectura íntegra de `Dockerfile`, `.github/workflows/docker-publish.yml`, `scripts/start.sh`, `scripts/download-models.sh`, `config/*`, `README.md`, `docs/decisiones.md` y `docs/walkthrough.md`. **Rol estrictamente de auditor: no se ejecutó ningún comando de prueba, no se construyó la imagen ni se lanzó el workflow.** |
| Verificación de red | No repetida en esta ronda. `config/nodes.lock`, `config/constraints.txt`, los SHAs de las 7 acciones de GitHub y las 26 URLs de HuggingFace **no cambiaron** en `4e4e3e6`; siguen siendo válidas las comprobaciones de V2/V3 realizadas hoy mismo. |
| Destinatario | Agente implementador y dueño del proyecto. |

---

## 0. Veredicto

**Los 11 hallazgos accionables de V3 están cerrados de forma verificable en el código y la documentación (H-01, M-01, M-02, M-03, L-01 a L-07). L-08 queda como riesgo aceptado por el dueño.** El commit `4e4e3e6` no introduce ninguna regresión de severidad alta ni media. Lo que queda son **3 mejoras de severidad baja y 3 notas informativas**, ninguna de las cuales debe retrasar el primer push.

**Estado para producción:** el repositorio está **listo para el primer push a GitHub**. Sigo sin poder afirmar "OK para producción" por una razón que ya no depende del código: **la imagen nunca se ha construido ni ejecutado**. Cuatro rondas de revisión estática han eliminado todo lo que se puede detectar leyendo; lo que falta solo lo detecta el runner de GitHub Actions y una GPU real. El criterio de aceptación no cambia respecto a V3 y se repite en la sección 6:

1. Primer workflow verde de principio a fin (incluida la promoción a `latest`).
2. Segundo push trivial que demuestre que la caché `buildcache` se reutiliza y el build baja de ~90 min a minutos.
3. Sesión de aceptación de 30 minutos en una RTX 4090/5090 en Vast.ai con los 10 puntos de la sección 5.

Si esos tres pasos pasan sin novedades, el proyecto está en producción. Si aparece algo, será material para una V5 breve y concreta, no para otra ronda estática.

---

## 1. Evidencia de esta ronda

| Comprobación | Resultado |
|---|---|
| `git log` / `git status` | `4e4e3e6` sobre `15a6b77`, 2 commits en `main`, árbol limpio. Los avisos de modificación externa sobre `config/manager-config.ini`, `config/extra_model_paths.yaml` y `scripts/download-models.sh` corresponden a toques de marca de tiempo de OneDrive; `git diff` está vacío (ver L-08). |
| Archivos tocados en `4e4e3e6` | `.github/workflows/docker-publish.yml`, `Dockerfile`, `README.md`, `scripts/start.sh`, `docs/decisiones.md`, `docs/walkthrough.md`, `docs/auditorias/AUDITORIA_AGENTE_V3.md` (solo añadido, sin edición). |
| Archivos con SHAs/URLs/versiones | `config/nodes.lock`, `config/constraints.txt`, `scripts/download-models.sh` y los `uses:` de las acciones **sin cambios** → la verificación de red de V3 sigue vigente (12/12 SHAs, 7/7 acciones, 26/26 HF 302, terna torch cu128 cp312). |
| `--args --direct` | Eliminado en `README.md` y `docs/decisiones.md:11`. Ya no aparece en ningún archivo salvo en el histórico de V3. |
| Afirmaciones documentales (L-05) | `walkthrough.md` ya dice `git fetch --depth=1 --filter=tree:0` y `setsid -f`; `decisiones.md` idem. No queda ninguna frase que el código no cumpla. |
| L-04 / L-06 | `docs/decisiones.md` sección 3.1 (coexistencia `albumentations`/`albumentationsx`) y 3.2 (procedimiento de bump con criterio "workflow en verde + revisar artefacto `requirements.lock`"). Correcto y suficiente. |

---

## 2. Estado de los 12 hallazgos de V3

| ID V3 | Estado | Verificación en `4e4e3e6` |
|---|---|---|
| **H-01** caché por commit | ✅ Resuelto | `docker-publish.yml:122-128` paso `meta_cache` con `type=raw,value=buildcache`; `cache-from`/`cache-to` (`:146-147`) usan `fromJSON(steps.meta_cache.outputs.json).tags[0]` → `ghcr.io/<owner>/<repo>:buildcache` en minúsculas, estable entre commits. En el primer run la ref no existirá; BuildKit lo avisa y continúa, no falla. |
| **M-01** verificación de SHAs | ✅ Resuelto | `:44-52`: `git init -q /tmp/chk_sha && git -C /tmp/chk_sha fetch -q --depth=1 --filter=tree:0 "$url" "$sha"` sin fallback. GitHub permite `allowAnySHA1InWant` y filtros parciales, así que un SHA inexistente devuelve `fatal: remote error: upload-pack: not our ref` y el paso falla. Ver N-03 para una mejora menor de legibilidad. |
| **M-02** disco del runner | ✅ Resuelto (parcial por naturaleza) | `:149-154`: `df -h; docker buildx prune --all --force; sudo docker system prune --all --force; df -h` entre el build y el `docker pull`. Orden correcto: el builder `docker-container` sigue en ejecución y `system prune` no lo toca (contenedor activo, imagen en uso, volúmenes no incluidos). Lo que **no** puede resolver este paso es el pico de disco *durante* el build (snapshots de BuildKit + blobs comprimidos de exportación). Eso solo lo confirma el primer run; ver sección 5. |
| **M-03** warm-up sin timeout | ✅ Resuelto | `Dockerfile:186-195`: subshell `( rc=0; timeout 600 … \|\| rc=$?; cat log; if [ "$rc" -ne 0 ] \|\| grep -q "IMPORT FAILED" …; exit 1 )`. Válido en `/bin/sh` (dash): `rc=$?` tras `\|\|`, `exit 1` dentro del subshell corta la cadena `&&` y el `RUN` falla. `timeout` devuelve 124 si expira. Mismo patrón en el smoke test (`:166-167`). Correcto. |
| **L-01** purge de cmake en capa distinta | ✅ Resuelto | `cmake` y `pkg-config` en la capa 1 (`Dockerfile:37-38`), sin `apt-get purge` posterior. Coherente con la decisión ALTA-03 de conservar toolchain. |
| **L-02** `--args --direct` | ✅ Resuelto | `README.md` y `docs/decisiones.md`. |
| **L-03** `setsid` sin `-f` | ✅ Resuelto | `start.sh:164`: `setsid -f … >> log 2>&1 < /dev/null`, sin `&`. Las redirecciones se aplican a `setsid` y las hereda el hijo; `setsid -f` retorna 0 inmediatamente, compatible con `set -e`. El proceso cuelga de `tini`, que lo cosecha. INF-02 de V2 queda cerrado de verdad. |
| **L-04** albumentations/albumentationsx | ✅ Documentado | `docs/decisiones.md` §3.1. |
| **L-05** precisión documental | ✅ Resuelto | Ver sección 1. |
| **L-06** procedimiento de bump | ✅ Documentado | `docs/decisiones.md` §3.2. |
| **L-07** restos del warm-up | ✅ Resuelto | `Dockerfile:196-199`: elimina log, `temp/` (ambas rutas), `comfy.settings.json`, `user/__manager`, `/root/.config`, `/root/.cache`; recrea `user/__manager` y `user/default/ComfyUI-Manager` y vuelve a copiar `config.ini`. Ver N-04 para un resto menor posible. |
| **L-08** repo en OneDrive | ⚪ Aceptado | Registrado en `decisiones.md`. El síntoma reaparece en esta ronda (OneDrive marca archivos como modificados sin cambiar contenido). No afecta a lo que se publica. |

**Resumen: 11 resueltos, 1 aceptado, 0 regresiones.**

---

## 3. Revisión de regresiones del commit `4e4e3e6`

Puntos que revisé específicamente porque los cambios podían romper algo que antes funcionaba. Todos salieron limpios:

| Punto | Resultado |
|---|---|
| Subshell con `rc=$?` y `exit 1` en `RUN` bajo `/bin/sh` (dash) | Sintaxis POSIX válida; `exit` dentro de `( … )` solo termina el subshell y propaga el código a la cadena `&&`. |
| Hadolint con `failure-threshold: error` sobre las líneas nuevas | El `cd /opt/ComfyUI` dentro de `RUN` dispara DL3003 (warning) y el `awk \| while` dispara DL4006 (warning); ninguno es `error`. Las reglas de nivel error (DL3000, DL3004, DL3011, DL3021, DL3061…) no aplican. No hace falta ampliar la lista `ignore`. |
| `docker buildx prune` + `sudo docker system prune --all` antes del `pull` | Actúa sobre el builder actual creado por `setup-buildx-action`. El contenedor `buildx_buildkit_*` está en ejecución, por lo que ni él ni su imagen ni su volumen de estado se eliminan. `imagetools create` posterior trabaja contra el registro y no depende de imágenes locales. |
| Bucle `awk \| while read` del job `validate` bajo `set -e` | GitHub ejecuta `run:` con `bash -e {0}` cuando no se indica `shell:`. El `while` es el último elemento del pipeline y no está en un contexto donde `-e` se ignore; un `git fetch` fallido aborta el subshell con código ≠ 0 y el paso falla. Correcto, aunque implícito (N-03). |
| `--filter=tree:0` en `git fetch` | Soportado por GitHub. Descarga solo el objeto commit; ideal para 12 comprobaciones en <1 min. |
| `rm -rf …/user/__manager` y recreación | La copia de `config.ini` se rehace en `:198-199`. `start.sh:104-109` no la pisa si ya existe. Coherente. |
| `cmake`/`pkg-config` permanentes | +~40 MB. Sin efectos secundarios en pip (ninguna dependencia fijada compila con cmake en el build). |
| `concurrency.cancel-in-progress: true` + `cache-to` | Un run cancelado a mitad de exportación no deja el tag `buildcache` corrupto: el registro solo actualiza el tag cuando el manifiesto completo se sube. El siguiente run simplemente no encuentra caché parcial. |

---

## 4. Hallazgos nuevos

Ninguno es bloqueante. Los tres de severidad baja se aplican en menos de 15 minutos; los informativos son para decidir, no para actuar ahora.

#### N-01 — Timeout del smoke test (300 s) menor que el del warm-up del build (600 s) · **Baja** · Aplicar
- `docker-publish.yml:167` usa `timeout 300`; `Dockerfile:188` usa `timeout 600`. Ambos ejecutan el mismo `python3 main.py --cpu --quick-test-for-ci` en el mismo tipo de runner. Si la carga de nodos tarda entre 300 y 600 s (posible en runners lentos: import de torch + ultralytics + insightface + Manager consultando red), el build pasa y el smoke test falla sin motivo real, bloqueando la promoción a `latest`.
- **Acción**: unificar a `timeout 600` en el smoke test. Es un cambio de un número.

#### N-02 — Fase 2 no completa lo que el `timeout 600` de la Fase 1 dejó a medias · **Baja** · Consultar / Aplicar
- `start.sh:160`: si la Fase 1 supera 600 s (red lenta en el host de Vast.ai), `timeout` la corta y ComfyUI arranca; está bien. Pero `download-models.sh --phase2` solo descarga checkpoints, así que `inswapper_128.onnx`, CodeFormer, `buffalo_l` o los VAE que no llegaron a bajar **quedan ausentes hasta el próximo reinicio del contenedor**. ReActor e IPAdapter aparecerán sin modelos aunque la UI esté operativa.
- `hf_fetch` ya es idempotente (`SKIP (ya existe)`), así que la solución es gratis: en `run_phase2()`, antes de los checkpoints, llamar a `run_phase1` (o añadir en `start.sh` un `--phase1` previo dentro del mismo `setsid -f`). Coste en el caso normal: 12 comprobaciones `[ -s archivo ]`.
- **Acción propuesta**: en `download-models.sh`, caso `--phase2`: `run_phase1; run_phase2`. O bien documentar explícitamente en el README que si la Fase 1 agota el tiempo hay que reiniciar la instancia.

#### N-03 — El paso M-01 depende del `-e` implícito del runner · **Baja** · Aplicar
- `docker-publish.yml:44-52`. Funciona porque GitHub inyecta `bash -e {0}`, pero no hay `set -euo pipefail` ni mensaje de error propio: en caso de fallo el log mostrará el `fatal:` de git y un paso rojo sin indicar qué entrada de `nodes.lock` es la culpable (el `echo "Verificando …"` previo ayuda, pero no es explícito).
- **Acción**: añadir `set -euo pipefail` al inicio del bloque y envolver el fetch: `git -C /tmp/chk_sha fetch -q --depth=1 --filter=tree:0 "$url" "$sha" || { echo "::error::SHA $sha no existe en $url ($name)"; exit 1; }`. Mismo criterio que ya se aplica en los demás scripts del repo.

#### N-04 — Posibles restos del warm-up en `user/default/ComfyUI-Manager/` · **Informativo**
- L-07 limpia `user/__manager` (ruta actual de Manager ≥ 3.38) pero no `user/default/ComfyUI-Manager/`, donde versiones anteriores escribían `snapshots/`, `startup-scripts/` y cachés. Con el SHA fijado de Manager lo esperable es que esa carpeta solo contenga el `config.ini` copiado, pero no lo he podido comprobar sin ejecutar.
- **Acción**: en el primer build verde, inspeccionar con `docker run --rm --entrypoint find <sha-tag> /workspace/comfyui-data/user -type f` y, si hay restos, añadir la ruta al `rm -rf` de `Dockerfile:196`. Cero riesgo funcional.

#### N-05 — `build-push-action` v6 publica atestación de procedencia por defecto · **Informativo** · Consultar
- Con `push: true`, v6 genera un índice OCI con un manifiesto de atestación (`provenance: mode=min`). En la UI de GHCR aparece como una entrada de plataforma `unknown/unknown` junto a `linux/amd64`. `docker pull` y Vast.ai lo manejan sin problema; `imagetools create` copia el índice completo. Es cosmético.
- **Decisión para el dueño**: dejarlo (trazabilidad SLSA gratis) o añadir `provenance: false` al paso de build para que el paquete muestre solo `linux/amd64`. Recomiendo dejarlo.

#### N-06 — Redundancias menores sin efecto · **Informativo**
- `Dockerfile:36`: `g++` ya viene con `build-essential`. `docker-publish.yml:153`: `sudo` no es necesario para `docker` en el runner (usuario en grupo `docker`), aunque tampoco molesta. No tocar salvo que se edite esa zona por otro motivo.

---

## 5. Verificaciones que solo cierra el primer build y una sesión real

Sin cambios de fondo respecto a V3. Las mantengo numeradas para que el agente que ejecute el primer push las marque una a una.

**Al primer workflow verde:**
- V1 El `Dockerfile` construye de principio a fin (cubre CRIT-02, ALTA-03, M-03 y cualquier sdist sin rueda).
- V2 Tamaño final de la imagen (`docker images`) y pico de disco del runner (`df -h` antes y después del prune de M-02). Anotar ambas cifras en `docs/decisiones.md`.
- V6 Ningún `IMPORT FAILED` en el warm-up ni en el smoke test.
- V7 `hf version` y `import hf_xet` correctos en el smoke test.
- V11 *(nuevo)* Duración del primer build y del segundo push trivial (debe bajar a minutos gracias a `buildcache`). Si el segundo build vuelve a tardar >30 min, H-01 no está funcionando y hay que revisar `cache-from` en el log de BuildKit (`importing cache manifest from ghcr.io/…:buildcache`).
- V12 *(nuevo)* Contenido de `/workspace/comfyui-data/user` en la imagen (N-04).
- Artefacto `requirements.lock`: guardarlo como línea base del entorno.

**En una sesión en Vast.ai (RTX 4090 o 5090, 30 minutos):**
- V3 `onnxruntime` crea una `InferenceSession` con `CUDAExecutionProvider` (ReActor realmente usa GPU).
- V4 Workers de Nginx como `www-data`; `curl -u` devuelve 200, no 500.
- V5 Carga de la UI sin 429 en la pestaña Network; `/ws` estable 10 minutos.
- V8 Desplegables de IPAdapter, ReActor e Impact con ficheros tras la Fase 1. Si la Fase 1 agotó los 600 s, confirmar el comportamiento descrito en N-02.
- V9 Subida de un vídeo >1 GB y un workflow de >10 min sin cortes del proxy.
- V10 `VHS_VideoCombine` con `h264_nvenc` produce un MP4 válido.
- `nvidia-smi` dentro del contenedor muestra la GPU y `torch.cuda.is_available()` es `True`.
- `ps -o ppid,stat,cmd -C download-models.sh` durante la Fase 2: el padre debe ser PID 1 (`tini`) y, al terminar, no debe quedar ningún proceso `<defunct>` (confirma L-03).

---

## 6. Lo que está bien y no debe tocarse

- La estructura del workflow (validar → construir candidato → purgar → smoke → promover) es la correcta. Con `meta_cache` fijo, ya es el pipeline que CI-02 pedía en V1.
- La regex del paso 4 del `Dockerfile` y la secuencia de limpieza/reinstalación de `cv2`/`onnxruntime` (`Dockerfile:112`, `:121-122`). Verificadas contra los `requirements.txt` reales en V2/V3; sin cambios.
- `constraints.txt` y `nodes.lock`: sin cambios desde V3, con procedimiento de bump ahora documentado.
- `start.sh` completo. Con `setsid -f` ya no hay nada pendiente; N-02 es una mejora funcional, no una corrección.
- `nginx.conf`, `download-models.sh`, `extra_model_paths.yaml`, `manager-config.ini`, `_starting.html`: sin cambios y sin observaciones nuevas.
- `docs/decisiones.md` y `docs/walkthrough.md`: por primera vez no contienen ninguna afirmación que el código no cumpla. Mantener esa disciplina al registrar los resultados del primer build.

---

## 7. Plan de cierre

### Antes del primer push (≤ 15 min, opcional)
1. N-01: `timeout 300` → `timeout 600` en el smoke test.
2. N-03: `set -euo pipefail` + mensaje `::error::` en el paso M-01.
3. N-02: decidir entre `run_phase1; run_phase2` en `--phase2` o una nota en el README.

Si el dueño prefiere no tocar nada más antes de ver el primer resultado real, es una decisión razonable: ninguno de los tres cambia el resultado del build.

### Primer push
4. Observar el workflow completo. Registrar en `docs/decisiones.md`: fecha, duración, tamaño de la imagen, `df -h` antes/después del prune, y adjuntar o enlazar el artefacto `requirements.lock`.
5. Inspeccionar `user/` en la imagen (V12 / N-04).
6. Cambiar la visibilidad del paquete GHCR a **Public** (ya documentado en `README.md` paso 1.3).

### Segundo push
7. Cambio trivial (README) para confirmar reutilización de `buildcache` (V11).

### Sesión de aceptación en Vast.ai
8. Recorrer la lista de la sección 5. Registrar fecha, GPU, driver y resultado en `docs/decisiones.md`.

### Criterio de "listo para producción"
- [ ] Workflow verde con promoción a `latest`.
- [ ] Segundo build en minutos (caché funcionando).
- [ ] Sesión de aceptación en Vast.ai con todos los puntos de la sección 5 superados.
- [ ] `docs/decisiones.md` actualizado con las cifras reales del primer build y la sesión.

Cuando esas cuatro casillas estén marcadas, no hace falta una V5 salvo que el primer build o la sesión revelen algo concreto.
