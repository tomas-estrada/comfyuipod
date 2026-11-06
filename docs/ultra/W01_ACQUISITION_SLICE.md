# W01 slice — Launch payload and manifest-backed acquisition

Status: partial W01 implementation candidate, pending PR/CI. This slice does
not claim all A01 requirements are complete. Base reviewed: `main` at
`8393fe1ae1d97942db54250a92afc632e07f4d1a`; audit baseline:
`2c0fbd37954f3f8f03f64c43b8882ae1079b4b29`.

## Changes in this slice

- Convert `df -Pk` available blocks from KiB to bytes while retaining the walk
  to the nearest existing parent for a not-yet-created destination.
- Route legacy `hf_fetch` calls through `comfy_studio.asset_acquire`, selecting
  an exact repo/path/type/destination match from the core or Studio manifest.
  Missing revisions, digests, or conflicting declarations fail closed.
- Use the existing acquirer for verified cache reuse, digest/size validation,
  pinned `--revision`, staging, and atomic publication. Buffalo ZIP acquisition
  now uses its canonical manifest destination before extraction.
- Pass the explicit empty Vast CLI payload `--args ''`; the image ENTRYPOINT
  remains `/usr/bin/tini -- /start.sh`, which proceeds to `comfyctl supervise`.

## Evidence collected

- `bash -n scripts/download-models.sh tools/mac/comfy` — passed.
- `python3 -m py_compile studio/comfy_studio/asset_acquire_cli.py
  studio/tests/test_asset_acquire_cli.py` — passed.
- `git diff --check` — passed.
- `PYTHONPATH=studio bash scripts/download-models.sh --selftest` — passed;
  covers KiB conversion, existing-parent resolution, manifest destination
  handling, a fake download, and forwarding the fixed revision.
- CPU fixture run of legacy phase 1/2 — 33 fixture acquisition calls, no
  failures; verified cache reuse on phase 2.
- CPU fixture run of Studio phase 3 — 79 assets published, zero failures.
- Added mocked Vast launch and Dockerfile/startup regression tests. They compile,
  but could not be executed locally because `pytest` is not installed.

## Evidence limits and next gates

This is code/CPU evidence (L1 for the changed paths), not an actual Vast
container launch or GPU campaign. The local environment lacks `pytest`, `ruff`,
Cargo, and nginx, so the repository-wide AGENTS checks have not run locally.
CI must run them on the PR. W26's manual-start finding remains unclosed at the
runtime level until a future authorized launch confirms the Vast payload boots
without an interactive `/start.sh` command. No paid GPU or API validation was
performed for this slice.
