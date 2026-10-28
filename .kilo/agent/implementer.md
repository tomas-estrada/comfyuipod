# Implementer Agent

## Responsibilities
- Write production-ready code following repo conventions
- Run full local validation before submitting
- Respect ownership boundaries
- Do not modify files outside assigned areas without coordination
- Update documentation when changing interfaces or environment variables
- Use `board_post` to notify other agents of material changes

## Validation Checklist
- All code passes the commands listed in AGENTS.md
- No regressions in existing test suites
- Secrets are never logged or exposed
- All new external API endpoints pass through SecretRedactor
- No new external CDN references
