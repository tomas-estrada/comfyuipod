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
import subprocess
import sys
import time
from dataclasses import dataclass, field
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
        generated.append(out_dir / "D05_cpu_fixture_4k.mp4")
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


def _render_d05(data: dict[str, Any], out_dir: Path) -> tuple[Path, dict[str, Any]]:
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise DemoError("D05 requiere ffmpeg y ffprobe reales en PATH")
    render = data["render"]
    width, height = render["width"], render["height"]
    if width != 3840 or height != 2160:
        raise DemoError("D05 CPU acceptance fixture must target exact 3840x2160")
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "D05_cpu_fixture_4k.mp4"
    # Deliberately synthetic, short, and unmistakably labeled. It checks the
    # FFmpeg toolchain and exact dimensions; it is not a creative-quality render.
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", "color=c=0x174a7e:s=3840x2160:r=1:d=1", "-frames:v", "1",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               str(output)]
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DemoError(f"D05 FFmpeg execution failed: {exc}") from exc
    if result.returncode:
        raise DemoError(f"D05 FFmpeg failed (exit {result.returncode}): {result.stderr[-1200:]}")
    probe_cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "json", str(output)]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30, check=False)
    if probe.returncode:
        raise DemoError(f"D05 ffprobe failed: {probe.stderr[-1200:]}")
    stream = json.loads(probe.stdout)["streams"][0]
    actual = {"width": int(stream["width"]), "height": int(stream["height"])}
    return output, {"dimensions_verified": actual == {"width": width, "height": height},
                    "actual_dimensions": actual, "duration_s": round(time.monotonic() - started, 3),
                    "synthetic_fixture": True, "quality_claim": False}


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
                prior.get("schema") == "comfy_studio.demo_run.v1"
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
        checks["ffmpeg_encode"] = render_checks
        project_artifact = out_dir / "editable_project.json"
        project_artifact.write_text(
            json.dumps(
                {
                    "schema": "comfy_studio.editable_delivery.v1",
                    "project": fixture_data["project_name"],
                    "titles": fixture_data["titles"],
                    "credits": fixture_data["credits"],
                    "render": artifact.name,
                    "render_kind": "synthetic_cpu_fixture",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    failed = [name for name, value in checks.items() if isinstance(value, bool) and not value]
    if failed:
        raise DemoError(f"{demo_id} failed fixture checks: {', '.join(failed)}")
    manifest: dict[str, Any] = {
        "schema": "comfy_studio.demo_run.v1",
        "demo": demo.to_dict(),
        "result": "cpu_fixture_checks_passed",
        "claim_scope": "fixture consistency/toolchain only; no model quality or capability proof",
        "inputs": [{"path": f"fixtures/{fixture.name}", "sha256": _sha256(fixture)}],
        "checks": checks,
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
    if artifact:
        manifest["artifacts"].append({"path": artifact.name, "sha256": _sha256(artifact),
                                      "bytes": artifact.stat().st_size})
    if project_artifact:
        manifest["artifacts"].append({"path": project_artifact.name,
                                      "sha256": _sha256(project_artifact),
                                      "bytes": project_artifact.stat().st_size})
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
