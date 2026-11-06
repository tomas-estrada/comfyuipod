# W01 slice — Launch payload and manifest-backed acquisition

Status: integrated partial W01/A01 implementation. PR #122 is merged; follow-up
PRs #141 and #170 harden concurrent install cleanup and expected-size checks.
This slice does not claim all A01 requirements are complete. Current review base:
`main` at `7ae1d8bb47312d02436eb8b08cbeff2cded8dd9b` (83 commits past the audit); audit baseline:
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
PR CI is the Linux test authority. W26's manual-start finding is addressed in
the container contract (`ENTRYPOINT ["/usr/bin/tini", "--", "/start.sh"]`),
the Vast helper's `--args ''` payload, and image smoke coverage, but hosted Vast
startup remains unverified because no new instance was launched. PR #170 also
adds an expected-byte cross-check between the shell caller and manifest. A01's
full concurrent-process, cancellation/resumption, and recovery matrix remains
incomplete. No paid GPU or API validation was performed for this slice.

## Integrated state recheck (2026-11-06)

- PR #122 merged as `d9bcd1bc3c45cbb895eac51825cbca0513bd93bf`.
- PR #141 merged as `09fa8538fd313ca089e47018b10ef2e1ca02744c`; an old-mtime
  partial owned by a live PID now survives cleanup.
- PR #170 merged as `f0ff78794757f24f45f7fd634ce07fdc7b13440d`; a mismatched
  caller byte count is rejected before download.
- Current CPU checks: downloader `--selftest` passed; manifest fixture passed
  4/4; `py_compile` passed for probes, manifest and acquisition modules. Local
  `pytest` is unavailable locally. The prior PR head passed workflow run `42174104222`; this refreshed documentation commit requires its own CI result.
