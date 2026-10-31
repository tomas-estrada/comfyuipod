# ComfyUIPod Documentation

Welcome to the ComfyUIPod documentation. This repository contains documentation for runbooks, architecture diagrams, and audit reports.

## Runbooks

Runbooks for common failure modes:
- [ComfyUI Crash](runbooks/comfyui-crash.md)
- [GPU Out-of-Memory (OOM)](runbooks/gpu-oom.md)
- [Model Download Failures](runbooks/model-download-failures.md)
- [Nginx Issues](runbooks/nginx-issues.md)
- [Studio Agent Failures](runbooks/studio-agent-failures.md)

## Architecture Diagrams

Architecture diagrams for data flow and component interactions:
- [High-Level Architecture](architecture/high-level.md)
- [ComfyUI Data Flow](architecture/comfyui-data-flow.md)
- [Studio Agent Workflow](architecture/studio-agent-workflow.md)
- [Nginx Configuration](architecture/nginx-config.md)

## Audit Tooling

Automated security audits are integrated into the CI/CD pipeline to detect vulnerabilities in dependencies and container images.

### Running Audits Locally
To run audits locally, use the following script:

```bash
chmod +x scripts/audit.sh
./scripts/audit.sh
```

### Audit Reports

Automated audit reports for security and compliance:
- [Trivy Vulnerability Reports](audits/trivy-report.md)
- [Grype Dependency Reports](audits/grype-report.md)

### CI/CD Integration
Audits run automatically on:
- Push to `main` branch.
- Pull requests to `main` branch.
- Daily scheduled runs.

View audit results in the [GitHub Actions](https://github.com/<your-repo>/actions) tab.
