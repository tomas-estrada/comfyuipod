# 04 — Informe ATAM

**Rama:** `docs/maad-architecture-2026-11-10` · **Base:** `main` `b7f21de`
**Fecha:** 2026-11-10 · **Fase:** 4 de 4 (contraste)

Análisis de compromisos arquitectónicos (ATAM) sobre el estado verificado de
`main`. No es una revisión de diseño: documenta los compromisos **que el código ya
toma**, con su evidencia y su coste.

---

## 1. Motores de negocio

| Motor | Evidencia | Confianza |
|---|---|---|
| Instancia única Vast.ai, sin orquestador | `docs/ARCHITECTURE_CANONICAL.md:20` | confirmado |
| Un solo usuario por pod | `docs/ARCHITECTURE_CANONICAL.md:20` | confirmado |
| GPU de 96 GB (RTX PRO 6000 Blackwell) | `studio/comfy_studio/hardware_profile.py:77-92` | confirmado |
| Fiabilidad de producción sobre rendimiento | `comfyctl/src/strict.rs:1` | confirmado |
| Gasto GPU bajo control humano | `studio/comfy_studio/routers/productions.py:3-8` | confirmado |

---

## 2. Atributos de calidad y escenarios

| # | Atributo | Escenario | Respuesta actual | Confianza |
|---|---|---|---|---|
| QA-1 | Seguridad | Petición sin credenciales a `/status/api` | 401 (`config/nginx.conf:146-147`) | confirmado |
| QA-2 | Seguridad | Cliente envía cabecera de bypass | Ignorada; `require_auth` no la lee (`studio/comfy_studio/app.py:540-544`) | confirmado |
| QA-3 | Seguridad | Secreto en una respuesta JSON | Redactado (`studio/comfy_studio/app.py:609-623`) | confirmado |
| QA-4 | Integridad de datos | Reinicio del pod con modelos ya descargados | Symlinks al volumen; nada se re-descarga (`scripts/start.sh:215-220`) | confirmado |
| QA-5 | Integridad de datos | Host sin GPU/VRAM suficiente | `strict-check` fatal → exit 20 (`comfyctl/src/strict.rs:1568`) | confirmado |
| QA-6 | Disponibilidad | ComfyUI muere bajo supervisión | Reinicio acotado (`comfyctl/src/supervisor.rs:1`) | confirmado |
| QA-7 | Disponibilidad | Reinicio con jobs en vuelo | Reintento acotado + re-enganche (`studio/comfy_studio/recovery.py:32`) | confirmado |
| QA-8 | Coste | Agente intenta lanzar render sin aprobación | Imposible; `approve` no está en MCP (`studio/comfy_studio/mcp_server.py:155`) | confirmado |
| QA-9 | Coste | Motor no listo en producción | `EngineNotReadyError` (`studio/comfy_studio/engines/health.py:122`) | confirmado |
| QA-10 | Observabilidad | Operario necesita diagnóstico | `/diag`, `/report.md`, `/logs/*` con lista blanca (`comfyctl/src/http.rs:194-232`) | confirmado |

---

## 3. Enfoques arquitectónicos y sus costes

### A-1 — Nginx como único borde, servicios en loopback

- **Gana:** una sola superficie que auditar; Basic Auth, rate-limit y cabeceras en
  un punto (`config/nginx.conf:53-366`).
- **Cuesta:** todo el tráfico pasa por un proceso adicional; la configuración de
  `proxy_read_timeout` por ubicación se convierte en el límite de duración
  efectivo de cada operación larga (`config/nginx.conf:248`).

### A-2 — Supervisor nativo en Rust

- **Gana:** arranque determinista, sin runtime de Python para el control;
  `strict-check` evaluable antes de servir (`comfyctl/src/supervisor.rs:342-377`).
- **Cuesta:** duplicación de lógica de recolección entre Rust y Python
  (`comfyctl/src/collect/` vs `studio/comfy_studio/hardware_detect.py`); dos
  toolchains que mantener.

### A-3 — Manifiesto-primero para adquisición de modelos

- **Gana:** verificabilidad (revisión + digest) antes de escribir en el volumen
  (`studio/comfy_studio/asset_acquire.py:1002-1006`).
- **Cuesta:** el catálogo es una fuente más que mantener en sincronía; de ahí los
  chequeos de paridad (`scripts/check_profile_parity.py:1`,
  `.github/workflows/docker-publish.yml:82-91`).

### A-4 — Confinamiento de rutas en un punto de paso único

- **Gana:** los tres puntos de entrada de motor comparten una sola política
  (`studio/comfy_studio/orchestration/adapters.py:490`, `:568`, `:650`).
- **Cuesta:** el planner puede emitir `params` arbitrarios; sin allowlist por
  motor, el confinamiento es la única defensa.

### A-5 — Gate de gasto humano sincrónico

- **Gana:** el llamador recibe el resultado completo, y dos tests E2E verifican
  la canalización entera incluido el MP4 final
  (`studio/tests/test_m6_production_wiring.py:160-186`).
- **Cuesta:** la petición HTTP se retiene durante toda la producción; el límite
  real es `proxy_read_timeout 3600s` (`config/nginx.conf:248`). La alternativa
  (202 + job-id) rompería ese contrato.

### A-6 — Redacción en buffer en lugar de streaming

- **Gana:** simplicidad; no altera la contrapresión de subidas chunked.
- **Cuesta:** `SecretStreamRedactor.push` es O(n²) con cuerpos grandes, por lo que
  la redacción central de respuestas JSON se mantiene bufferizada
  (`docs/decisiones.md:280`).

---

## 4. Puntos de compromiso

| # | Compromiso | Decisión tomada | Coste aceptado |
|---|---|---|---|
| T-1 | Fail-closed vs fail-open en arranque | Fail-closed: exit 20 (`comfyctl/src/strict.rs:1568`) | Un host límite no arranca; el operario debe alquilar otra oferta |
| T-2 | Sincronía de `approve` | Sincrónico (`studio/comfy_studio/routers/productions.py:107-120`) | Producciones > 1 h darían 504 |
| T-3 | Confinamiento por basename vs error explícito | Colapsa a basename (`studio/comfy_studio/orchestration/adapters.py:258-282`) | El fallo se ve como "fichero no encontrado", no como "ruta rechazada" |
| T-4 | Runtime del supervisor | Rust nativo (`comfyctl/Cargo.toml:9-19`) | Duplicación de recolección con Python |
| T-5 | Redacción bufferizada | Buffer (`docs/decisiones.md:280`) | Cuerpos grandes se redactan de una vez |

---

## 5. Riesgos

| # | Riesgo | Probabilidad | Impacto | Mitigación actual |
|---|---|---|---|---|
| AR-1 | Build de imagen no validado localmente (sin Docker/GPU) | — | Alto | CI construye y hace smoke en cada push a `main` |
| AR-2 | Deriva entre catálogos y código | Media | Medio | Chequeos de paridad en CI (`scripts/check_profile_parity.py`) |
| AR-3 | Documentación de estado vivo desfasada | Media | Bajo | Ver `05-mismatch-report.md`; 3 desfases corregidos en esta rama |
| AR-4 | `params` del planner sin allowlist | Baja | Medio | Confinamiento de rutas ya cubre el vector de escritura |
| AR-5 | Timeout de espera de `studio-server` sin respaldo empírico | Media | Medio | Acotado y fail-closed (`comfyctl/src/supervisor.rs:342-377`) |

---

## 6. Conclusión

La arquitectura prioriza **seguridad, integridad de datos y control de coste**
sobre **latencia y flexibilidad**. Los compromisos T-1 a T-5 son coherentes con
esos motores y están evidenciados en el código. El riesgo dominante no es de
diseño sino de **validación**: sin GPU ni Docker en el entorno de generación, las
afirmaciones sobre arranque real dependen de la CI.
