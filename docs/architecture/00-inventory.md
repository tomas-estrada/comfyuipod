# 00 — Inventario

**Rama:** `docs/maad-architecture-2026-11-10` · **Base:** `main` `b7f21de`
**Fecha:** 2026-11-10 · **Fase:** 1 de 4 (inventario)

Convención de confianza usada en todo el documento:
- **confirmado** — leído directamente en el fichero citado.
- **inferido** — derivado de evidencia indirecta; la derivación se indica.
- **hueco** — no verificable en este entorno; se marca como pendiente.

---

## 1. Módulos

### 1.1 `comfyctl/` — supervisor y servidor de estado (Rust)

Binario nativo. 20 ficheros `.rs` en `src/`, ~7 800 líneas.

| Módulo | Líneas | Responsabilidad | Confianza |
|---|---|---|---|
| `comfyctl/src/main.rs` | 119 | CLI; propaga el código de salida del subcomando | confirmado |
| `comfyctl/src/config.rs` | 463 | `RuntimeConfig` desde entorno; defaults y normalización de `MODEL_PROFILE` | confirmado |
| `comfyctl/src/supervisor.rs` | 1015 | Bucle de supervisión de ComfyUI, nginx, ttyd y `studio-server`; truncado de logs | confirmado |
| `comfyctl/src/strict.rs` | 1572 | Comprobaciones estrictas S1–S17; resultado `fatal` → código 20 | confirmado |
| `comfyctl/src/http.rs` | 365 | Servidor HTTP `:18189` (`/status`, `/status/api`, `/report.md`, `/diag`, `/logs/*`) | confirmado |
| `comfyctl/src/report.rs` | 682 | Generación determinista de `/report.md` | confirmado |
| `comfyctl/src/collect/` (6 ficheros) | 1 937 | Recogida de GPU, disco, memoria, procesos, deriva y fases | confirmado |
| `comfyctl/src/redact.rs` | 220 | Redacción de secretos en superficie pública | confirmado |
| `comfyctl/src/triage.rs` | 209 | Motor de triaje automático sobre logs | confirmado |
| `comfyctl/src/logtail.rs` | 108 | Cola de logs con lista blanca de nombres | confirmado |
| `comfyctl/src/diag.rs` | 367 | Autodiagnóstico `/diag` | confirmado |
| `comfyctl/src/sys.rs` | 321 | Ejecución de comandos con timeout | confirmado |
| `comfyctl/src/storage.rs` | 126 | Cálculo de requisito de almacenamiento | confirmado |
| `comfyctl/src/idle.rs` | 271 | Política de inactividad | confirmado |

`comfyctl/src/lib.rs:3-15` declara los 13 módulos `pub`, exponiendo la API de
biblioteca usada por los tests de integración (`comfyctl/tests/`).

### 1.2 `studio/comfy_studio/` — servidor FastAPI y agente (Python)

44 módulos de nivel superior (~25 000 líneas) y 16 subpaquetes.

| Subpaquete | Ficheros `.py` | Responsabilidad | Confianza |
|---|---|---|---|
| `routers/` | 16 | Rutas `/studio/api/*` registradas por `register_all_routes` | confirmado |
| `engines/` | 31 | Motores de render (comfyui, editor, upscaler, qa, …) | confirmado |
| `orchestration/` | 18 | Grafo de producción, planner, checkpointer, lease de GPU | confirmado |
| `atlas/` | 19 | Atlas georreferenciado (adquisición, dedupe, composición) | confirmado |
| `creative/` | 8 | Brief, guion, storyboard, variantes | confirmado |
| `perception/` | 5 | Índice CLIP, índice facial, detección | confirmado |
| `asr/` | 3 | Transcripción y diarización | confirmado |
| `agent/` | 4 | Bucle del agente, cliente LLM, cinturón de 54 herramientas | confirmado |
| `agent_brain/` | 4 | Memoria y capacidades del agente | confirmado |
| `ml/` | 5 | Workers ML en subproceso aislado | confirmado |
| `edl/` | 4 | Listas de decisión de edición | confirmado |
| `stubs/` | 3 | Stubs deterministas para tests | confirmado |
| `templates/` | 4 | Registro y validación de plantillas | confirmado |
| `ads/` | 3 | Estudio de anuncios | confirmado |
| `montage/` | 2 | Montaje desde brief | confirmado |
| `data/demos/` | 2 | Demos ejecutables | confirmado |

Módulos de nivel superior de mayor peso:
`projects.py` (2 554), `asset_acquire.py` (2 435), `comfyui_client.py` (2 057),
`webutil.py` (830), `config.py` (858), `app.py` (717).

### 1.3 `panel/` — interfaz web (JavaScript vainilla)

8 ficheros. `app.js` (168 069 B) e `index.html` (61 280 B) constituyen el panel;
`agent.js` (37 533 B) y `agent.html` el compositor del agente. Sin framework ni
bundler: se sirven como ficheros estáticos.

`panel/test_agent_composer.py:1` ejecuta `agent.js` real en un contexto `vm` de
Node con un DOM mínimo.

### 1.4 `config/` — configuración horneada en la imagen

15 entradas. Las relevantes para la arquitectura:

| Fichero | Rol | Confianza |
|---|---|---|
| `config/nginx.conf` | Única superficie pública (`:8188`) | confirmado |
| `config/nodes.lock` | SHAs fijados de ComfyUI core y 12 custom nodes | confirmado |
| `config/constraints.txt` | Terna PyTorch y cotas de paquetes críticos | confirmado |
| `config/models-core.json` | Catálogo de 26 modelos core | confirmado |
| `config/models-studio.json` | Catálogo de estudio (Fase 3) | confirmado |
| `config/extra_model_paths.yaml` | Rutas de modelos para ComfyUI | confirmado |
| `config/portal/` | Portal `/home` | confirmado |
| `config/probes/` | Sondas Python de health | confirmado |
| `config/_starting.html` | Pantalla de espera auto-recargable | confirmado |

### 1.5 `scripts/` — arranque y adquisición

| Fichero | Rol | Confianza |
|---|---|---|
| `scripts/start.sh` | Punto de entrada del contenedor; 7 fases | confirmado |
| `scripts/download-models.sh` | Adquisición por fases (1, 2, 3, bootstrap) | confirmado |
| `scripts/storage_requirement.py` | Deriva el requisito de disco por perfil | confirmado |
| `scripts/check_models_manifest.py` | Valida manifiestos | confirmado |
| `scripts/check_profile_parity.py` | Paridad perfil bash ↔ manifiesto | confirmado |
| `scripts/update_csp_hashes.py` | Sincroniza hashes CSP ↔ HTML | confirmado |
| `scripts/prune_model_cache.py` | Limpieza de caché | confirmado |
| `scripts/triage_patterns.json` | 17+ patrones de triaje | confirmado |

### 1.6 `tools/mac/` — cliente de operario

`tools/mac/comfy:1` — helper Bash para alquilar y operar el pod desde macOS.
Pasa credenciales por stdin a `curl -K -` para no exponerlas en `ps`.

### 1.7 `atlas/` — datos del atlas

Base de datos SQLite del atlas georreferenciado del Valle de Aburrá
(`atlas/catalog.db`, binario versionado).

### 1.8 `Dockerfile` y `.github/workflows/`

`Dockerfile:1` construye la imagen en 6 bloques comentados.
`.github/workflows/docker-publish.yml:1` define el pipeline con 3 jobs.

---

## 2. Dependencias

### 2.1 Runtime — Rust (`comfyctl/Cargo.toml:9-19`)

| Dependencia | Versión | Uso | Confianza |
|---|---|---|---|
| `chrono` | 0.4 (sin `default-features`) | Marcas de tiempo | confirmado |
| `libc` | 0.2 | Llamadas al sistema | confirmado |
| `pico-args` | 0.5 | Parseo de CLI | confirmado |
| `regex` | 1 | Redacción y triaje | confirmado |
| `serde` / `serde_json` | 1.0 | Serialización JSON | confirmado |
| `tiny_http` | 0.12 (sin `default-features`) | Servidor HTTP `:18189` | confirmado |

`comfyctl/rust-toolchain.toml:2` fija la toolchain en 1.86.0, con `rustfmt` y
`clippy` (`:3`).

### 2.2 Runtime — Python (`studio/pyproject.toml:11-20`)

| Dependencia | Versión | Uso | Confianza |
|---|---|---|---|
| `fastapi` | 0.162.0 | Framework del servidor Studio | confirmado |
| `uvicorn` | 0.54.0 | ASGI server (`studio-server`) | confirmado |
| `httpx` | 0.28.1 | Cliente HTTP hacia ComfyUI y LLM | confirmado |
| `openai` | 3.42.0 | Cliente LLM compatible-OpenAI | confirmado |
| `pydantic` | 2.14.2 | Modelos de request/response | confirmado |
| `langgraph` | 1.2.20 | Grafo de orquestación de producción | confirmado |
| `sqlite-vec` | 0.1.9 | Índice vectorial (percepción) | confirmado |
| `python-multipart` | 0.0.33 | Subida de ficheros | confirmado |

`requires-python = ">=3.11"` (`studio/pyproject.toml:10`).

Dev: `pytest==9.2.0`, `ruff==0.17.3`, `pillow>=10.0.0`
(`studio/pyproject.toml:22-31`).

### 2.3 Runtime — sistema (imagen)

| Componente | Versión | Dónde se fija | Confianza |
|---|---|---|---|
| Ubuntu | 24.04 | `Dockerfile:1` | confirmado |
| PyTorch | 2.9.1+cu128 | `Dockerfile:111-114` | confirmado |
| CUDA | 12.8 | `Dockerfile:111-114` | confirmado |
| Node.js | 22 LTS | `Dockerfile:248` | confirmado |
| Nginx | paquete de Ubuntu 24.04 | `config/nginx.conf:1` | confirmado |
| ffmpeg / ffprobe | paquete de Ubuntu | `scripts/start.sh:1` | confirmado |

La terna de PyTorch está acotada además por `config/constraints.txt:10-12`, y el
sufijo `+cu128` actúa como guardrail (ver comentario en
`config/constraints.txt:5-8`).

### 2.4 Dependencias internas (acoplamiento entre módulos)

```
panel/  ──HTTP──▶  nginx :8188  ──proxy──▶  studio-server :18190
                                            comfyctl       :18189
                                            ComfyUI        :18188
                                            ttyd           :7681
```

Confirmado en `config/nginx.conf:53-366` (único bloque `server`) y en
`scripts/start.sh:17-18` (`COMFY_APP_DIR`, `COMFY_DATA_DIR`).

`comfyctl` **no** depende de Python en tiempo de compilación, pero
`comfyctl/src/supervisor.rs:336-377` lanza y espera a `studio-server` antes del
`strict-check`.

`studio/comfy_studio/asset_acquire.py:1` es la implementación de referencia de la
adquisición; `scripts/download-models.sh:231-250` la invoca vía
`asset_acquire_cli`. **Inferido:** el shell es un envoltorio fino sobre el CLI de
Python.

---

## 3. Documentación `.md` previa

61 ficheros Markdown. Clasificación por madurez:

### 3.1 Fuentes de verdad declaradas

| Documento | Rol | Confianza |
|---|---|---|
| `README.md` | Capacidades, perfiles, variables de entorno | confirmado |
| `AGENTS.md` | Convenciones y verificaciones obligatorias | confirmado |
| `CONTRIBUTING.md` | Guía de contribución | confirmado |
| `docs/ARCHITECTURE.md` | Componentes y límites de confianza | confirmado |
| `docs/ARCHITECTURE_CANONICAL.md` | Arquitectura canónica en 5 fases | confirmado |
| `docs/decisiones.md` | Decisiones técnicas con justificación | confirmado |
| `docs/POLITICA_ARQUITECTURA_RECURSOS.md` | Política de recursos | confirmado |

`docs/README.md:3` advierte explícitamente que los informes de campaña son
**registros fechados, no estado vivo**, y que `docs/ultra/status.json` apunta a un
`main` anterior.

### 3.2 Registros fechados (no estado vivo)

- `docs/auditorias/` — 15 informes (V1–V9, M3, integral, implementaciones).
- `docs/propuestas/` — 10 planes e investigaciones.
- `docs/ultra/` — 11 informes de sesión y campaña (W01, W25, W26, sesiones).
- `docs/upgrades/` — 2 procedimientos de migración (añadidos en `a544d6b`).

### 3.3 Documentación por área

`docs/plantillas.md`, `docs/estudio_anuncios.md`, `docs/cinematic-studio.md`,
`docs/atlas.md`, `docs/atlas-merge-runbook.md`, `docs/atlas-casos-aceptacion.md`,
`docs/walkthrough.md`, `docs/engines/` (5 motores), `docs/contratos/` (esquemas
JSON congelados).

### 3.4 Solapamiento detectado

**Hueco:** existe solapamiento temático entre `docs/ARCHITECTURE.md`,
`docs/ARCHITECTURE_CANONICAL.md` y `docs/walkthrough.md`. No se ha verificado
hasta qué punto divergen; se trata en `05-mismatch-report.md`.

---

## 4. Superficie de red verificada

| Servicio | Dirección | Puerto | Origen | Confianza |
|---|---|---|---|---|
| Nginx (público) | `0.0.0.0` / `[::]` | 8188 | `config/nginx.conf:54-55` | confirmado |
| ComfyUI | `127.0.0.1` | 18188 | `scripts/start.sh:12` | confirmado |
| comfyctl HTTP | `127.0.0.1` | 18189 | `scripts/start.sh:11` | confirmado |
| studio-server | `127.0.0.1` | 18190 | `comfyctl/src/supervisor.rs:336` | confirmado |
| ttyd (terminal) | `127.0.0.1` | 7681 | `scripts/start.sh:386` | confirmado |

Todos los servicios internos escuchan exclusivamente en loopback; solo Nginx
expone el puerto público.

---

## 5. Huecos registrados

| # | Hueco | Impacto | Confianza |
|---|---|---|---|
| H-01 | `cargo`, `docker` y `nvidia-smi` no disponibles en el entorno de generación | Los chequeos de Rust y el build de imagen no se ejecutaron localmente | hueco |
| H-02 | Coste real de arranque de `studio-server` no medido | El timeout de `wait_for_studio_ready` es un valor sin respaldo empírico publicado | hueco |
| H-03 | Solapamiento entre los tres documentos de arquitectura existentes | Riesgo de deriva documental | hueco |
| H-04 | Versión exacta de Nginx y ffmpeg no fijada | La imagen depende de los paquetes de Ubuntu 24.04 en el momento del build | hueco |
