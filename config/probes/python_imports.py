#!/usr/bin/env python3
"""Sonda de importaciones críticas de Python invocada por `comfyctl diag` (§4.1)."""


def main() -> None:
    try:
        import cv2
        import hf_xet
        import huggingface_hub
        import insightface

        xet_ver = getattr(hf_xet, "__version__", "installed")
        print(
            f"OK|cv2={cv2.__version__} insightface={insightface.__version__} "
            f"huggingface_hub={huggingface_hub.__version__} hf_xet={xet_ver}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL|Error importando módulos críticos: {exc}")


if __name__ == "__main__":
    main()
