"""Tests deterministas (CPU, sin ffmpeg) del armado de comandos de variantes de anuncio.

Cubre funciones puras de `comfy_studio.ads.pipeline`:
  - `_ffmpeg_variant_cmd` (pipeline.py:319): inputs de imagen ("-loop 1"), clamp de
    vídeo con `media_duration_s`, grafos de audio (voz / sólo música / ambos: amix vs
    anull) y la cola "-map [aout] ... -shortest" frente a "-an";
  - `_resolve_segments` (pipeline.py:195): desajuste de `chunk_asset_ids`, asset
    ausente, kind distinto de video/imagen, archivo físico ausente y sonda con
    `probe_ok=false`;
  - `_render_variant` (pipeline.py:399): sustitución del placeholder del binario y
    errores tipados (reintentos, timeout, OSError).

Ningún test lanza ffmpeg/ffprobe reales: las ramas de vídeo parchean
`comfy_studio.ads.pipeline.probe_media_info` y las de imagen ni siquiera lo invocan.

Documenta (sin corregir, regla de repo) el bug actual de audio: `audio_filters`
(pipeline.py:349, :353, :357) nunca se fusiona a `filters` al armar
`filter_complex` (pipeline.py:366), así que los productores `aresample` se
descartan y `anull`/`amix` consumen etiquetas sin productor.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from comfy_studio.ads import pipeline as ad_pipeline
from comfy_studio.ads.pipeline import (
    AdPipelineError,
    _ffmpeg_variant_cmd,
    _render_variant,
    _resolve_segments,
    _Segment,
)
from comfy_studio.ads.schema import AdBrief, AdStoryboard, build_storyboard, compose_script
from comfy_studio.config import SecretRedactor
from comfy_studio.projects import ProjectStore

_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)
_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
_FAKE_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16

_BRIEF = {
    "product_name": "Cafetera Nova",
    "key_message": "El café perfecto cada mañana en 30 segundos.",
    "audience": "profesionales ocupados",
    "tone": "cercano",
    "target_duration_s": 12.0,
    "cta": "Pruébala hoy con un 20% de descuento.",
    "language": "es",
    "product_asset_ids": [],
}


def _brief(**overrides: Any) -> AdBrief:
    return AdBrief.model_validate({**_BRIEF, **overrides})


def _store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(studio_data_dir=tmp_path / "studio_data")


def _storyboard(product_asset_ids: list[str]) -> AdStoryboard:
    script = compose_script(_brief(product_asset_ids=product_asset_ids))
    return build_storyboard(script, ad_id="adcmdtest1")


def _save_image(store: ProjectStore, project_id: str) -> str:
    asset = store.save_upload(
        project_id,
        filename="producto.png",
        content_type="image/png",
        data=_PNG_1PX,
        max_upload_bytes=2 * 1024 * 1024,
        category="uploads",
    )
    return str(asset["id"])


def _save_video(store: ProjectStore, project_id: str) -> str:
    asset = store.save_upload(
        project_id,
        filename="clip.mp4",
        content_type="video/mp4",
        data=_FAKE_MP4,
        max_upload_bytes=2 * 1024 * 1024,
        category="uploads",
    )
    return str(asset["id"])


def _save_audio(store: ProjectStore, project_id: str) -> str:
    asset = store.save_upload(
        project_id,
        filename="voz.wav",
        content_type="audio/wav",
        data=_FAKE_WAV,
        max_upload_bytes=2 * 1024 * 1024,
        category="uploads",
    )
    return str(asset["id"])


def _image_seg(path: Path, duration_s: float, asset_id: str = "img1") -> _Segment:
    return _Segment(path=path, kind="image", duration_s=duration_s, asset_id=asset_id)


def _filter_complex(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def _has_subseq(seq: list[str], sub: list[str]) -> bool:
    n = len(sub)
    return any(list(seq[i : i + n]) == sub for i in range(len(seq) - n + 1))


# ---------------------------------------------------------------------------
# _ffmpeg_variant_cmd: entradas, gráficos de audio y cola de mapeo
# ---------------------------------------------------------------------------


def test_ffmpeg_cmd_image_inputs_no_audio_exact(tmp_path: Path) -> None:
    """Argv completo para 2 imágenes sin audio: -loop 1, filtros y "-an" exactos."""
    img1 = tmp_path / "uno.png"
    img2 = tmp_path / "dos.png"
    out = tmp_path / "out.mp4"
    segs = [_image_seg(img1, 3.0, "a1"), _image_seg(img2, 2.5, "a2")]
    fc = ";".join(
        [
            "[0:v]scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,fps=24,format=yuv420p[v0]",
            "[1:v]scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,fps=24,format=yuv420p[v1]",
            "[v0][v1]concat=n=2:v=1:a=0[vout]",
        ]
    )
    cmd = _ffmpeg_variant_cmd(
        segs,
        width=720,
        height=1280,
        fps=24,
        out_path=out,
        voice_path=None,
        music_path=None,
        total_duration_s=5.5,
    )
    assert cmd == [
        "ffmpeg_bin_placeholder",
        "-y",
        "-v",
        "error",
        "-loop",
        "1",
        "-t",
        "3.000",
        "-i",
        str(img1),
        "-loop",
        "1",
        "-t",
        "2.500",
        "-i",
        str(img2),
        "-filter_complex",
        fc,
        "-map",
        "[vout]",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        "-t",
        "5.500",
        str(out),
    ]


@pytest.mark.parametrize(
    ("media_duration_s", "expected_t"),
    [
        (0.0, "6.000"),  # sin duración de medios -> sin clamp
        (4.0, "4.000"),  # medio más corto que el plano -> clamp al medio
        (10.0, "6.000"),  # medio más largo que el plano -> duración del plano
    ],
)
def test_ffmpeg_cmd_video_input_clamps_to_media_duration(
    tmp_path: Path, media_duration_s: float, expected_t: str
) -> None:
    clip = tmp_path / "clip.mp4"
    out = tmp_path / "out.mp4"
    seg = _Segment(
        path=clip,
        kind="video",
        duration_s=6.0,
        asset_id="v1",
        media_duration_s=media_duration_s,
    )
    cmd = _ffmpeg_variant_cmd(
        [seg],
        width=1280,
        height=720,
        fps=24,
        out_path=out,
        voice_path=None,
        music_path=None,
        total_duration_s=6.0,
    )
    # Entrada de vídeo: -ss 0 -t <dur clampada> -i <path> en las posiciones 4..9.
    assert cmd[4:10] == ["-ss", "0", "-t", expected_t, "-i", str(clip)]
    assert "-loop" not in cmd  # el truco de "-loop 1" es sólo para imágenes


def test_ffmpeg_cmd_voice_only_uses_anull_and_shortest(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    voice = tmp_path / "ad_voice.wav"
    out = tmp_path / "out.mp4"
    cmd = _ffmpeg_variant_cmd(
        [_image_seg(img, 3.0)],
        width=720,
        height=720,
        fps=30,
        out_path=out,
        voice_path=voice,
        music_path=None,
        total_duration_s=3.0,
    )
    fc = _filter_complex(cmd)
    # La voz entra después de la imagen (input idx 1) y su `-i` está en el argv.
    assert cmd.index(str(img)) < cmd.index(str(voice))
    assert cmd[cmd.index(str(voice)) - 1] == "-i"
    # The resampler output is present in filter_complex and feeds the audio passthrough.
    assert "[1:a]aresample=44100[a_voice]" in fc
    assert "[a_voice]anull[aout]" in fc
    assert "amix" not in fc and "a_music" not in fc
    assert _has_subseq(
        cmd, ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    )
    assert "-an" not in cmd


def test_ffmpeg_cmd_music_only_uses_anull(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    music = tmp_path / "ad_music.wav"
    out = tmp_path / "out.mp4"
    cmd = _ffmpeg_variant_cmd(
        [_image_seg(img, 3.0)],
        width=720,
        height=720,
        fps=30,
        out_path=out,
        voice_path=None,
        music_path=music,
        total_duration_s=3.0,
    )
    fc = _filter_complex(cmd)
    assert cmd.index(str(img)) < cmd.index(str(music))
    assert cmd[cmd.index(str(music)) - 1] == "-i"
    # The music resampler output is present and feeds the audio passthrough.
    assert "[1:a]aresample=44100[a_music]" in fc
    assert "[a_music]anull[aout]" in fc
    assert "a_voice" not in fc and "amix" not in fc
    assert _has_subseq(
        cmd, ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    )
    assert "-an" not in cmd


def test_ffmpeg_cmd_voice_and_music_uses_amix(tmp_path: Path) -> None:
    img1 = tmp_path / "a.png"
    img2 = tmp_path / "b.png"
    voice = tmp_path / "ad_voice.wav"
    music = tmp_path / "ad_music.wav"
    out = tmp_path / "out.mp4"
    cmd = _ffmpeg_variant_cmd(
        [_image_seg(img1, 3.0, "a1"), _image_seg(img2, 2.0, "a2")],
        width=720,
        height=1280,
        fps=24,
        out_path=out,
        voice_path=voice,
        music_path=music,
        total_duration_s=5.0,
    )
    fc = _filter_complex(cmd)
    # Índices de audio tras las 2 entradas de imagen: voz=2, música=3.
    assert (
        cmd.index(str(img1))
        < cmd.index(str(img2))
        < cmd.index(str(voice))
        < cmd.index(str(music))
    )
    # Both input resamplers must be in the same graph as the mixer.
    assert "[2:a]aresample=44100[a_voice]" in fc
    assert "[3:a]aresample=44100[a_music]" in fc
    assert (
        "[a_voice][a_music]amix=inputs=2:duration=shortest:dropout_transition=0[aout]"
        in fc
    )
    assert "anull" not in fc
    assert _has_subseq(
        cmd, ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    )
    assert "-an" not in cmd


# ---------------------------------------------------------------------------
# _resolve_segments: errores tipados y resolución de assets
# ---------------------------------------------------------------------------


def test_resolve_segments_images_never_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    img_id = _save_image(store, project["id"])

    def _boom(path: Path) -> None:
        raise AssertionError(f"no debe sondearse una imagen: {path}")

    monkeypatch.setattr(ad_pipeline, "probe_media_info", _boom)
    segments = _resolve_segments(
        store,
        project["id"],
        _storyboard([img_id]),
        source_asset_id=None,
        chunk_asset_ids=None,
    )
    assert len(segments) == 4  # 4 planos del guion, round-robin sobre el asset
    assert all(s.kind == "image" for s in segments)
    assert all(s.media_duration_s == 0.0 for s in segments)
    assert all(s.path.is_file() for s in segments)
    assert [s.asset_id for s in segments] == [img_id] * 4


def test_resolve_segments_chunk_count_mismatch_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    img_id = _save_image(store, project["id"])
    with pytest.raises(AdPipelineError, match=r"4 planos, 2 assets"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([img_id]),
            source_asset_id=None,
            chunk_asset_ids=[img_id, img_id],
        )


def test_resolve_segments_missing_asset_id_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    with pytest.raises(AdPipelineError, match=r"no tiene asset de video/imagen asignado"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([]),
            source_asset_id=None,
            chunk_asset_ids=None,
        )


def test_resolve_segments_unknown_asset_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    with pytest.raises(AdPipelineError, match=r"Asset no encontrado: 'noexiste'"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([]),
            source_asset_id="noexiste",
            chunk_asset_ids=None,
        )


def test_resolve_segments_rejects_non_video_image_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    audio_id = _save_audio(store, project["id"])
    with pytest.raises(AdPipelineError, match=r"no es video ni imagen \(kind='audio'\)"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([audio_id]),
            source_asset_id=None,
            chunk_asset_ids=None,
        )


def test_resolve_segments_missing_physical_file_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    img_id = _save_image(store, project["id"])
    path = store.get_asset_path(project["id"], img_id)
    assert path is not None
    path.unlink()  # el asset sigue en la BD pero el archivo desaparece
    with pytest.raises(AdPipelineError, match=r"no tiene archivo físico"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([img_id]),
            source_asset_id=None,
            chunk_asset_ids=None,
        )


def test_resolve_segments_dangling_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cubre la segunda mitad de `path is None or not path.is_file()` (pipeline.py:230)."""
    store = _store(tmp_path)
    project = store.create_project(name="P")
    img_id = _save_image(store, project["id"])

    def _dangling(self: ProjectStore, project_id: str, asset_id: str) -> Path:
        return tmp_path / "ya_no_existe.png"  # path no None pero inexistente

    monkeypatch.setattr(ProjectStore, "get_asset_path", _dangling)
    with pytest.raises(AdPipelineError, match=r"no tiene archivo físico"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([img_id]),
            source_asset_id=None,
            chunk_asset_ids=None,
        )


def test_resolve_segments_video_probe_sets_media_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    video_id = _save_video(store, project["id"])
    probe_calls: list[tuple[Path, dict[str, Any]]] = []

    def _fake_probe(path: Path, **kwargs: Any) -> dict[str, Any]:
        probe_calls.append((Path(path), dict(kwargs)))
        return {
            "probe_ok": True,
            "duration_sec": 3.25,
            "width": 320,
            "height": 180,
            "has_video": True,
            "has_audio": False,
        }

    monkeypatch.setattr(ad_pipeline, "probe_media_info", _fake_probe)
    segments = _resolve_segments(
        store,
        project["id"],
        _storyboard([video_id]),
        source_asset_id=None,
        chunk_asset_ids=None,
    )
    assert len(segments) == 4
    assert all(s.kind == "video" for s in segments)
    assert all(s.media_duration_s == 3.25 for s in segments)
    assert all(s.duration_s > 0.0 for s in segments)
    assert len(probe_calls) == 4
    # Comportamiento actual (pipeline.py:233): la sonda de entrada va sin timeout_s.
    assert all(kwargs == {} for _, kwargs in probe_calls)
    assert probe_calls[0][0] == segments[0].path


def test_resolve_segments_video_probe_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    video_id = _save_video(store, project["id"])

    def _fake_probe(path: Path, **kwargs: Any) -> dict[str, Any]:
        return {"probe_ok": False, "duration_sec": 0.0}

    monkeypatch.setattr(ad_pipeline, "probe_media_info", _fake_probe)
    with pytest.raises(AdPipelineError, match=r"sondear el video del asset"):
        _resolve_segments(
            store,
            project["id"],
            _storyboard([video_id]),
            source_asset_id=None,
            chunk_asset_ids=None,
        )


def test_resolve_segments_chunk_overrides_shots_and_source_fallback(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project = store.create_project(name="P")
    img_a = _save_image(store, project["id"])
    img_b = _save_image(store, project["id"])
    # chunk_asset_ids manda sobre el asset_id de cada plano.
    segs_chunk = _resolve_segments(
        store,
        project["id"],
        _storyboard([img_a]),
        source_asset_id=None,
        chunk_asset_ids=[img_b] * 4,
    )
    assert [s.asset_id for s in segs_chunk] == [img_b] * 4
    # Sin assets en los planos, source_asset_id es el fallback.
    storyboard = _storyboard([])
    segs_src = _resolve_segments(
        store,
        project["id"],
        storyboard,
        source_asset_id=img_a,
        chunk_asset_ids=None,
    )
    assert [s.asset_id for s in segs_src] == [img_a] * 4
    # duration_s siempre proviene del storyboard (no del medio).
    assert [s.duration_s for s in segs_src] == [sh.duration_s for sh in storyboard.shots]


# ---------------------------------------------------------------------------
# _render_variant: placeholder, reintentos y errores tipados
# ---------------------------------------------------------------------------


def _fake_subprocess(run: Any) -> SimpleNamespace:
    return SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


def test_render_variant_replaces_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = tmp_path / "a.png"
    out = tmp_path / "out.mp4"
    segs = [_image_seg(img, 3.0)]
    seen: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.append(list(cmd))
        out.write_bytes(b"mp4-bytes")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(ad_pipeline, "subprocess", _fake_subprocess(_run))
    _render_variant(
        "/fake/ffmpeg",
        segs,
        aspect_ratio="9:16",
        fps=24,
        out_path=out,
        voice_path=None,
        music_path=None,
        total_duration_s=3.0,
        timeout_s=30.0,
        max_retries=0,
        redactor=SecretRedactor(),
    )
    assert len(seen) == 1
    expected = _ffmpeg_variant_cmd(
        segs,
        width=720,
        height=1280,
        fps=24,
        out_path=out,
        voice_path=None,
        music_path=None,
        total_duration_s=3.0,
    )
    # El placeholder del primer elemento se sustituye por el binario real.
    assert seen[0] == ["/fake/ffmpeg", *expected[1:]]
    assert out.is_file() and out.stat().st_size > 0


def test_render_variant_failure_is_typed_after_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = tmp_path / "a.png"
    out = tmp_path / "out.mp4"
    attempts: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        attempts.append(list(cmd))
        return SimpleNamespace(returncode=1, stderr="boom")

    monkeypatch.setattr(ad_pipeline, "subprocess", _fake_subprocess(_run))
    with pytest.raises(AdPipelineError, match=r"9:16 falló tras 2 intento\(s\): boom"):
        _render_variant(
            "/fake/ffmpeg",
            [_image_seg(img, 3.0)],
            aspect_ratio="9:16",
            fps=24,
            out_path=out,
            voice_path=None,
            music_path=None,
            total_duration_s=3.0,
            timeout_s=30.0,
            max_retries=1,  # 2 intentos en total
            redactor=SecretRedactor(),
        )
    assert len(attempts) == 2
    assert all(a[0] == "/fake/ffmpeg" for a in attempts)


def test_render_variant_oserror_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = tmp_path / "a.png"
    out = tmp_path / "out.mp4"

    def _run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        raise OSError("ffmpeg fantasma no existe")

    monkeypatch.setattr(ad_pipeline, "subprocess", _fake_subprocess(_run))
    with pytest.raises(AdPipelineError, match=r"no se pudo lanzar ffmpeg"):
        _render_variant(
            "/fake/ffmpeg",
            [_image_seg(img, 3.0)],
            aspect_ratio="16:9",
            fps=24,
            out_path=out,
            voice_path=None,
            music_path=None,
            total_duration_s=3.0,
            timeout_s=30.0,
            max_retries=0,
            redactor=SecretRedactor(),
        )


def test_render_variant_timeout_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = tmp_path / "a.png"
    out = tmp_path / "out.mp4"
    calls = {"n": 0}

    def _run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        out.write_bytes(b"mp4-bytes")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(ad_pipeline, "subprocess", _fake_subprocess(_run))
    _render_variant(
        "/fake/ffmpeg",
        [_image_seg(img, 3.0)],
        aspect_ratio="16:9",
        fps=24,
        out_path=out,
        voice_path=None,
        music_path=None,
        total_duration_s=3.0,
        timeout_s=30.0,
        max_retries=1,
        redactor=SecretRedactor(),
    )
    assert calls["n"] == 2  # 1 timeout + 1 reintento exitoso
    assert out.is_file() and out.stat().st_size > 0
