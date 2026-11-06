# Capability evidence snapshot

Snapshot: 2026-11-06 16:39 UTC. Repository state: `main` at `c678d9ddf228052cad063a0dcb1ff1163cd5a0f2`, 126 commits ahead of audit commit `2c0fbd37954f3f8f03f64c43b8882ae1079b4b29` (GitHub compare verified). Main workflow [42251150725](https://github.com/tomas-estrada/comfyuipod/actions/runs/42251150725) attempt 3 passed validation, Rust checks, candidate image build, full-stack smoke, Atlas smoke, Studio-profile smoke, and image promotion.

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

- [`W26_GPU_RESULTS.md`](W26_GPU_RESULTS.md) records one measured SDXL image-generation run at L3 for that specific earlier image digest. It does not meet the acceptance criteria of any full C01–C34 capability, D01–D05, or L4. This stabilization pass used no GPU or paid API.
- Merged orchestration changes include PRs #123, #124, #127, #129, and #207. PR #207 squash merge is `8e3162b9d4a243a34ea4812a09996a5fe9581c6b`. Its current head `0efa9a6a77c66bb744707b96ddc063c3013126c6` passed run [42220916631](https://github.com/tomas-estrada/comfyuipod/actions/runs/42220916631): repository validation (Ruff and 2,057 Python tests), Rust format/Clippy/tests, candidate build, full-stack, Atlas, and Studio-profile smoke. The candidate passed; this does not supply a digest for the published `main` image.
- PR #122 (`d9bcd1b`) routes legacy model downloads through the same manifest-backed Python acquirer used by other paths and corrects Vast's empty `--args` launch payload. PR #170 (`f0ff787`) compares shell expected byte counts with the manifest, fail-closed. PR #141 (`09fa853`) preserves live cross-device install temporaries regardless of mtime. The shared acquisition CLI requires a manifest entry at the exact destination, a 40-character pinned revision, and SHA-256 or SHA-1; it passes that manifest need to the shared acquirer, which stages and atomically publishes. This supports the download path without proving a real paid Vast launch.
- The current shell self-test covers KiB-to-byte conversion, resolution to an existing parent for not-yet-created paths, manifest destination, and fixed revision. The offline manifest fixture and Python/Rust test suites run in repository CI. Local pytest is unavailable in this workspace, so the workflow is the broad Python-test evidence.
- The Vast helper derives storage requirements from `scripts/storage_requirement.py`. The supported launch payload uses empty `--args ''` so the Docker image ENTRYPOINT is preserved. Docker ENTRYPOINT is `/usr/bin/tini -- /start.sh`, which proceeds to `comfyctl supervise`. CPU regressions and candidate smoke checks cover this chain; no new Vast instance was launched in this pass.
- PR #133 (`fcf14d1`) aligns Remotion engine lock and direct baked packages at `4.0.541`. The package manifest pins `remotion` and `@remotion/cli` to that exact version. The repo's Docker build does not use a committed npm lockfile, so transitive Node dependency resolution remains less reproducible than the Python/Rust locks.
- PR #134 (`00ae327`) validates camera constraints against the exact requested Atlas view. PR #136 (`5850191`) introduced this ledger.
- Audit comparison: current snapshot `c678d9ddf228052cad063a0dcb1ff1163cd5a0f2` is 126 commits ahead of audit `2c0fbd37954f3f8f03f64c43b8882ae1079b4b29`. The snapshot includes merged PR #207 GPU lease cleanup/watchdog recovery, PR #177 evidence-ledger refresh, and PR #215 real orchestration defaults.
- Current-main workflow [42251150725](https://github.com/tomas-estrada/comfyuipod/actions/runs/42251150725) attempt 3 passed on merge commit `c678d9ddf228052cad063a0dcb1ff1163cd5a0f2`. Candidate tag `ghcr.io/tomas-estrada/comfyuipod:sha-c678d9d` has OCI manifest digest `sha256:177b59c4525504a7ff843ac13d7d7a83614c91678f06fe546a652aa7f1f49f78` and config digest `sha256:4ce5051e385d6b731cff66af66a48e603588f119ff225f8644079a6b22225b58`. The workflow promoted this verified image to `latest`; the promoted manifest digest is `sha256:1b78b36d389d1055155f28991a9e6223b557107ca9855993376ee5fb14959ac3`. The candidate passed full-stack, Atlas, and Studio-profile HTTP/TLS smoke checks.
- PR #123 closes concurrent approval/idempotency and cancellation slices but explicitly leaves recovery of an orphaned `executing` checkpoint after process restart as an A05 durability gap.
- PR #129 demonstrates project creation → normal Studio production endpoints → FFmpeg clip/reel → gallery/download with a CPU fixture. This is L1 evidence, not L2 real-engine integration, GPU execution, or full 4K/editable-delivery acceptance.
- PR #207 adds process-group cleanup and fail-closed GPU lease recovery. An orphaned owner does not release the lease until engine-specific cleanup is verified; cancellation/timeout cleanup is covered by CPU/Rust regressions. Target-GPU VRAM/cleanup behavior remains unmeasured in this pass.
- PR #215 (`21960cd2ed1244170e28586be7a33366019de179`) is merged. For `MODEL_PROFILE=studio` and `unreal`, live adapters, recipe binding, and resource admission are now the default; simulation is explicit for CI. Startup fails closed if hardware verification or adapter construction fails. Its candidate image job was canceled by later main updates; current main workflow [42251150725](https://github.com/tomas-estrada/comfyuipod/actions/runs/42251150725) attempt 3 passes the verified image build and smoke suite.
- PR #177 (`3e971fef6b38875360bc41e3632567fc652b4725`) is merged. Its refreshed tests passed; current-main image evidence is provided by run [42251150725](https://github.com/tomas-estrada/comfyuipod/actions/runs/42251150725), attempt 3.
- Capability levels remain conservative: C07 has L1 evidence from offline Atlas data and the exact-view regression; C32 has L1 CPU project-to-artifact evidence. The other table entries retain their previously documented levels. The single W26 SDXL demo is L3 only for its specific prior digest.
- PR #223 (`34fc05985f17c08752dbfeed3eb61009deff1ad6`) updates the three voice/music filter-graph assertions to match #214. PR #225 (`c678d9ddf228052cad063a0dcb1ff1163cd5a0f2`) then refreshed the test docstring and verified 21 targeted cases; current-main run [42251150725](https://github.com/tomas-estrada/comfyuipod/actions/runs/42251150725), attempt 3, passed the complete image smoke and promotion gates. No GPU or paid API was used for this stabilization pass.
- Remaining acceptance gaps include D01–D05, L4 held-out/baseline validation, real-GPU execution/quality measurements, all other unproven capabilities, orphaned-execution restart recovery, and full 4K/editable-delivery acceptance. Paid GPU/API validation remains outside this run.
