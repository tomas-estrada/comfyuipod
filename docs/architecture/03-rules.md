# 03 — Reglas arquitectónicas

**Rama:** `docs/maad-architecture-2026-11-10` · **Base:** `main` `b7f21de`
**Fecha:** 2026-11-10 · **Fase:** 4 de 4 (contraste)

Cada regla declara: enunciado, **evidencia** (`archivo:línea`), **confianza** y,
cuando aplica, el **hueco** que la rodea. Una regla sin evidencia ejecutable se
marca `hueco`, no se afirma.

---

## R-01 — Una sola superficie de red pública

**Enunciado.** El contenedor expone únicamente el puerto 8188 vía Nginx; todo
servicio interno se liga a `127.0.0.1`.

- Evidencia: `config/nginx.conf:54-55` · `scripts/start.sh:11-12` ·
  `comfyctl/src/supervisor.rs:336` · `scripts/start.sh:386`
- Confianza: **confirmado**
- Hueco: la versión de Nginx no está fijada en el repo; el comportamiento
  depende de los defaults de Ubuntu 24.04 en el momento del build.

## R-02 — Autenticación en el borde, sin bypass

**Enunciado.** Toda ruta de contenido exige Basic Auth; `require_auth` se deriva
solo de configuración y entorno, nunca de una cabecera de petición.

- Evidencia: `config/nginx.conf:109-110` (y 5 ubicaciones más) ·
  `studio/comfy_studio/app.py:540-544` · `studio/comfy_studio/app.py:566-572`
- Confianza: **confirmado**
- Hueco: las exenciones (`/favicon.ico`, `/healthz`, `/_starting.html`) son
  deliberadas; no hay evidencia de que `/healthz` filtre metadatos sensibles
  más allá de un HEAD sin cuerpo (`config/nginx.conf:80-91`).

## R-03 — Separación de código y datos

**Enunciado.** `/opt/ComfyUI` contiene solo código inmutable; modelos, entradas,
salidas y configuración de usuario viven en `COMFY_DATA_DIR` vía symlinks.

- Evidencia: `scripts/start.sh:215-220` · `scripts/start.sh:222-228`
- Confianza: **confirmado**
- Hueco: si el volumen no está montado, el arranque avisa y (con
  `COMFY_REQUIRE_PERSISTENT_VOLUME=1`) aborta
  (`scripts/start.sh:160-163`). Sin esa variable, los datos caen en el overlay y
  se pierden: es una decisión del operario, no un defecto.

## R-04 — Arranque estricto con salida codificada

**Enunciado.** `strict-check` evalúa S1–S17; un fallo fatal con `STRICT_START=1`
mantiene el pod sirviendo `/status` durante una gracia y termina con código 20.

- Evidencia: `comfyctl/src/strict.rs:1572` · `comfyctl/src/strict.rs:1568` ·
  `comfyctl/src/supervisor.rs:937-961`
- Confianza: **confirmado**
- Hueco: la duración de `STRICT_FATAL_GRACE` y el tiempo real de arranque de
  `studio-server` no están medidos; el timeout de espera
  (`comfyctl/src/supervisor.rs:342-377`) es un valor sin respaldo empírico
  publicado.

## R-05 — Adquisición de modelos manifiesto-primero

**Enunciado.** Ningún activo se descarga sin estar declarado en el manifiesto de
destino, con revisión fijada y digest verificado antes de usarse.

- Evidencia: `studio/comfy_studio/asset_acquire.py:1286` ·
  `studio/comfy_studio/asset_acquire.py:1002-1006` ·
  `studio/comfy_studio/asset_acquire.py:1085-1087`
- Confianza: **confirmado**
- Hueco: la política exacta de reintentos no se leyó línea a línea en esta pasada
  (`REQ-E-04` queda marcado `hueco` en el SRS).

## R-06 — Confinamiento de rutas de motor

**Enunciado.** Toda ruta que un motor recibe cae bajo el volumen; los tres puntos
de entrada (`run_layer`, `evaluate`, `assemble`) aplican el mismo confinamiento.

- Evidencia: `studio/comfy_studio/orchestration/adapters.py:229-303` ·
  `:490` · `:568` · `:650`
- Confianza: **confirmado**
- Hueco: el planner sigue aceptando `params` sin esquema por motor. Las rutas
  están confinadas, pero una clave inventada llega al motor, que hoy la ignora.
  Endurecer esto requiere una allowlist por motor.

## R-07 — Redacción en todas las salidas

**Enunciado.** Respuestas JSON, streams SSE, registros de log y superficie HTTP
de comfyctl pasan por el redactor; `/logs/*` se limita a una lista blanca.

- Evidencia: `studio/comfy_studio/app.py:609-623` ·
  `studio/comfy_studio/agent/llm.py:441` ·
  `studio/comfy_studio/config.py` (`install_log_redaction`) ·
  `comfyctl/src/logtail.rs:15-26`
- Confianza: **confirmado**
- Hueco: el redactor de Python es un subconjunto del de Rust (faltan patrones de
  AWS/GitHub/Slack/OpenAI/Google/Anthropic). `/logs/*` sí pasa por el de Rust,
  así que el hueco está acotado a `/studio/api/*`.

## R-08 — Gate de gasto humano

**Enunciado.** El lazo caro de una producción no arranca sin aprobación explícita
de una persona; el agente nunca puede aprobar.

- Evidencia: `studio/comfy_studio/orchestration/service.py:186` ·
  `studio/comfy_studio/routers/productions.py:3-8` ·
  `studio/comfy_studio/mcp_server.py:155`
- Confianza: **confirmado**
- Hueco: `approve` es sincrónico a propósito; el límite de duración lo fija
  `proxy_read_timeout 3600s`. Una producción de más de una hora daría 504. Está
  documentado como alternativa, no implementado.

## R-09 — Recuperación acotada, nunca bucles infinitos

**Enunciado.** Un job interrumpido se reintenta como máximo
`MAX_INTERRUPT_RETRIES` veces; los prompts vivos se re-enganchan en vez de
re-renderizarse.

- Evidencia: `studio/comfy_studio/recovery.py:32` ·
  `studio/comfy_studio/recovery.py:174` ·
  `studio/comfy_studio/recovery.py:131-168`
- Confianza: **confirmado**

## R-10 — Health de motores antes de producci\u00f3n

**Enunciado.** `assert_production_ready` exige estado `ready` para los motores
requeridos; `stub` y `unwired` rechazan.

- Evidencia: `studio/comfy_studio/engines/health.py:109` ·
  `studio/comfy_studio/engines/health.py:122`
- Confianza: **confirmado**

---

## Huecos transversales

| # | Hueco | Reglas afectadas |
|---|---|---|
| X-01 | Sin `docker`/GPU en el entorno de generación: build y smoke no ejecutados localmente | R-01, R-03, R-04 |
| X-02 | Coste de arranque de `studio-server` no medido | R-04 |
| X-03 | Redactor Python subconjunto del de Rust | R-07 |
| X-04 | `params` del planner sin allowlist por motor | R-06 |
| X-05 | Versiones de Nginx/ffmpeg no fijadas | R-01, R-02 |
| X-06 | Política de reintentos de `asset_acquire` no leída línea a línea | R-05 |
