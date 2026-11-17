# Security Policy

ComfyUIPod packages ComfyUI + Comfy Studio into a single Linux GPU container meant to be
deployed fast on a rented pod (Vast.ai today). Because pods are public-facing and most
contributions — including automated/AI-agent ones — move quickly, this policy is short:
report privately, we respond fast, fixes ship on `main` and in the next published image.

## Reporting a vulnerability

**Preferred: GitHub Private Vulnerability Reporting.**
Go to <https://github.com/tomas-estrada/comfyuipod/security/advisories/new>
(repo → *Security* → *Report a vulnerability*) and open a private advisory.

**Alternative:** email `SECURITY-CONTACT-EMAIL` *(replace with your address)*.

Please **do not** report vulnerabilities in public issues, pull requests, comments, or
discussions — those are visible to everyone, including attackers.

**Never include live credentials or tokens in a report** (`COMFY_PASS`, `STUDIO_API_TOKEN`,
`STUDIO_LLM_KEY`, `STUDIO_LLM_FALLBACK_KEY`, `STUDIO_WEBHOOK_SECRET`, `HF_TOKEN`, ...).
If your reproduction needs one, redact it. A running pod's `/report.md` is automatically
secret-redacted by `comfyctl` and is safe to attach as-is.

## What to include

- Affected component and version: image tag (or commit) and, if relevant, the pod setup
  (Vast.ai, exposed port, `COMFY_TLS`, `ENABLE_WEB_TERMINAL`, ...)
- Clear description of the issue and its security impact
- Reproduction steps, PoC, or redacted logs
- Suggested fix, if you have one
- Whether the issue has already been disclosed publicly anywhere

If you're unsure whether something is in scope: **ask privately anyway**. We'd rather hear it.

## What happens next

- **Acknowledgement** within 3 days.
- **Triage and severity assessment** within 7 days; we'll tell you if we need more info.
- Status updates as we work. There are no long release cycles here: fixes land on `main`
  and a new image is published promptly (see `.github/workflows/docker-publish.yml`).
- **Coordinated disclosure**: we agree a publication date with you, then publish a GitHub
  security advisory (and request a CVE for high-severity issues). You are credited unless
  you prefer to stay anonymous.

## Supported versions

| Version | Supported |
|---|---|
| `main` branch and the latest published container image | Yes |
| Older commits / older image tags | No — upgrade to the latest image |

Security fixes are only shipped forward, so the answer to "am I affected?" is almost always
"upgrade to the latest image".

## Scope

**In scope** — the pod image and everything in this repo:

- `Dockerfile` and the image build, `config/` (incl. `nginx.conf`), `scripts/`
- `comfyctl` (supervisor, `/status`, `/report.md` redaction, `/diag`)
- Comfy Studio (`studio/`, `panel/`): `/studio/`, `/studio/api/*`, `/mcp` (token auth)
- The public surface of a deployed pod on port `8188`: Basic Auth, optional TLS, the
  `/home` portal, security headers, optional `/terminal/` (off by default)
- Outbound webhooks (HMAC-SHA256 signing, SSRF protections)
- Secret handling and redaction everywhere (logs, `/status/api`, `/report.md`)
- CI (`.github/workflows/`) and docs that recommend insecure settings

**Out of scope** (report upstream instead):

- The Vast.ai / RunPod platforms themselves
- ComfyUI core, custom nodes, and third-party models — report to their own projects
- Pods where `COMFY_PASS` was exposed in a *public* template (rotate the password instead)
- Denial of service against your own instance, social engineering, physical access

## Safe harbor

We support good-faith security research. If you report under this policy we will not
pursue legal action against you. In return: test only on pods you own, do not access or
modify other users' data, do not degrade the service for others, and give us a reasonable
window to fix before any public disclosure.

## Notes for automated tools and AI agents

Most pull requests in this repo come from AI agents — welcome. A few rules keep that safe:

- Security findings from agents go through the **same private channel** above, never
  through public issues or PRs.
- Never commit real secrets or tokens, in code, config, logs, tests, or PR descriptions.
- If you find a leaked secret anywhere in the repo or its history: report it privately
  **and rotate it immediately** — do not quote it publicly.
- Prefer `/report.md` (auto-redacted) over raw logs when attaching diagnostics.
- Automated scanners: open a private advisory only for confirmed, reproducible issues in
  scope. Please don't file public issues for theoretical or unverified findings.

## Recognition

Reporters of confirmed vulnerabilities are credited in the published security advisory
(or kept anonymous if they prefer).
