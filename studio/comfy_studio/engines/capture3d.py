"""Motor de captura 3D / Nerfstudio (M5-SCR, fase 9).

Convierte fotos o vídeo reales de una escena en una reconstrucción 3D
(Gaussian Splatting) exportable a .ply para Blender. tier INSTALLABLE (pesado:
CUDA, no reside). Stub-first (`STUDIO_STUB_CAPTURE3D=1`). Backend real vía los
binarios horneados en la imagen (`STUDIO_CAPTURE3D_BIN_DIR=/usr/local/bin`).

Operaciones reales de Nerfstudio 1.1.5 (verificadas contra los contratos de la
versión fijada en la imagen; los "formatos" de exportación son MÉTODOS de
`ns-export`, no extensiones):

  process  ns-process-data images --data <fotos> --output-dir <proc>
           (o `video --data <clip>`) -> dataset procesado con COLMAP
  train    ns-train splatfacto --data <proc> --output-dir <out> [--experiment-name run]
           -> config.yml bajo <out>/<exp>/splatfacto/<ts>/config.yml
  render   ns-render dataset --load-config <config.yml> --output-dir <out>
           -> frames rgb/ y depth/ por cámara del dataset
  export   ns-export gaussian-splat --load-config <config.yml> --output-dir <out>
           -> <out>/…/*.ply (también `point-cloud`)

Cada op verifica los ARTEFACTOS esperados antes de dar ok: un directorio vacío,
un stub o un PLY ficticio no cuentan como reconstrucción. Un splat .ply no es
automáticamente una malla texturizada importable en Blender (eso se declara en
el escenario, no aquí).
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

_OPS = ("process", "train", "render", "export")
#: métodos reales de `ns-export` (no extensiones de archivo)
_EXPORT_METHODS = ("gaussian-splat", "point-cloud")
_PROCESS_KINDS = ("images", "video")
_STUB_PLY = b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"
_BIN_ENV = "STUDIO_CAPTURE3D_BIN_DIR"  # dir con ns-process-data/ns-train/ns-render/ns-export
_DEFAULT_TIMEOUT_S = 7200.0  # entrenar NeRF/splat es largo
_REQUIRED_BINS = ("ns-process-data", "ns-train", "ns-export")


@dataclass(slots=True)
class Capture3dSpec:
    op: str
    output_path: Path
    input_path: Path | None = None  # process: dir de fotos o clip; train: dataset procesado
    process_kind: str = "images"  # images | video
    model_path: Path | None = None  # render/export: config.yml del entrenamiento
    export_method: str = "gaussian-splat"
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def validate(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"op debe ser uno de {_OPS}, no {self.op!r}")
        if self.op == "process":
            if self.input_path is None:
                raise ValueError("op=process requiere 'input_path'")
            if self.process_kind not in _PROCESS_KINDS:
                raise ValueError(
                    f"process_kind debe ser uno de {_PROCESS_KINDS}, no {self.process_kind!r}"
                )
        if self.op == "train" and self.input_path is None:
            raise ValueError("op=train requiere 'input_path' (dataset procesado)")
        if self.op in ("render", "export") and self.model_path is None:
            raise ValueError(f"op={self.op} requiere 'model_path' (config.yml entrenado)")
        if self.op == "export" and self.export_method not in _EXPORT_METHODS:
            raise ValueError(
                f"export_method debe ser un método de ns-export {_EXPORT_METHODS}, "
                f"no {self.export_method!r} (glb/ply no son métodos)"
            )


class Capture3dEngine:
    """Motor de captura 3D (Nerfstudio). Contrato de 4 verbos reales."""

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

    def _available(self, tool: str | None = None) -> bool:
        if tool:
            return bool(self._bin(tool))
        return all(self._bin(t) for t in _REQUIRED_BINS)

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "export_methods": list(_EXPORT_METHODS),
                "process_kinds": list(_PROCESS_KINDS),
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
        missing = [t for t in _REQUIRED_BINS if not self._bin(t)]
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={
                "available": self._available(),
                "missing_bins": missing,
                "stub": self.is_stub(),
            },
            error=None if ready else f"binarios Nerfstudio ausentes: {missing}",
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
            c.output_path.mkdir(parents=True, exist_ok=True)
            (c.output_path / "stub.ply").write_bytes(_STUB_PLY)
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

        needed = {"process": "ns-process-data", "train": "ns-train",
                  "render": "ns-render", "export": "ns-export"}[c.op]
        if not self._bin(needed) or not self._available():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"Nerfstudio no disponible ({needed} ausente en {_BIN_ENV} o PATH); "
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
            process_kind=str(job.get("process_kind", "images")),
            model_path=Path(job["model_path"]) if job.get("model_path") else None,
            export_method=str(job.get("export_method", "gaussian-splat")),
        )

    def _cmd(self, c: Capture3dSpec) -> list[str]:
        if c.op == "process":
            return [
                self._bin("ns-process-data"), c.process_kind,
                "--data", str(c.input_path),
                "--output-dir", str(c.output_path),
            ]
        if c.op == "train":
            return [
                self._bin("ns-train"), "splatfacto",
                "--data", str(c.input_path),
                "--output-dir", str(c.output_path),
                "--experiment-name", "run",
            ]
        if c.op == "render":
            return [
                self._bin("ns-render"), "dataset",
                "--load-config", str(c.model_path),
                "--output-dir", str(c.output_path),
            ]
        return [
            self._bin("ns-export"), c.export_method,
            "--load-config", str(c.model_path),
            "--output-dir", str(c.output_path),
        ]

    def _expected_artifacts(self, c: Capture3dSpec) -> list[Path]:
        out = c.output_path
        if c.op == "process":
            # ns-process-data images produce <out>/images y <out>/colmap
            return [p for p in (out / "images", out / "colmap") if p.exists()]
        if c.op == "train":
            # config.yml del entrenamiento bajo <out>/<exp>/<method>/<ts>/
            return sorted(out.rglob("config.yml"))[:1] if out.is_dir() else []
        if c.op == "render":
            return [p for p in out.rglob("*") if p.suffix in (".png", ".jpg", ".mp4")][:1] \
                if out.is_dir() else []
        # export: un .ply real bajo el output dir
        return sorted(out.rglob("*.ply"))[:1] if out.is_dir() else []

    def _run_ns(self, c: Capture3dSpec, start: float) -> EngineResult:
        redactor = SecretRedactor()
        cmd = self._cmd(c)
        c.output_path.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(
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
                    f"ns-{c.op} falló rc={res.returncode}: {(res.stderr or '')[:300]}"
                ),
            )
        artifacts = self._expected_artifacts(c)
        if not artifacts:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    f"ns-{c.op} terminó rc=0 pero no produjo los artefactos esperados "
                    f"en {c.output_path} (un stub o directorio vacío no es una reconstrucción)"
                ),
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={
                "op": c.op,
                "output": str(c.output_path),
                "artifacts": [str(a) for a in artifacts],
                "duration_s": round(time.monotonic() - start, 3),
            },
        )
