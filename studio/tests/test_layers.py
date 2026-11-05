"""Edición causal, matting y capas RGBA (plan Ultra, W15 / A17).

Composición over lossless, edición causal (solo efectos relacionados), matte
local configurable. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.layers import (
    Layer,
    LayerError,
    MatteSource,
    affected_by,
    composite,
    edit_causal,
    reconstruct_lossless,
    resolve_matte_source,
)


def test_over_opaque_on_top_wins() -> None:
    bottom = Layer("bg", (1.0, 0.0, 0.0, 1.0))   # rojo opaco
    top = Layer("fg", (0.0, 0.0, 1.0, 1.0))      # azul opaco
    out = composite([bottom, top])
    assert out == (0.0, 0.0, 1.0, 1.0)  # el de arriba opaco gana


def test_over_half_alpha_blends() -> None:
    bottom = Layer("bg", (0.0, 0.0, 0.0, 1.0))         # negro opaco
    top = Layer("fg", (1.0, 1.0, 1.0, 0.5))            # blanco 50%
    out = composite([bottom, top])
    assert out[0] == pytest.approx(0.5)  # gris medio
    assert out[3] == pytest.approx(1.0)


def test_alpha_out_of_range_rejected() -> None:
    with pytest.raises(LayerError, match="alpha"):
        composite([Layer("x", (0.0, 0.0, 0.0, 1.5))])


def test_causal_edit_only_touches_related_effects() -> None:
    """A17: modificar una causa actualiza solo efectos relacionados."""
    actor = Layer("actor", (0.2, 0.2, 0.2, 1.0))
    shadow = Layer("shadow", (0.0, 0.0, 0.0, 0.5), caused_by="actor", region="floor")
    logo = Layer("logo", (1.0, 1.0, 1.0, 1.0), region="corner")  # independiente
    layers = [actor, shadow, logo]

    _, to_recompute = edit_causal(layers, "actor", (0.9, 0.1, 0.1, 1.0))
    # Se recomputan el actor y su sombra (efecto causal); el logo NO.
    assert to_recompute == {"actor", "shadow"}
    assert "logo" not in to_recompute


def test_unrelated_region_keeps_its_source() -> None:
    actor = Layer("actor", (0.2, 0.2, 0.2, 1.0))
    logo = Layer("logo", (1.0, 1.0, 1.0, 1.0), region="corner")
    out, _ = edit_causal([actor, logo], "actor", (0.9, 0.1, 0.1, 1.0))
    # El logo conserva exactamente su color/identidad.
    logo_after = next(ly for ly in out if ly.layer_id == "logo")
    assert logo_after.rgba == (1.0, 1.0, 1.0, 1.0)


def test_affected_by_is_transitive() -> None:
    layers = [
        Layer("a", (0, 0, 0, 1)),
        Layer("b", (0, 0, 0, 1), caused_by="a"),
        Layer("c", (0, 0, 0, 1), caused_by="b"),
        Layer("d", (0, 0, 0, 1)),  # independiente
    ]
    assert affected_by(layers, "a") == {"a", "b", "c"}
    assert "d" not in affected_by(layers, "a")


def test_lossless_reconstruction_verifies_composition() -> None:
    layers = [Layer("bg", (0.0, 0.0, 0.0, 1.0)), Layer("fg", (1.0, 1.0, 1.0, 0.5))]
    expected = composite(layers)
    assert reconstruct_lossless(layers, expected) is True
    # Un resultado distinto NO se valida como lossless.
    assert reconstruct_lossless(layers, (0.0, 0.0, 0.0, 1.0)) is False


def test_edit_nonexistent_layer_rejected() -> None:
    with pytest.raises(LayerError, match="inexistente"):
        edit_causal([Layer("a", (0, 0, 0, 1))], "no_existe", (0, 0, 0, 1))


def test_matte_prefers_local_source() -> None:
    """A17: VOID no depende obligatoriamente de cloud; se prefiere local."""
    sources = [
        MatteSource(name="void_cloud", kind="cloud", available=True),
        MatteSource(name="sam_local", kind="local", available=True),
    ]
    chosen = resolve_matte_source(sources)
    assert chosen.name == "sam_local" and chosen.is_local()


def test_matte_falls_to_cloud_only_if_no_local() -> None:
    sources = [MatteSource(name="void_cloud", kind="cloud", available=True)]
    assert resolve_matte_source(sources).name == "void_cloud"


def test_matte_none_available_raises_not_fakes() -> None:
    sources = [MatteSource(name="x", kind="local", available=False)]
    with pytest.raises(LayerError, match="ninguna fuente"):
        resolve_matte_source(sources)
