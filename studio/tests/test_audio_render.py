"""Ensamble FFmpeg real de stems causales a partir de muestras del proyecto (A20/C25).

TDD Task 2 del plan 2026-11-12-ultra-nongpu-closure: la ruta debe ensamblar
muestras de audio PROPIAS del proyecto en stems separados con FFmpeg acotado,
alineando onsets al timebase, y medir en la SALIDA sample rate, clipping,
loudness y true peak. Borrar una causa elimina solo sus renders/eventos; los
stems ajenos se conservan. Sin audio generado ni separación por modelo.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.audio import (
    MAX_SAMPLE_DURATION_S,
    MAX_STEM_EVENTS,
    TARGET_SAMPLE_RATE,
    AudioError,
    Mix,
    SampleValidationError,
    SoundEvent,
    Stem,
    build_stem_assembly_cmd,
    frame_aligned_delay_ms,
    mix_digest,
    validate_render_bounds,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

FF_REQUIRED = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe requeridos para el ensamblador real de stems",
)


def _make_sample(path: Path, *, freq: int = 440, seconds: float = 1.0, gain: float = 4.0) -> None:
    """Muestra WAV de prueba: seno con amplitud moderada (no clipea)."""
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-af",
            f"volume={gain},aformat=sample_rates=48000:channel_layouts=stereo",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _make_hot_sample(path: Path, *, seconds: float = 1.0) -> None:
    """Muestra que CLIPEA (true peak > techo) al ensamblarla tal cual."""
    _make_sample(path, freq=440, seconds=seconds, gain=24.0)


def _mix_with_samples(sample_a: str, sample_b: str) -> Mix:
    foley = Stem(
        name="foley",
        events=[
            SoundEvent(
                "step-1", onset_s=0.5, caused_by="walk", sample_asset_id=sample_a,
                sample_license="user-owned", sample_attribution="Provided by project owner",
            ),
            SoundEvent(
                "door-1", onset_s=1.5, caused_by="door", sample_asset_id=sample_b,
                sample_license="user-owned", sample_attribution="Provided by project owner",
            ),
        ],
    )
    ambience = Stem(
        name="ambience",
        events=[SoundEvent(
            "wind", onset_s=0.0, sample_asset_id=sample_a,
            sample_license="user-owned", sample_attribution="Provided by project owner",
        )],
    )
    dialogue = Stem(name="dialogue", events=[SoundEvent("line-1", onset_s=1.0)])
    return Mix(fps=24, stems=[foley, ambience, dialogue], duration_s=3.0)


# --- Unidades puras (sin ffmpeg) -------------------------------------------------


def test_frame_aligned_delay_snaps_onset_to_timebase_frame_grid() -> None:
    """El onset se alinea al grid de frames del timebase compartido (A20)."""
    assert frame_aligned_delay_ms(0.5, 24) == (500, 12)
    # 0.502 s @24fps cae en el frame 12 -> 500 ms, no 502 ms.
    assert frame_aligned_delay_ms(0.502, 24) == (500, 12)
    assert frame_aligned_delay_ms(0.0, 30) == (0, 0)


def test_validate_render_bounds_rejects_too_many_events() -> None:
    stem = Stem(
        name="foley",
        events=[
            SoundEvent(f"e{i}", onset_s=float(i) / 10, sample_asset_id="a1")
            for i in range(MAX_STEM_EVENTS + 1)
        ],
    )
    mix = Mix(fps=24, stems=[stem], duration_s=10.0)
    with pytest.raises(AudioError, match="demasiados eventos"):
        validate_render_bounds(mix)


def test_validate_render_bounds_rejects_undeclared_or_excessive_duration() -> None:
    with pytest.raises(AudioError, match="duration_s"):
        validate_render_bounds(Mix(fps=24, duration_s=0.0))
    stem = Stem(name="foley", events=[SoundEvent("e0", onset_s=0.0, sample_asset_id="a1")])
    with pytest.raises(AudioError, match="demasiado larga"):
        validate_render_bounds(Mix(fps=24, stems=[stem], duration_s=10_000.0))


def test_validate_render_bounds_rejects_single_mixed_stem() -> None:
    stem = Stem(name="mixed", events=[SoundEvent("e0", onset_s=0.0, sample_asset_id="a1")])
    mix = Mix(fps=24, stems=[stem], duration_s=3.0)
    with pytest.raises(AudioError, match="no acredita separación"):
        validate_render_bounds(mix)


def test_validate_render_bounds_requires_license_and_attribution_for_samples() -> None:
    mix = Mix(
        fps=24,
        stems=[Stem(name="foley", events=[SoundEvent("e0", 0.0, sample_asset_id="a1")])],
        duration_s=3.0,
    )

    with pytest.raises(AudioError, match="licencia y atribución"):
        validate_render_bounds(mix)


def test_sound_event_preserves_user_declared_source_rights() -> None:
    mix = Mix(
        fps=24,
        stems=[
            Stem(
                name="foley",
                events=[
                    SoundEvent(
                        "e0",
                        0.0,
                        sample_asset_id="a1",
                        sample_license="CC0-1.0",
                        sample_attribution="Source: project Foley library",
                    )
                ],
            )
        ],
        duration_s=3.0,
    )

    restored = Mix.from_dict(mix.to_dict())
    event = restored.stems[0].events[0]
    assert event.sample_license == "CC0-1.0"
    assert event.sample_attribution == "Source: project Foley library"


def test_build_stem_assembly_cmd_is_deterministic_and_bounded() -> None:
    mix = _mix_with_samples("a1", "b2")
    stem = mix.stems[0]  # foley: a1@0.5s, b2@1.5s
    cmd = build_stem_assembly_cmd(
        stem,
        {"a1": "/proj/uploads/a1.wav", "b2": "/proj/uploads/b2.wav"},
        Path("/tmp/out/stem_foley.wav"),
        fps=24,
        duration_s=3.0,
    )
    assert cmd[0] == FFMPEG or cmd[0] == "ffmpeg"
    joined = " ".join(cmd)
    assert "/proj/uploads/a1.wav" in joined and "/proj/uploads/b2.wav" in joined
    assert "adelay=500:all=1" in joined  # onset 0.5s @24fps -> frame 12 -> 500ms
    assert "adelay=1500:all=1" in joined
    assert "amix=inputs=2:normalize=0:dropout_transition=0" in joined
    assert "apad=whole_dur=3.000" in joined
    assert "-ar" in cmd and "48000" in cmd  # sample rate fijo y verificado
    assert "-ac" in cmd and "2" in cmd
    assert "-bitexact" in cmd  # salida reproducible byte a byte
    assert "-t" in cmd and "3.000" in cmd  # acotado por la duración declarada
    assert "pcm_s16le" in joined
    assert cmd[-1] == "/tmp/out/stem_foley.wav"
    # Determinista: dos construcciones idénticas producen el mismo argv.
    again = build_stem_assembly_cmd(
        stem,
        {"a1": "/proj/uploads/a1.wav", "b2": "/proj/uploads/b2.wav"},
        Path("/tmp/out/stem_foley.wav"),
        fps=24,
        duration_s=3.0,
    )
    assert cmd == again


def test_sound_event_sample_reference_round_trips_and_old_metadata_still_loads() -> None:
    mix = _mix_with_samples("a1", "b2")
    data = mix.to_dict()
    restored = Mix.from_dict(data)
    assert restored.to_dict() == data
    assert restored.stems[0].events[0].sample_asset_id == "a1"
    # Contrato antiguo metadata-only: dict sin sample_asset_id sigue cargando.
    legacy = {
        "schema": "audio-mix/1",
        "fps": 24,
        "duration_s": 3.0,
        "stems": {"foley": [{"event_id": "e0", "onset_s": 0.5, "caused_by": "walk"}]},
    }
    old = Mix.from_dict(legacy)
    assert old.stems[0].events[0].sample_asset_id is None


def test_mix_digest_is_deterministic_and_edit_sensitive() -> None:
    mix = _mix_with_samples("a1", "b2")
    first = mix_digest(mix)
    assert first == mix_digest(_mix_with_samples("a1", "b2"))
    mix.stems[0].events[0].onset_s = 0.6
    assert mix_digest(mix) != first


# --- Ensamble real con ffmpeg -----------------------------------------------------


def _render_kwargs(tmp_path: Path) -> dict:
    return {
        "ffmpeg_bin": FFMPEG,
        "ffprobe_bin": FFPROBE,
        "timeout_s": 60.0,
    }


@FF_REQUIRED
def test_render_stems_assembles_project_samples_into_measured_separate_stems(
    tmp_path: Path,
) -> None:
    sample_a = tmp_path / "sample_a.wav"
    sample_b = tmp_path / "sample_b.wav"
    _make_sample(sample_a, freq=440)
    _make_sample(sample_b, freq=880)
    mix = _mix_with_samples("a1", "b2")
    samples = {"a1": sample_a, "b2": sample_b}

    from comfy_studio.audio import render_stems

    rendered = render_stems(mix, samples, tmp_path / "out", **_render_kwargs(tmp_path))

    names = {r.stem for r in rendered}
    assert names == {"foley", "ambience"}  # dialogue no tiene muestras: no se rinde

    by_name = {r.stem: r for r in rendered}
    foley = by_name["foley"]
    assert foley.event_ids == ["step-1", "door-1"]
    assert foley.sample_rate == TARGET_SAMPLE_RATE == 48000
    assert foley.channels == 2
    assert abs(foley.duration_s - 3.0) < 0.1
    assert foley.integrated_loudness_lufs <= -0.1  # valor medido finito y razonable
    assert foley.true_peak_dbfs < -0.1
    assert foley.clipped is False
    assert foley.frames == {"step-1": 12, "door-1": 36}

    # Verificación independiente con ffprobe de lo que el código declara.
    for rendered_stem in rendered:
        probe = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-show_entries",
                "stream=sample_rate,channels,codec_name",
                "-of",
                "json",
                str(rendered_stem.path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        import json

        stream = json.loads(probe.stdout)["streams"][0]
        assert stream["codec_name"] == "pcm_s16le"
        assert int(stream["sample_rate"]) == 48000
        assert int(stream["channels"]) == 2
        # Stems separados: archivos distintos, no una mezcla única.
    assert by_name["foley"].path.read_bytes() != by_name["ambience"].path.read_bytes()


@FF_REQUIRED
def test_render_stems_is_byte_deterministic(tmp_path: Path) -> None:
    sample_a = tmp_path / "sample_a.wav"
    _make_sample(sample_a, freq=440)
    mix = _mix_with_samples("a1", "a1")

    from comfy_studio.audio import render_stems

    first = render_stems(mix, {"a1": sample_a}, tmp_path / "out1", **_render_kwargs(tmp_path))
    second = render_stems(mix, {"a1": sample_a}, tmp_path / "out2", **_render_kwargs(tmp_path))
    digest = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731
    assert digest(first[0].path.read_bytes()) == digest(second[0].path.read_bytes())


@FF_REQUIRED
def test_render_stems_rejects_clipped_output(tmp_path: Path) -> None:
    hot = tmp_path / "hot.wav"
    _make_hot_sample(hot)
    mix = _mix_with_samples("a1", "a1")

    from comfy_studio.audio import render_stems

    with pytest.raises(AudioError, match="true peak"):
        render_stems(mix, {"a1": hot}, tmp_path / "out", **_render_kwargs(tmp_path))


@FF_REQUIRED
def test_verify_rendered_stem_rejects_sample_rate_mismatch(tmp_path: Path) -> None:
    odd = tmp_path / "odd.wav"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "22050",
            str(odd),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    from comfy_studio.audio import verify_rendered_stem

    with pytest.raises(AudioError, match="sample rate"):
        verify_rendered_stem(
            odd,
            expected_sample_rate=48000,
            ffmpeg_bin=FFMPEG,
            ffprobe_bin=FFPROBE,
            timeout_s=60.0,
        )


@FF_REQUIRED
def test_render_stems_rejects_unreadable_sample(tmp_path: Path) -> None:
    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"esto no es audio")
    mix = _mix_with_samples("a1", "a1")

    from comfy_studio.audio import render_stems

    with pytest.raises(SampleValidationError, match="no es audio legible"):
        render_stems(mix, {"a1": fake}, tmp_path / "out", **_render_kwargs(tmp_path))


@FF_REQUIRED
def test_render_stems_rejects_sample_longer_than_bound(tmp_path: Path) -> None:
    long_sample = tmp_path / "long.wav"
    _make_sample(long_sample, freq=440, seconds=MAX_SAMPLE_DURATION_S + 1)
    mix = _mix_with_samples("a1", "a1")

    from comfy_studio.audio import render_stems

    with pytest.raises(SampleValidationError, match="demasiado larga"):
        render_stems(mix, {"a1": long_sample}, tmp_path / "out", **_render_kwargs(tmp_path))


# --- Ruta HTTP --------------------------------------------------------------------


def _upload_sample(client: TestClient, project_id: str, path: Path) -> dict:
    response = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": (path.name, path.read_bytes(), "audio/wav")},
        data={"category": "uploads"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _put_mix(client: TestClient, project_id: str, payload: dict):
    return client.put(f"/studio/api/projects/{project_id}/sound/mix", json=payload)


def _render_url(project_id: str) -> str:
    return f"/studio/api/projects/{project_id}/sound/render"


def _stems_url(project_id: str) -> str:
    return f"/studio/api/projects/{project_id}/sound/stems"


@FF_REQUIRED
def test_render_endpoint_produces_measured_separate_stem_assets(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Stems reales"}).json()["id"]
    sample_a = tmp_path / "sample_a.wav"
    sample_b = tmp_path / "sample_b.wav"
    _make_sample(sample_a, freq=440)
    _make_sample(sample_b, freq=880)
    asset_a = _upload_sample(client, project_id, sample_a)
    asset_b = _upload_sample(client, project_id, sample_b)

    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
            "foley": [
                {
                    "event_id": "step-1",
                    "onset_s": 0.5,
                    "caused_by": "walk",
                    "sample_asset_id": asset_a["id"],
                    "sample_license": "CC0-1.0",
                    "sample_attribution": "Project Foley library",
                },
                {
                    "event_id": "door-1",
                    "onset_s": 1.5,
                    "caused_by": "door",
                    "sample_asset_id": asset_b["id"],
                    "sample_license": "CC-BY-4.0",
                    "sample_attribution": "Example sound by A. Artist",
                },
            ],
            "ambience": [{
                "event_id": "wind",
                "onset_s": 0.0,
                "sample_asset_id": asset_a["id"],
                "sample_license": "CC0-1.0",
                "sample_attribution": "Project Foley library",
            }],
            "dialogue": [{"event_id": "line-1", "onset_s": 1.0}],
        },
    }
    assert _put_mix(client, project_id, payload).status_code == 200

    rendered = client.post(_render_url(project_id))
    assert rendered.status_code == 200, rendered.text
    body = rendered.json()
    assert body["audio_generation"] == "ffmpeg_assembled"
    assert body["separation_claim"] == "rendered_stems"
    assert body["simulated"] is False
    assert body["engine"] == "ffmpeg"
    stems = {stem["stem"]: stem for stem in body["stems"]}
    assert set(stems) == {"foley", "ambience"}
    assert body["skipped_stems"] == ["dialogue"]

    foley = stems["foley"]
    assert foley["event_ids"] == ["step-1", "door-1"]
    assert foley["sample_rate"] == 48000
    assert foley["clipped"] is False
    assert isinstance(foley["integrated_loudness_lufs"], float)
    assert isinstance(foley["true_peak_dbfs"], float)
    assert body["mix_digest"]

    # Cada stem es un asset de audio real del proyecto, distinto y verificable.
    for name, stem in stems.items():
        asset = client.get(f"/studio/api/projects/{project_id}/assets/{stem['asset_id']}").json()
        assert asset["kind"] == "audio"
        assert asset["metadata"]["stem"] == name
        assert asset["metadata"]["rendered_by"] == "ffmpeg"
        assert asset["metadata"]["rights_state"] == "user_declared_not_independently_verified"
        assert asset["metadata"]["source_rights"]
        probe = client.get(f"/studio/api/projects/{project_id}/assets/{stem['asset_id']}/file")
        assert probe.status_code == 200
    foley_asset = client.get(
        f"/studio/api/projects/{project_id}/assets/{stems['foley']['asset_id']}"
    ).json()
    assert foley_asset["metadata"]["source_rights"] == sorted([
        {
            "sample_asset_id": asset_a["id"],
            "license": "CC0-1.0",
            "attribution": "Project Foley library",
        },
        {
            "sample_asset_id": asset_b["id"],
            "license": "CC-BY-4.0",
            "attribution": "Example sound by A. Artist",
        },
    ], key=lambda rights: rights["sample_asset_id"])

    # El índice de stems muestra los renders no obsoletos.
    index = client.get(_stems_url(project_id)).json()
    assert {entry["stem"] for entry in index["stems"]} == {"foley", "ambience"}
    assert all(entry["stale"] is False for entry in index["stems"])


@FF_REQUIRED
def test_render_endpoint_is_deterministic_across_runs(client: TestClient, tmp_path: Path) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Stems idempotentes"}).json()[
        "id"
    ]
    sample_a = tmp_path / "sample_a.wav"
    _make_sample(sample_a, freq=440)
    asset_a = _upload_sample(client, project_id, sample_a)
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
            "foley": [
                {
                    "event_id": "e0", "onset_s": 0.5, "sample_asset_id": asset_a["id"],
                    "sample_license": "user-owned", "sample_attribution": "Project owner upload",
                },
                {
                    "event_id": "e1", "onset_s": 1.5, "sample_asset_id": asset_a["id"],
                    "sample_license": "user-owned", "sample_attribution": "Project owner upload",
                },
            ]
        },
    }
    assert _put_mix(client, project_id, payload).status_code == 200
    first = client.post(_render_url(project_id)).json()
    second = client.post(_render_url(project_id)).json()
    first_assets = {s["stem"]: s["asset_id"] for s in first["stems"]}
    second_assets = {s["stem"]: s["asset_id"] for s in second["stems"]}
    a = client.get(f"/studio/api/projects/{project_id}/assets/{first_assets['foley']}/file")
    b = client.get(f"/studio/api/projects/{project_id}/assets/{second_assets['foley']}/file")
    assert hashlib.sha256(a.content).hexdigest() == hashlib.sha256(b.content).hexdigest()


@FF_REQUIRED
def test_render_endpoint_rejects_sample_without_declared_rights(
    client: TestClient, tmp_path: Path
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Sin licencia"}).json()["id"]
    sample = tmp_path / "sample.wav"
    _make_sample(sample)
    asset = _upload_sample(client, project_id, sample)
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
            "foley": [{"event_id": "e0", "onset_s": 0.5, "sample_asset_id": asset["id"]}]
        },
    }
    assert _put_mix(client, project_id, payload).status_code == 200

    response = client.post(_render_url(project_id))

    assert response.status_code == 422
    assert "licencia y atribución" in response.json()["detail"]
    assert client.get(_stems_url(project_id)).status_code == 404


def test_render_endpoint_rejects_sample_from_another_project(
    client: TestClient,
    tmp_path: Path,
) -> None:
    other_project = client.post("/studio/api/projects", json={"name": "Otro"}).json()["id"]
    sample = tmp_path / "sample_a.wav"
    _make_sample(sample, freq=440)
    foreign = _upload_sample(client, other_project, sample)

    project_id = client.post("/studio/api/projects", json={"name": "Dueño"}).json()["id"]
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {"foley": [{"event_id": "e0", "onset_s": 0.5, "sample_asset_id": foreign["id"]}]},
    }
    response = _put_mix(client, project_id, payload)
    assert response.status_code == 400  # fail closed: la muestra no es del proyecto
    assert client.get(_stems_url(project_id)).status_code == 404


def test_render_endpoint_rejects_non_audio_asset(client: TestClient, tmp_path: Path) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Con png"}).json()["id"]
    png = tmp_path / "img.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    response = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("img.png", png.read_bytes(), "image/png")},
        data={"category": "uploads"},
    )
    assert response.status_code == 201
    asset_id = response.json()["id"]
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {"foley": [{"event_id": "e0", "onset_s": 0.5, "sample_asset_id": asset_id}]},
    }
    rejected = _put_mix(client, project_id, payload)
    assert rejected.status_code == 400
    assert "audio" in rejected.json()["detail"].lower()


@FF_REQUIRED
def test_render_endpoint_rejects_clipped_output_and_persists_nothing(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Clipeado"}).json()["id"]
    hot = tmp_path / "hot.wav"
    _make_hot_sample(hot)
    asset = _upload_sample(client, project_id, hot)
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {"foley": [{
            "event_id": "e0", "onset_s": 0.5, "sample_asset_id": asset["id"],
            "sample_license": "user-owned", "sample_attribution": "Project owner upload",
        }]},
    }
    assert _put_mix(client, project_id, payload).status_code == 200
    rendered = client.post(_render_url(project_id))
    assert rendered.status_code == 503
    assert "true peak" in rendered.json()["detail"].lower()
    # Nada queda persistido como stem renderizado.
    index = client.get(_stems_url(project_id))
    assert index.status_code == 404


def test_render_endpoint_requires_declared_duration_and_samples(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Sin duración"}).json()["id"]
    sample = tmp_path / "sample_a.wav"
    _make_sample(sample, freq=440)
    asset = _upload_sample(client, project_id, sample)
    no_duration = {
        "fps": 24,
        "duration_s": 0.0,
        "stems": {"foley": [{"event_id": "e0", "onset_s": 0.5, "sample_asset_id": asset["id"]}]},
    }
    assert _put_mix(client, project_id, no_duration).status_code == 200
    response = client.post(_render_url(project_id))
    assert response.status_code == 422
    assert "duration_s" in response.json()["detail"]

    # Contrato metadata-only intacto: sin muestras no hay render.
    empty_project = client.post("/studio/api/projects", json={"name": "Metadata"}).json()["id"]
    assert (
        _put_mix(
            client,
            empty_project,
            {
                "fps": 24,
                "duration_s": 3.0,
                "stems": {"foley": [{"event_id": "e0", "onset_s": 0.5}]},
            },
        ).status_code
        == 200
    )
    response = client.post(_render_url(empty_project))
    assert response.status_code == 422
    assert "muestra" in response.json()["detail"].lower()


def test_render_endpoint_respects_runtime_output_byte_budget(
    client: TestClient, tmp_path: Path
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Presupuesto"}).json()["id"]
    sample = tmp_path / "sample_a.wav"
    _make_sample(sample, freq=440)
    asset = _upload_sample(client, project_id, sample)
    payload = {
        "fps": 24,
        "duration_s": 100.0,
        "stems": {
                "foley": [{
                    "event_id": "e0", "onset_s": 0.5, "sample_asset_id": asset["id"],
                    "sample_license": "user-owned", "sample_attribution": "Project owner upload",
                }]
        },
    }
    assert _put_mix(client, project_id, payload).status_code == 200
    response = client.post(_render_url(project_id))
    assert response.status_code == 413
    assert "presupuesto" in response.json()["detail"].lower()
    assert client.get(_stems_url(project_id)).status_code == 404


@FF_REQUIRED
def test_delete_cause_removes_only_linked_rendered_stems_and_preserves_unrelated(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Causal renders"}).json()["id"]
    sample_a = tmp_path / "sample_a.wav"
    sample_b = tmp_path / "sample_b.wav"
    _make_sample(sample_a, freq=440)
    _make_sample(sample_b, freq=880)
    asset_a = _upload_sample(client, project_id, sample_a)
    asset_b = _upload_sample(client, project_id, sample_b)
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
            "foley": [
                {
                    "event_id": "step-1",
                    "onset_s": 0.5,
                    "caused_by": "walk",
                    "sample_asset_id": asset_a["id"],
                        "sample_license": "user-owned",
                        "sample_attribution": "Project owner upload",
                },
                {
                    "event_id": "door-1",
                    "onset_s": 1.5,
                    "caused_by": "door",
                    "sample_asset_id": asset_b["id"],
                        "sample_license": "user-owned",
                        "sample_attribution": "Project owner upload",
                },
            ],
                "ambience": [{
                    "event_id": "wind", "onset_s": 0.0, "sample_asset_id": asset_a["id"],
                    "sample_license": "user-owned", "sample_attribution": "Project owner upload",
                }],
        },
    }
    assert _put_mix(client, project_id, payload).status_code == 200
    rendered = client.post(_render_url(project_id)).json()
    stem_assets = {s["stem"]: s["asset_id"] for s in rendered["stems"]}

    # 1) Borrar la causa 'walk' marca el stem 'foley' como obsoleto (sus eventos
    #    cambian) pero NO elimina su render ni toca 'ambience'.
    walk = client.delete(f"/studio/api/projects/{project_id}/sound/causes/walk")
    assert walk.status_code == 200
    walk_body = walk.json()
    assert walk_body["removed_event_ids"] == ["step-1"]
    assert walk_body["removed_stem_asset_ids"] == []
    assert walk_body["stale_stems"] == ["foley"]
    ambience_file = client.get(
        f"/studio/api/projects/{project_id}/assets/{stem_assets['ambience']}/file"
    )
    assert ambience_file.status_code == 200  # el stem ajeno se conserva
    index = client.get(_stems_url(project_id)).json()
    by_stem = {entry["stem"]: entry for entry in index["stems"]}
    assert by_stem["foley"]["stale"] is True
    assert by_stem["ambience"]["stale"] is False

    # 2) Borrar la causa 'door' deja el stem 'foley' sin eventos: su render se
    #    elimina; 'ambience' (sin causa vinculada) sigue intacto con su evento.
    door = client.delete(f"/studio/api/projects/{project_id}/sound/causes/door")
    assert door.status_code == 200
    door_body = door.json()
    assert door_body["removed_event_ids"] == ["door-1"]
    assert door_body["removed_stem_asset_ids"] == [stem_assets["foley"]]
    assert (
        client.get(
            f"/studio/api/projects/{project_id}/assets/{stem_assets['foley']}/file"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/studio/api/projects/{project_id}/assets/{stem_assets['ambience']}/file"
        ).status_code
        == 200
    )
    final_index = {
        entry["stem"]: entry for entry in client.get(_stems_url(project_id)).json()["stems"]
    }
    assert set(final_index) == {"ambience"}
    mix = client.get(f"/studio/api/projects/{project_id}/sound/mix").json()
    remaining = {event["event_id"] for events in mix["stems"].values() for event in events}
    assert remaining == {"wind"}  # el evento sin causa se conserva


def test_delete_cause_with_no_links_preserves_every_rendered_stem(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Causa inexistente"}).json()[
        "id"
    ]
    sample = tmp_path / "sample_a.wav"
    _make_sample(sample, freq=440)
    asset = _upload_sample(client, project_id, sample)
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
                "ambience": [{
                    "event_id": "wind", "onset_s": 0.0, "sample_asset_id": asset["id"],
                    "sample_license": "user-owned", "sample_attribution": "Project owner upload",
                }]
        },
    }
    assert _put_mix(client, project_id, payload).status_code == 200
    rendered = client.post(_render_url(project_id)).json()
    asset_id = rendered["stems"][0]["asset_id"]
    response = client.delete(f"/studio/api/projects/{project_id}/sound/causes/no-existe")
    assert response.status_code == 200
    body = response.json()
    assert body["removed_event_ids"] == []
    assert body["removed_stem_asset_ids"] == []
    assert body["stale_stems"] == []
    assert (
        client.get(f"/studio/api/projects/{project_id}/assets/{asset_id}/file").status_code == 200
    )
