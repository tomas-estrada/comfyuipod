"""Tests CPU de reparacion localizada (C24/A17)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from comfy_studio.edl.localized_repair import (
    LocalizedRepairError,
    _frame_hashes,
    _ordered_hash_digest,
    build_localized_repair_video,
    localized_repair,
    verify_source_frame_provenance,
)
from comfy_studio.projects import ProjectStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe requeridos",
)


def _make_video(path, seconds, color):
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:d={seconds}:r=16",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _make_test_pattern_video(path, seconds, rate=16):
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate={rate}:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    s.create_project(name="Proyecto Repair")
    return s


def _upload(s, pid, path):
    asset = s.save_upload(
        pid,
        filename=path.name,
        content_type="video/mp4",
        data=path.read_bytes(),
        max_upload_bytes=64 * 1024 * 1024,
        category="outputs",
    )
    return str(asset["id"])


def test_build_localized_repair_and_provenance(tmp_path):
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    out = tmp_path / "out.mp4"
    _make_video(src, 10.0, "red")
    _make_video(repl, 2.0, "blue")
    build = build_localized_repair_video(src, repl, out, edit_start_sec=4.0, edit_end_sec=6.0)
    assert Path(build["output_path"]).is_file()
    assert build["verification"]["probe_ok"] is True
    assert build["verification"]["has_audio"] is True
    exact = build["frame_provenance"]
    assert exact["provenance_ok"] is True
    assert exact["comparison"] == "sha256_per_frame_pre_final_encode"
    assert exact["head_frame_count"] > 0
    assert exact["tail_frame_count"] > 0
    assert exact["head_hashes_match"] is True
    assert exact["tail_hashes_match"] is True
    assert exact["byte_identity_claimed"] is False
    assert exact["lossy_final_encode"] is True
    prov = verify_source_frame_provenance(src, out, edit_start_sec=4.0, edit_end_sec=6.0)
    assert prov["byte_identity_claimed"] is False
    assert prov["provenance_ok"] is True
    assert prov["sample_count"] > 0
    assert prov["max_delta_rgb"] <= prov["tolerance_rgb"]


def test_frame_hashes_cover_varying_unchanged_frames(tmp_path):
    src = tmp_path / "moving_source.mp4"
    repl = tmp_path / "repl.mp4"
    out = tmp_path / "out.mp4"
    _make_test_pattern_video(src, 6.0)
    _make_video(repl, 2.0, "blue")

    build = build_localized_repair_video(src, repl, out, edit_start_sec=2.0, edit_end_sec=4.0)
    proof = build["frame_provenance"]
    assert proof["provenance_ok"] is True
    assert proof["head_frame_count"] == 48
    assert proof["replacement_frame_count"] == 48
    assert proof["tail_frame_count"] == 48
    assert proof["assembled_frame_count"] == 144
    assert proof["head_expected_sha256"] == proof["head_assembled_sha256"]
    assert proof["tail_expected_sha256"] == proof["tail_assembled_sha256"]


def test_c24_ledger_acceptance_15s_moving_source_2s_replacement(tmp_path):
    """Caso exacto del ledger C24: fuente movil de 15 s, reemplazo exacto de 2 s.

    Afirmaciones: los hashes SHA-256 por fotograma de cabecera y cola coinciden
    con la secuencia del fuente antes del encode final con perdida; el numero de
    fotogramas de reemplazo es 2*fps; el total sigue siendo 15*fps; y el reporte
    declara encode final con perdida sin reclamar identidad de bytes.
    """
    fps = 12
    start_sec, end_sec = 6.0, 8.0
    src = tmp_path / "ledger_src15.mp4"
    repl = tmp_path / "ledger_repl2.mp4"
    out = tmp_path / "ledger_out.mp4"
    _make_test_pattern_video(src, 15.0, rate=fps)
    _make_video(repl, 2.0, "blue")

    build = build_localized_repair_video(
        src,
        repl,
        out,
        edit_start_sec=start_sec,
        edit_end_sec=end_sec,
        fps=fps,
    )
    proof = build["frame_provenance"]

    # Secuencia SHA-256 por fotograma del fuente completo (15 s * fps).
    source_hashes = _frame_hashes(src, ffmpeg_bin=FFMPEG, timeout_s=180.0)
    assert len(source_hashes) == 15 * fps

    head_expected = source_hashes[: 6 * fps]
    tail_expected = source_hashes[8 * fps :]
    assert proof["head_frame_count"] == 6 * fps
    assert proof["tail_frame_count"] == 7 * fps
    assert proof["replacement_frame_count"] == 2 * fps
    assert proof["assembled_frame_count"] == 15 * fps
    assert proof["provenance_ok"] is True
    assert proof["head_hashes_match"] is True
    assert proof["tail_hashes_match"] is True
    # Las secuencias de cabecera y cola provienen del propio fuente.
    source_head_digest = _ordered_hash_digest(head_expected)
    source_tail_digest = _ordered_hash_digest(tail_expected)
    assert proof["head_expected_sha256"] == source_head_digest
    assert proof["head_assembled_sha256"] == source_head_digest
    assert proof["tail_expected_sha256"] == source_tail_digest
    assert proof["tail_assembled_sha256"] == source_tail_digest
    # El reporte describe el encode final con perdida y no reclama identidad de bytes.
    assert proof["comparison"] == "sha256_per_frame_pre_final_encode"
    assert proof["byte_identity_claimed"] is False
    assert proof["lossy_final_encode"] is True
    assert build["verification"]["probe_ok"] is True
    assert build["verification"]["codec_name"] == "h264"

    # El MP4 final conserva 15*fps fotogramas (solo el codec cambia, no el conteo).
    output_hashes = _frame_hashes(out, ffmpeg_bin=FFMPEG, timeout_s=180.0)
    assert len(output_hashes) == 15 * fps
    post = verify_source_frame_provenance(
        src, out, edit_start_sec=start_sec, edit_end_sec=end_sec
    )
    assert post["provenance_ok"] is True
    assert post["byte_identity_claimed"] is False
    assert post["lossy_encode"] is True


def test_localized_repair_store_flow(tmp_path, store):
    pid = store.list_projects()[0]["id"]
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    _make_video(src, 8.0, "green")
    _make_video(repl, 1.0, "yellow")
    sid = _upload(store, pid, src)
    rid = _upload(store, pid, repl)
    result = localized_repair(
        store,
        pid,
        source_asset_id=sid,
        replacement_asset_id=rid,
        edit_start_sec=2.0,
        edit_end_sec=3.0,
    )
    assert result["status"] == "repaired"
    out_asset = result["output_asset"]
    assert out_asset["kind"] == "video"
    meta = out_asset.get("metadata") or {}
    assert meta.get("localized_repair") is True
    assert meta.get("frame_provenance_ok") is True
    assert meta.get("frame_check_method") == "sha256_per_frame_pre_final_encode"
    assert meta.get("frame_provenance_unchanged_frame_count") == (
        meta.get("frame_provenance_head_frame_count")
        + meta.get("frame_provenance_tail_frame_count")
    )
    assert meta.get("frame_post_encode_rgb_sample_count", 0) > 0
    assert meta.get("byte_identity_claimed") is False
    assert store.get_asset_path(pid, out_asset["id"]) is not None


def test_localized_repair_does_not_register_failed_frame_check(tmp_path, store, monkeypatch):
    from importlib import import_module

    repair_module = import_module("comfy_studio.edl.localized_repair")

    pid = store.list_projects()[0]["id"]
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    _make_video(src, 8.0, "green")
    _make_video(repl, 1.0, "yellow")
    sid = _upload(store, pid, src)
    rid = _upload(store, pid, repl)
    initial_output_count = len(store.list_assets(pid, category="outputs"))
    original_build = repair_module.build_localized_repair_video

    def fail_exact_provenance(*args, **kwargs):
        result = original_build(*args, **kwargs)
        result["frame_provenance"]["provenance_ok"] = False
        return result

    monkeypatch.setattr(repair_module, "build_localized_repair_video", fail_exact_provenance)

    with pytest.raises(LocalizedRepairError, match="no se registró"):
        localized_repair(
            store,
            pid,
            source_asset_id=sid,
            replacement_asset_id=rid,
            edit_start_sec=2.0,
            edit_end_sec=3.0,
        )

    assert len(store.list_assets(pid, category="outputs")) == initial_output_count


def test_verify_fails_if_outside_changed(tmp_path):
    src = tmp_path / "src.mp4"
    other = tmp_path / "other.mp4"
    out = tmp_path / "out.mp4"
    _make_video(src, 8.0, "red")
    _make_video(other, 8.0, "blue")
    import shutil as sh

    sh.copyfile(other, out)
    prov = verify_source_frame_provenance(src, out, edit_start_sec=2.0, edit_end_sec=4.0)
    assert prov["provenance_ok"] is False


def test_exact_frame_hash_comparison_detects_one_untouched_frame_change():
    from comfy_studio.edl.localized_repair import _compare_unchanged_frames

    head = [bytes.fromhex("11" * 32), bytes.fromhex("22" * 32)]
    tail = [bytes.fromhex("33" * 32), bytes.fromhex("44" * 32)]
    assembled = [*head, bytes.fromhex("55" * 32), *tail]
    assert _compare_unchanged_frames(head, 1, tail, assembled)["provenance_ok"] is True

    assembled[-1] = bytes.fromhex("66" * 32)
    result = _compare_unchanged_frames(head, 1, tail, assembled)
    assert result["provenance_ok"] is False
    assert result["tail_hashes_match"] is False


def test_invalid_interval_raises(tmp_path):
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    out = tmp_path / "out.mp4"
    _make_video(src, 5.0, "red")
    _make_video(repl, 1.0, "blue")
    with pytest.raises(LocalizedRepairError):
        build_localized_repair_video(src, repl, out, edit_start_sec=4.0, edit_end_sec=9.0)


def test_replacement_duration_must_match_interval(tmp_path):
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    out = tmp_path / "out.mp4"
    _make_video(src, 8.0, "red")
    _make_video(repl, 1.0, "blue")
    with pytest.raises(LocalizedRepairError, match="duracion del segmento"):
        build_localized_repair_video(src, repl, out, edit_start_sec=2.0, edit_end_sec=4.0)


def test_localized_repair_checks_dynamic_temp_disk_space(tmp_path, monkeypatch):
    from importlib import import_module
    from types import SimpleNamespace

    repair_module = import_module("comfy_studio.edl.localized_repair")
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    out = tmp_path / "out.mp4"
    _make_video(src, 4.0, "red")
    _make_video(repl, 1.0, "blue")
    monkeypatch.setattr(
        repair_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0)
    )

    with pytest.raises(LocalizedRepairError, match="Espacio temporal insuficiente"):
        build_localized_repair_video(src, repl, out, edit_start_sec=1.0, edit_end_sec=2.0)
    assert not out.exists()


def test_localized_repair_tool_registered():
    from comfy_studio.agent.tools import create_default_tool_belt

    belt = create_default_tool_belt()
    spec = belt.get("localized_repair")
    assert spec is not None
    assert "sha-256" in (spec.description or "").lower()
    assert "antes del encode final" in (spec.description or "").lower()
    assert "identidad de bytes" in (spec.description or "").lower()


def test_output_filename_cannot_escape_project_temp_area(tmp_path, store):
    pid = store.list_projects()[0]["id"]
    src = tmp_path / "src.mp4"
    repl = tmp_path / "repl.mp4"
    _make_video(src, 8.0, "green")
    _make_video(repl, 1.0, "yellow")
    sid = _upload(store, pid, src)
    rid = _upload(store, pid, repl)
    result = localized_repair(
        store,
        pid,
        source_asset_id=sid,
        replacement_asset_id=rid,
        edit_start_sec=2.0,
        edit_end_sec=3.0,
        output_filename="../../outside.mp4",
    )
    assert result["output_asset"]["original_filename"] == "outside.mp4"
    assert not (tmp_path.parent / "outside.mp4").exists()
