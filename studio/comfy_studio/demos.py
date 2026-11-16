"""Reproducible D01–D05 evaluation catalog and CPU evidence runner.

The runner executes only declared CPU checks. Neural stages remain pending GPU;
CPU fixture consistency and a synthetic FFmpeg encode are not capability or
quality evidence. Every run writes input hashes, tool versions, stage results,
and artifact hashes to a JSON manifest under the caller-selected output root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import wave
import zlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .media_bins import find_ffmpeg, find_ffprobe

FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "demos"


class EvidenceLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class StageStatus(StrEnum):
    READY_CPU = "ready_cpu"
    PENDING_GPU = "pending_gpu"
    DONE = "done"


_GPU_PENDING_REASON = "requiere hardware GPU objetivo y pesos disponibles; no ejecutado en CPU"


class DemoError(ValueError):
    """Invalid demo definition or failed CPU fixture check."""


@dataclass(slots=True)
class DemoStage:
    name: str
    level: EvidenceLevel
    status: StageStatus
    neural: bool = False
    pending_reason: str | None = None

    def validate(self) -> None:
        if self.status is StageStatus.PENDING_GPU and not self.pending_reason:
            raise DemoError(f"paso {self.name} pendiente sin motivo explícito")
        if self.neural and self.status is StageStatus.DONE and self.level not in (
            EvidenceLevel.L3,
            EvidenceLevel.L4,
        ):
            raise DemoError(f"paso neural {self.name} marcado done sin evidencia GPU (L3/L4)")


@dataclass(slots=True)
class Demo:
    demo_id: str
    title: str
    inputs: list[str]
    expected: list[str]
    splits: list[str]
    budget_runs: int
    stages: list[DemoStage] = field(default_factory=list)

    def validate(self) -> None:
        if not self.inputs or not self.expected or not self.splits:
            raise DemoError(f"{self.demo_id}: manifest sin inputs/esperados/splits")
        if self.budget_runs <= 0:
            raise DemoError(f"{self.demo_id}: budget_runs debe ser positivo")
        for stage in self.stages:
            stage.validate()

    def cpu_ready(self) -> list[DemoStage]:
        return [s for s in self.stages if s.status is StageStatus.READY_CPU]

    def gpu_pending(self) -> list[DemoStage]:
        return [s for s in self.stages if s.status is StageStatus.PENDING_GPU]

    def to_dict(self) -> dict[str, Any]:
        return {
            "demo_id": self.demo_id,
            "title": self.title,
            "inputs": list(self.inputs),
            "expected": list(self.expected),
            "splits": list(self.splits),
            "budget_runs": self.budget_runs,
            "stages": [
                {
                    "name": s.name,
                    "level": str(s.level),
                    "status": str(s.status),
                    "neural": s.neural,
                    "pending_reason": s.pending_reason,
                }
                for s in self.stages
            ],
            "cpu_ready": len(self.cpu_ready()),
            "gpu_pending": len(self.gpu_pending()),
        }


def _stage(name: str, neural: bool) -> DemoStage:
    return DemoStage(
        name=name,
        level=EvidenceLevel.L2 if neural else EvidenceLevel.L1,
        status=StageStatus.PENDING_GPU if neural else StageStatus.READY_CPU,
        neural=neural,
        pending_reason=_GPU_PENDING_REASON if neural else None,
    )


def demo_catalog() -> list[Demo]:
    """Five scenarios from the Ultra plan; neural work is never implied done."""
    return [
        Demo("D01", "Coche en escena de fixture, varias cámaras/luces/sonido",
             ["fixtures/d01_vehicle_cameras.json"],
             ["reprojection_rmse_below_fixture_limit", "logo_mask_unchanged"],
             ["heldout_views"], 3,
             [_stage("reconstruccion_guias", False), _stage("generacion", True),
              _stage("relight", True), _stage("foley", False), _stage("finishing_4k", True)]),
        Demo("D02", "Eliminar una causa y sus consecuencias (omnimatte)",
             ["fixtures/d02_causal_layers.json"],
             ["actor_removed", "shadow_removed", "reflection_removed",
              "intact_pixels_invariant", "actor_sound_removed",
              "ambient_sound_preserved"],
             ["actor_on_off"], 2,
             [_stage("matting", True), _stage("causal_delete", False)]),
        Demo("D03", "Producto articulado con cambio de material y varias cámaras",
             ["fixtures/d03_articulated_product.json"],
             ["joint_graph_valid", "heldout_views_present"], ["heldout_multivista"], 3,
             [_stage("reconstruccion_partes", True), _stage("mitsuba_ajuste", False),
              _stage("transferencia_neural", True)]),
        Demo("D04", "Actor hablante con identidad persistente y voz español",
             ["fixtures/d04_spanish_actor.json"],
             ["identity_reference_consistent", "word_timing_valid"], ["shots_multiples"], 3,
             [_stage("voz_tts", True), _stage("lipsync", True),
              _stage("identidad_check", False)]),
        Demo("D05", "Montaje desde brief a reel 4K editable con créditos",
             ["fixtures/d05_editable_delivery.json"],
             ["4k_dimensions_verified", "credits_and_project_manifest"],
             ["aspectos_16_9_9_16"], 2,
             [_stage("montaje", False), _stage("generacion_clips", True),
              _stage("entrega_4k_cpu_encode", False)]),
    ]


def assert_no_fake_gpu_result(demo: Demo) -> None:
    for stage in demo.stages:
        if stage.neural and stage.status is StageStatus.DONE and stage.level not in (
            EvidenceLevel.L3,
            EvidenceLevel.L4,
        ):
            raise DemoError(
                f"{demo.demo_id}/{stage.name}: render futuro marcado como resultado existente"
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_fixture(demo: Demo) -> tuple[Path, dict[str, Any]]:
    relative = demo.inputs[0].removeprefix("fixtures/")
    path = (FIXTURE_DIR / relative).resolve()
    if path.parent != FIXTURE_DIR.resolve() or not path.is_file():
        raise DemoError(f"fixture inexistente o fuera del catálogo: {relative}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoError(f"fixture inválido {relative}: {exc}") from exc
    return path, data


def clean_demo_output(demo_id: str, output_root: Path) -> bool:
    """Remove only the selected demo directory under the selected output root."""
    if demo_id not in {demo.demo_id for demo in demo_catalog()}:
        raise DemoError(f"demo desconocida: {demo_id}")
    root = output_root.expanduser().resolve()
    target = root / demo_id
    if target.is_symlink():
        raise DemoError("refusing to clean a demo output symlink")
    if target.exists():
        if target.resolve().parent != root or target.resolve() == root:
            raise DemoError("demo output directory escapes output root")
        if not target.is_dir():
            raise DemoError("demo output path exists and is not a directory")
        shutil.rmtree(target)
        return True
    return False


def _prepare_demo_output(demo_id: str, output_root: Path) -> tuple[Path, Path]:
    """Create a contained output directory and reject aliased run files."""
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    out_dir = root / demo_id
    if out_dir.is_symlink():
        raise DemoError("refusing to write into a demo output symlink")
    out_dir.mkdir(exist_ok=True)
    if not out_dir.is_dir() or out_dir.resolve().parent != root:
        raise DemoError("demo output directory escapes output root")

    manifest = out_dir / "run_manifest.json"
    generated = [manifest, out_dir / "editable_project.json"]
    if demo_id == "D01":
        generated.append(out_dir / _D01_REPORT_ARTIFACT)
    if demo_id == "D02":
        generated.extend(out_dir / name for name in _D02_ALL_ARTIFACTS)
    if demo_id == "D05":
        generated.extend(out_dir / name for name in (
            "D05_delivery_package.zip", "D05_master_16x9.mp4", "D05_master_9x16.mp4"))
        generated.append(out_dir / "D05_asset_inventory.json")
    for path in generated:
        if path.is_symlink():
            raise DemoError("refusing to overwrite a demo output symlink")
        try:
            file_stat = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            raise DemoError("refusing to overwrite a non-file demo output path")
        if file_stat.st_nlink > 1:
            raise DemoError("refusing to overwrite a hard-linked output file")
    return out_dir, manifest


def _execution_fingerprint(demo_id: str) -> dict[str, Any]:
    """Identify the runner and external tools used to produce resumable evidence."""
    fingerprint: dict[str, Any] = {
        "runner_sha256": _sha256(Path(__file__)),
        "python_version": sys.version.split()[0],
        "tool_versions": {},
    }
    if demo_id != "D05":
        return fingerprint

    for name, executable in (("ffmpeg", find_ffmpeg()), ("ffprobe", find_ffprobe())):
        try:
            result = subprocess.run(
                [executable, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DemoError(f"unable to identify {name} version: {exc}") from exc
        first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
        if result.returncode or not first_line:
            raise DemoError(f"unable to identify {name} version")
        fingerprint["tool_versions"][name] = first_line
    return fingerprint


def _expected_artifact_names(demo_id: str) -> set[str]:
    if demo_id == "D01":
        return {_D01_REPORT_ARTIFACT}
    if demo_id == "D02":
        return set(_D02_ALL_ARTIFACTS)
    if demo_id == "D05":
        return {
            "D05_delivery_package.zip",
            "D05_master_16x9.mp4",
            "D05_master_9x16.mp4",
            "D05_asset_inventory.json",
            "editable_project.json",
        }
    return set()


def _check_fixture(demo_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Run small, explicit fixture integrity checks; these do not validate ML."""
    if demo_id == "D02":
        return _check_d02_causal_layers(data)
    if demo_id == "D01":
        return _check_d01_vehicle_cameras(data)
    if demo_id == "D03":
        parts = {part["id"] for part in data["parts"]}
        joints = data["joints"]
        valid = all(j["parent"] in parts and j["child"] in parts and j["parent"] != j["child"]
                    for j in joints)
        return {"joint_graph_valid": valid, "parts": len(parts),
                "heldout_views_present": bool(data["heldout_views"]),
                "heldout_view_count": len(data["heldout_views"])}
    if demo_id == "D04":
        refs = data["identity_reference_ids"]
        consistent = len(refs) >= 2 and len(set(refs)) == 1
        timings = data["word_timings"]
        valid = bool(timings) and all(t["start_s"] >= 0 and t["end_s"] > t["start_s"]
                                     for t in timings)
        ordered = all(
            a["end_s"] <= b["start_s"]
            for a, b in zip(timings, timings[1:], strict=False)
        )
        return {"identity_reference_consistent": consistent,
                "word_timing_valid": valid and ordered, "timed_words": len(timings),
                "locale": data["locale"]}
    if demo_id == "D05":
        credits = data["credits"]
        return {"editable_project_valid": bool(data["project_name"] and data["titles"]),
                "credits_and_project_manifest": bool(credits) and all(
                    row.get("license") and row.get("source") for row in credits
                ), "render": data["render"]}
    raise DemoError(f"demo desconocida: {demo_id}")


_D01_REPORT_ARTIFACT = "D01_reprojection_coverage.json"
_D01_FIXTURE_SCHEMA = "comfy_studio.demo_fixture.d01.v2"
_D01_REPORT_SCHEMA = "comfy_studio.d01_reprojection_coverage.v1"
_D01_WORLD_UP = (0.0, 0.0, 1.0)
_D01_EVIDENCE_SCOPE = "synthetic_cpu_only"


def _d01_finite_list(value: Any, length: int, name: str) -> list[float]:
    """Strict finite-number list: exact length, no bool/int coercion surprises."""
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(
            isinstance(component, bool)
            or not isinstance(component, (int, float))
            or not math.isfinite(component)
            for component in value
        )
    ):
        raise DemoError(f"D01: {name} malformado")
    return [float(component) for component in value]


def _d01_sub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _d01_cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _d01_dot(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _d01_normalize(
    a: tuple[float, float, float], name: str
) -> tuple[float, float, float]:
    norm = math.sqrt(_d01_dot(a, a))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise DemoError(f"D01: vector {name} degenerado")
    return (a[0] / norm, a[1] / norm, a[2] / norm)


def _d01_camera_basis(
    position: tuple[float, float, float], look_at: tuple[float, float, float]
) -> tuple[tuple[float, float, float], ...]:
    """Deterministic look-at basis: right = forward x world_up, up = right x forward."""
    forward = _d01_normalize(_d01_sub(look_at, position), "forward")
    right = _d01_normalize(_d01_cross(forward, _D01_WORLD_UP), "right")
    up = _d01_cross(right, forward)
    return (right, up, forward)


def _d01_project(
    point: tuple[float, float, float],
    position: tuple[float, float, float],
    basis: tuple[tuple[float, float, float], ...],
    intrinsics: tuple[float, float, float, float],
) -> tuple[float, float, float] | None:
    """Pinhole projection (u, v, depth); None when the point is behind the camera."""
    right, up, forward = basis
    delta = _d01_sub(point, position)
    depth = _d01_dot(delta, forward)
    if depth <= 1e-9:
        return None
    fx, fy, cx, cy = intrinsics
    u = fx * _d01_dot(delta, right) / depth + cx
    v = fy * _d01_dot(delta, up) / depth + cy
    return (u, v, depth)


def _d01_ray_hits_box(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    box_min: tuple[float, float, float],
    box_max: tuple[float, float, float],
) -> bool:
    """Segment (origin, origin + direction) vs axis-aligned box via the slab method."""
    t_enter = 0.0
    t_exit = 1.0
    for axis in range(3):
        o = origin[axis]
        d = direction[axis]
        lo = box_min[axis]
        hi = box_max[axis]
        if abs(d) < 1e-12:
            if o < lo or o > hi:
                return False
            continue
        t0 = (lo - o) / d
        t1 = (hi - o) / d
        if t0 > t1:
            t0, t1 = t1, t0
        t_enter = max(t_enter, t0)
        t_exit = min(t_exit, t1)
        if t_enter > t_exit:
            return False
    return True


@dataclass(slots=True)
class _D01Landmark:
    landmark_id: str
    xyz: tuple[float, float, float]


@dataclass(slots=True)
class _D01Occluder:
    occluder_id: str
    box_min: tuple[float, float, float]
    box_max: tuple[float, float, float]


@dataclass(slots=True)
class _D01View:
    view_id: str
    split: str
    position: tuple[float, float, float]
    look_at: tuple[float, float, float]
    intrinsics: tuple[float, float, float, float]
    image_size: tuple[int, int]
    observations: dict[str, tuple[float, float]]


@dataclass(slots=True)
class _D01Scene:
    landmarks: list[_D01Landmark]
    views: list[_D01View]
    occluders: list[_D01Occluder]
    max_rmse_px: float
    logo_mask_unchanged: bool


def _d01_intrinsics(value: Any, view_id: str) -> tuple[float, float, float, float]:
    """Strict calibration: exactly four finite numbers with positive focal lengths."""
    components = _d01_finite_list(value, 4, f"calibración de {view_id}")
    fx, fy, cx, cy = components
    if fx <= 0.0 or fy <= 0.0:
        raise DemoError(f"D01: calibración de {view_id} con foco no positivo")
    return (fx, fy, cx, cy)


def _d01_parse_fixture(data: dict[str, Any]) -> _D01Scene:
    """Validate the strict D01 scene; malformed input raises DemoError."""
    if not isinstance(data, dict) or data.get("schema") != _D01_FIXTURE_SCHEMA:
        raise DemoError("D01: esquema de fixture no reconocido")
    limit = data.get("max_reprojection_rmse_px")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(limit)
        or limit <= 0
    ):
        raise DemoError("D01: límite de RMSE malformado")

    raw_landmarks = data.get("landmarks")
    if not isinstance(raw_landmarks, list) or not raw_landmarks:
        raise DemoError("D01: landmarks ausentes o vacíos")
    landmarks: list[_D01Landmark] = []
    seen_landmarks: set[str] = set()
    for landmark in raw_landmarks:
        if not isinstance(landmark, dict):
            raise DemoError("D01: hito malformado")
        landmark_id = landmark.get("id")
        if (
            not isinstance(landmark_id, str)
            or not landmark_id
            or landmark_id in seen_landmarks
        ):
            raise DemoError(f"D01: hito malformado: {landmark_id!r}")
        seen_landmarks.add(landmark_id)
        xyz = _d01_finite_list(landmark.get("xyz"), 3, f"hito {landmark_id} xyz")
        landmarks.append(_D01Landmark(landmark_id, (xyz[0], xyz[1], xyz[2])))

    raw_views = data.get("views")
    if not isinstance(raw_views, list) or not raw_views:
        raise DemoError("D01: vistas ausentes o vacías")
    views: list[_D01View] = []
    seen_views: set[str] = set()
    for view in raw_views:
        if not isinstance(view, dict):
            raise DemoError("D01: vista malformada")
        view_id = view.get("id")
        if not isinstance(view_id, str) or not view_id or view_id in seen_views:
            raise DemoError(f"D01: vista malformada: {view_id!r}")
        seen_views.add(view_id)
        split = view.get("split")
        if split not in ("train", "heldout"):
            raise DemoError(f"D01: vista {view_id} con split inválido")
        position = tuple(
            _d01_finite_list(view.get("position"), 3, f"vista {view_id} position")
        )
        look_at = tuple(
            _d01_finite_list(view.get("look_at"), 3, f"vista {view_id} look_at")
        )
        if position == look_at:
            raise DemoError(f"D01: vista {view_id} con posición igual al objetivo")
        intrinsics = _d01_intrinsics(view.get("intrinsics"), view_id)
        raw_size = view.get("image_size")
        if (
            not isinstance(raw_size, list)
            or len(raw_size) != 2
            or any(
                isinstance(side, bool) or not isinstance(side, int) or side <= 0
                for side in raw_size
            )
        ):
            raise DemoError(f"D01: vista {view_id} con image_size malformado")
        raw_observations = view.get("observations")
        if not isinstance(raw_observations, dict):
            raise DemoError(f"D01: vista {view_id} sin observaciones")
        if set(raw_observations) != {lm.landmark_id for lm in landmarks}:
            raise DemoError(f"D01: vista {view_id} no observa exactamente todos los hitos")
        observations: dict[str, tuple[float, float]] = {}
        for landmark_id, uv in raw_observations.items():
            parsed_uv = _d01_finite_list(
                uv, 2, f"vista {view_id} observación {landmark_id}"
            )
            observations[landmark_id] = (parsed_uv[0], parsed_uv[1])
        views.append(
            _D01View(
                view_id,
                split,
                position,  # type: ignore[arg-type]
                look_at,  # type: ignore[arg-type]
                intrinsics,
                (raw_size[0], raw_size[1]),
                observations,
            )
        )
    if not any(view.split == "train" for view in views):
        raise DemoError("D01: falta al menos una vista train")
    if not any(view.split == "heldout" for view in views):
        raise DemoError("D01: falta al menos una vista heldout")

    raw_occluders = data.get("occluders")
    if not isinstance(raw_occluders, list):
        raise DemoError("D01: occluders malformado")
    occluders: list[_D01Occluder] = []
    seen_occluders: set[str] = set()
    for occluder in raw_occluders:
        if not isinstance(occluder, dict):
            raise DemoError("D01: oclusor malformado")
        occluder_id = occluder.get("id")
        if (
            not isinstance(occluder_id, str)
            or not occluder_id
            or occluder_id in seen_occluders
        ):
            raise DemoError(f"D01: oclusor malformado: {occluder_id!r}")
        seen_occluders.add(occluder_id)
        box_min = tuple(_d01_finite_list(occluder.get("min"), 3, f"oclusor {occluder_id} min"))
        box_max = tuple(_d01_finite_list(occluder.get("max"), 3, f"oclusor {occluder_id} max"))
        if any(lo >= hi for lo, hi in zip(box_min, box_max, strict=True)):
            raise DemoError(f"D01: oclusor {occluder_id} con caja degenerada")
        occluders.append(_D01Occluder(occluder_id, box_min, box_max))  # type: ignore[arg-type]

    before = data.get("logo_mask_before")
    after = data.get("logo_mask_after")
    for mask in (before, after):
        if (
            not isinstance(mask, list)
            or not mask
            or any(isinstance(pixel, bool) or not isinstance(pixel, int) for pixel in mask)
        ):
            raise DemoError("D01: logo mask malformado")
    logo_mask_unchanged = bool(before == after)

    return _D01Scene(
        landmarks=landmarks,
        views=views,
        occluders=occluders,
        max_rmse_px=float(limit),
        logo_mask_unchanged=logo_mask_unchanged,
    )


def _d01_measure_scene(scene: _D01Scene) -> dict[str, Any]:
    """Project every landmark in every view and measure errors and visibility.

    Observed pixels come from the fixture; projected pixels are recomputed from
    the fixture calibrations (position, look-at route, intrinsics). Visibility
    is ray/AABB occlusion plus image-bounds membership; it is never stored in
    the fixture. Held-out views are aggregated separately.
    """
    split_errors: dict[str, list[float]] = {"train": [], "heldout": []}
    views_report: dict[str, dict[str, Any]] = {}
    occluded_pairs: list[str] = []
    for view in scene.views:
        basis = _d01_camera_basis(view.position, view.look_at)
        width, height = view.image_size
        visible = 0
        landmark_report: dict[str, dict[str, Any]] = {}
        for landmark in scene.landmarks:
            projected = _d01_project(landmark.xyz, view.position, basis, view.intrinsics)
            if projected is None:
                raise DemoError(
                    f"D01: hito {landmark.landmark_id} detrás de la cámara {view.view_id}"
                )
            u, v, _depth = projected
            direction = _d01_sub(landmark.xyz, view.position)
            occluded = any(
                _d01_ray_hits_box(
                    view.position, direction, occluder.box_min, occluder.box_max
                )
                for occluder in scene.occluders
            )
            in_bounds = 0.0 <= u < width and 0.0 <= v < height
            observed = view.observations[landmark.landmark_id]
            error_px = math.hypot(u - observed[0], v - observed[1])
            if occluded:
                occluded_pairs.append(f"{view.view_id}/{landmark.landmark_id}")
            if not occluded and in_bounds:
                visible += 1
            split_errors[view.split].append(error_px)
            landmark_report[landmark.landmark_id] = {
                "observed_uv": [round(observed[0], 6), round(observed[1], 6)],
                "projected_uv": [round(u, 6), round(v, 6)],
                "error_px": round(error_px, 6),
                "occluded": occluded,
                "in_bounds": in_bounds,
            }
        views_report[view.view_id] = {
            "split": view.split,
            "visible_landmarks": visible,
            "coverage": round(visible / len(scene.landmarks), 6),
            "landmarks": landmark_report,
        }

    def rmse(errors: list[float]) -> float:
        if not errors:
            raise DemoError("D01: sin observaciones para el RMSE")
        return math.sqrt(sum(err * err for err in errors) / len(errors))

    heldout_views = [view for view in scene.views if view.split == "heldout"]
    heldout_coverage_mean = round(
        sum(views_report[view.view_id]["coverage"] for view in heldout_views)
        / len(heldout_views),
        6,
    )
    return {
        "views": views_report,
        "rmse_px": {
            "train": round(rmse(split_errors["train"]), 6),
            "heldout": round(rmse(split_errors["heldout"]), 6),
            "overall": round(
                rmse(split_errors["train"] + split_errors["heldout"]), 6
            ),
        },
        "coverage_by_view": {
            view_id: detail["coverage"] for view_id, detail in views_report.items()
        },
        "occluded_ids": sorted(occluded_pairs),
        "heldout_aggregate": {
            "view_count": len(heldout_views),
            "rmse_px": round(rmse(split_errors["heldout"]), 6),
            "coverage_mean": heldout_coverage_mean,
            "landmarks_visible": sum(
                views_report[view.view_id]["visible_landmarks"]
                for view in heldout_views
            ),
            "landmarks_total": len(heldout_views) * len(scene.landmarks),
        },
    }


def _check_d01_vehicle_cameras(data: dict[str, Any]) -> dict[str, Any]:
    """D01 scene-to-report contract over the synthetic CPU fixture.

    Projects the six 3D landmarks through every calibrated view of the explicit
    position/look-at route, measures reprojection RMSE against the observed
    pixels, and derives ray/AABB visibility against the box occluders.
    Held-out views are aggregated separately. This is synthetic CPU-only
    evidence: no real camera calibration, no GPU, no neural-quality claim.
    """
    scene = _d01_parse_fixture(data)
    measurement = _d01_measure_scene(scene)
    limit = scene.max_rmse_px
    heldout = measurement["heldout_aggregate"]
    return {
        "evidence_scope": _D01_EVIDENCE_SCOPE,
        "reprojection_rmse_px": measurement["rmse_px"]["overall"],
        "train_reprojection_rmse_px": measurement["rmse_px"]["train"],
        "heldout_reprojection_rmse_px": measurement["rmse_px"]["heldout"],
        "fixture_limit_px": limit,
        "reprojection_rmse_below_fixture_limit": (
            measurement["rmse_px"]["overall"] <= limit
        ),
        "heldout_rmse_below_fixture_limit": measurement["rmse_px"]["heldout"] <= limit,
        "logo_mask_unchanged": scene.logo_mask_unchanged,
        "landmark_count": len(scene.landmarks),
        "train_view_count": sum(1 for view in scene.views if view.split == "train"),
        "heldout_view_count": heldout["view_count"],
        "occluder_count": len(scene.occluders),
        "coverage_by_view": measurement["coverage_by_view"],
        "heldout_coverage_mean": heldout["coverage_mean"],
        "heldout_landmarks_visible": heldout["landmarks_visible"],
        "occluded_ids": measurement["occluded_ids"],
    }


def _write_d01_reprojection_report(
    fixture: Path, data: dict[str, Any], out_dir: Path
) -> Path:
    """Write the D01 reprojection/coverage report (synthetic CPU evidence only).

    Contents: fixture SHA-256, per-view route/calibration, observed vs
    projected pixels with per-landmark errors, coverage, occluded landmark IDs,
    train/held-out RMSE aggregates, explicit ``evidence_scope:
    synthetic_cpu_only`` and ``gpu_used: false``. No GPU or model-quality claim.
    """
    scene = _d01_parse_fixture(data)
    measurement = _d01_measure_scene(scene)
    report = {
        "schema": _D01_REPORT_SCHEMA,
        "evidence_scope": _D01_EVIDENCE_SCOPE,
        "gpu_used": False,
        "scope": (
            "synthetic CPU reprojection and visibility evidence only: fixture "
            "calibration consistency; no real camera calibration, no GPU, no "
            "neural reconstruction quality claim"
        ),
        "fixture_sha256": _sha256(fixture),
        "fixture_limit_px": scene.max_rmse_px,
        "landmarks": [landmark.landmark_id for landmark in scene.landmarks],
        "occluders": [occluder.occluder_id for occluder in scene.occluders],
        "views": [
            {
                "id": view.view_id,
                "split": view.split,
                "position": list(view.position),
                "look_at": list(view.look_at),
                "intrinsics": list(view.intrinsics),
                "image_size": list(view.image_size),
                "coverage": measurement["views"][view.view_id]["coverage"],
                "visible_landmarks": (
                    measurement["views"][view.view_id]["visible_landmarks"]
                ),
                "landmarks": measurement["views"][view.view_id]["landmarks"],
            }
            for view in scene.views
        ],
        "rmse_px": measurement["rmse_px"],
        "coverage_by_view": measurement["coverage_by_view"],
        "heldout_aggregate": measurement["heldout_aggregate"],
        "occluded_ids": measurement["occluded_ids"],
        "logo_mask_unchanged": scene.logo_mask_unchanged,
    }
    path = out_dir / _D01_REPORT_ARTIFACT
    path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


_D02_REGIONS = ("actor_region", "shadow_region", "reflection_region", "intact_region")
_D02_MEDIA_FILES = (
    "D02_actor_before.png",
    "D02_actor_after.png",
    "D02_audio_before.wav",
    "D02_audio_after.wav",
)
_D02_MEASUREMENT_ARTIFACT = "D02_causal_layer_measurement.json"
_D02_ALL_ARTIFACTS = (*_D02_MEDIA_FILES, _D02_MEASUREMENT_ARTIFACT)


def _d02_buffer(buffer: Any, pixels: int, name: str) -> list[int]:
    """Strict D02 layer buffer: exact length, 8-bit ints, no bool/int coercion."""
    if (
        not isinstance(buffer, list)
        or len(buffer) != pixels
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 255
            for value in buffer
        )
    ):
        raise DemoError(f"D02: buffer {name} malformado")
    return buffer


def _d02_region(data: dict[str, Any], pixels: int, name: str) -> list[bool]:
    """Strict D02 region mask: exact length, real booleans, non-empty."""
    region = data.get(name)
    if (
        not isinstance(region, list)
        or len(region) != pixels
        or not all(isinstance(value, bool) for value in region)
        or not any(region)
    ):
        raise DemoError(f"D02: región {name} malformada")
    return region


@dataclass(slots=True)
class _D02Scene:
    """Parsed D02 scene: strict buffers, disjoint regions, typed sound events."""

    pixels: int
    frame_width: int
    frame_height: int
    sample_rate_hz: int
    duration_s: float
    background: list[int]
    before: list[int]
    after: list[int]
    regions: dict[str, list[bool]]
    events: list[tuple[str, str, bool, bool, int, int, float, float]]


def _d02_parse_scene(data: dict[str, Any]) -> _D02Scene:
    """Validate the strict D02 scene structure; malformed input raises DemoError."""
    pixels = data.get("pixels")
    if not isinstance(pixels, int) or isinstance(pixels, bool) or pixels <= 0:
        raise DemoError("D02: contador de píxeles inválido")
    frame_width = data.get("frame_width")
    frame_height = data.get("frame_height")
    if (
        not isinstance(frame_width, int)
        or isinstance(frame_width, bool)
        or frame_width <= 0
        or not isinstance(frame_height, int)
        or isinstance(frame_height, bool)
        or frame_height <= 0
        or frame_width * frame_height != pixels
    ):
        raise DemoError("D02: dimensiones de imagen inválidas")
    audio = data.get("audio")
    if not isinstance(audio, dict):
        raise DemoError("D02: configuración de audio inválida")
    sample_rate_hz = audio.get("sample_rate_hz")
    duration_s = audio.get("duration_s")
    if (
        not isinstance(sample_rate_hz, int)
        or isinstance(sample_rate_hz, bool)
        or not 8000 <= sample_rate_hz <= 48000
        or isinstance(duration_s, bool)
        or not isinstance(duration_s, (int, float))
        or not math.isfinite(duration_s)
        or not 0 < duration_s <= 30
    ):
        raise DemoError("D02: parámetros de audio inválidos")
    if duration_s * sample_rate_hz > 250_000:
        raise DemoError("D02: duración excede el límite de muestras CPU")
    background = _d02_buffer(data.get("background"), pixels, "background")
    before = _d02_buffer(data.get("before"), pixels, "before")
    after = _d02_buffer(data.get("after"), pixels, "after")

    regions = {name: _d02_region(data, pixels, name) for name in _D02_REGIONS}
    for i in range(pixels):
        if sum(region[i] for region in regions.values()) > 1:
            raise DemoError(f"D02: regiones solapadas en el píxel {i}")
    for name in ("actor_region", "shadow_region", "reflection_region"):
        if all(before[i] == background[i] for i, flag in enumerate(regions[name]) if flag):
            raise DemoError(f"D02: la región {name} no oculta nada en before")

    raw_events = data.get("sound_events")
    if not isinstance(raw_events, list) or not 1 <= len(raw_events) <= 128:
        raise DemoError("D02: sound_events ausente o vacío")
    parsed: list[tuple[str, str, bool, bool, int, int, float, float]] = []
    seen_ids: set[str] = set()
    for event in raw_events:
        if not isinstance(event, dict):
            raise DemoError("D02: evento de sonido malformado")
        event_id = event.get("id")
        cause = event.get("caused_by")
        presence = (event.get("present_before"), event.get("present_after"))
        start_s = event.get("start_s")
        end_s = event.get("end_s")
        frequency_hz = event.get("frequency_hz")
        amplitude = event.get("amplitude")
        timing_valid = not any(isinstance(t, bool) for t in (start_s, end_s)) and all(
            isinstance(t, (int, float)) and math.isfinite(t) for t in (start_s, end_s)
        )
        waveform_valid = (
            isinstance(frequency_hz, int)
            and not isinstance(frequency_hz, bool)
            and 1 <= frequency_hz < sample_rate_hz // 2
            and isinstance(amplitude, int)
            and not isinstance(amplitude, bool)
            and 1 <= amplitude <= 32767
        )
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in seen_ids
            or cause not in ("actor", "ambient")
            or not all(isinstance(value, bool) for value in presence)
            or not timing_valid
            or not 0 <= start_s < end_s  # type: ignore[operator]
            or end_s > duration_s
            or not waveform_valid
        ):
            raise DemoError(f"D02: evento de sonido malformado: {event_id!r}")
        seen_ids.add(event_id)
        parsed.append((
            event_id,
            cause,
            presence[0],
            presence[1],
            frequency_hz,
            amplitude,
            float(start_s),
            float(end_s),
        ))

    if sum(event[5] for event in parsed) > 32767:
        raise DemoError("D02: amplitudes simultáneas exceden PCM de 16 bits")

    return _D02Scene(
        pixels=pixels,
        frame_width=frame_width,
        frame_height=frame_height,
        sample_rate_hz=sample_rate_hz,
        duration_s=float(duration_s),
        background=background,
        before=before,
        after=after,
        regions=regions,
        events=parsed,
    )


def _d02_measure_scene(scene: _D02Scene) -> dict[str, Any]:
    """Per-invariant error counts and totals over an already-parsed scene.

    Removal errors count removed-cause pixels whose `after` value deviates from
    the background; the intact deviation counts changed pixels in the intact
    region; sound errors count events that violate their expected presence.
    """
    background, before, after = scene.background, scene.before, scene.after
    regions = scene.regions
    removal_errors: dict[str, int] = {}
    for name in ("actor_region", "shadow_region", "reflection_region"):
        region = regions[name]
        removal_errors[name] = sum(
            1 for i, flag in enumerate(region) if flag and after[i] != background[i]
        )
    intact_region = regions["intact_region"]
    actor_events = [event for event in scene.events if event[1] == "actor"]
    ambient_events = [event for event in scene.events if event[1] == "ambient"]
    return {
        "removal_errors_by_region": removal_errors,
        "intact_deviation_errors": sum(
            1 for i, flag in enumerate(intact_region) if flag and before[i] != after[i]
        ),
        "actor_sound_errors": sum(
            1 for event in actor_events if not (event[2] and not event[3])
        ),
        "actor_sound_total": len(actor_events),
        "ambient_sound_errors": sum(
            1 for event in ambient_events if not (event[2] and event[3])
        ),
        "ambient_sound_total": len(ambient_events),
    }


def _d02_invariants(measurement: dict[str, Any]) -> dict[str, bool]:
    """Derive the six D02 booleans from structured per-check error counts."""
    removal = measurement["removal_errors_by_region"]
    return {
        "actor_removed": removal["actor_region"] == 0,
        "shadow_removed": removal["shadow_region"] == 0,
        "reflection_removed": removal["reflection_region"] == 0,
        "intact_pixels_invariant": measurement["intact_deviation_errors"] == 0,
        "actor_sound_removed": (
            measurement["actor_sound_total"] > 0 and measurement["actor_sound_errors"] == 0
        ),
        "ambient_sound_preserved": (
            measurement["ambient_sound_total"] > 0
            and measurement["ambient_sound_errors"] == 0
        ),
    }


def _d02_scene_measurement(data: dict[str, Any]) -> dict[str, Any]:
    """Structured measurement of one D02 scene: invariants, counts and errors."""
    scene = _d02_parse_scene(data)
    measurement = _d02_measure_scene(scene)
    return {
        "pixels_checked": scene.pixels,
        "sound_events_checked": len(scene.events),
        "invariants": _d02_invariants(measurement),
        "errors": measurement,
    }


def _check_d02_causal_layers(data: dict[str, Any]) -> dict[str, Any]:
    """Run D02's six separate causal-layer invariants over the synthetic fixture.

    Each invariant is an independent boolean: actor, shadow, and reflection
    removal (the deleted cause reveals the background), intact pixel invariance
    in the logo/prop region, actor-caused sound removal, and ambient sound
    preservation. Malformed buffers, regions, or sound events raise DemoError.
    The same strict validation also measures the deterministic held-out scene
    written to the D02 measurement report. All evidence is synthetic CPU
    fixture consistency; it is not omnimatte or model-quality evidence.
    """
    checks = _d02_scene_measurement(data)
    result: dict[str, Any] = dict(checks["invariants"])
    result["pixels_checked"] = checks["pixels_checked"]
    result["sound_events_checked"] = checks["sound_events_checked"]
    return result


_D02_HELDOUT_SEED = 0x00D02CA1  # fixed in code: reproducible held-out scene
_D02_HELDOUT_PIXELS = 32


def _d02_heldout_data() -> dict[str, Any]:
    """Deterministic held-out synthetic scene: 32 values per buffer, CPU only.

    The seed drives an integer LCG so the scene is byte-identical on every
    run without depending on stdlib RNG stability. Disjoint actor/shadow/
    reflection/intact spans cover all 32 values; removed-cause pixels reveal
    the background in `after`, and intact pixels stay invariant. Four sound
    events: two actor-caused (removed) and two ambient (retained).
    """
    pixels = _D02_HELDOUT_PIXELS
    spans = {
        "actor_region": (0, 8),
        "shadow_region": (8, 16),
        "reflection_region": (16, 24),
        "intact_region": (24, pixels),
    }
    state = _D02_HELDOUT_SEED & 0xFFFFFFFFFFFFFFFF

    def noise(modulus: int) -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return (state >> 33) % modulus

    background = [noise(256) for _ in range(pixels)]
    occlusion = {
        name: 17 + noise(64)
        for name, (_start, _end) in spans.items()
        if name != "intact_region"
    }

    def cause_region(index: int) -> str | None:
        for name, (start, end) in spans.items():
            if name != "intact_region" and start <= index < end:
                return name
        return None

    before: list[int] = []
    after: list[int] = []
    for index in range(pixels):
        cause = cause_region(index)
        if cause is None:
            before.append(background[index])
            after.append(background[index])
        else:
            before.append((background[index] + occlusion[cause]) % 256)
            after.append(background[index])
    sound_events = [
        {"id": "heldout-actor-step", "caused_by": "actor",
         "present_before": True, "present_after": False,
         "start_s": 0.0, "end_s": 0.5, "frequency_hz": 660, "amplitude": 1200},
        {"id": "heldout-actor-vocal", "caused_by": "actor",
         "present_before": True, "present_after": False,
         "start_s": 0.5, "end_s": 1.0, "frequency_hz": 440, "amplitude": 1400},
        {"id": "heldout-ambient-wind", "caused_by": "ambient",
         "present_before": True, "present_after": True,
         "start_s": 0.0, "end_s": 1.0, "frequency_hz": 110, "amplitude": 1600},
        {"id": "heldout-ambient-birds", "caused_by": "ambient",
         "present_before": True, "present_after": True,
         "start_s": 0.25, "end_s": 0.75, "frequency_hz": 880, "amplitude": 900},
    ]
    return {
        "pixels": pixels,
        "frame_width": 8,
        "frame_height": 4,
        "audio": {"sample_rate_hz": 8000, "duration_s": 1.0},
        "background": background,
        "before": before,
        "after": after,
        **{name: [start <= i < end for i in range(pixels)]
           for name, (start, end) in spans.items()},
        "sound_events": sound_events,
    }


def _d02_heldout_measurement() -> dict[str, Any]:
    """Measure the held-out scene with the same strict rules as the fixture."""
    scene = _d02_parse_scene(_d02_heldout_data())
    measurement = _d02_measure_scene(scene)
    return {
        "seed": _D02_HELDOUT_SEED,
        "pixels_checked": scene.pixels,
        "sound_events_checked": len(scene.events),
        "invariants": _d02_invariants(measurement),
        "errors": measurement,
        "metrics": {
            "removal_error_pixels": sum(measurement["removal_errors_by_region"].values()),
            "intact_deviation_pixels": measurement["intact_deviation_errors"],
            "ambient_sound_retained": (
                measurement["ambient_sound_total"] - measurement["ambient_sound_errors"]
            ),
            "ambient_sound_total": measurement["ambient_sound_total"],
            "actor_sound_removed": (
                measurement["actor_sound_total"] - measurement["actor_sound_errors"]
            ),
            "actor_sound_total": measurement["actor_sound_total"],
        },
    }


def _d02_png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _d02_write_png(path: Path, scene: _D02Scene, values: list[int]) -> None:
    """Write one valid RGB frame from the fixture's scalar grayscale pixels."""
    if len(values) != scene.pixels:
        raise DemoError("D02: buffer PNG no coincide con dimensiones declaradas")
    rows = []
    for row in range(scene.frame_height):
        start = row * scene.frame_width
        row_values = values[start : start + scene.frame_width]
        rows.append(b"\x00" + b"".join(bytes((value, value, value)) for value in row_values))
    header = struct.pack(">IIBBBBB", scene.frame_width, scene.frame_height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _d02_png_chunk(b"IHDR", header)
        + _d02_png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + _d02_png_chunk(b"IEND", b"")
    )


def _d02_wave_samples(
    scene: _D02Scene, *, presence_index: int, cause: str | None = None
) -> list[int]:
    total_samples = round(scene.duration_s * scene.sample_rate_hz)
    output: list[int] = []
    for sample_index in range(total_samples):
        time_s = sample_index / scene.sample_rate_hz
        value = sum(
            event[5] * math.sin(2.0 * math.pi * event[4] * time_s)
            for event in scene.events
            if event[presence_index]
            and (cause is None or event[1] == cause)
            and event[6] <= time_s < event[7]
        )
        sample = round(value)
        if not -32768 <= sample <= 32767:
            raise DemoError("D02: mezcla de audio excede PCM de 16 bits")
        output.append(sample)
    return output


def _d02_write_wav(path: Path, sample_rate_hz: int, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate_hz)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _d02_rms(values: list[int]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def _write_d02_causal_media(data: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Materialize deterministic synthetic RGB and PCM before/after artifacts."""
    scene = _d02_parse_scene(data)
    image_paths = {
        "before": out_dir / _D02_MEDIA_FILES[0],
        "after": out_dir / _D02_MEDIA_FILES[1],
    }
    _d02_write_png(image_paths["before"], scene, scene.before)
    _d02_write_png(image_paths["after"], scene, scene.after)

    before_samples = _d02_wave_samples(scene, presence_index=2)
    after_samples = _d02_wave_samples(scene, presence_index=3)
    actor_samples = _d02_wave_samples(scene, presence_index=2, cause="actor")
    ambient_samples = _d02_wave_samples(scene, presence_index=3, cause="ambient")
    wave_paths = {
        "before": out_dir / _D02_MEDIA_FILES[2],
        "after": out_dir / _D02_MEDIA_FILES[3],
    }
    _d02_write_wav(wave_paths["before"], scene.sample_rate_hz, before_samples)
    _d02_write_wav(wave_paths["after"], scene.sample_rate_hz, after_samples)

    ambient_errors = [
        actual - expected
        for actual, expected in zip(after_samples, ambient_samples, strict=True)
    ]
    actor_errors = [
        (before - after) - actor
        for before, after, actor in zip(
            before_samples, after_samples, actor_samples, strict=True
        )
    ]
    removal_errors = {
        name: sum(
            1
            for index, active in enumerate(scene.regions[name])
            if active and scene.after[index] != scene.background[index]
        )
        for name in ("actor_region", "shadow_region", "reflection_region")
    }
    image_metrics = {
        "width": scene.frame_width,
        "height": scene.frame_height,
        "channels": 3,
        "frame_count": 1,
        "changed_pixels": sum(
            before != after
            for before, after in zip(scene.before, scene.after, strict=True)
        ),
        "removal_errors_by_region": removal_errors,
        "intact_deviation_pixels": sum(
            1
            for index, active in enumerate(scene.regions["intact_region"])
            if active and scene.before[index] != scene.after[index]
        ),
    }
    audio_metrics = {
        "sample_rate_hz": scene.sample_rate_hz,
        "channels": 1,
        "sample_width_bytes": 2,
        "sample_count": len(before_samples),
        "actor_removed_rms": _d02_rms(actor_samples),
        "ambient_preservation_rmse": _d02_rms(ambient_errors),
        "actor_subtraction_rmse": _d02_rms(actor_errors),
        "before_rms": _d02_rms(before_samples),
        "after_rms": _d02_rms(after_samples),
        "events_checked": len(scene.events),
    }
    artifacts = {
        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in (*image_paths.values(), *wave_paths.values())
    }
    return {"image": image_metrics, "audio": audio_metrics, "artifacts": artifacts}


def _write_d02_measurement_report(
    fixture: Path, data: dict[str, Any], media: dict[str, Any], out_dir: Path
) -> Path:
    """Write D02 scalar and materialized-media measurements (synthetic CPU only).

    The report includes source fixture SHA-256, deterministic heldout checks,
    measured PNG/WAV artifacts and ``gpu_used: false``. It makes no model claim.
    """
    report = {
        "schema": "comfy_studio.d02_measurement.v2",
        "evidence_scope": "synthetic_cpu_only",
        "scope": (
            "synthetic CPU evidence only: fixture consistency and a deterministic "
            "held-out scene; no GPU, no omnimatte or model-quality claim"
        ),
        "gpu_used": False,
        "fixture_sha256": _sha256(fixture),
        "seed": _D02_HELDOUT_SEED,
        "media": media,
        "checks": {
            "fixture": _d02_scene_measurement(data),
            "heldout": _d02_heldout_measurement(),
        },
    }
    path = out_dir / _D02_MEASUREMENT_ARTIFACT
    path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path



def _png_fixture(path: Path, seed: int) -> None:
    """Write a tiny valid RGB PNG without adding an imaging dependency."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    pixels = bytes((seed * 17 + i * 29) % 256 for i in range(12))
    raw = b"".join(b"\0" + pixels[row * 6 : (row + 1) * 6] for row in range(2))
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(data)


def _make_d05_inventory(root: Path, ffmpeg: str) -> dict[str, Any]:
    """Exercise real heterogeneous media classification and per-file failure isolation."""
    from .ingest import AssetStatus, ingest_assets

    inventory_dir = root / "inventory"
    inventory_dir.mkdir()
    paths: list[Path] = []
    for i in range(30):
        image = inventory_dir / f"still-{i:02d}.png"
        _png_fixture(image, i)
        paths.append(image)
    for i in range(30):
        audio = inventory_dir / f"audio-{i:02d}.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\0\0" * ((i + 1) * 80))
        paths.append(audio)
    for i in range(30):
        clip = inventory_dir / f"clip-{i:02d}.mp4"
        duration = 0.5 + i * 0.05
        made = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=320x180:rate=12:duration={duration:.2f}",
                "-vf",
                f"hue=h={i * 11}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if made.returncode or not clip.is_file():
            raise DemoError(f"D05 video inventory generation failed: {made.stderr[-1200:]}")
        paths.append(clip)
    for i in range(5):
        unsupported = inventory_dir / f"document-{i:02d}.xyz"
        unsupported.write_bytes(f"unsupported-{i}".encode())
        paths.append(unsupported)
    for i in range(5):
        corrupt = inventory_dir / f"corrupt-{i:02d}.mp4"
        corrupt.write_bytes(b"")
        paths.append(corrupt)
    if len(paths) != 100:
        raise DemoError(f"D05 inventory expected 100 inputs, got {len(paths)}")

    report = ingest_assets(
        paths,
        inflight=16,
        verify_media=True,
        ffprobe_timeout_s=3.0,
        weights_available=False,
    )
    counts = report.counts()
    expected = {
        AssetStatus.PENDING_WEIGHTS: 90,
        AssetStatus.DUPLICATE: 0,
        AssetStatus.UNSUPPORTED: 5,
        AssetStatus.CORRUPT: 5,
    }
    if any(counts.get(status, 0) != value for status, value in expected.items()):
        raise DemoError(f"D05 partial-ingest recovery mismatch: {counts}")
    inventory_manifest = root.parent / "D05_asset_inventory.json"
    inventory_manifest.write_text(
        json.dumps(
            {
                "schema": "comfy_studio.d05_asset_inventory.v1",
                "inputs": [
                    {
                        "path": Path(item.source_path).name,
                        "kind": str(item.kind),
                        "status": str(item.status),
                        "sha256": item.content_sha256,
                        "bytes": item.bytes,
                        "duplicate_of": item.duplicate_of,
                        "error": item.error,
                        "duration_sec": (item.media_info or {}).get("duration_sec"),
                        "video_dimensions": [
                            [stream.get("width"), stream.get("height")]
                            for stream in (item.media_info or {}).get("streams", [])
                            if stream.get("codec_type") == "video"
                        ],
                    }
                    for item in report.assets
                ],
                "counts": counts,
                "weights_available": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "input_count": len(paths),
        "inventory_manifest": inventory_manifest.name,
        "by_kind": {
            kind: sum(1 for item in report.assets if str(item.kind) == kind)
            for kind in ("image", "audio", "video", "unknown")
        },
        "pending_weights": counts.get(AssetStatus.PENDING_WEIGHTS, 0),
        "duplicate": counts.get(AssetStatus.DUPLICATE, 0),
        "unsupported": counts.get(AssetStatus.UNSUPPORTED, 0),
        "corrupt": counts.get(AssetStatus.CORRUPT, 0),
        "valid_assets_survived_partial_failures": (
            counts.get(AssetStatus.PENDING_WEIGHTS, 0) + counts.get(AssetStatus.DUPLICATE, 0) == 90
        ),
    }


def _render_d05(
    data: dict[str, Any],
    out_dir: Path,
    *,
    source_video: Path | None = None,
    brief: str | None = None,
    transcript: str | None = None,
    transcript_start: float = 0.0,
    transcript_end: float | None = None,
    source_license: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise DemoError("D05 requiere ffmpeg y ffprobe reales en PATH")
    from .delivery_4k import (
        Clip,
        TimelineSpec,
        Title,
        edit_text,
        load_editable_package,
        write_editable_package,
    )
    from .edl.ffmpeg_edl import execute_edl_ffmpeg
    from .montage.brief import plan_montage_from_brief
    from .projects import ProjectStore

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    checkpoints: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="d05-workspace-", dir=out_dir) as workspace:
        root = Path(workspace)
        real_source = source_video is not None
        if real_source:
            try:
                source = source_video.expanduser().resolve(strict=True)
            except OSError as exc:
                raise DemoError(f"D05 source video is unavailable: {exc}") from exc
            if not source.is_file() or source.stat().st_size > 512 * 1024 * 1024:
                raise DemoError("D05 source must be a regular video file no larger than 512 MiB")
            active_brief = (brief or "").strip()
            active_transcript = (transcript or "").strip()
            if len(active_brief) < 3 or not active_transcript:
                raise DemoError("D05 real-source mode requires --brief and --transcript")
            if (
                not math.isfinite(transcript_start)
                or transcript_start < 0
                or transcript_end is None
                or not math.isfinite(transcript_end)
                or transcript_end <= transcript_start
            ):
                raise DemoError("D05 real-source mode requires a valid transcript time range")
            if not (source_license or "").strip():
                raise DemoError("D05 real-source mode requires a source license or rights label")
            inventory = {
                "input_count": 1,
                "real_project_source": True,
                "source_filename": source.name,
                "source_sha256": _sha256(source),
                "source_bytes": source.stat().st_size,
            }
            effective_brief = active_brief
            effective_transcript = active_transcript
            effective_license = source_license.strip()
            (out_dir / "D05_asset_inventory.json").write_text(
                json.dumps(
                    {
                        "schema": "comfy_studio.d05_asset_inventory.v1",
                        "mode": "user_project_source",
                        "source": {
                            "filename": source.name,
                            "sha256": inventory["source_sha256"],
                            "bytes": inventory["source_bytes"],
                            "caller_asserted_rights_label": effective_license,
                            "rights_verified": False,
                        },
                        "brief": active_brief,
                        "transcript_sha256": hashlib.sha256(
                            active_transcript.encode("utf-8")
                        ).hexdigest(),
                        "transcript_time_range_sec": [transcript_start, transcript_end],
                        "weights_available": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        else:
            source = root / "source.mp4"
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "testsrc2=size=320x180:rate=12:duration=2", "-an", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source),
            ]
            try:
                made = subprocess.run(
                    command, capture_output=True, text=True, timeout=60, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DemoError(f"D05 source fixture generation failed: {exc}") from exc
            if made.returncode or not source.is_file():
                raise DemoError(f"D05 source fixture generation failed: {made.stderr[-1200:]}")
            inventory = _make_d05_inventory(root, ffmpeg)
            effective_brief = "bosque amanecer"
            effective_transcript = "bosque amanecer paisaje de prueba"
            effective_license = "CC0-1.0"
        checkpoints.append(
            {
                "stage": "project_source_ingest" if real_source else "100_asset_ingest",
                "status": "done",
                "failed_items_isolated": (
                    inventory.get("corrupt", 0) + inventory.get("unsupported", 0)
                ),
            }
        )

        store = ProjectStore(root / "studio-data")
        project = store.create_project(
            data["project_name"],
            description=(
                "User-provided CPU input" if real_source else "Synthetic CPU acceptance fixture"
            ),
        )
        source_mime = {
            ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
            ".mov": "video/quicktime", ".qt": "video/quicktime", ".mkv": "video/x-matroska",
        }.get(source.suffix.lower())
        if source_mime is None:
            raise DemoError(
                "D05 source must use a supported video extension: mp4, m4v, webm, mov, qt, mkv"
            )
        uploaded = store.save_upload(
            project["id"],
            filename=source.name,
            content_type=source_mime,
            data=source.read_bytes(),
            max_upload_bytes=512 * 1024 * 1024,
            category="uploads",
            metadata={"license": effective_license},
        )
        stored_source_path = store.get_asset_path(project["id"], uploaded["id"])
        if stored_source_path is None or _sha256(stored_source_path) != _sha256(source):
            raise DemoError("D05 project ingest did not preserve the source video bytes")
        store.save_transcription(
            project["id"],
            asset_id=uploaded["id"],
            language="es",
            backend="user_provided" if real_source else "fixture",
            segments=[
                {
                    "id": "seg-001",
                    "end": transcript_end if real_source else 1.5,
                    "start": transcript_start,
                    "text": effective_transcript,
                }
            ],
        )
        plan = plan_montage_from_brief(
            store,
            project["id"],
            brief=effective_brief,
            target_duration_sec=1.0,
            max_cuts=2,
            min_segment_sec=0.2,
            max_segment_sec=2.0,
            asset_ids=[uploaded["id"]],
        )
        if plan.get("status") != "planned" or not plan.get("cuts"):
            raise DemoError(f"D05 brief did not produce a bounded EDL: {plan.get('selection')}")
        checkpoints.append({"stage": "brief_to_edl", "status": "done", "cuts": len(plan["cuts"])})

        masters: dict[str, Path] = {}
        delivery_verifications: dict[str, dict[str, Any]] = {}
        dimensions = {
            "16:9": tuple(data["render"]["horizontal"]),
            "9:16": tuple(data["render"]["vertical"]),
        }
        if dimensions != {"16:9": (3840, 2160), "9:16": (2160, 3840)}:
            raise DemoError("D05 fixture must declare exact horizontal and vertical 4K targets")
        fps = int(data["render"]["fps"])
        for aspect, filename in (("16:9", "D05_master_16x9.mp4"), ("9:16", "D05_master_9x16.mp4")):
            rendered = execute_edl_ffmpeg(
                store,
                project["id"],
                aspect_ratio=aspect,
                resolution="4k",
                fps=fps,
                color_range="limited",
            )
            delivery_verifications[aspect] = rendered["verification"]
            path = store.get_asset_path(project["id"], rendered["output_asset"]["id"])
            if path is None:
                raise DemoError(f"D05 {aspect} output missing from project store")
            destination = out_dir / filename
            shutil.copyfile(path, destination)
            masters[aspect] = destination
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if probe.returncode:
                raise DemoError(f"D05 ffprobe failed for {aspect}: {probe.stderr[-1200:]}")
            stream = json.loads(probe.stdout)["streams"][0]
            actual = (int(stream["width"]), int(stream["height"]))
            if actual != dimensions[aspect]:
                raise DemoError(f"D05 {aspect} dimensions mismatch: {actual}")
        checkpoints.append({"stage": "4k_exports", "status": "done", "aspects": 2})

        source_path = store.get_asset_path(project["id"], uploaded["id"])
        if source_path is None:
            raise DemoError("D05 source asset was not retained")
        selected = plan["cuts"][0]
        source_probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "json", str(source_path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if source_probe.returncode:
            raise DemoError(f"D05 could not inspect source video: {source_probe.stderr[-1200:]}")
        source_streams = json.loads(source_probe.stdout).get("streams", [])
        if not source_streams:
            raise DemoError("D05 source has no video stream")
        source_width = int(source_streams[0]["width"])
        source_height = int(source_streams[0]["height"])
        timeline = TimelineSpec(
            name=data["project_name"],
            aspect="16:9",
            fps=fps,
            clips=[
                Clip(
                    "approved-cut-001",
                    str(source_path),
                    selected["in_sec"],
                    selected["out_sec"],
                    license=effective_license,
                    src_width=source_width,
                    src_height=source_height,
                    source_asset_id=uploaded["id"],
                    reason=(
                        f"user brief evidence: {effective_brief}"
                        if real_source else "fixture evidence: bosque/amanecer"
                    ),
                )
            ],
            titles=[Title(data["titles"][0]["id"], data["titles"][0]["text"], 0.0, 1.0)],
            transitions=["cut"],
        )
        package = write_editable_package(
            timeline,
            root / "editable-package",
            additional_artifacts=[
                {
                    "source_path": str(path),
                    "filename": path.name,
                    "kind": "master",
                    "media_type": "video/mp4",
                    "metadata": {
                        "aspect": aspect,
                        "dimensions": list(dimensions[aspect]),
                        "synthetic_cpu_fixture": not real_source,
                    },
                }
                for aspect, path in masters.items()
            ],
        )
        reopened = load_editable_package(package)
        delivery_manifest_path = package / "delivery-manifest.json"
        delivery_manifest = json.loads(delivery_manifest_path.read_text())
        delivery_manifest["rights_assertion"] = {
            "caller_asserted_label": effective_license,
            "rights_verified": False,
        }
        delivery_manifest_path.write_text(
            json.dumps(delivery_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        credit_metadata_retained = bool(delivery_manifest.get("credits")) and all(
            credit.get("license") == effective_license
            and credit.get("source_asset_id") == uploaded["id"]
            for credit in delivery_manifest["credits"]
        )
        original_clips = [asdict(clip) for clip in reopened.clips]
        invalidated = edit_text(reopened, data["titles"][0]["id"], "Título editado tras reabrir")
        editable_ok = invalidated == {data["titles"][0]["id"]} and original_clips == [
            asdict(clip) for clip in reopened.clips
        ]
        project_json = out_dir / "editable_project.json"
        project_json.write_text(reopened.to_json() + "\n", encoding="utf-8")
        (package / "project.json").write_text(reopened.to_json(), encoding="utf-8")
        reopened_after_edit = load_editable_package(package)
        editable_ok = (
            editable_ok and reopened_after_edit.titles[0].text == "Título editado tras reabrir"
        )
        if not editable_ok:
            raise DemoError("D05 editable project changed approved clip data during title edit")
        if not credit_metadata_retained:
            raise DemoError("D05 delivery package did not retain caller-provided credit metadata")
        archive = out_dir / "D05_delivery_package.zip"
        from zipfile import ZIP_STORED, ZipFile

        with ZipFile(archive, "w", compression=ZIP_STORED) as bundle:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(package).as_posix())
        checkpoints.append({"stage": "editable_roundtrip", "status": "done"})

    model_run_budget = next(demo.budget_runs for demo in demo_catalog() if demo.demo_id == "D05")
    return archive, {
        "asset_inventory": inventory,
        "brief_to_edl": {
            "cuts": len(plan["cuts"]),
            "simulated_ml": plan["simulated"],
            "evidence_mode": (
                "user-provided transcript" if real_source else "CPU fixture transcript"
            ),
        },
        "ffmpeg_encode": {
            "dimensions_verified": {aspect: True for aspect in dimensions},
            "actual_dimensions": {aspect: list(size) for aspect, size in dimensions.items()},
            "fps_verified": {
                aspect: detail["verified"] and detail["probed_fps"] == fps
                for aspect, detail in delivery_verifications.items()
            },
            "duration_sec": {
                aspect: detail["actual_duration_sec"]
                for aspect, detail in delivery_verifications.items()
            },
            "color_range_verified": {
                aspect: detail["probed_color_range"] == "tv"
                for aspect, detail in delivery_verifications.items()
            },
            "synthetic_fixture": not real_source,
            "real_project_source": real_source,
            "quality_claim": False,
        },
        "editable_roundtrip": {
            "reopened": True,
            "title_edit_preserved_clips": editable_ok,
            "credit_metadata_retained": credit_metadata_retained,
            "rights_verified": False,
        },
        "recovery": {
            "partial_failures_isolated": True,
            "successful_inputs_retained": inventory.get(
                "valid_assets_survived_partial_failures", real_source
            ),
        },
        "duration_s": round(time.monotonic() - started, 3),
        "checkpoints": checkpoints + [{"stage": "delivery_package", "status": "done"}],
        "budget": {
            "model_run_budget": model_run_budget,
            "model_runs_consumed": 0,
            "cpu_export_count": len(masters),
            "within_budget": 0 <= model_run_budget,
        },
    }


def run_demo(
    demo_id: str,
    output_root: Path,
    *,
    resume: bool = False,
    source_video: Path | None = None,
    brief: str | None = None,
    transcript: str | None = None,
    transcript_start: float = 0.0,
    transcript_end: float | None = None,
    source_license: str | None = None,
) -> dict[str, Any]:
    demos = {demo.demo_id: demo for demo in demo_catalog()}
    if demo_id not in demos:
        raise DemoError(f"demo desconocida: {demo_id}; disponibles: {', '.join(demos)}")
    demo = demos[demo_id]
    fixture, fixture_data = _load_fixture(demo)
    out_dir, manifest_path = _prepare_demo_output(demo_id, output_root)
    input_hash = _sha256(fixture)
    checks = _check_fixture(demo_id, fixture_data)
    inputs = [{
        "kind": "fixture",
        "path": f"fixtures/{fixture.name}",
        "sha256": input_hash,
    }]
    if source_video is not None:
        if demo_id != "D05":
            raise DemoError("--source-video is supported only for D05")
        try:
            source_path = source_video.expanduser().resolve(strict=True)
        except OSError as exc:
            raise DemoError(f"D05 source video is unavailable: {exc}") from exc
        if not source_path.is_file() or source_path.stat().st_size > 512 * 1024 * 1024:
            raise DemoError("D05 source must be a regular video file no larger than 512 MiB")
        if source_path.suffix.lower() not in {
            ".mp4", ".m4v", ".webm", ".mov", ".qt", ".mkv"
        }:
            raise DemoError("D05 source must use a supported video extension")
        if (
            not math.isfinite(transcript_start)
            or transcript_start < 0
            or transcript_end is None
            or not math.isfinite(transcript_end)
            or transcript_end <= transcript_start
        ):
            raise DemoError(
                "D05 real-source mode requires --transcript-start and --transcript-end "
                "with start < end"
            )
        inputs.append({
            "kind": "project_source_video",
            "path": source_path.name,
            "sha256": _sha256(source_path),
            "bytes": source_path.stat().st_size,
            "brief": (brief or "").strip(),
            "transcript_sha256": hashlib.sha256((transcript or "").encode("utf-8")).hexdigest(),
            "transcript_start_s": transcript_start,
            "transcript_end_s": transcript_end,
            "caller_asserted_rights_label": source_license,
            "rights_verified": False,
        })
    execution_fingerprint = _execution_fingerprint(demo_id)
    if resume and manifest_path.is_file():
        def artifact_is_valid(item: dict[str, Any]) -> bool:
            name = item.get("path")
            if not isinstance(name, str) or Path(name).name != name:
                return False
            candidate = out_dir / name
            return (
                not candidate.is_symlink()
                and candidate.is_file()
                and candidate.resolve().parent == out_dir
                and _sha256(candidate) == item.get("sha256")
                and candidate.stat().st_size == item.get("bytes")
            )

        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            valid = (
                prior.get("schema") == "comfy_studio.demo_run.v3"
                and prior.get("result") == (
                    "cpu_real_input_flow_completed" if source_video is not None
                    else "cpu_fixture_checks_passed"
                )
                and prior.get("inputs") == inputs
                and prior.get("execution_fingerprint") == execution_fingerprint
                and isinstance(prior.get("artifacts"), list)
                and {
                    item.get("path")
                    for item in prior["artifacts"]
                    if isinstance(item, dict)
                } == _expected_artifact_names(demo_id)
                and len(prior["artifacts"]) == len(_expected_artifact_names(demo_id))
                and all(
                    isinstance(item, dict) and artifact_is_valid(item)
                    for item in prior["artifacts"]
                )
            )
        except (OSError, json.JSONDecodeError, IndexError, KeyError, TypeError):
            valid = False
            prior = {}
        if valid:
            return {"result": prior["result"], "manifest": str(manifest_path),
                    "artifacts": prior.get("artifacts", []), "checks": prior.get("checks", {}),
                    "resumed": True}
    artifact: Path | None = None
    project_artifact: Path | None = None
    measurement_report: Path | None = None
    media_artifacts: list[Path] = []
    if demo_id == "D02":
        media = _write_d02_causal_media(fixture_data, out_dir)
        media_artifacts = [out_dir / name for name in _D02_MEDIA_FILES]
        measurement_report = _write_d02_measurement_report(
            fixture, fixture_data, media, out_dir
        )
    elif demo_id == "D01":
        measurement_report = _write_d01_reprojection_report(fixture, fixture_data, out_dir)
    elif demo_id == "D05":
        if source_video is None:
            artifact, render_checks = _render_d05(fixture_data, out_dir)
        else:
            artifact, render_checks = _render_d05(
                fixture_data, out_dir, source_video=source_video, brief=brief,
                transcript=transcript, transcript_start=transcript_start,
                transcript_end=transcript_end, source_license=source_license,
            )
        checks.update(render_checks)
        project_artifact = out_dir / "editable_project.json"
    failed = [name for name, value in checks.items() if isinstance(value, bool) and not value]
    if failed:
        raise DemoError(f"{demo_id} failed fixture checks: {', '.join(failed)}")
    manifest: dict[str, Any] = {
        "schema": "comfy_studio.demo_run.v3",
        "demo": demo.to_dict(),
        "result": "cpu_real_input_flow_completed" if source_video is not None
        else "cpu_fixture_checks_passed",
        "claim_scope": (
            "synthetic CPU pixel/audio fixture evidence only; no model quality, "
            "real-footage, or GPU capability proof"
            if demo_id == "D02"
            else (
                "fixture consistency/toolchain only; no real camera "
                "calibration, scene coverage, model quality, or capability proof"
                if demo_id == "D01"
            else (
                "user-provided source and transcript processed through project ingest, montage, "
                "FFmpeg delivery and editable package; CPU/toolchain evidence only, no model "
                "quality or capability proof"
                if source_video is not None
                else "fixture consistency/toolchain only; no model quality or capability proof"
            )
            )
        ),
        "inputs": inputs,
        "execution_fingerprint": execution_fingerprint,
        "checks": checks,
        "checkpoints": checks.get("checkpoints", []),
        "stages": [
            {
                "name": s.name,
                "status": str(
                    StageStatus.DONE if s.status is StageStatus.READY_CPU else s.status
                ),
                "level": str(s.level),
                "neural": s.neural,
                "pending_reason": s.pending_reason,
                "evidence": "CPU real-input flow only" if source_video is not None
                else "CPU fixture check only"
                if s.status is StageStatus.READY_CPU
                else None,
            }
            for s in demo.stages
        ],
        "artifacts": [],
    }
    generated_artifacts = [artifact] if artifact else []
    generated_artifacts.extend(media_artifacts)
    if measurement_report is not None:
        generated_artifacts.append(measurement_report)
    if demo_id == "D05":
        generated_artifacts.extend(out_dir / name for name in (
            "D05_master_16x9.mp4", "D05_master_9x16.mp4", "D05_asset_inventory.json"))
    if project_artifact:
        generated_artifacts.append(project_artifact)
    for generated_artifact in generated_artifacts:
        manifest["artifacts"].append({"path": generated_artifact.name,
                                      "sha256": _sha256(generated_artifact),
                                      "bytes": generated_artifact.stat().st_size})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return {"result": manifest["result"], "manifest": str(manifest_path),
            "artifacts": manifest["artifacts"], "checks": checks, "resumed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run declared Comfy Studio D01–D05 CPU fixture checks"
    )
    parser.add_argument("--list", action="store_true", help="list scenarios without running them")
    parser.add_argument("--demo", choices=[f"D{i:02d}" for i in range(1, 6)])
    parser.add_argument("--output-root", type=Path, default=Path("./demo-runs"))
    parser.add_argument("--resume", action="store_true", help="reuse verified prior artifacts")
    parser.add_argument("--clean", action="store_true", help="remove only the selected demo output")
    parser.add_argument(
        "--source-video", type=Path,
        help="D05: ingest a project-owned video instead of generating a fixture",
    )
    parser.add_argument("--brief", help="D05 real-source mode: montage brief")
    parser.add_argument(
        "--transcript", help="D05 real-source mode: transcript text for the selected source range"
    )
    parser.add_argument("--transcript-start", type=float, default=0.0)
    parser.add_argument("--transcript-end", type=float)
    parser.add_argument(
        "--source-license",
        help=(
            "D05: caller-asserted rights/credit label to retain as metadata; "
            "not independently verified"
        ),
    )
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps([d.to_dict() for d in demo_catalog()], ensure_ascii=False, indent=2))
        return 0
    if not args.demo:
        parser.error("choose --list or --demo D01..D05")
    try:
        if args.clean:
            print(json.dumps({"removed": clean_demo_output(args.demo, args.output_root)}))
            return 0
        print(json.dumps(
            run_demo(
                args.demo, args.output_root, resume=args.resume,
                source_video=args.source_video, brief=args.brief, transcript=args.transcript,
                transcript_start=args.transcript_start, transcript_end=args.transcript_end,
                source_license=args.source_license,
            ),
            ensure_ascii=False,
            indent=2,
        ))
    except DemoError as exc:
        print(f"demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Demo", "DemoError", "DemoStage", "EvidenceLevel", "StageStatus",
           "assert_no_fake_gpu_result", "clean_demo_output", "demo_catalog", "run_demo"]
