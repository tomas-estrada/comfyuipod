# Atlas audiovisual del Valle de Aburrá (ComfyUIPod)

Estado: integrado en main vía PR #63. La imagen publicada en GHCR contiene el
bundle real en `/opt/studio-atlas` (verificado en build y en smoke test).

## Qué es

Biblioteca audiovisual georreferenciada del Valle de Aburrá que vive DENTRO de la
imagen del estudio. El pod arranca con lugares, archivos y herramientas
disponibles localmente: ni el arranque ni el uso normal consultan Wikimedia,
Mapillary, GeoMedellín ni terceros.

- Código: `studio/comfy_studio/atlas/` (modelos, store SQLite RTree+FTS5, servicio,
  adquisición, empaquetado, CLI, routers, herramientas del agente).
- Datos: `atlas/` en el repo → `COPY atlas/ /opt/studio-atlas/` en el Dockerfile
  (capa tardía; `ENV STUDIO_ATLAS_DIR=/opt/studio-atlas`). `.dockerignore` incluye
  licencias y avisos (`!atlas/**`).
- Verificación: `python -m comfy_studio.atlas verify-bundle --root atlas` en CI
  (checkout) y dentro de la imagen candidata (`/opt/studio-atlas`) antes de
  promover; un archivo ausente/corrupto/sin licencia bloquea la promoción.

## Inventario medido (bundle publicado)

Verificar en vivo: `python -m comfy_studio.atlas stats --root atlas`.

- 612 referencias `fondo_urbano` enviadas (de 1203 adquiridas y clasificadas por
  `content_class`; el resto queda registrado con su clase y usable=0).
- 152 lugares con vistas fotográficas utilizables; el catálogo registra 10 209
  entidades geográficas (OSM) además de los lugares curados.
- 168 escenarios: 152 `photo_bg` verificados + 16 candidatos `multiview` (draft,
  reconstrucción GPU pendiente).
- Municipios con cobertura fotográfica: Medellín, Envigado, Sabaneta, Itagüí,
  Bello, Copacabana, Caldas, Girardota, La Estrella.
- Licencias: CC BY / CC BY-SA (1.0–4.0), CC0, dominio público. Atribuciones en
  `atlas/ATTRIBUTION.md` y `atlas/licenses/<ref_id>.txt`.
- Base geográfica: extracto OSM Geofabrik `colombia-261103` (md5 verificado,
  sha256 registrado) recortado al Aburrá (`atlas/osm/aburra-valley.osm.pbf`, ODbL).

## Uso (agente / API / panel / CLI)

- Agente (tool-belt y MCP): `atlas_search_places`, `atlas_resolve_place`,
  `atlas_place_detail`, `atlas_select_view`, `atlas_prepare_shot`, `atlas_stats`,
  `atlas_verify`. La ubicación explícita siempre se respeta; la ambigüedad se
  devuelve como candidatos, nunca se sustituye en silencio.
- API: `GET /studio/api/atlas/{health,stats,places,resolve,search,selection,verify,map.svg}`,
  `GET /studio/api/atlas/places/{id}[/{coverage,views}]`,
  `POST /studio/api/atlas/select-view`,
  `GET /studio/api/atlas/references/{ref_id}[/{file,preview,license}]`.
  Los archivos se sirven sólo desde rutas resueltas dentro del atlas.
- Planner→ejecutor: `plan_production` pasa `context["atlas"]` al planner y lo
  guarda en `state.metadata["atlas"]` (serializable con el checkpoint);
  `EngineRunnerAdapter` resuelve `job["atlas"]` a `background_image` real
  (archivo local verificado) o falla tipado. Sin placeholder.
- Panel: pestaña "Atlas Aburrá" con mapa SVG local (OSM propio), filtros por
  municipio/entorno/cobertura, previews y selección de vista activa.
- CLI (macOS/Windows/Linux): `python -m comfy_studio.atlas {stats,search,resolve,place,select-view,prepare-shot,verify,verify-bundle,manifest,map,acquire,package,serve-check} --root DIR`.

## Ampliación / actualización / rollback

```
# adquirir más material (reanudable; respeta 429/403 con circuit breaker)
python -m comfy_studio.atlas acquire --root /ruta/atlas --source all --max-refs 900
# reasignar lugares, refinar semillas, deduplicar perceptual
# (métodos del pipeline: reassign_places / refine_seed_coordinates / _dedupe_phash)
# reempaquetar (cura ≤620 refs, ≤2048 px, reescribe catálogo y manifest)
python -m comfy_studio.atlas package --root /ruta/atlas --dest atlas --max-shipped 620
python -m comfy_studio.atlas verify-bundle --root atlas
```

- Versionado: los ids de lugar son estables; referencias/escenarios se versionan.
  La fecha de incorporación no es la del estado físico observado.
- Rollback: el bundle anterior queda en el historial de git y en la imagen
  publicada anterior (GHCR conserva tags por SHA); `git revert` del commit del
  bundle + rebuild recupera la versión previa. Nunca se promueve `latest` desde
  un build incompleto.
- Congelación por producción: `state.metadata["atlas"]` fija lugar/vista/ref por
  producción; una actualización no cambia fondos entre planos en curso.

## Déficits y bloqueos reales

- GeoMedellín (edificios con altura): `www.medellin.gov.co` no respondió desde el
  entorno de adquisición (circuit breaker abierto) y su licencia de redistribución
  no quedó verificable por metadata → NO se publica esa capa. Base geográfica: OSM.
- Mapillary: sin token legítimo en el entorno → no se usó.
- Orientación de cámara (heading) desconocida en las fotos: se devuelve como
  limitación tipada (`heading_unknown`), nunca como 0.
- Validación en GPU (splatfacto/COLMAP, 4K final): fase posterior coordinada por
  otro operador; aquí quedan los datasets, comandos y contratos listos (capture3d
  corregido a los verbos reales de Nerfstudio 1.1.5).
- Gasto externo nuevo: cero (sólo recursos gratuitos usados).
