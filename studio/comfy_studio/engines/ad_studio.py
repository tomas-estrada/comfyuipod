"""Motor del estudio de anuncios (ad_studio): brief -> guion -> storyboard -> variantes.

Paquete de 3 ops coherentes (ver docs/propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md),
equivalente abierto a las funciones "Marketing Studio / URL-to-video / variantes de
formato" de Higgsfield, ClipLoft o CapCut auto-reframe, usando solo motores ya
presentes en el pod (ffmpeg/EDL, audio TTS, music_gen, qa):

    script     -> brief estructurado -> guion JSON (composer determinista).
    storyboard -> guion -> storyboard JSON (planos + texto en pantalla + voz).
    variants   -> storyboard + assets -> MP4 por aspect ratio (9:16/1:1/16:9/4:5)
                  con voz/musica opcionales, QA automatica y progreso/cancelacion
                  vía job. Requiere las dependencias inyectadas (store/broker/config).

tier WARM. Stub-first (`STUDIO_STUB_AD_STUDIO=1`): las variantes escriben un MP4
mínimo y se marcan `simulated: true`. Sin dependencias o sin ffmpeg, falla TIPADO.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from comfy_studio.ads.pipeline import (
    AdCancelledError,
    AdPipelineError,
    attach_storyboard,
    create_ad_doc,
    get_ad_doc,
    run_ad_variants,
)
from comfy_studio.ads.schema import (
    AdBrief,
    AdScript,
    AdStoryboard,
    build_storyboard,
    compose_script,
)
from comfy_studio.config import StudioConfig
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_stub_mode,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

_OPS = ("script", "storyboard", "variants")


class AdStudioEngine:
    """Motor del estudio de anuncios. Contrato de 4 verbos; `run` despacha por op."""

    name = "ad_studio"
    tier = EngineTier.WARM

    def __init__(
        self,
        *,
        store: ProjectStore | None = None,
        broker: ProgressBroker | None = None,
        config: StudioConfig | None = None,
    ) -> None:
        self._store = store
        self._broker = broker
        self._config = config

    def is_stub(self) -> bool:
        return is_stub_mode(self.name)

    # -- contrato ------------------------------------------------------------

    def list(self) -> EngineResult:
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={
                "ops": list(_OPS),
                "aspect_ratios": ["16:9", "9:16", "1:1", "4:5"],
                "stub": self.is_stub(),
                "deps_ready": self._store is not None,
            },
        )

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        return EngineResult(
            ok=False,
            engine=self.name,
            verb="install",
            error=(
                "ad_studio no instala submotores: reutiliza ffmpeg/EDL, audio, "
                "music_gen y qa ya horneados; los modelos nuevos van por el "
                "manifiesto controlado (config/models-studio.json)"
            ),
        )

    def verify(self, submotor: str) -> EngineResult:
        if submotor not in _OPS:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="verify",
                error=f"op desconocida: {submotor!r} (usa una de {list(_OPS)})",
            )
        ready = submotor in ("script", "storyboard") or self._store is not None or self.is_stub()
        return EngineResult(
            ok=ready,
            engine=self.name,
            verb="verify",
            payload={
                "op": submotor,
                "stub": self.is_stub(),
                "deps_ready": self._store is not None,
            },
            error=None
            if ready
            else "op 'variants' requiere store inyectado (o STUDIO_STUB_AD_STUDIO=1)",
        )

    def run(self, spec: EngineRunSpec) -> EngineResult:
        job = spec.job
        op = str(job.get("op", ""))
        if op not in _OPS:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"op debe ser una de {list(_OPS)}, no {op!r}",
            )
        start = time.monotonic()
        try:
            if op == "script":
                return self._op_script(job, start)
            if op == "storyboard":
                return self._op_storyboard(job, start)
            return self._op_variants(job, spec, start)
        except AdCancelledError as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=str(exc), payload={"cancelled": True}
            )
        except AdPipelineError as exc:
            return EngineResult(ok=False, engine=self.name, verb="run", error=str(exc))
        except (KeyError, ValueError, TypeError) as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="run", error=f"spec inválido: {exc}"
            )

    # -- ops -----------------------------------------------------------------

    def _op_script(self, job: dict[str, Any], start: float) -> EngineResult:
        brief = AdBrief.model_validate(job.get("brief") or {})
        script = compose_script(brief)
        payload: dict[str, Any] = {"script": script.to_dict()}
        out = job.get("output_path")
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(script.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            payload["output"] = str(path)
        if self._store is not None and job.get("project_id"):
            doc = create_ad_doc(self._store, str(job["project_id"]), brief)
            payload["ad_id"] = doc["id"]
            payload["ad"] = doc
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={**payload, "duration_s": round(time.monotonic() - start, 3)},
        )

    def _op_storyboard(self, job: dict[str, Any], start: float) -> EngineResult:
        if self._store is None:
            script_raw = job.get("script")
            ad_id = str(job.get("ad_id") or "")
            if not script_raw or not ad_id:
                return EngineResult(
                    ok=False,
                    engine=self.name,
                    verb="run",
                    error=(
                        "storyboard requiere 'store'+'ad_id' (persistir) "
                        "o 'script'+'ad_id' (puro)"
                    ),
                )
            script = AdScript.model_validate(script_raw)
            storyboard = build_storyboard(script, ad_id=ad_id)
        else:
            project_id = str(job.get("project_id") or "")
            ad_id = str(job.get("ad_id") or "")
            if not project_id or not ad_id:
                return EngineResult(
                    ok=False,
                    engine=self.name,
                    verb="run",
                    error="storyboard requiere 'project_id' y 'ad_id'",
                )
            doc = attach_storyboard(self._store, project_id, ad_id)
            storyboard = AdStoryboard.model_validate(doc["storyboard"])
        payload: dict[str, Any] = {
            "ad_id": storyboard.ad_id,
            "storyboard": storyboard.to_dict(),
        }
        out = job.get("output_path")
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(storyboard.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            payload["output"] = str(path)
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={**payload, "duration_s": round(time.monotonic() - start, 3)},
        )

    def _op_variants(self, job: dict[str, Any], spec: EngineRunSpec, start: float) -> EngineResult:
        if self._store is None:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=(
                    "variants requiere store inyectado en el motor "
                    "(o STUDIO_STUB_AD_STUDIO=1 con store)"
                ),
            )
        project_id = str(job.get("project_id") or "")
        ad_id = str(job.get("ad_id") or "")
        if not project_id or not ad_id:
            return EngineResult(
                ok=False, engine=self.name, verb="run",
                error="variants requiere 'project_id' y 'ad_id'",
            )
        summary = run_ad_variants(
            self._store,
            project_id,
            ad_id,
            broker=self._broker,
            config=self._config,
            job_id=job.get("job_id"),
            aspect_ratios=job.get("aspect_ratios") or None,
            voiceover=bool(job.get("voiceover", False)),
            voice_id=str(job.get("voice_id", "default")),
            music=bool(job.get("music", False)),
            music_prompt=str(job.get("music_prompt", "")),
            music_duration_s=float(job.get("music_duration_s", 0.0) or 0.0),
            source_asset_id=job.get("source_asset_id"),
            chunk_asset_ids=job.get("chunk_asset_ids") or None,
            fps=int(job.get("fps", 24)),
            max_retries=int(job.get("max_retries", 0)),
            acceptance=job.get("acceptance") or None,
            simulated=self.is_stub(),
        )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={**summary, "duration_s": round(time.monotonic() - start, 3)},
            simulated=bool(summary.get("simulated")),
            stderr_tail="STUDIO_STUB_AD_STUDIO: variantes simuladas" if self.is_stub() else None,
        )


def get_ad_doc_public(store: ProjectStore, project_id: str, ad_id: str) -> dict[str, Any] | None:
    return get_ad_doc(store, project_id, ad_id)
