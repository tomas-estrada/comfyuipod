import pytest

from comfy_studio.staging_optimizer import generate_staging_candidates, score_rgba_render

BOX = {"min": [-1.0, -2.0, -3.0], "max": [1.0, 2.0, 3.0]}


@pytest.mark.parametrize("up_axis", ["y_up", "z_up"])
def test_candidates_are_deterministic_bounded_and_vary_view_and_light(up_axis):
    first = generate_staging_candidates(BOX, up_axis)
    assert first == generate_staging_candidates(BOX, up_axis)
    assert len(first) == 8
    assert {item["azimuth_degrees"] for item in first} == {0, 90, 180, 270}
    assert {item["key_light_side"] for item in first} == {"left", "right"}
    for candidate in first:
        assert candidate["camera_target"] == [0.0, 0.0, 0.0]
        assert 1 <= candidate["camera_fov_degrees"] <= 120
        assert len(candidate["camera_position"]) == 3
        assert 0 <= candidate["light_rig"]["key"]["intensity"] <= 4
        assert 0 <= candidate["light_rig"]["fill"]["intensity"] <= 4
        assert "AABB" in candidate["assumptions"]
    assert len(generate_staging_candidates(BOX, "z_up", max_candidates=3)) == 3


@pytest.mark.parametrize(
    "aabb,axis",
    [
        ({"min": [0, 0], "max": [1, 1]}, "y_up"),
        ({"min": [0, 0, 0], "max": [0, 0, 0]}, "y_up"),
        ({"min": [0, 0, 0], "max": [float("inf"), 1, 1]}, "z_up"),
        (BOX, "x_up"),
    ],
)
def test_candidate_generator_rejects_invalid_inputs(aabb, axis):
    with pytest.raises(ValueError):
        generate_staging_candidates(aabb, axis)


def test_rgba_score_reports_bounded_coverage_centering_and_luminance():
    transparent = [0.0, 0.0, 0.0, 0.0]
    white = [0.8, 0.6, 0.4, 1.0]
    image = [[transparent for _ in range(4)] for _ in range(4)]
    image[1][1] = white
    image[1][2] = white
    result = score_rgba_render(image)
    assert result["transparent_object_coverage"] == pytest.approx(2 / 16)
    assert result["centering"] > 0.8
    assert result["luminance_mean"] == pytest.approx(0.62808)
    assert result["luminance_contrast"] == pytest.approx(0.0)
    assert 0 <= result["score"] <= 1


@pytest.mark.parametrize(
    "image",
    [[], [[]], [[[0, 0, 0, 0]]], [[[float("nan"), 0, 0, 1]]], [[[0, 0, 0, 2]]], [[[0, 0, 0]]]],
)
def test_rgba_scorer_rejects_empty_or_invalid_renders(image):
    with pytest.raises(ValueError):
        score_rgba_render(image)


def test_candidate_generator_accepts_planar_geometry():
    planar = {"min": [-1.0, 0.0, -2.0], "max": [1.0, 0.0, 2.0]}
    assert len(generate_staging_candidates(planar, "y_up")) == 8
