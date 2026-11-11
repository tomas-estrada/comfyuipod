"""Contrato de activos 3D con procedencia (plan Ultra, W10 / A12).

Prueban el contrato (transforms/units/procedencia), no la geometría ni los
kernels CUDA. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.assets3d import (
    Asset3D,
    Asset3DError,
    CameraPose,
    Material,
    Part,
    Provenance,
    assert_camera_not_presented_as_observed,
    assert_not_presented_as_observed,
)


def _asset(**kw) -> Asset3D:
    mats = [Material(name="pbr_base", provenance=Provenance.OBSERVED)]
    parts = [Part(part_id="p1", name="cuerpo", material="pbr_base", provenance=Provenance.OBSERVED)]
    defaults = {"asset_id": "a1", "name": "silla", "materials": mats, "parts": parts}
    defaults.update(kw)
    return Asset3D(**defaults)


def test_valid_asset_passes_validation() -> None:
    a = _asset(source_engine="trellis")
    a.validate()  # no lanza
    assert a.coverage_report()["fully_observed"] is True


def test_empty_asset_id_is_rejected() -> None:
    a = _asset(asset_id="")
    with pytest.raises(Asset3DError, match="asset_id"):
        a.validate()


def test_bad_units_rejected() -> None:
    a = _asset(units="furlongs")
    with pytest.raises(Asset3DError, match="units"):
        a.validate()


def test_transform_must_be_4x4() -> None:
    a = _asset(transform=((1.0, 0.0), (0.0, 1.0)))
    with pytest.raises(Asset3DError, match="4x4"):
        a.validate()


def test_part_referencing_missing_material_rejected() -> None:
    bad = Part(part_id="p", name="x", material="no_existe", provenance=Provenance.OBSERVED)
    a = _asset(parts=[bad])
    with pytest.raises(Asset3DError, match="material inexistente"):
        a.validate()


def test_invalid_pbr_values_and_bbox_are_rejected() -> None:
    bad_material = Material(name="bad", base_color_rgba=(0.8, 0.8, 0.8, 2.0))
    with pytest.raises(Asset3DError, match="base_color_rgba"):
        _asset(
            materials=[bad_material],
            parts=[
                Part(
                    part_id="p1",
                    name="cuerpo",
                    material="bad",
                    provenance=Provenance.OBSERVED,
                )
            ],
        ).validate()

    bad_part = Part(
        part_id="bad",
        name="bad bbox",
        material="pbr_base",
        provenance=Provenance.OBSERVED,
        bbox=(2.0, 0.0, 0.0, 1.0, 1.0, 1.0),
    )
    with pytest.raises(Asset3DError, match="bbox invertido"):
        _asset(parts=[bad_part]).validate()


def test_inferred_part_is_not_presentable_as_observed() -> None:
    """A12: una cara oculta inferida NO puede presentarse como observación."""
    mats = [Material(name="m", provenance=Provenance.OBSERVED)]
    parts = [
        Part(part_id="front", name="fachada", material="m", provenance=Provenance.OBSERVED),
        Part(part_id="back", name="cara_oculta", material="m", provenance=Provenance.INFERRED),
    ]
    a = Asset3D(asset_id="edificio", name="e", materials=mats, parts=parts)
    a.validate()
    # La parte observada se puede presentar como factual.
    assert_not_presented_as_observed(a, "front")  # no lanza
    # La inferida NO.
    with pytest.raises(Asset3DError, match="INFERIDA"):
        assert_not_presented_as_observed(a, "back")


def test_coverage_report_distinguishes_observed_from_inferred() -> None:
    mats = [Material(name="m", provenance=Provenance.OBSERVED)]
    parts = [
        Part(part_id="a", name="a", material="m", provenance=Provenance.OBSERVED),
        Part(part_id="b", name="b", material="m", provenance=Provenance.INFERRED),
        Part(part_id="c", name="c", material="m", provenance=Provenance.INFERRED),
    ]
    a = Asset3D(asset_id="x", name="x", materials=mats, parts=parts)
    rep = a.coverage_report()
    assert rep["parts_total"] == 3
    assert rep["parts_observed"] == 1
    assert rep["parts_inferred"] == 2
    assert rep["has_inferred_geometry"] is True
    assert rep["fully_observed"] is False


def test_inferred_camera_flags_inferred_geometry() -> None:
    a = _asset()
    a.cameras = [
        CameraPose(
            name="vista_sintetica",
            position=(0.0, 1.0, 5.0),
            target=(0.0, 0.0, 0.0),
            provenance=Provenance.INFERRED,
        )
    ]
    assert a.has_inferred_geometry() is True


def test_material_carries_explicit_alpha() -> None:
    """El alpha es explícito: no se asume opaco (invariante 'fijar alpha')."""
    m = Material(name="vidrio", base_color_rgba=(0.5, 0.7, 0.9, 0.3))
    assert m.base_color_rgba[3] == 0.3
    assert m.to_dict()["base_color_rgba"][3] == 0.3


def test_roundtrip_serialization() -> None:
    a = _asset(
        source_engine="partcrafter",
        cameras=[
            CameraPose(
                name="vista",
                position=(1.0, 2.0, 3.0),
                target=(0.0, 0.0, 0.0),
                provenance=Provenance.INFERRED,
            )
        ],
    )
    d = a.to_dict()
    assert d["units"] == "meters"
    assert d["transform"][0] == [1.0, 0.0, 0.0, 0.0]
    assert d["coverage"]["fully_observed"] is True
    assert d["source_engine"] == "partcrafter"
    reopened = Asset3D.from_dict(d)
    assert reopened.to_dict() == d
    assert reopened.has_inferred_geometry()


def test_reopen_rejects_invalid_part_material_reference() -> None:
    data = _asset().to_dict()
    data["parts"][0]["material"] = "missing"
    with pytest.raises(Asset3DError, match="material inexistente"):
        Asset3D.from_dict(data)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("forward_axis", [0.0, 0.0, -2.0], "unitario"),
        ("forward_axis", [0.0, 1.0, 0.0], "paralelo"),
        ("transform", [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 1, 0, 1]], "homogénea"),
    ],
)
def test_reopen_rejects_invalid_axes_and_transform(field, value, message) -> None:
    data = _asset().to_dict()
    data[field] = value
    with pytest.raises(Asset3DError, match=message):
        Asset3D.from_dict(data)


def test_inferred_camera_is_not_presentable_as_observed() -> None:
    asset = _asset(
        cameras=[
            CameraPose(
                name="vista_sintetica",
                position=(0.0, 1.0, 5.0),
                target=(0.0, 0.0, 0.0),
                provenance=Provenance.INFERRED,
            )
        ]
    )
    with pytest.raises(Asset3DError, match="INFERIDA"):
        assert_camera_not_presented_as_observed(asset, "vista_sintetica")
