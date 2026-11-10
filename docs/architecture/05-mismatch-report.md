# 05 — Informe de desfases (contraste)

**Rama:** `docs/maad-architecture-2026-11-10` · **Base:** `main` `b7f21de`
**Fecha:** 2026-11-10 · **Fase:** 4 de 4 (contraste)

Contraste entre lo que la documentación **afirma** y lo que el código **hace**.
Cada desfase declara evidencia de ambos lados, severidad y estado.

Clasificación de documentos:
- **Estado vivo** — debe reflejar `main` hoy: `README.md`, `AGENTS.md`,
  `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_CANONICAL.md`, `docs/walkthrough.md`,
  `docs/POLITICA_ARQUITECTURA_RECURSOS.md`.
- **Registro fechado** — describe un momento pasado; se espera que derive:
  `docs/decisiones.md`, `docs/auditorias/`, `docs/propuestas/`, `docs/ultra/`,
  `docs/atlas-merge-runbook.md`. `docs/README.md:3` lo advierte explícitamente.

---

## 1. Desfases en documentación de estado vivo

### M-01 — Recuento de motores: 16 vs 21 — CORREGIDO en esta rama

| Lado | Evidencia |
|---|---|
| Documento | `docs/ARCHITECTURE.md:19` — "Contrato uniforme de 16 motores" |
| Código | `studio/comfy_studio/engines/registry.py` — `ENGINE_CLASSES` con 21 entradas |
| Documento | `docs/POLITICA_ARQUITECTURA_RECURSOS.md:132` — "los 16 motores" |

Severidad: **baja** (cifra obsoleta, no contraria a la arquitectura).
Estado: **corregido** — ambos pasan a 21.
Nota: `README.md:21` ya decía 21 (corregido en una pasada anterior); el desfase
sobrevivió porque el barrido no alcanzó estos dos ficheros.

### M-02 — Enumeración de herramientas incompleta — CORREGIDO en esta rama

| Lado | Evidencia |
|---|---|
| Documento | `docs/walkthrough.md:13` — afirma "54 herramientas" y enumera 46 |
| Código | `studio/comfy_studio/agent/tools.py` — 46 `ToolSpec`; `studio/comfy_studio/atlas/agent_tools.py` — 8 specs Atlas. Total **54** |

Severidad: **baja** (la cifra es correcta; la lista está 8 nombres corta).
Estado: **corregido** — añadidos los 8 nombres Atlas.
Nota: mismo defecto que tuvo `README.md:21`, corregido allí con anterioridad.

### M-03 — Topología de puertos — SIN DESFASE

| Afirmación | Código |
|---|---|
| `docs/ARCHITECTURE.md:9` — Nginx única entrada pública en 8188 | `config/nginx.conf:54-55` ✅ |
| `docs/ARCHITECTURE.md:31` — Studio en `127.0.0.1:18190` | `comfyctl/src/supervisor.rs:336` ✅ |
| `docs/ARCHITECTURE_CANONICAL.md:29-31` — mismo modelo | ✅ |

Confirmado: **sin desfase**.

---

## 2. Desfases esperados en registros fechados (no son defectos)

| Documento | Afirmación | Realidad hoy | Por qué es esperado |
|---|---|---|---|
| `docs/decisiones.md:191` | "699 passed" | 2524 passed | Registro de una sesión |
| `docs/decisiones.md:280` | `routers/` con 9 módulos | 15 módulos | Describe la decomposición en su fecha |
| `docs/decisiones.md:280` | `app.py` de 2.622 líneas | 717 líneas | La propia decisión lo redujo |
| `docs/walkthrough.md:42` | "699 passed" | 2524 passed | Registro de corrida local |
| `docs/atlas-merge-runbook.md:26` | "819 passed" | 2524 passed | Runbook fechado |

`docs/README.md:3` marca estos ficheros como "registros fechados, no estado vivo".
**No se corrigen**: reescribirlos destruiría el histórico que los justifica.

---

## 3. Falsos positivos descartados (y por qué importan)

Durante el barrido se detectaron dos variables de entorno que un grep estático
marcó como "documentadas pero nunca leídas". **Ambas son consumidas**; el grep se
equivocó.

### F-01 — `COMFY_DISK_GB`

- Sospecha: no aparece en `studio/`, `scripts/` ni `comfyctl/`.
- Realidad: la consume `tools/mac/comfy:1080` y `:1097` — un fichero **sin
  extensión**, excluido del barrido.
- Lección: un grep sobre extensiones concretas da falsa confianza.

### F-02 — `STUDIO_STUB_COMFYUI`

- Sospecha: el literal no aparece en código de producción.
- Realidad: se **construye en tiempo de ejecución** —
  `studio/comfy_studio/engines/__init__.py:116`
  (`f"STUDIO_STUB_{engine.upper()}"`), y se lee en `:121`.
- Lección: un nombre ausente del código no es un nombre fantasma; puede estar
  derivado. Este es exactamente el error que motivó el commit `b7f21de`, donde
  corregir un nombre "fantasma" rompió un test.

---

## 4. Solapamiento estructural

| Solapamiento | Ficheros | Observación |
|---|---|---|
| Arquitectura general | `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_CANONICAL.md`, `docs/walkthrough.md` | Tres vistas del mismo sistema sin un puntero único de "cuál manda" |
| Variables de entorno | `README.md:138-186`, `docs/ARCHITECTURE_CANONICAL.md:120-160` | La tabla del README es un subconjunto de la canónica |

**Hueco:** no se ha verificado hasta qué punto divergen los tres documentos de
arquitectónica más allá del desfase M-01. Requeriría una comparación assertion a
assertion.

---

## 5. Resumen

| Categoría | Encontrados | Corregidos | Pendientes |
|---|---|---|---|
| Estado vivo | 2 | 2 | 0 |
| Registros fechados | 5 | 0 (por diseño) | 5 |
| Falsos positivos | 2 | — | 0 |
| Solapamiento estructural | 2 | 0 | 2 (hueco) |

**Conclusión:** la documentación de estado vivo queda alineada con el código tras
esta rama. La deriva restante está en registros fechados, que es el comportamiento
diseñado. El riesgo real que queda es de **proceso**, no de contenido: los
desfases M-01 y M-02 sobrevivieron a una pasada anterior porque el barrido no
alcanzó esos ficheros.
