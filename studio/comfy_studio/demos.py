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
        Demo("D01", "Coche en lugar real del Aburrá, varias cámaras/luces/sonido",
             ["fixtures/d01_vehicle_cameras.json"],
             ["reprojection_rmse_below_fixture_limit", "logo_mask_unchanged"],
             ["heldout_views"], 3,
             [_stage("reconstruccion_guias", False), _stage("generacion", True),
              _stage("relight", True), _stage("foley", False), _stage("finishing_4k", True)]),
        Demo("D02", "Eliminar una causa y sus consecuencias (omnimatte)",
             ["fixtures/d02_causal_layers.json"],
             ["unaffected_region_unchanged", "removed_region_revealed"],
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
    """Create a contained output directory and reject symlinked run files."""
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
    if demo_id == "D05":
        generated.extend(out_dir / name for name in (
            "D05_delivery_package.zip", "D05_master_16x9.mp4", "D05_master_9x16.mp4"))
        generated.append(out_dir / "D05_asset_inventory.json")
    if any(path.is_symlink() for path in generated):
        raise DemoError("refusing to overwrite a demo output symlink")
    return out_dir, manifest


def _check_fixture(demo_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Run small, explicit fixture integrity checks; these do not validate ML."""
    if demo_id == "D01":
        cameras = data["cameras"]
        errors = []
        for camera in cameras:
            for point in camera["points"]:
                x, y, z = point["xyz"]
                u, v = point["uv"]
                fx, fy, cx, cy = camera["intrinsics"]
                projected = (fx * x / z + cx, fy * y / z + cy)
                errors.append(math.dist(projected, (u, v)))
        rmse = math.sqrt(sum(err * err for err in errors) / len(errors))
        logos_same = data["logo_mask_before"] == data["logo_mask_after"]
        limit = float(data["max_reprojection_rmse_px"])
        return {"reprojection_rmse_px": rmse, "fixture_limit_px": limit,
                "reprojection_fixture_check": rmse <= limit, "logo_mask_unchanged": logos_same}
    if demo_id == "D02":
        before, after, mask = data["before"], data["after"], data["causal_mask"]
        if not (len(before) == len(after) == len(mask)):
            raise DemoError("D02: buffers de capas con longitud diferente")
        unaffected = all(before[i] == after[i] for i, m in enumerate(mask) if not m)
        revealed = all(after[i] == data["background"][i] for i, m in enumerate(mask) if m)
        return {"unaffected_region_unchanged": unaffected, "removed_region_revealed": revealed,
                "pixels_checked": len(mask)}
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


def _render_d05(data: dict[str, Any], out_dir: Path) -> tuple[Path, dict[str, Any]]:
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
        source = root / "source.mp4"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=12:duration=2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
        try:
            made = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DemoError(f"D05 source fixture generation failed: {exc}") from exc
        if made.returncode or not source.is_file():
            raise DemoError(f"D05 source fixture generation failed: {made.stderr[-1200:]}")
        inventory = _make_d05_inventory(root, ffmpeg)
        checkpoints.append(
            {
                "stage": "100_asset_ingest",
                "status": "done",
                "failed_items_isolated": inventory["corrupt"] + inventory["unsupported"],
            }
        )

        store = ProjectStore(root / "studio-data")
        project = store.create_project(
            data["project_name"], description="Synthetic CPU acceptance fixture"
        )
        uploaded = store.save_upload(
            project["id"],
            filename="source.mp4",
            content_type="video/mp4",
            data=source.read_bytes(),
            max_upload_bytes=8 * 1024 * 1024,
            category="uploads",
            metadata={"license": "CC0-1.0"},
        )
        store.save_transcription(
            project["id"],
            asset_id=uploaded["id"],
            language="es",
            backend="fixture",
            segments=[
                {
                    "id": "seg-001",
                    "start": 0.0,
                    "end": 1.5,
                    "text": "bosque amanecer paisaje de prueba",
                }
            ],
        )
        plan = plan_montage_from_brief(
            store,
            project["id"],
            brief="bosque amanecer",
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
                    license="CC0-1.0",
                    src_width=320,
                    src_height=180,
                    source_asset_id=uploaded["id"],
                    reason="brief evidence: bosque/amanecer",
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
                        "synthetic_cpu_fixture": True,
                    },
                }
                for aspect, path in masters.items()
            ],
        )
        reopened = load_editable_package(package)
        delivery_manifest = json.loads((package / "delivery-manifest.json").read_text())
        credits_verified = bool(delivery_manifest.get("credits")) and all(
            credit.get("license") == "CC0-1.0"
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
        if not credits_verified:
            raise DemoError("D05 delivery package did not retain source credit and license")
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
            "evidence_mode": "CPU fixture transcript",
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
            "synthetic_fixture": True,
            "quality_claim": False,
        },
        "editable_roundtrip": {
            "reopened": True,
            "title_edit_preserved_clips": editable_ok,
            "credits_verified": credits_verified,
        },
        "recovery": {
            "partial_failures_isolated": True,
            "successful_inputs_retained": inventory["valid_assets_survived_partial_failures"],
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


def run_demo(demo_id: str, output_root: Path, *, resume: bool = False) -> dict[str, Any]:
    demos = {demo.demo_id: demo for demo in demo_catalog()}
    if demo_id not in demos:
        raise DemoError(f"demo desconocida: {demo_id}; disponibles: {', '.join(demos)}")
    demo = demos[demo_id]
    fixture, fixture_data = _load_fixture(demo)
    out_dir, manifest_path = _prepare_demo_output(demo_id, output_root)
    input_hash = _sha256(fixture)
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
            )

        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            valid = (
                prior.get("schema") == "comfy_studio.demo_run.v2"
                and prior.get("inputs", [{}])[0].get("sha256") == input_hash
                and isinstance(prior.get("artifacts", []), list)
                and all(artifact_is_valid(item) for item in prior.get("artifacts", []))
            )
        except (OSError, json.JSONDecodeError, IndexError, KeyError, TypeError):
            valid = False
            prior = {}
        if valid:
            return {"result": prior["result"], "manifest": str(manifest_path),
                    "artifacts": prior.get("artifacts", []), "checks": prior.get("checks", {}),
                    "resumed": True}
    checks = _check_fixture(demo_id, fixture_data)
    artifact: Path | None = None
    project_artifact: Path | None = None
    if demo_id == "D05":
        artifact, render_checks = _render_d05(fixture_data, out_dir)
        checks.update(render_checks)
        project_artifact = out_dir / "editable_project.json"
    failed = [name for name, value in checks.items() if isinstance(value, bool) and not value]
    if failed:
        raise DemoError(f"{demo_id} failed fixture checks: {', '.join(failed)}")
    manifest: dict[str, Any] = {
        "schema": "comfy_studio.demo_run.v2",
        "demo": demo.to_dict(),
        "result": "cpu_fixture_checks_passed",
        "claim_scope": "fixture consistency/toolchain only; no model quality or capability proof",
        "inputs": [{"path": f"fixtures/{fixture.name}", "sha256": _sha256(fixture)}],
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
                "evidence": "CPU fixture check only"
                if s.status is StageStatus.READY_CPU
                else None,
            }
            for s in demo.stages
        ],
        "artifacts": [],
    }
    generated_artifacts = [artifact] if artifact else []
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
            run_demo(args.demo, args.output_root, resume=args.resume),
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
