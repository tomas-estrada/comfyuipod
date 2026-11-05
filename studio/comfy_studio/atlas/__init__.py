"""Atlas audiovisual georreferenciado del Valle de Aburrá (ComfyUIPod).

Biblioteca local y persistente de lugares, referencias fotográficas
georreferenciadas, vistas/cámaras, cobertura y producciones, servida desde el
disco (SQLite con índice espacial RTree + FTS5) sin depender en runtime de
Wikimedia, Mapillary, GeoMedellín ni ningún tercero.

Raíz por defecto: ``STUDIO_ATLAS_DIR`` o ``/opt/studio-atlas`` en la imagen.
Layout esperado en la raíz:

    catalog.db            índice SQLite (lugares, referencias, vistas, ...)
    files/<aa>/<sha256>.<ext>   bytes de las referencias
    previews/<ref_id>.jpg      miniaturas locales
    licenses/<ref_id>.txt      atribución/licencia por referencia
    osm/                  extracto OSM recortado y derivados (ODbL)
    geodata/              capas oficiales derivada (p. ej. edificios GeoMedellín)
    sources.json          procedencia y licencias por fuente
    manifest.json         hashes sha256 de todo el bundle
    selection.json        vista seleccionada actualmente (estado del agente)
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ATLAS_DIR = "/opt/studio-atlas"
ENV_ATLAS_DIR = "STUDIO_ATLAS_DIR"


def default_atlas_root(env: dict[str, str] | None = None) -> Path:
    """Raíz del atlas: $STUDIO_ATLAS_DIR o /opt/studio-atlas (imagen)."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_ATLAS_DIR) or "").strip()
    return Path(raw) if raw else Path(DEFAULT_ATLAS_DIR)


__all__ = ["DEFAULT_ATLAS_DIR", "ENV_ATLAS_DIR", "default_atlas_root"]
