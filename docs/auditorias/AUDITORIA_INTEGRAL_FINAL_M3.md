# Auditoría Adversarial Integral y Verificación Exhaustiva — ComfyUIPod (Hito M3)

**Fecha:** 2026-10-28 · **Alcance:** Auditoría completa de código, configuración, CI y documentación tras la finalización de los Hitos M1, M2 y M3 (Percepción visual/facial con `sqlite-vec`, ASR con `whisperx` y fallback `sm_120`, Diarización con `cam++`, Montaje y renderizado determinista EDL con `ffmpeg`, Webhooks firmados HMAC-SHA256 y Agente de 10 herramientas).

---

## 1. Resumen Ejecutivo y Veredicto

Se ha realizado una auditoría exhaustiva y adversarial de extremo a extremo en todo el repositorio. Como resultado, se han identificado, corregido y verificado empíricamente todas las discrepancias y fallos encontrados:

1. **Corrección de linting estricto en Rust (`cargo clippy`)**:
   - En [`comfyctl/src/config.rs`](file:///c:/Users/estra/OneDrive/Documents/ComfyUIPod/comfyctl/src/config.rs#L16), la documentación del enum `ModelProfile::Unreal` contenía la palabra `Hunyuan3D` sin comillas invertidas, disparando `clippy::doc_markdown` bajo la bandera obligatoria de CI `-D warnings`.
   - *Corrección*: Se agregaron las comillas invertidas correspondientes: ``(`Hunyuan3D` 2.1, Blender)``.

2. **Limpieza de advertencias de formato y sintaxis en Python (`ruff`)**:
   - En [`studio/comfy_studio/blender.py`](file:///c:/Users/estra/OneDrive/Documents/ComfyUIPod/studio/comfy_studio/blender.py):
     - L219: Se reemplazó el formato heredado `stem = r"%s" % stem` (`UP031`) por f-string `f'stem = r"{stem}"'`.
     - L276 y L328: Se dividieron las líneas que excedían el límite de 100 caracteres (`E501`).
     - L352: Se actualizó `except asyncio.TimeoutError` a `except TimeoutError` (`UP041`).
   - `ruff check studio panel scripts config/probes` pasa con 0 errores y 0 advertencias.

3. **Prevención de bloqueo en el bucle de eventos (`Event Loop`) en FastAPI**:
   - En [`studio/comfy_studio/app.py`](file:///c:/Users/estra/OneDrive/Documents/ComfyUIPod/studio/comfy_studio/app.py#L1767), el endpoint `POST /studio/api/projects/{project_id}/edl/highlights` ejecutaba `generate_highlights_edl` síncronamente en el hilo principal (lo que disparaba transcripciones o análisis acústico de archivos multimedia bloqueando peticiones concurrentes y SSE).
   - *Corrección*: Se delegó la llamada pesada mediante `await run_in_threadpool(generate_highlights_edl, ...)`.

4. **Protección contra timestamps negativos en diarización corta (`M3-SCR-017`)**:
   - En [`studio/comfy_studio/asr/diarize.py`](file:///c:/Users/estra/OneDrive/Documents/ComfyUIPod/studio/comfy_studio/asr/diarize.py#L120), si un archivo de audio tenía una duración menor a 0.2 segundos, la expresión `dur - 0.2` generaba un timestamp de inicio negativo (`start: -0.1`).
   - *Corrección*: Se acotó con `max(0.0, min(dur - 0.2, round(mid + 0.1, 3)))`.

5. **Alineación de pruebas de registro de plantillas**:
   - Las pruebas en [`studio/tests/test_templates_registry.py`](file:///c:/Users/estra/OneDrive/Documents/ComfyUIPod/studio/tests/test_templates_registry.py) y suites de herramientas fueron actualizadas para contemplar la totalidad de las 7 plantillas curadas del pod (incluyendo `hunyuan3d_image_to_3d` para la ingesta de imagen a malla 3D).

---

## 2. Matriz de Validación Automatizada del Repositorio

| Validador / Suite | Comando | Resultado |
|---|---|---|
| **Linter Python (Ruff)** | `ruff check studio panel scripts config/probes` | **PASÓ** (`All checks passed!`, 0 avisos) |
| **Compilación de Sondas y Scripts** | `python -m py_compile config/probes/*.py scripts/check_models_manifest.py` | **PASÓ** (`exit code 0`) |
| **Validador de Plantillas Studio** | `python -m comfy_studio.templates` | **PASÓ** (7 plantillas validadas correctamente) |
| **Manifiesto de Modelos (Offline & Fixture)** | `python scripts/check_models_manifest.py --fixture --offline` | **PASÓ** (77 base + 2 opt-in = 79 modelos) |
| **Suite de Pruebas Python (FastAPI/Agent/M3)** | `pytest studio/tests -q` | **PASÓ** (**181 passed** en 97.70s) |
| **Formato Rust** | `cargo fmt --manifest-path comfyctl/Cargo.toml --check` | **PASÓ** (`exit code 0`) |
| **Linter Rust Estricto (Clippy)** | `cargo clippy --manifest-path comfyctl/Cargo.toml --all-targets -- -D warnings` | **PASÓ** (0 warnings, 0 errores) |
| **Tests de Integración y Unitarios Rust** | `cargo test --manifest-path comfyctl/Cargo.toml --locked` | **PASÓ** (25 tests de integración en 8 suites) |
| **Contratos de CI (`docker-publish.yml`)** | 17 patrones de triaje, 26 modelos base O-11, portal `< 40 KiB` sin CDNs, JSON Schemas congelados | **PASÓ** (`exit code 0`) |
| **Finales de Línea (`LF`)** | Inspección binaria de `\r\n` en archivos fuente y scripts | **PASÓ** (0 archivos con CRLF) |

---

## 3. Verificaciones de Seguridad y Robustez de Producción

1. **Redacción de Secretos (`SecretRedactor`)**:
   - Probado en `comfyctl/tests/redact_test.rs` y `studio/comfy_studio/config.py`: las claves `COMFY_PASS`, `STUDIO_LLM_KEY`, `STUDIO_LLM_FALLBACK_KEY`, `HF_TOKEN`, tokens `sk-*`/`hf_*` y credenciales de URLs se redactan automáticamente en `/status/api`, `/report.md` y endpoints públicos de Studio.
2. **Defensas Anti-Path Traversal**:
   - `save_upload` y `ProjectStore` validan la seguridad de nombres de archivo mediante `is_relative_to` impidiendo accesos fuera de `COMFY_DATA_DIR`.
3. **Validación Anti-SSRF en Webhooks**:
   - `validate_webhook_url` en `webhooks.py` rechaza explícitamente esquemas no HTTP/HTTPS, loopback (`127.0.0.1`, `localhost`, `::1`) y rangos privados RFC1918 / metadatos link-local (`169.254.169.254`).
4. **Resiliencia de Procesamiento y Backoff**:
   - Webhooks no reintentan respuestas 4xx definitivas (excepto 408 y 429).
   - El ejecutor de EDL impone una cota dura de 200 cortes por lista (`MAX_EDL_CUTS = 200`) y timeouts encapsulados en `RuntimeError`/`ValueError` retornando HTTP 400.
