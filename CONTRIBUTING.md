# Contributing to ComfyUIPod

Thanks for considering a contribution. ComfyUIPod packages ComfyUI and Comfy Studio in a single Linux GPU container, with Vast.ai as its current deployment target. The browser UI works from Windows, macOS, or Linux; the optional `comfy` shell helper is for macOS/Linux (or WSL2).

## Start with the current project state

- Read the [project overview](README.md), [architecture and resource policy](docs/POLITICA_ARQUITECTURA_RECURSOS.md), and [agent/developer conventions](AGENTS.md).
- The default hardware profile in `studio/comfy_studio/hardware_profile.py` is one RTX PRO 6000 Blackwell with 96 GB VRAM and 128 GB host RAM. A 256 GB host-RAM profile is also defined; the 32 GB consumer profile is retained for compatibility. These are selection and sizing profiles, not proof that every workflow runs on every matching offer.
- The [W26 GPU report](docs/ultra/W26_GPU_RESULTS.md) records one measured SDXL image-generation run on a candidate image built from `f5c29dc`. This checkout is newer (`5850191`): the run is useful evidence for that recipe, not validation of the current image or of all video, speech, and 3D workflows.
- Startup currently downloads only assets classified as `bootstrap` (the checked-in manifests contain none). Task and optional models are acquired when needed. Use `scripts/storage_requirement.py` for derived disk sizing rather than copying a number from an older report.

## Make a reviewable change

Please keep changes focused, include or update tests for behavior changes, and update the relevant documentation when a contract or user workflow changes. In a pull request, explain the problem, intended behavior, important trade-offs, and checks you ran. For model or engine changes, include the pinned revision, integrity evidence, license constraints, and the hardware/recipe evidence you actually have; do not present imports, stubs, or estimates as measured inference.

Do not commit credentials, model weights, private media, generated renders, or machine-specific runtime data. Check model and engine licenses before adding them to the image or recommending them for commercial work.

## Run the repository checks

Follow the complete commands in [`AGENTS.md`](AGENTS.md). The core Python checks are:

```bash
ruff check studio panel scripts config/probes
pytest studio/tests -q
python -m py_compile config/probes/*.py scripts/check_models_manifest.py
python -m comfy_studio.templates
python scripts/check_models_manifest.py --fixture --offline
python scripts/prune_model_cache.py --json
```

For `comfyctl`, run `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test --locked` from `comfyctl/`. For Nginx configuration changes, run `nginx -t -c config/nginx.conf`. Also check relative links in Markdown you change and review the resulting diff before submitting.

## Language

Most existing user-facing documentation is in Spanish. English documentation and contributions are welcome; when editing an established Spanish guide, keep its language consistent unless the change is a new standalone document.
