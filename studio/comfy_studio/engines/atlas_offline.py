"""Motor del atlas offline (plan Ultra, W07 / C07).

Envuelve ``comfy_studio.atlas.packaging`` con el contrato de 4 verbos.

  - ``list``    → estadísticas del bundle si hay una raíz configurada.
  - ``verify``  → ``verify_bundle(require_files=True)`` sobre la raíz.
  - ``run``     → opera ``verify_bundle``; el ``job['op']`` debe ser
                  ``'verify_bundle'``.

Honestidad: ``simulated`` solo es ``False`` cuando ``verify_bundle`` devolvió
sin excepción. Raíz ausente o bundle corrupto → ``ok=False`` tipificado,
nunca ``simulated=True``, nunca lanza.
"""

from __future__ import annotations

import os
from pathlib import Path

from comfy_studio.atlas.packaging import verify_bundle
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
)
from comfy_studio.engines.base import Engine

_OPS = ("verify_bundle",)


def _atlas_root() -> Path | None:
    """Raíz del atlas desde ``STUDIO_ATLAS_ROOT``, o None si no está configurada."""
    val = os.environ.get("STUDIO_ATLAS_ROOT", "").strip()
    if not val:
        return None
    p = Path(val)
    return p if p.is_dir() else None


class AtlasOfflineEngine(Engine):
    """Atlas offline (warm)."""

    name = "atlas-offline"
    tier = EngineTier.WARM

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        root = _atlas_root()
        if root is None:
            return EngineResult(
                ok=True,
                engine=self.name,
                verb="list",
                payload={
                    "root_configured": False,
                    "ops": list(_OPS),
                    "stub": self.is_stub(),
                },
            )
        try:
            report = verify_bundle(root, require_files=True)
        except Exception as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="list",
                error=f"verify_bundle falló al listar: {type(exc).__name__}: {exc}",
            )
        return EngineResult(
            ok=report.get("ok", False),
            engine=self.name,
            verb="list",
            payload={
                "root_configured": True,
                "root": str(root),
                "bundle_ok": report.get("ok", False),
                "checks": report.get("checks", {}),
                "stats": report.get("stats", {}),
                "errors": report.get("errors", [])[:10],
                "ops": list(_OPS),
                "stub": self.is_stub(),
            },
        )

    def verify(self, submotor: str) -> EngineResult:
        root = _atlas_root()
        if root is None:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=(
                    "atlas-offline sin raíz configurada; "
                    "definir STUDIO_ATLAS_ROOT al directorio del bundle"
                ),
            )
        try:
            report = verify_bundle(root, require_files=True)
        except Exception as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"verify_bundle lanzó: {type(exc).__name__}: {exc}",
            )
        ok = report.get("ok", False)
        return EngineResult(
            ok=ok,
            engine=self.name,
            verb="verify",
            payload={"bundle_ok": ok, "checks": report.get("checks", {})},
            error=None if ok else "; ".join(report.get("errors", [])[:10]),
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error="atlas-offline no requiere instalación de submotores",
        )

    def _stub_run(self, spec: EngineRunSpec) -> EngineResult:
        try:
            root = _atlas_root()
            if root is None:
                return EngineResult(
                    ok=False,
                    engine=self.name,
                    verb="run",
                    error=(
                        "atlas-offline sin raíz configurada; "
                        "definir STUDIO_ATLAS_ROOT al directorio del bundle"
                    ),
                    simulated=True,
                )
            report = verify_bundle(root, require_files=True)
            ok = report.get("ok", False)
            return EngineResult(
                ok=ok,
                engine=self.name,
                verb="run",
                payload={"report": report},
                error=None if ok else "; ".join(report.get("errors", [])[:10]),
                simulated=True,
                stderr_tail=(
                    f"STUDIO_STUB_{self.name.upper().replace('-', '_')}:"
                    " verify_bundle simulado"
                ),
            )
        except Exception as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"verify_bundle lanzó (stub): {type(exc).__name__}: {exc}",
                simulated=True,
            )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        if self.is_stub():
            return self._stub_run(spec)
        job = spec.job if isinstance(spec.job, dict) else {}
        op = str(job.get("op", ""))
        if op not in _OPS:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"op desconocida para {self.name}: {op!r}",
            )
        root = _atlas_root()
        if root is None:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    "atlas-offline sin raíz configurada; "
                    "definir STUDIO_ATLAS_ROOT al directorio del bundle"
                ),
            )
        try:
            report = verify_bundle(root, require_files=True)
        except Exception as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"verify_bundle lanzó: {type(exc).__name__}: {exc}",
                simulated=False,
            )
        ok = report.get("ok", False)
        return EngineResult(
            ok=ok,
            engine=self.name,
            verb="run",
            payload={"report": report},
            error=None if ok else "; ".join(report.get("errors", [])[:10]),
            simulated=False,
        )
