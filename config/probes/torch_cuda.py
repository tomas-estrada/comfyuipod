#!/usr/bin/env python3
"""Sonda de PyTorch + CUDA + matmul 1024x1024 fp16 invocada por `comfyctl diag` (§4.1)."""

import time


def main() -> None:
    try:
        import torch

        ver = torch.__version__
        cu = torch.version.cuda
        avail = torch.cuda.is_available()
        if avail:
            dev = torch.cuda.get_device_name(0)
            t0 = time.perf_counter()
            a = torch.randn((1024, 1024), device="cuda", dtype=torch.float16)
            b = torch.randn((1024, 1024), device="cuda", dtype=torch.float16)
            _ = torch.matmul(a, b)
            torch.cuda.synchronize()
            dt_ms = (time.perf_counter() - t0) * 1000.0
            print(
                f"OK|torch={ver} cuda={cu} cuda.is_available=True "
                f"device='{dev}' matmul_1024x1024={dt_ms:.2f}ms"
            )
        else:
            print(f"WARN|torch={ver} cuda={cu} cuda.is_available=False (ejecutando en modo CPU)")
    except Exception as exc:
        print(f"FAIL|Error importando o probando torch: {exc}")


if __name__ == "__main__":
    main()
