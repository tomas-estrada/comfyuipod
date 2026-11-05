"""Edición causal, matting y capas RGBA (plan Ultra, W15 / A17).

W15 pide omnimatte/capas RGBA con alpha/color y conservación por región, edición
causal (modificar una causa actualiza solo efectos relacionados) y reconstrucción
lossless. El invariante (A17): no reempaquetar CorridorKey, no etiquetar una
imagen como video multicapa, y la máscara local/reasoner es configurable (VOID no
depende obligatoriamente de un servicio cloud).

Este módulo implementa el CONTRATO de composición por capas y la edición causal:

- `Layer`: una capa RGBA con su alpha y una dependencia causal opcional (de qué
  capa "fuente" depende, p. ej. una sombra depende del actor).
- `composite`: compone las capas con el operador *over* (Porter-Duff), premul,
  en orden. Es lossless y reproducible.
- `edit_causal`: al modificar una capa, invalida SOLO las capas cuyo `caused_by`
  la referencia (transitivo), conservando las demás intactas — reutiliza la idea
  del lineage (W04) a nivel de píxeles/capas.
- `reconstruct_lossless`: dadas las capas y el resultado, verifica que la
  composición es exactamente reproducible (A17).

Opera sobre un modelo de color/alpha simbólico (no decodifica frames reales: eso
es FFmpeg/engine, build/GPU). Verifica el álgebra de composición y la causalidad,
que es lo que A17 pide a nivel de contrato.
"""

from __future__ import annotations

from dataclasses import dataclass

#: color RGBA en [0,1].
RGBA = tuple[float, float, float, float]


class LayerError(ValueError):
    """Contrato de capas inválido."""


@dataclass(slots=True)
class Layer:
    """Una capa RGBA componible con su procedencia causal."""

    layer_id: str
    #: color RGBA uniforme (modelo simbólico de un parche de la capa).
    rgba: RGBA
    #: id de la capa de la que ESTA depende causalmente (sombra->actor), o None.
    caused_by: str | None = None
    #: región propia (para conservación por región); etiqueta opaca.
    region: str = "full"

    def validate(self) -> None:
        if not (0.0 <= self.rgba[3] <= 1.0):
            raise LayerError(f"{self.layer_id}: alpha fuera de [0,1]")
        for c in self.rgba[:3]:
            if not (0.0 <= c <= 1.0):
                raise LayerError(f"{self.layer_id}: color fuera de [0,1]")


def _over(src: RGBA, dst: RGBA) -> RGBA:
    """Operador *over* de Porter-Duff (src sobre dst), alpha recto."""
    sa = src[3]
    da = dst[3]
    out_a = sa + da * (1.0 - sa)
    if out_a <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    out_rgb = tuple(
        (src[i] * sa + dst[i] * da * (1.0 - sa)) / out_a for i in range(3)
    )
    return (out_rgb[0], out_rgb[1], out_rgb[2], out_a)


def composite(layers: list[Layer], *, background: RGBA = (0.0, 0.0, 0.0, 0.0)) -> RGBA:
    """Compone las capas de abajo (índice 0) a arriba con *over*. Lossless."""
    for layer in layers:
        layer.validate()
    acc = background
    for layer in layers:
        acc = _over(layer.rgba, acc)
    return acc


def affected_by(layers: list[Layer], changed_id: str) -> set[str]:
    """Capas afectadas por un cambio en `changed_id`: ella y su cadena causal.

    Una capa B con `caused_by == A` se ve afectada si A cambia (sombra sigue al
    actor). Transitivo. Una capa de otra región/fuente NO se toca (A17:
    «regiones intactas conservan sus fuentes»).
    """
    affected = {changed_id}
    changed = True
    while changed:
        changed = False
        for layer in layers:
            if layer.caused_by in affected and layer.layer_id not in affected:
                affected.add(layer.layer_id)
                changed = True
    return affected


def edit_causal(
    layers: list[Layer], changed_id: str, new_rgba: RGBA
) -> tuple[list[Layer], set[str]]:
    """Modifica una capa; devuelve (capas nuevas, ids a recomponer).

    Solo la capa cambiada y sus efectos causales quedan marcados para recomputar;
    las demás conservan su fuente intacta. No muta las no afectadas.
    """
    ids = {ly.layer_id for ly in layers}
    if changed_id not in ids:
        raise LayerError(f"capa inexistente: {changed_id}")
    to_recompute = affected_by(layers, changed_id)
    out: list[Layer] = []
    for ly in layers:
        if ly.layer_id == changed_id:
            out.append(Layer(ly.layer_id, new_rgba, ly.caused_by, ly.region))
        else:
            # Las capas no cambiadas se conservan tal cual (misma identidad).
            out.append(ly)
    return out, to_recompute


def reconstruct_lossless(layers: list[Layer], expected: RGBA, *, tol: float = 1e-9) -> bool:
    """Verifica que la composición reproduce exactamente `expected` (A17)."""
    got = composite(layers)
    return all(abs(got[i] - expected[i]) <= tol for i in range(4))


@dataclass(slots=True)
class MatteSource:
    """Fuente de máscara/matte configurable (A17: VOID no depende de cloud)."""

    name: str
    kind: str  # "local" | "cloud"
    available: bool = True
    note: str = ""

    def is_local(self) -> bool:
        return self.kind == "local"


def resolve_matte_source(sources: list[MatteSource]) -> MatteSource:
    """Elige una fuente de matte; prefiere una LOCAL disponible (A17).

    VOID/omnimatte no deben depender obligatoriamente de un servicio cloud: si
    hay una fuente local disponible, se usa. Si solo hay cloud y no está
    disponible, se lanza en vez de fingir una máscara.
    """
    local = [s for s in sources if s.is_local() and s.available]
    if local:
        return local[0]
    cloud = [s for s in sources if not s.is_local() and s.available]
    if cloud:
        return cloud[0]
    raise LayerError("ninguna fuente de matte disponible (local ni cloud)")


__all__ = [
    "Layer",
    "LayerError",
    "MatteSource",
    "affected_by",
    "composite",
    "edit_causal",
    "reconstruct_lossless",
    "resolve_matte_source",
]
