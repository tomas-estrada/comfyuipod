"""Tests de integración en imagen (Dockerfile, nginx.conf, .dockerignore y CI workflow).

Verifica los contratos de VAL-PROFILE-001, VAL-PROFILE-005, VAL-SERVER-002 y VAL-CROSS-003,
así como la limpieza de los 6 warnings Hadolint preexistentes (DL3003, DL4006, DL3025).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"
NGINX_CONF_PATH = REPO_ROOT / "config" / "nginx.conf"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"
START_SH_PATH = REPO_ROOT / "scripts" / "start.sh"


def _extract_dockerfile_run_blocks(dockerfile_text: str) -> list[str]:
    """Extrae todos los bloques lógicos de instrucciones RUN del Dockerfile."""
    blocks: list[str] = []
    lines = dockerfile_text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("RUN "):
            buf = [stripped]
            while buf[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                buf.append(lines[i].strip())
            blocks.append("\n".join(buf))
        i += 1
    return blocks


def test_dockerfile_installs_isolated_studio_venv_and_templates() -> None:
    """Dockerfile copia studio/ y panel/, crea /opt/venvs/studio e instala comfy_studio."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "python3-venv" in text
    assert "PIP_CONSTRAINT=/etc/comfyui/constraints.txt" in text
    assert "COPY studio/ /opt/studio/" in text
    assert "COPY panel/ /etc/comfyui/panel/" in text
    assert "python3 -m venv /opt/venvs/studio" in text
    assert "/opt/venvs/studio/bin/pip install --no-cache-dir /opt/studio" in text
    assert "/opt/venvs/studio/bin/python -m comfy_studio.templates" in text

    # requirements.lock de ComfyUI se genera antes de crear /opt/venvs/studio (cero deriva)
    pos_lock = text.index("pip3 freeze > /etc/comfyui/requirements.lock")
    pos_venv = text.index("python3 -m venv /opt/venvs/studio")
    assert pos_lock < pos_venv


def test_dockerfile_hadolint_warnings_cleaned() -> None:
    """Los 6 warnings Hadolint preexistentes (DL3003, DL4006, DL3025) están limpios."""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")

    # DL4006: SHELL con -o pipefail antes de los RUN con tuberías en el stage 2
    assert 'SHELL ["/bin/bash", "-o", "pipefail", "-c"]' in text
    stage2_text = text.split("FROM nvidia/cuda:", 1)[1]
    pos_shell = stage2_text.index('SHELL ["/bin/bash", "-o", "pipefail", "-c"]')
    pos_first_pipe_run = stage2_text.index("awk '$1 == \"core\"")
    assert pos_shell < pos_first_pipe_run

    # DL3003: Ningún bloque RUN utiliza `cd <dir>`
    for block in _extract_dockerfile_run_blocks(text):
        cd_matches = re.findall(r"(?:^|[;&|]\s*)cd\s+\S+", block)
        assert not cd_matches, f"DL3003: uso de cd en bloque RUN: {cd_matches}"

    # DL3025: HEALTHCHECK CMD y ENTRYPOINT en notación JSON
    assert 'CMD ["curl", "-fsSI", "http://127.0.0.1:8188/healthz"]' in text
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "/start.sh"]' in text


def test_nginx_conf_studio_locations_and_existing_routes_intact() -> None:
    """nginx.conf define los 3 bloques de /studio con Basic-Auth sin alterar rutas previas."""
    text = NGINX_CONF_PATH.read_text(encoding="utf-8")

    # Rutas existentes intactas
    for existing_loc in (
        "location = /favicon.ico",
        "location = /healthz",
        "location = /_starting.html",
        "location = /home",
        "location = /report.md",
        r"location ~ ^/(status|status/api|report\.md|diag|logs/.*)$",
        "location = /terminal",
        "location /terminal/",
        "location = /download-status",
        "location /ws",
        "location /",
    ):
        assert existing_loc in text, f"Ruta existente ausente en nginx.conf: {existing_loc}"

    # Los 3 nuevos bloques de Studio
    for studio_loc in (
        "location /studio/",
        "location /studio/api/",
        "location = /studio/api/progress",
    ):
        assert studio_loc in text, f"Falta bloque {studio_loc} en nginx.conf"

    # Extraer cada bloque y verificar sus directivas críticas
    block_static = text.split("location /studio/ {", 1)[1].split("}", 1)[0]
    assert 'auth_basic "ComfyUI Vast.ai Access";' in block_static
    assert "auth_basic_user_file /etc/nginx/.htpasswd;" in block_static
    assert "alias /etc/comfyui/panel/;" in block_static

    block_api = text.split("location /studio/api/ {", 1)[1].split("}", 1)[0]
    assert 'auth_basic "ComfyUI Vast.ai Access";' in block_api
    assert "auth_basic_user_file /etc/nginx/.htpasswd;" in block_api
    assert "proxy_pass http://127.0.0.1:18190;" in block_api
    assert "proxy_buffering off;" in block_api

    block_sse = text.split("location = /studio/api/progress {", 1)[1].split("}", 1)[0]
    assert 'auth_basic "ComfyUI Vast.ai Access";' in block_sse
    assert "auth_basic_user_file /etc/nginx/.htpasswd;" in block_sse
    assert "proxy_pass http://127.0.0.1:18190;" in block_sse
    assert "proxy_buffering off;" in block_sse
    assert "proxy_cache off;" in block_sse


def test_dockerignore_and_ci_workflow_studio_smoke_and_ghcr_intact() -> None:
    """Verifica .dockerignore, start.sh y el workflow de CI con smoke studio y promoción GHCR."""
    dockerignore = DOCKERIGNORE_PATH.read_text(encoding="utf-8")
    for pattern in (
        ".venv",
        "**/__pycache__/",
        "**/.pytest_cache/",
        "**/.ruff_cache/",
        "**/*.egg-info/",
    ):
        assert pattern in dockerignore

    start_sh = START_SH_PATH.read_text(encoding="utf-8")
    assert "/var/log/studio-server.log" in start_sh

    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "failure-threshold: error" in workflow
    assert "MODEL_PROFILE=studio" in workflow
    assert "smoke-studio" in workflow
    assert "http://127.0.0.1:18188/studio/api/health" in workflow
    assert "http://127.0.0.1:18188/studio/api/progress?max_events=1" in workflow
    assert "docker buildx imagetools create" in workflow
