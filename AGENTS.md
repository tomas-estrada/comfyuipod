# ComfyUIPod Agent Conventions

## Project Structure- `comfyctl/` — Rust supervisor, status server, strict checks (S1..S17 + S13b/S13c), triage, redaction
- `studio/comfy_studio/` — Python FastAPI service (`comfy_studio`), package layout:
  - `app.py` / `server.py` / `webutil.py` — app assembly, single-worker CLI entry, shared HTTP/upload/auth helpers
  - `routers/` — 14 route modules under `/studio/api/*` (core, projects, assets, jobs, templates, chat, perception, media_ops, brief, ads, review_pins, montage, productions)
  - `agent/` — LLM agent loop, OpenAI-compatible client, 54-tool belt, roles, budgets, memory
  - `templates/` — 17 curated workflow templates + structural/semantic validator
  - `engines/` — 16 engines behind a uniform list/install/verify/run contract, install gate
  - `orchestration/` — production graph, planner, executor, evaluator, SQLite checkpointer
  - `creative/` — project understanding, creative brief, review, delivery
  - `ads/` — `ad_studio` engine (script → storyboard → variants + QA)
  - `montage/` — assisted, verifiable montage from a brief
  - `edl/` — EDL document, deterministic ffmpeg executor, highlights, windowed assembly
  - `perception/` — CLIP/InsightFace indexes over sqlite-vec, keyframes
  - `asr/` — WhisperX transcription + FunASR diarization
  - `ml/` — subprocess worker protocol, backend probe, real/sim backends
  - `agent_brain/` — memory/compress/retrieve/rerank capabilities (health-reported)
  - `stubs/` — deterministic ComfyUI and LLM doubles for tests/CI
- `panel/` — vanilla JS SPA (projects, chat, jobs, gallery, EDL, ads), no build, no CDN
- `config/` — nodes.lock, constraints.txt, nginx.conf, extra_model_paths.yaml, model manifests, probes, portal
- `scripts/` — start.sh, download-models.sh, check_models_manifest.py, triage_patterns.json
- `tools/mac/comfy` — macOS/Linux Vast.ai CLI helper
- `docs/` — architecture, guides, decisions, contracts (frozen JSON schemas), proposals
- `docs/auditorias/` — point-in-time audit records (do not treat as current state)

- `studio/comfy_studio/model_assets.py` — activos de modelo como dependencias de tarea
- `scripts/storage_requirement.py` — requisito de almacenamiento derivado
- `scripts/check_models_manifest.py` — validador del manifiesto (clases, bytes, integridad)
- `scripts/pin_manifest_integrity.py` — fija `revision` + hash en los manifiestos
- `scripts/prune_model_cache.py` — política de retención de caché (dry-run por defecto)
- `render_recipes` (tool del agente) — recetas medidas: VRAM/RAM/tiempo reales por plantilla

## Architecture & Resource Policy
[`docs/POLITICA_ARQUITECTURA_RECURSOS.md`](docs/POLITICA_ARQUITECTURA_RECURSOS.md) is the source of truth for what is prepared at build time, what is downloaded on demand, what may run, and what hardware to rent. Key rules that are enforced in code:
- Storage is derived (`scripts/storage_requirement.py`), never a hardcoded guess.
- Engines are baked into the image; the agent never installs them (`install_engine` only decides; `STUDIO_DYNAMIC` off by default).
- "Installed" is not "running": health distinguishes installed / integrated / weights-missing / on-disk / hardware / loaded / execution-validated (`engines/preparedness.py`).
- Model assets are classified `bootstrap` / `task` / `optional` in `config/models-studio.json`; a large collection is never bootstrap just because an engine supports it.
- Weight availability is a filesystem fact (`*_weights_available()`), not an env flag.

## Validation Commands
Always run these before submitting changes:

> **Diagramas: renderizar antes de commitear.** Un `.puml` cuya sintaxis es
> valida puede renderizar vacio o con el texto solapado, y nadie lo nota hasta
> que una persona lo abre. Regla obligatoria en
> [`docs/architecture/README.md`](docs/architecture/README.md); la CI la hace
> cumplir en el job `Validate`.

1. **Python**:
```
ruff check studio panel scripts config/probes
pytest studio/tests -q
python -m py_compile config/probes/*.py scripts/check_models_manifest.py
python -m comfy_studio.templates
python scripts/check_models_manifest.py --fixture --offline
python scripts/prune_model_cache.py --json   # plan de retención (no borra nada)
```

2. **Rust**:
```
cd comfyctl
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --locked
```

3. **Nginx**:
```
nginx -t -c config/nginx.conf
```

## Security Rules
1. **NEVER print/persist** `COMFY_PASS`, `STUDIO_LLM*KEY`, `STUDIO_API_TOKEN`, `VAST_API_KEY`, `STUDIO_WEBHOOK_SECRET`
   - **Excepción documentada:** cuando `COMFY_PASS` o `STUDIO_API_TOKEN` se **autogeneran** (el operario no los definió), `scripts/start.sh` los imprime una sola vez en el banner de arranque. Es la única forma de que el operario los lea: los ficheros efímeros `/run/comfyui-pass` y `/run/comfyui-api-token` (0600) los consume `comfyctl supervise` y los borra con `unlink`. El smoke test de CI (paso 15b) exige esa impresión única. Si el operario los define, el banner muestra `[OCULTO]`/`[OCULTA]`.
2. All services bind exclusively to `127.0.0.1`; only nginx exposes port 8188
3. All outputs through public endpoints must pass `SecretRedactor` (studio/config.py)
4. No external CDNs anywhere in the web UI (home, panel, status) — all assets vendored locally
5. Never accept user-provided URLs that point to internal loopback services
6. Never run `pip install` at runtime except through ComfyUI-Manager, which is protected by `PIP_CONSTRAINT=/etc/comfyui/constraints.txt`

## Finding ID Convention
- Mx-SCR-nnn for production issues
- Mx-AGT-nnn for agent governance issues
- Mx-OPS-nnn for operations/volumes/CI
- M3-* for the current "Perfect Pod" milestone

## Board Protocol
- Only post material findings with evidence
- No per-cycle broadcasts to `ALL`
- Use `reply_to` when correcting earlier findings
- Audit cadence: max one audit per day or per PR, no automatic minute-by-minute cycles
- Auditor must include a unique finding ID for every issue; empty or duplicate audits are invalid

## Ownership Map
| Area           | Domain language | Lead agent |
|----------------|-----------------|------------|
| comfyctl/      | Rust            | Agent 3    |
| studio/agent   | Python          | Agent 2    |
| studio/api     | Python          | Agent 2    |
| panel/         | JS              | Agent 2    |
| config/        | Bash/JSON       | Agent 3    |
| scripts/       | Bash/Python     | Agent 1    |
| docs/          | Markdown        | Agent 1    |
| CI/CD          | GitHub Actions  | Agent 1    |
| tools/mac/     | Bash/Python     | Agent 1    |

## Active Agents
1. Implementer: writes code, runs tests, submits changes
2. Scrutiny Auditor: reviews changes, runs full validation, files findings (governed by cadence rules above)
3. Reviewer: final merge gate, CI integration
