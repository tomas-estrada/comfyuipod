"""Paquete principal del servidor Comfy Studio."""

from __future__ import annotations

__version__ = "0.1.0"

# M3-IDEA-005: los subsistemas de percepción/ASR actuales son simulaciones
# deterministas (sin pesos reales cargados). Toda salida orientada al usuario
# debe etiquetarse con `simulated: true` y este aviso hasta que se integren
# los backends reales (M3-IDEA-001..004).
SIMULATED_ML_NOTICE = (
    "Resultado generado por el modo de simulación determinista de Comfy Studio "
    "(sin modelos reales cargados en esta versión). No evaluar calidad de "
    "modelo a partir de esta salida."
)
