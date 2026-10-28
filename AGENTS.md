# ComfyUIPod Agent Conventions

## Project Structure
- `comfyctl/` — Rust supervisor, status server, strict checks, triage
- `studio/` — Python FastAPI + LLM agent + job queue + templates
- `panel/` — JS frontend, home portal
- `config/` — nodes.lock, constraints.txt, nginx.conf, model manifests
- `scripts/` — start.sh, download-models.sh, triage patterns
- `tools/mac/comfy` — macOS CLI helper
- `docs/auditorias/` — audit findings

## Validation Commands
Always run these before submitting changes:

1. **Python**:
```
ruff check studio panel scripts config/probes
pytest studio/tests -q
python -m py_compile config/probes/*.py scripts/check_models_manifest.py
python -m comfy_studio.templates
python scripts/check_models_manifest.py --fixture --offline
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
