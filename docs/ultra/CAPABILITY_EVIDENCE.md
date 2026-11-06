# Capability evidence snapshot

Snapshot: 2026-11-06 10:42 UTC. Repository state: `main` at `93b2af2cd5a480ef785ce04987cd79ed01fe0472`, 102 commits ahead of audit commit `2c0fbd37954f3f8f03f64c43b8882ae1079b4b29` (verified by comparing the GitHub refs).

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
| C07 | Lugares reales del Aburrá | L1 partial | W09 offline atlas data and CPU tests exist; merged PR #134 adds a regression that checks camera constraints against the explicitly requested view. Full stratified atlas acceptance remains unproven. |
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
| C32 | Entregables reproducibles | L1 partial | Main has production-output gallery registration code and CPU unit coverage. Merged PR #129 adds project creation → normal Studio production endpoints → FFmpeg clip/reel → gallery/download. This CPU flow is L1 evidence and does not establish L2 engine integration or full 4K/editable-delivery acceptance. |
| C33 | Investigador Pareto de recetas | L0 | W23/W24 acceptance is not complete. |
| C34 | Aprendizaje de adaptadores | L0 | W23 acceptance is not complete. |

## Non-capability evidence

- [`W26_GPU_RESULTS.md`](W26_GPU_RESULTS.md) records one measured SDXL image-generation run at L3 for that specific demo. It does not meet the acceptance criteria of a full C01–C34 capability, D01–D05, or L4.
- Merged orchestration changes: PR #123 (`369b012`), #124 (`a805d85`), #127 (`01cfde0`), and #129 (`5228263`). PR #129 head `2093cc8b2fcf574b5c4900fc1702a1dd0e8bfa2f` passed workflow run [42165540142](https://github.com/tomas-estrada/comfyuipod/actions/runs/42165540142): Rust checks, repository validation, Studio Ruff/pytest, candidate image build, full-stack/Atlas/Studio-profile smoke checks. This establishes CPU/integration coverage only; see the capability limits above.
- PR #122 (`d9bcd1b`) routes legacy model downloads through the manifest-backed acquirer and corrects Vast's empty `--args` launch payload. PR #170 (`f0ff787`) also checks the shell caller's expected byte count against the manifest, fail-closed. PR #141 (`09fa853`) preserves live cross-device install temporaries regardless of mtime. These are A01 launch/acquisition improvements, not proof of the full A01 matrix or an actual paid Vast launch.
- Current CPU checks on the audited source: `PYTHONPATH=studio bash scripts/download-models.sh --selftest` passed (KiB-to-byte conversion, existing-parent resolution, manifest destination and fixed revision); `PYTHONPATH=studio python3 scripts/check_models_manifest.py --fixture --offline` passed 4/4; `py_compile` passed for probes, manifest and acquisition modules. Local pytest is unavailable; the active GitHub workflow is the pytest authority.
- The Dockerfile entrypoint is `/usr/bin/tini -- /start.sh`; `start.sh` proceeds to `comfyctl supervise`. The launch payload and container startup chain have CPU regression coverage and image smoke coverage. The historical W26 finding records a manual start in that campaign; no new Vast instance was launched to verify hosted behavior in this run.
- PR #133 (`fcf14d1`) aligns the baked Remotion packages with engine lock version `4.0.541`; the merged manifest declares both `remotion` and `@remotion/cli` at that version.
- PR #134 (`00ae327`) validates Atlas camera constraints against the exact requested view. PR #136 (`5850191`) introduced this evidence ledger.
- Main snapshot `93b2af2cd5a480ef785ce04987cd79ed01fe0472` is 102 commits ahead of the audit. No current-main OCI image digest has been established in this review; historical candidate image/cache digests are not a published-main digest.
- PR #208 (watchdog recovery) is open at `a447ab5df2041622e3a3dcfb1350bd021f2829ef`; workflow run [42194196499](https://github.com/tomas-estrada/comfyuipod/actions/runs/42194196499) passed. It fixes the independent Studio restart budget and avoids counting one exited child repeatedly.
- PR #207 (fail-closed GPU lease recovery) is open at `7b9427dcd76f6946a5ec0a819a022a656b8610db`. Workflow run [42203666127](https://github.com/tomas-estrada/comfyuipod/actions/runs/42203666127) has a Rust test failure in `supervisor_respawns_studio_server_independently_while_comfyui_healthy_and_exhausted`, which PR #208 addresses; the repository validation job was still running at snapshot time. The focused change has local syntax, diff, and process-group/lease smoke evidence, but requires a green CI run on the rebased current head before merge.
- PR #214 (ad audio filter graph) is open at `2a959574fb9f0dcc1483c291c47b0540bae3962a`; its Python Ruff/pytest checks passed in run [42202045867](https://github.com/tomas-estrada/comfyuipod/actions/runs/42202045867), while the same watchdog Rust test failed and the candidate image was skipped. It is not current-main evidence.
- PR #177 head `43774596e8e1cde9338098848388b0df40f64913` passed prior workflow run [42179215943](https://github.com/tomas-estrada/comfyuipod/actions/runs/42179215943), including candidate image build, full-stack, Atlas, and Studio-profile smoke tests. That result predates current main and does not establish the main-image digest.
- Historical PR candidate digests above are smoke-test artifacts and are not the published main image digest.
- PR #123 closes the concurrent approval/idempotency slice but explicitly leaves recovery of an orphaned `executing` checkpoint after process restart as an A05 durability gap.
- No paid GPU or API validation was performed for this snapshot. GPU performance/quality claims remain pending separate authorization.
