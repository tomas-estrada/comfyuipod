import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
START_SH = ROOT / "scripts/start.sh"


def test_dockerfile_entrypoint_points_to_start_sh():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "/start.sh"]' in text, (
        "Dockerfile debe declarar ENTRYPOINT -> /start.sh"
    )


def test_start_sh_has_idempotency_guard():
    text = START_SH.read_text(encoding="utf-8")
    assert re.search(r'pgrep\s+-x\s+"comfyctl"', text), (
        "start.sh debe tener un guard de idempotencia (pgrep comfyctl)"
    )
    assert "start.sh saliendo sin duplicar procesos" in text, (
        "start.sh debe salir sin duplicar si comfyctl ya corre"
    )
