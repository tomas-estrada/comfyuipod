# Auditoría de Agente — Versión 5 (cierre de la fase estática)

| Campo | Valor |
|---|---|
| Proyecto | ComfyUI "Everything-Ready" Executor para Vast.ai |
| Fecha | 2026-10-16 |
| Commit auditado | `4842937` — `chore(ci,docker,scripts): resolve Audit V4 findings (N-01..N-06)` sobre `4e4e3e6`, rama `main`, árbol limpio. |
| Alcance | Revisión estática del diff `4e4e3e6..4842937` (6 archivos, +203/−14) y comprobación de coherencia con `docs/decisiones.md` y `docs/walkthrough.md`. **Rol de auditor: no se ejecutó ningún comando de prueba, build ni workflow.** |
| Verificación de red | No necesaria. `config/nodes.lock`, `config/constraints.txt`, las acciones de GitHub y las URLs de HuggingFace no cambiaron. Siguen vigentes las comprobaciones de V2/V3 de hoy. |
| Destinatario | Agente implementador y dueño del proyecto. |

---

## 0. Veredicto

**Los 6 puntos de V4 están cerrados correctamente (5 aplicados, N-05 decidido y documentado). No hay hallazgos nuevos ni regresiones.**

Con esto se agota lo que una revisión estática puede aportar. Cinco rondas han dejado el repositorio sin defectos detectables por lectura: todo lo que queda abierto son comprobaciones que solo cierra el primer workflow y una sesión en GPU real. **No recomiendo una sexta ronda estática.** El siguiente paso es el primer push; la siguiente auditoría, si hace falta, debe partir de los logs reales del workflow y de la sesión en Vast.ai.

---

## 1. Estado de los 6 hallazgos de V4

| ID V4 | Estado | Verificación en `4842937` |
|---|---|---|
| **N-01** timeout smoke test | ✅ | `docker-publish.yml`: `timeout 600` en el smoke test, igual que `Dockerfile:187`. |
| **N-02** Fase 2 no completa pendientes de Fase 1 | ✅ | `download-models.sh:279-283`: el caso `--phase2` ejecuta `run_phase1` y después `run_phase2`. Correcto: `run_phase1` solo se añade en el `case`, no dentro de `run_phase2`, así que la ruta `--all` no la duplica. Cada función reinicia `FAILED_MODELS` y emite su propio resumen; el log de `/download-status` mostrará dos banners de Fase 1 cuando la primera haya sido cortada por `timeout`, lo cual es informativo y correcto. Sin riesgo de descargas concurrentes: `timeout` envía `SIGTERM` a todo el grupo de procesos de la Fase 1 antes de que `start.sh` lance la Fase 2. |
| **N-03** `-e` implícito en M-01 | ✅ | `set -euo pipefail` al inicio del bloque y `\|\| { echo "::error::SHA $sha no existe en $url ($name)"; exit 1; }`. Con `pipefail`, el `exit 1` del subshell `while` hace fallar el pipeline y el paso. `set -u` es seguro: `name`, `url` y `sha` siempre vienen de `read`. |
| **N-04** restos en `user/default/ComfyUI-Manager` | ✅ | `Dockerfile:195` añade la ruta al `rm -rf` y la recrea en `:196-198`. El smoke test incluye `find /workspace/comfyui-data/user -type f` (V12): la salida esperada son exactamente dos ficheros, `user/__manager/config.ini` y `user/default/ComfyUI-Manager/config.ini`. Cualquier otro fichero indica un resto del warm-up a añadir a la limpieza. |
| **N-05** atestación de procedencia | ✅ Decidido | Se conserva `provenance` por defecto. Registrado en `docs/decisiones.md`. En GHCR aparecerá una entrada `unknown/unknown`; es esperado. |
| **N-06** redundancias | ✅ | `g++` eliminado (`build-essential` lo arrastra como dependencia dura). `sudo` eliminado delante de los dos `docker … prune`; el usuario `runner` pertenece al grupo `docker`. Los `sudo rm -rf` de las rutas del sistema se mantienen, como debe ser. |

Documentación: `decisiones.md` añade la tabla "Ronda V4" y actualiza L-07; `walkthrough.md` refleja `timeout 600`, la inspección de `user/`, `set -euo pipefail` y la continuación idempotente de Fase 1. Ninguna afirmación sin respaldo en el código.

---

## 2. Regresiones revisadas

| Punto | Resultado |
|---|---|
| Línea de continuación tras quitar `g++` en el `apt-get install` | `build-essential \` seguido de `cmake \`; sintaxis intacta. |
| `find … -type f` en el smoke test | Devuelve 0 aunque no haya ficheros; no puede romper el paso. Solo informa. |
| `pipefail` sobre `awk \| while` | `awk` termina con 0; el estado del pipeline lo fija el `while`. Sin falsos positivos. |
| `docker image prune` / `docker system prune` sin `sudo` | Mismo demonio, mismos permisos efectivos en `ubuntu-latest`. |
| `run_phase1` dentro de `--phase2` bajo `setsid -f` | Hereda `COMFY_DATA_DIR`, `MODEL_PROFILE` y `HF_TOKEN` exportados o del entorno del contenedor; `set -uo pipefail` sin `-e` sigue permitiendo que un fallo de Fase 1 no impida la Fase 2. |

---

## 3. Lo único que queda abierto (requiere ejecución)

Idéntico a la sección 5 de V4; lo reproduzco como checklist para quien haga el primer push.

**Primer workflow:**
- [ ] V1 Build completo sin errores.
- [ ] V2 Tamaño de imagen y `df -h` antes/después del prune de M-02 anotados en `docs/decisiones.md`.
- [ ] V6 Sin `IMPORT FAILED` en warm-up ni smoke test.
- [ ] V7 `hf version` e `import hf_xet` correctos.
- [ ] V12 `find /workspace/comfyui-data/user -type f` lista solo los dos `config.ini`.
- [ ] Artefacto `requirements.lock` guardado como línea base.
- [ ] Paquete GHCR cambiado a **Public**.

**Segundo push (cambio trivial):**
- [ ] V11 Build en minutos; el log de BuildKit muestra `importing cache manifest from ghcr.io/…:buildcache`.

**Sesión en Vast.ai (RTX 4090 o 5090, 30 min):**
- [ ] V3 `InferenceSession` con `CUDAExecutionProvider`.
- [ ] V4 Workers de Nginx como `www-data`; `curl -u` → 200.
- [ ] V5 UI sin 429; `/ws` estable 10 min.
- [ ] V8 Desplegables de IPAdapter, ReActor e Impact poblados. Si la Fase 1 agotó 600 s, confirmar que la Fase 2 los completa en segundo plano (N-02).
- [ ] V9 Subida >1 GB y workflow >10 min sin cortes.
- [ ] V10 `VHS_VideoCombine` con `h264_nvenc` genera MP4 válido.
- [ ] `torch.cuda.is_available()` es `True`; `nvidia-smi` ve la GPU.
- [ ] Fase 2 cuelga de PID 1 y no deja `<defunct>` al terminar (L-03).

---

## 4. Criterio de "listo para producción"

- [ ] Workflow verde con promoción a `latest`.
- [ ] Segundo build usando caché.
- [ ] Sesión de aceptación superada.
- [ ] `docs/decisiones.md` con fecha, GPU, driver, duración del build, tamaño de imagen y resultado de la sesión.

Marcadas esas cuatro casillas, el proyecto está en producción y esta serie de auditorías queda cerrada. Si el primer build o la sesión revelan algo, la V6 debe ser corta y basada en logs reales, no en lectura.
