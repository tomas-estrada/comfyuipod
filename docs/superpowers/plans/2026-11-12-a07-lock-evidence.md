# A07 Lock Evidence Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make A07 lock hashes prove the bytes of a tracked repository lock file instead of accepting a syntactically valid but unverifiable digest.

**Architecture:** The engine manifest checker will resolve every declared lock path beneath the repository root, reject unsafe or missing files, and compare the file's SHA-256 with its declared digest. Existing external binary pins will move into a tracked lock record that contains the immutable artifact URL and digest; the checker validates the lock record bytes while A07's separate image/runtime evidence remains explicitly out of scope.

**Tech Stack:** Python 3.12, pytest, JSON manifest.

**Spec:** `upgrades-ultra-version/acceptance.md` A07 and `upgrades-ultra-version/engines.json`.

## Global Constraints

- Never mark an engine implemented without a real adapter/entrypoint.
- Never treat a syntactically valid but missing/mismatched lock as accepted evidence.
- A lock file only counts as repository evidence when Git tracks its path.
- Keep manifest coherence errors distinct from expected A07 release blockers.
- Keep image inventory, offline boot, and runtime probe evidence separate from manifest checks.

## Review Focus

- A nonexistent lock path must remain a release blocker.
- Absolute paths and symlinks escaping the repository must not be read as lock evidence.
- Untracked lock files must not count as release pins.
- A tracked symlink is insufficient when the bytes it points to are untracked.
- A changed lock file must invalidate its digest.
- Existing valid lock files must continue to resolve and verify.

### Task 1: Verify repository lock-file evidence

**Files:**
- Modify: `scripts/check_engines_manifest.py`
- Modify: `studio/tests/test_engines_manifest.py`
- Create: `upgrades-ultra-version/locks/blender-linux-x64-5.2.4.lock.json`
- Modify: `upgrades-ultra-version/engines.json`

**Interfaces:** `validate(manifest, cap_ids, repo_root=None, require_git_tracked=None)` returns the existing `(problems, blockers)` tuple; unverifiable lock evidence is appended to `blockers`.

- [x] Add failing tests for matching, missing, mismatched, escaping, invalid, unreadable, and untracked lock paths.
- [x] Run the focused tests and confirm they fail on absent verification behavior.
- [x] Implement path containment and SHA-256 verification.
- [x] Replace Blender's non-file dependency lock key with a tracked lock record containing its release artifact URL and digest.
- [x] Run focused tests and the repository manifest check; leave unrelated release blockers visible.
