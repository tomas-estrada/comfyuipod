# Atlas — corrección de defectos y casos de aceptación (PR #63, revisión independiente)

Cabeza revisada: `799dea078cf877e2f6b2b44886b2446c4e88ca88`. Cada defecto se reprodujo,
se corrigió y quedó fijado con una prueba de regresión (`studio/tests/test_atlas_regresiones.py`,
`test_atlas_aceptacion.py`).

## 1. Defecto → causa → corrección → prueba

| # | Defecto | Causa | Corrección | Prueba |
|---|---------|-------|------------|--------|
| 1 | El fondo no llegaba al grafo (mismo grafo para dos fotos) | `background_image` no es parámetro de `wan_t2v` y `validate_params(strict=False)` lo descartaba | El adaptador inyecta el fondo por la entrada de imagen declarada (`wan_i2v` → `image_input` → `LoadImage`), lo copia al input de ComfyUI y verifica sha256; plantilla sin entrada de imagen falla tipado; el planner stub usa `wan_i2v` cuando hay atlas | `test_grafo_final_carga_la_referencia_y_cambia_con_el_fondo`, `test_adapter_inyecta_image_input_y_rechaza_t2v` |
| 2 | `title_match` asignaba «Puente Beta» a «Puente Alfa» | bastaba compartir el token «puente» dentro de 2,5 km | sólo coinciden nombre/alias COMPLETOS (frase, len≥4) y gana la semilla más cercana por GPS | `test_title_match_no_confunde_puentes_alfa_beta` |
| 3 | La ubicación del prompt se ignoraba (ganaba la selección previa) | el planner llamaba `prepare_shot(prompt=…)` sin resolver el lugar | `extract_place_hint` detecta el lugar mencionado en el prompt (frase completa, coincidencia más larga) y prevalece sobre la selección | `test_prompt_explicito_prevalece_sobre_seleccion` |
| 4 | Vista con rumbo 0° aceptada ante 180° con `status=ok` | no había evaluación angular | `select_view` evalúa rumbo (diferencia angular > 45° → `unsupported_camera` con `heading_mismatch` y alternativas), posición (>150 m) y FOV; lo desconocido queda como limitación (`heading_unknown`/`fov_unknown`), nunca como compatible | `test_select_view_rechaza_rumbo_incompatible_y_conserva_desconocido` |
| 5 | Municipio desconocido → «Medellín» | `return "Medellín"` por defecto y bbox amplio | fuera de todo bbox → `desconocido`; el bbox queda marcado en provenance como `bbox_heuristic_no_verificado` (acelerador, no identidad) | `test_municipio_fuera_de_alcance_es_desconocido` |

## 2. Depuración de la biblioteca (medida, no sólo contact sheet)

Clasificación por contenido (`content_class` en `atlas_refs`, esquema v3) aplicada a TODO el
material adquirido (`purge_library`), no sólo a las miniaturas:

| clase | n | tratamiento |
|-------|--:|-------------|
| fondo_urbano | 785 | usable como fondo (612 enviadas en el bundle por cap de repo) |
| detalle_macro (insecto `acrochaeta`, flores…) | 258 | usable=0 |
| territorialmente_incompatible (p. ej. alumbrados de Marinilla) | 126 | usable=0 |
| historico (<2005) | 10 | referencia histórica, nunca fondo actual |
| comida_objeto (ajiaco…) | 10 | usable=0 |
| retrato_persona | 8 | usable=0 |
| interior | 6 | usable=0 |

Catálogo, vistas, cobertura, previews, atribuciones y manifest reconstruidos desde cero
(`build_bundle` + `write_manifest` + `verify_bundle` → ok). Objetivos del encargo cumplidos
tras la depuración: **612 referencias útiles enviadas (≥500)** y **152 lugares con vistas
utilizables (≥50)**, con los 5 municipios prioritarios cubiertos. No se ampliaron radios ni
se debilitaron criterios para recuperar cifras. Déficit real: límites municipales oficiales
(GeoMedellín inalcanzable y sin licencia verificable) → el municipio por bbox queda marcado
heurístico y fuera de alcance es «desconocido».

## 3. Recorrido real de producción (CPU)

`prompt → lugar → referencia compatible → grafo/composición → salida → procedencia`:

- **Composición sobre fotografía (preserva el fondo):** `atlas_compose_shot` (PIL, CPU)
  pega un asset con alfa sobre el fondo verificado y **comprueba píxeles idénticos fuera de
  la región del asset** (`background_pixels_preserved_outside_foreground`), guarda la salida
  en `outputs` del proyecto y registra la producción en `catalog.db` con ref/hash/licencia.
- **I2V real:** el grafo final contiene `LoadImage` apuntando al fondo (`image_input`);
  dos fondos distintos producen grafos distintos (prueba). Una plantilla sin entrada de
  imagen no puede declarar uso del atlas (falla tipado).
- **Cambio de cámara:** requiere compatibilidad real (rumbo/posición/FOV) o devuelve
  `unsupported_camera` + vistas alternativas; no se rellena lo oculto.
- La elección automática considera la tarea (p. ej. «carro por una avenida» →
  road_corridor/bridge/street/intersection) y una ubicación explícita sin cobertura produce
  limitación concreta con alternativas, sin sustitución silenciosa.

## 4. Escenarios y cobertura con significado

- 152 escenarios `photo_bg` **verified** (id estable `scn_<place>_v1`, refs, hashes,
  capacidades reales: preserve_background_pixels=true, translación libre=false,
  rotación en el punto=false, profundidad desde foto única=false).
- 16 candidatos `multiview` en **draft** (≥5 vistas GPS en ≤200 m, solapamiento por
  proximidad) con `reconstruction_validated=false` y receta COLMAP/nerfstudio pendiente de
  GPU. Ninguna reconstrucción se declara lista por tener comandos.

## 5. Continuidad e integridad

- La selección del panel es **por proyecto** (`selection_<project_id>.json`); la de un
  proyecto no contamina otros, y `state.metadata["atlas"]` de la producción prevalece.
- Al ejecutar/reanudar se verifica el sha256 del fondo contra el catálogo; un archivo
  ausente o modificado falla tipado (pruebas en regresiones y aceptación).
- El catálogo base distribuido (`atlas/`) queda separado del estado mutable (selecciones y
  producciones viven en `selection_*.json` y en la tabla `productions`).

## 6. Casos de aceptación (ejecutados sobre el bundle real del repo)

`studio/tests/test_atlas_aceptacion.py` (9 pruebas, todas verdes sobre `atlas/`):
≥2 lugares verificados por cada municipio prioritario (Medellín, Envigado, Sabaneta, Itagüí,
Bello), ubicación explícita vs selección previa de otro proyecto, prompt sin ubicación con
elección compatible con la tarea, asset alfa sobre fondo real con píxeles preservados,
cámara compatible e incompatible, archivo modificado (verify falla), reanudación estable
(mismo ref y hash), consulta/uso offline (`verify-bundle` ok, `prepare-shot` ok sin red),
escenarios con capacidades declaradas. La inspección visual de `atlas/examples/` confirma
fondos reales (p. ej. Avenida El Poblado con viaducto, CC BY 3.0) y el contact sheet sólo
incluye referencias enviadas.

Ejemplos por municipio (con vistas verificadas): Medellín: Aeropuerto Olaya Herrera, Avenida
El Poblado; Envigado: Avenida 33 Envigado, Calle 6 Sur; Sabaneta: Calle 75A Sur, Parque de
Sabaneta; Itagüí: Calle 20 Sur, Calle 20B Sur; Bello: Autopista Norte, Puente hacia Medellín.

## 7. Estado de CI y publicación (exacto, sin afirmaciones)

- Run de PR para el primer head `7eb4a587`: **verde** (run 41944587116).
- Push a ramas distintas de main/master: el workflow no dispara (`on.push.branches:
  [main, master]`) y GitHub registra un placeholder "failure" sin jobs nombrado por el
  archivo del workflow — evidencia: los 6 push de esta rama lo muestran y TODOS los push a
  `main` (ej. fb62023e) salen con nombre normal y éxito.
- No se crearon runs `pull_request` para los heads posteriores de esta rama (los push con
  este token no generan synchronize; otros PR como #62 sí reciben run por push). Por eso la
  validación del head final se hizo local con las mismas comprobaciones: ruff OK, pytest
  729 passed (11 fallos preexistentes del sandbox sin ffmpeg/torch, reproducibles en
  `origin/main` sin mis cambios), `templates`, `check_models_manifest --fixture --offline`,
  actionlint OK, `verify-bundle` OK, `serve-check`/`prepare-shot` OK sobre el bundle real.
- **No se afirma merge ni publicación**: el merge está bloqueado por permisos del token
  (`gh pr merge` → "Merging pull requests via CLI is not allowed"; `PUT /pulls/63/merge` →
  "Mutating GitHub API calls are not allowed") y la lectura de GHCR da 404 sin scope
  `packages:read`. Tras el merge, el push a `main` ejecuta `docker-publish.yml` (build +
  verify-bundle dentro de la imagen + promote a `latest` sólo en main) y el digest queda en
  los logs del run.
