# Capability evidence snapshot

Snapshot: 2026-11-06 UTC. Repository state: `main` at `3b0afc072dd8854e697e51075dceba7b1a030023`, 26 commits ahead of audit commit `2c0fbd37954f3f8f03f64c43b8882ae1079b4b29`.

This records the highest evidence level actually demonstrated for each capability, not whether its full acceptance criteria are complete. Levels follow [`upgrades-ultra-version/acceptance.md`](../../upgrades-ultra-version/acceptance.md): L0 source/specification, L1 code/contracts/tests, L2 prepared engine and connected adapter with CPU integration, L3 target-GPU execution, L4 held-out reproduction and baselines. Partial evidence is identified explicitly. A green test with a fake collaborator does not establish L2.

## C01–C34

| ID | Capability | Main evidence level | Evidence and limit |
|---|---|---:|---|
| C01 | Archivo audiovisual semántico | L1 partial | W05 code and CPU regression evidence are summarized in [`CLOSING.md`](CLOSING.md); the 100-file mixed-media acceptance and real extraction remain unproven. |
| C02 | Referencia a receta editable | L0 | W06/W20 acceptance is not complete; specification only. |
| C03 | Dirección desde intención incompleta | L0 | W22 acceptance is not complete; specification only. |
| C04 | Memoria de lenguaje audiovisual | L1 partial | W04 persistence/lineage contracts are summarized in [`CLOSING.md`](CLOSING.md); director-level memory behavior remains unproven. |
| C05 | Cámara cinematográfica dirigida | L0 | W11 animatic and camera-path acceptance is not complete. |
| C06 | Mundos con memoria espacial | L0 | W17 acceptance is not complete. |
| C07 | Lugares reales del Aburrá | L1 partial | W09 offline atlas data and CPU tests exist; PR #134 adds a regression that checks camera constraints against the explicitly requested view. Current-main image inclusion/digest and the full stratified atlas acceptance are not established here. |
| C08 | Animatic y ensayo virtual | L0 | W11/W22 acceptance is not complete. |
| C09 | Producto 3D reutilizable | L0 | W10 acceptance is not complete. |
| C10 | Despiece y ensamblaje | L0 | W10/W12 acceptance is not complete. |
| C11 | Rigging y deformación | L0 | W13 acceptance is not complete. |
| C12 | Movimiento recuperado | L0 | W13 acceptance is not complete. |
| C13 | Coreografía generada | L0 | W13 acceptance is not complete. |
| C14 | Transferencia de actuación | L0 | W13 acceptance is not complete. |
| C15 | Trayectorias de objetos | L0 | W12/W13 acceptance is not complete. |
| C16 | Actores hablantes | L0 | W14/W19 acceptance is not complete. |
| C17 | Video a escena 4D | L0 | W17 acceptance is not complete. |
| C18 | Acciones y colisiones | L0 | W12 acceptance is not complete. |
| C19 | Efectos de materiales | L0 | W12/W20 acceptance is not complete. |
| C20 | Optimización de puesta en escena | L0 | W11/W22 acceptance is not complete. |
| C21 | Reiluminación temporal | L0 | W16 acceptance is not complete. |
| C22 | Restilizar una actuación | L0 | W16 acceptance is not complete. |
| C23 | Generación audiovisual multirreferencia | L1 partial | W08 recipe/catalog code and CPU contracts exist; the capability’s multireference output acceptance is not demonstrated. |
| C24 | Reparación localizada | L0 | W15/W20 acceptance is not complete. |
| C25 | Diseño de sonido por escena | L0 | W18 acceptance is not complete. |
| C26 | Música original dirigida | L0 | W19 acceptance is not complete. |
| C27 | Voz y pronunciación en español | L0 | W19 acceptance is not complete. |
| C28 | Keyframes y RGBA | L0 | W08/W15 acceptance is not complete. |
| C29 | Matting de video | L0 | W15 acceptance is not complete. |
| C30 | Restauración y acabado temporal | L0 | W08/W20 acceptance is not complete. |
| C31 | Montaje y efectos programables | L0 | W20 acceptance is not complete. |
| C32 | Entregables reproducibles | L1 partial on `main`; stronger L1 flow in open PR #129 | Main has production-output gallery registration code and CPU unit coverage. PR #129 adds project creation → normal Studio production endpoints → FFmpeg clip/reel → gallery/download. The flow uses test collaborators and is not an L2 engine integration or full 4K/editable-delivery acceptance. |
| C33 | Investigador Pareto de recetas | L0 | W23/W24 acceptance is not complete. |
| C34 | Aprendizaje de adaptadores | L0 | W23 acceptance is not complete. |

## Non-capability evidence

- [`W26_GPU_RESULTS.md`](W26_GPU_RESULTS.md) records one measured SDXL image-generation run at L3 for that specific demo. It does not meet the acceptance criteria of a full C01–C34 capability, D01–D05, or L4.
- PR #129 head `d037b866b06bbc6837e0b53814e271ae13d1e52a` passed current-base CI: 1,628 tests, Ruff, Rust checks, and image full-stack/atlas/Studio-profile smoke tests. Its CPU project-to-artifact test supports the L1 entry above. PR #129 remains open, so this is not main evidence.
- The PR candidate image built from merge ref `20e85fe1be14cb95744a5a158dca7cbd38508c0c` had OCI image digest `sha256:01f049a5e4f0ae774e09bc95ac46c35b6a7232241183e14c4b94901e79ba1981`. It was a PR smoke candidate, not the published main image or its final digest.
- No paid GPU or API validation was performed for this snapshot. GPU performance/quality claims remain pending separate authorization.
