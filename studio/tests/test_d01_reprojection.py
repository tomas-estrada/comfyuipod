"""Acceptance tests for the synthetic CPU-only D01 geometry evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

from comfy_studio.demos import (
    FIXTURE_DIR,
    DemoError,
    _check_d01_vehicle_cameras,
    _d01_measure_scene,
    _d01_parse_fixture,
    run_demo,
)


@pytest.fixture
def d01_fixture() -> dict:
    return json.loads((FIXTURE_DIR / "d01_vehicle_cameras.json").read_text())


def test_fixture_has_calibrated_train_and_heldout_views(d01_fixture: dict) -> None:
    data = d01_fixture
    assert data["schema"] == "comfy_studio.demo_fixture.d01.v2"
    landmark_ids = {item["id"] for item in data["landmarks"]}
    assert len(landmark_ids) == 6
    assert len(data["views"]) == 5
    assert [view["split"] for view in data["views"]].count("train") == 3
    assert [view["split"] for view in data["views"]].count("heldout") == 2
    for view in data["views"]:
        fx, fy, _cx, _cy = view["intrinsics"]
        assert fx > 0 and fy > 0
        assert set(view["observations"]) == landmark_ids
    assert len(data["occluders"]) == 2
    assert data["max_reprojection_rmse_px"] > 0


def test_fixture_reprojects_with_separate_heldout_aggregate(d01_fixture: dict) -> None:
    report = _check_d01_vehicle_cameras(d01_fixture)
    assert report["reprojection_rmse_below_fixture_limit"] is True
    assert report["evidence_scope"] == "synthetic_cpu_only"
    assert report["train_view_count"] == 3
    assert report["heldout_view_count"] == 2
    assert math.isclose(report["heldout_reprojection_rmse_px"], 0.000404, abs_tol=1e-6)
    assert report["occluded_ids"]

    measured = _d01_measure_scene(_d01_parse_fixture(d01_fixture))
    heldout = measured["heldout_aggregate"]
    assert heldout["view_count"] == 2
    assert heldout["landmarks_total"] == 12
    assert heldout["landmarks_visible"] < heldout["landmarks_total"]


def test_observation_perturbation_breaks_fixture_rmse(d01_fixture: dict) -> None:
    changed = copy.deepcopy(d01_fixture)
    train = next(view for view in changed["views"] if view["split"] == "train")
    landmark_id = sorted(train["observations"])[0]
    train["observations"][landmark_id][0] += 50.0
    train["observations"][landmark_id][1] -= 30.0

    report = _check_d01_vehicle_cameras(changed)
    assert report["reprojection_rmse_below_fixture_limit"] is False
    assert report["reprojection_rmse_px"] > changed["max_reprojection_rmse_px"]


def test_occluder_removal_changes_visibility_and_coverage(d01_fixture: dict) -> None:
    baseline = _d01_measure_scene(_d01_parse_fixture(d01_fixture))
    changed = copy.deepcopy(d01_fixture)
    changed["occluders"] = []
    unoccluded = _d01_measure_scene(_d01_parse_fixture(changed))

    assert baseline["occluded_ids"]
    assert unoccluded["occluded_ids"] == []
    assert (
        unoccluded["heldout_aggregate"]["landmarks_visible"]
        > baseline["heldout_aggregate"]["landmarks_visible"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["views"][0].__setitem__("intrinsics", [0, 800, 640, 360]),
        lambda data: data["views"][0].__setitem__("split", "validation"),
        lambda data: data["views"][0]["observations"].pop("hood_front"),
        lambda data: data["views"].__setitem__(
            slice(None), [view for view in data["views"] if view["split"] == "train"]
        ),
    ],
)
def test_malformed_calibration_or_observations_are_rejected(d01_fixture: dict, mutate) -> None:
    changed = copy.deepcopy(d01_fixture)
    mutate(changed)
    with pytest.raises(DemoError):
        _check_d01_vehicle_cameras(changed)


def test_run_demo_records_hashed_report_and_cpu_only_scope(tmp_path: Path) -> None:
    result = run_demo("D01", tmp_path)
    assert result["result"] == "cpu_fixture_checks_passed"
    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text())
    report_path = tmp_path / "D01" / "D01_reprojection_coverage.json"
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)

    artifact = next(item for item in manifest["artifacts"] if item["path"] == report_path.name)
    assert artifact["sha256"] == hashlib.sha256(report_bytes).hexdigest()
    assert artifact["bytes"] == len(report_bytes)
    assert report["evidence_scope"] == "synthetic_cpu_only"
    assert report["gpu_used"] is False
    assert report["heldout_aggregate"]["view_count"] == 2
    assert report["fixture_sha256"] == manifest["inputs"][0]["sha256"]
    assert manifest["checks"]["reprojection_rmse_below_fixture_limit"] is True
    assert manifest["checks"]["heldout_rmse_below_fixture_limit"] is True


def test_run_demo_resume_reuses_verified_report(tmp_path: Path) -> None:
    first = run_demo("D01", tmp_path)
    resumed = run_demo("D01", tmp_path, resume=True)

    assert resumed["resumed"] is True
    assert resumed["result"] == first["result"]
    assert resumed["artifacts"] == first["artifacts"]
