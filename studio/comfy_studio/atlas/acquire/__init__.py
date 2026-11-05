"""Adquisición del atlas desde fuentes gratuitas autorizadas.

Fuentes: Wikimedia Commons (API oficial, sin credenciales), extracto fechado de
OpenStreetMap (Geofabrik, ODbL) y datos oficiales de Medellín (GeoMedellín,
solo si su licencia de redistribución es verificable). Mapillary sólo con token
legítimo opcional (nunca requerido por los escenarios incluidos).

Todo pasa por `HttpClient`: User-Agent identificable, allowlist de hosts,
límites por host, Retry-After, backoff con máximo de intentos y circuit
breaker (un 429/403 suspende la fuente, no el inventario). Las descargas son
reanudables (Range), con hash, MIME y tamaño máximo antes de promover el archivo.
"""

from __future__ import annotations

DEFAULT_USER_AGENT = (
    "comfyuipod-atlas-worker/1.0 (https://github.com/tomas-estrada/comfyuipod; "
    "open data acquisition for georeferenced media library)"
)
