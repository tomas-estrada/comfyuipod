# Release CI Skill

## Usage
Use this skill when preparing releases, running CI, and updating manifests.

## Workflow
1. Run manifest validation: `python scripts/check_models_manifest.py --fixture --offline`
2. Run template validator: `python -m comfy_studio.templates`
3. Verify Docker build: `.github/workflows/docker-publish.yml`
4. Update README and documentation
5. Verify all validation commands from AGENTS.md pass
