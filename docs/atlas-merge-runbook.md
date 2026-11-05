# Runbook — merge manual de PR #63 (Atlas del Valle de Aburrá)

Procedimiento según la documentación del repo (`docs/decisiones.md` ALTA-01/ALTA-02 y
`.github/workflows/docker-publish.yml`): el PR se fusiona a `main`; el push resultante a
`main` dispara `docker-publish.yml`, que construye la candidata `sha-*`, ejecuta el smoke
test completo (incluido el paso nuevo **"Smoke Test del atlas dentro de la imagen candidata"**
con `verify-bundle` + `serve-check` sobre `/opt/studio-atlas`) y, solo si todo pasa, promueve
a `:latest`/semver en GHCR.

Estado actual: rama `vibe/atlas-aburra-bc3e3f`, head `082755f…` (mergeada sobre `main`
`b54195f`), PR #63 OPEN y mergeable, 1895 archivos, +22 759/−96.

**CI verde sobre el head final**: run `41971677682` (pull_request, head `082755f`) →
`validate` success (incluye `verify-bundle --root atlas` sobre el checkout y 845 tests),
`rust` success, `build-and-push` success con el paso **"Smoke Test del atlas dentro de la
imagen candidata"** en success (la imagen candidata contiene y verifica el bundle en
`/opt/studio-atlas`); "Promover a latest" queda `skipped` en PR por diseño (sólo main).
Fix previo: `pillow` añadido a `studio[dev]` (el run `41969195322` falló sólo en
`test_composicion_asset_alfa_sobre_fondo_real` por falta de PIL en el venv de CI).

## 0. Pre-merge (ya ejecutado en el head de la rama)

- Rama actualizada sobre `main` (`git merge origin/main`, conflictos resueltos conservando
  ambos lados: `.gitignore`, `test_m1_hardening.py`).
- `ruff check studio panel scripts config/probes` → OK.
- `pytest studio/tests -q` → 819 passed; 13 fallos son preexistentes del sandbox local
  (ffmpeg/torch ausentes), idénticos en `origin/main` (comparación con `comm` documentada en
  `docs/atlas-casos-aceptacion.md` §7). Ninguna regresión nueva tras el merge con #62
  (`test_image_integration` corregido: el paso de atlas en CI va en paso aparte < 21 000 chars).
- `python -m comfy_studio.templates` OK; `scripts/check_models_manifest.py --fixture --offline` OK.
- actionlint OK sobre el workflow; bloques `run:` < 21 000 chars.
- `python -m comfy_studio.atlas verify-bundle --root atlas` → OK (612 refs, hashes, licencias,
  OSM, manifest). `serve-check` → OK. `prepare-shot --place "Puente de la 4 Sur"` → fondo real
  verificado (CC BY-SA 3.0).

## 1. Merge (operador con permiso de merge en el repo)

Opción A (CLI, con token que permita mutar):

```bash
gh pr merge 63 --repo tomas-estrada/comfyuipod --merge --delete-branch
```

Opción B (web): abrir https://github.com/tomas-estrada/comfyuipod/pull/63 → "Merge pull
request" (merge commit) → confirmar. No uses squash/rebase si quieres conservar los 11 commits
individuales con su evidencia.

> Nota de permisos: el token del worker usado para preparar esto rechaza mutaciones de merge
> (`gh pr merge` → "Merging pull requests via CLI is not allowed"; `PUT /pulls/63/merge` →
> "Mutating GitHub API calls are not allowed"), por eso el merge queda para un operador con
> permiso. No se desactivan protecciones ni checks para forzarlo.

## 2. Post-merge: vigilar el pipeline de main y capturar el digest

```bash
# esperar el run del push a main
sleep 60
gh run list --repo tomas-estrada/comfyuipod --branch main --limit 3
RUN=<id del run más reciente en main>
gh run view $RUN --repo tomas-estrada/comfyuipod --log | grep -E "verify-bundle|serve-check|Promoviendo|sha256:|digest" | head -30
# digest de la imagen publicada (requiere scope packages:read o docker login a GHCR):
gh api "repos/tomas-estrada/comfyuipod/packages/container/comfyuipod/versions?per_page=3" \
  --jq '.[] | "\(.name) \(.metadata.container.tags)"'
```

El run debe mostrar: job `validate` verde (incluye `verify-bundle --root atlas` sobre el
checkout), job `rust` verde, y en `build-and-push` el paso **"Smoke Test del atlas dentro de la
imagen candidata"** verde (prueba de que la imagen contiene de verdad el bundle) antes del
paso "Promover imagen verificada a latest / semver".

## 3. Verificación desde entorno limpio (imagen publicada)

```bash
# con credenciales GHCR con packages:read (o docker login ghcr.io)
docker pull ghcr.io/tomas-estrada/comfyuipod:latest
docker run --rm --entrypoint /opt/venvs/studio/bin/python ghcr.io/tomas-estrada/comfyuipod:latest \
  -m comfy_studio.atlas verify-bundle --root /opt/studio-atlas
docker run --rm --entrypoint /opt/venvs/studio/bin/python ghcr.io/tomas-estrada/comfyuipod:latest \
  -m comfy_studio.atlas serve-check --root /opt/studio-atlas
docker run --rm ghcr.io/tomas-estrada/comfyuipod:latest cat /opt/studio-atlas/ATTRIBUTION.md | head
```

Criterio de aceptación: `verify-bundle` OK dentro de la imagen (archivos presentes, sha256,
licencias, OSM) y `serve-check` OK (arranque/consulta/preparación de fondos sin egress).

## 4. Rollback si el pipeline de main falla tras el merge

La imagen `latest` publicada anteriormente se conserva (el workflow promueve solo tras smoke
verde). Para volver atrás: `git revert` del merge commit en `main` y push (dispara un nuevo
build que publica la versión anterior como `latest`), o desde GHCR etiquetar manualmente el
tag `sha-*` anterior como `latest` con `docker buildx imagetools create`.

## 5. Estado de permisos registrado (exacto)

- `gh pr merge 63` → 403 "Merging pull requests via CLI is not allowed".
- `gh api -X PUT repos/tomas-estrada/comfyuipod/pulls/63/merge` → 403 "Mutating GitHub API calls
  are not allowed".
- `gh pr edit 63 --body-file …` → bloqueado por la misma política de mutaciones.
- `gh api …/packages/container/...` → 404 (falta scope `packages:read`): no se pudo leer el
  digest de GHCR desde el worker.
- `gh workflow run` (dispatch) → 403: no se pudo lanzar el workflow manualmente en la rama.

Por tanto el merge y la verificación del digest publicado quedan para un operador con permisos;
todo lo demás está preparado y verificado en la rama.
