"""Motor de captura 3D / Nerfstudio (M5-SCR, fase 9).

Convierte fotos o vídeo reales de un objeto/escena en una escena 3D (NeRF /
Gaussian Splatting) exportable a .ply/.glb para Blender. Es el salto de "malla
de juguete" a captura real de producto. tier INSTALLABLE (pesado: CUDA, no
reside). Stub-first (`STUDIO_STUB_CAPTURE3D=1`). Backend real vía los binarios
`ns-process-data` / `ns-train` / `ns-export` horneados; sin ellos falla TIPADO.

Operaciones (`spec.job['op']`):
  train   → imágenes/vídeo de entrada → modelo entrenado (directorio de salida).
  export  → modelo entrenado → malla/nube .ply o .glb.

Subproceso aislado, timeout amplio (entrenar es largo), stderr redactado.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from comfy_studio.config import SecretRedactor
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)

_OPS = ("train", "export")
_EXPORT_FORMATS = ("ply", "glb")
_STUB_PLY = b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"
_BIN_ENV = "STUDIO_CAPTURE3D_BIN_DIR"  # dir con ns-train/ns-export/ns-process-data
_DEFAULT_TIMEOUT_S = 7200.0  # entrenar NeRF es largo


@dataclass(slots=True)
class Capture3dSpec:
    op: str
    output_path: Path
    input_path: Path | None = None  # train: imágenes/vídeo
    model_path: Path | None = None  # export: directorio del modelo entrenado
    export_format: str = "glb"
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"op debe ser uno de {_OPS}, no {self.op!r}")
        if self.op == "train" and self.input_path is None:
            raise ValueError("op=train requiere 'input_path'")
        if self.op == "export":
            if self.model_path is None:
                raise ValueError("op=export requiere 'model_path'")
            if self.export_format not in _EXPORT_FORMATS:
                raise ValueError(f"export_format debe ser uno de {_EXPORT_FORMATS}")


class Capture3dEngine:
    """Motor de captura 3D (Nerfstudio). Contrato de 4 verbos."""

    name = "capture3d"
    tier = EngineTier.INSTALLABLE

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    def _bin(self, tool: str) -> str:
        bin_dir = (os.environ.get(_BIN_ENV) or "").strip()
        if bin_dir:
            cand = Path(bin_dir) / tool
            if cand.is_file():
                return str(cand)
        return shutil.which(tool) or ""

    def _available(self) -> bool:
        return bool(self._bin("ns-train")) or bool(self._bin("ns-export"))

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "export_formats": list(_EXPORT_FORMATS),
                "available": self._available(),
                "stub": self.is_stub(),
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="Nerfstudio se hornea pinned en la imagen; no se instala en runtime",
        )

    def verify(self, submotor: str) -> EngineResult:
        ready = self._available() or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={"available": self._available(), "stub": self.is_stub()},
            error=None if ready else f"binarios Nerfstudio ausentes ({_BIN_ENV} o PATH)",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            c = self._parse(spec.job)
            c.validate()
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

        start = time.monotonic()
        if self.is_stub():
            c.output_path.parent.mkdir(parents=True, exist_ok=True)
            c.output_path.write_bytes(_STUB_PLY if c.op == "export" else b"[STUB nerf model]")
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="run",
                payload={
                    "op": c.op,
                    "output": str(c.output_path),
                    "duration_s": round(time.monotonic() - start, 3),
                },
                simulated=True,
                stderr_tail="STUDIO_STUB_CAPTURE3D: captura simulada",
            )

        if not self._available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"Nerfstudio no disponible ({_BIN_ENV} o PATH); "
                    "hornear los binarios o usar STUDIO_STUB_CAPTURE3D=1"
                ),
            )
        return self._run_ns(c, start)

    @staticmethod
    def _parse(job: dict) -> Capture3dSpec:
        return Capture3dSpec(
            op=str(job.get("op", "")),
            output_path=Path(job["output_path"]),
            input_path=Path(job["input_path"]) if job.get("input_path") else None,
            model_path=Path(job["model_path"]) if job.get("model_path") else None,
            export_format=str(job.get("export_format", "glb")),
        )

    def _run_ns(self, c: Capture3dSpec, start: float) -> EngineResult:
        redactor = SecretRedactor()
        if c.op == "train":
            tool = self._bin("ns-train")
            cmd = [tool, "--data", str(c.input_path), "--output-dir", str(c.output_path)]
        else:
            tool = self._bin("ns-export")
            cmd = [
                tool,
                c.export_format,
                "--load-config",
                str(c.model_path),
                "--output-dir",
                str(c.output_path),
            ]
        if not tool:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"binario Nerfstudio para op={c.op} no encontrado",
            )
        c.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(  # noqa: S603 - args fijos, sin shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=c.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"Nerfstudio excedió el timeout ({c.timeout_s:.0f}s)",
            )
        except OSError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(f"no se pudo lanzar Nerfstudio: {exc}"),
            )
        if res.returncode != 0:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=redactor.redact(
                    f"Nerfstudio falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "op": c.op,
                "output": str(c.output_path),
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
