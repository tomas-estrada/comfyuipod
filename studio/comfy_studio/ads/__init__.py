"""Estudio de anuncios (ad_studio): brief de producto -> guion -> storyboard -> variantes.

Paquete coherente de tres funciones nativas del estudio (ver
`docs/propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md`):

  - `script`     : brief estructurado -> guion de anuncio (composer determinista).
  - `storyboard` : guion -> storyboard JSON con planos, texto en pantalla y voz.
  - `variants`   : storyboard + assets del proyecto -> variantes de formato
                   (9:16, 1:1, 16:9, 4:5) montadas con ffmpeg, con voz/musica
                   opcionales y evaluacion QA automatica por variante.

Todo el paquete compone motores ya existentes del pod (ffmpeg/EDL, audio TTS,
music_gen, QA) y no descarga modelos nuevos: los pesos siguen perteneciendo a
las rutas controladas de ComfyUI/manifiesto.
"""

from __future__ import annotations

from .pipeline import (
    AdCancelledError,
    AdPipelineError,
    attach_storyboard,
    create_ad_doc,
    get_ad_doc,
    list_ad_docs,
    run_ad_variants,
    save_ad_doc,
)
from .schema import (
    AD_DOC_SCHEMA,
    AD_SCRIPT_SCHEMA,
    AD_STORYBOARD_SCHEMA,
    AdBrief,
    AdScene,
    AdScript,
    AdShot,
    AdStoryboard,
    build_storyboard,
    compose_script,
)

__all__ = [
    "AD_DOC_SCHEMA",
    "AD_SCRIPT_SCHEMA",
    "AD_STORYBOARD_SCHEMA",
    "AdBrief",
    "AdCancelledError",
    "AdPipelineError",
    "AdScene",
    "AdScript",
    "AdShot",
    "AdStoryboard",
    "attach_storyboard",
    "build_storyboard",
    "compose_script",
    "create_ad_doc",
    "get_ad_doc",
    "list_ad_docs",
    "run_ad_variants",
    "save_ad_doc",
]
