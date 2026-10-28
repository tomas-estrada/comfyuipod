# Auditoría Senior V9 (Cierre Post-M2, Código, Tests y Documentación) — ComfyUIPod

**Fecha:** 2026-10-27 · **Alcance:** Revisión integral full-stack post-M2 (`comfyctl` en Rust, `studio/` y `panel/` en Python/JS, `scripts/*.sh`, `tools/mac/comfy`, `config/*`, `Dockerfile`, `.github/workflows/docker-publish.yml` y toda la documentación en `README.md` y `docs/`) · **Metodología:** Ejecución directa de linters, validadores y suites de pruebas + inspección cruzada de contratos API/UI y referencias documentales.

---

## 1. Resumen Ejecutivo y Veredicto

Tras la implementación del Hito M2 (**Comfy Studio**, `MODEL_PROFILE=studio`), varios agentes previos habían iterado sobre el repositorio dejando cinco discrepancias reales entre código, pruebas, linters y documentación, además de archivos placeholder de auditoría sin implementar.

En esta auditoría V9 se han corregido y verificado todos los puntos pendientes:

1. **`V9-01` — Desalineación de contrato JSON en `renderTemplateDoc` ([`panel/app.js`](../../panel/app.js))**:
   - **Problema**: [`studio/comfy_studio/templates/registry.py`](../../studio/comfy_studio/templates/registry.py) serializa cada plantilla en `GET /studio/api/templates` con `name`, `docs` y `docs.defaults`, mientras que `renderTemplateDoc(tpl)` en [`panel/app.js`](../../panel/app.js) leía `tpl.documentation`, `tpl.title` y `doc.performance_defaults`. Como resultado, la tarjeta de ayuda contextual (`#template-doc-box`) nunca se mostraba al seleccionar una plantilla real servida por el backend.
   - **Solución**: Se actualizó `renderTemplateDoc` en [`panel/app.js`](../../panel/app.js) (`L1117-L1123`) para aceptar ambos esquemas (`tpl.docs || tpl.documentation`, `tpl.name || tpl.title || tpl.id`, `doc.defaults || tpl.performance_defaults`) y se añadió el test de regresión `test_panel_render_template_doc_uses_api_template_schema` en [`studio/tests/test_panel_web.py`](../../studio/tests/test_panel_web.py) (`L1431-L1451`).

2. **`V9-02` — Normalización UTF-8 y CRLF en [`tools/mac/comfy`](../../tools/mac/comfy) y [`studio/tests/test_mac_helper.py`](../../studio/tests/test_mac_helper.py)**:
   - **Problema**: Al ejecutar `tools/mac/comfy` bajo Git Bash en Windows (donde `python3` hereda la página de códigos `cp1252` de consola y emite `\r\n`), los guiones largos (`—`) y tildes en `print(...)` provocaban `UnicodeEncodeError` o mojibake, y la captura `offer_id="$(python3 -c ...)"` en `cmd_rent` retenía un `\r` final que desalineaba los argumentos pasados a `vastai create instance`.
   - **Solución**: Se exportó `PYTHONUTF8=1` y `PYTHONIOENCODING=utf-8` al inicio de [`tools/mac/comfy`](../../tools/mac/comfy) (`L27-L31`), se añadió `sys.stdout.reconfigure(encoding="utf-8", newline="\n")` en `resolve_instance` (`L163-L166`), se limpió `offer_id="${offer_id%$'\r'}"` en `cmd_rent` (`L992`) y se configuró `_run_comfy` en [`studio/tests/test_mac_helper.py`](../../studio/tests/test_mac_helper.py) (`L185-L202`) con `PYTHONUTF8=1` y `encoding="utf-8"`. Los 21 tests de `test_mac_helper.py` pasan al 100%.

3. **`V9-03` — Limpieza total de Ruff (`UP035` y `BLE001`) en [`scripts/check_models_manifest.py`](../../scripts/check_models_manifest.py) y [`config/probes/`](../../config/probes/)**:
   - **Problema**: [`scripts/check_models_manifest.py`](../../scripts/check_models_manifest.py) importaba `Callable` desde `typing` en lugar de `collections.abc` (`UP035` en Python 3.12), y las sondas de diagnóstico de una línea en [`config/probes/ort_cuda.py`](../../config/probes/ort_cuda.py), [`config/probes/python_imports.py`](../../config/probes/python_imports.py) y [`config/probes/torch_cuda.py`](../../config/probes/torch_cuda.py) carecían de `# noqa: BLE001` en sus bloques `except Exception` intencionales.
   - **Solución**: Se migró `from collections.abc import Callable` en [`scripts/check_models_manifest.py`](../../scripts/check_models_manifest.py) y se añadieron `# noqa: BLE001` y ajuste de longitud de línea (`<= 100` cols) en [`config/probes/`](../../config/probes/). `ruff check studio panel scripts config/probes` pasa con `0` errores.

4. **`V9-04` — Sincronización de documentación con `HEAD` ([`README.md`](../../README.md), [`docs/walkthrough.md`](../walkthrough.md), [`docs/GUIA_PASO_A_PRODUCCION.md`](../GUIA_PASO_A_PRODUCCION.md), [`docs/decisiones.md`](../decisiones.md))**:
   - **Problema**: Tras añadir M2 (`MODEL_PROFILE=studio`, `S11` `studio_health`, 12 custom nodes, 15 pasos de smoke CI y 23 tests de integración en Rust), [`README.md`](../../README.md), [`docs/walkthrough.md`](../walkthrough.md), [`docs/GUIA_PASO_A_PRODUCCION.md`](../GUIA_PASO_A_PRODUCCION.md) y [`docs/decisiones.md`](../decisiones.md) conservaban referencias antiguas a `S1..S10`, `10 custom nodes`, `14 pasos`, `15 tests` en Rust, rutas antiguas `config/portal/index.html` y números de línea desactualizados de `comfyctl/src/config.rs`.
   - **Solución**: Se actualizaron todas las tablas, conteos de pruebas, variables de entorno (`STUDIO_LLM_*`, `STUDIO_SERVER_PORT`, `STRICT_MIN_DISK_GB` con `180` GB para `studio`) y referencias de línea (`comfyctl/src/config.rs` `L42-L52`, `L281-L289`, `L308-L309`).

5. **`V9-05` — Higiene de `.gitignore`, `.dockerignore`, `.gitattributes` y artefactos de agentes externos**:
   - **Solución**: Se añadió `.kilo/` a [`.gitignore`](../../.gitignore) y [`.dockerignore`](../../.dockerignore), se excluyó `studio/tests` del contexto de imagen en [`.dockerignore`](../../.dockerignore) (preservando `!config/constraints.txt`), se reforzó [`.gitattributes`](../../.gitattributes) con `eol=lf` explícito para todos los archivos fuente y para `tools/mac/comfy`, y se eliminaron los archivos vacíos autogenerados por tareas programadas externas.

---

## 2. Matriz de Verificación Ejecutada

| Verificación | Comando | Resultado |
|---|---|---|
| **Linter Python (Ruff)** | `ruff check studio panel scripts config/probes` | `All checks passed!` (`0` warnings/errors) |
| **Compilación Python** | `python -m py_compile config/probes/*.py scripts/check_models_manifest.py` | `EXIT 0` |
| **Validador de plantillas M2** | `python -m comfy_studio.templates` | `OK: 6 plantillas validadas correctamente` |
| **Validador de manifiesto Fase 3** | `python scripts/check_models_manifest.py --fixture` y `--offline` | `OK` (`77` archivos base = `146,049,867,640` B + `1` opt-in = `18,413,068,672` B) |
| **Suite de tests Python/JS/Bash** | `pytest studio/tests -q` | **`97 passed`** (`0` fallos) |
| **Formato Rust** | `cargo fmt --manifest-path comfyctl/Cargo.toml --check` | `EXIT 0` |
| **Linter Rust (Clippy estricto)** | `cargo clippy --manifest-path comfyctl/Cargo.toml --all-targets -- -D warnings` | `EXIT 0` (`0` warnings) |
| **Tests de integración Rust** | `cargo test --manifest-path comfyctl/Cargo.toml --locked` | **`23 passed`** en 8 suites (`strict_test` [9], `supervisor_test` [5], `phases_test` [3], `http_test` [2], `triage_test` [1], `report_test` [1], `redact_test` [1], `logtail_test` [1]) |
| **Contratos CI y paridad `O-11`** | Validación de `scripts/triage_patterns.json` (17 reglas), `config/portal/index.html` (`< 40 KiB`, 0 CDNs), `26/26` modelos base y esquemas JSON Schema congelados | `OK` |
| **Finales de línea (`LF`)** | Inspección binaria de `\r\n` en archivos del repositorio | `0` archivos con `CRLF` |
