"""Tests deterministas de `comfy_studio.creative.review` (CPU, sin ffmpeg/red/pesos).

Cubren los helpers puros, `save_review`/`latest_review` (doble store en memoria) y
los caminos de `review_output` que `test_m7_creative_brief.py` no alcanza: el probe
se sustituye por un dict fijo para no depender de ffprobe ni del contenido del archivo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from comfy_studio.creative import review as review_module
from comfy_studio.creative.review import (
    REVIEW_SCHEMA,
    _aspect_of,
    _constraint,
    _expects_audio,
    _parse_aspect,
    _utc_now_iso,
    latest_review,
    review_output,
    save_review,
)

PROJECT = {"id": "p1", "name": "Proyecto de prueba"}
ASSET = {"id": "a1", "filename": "render.mp4", "rel_path": "p1/outputs/render.mp4"}


class FakeStore:
    """Doble mínimo en memoria: solo los métodos que `review.py` invoca."""

    def __init__(
        self,
        *,
        projects: tuple[dict[str, Any], ...] = (),
        assets: tuple[dict[str, Any], ...] = (),
        asset_paths: dict[str, Path] | None = None,
        edl_versions: tuple[dict[str, Any], ...] = (),
        transcriptions: tuple[tuple[str, dict[str, Any]], ...] = (),
    ) -> None:
        self.projects = {p["id"]: p for p in projects}
        self.assets = {a["id"]: a for a in assets}
        self.asset_paths = asset_paths or {}
        self.edl_versions = list(edl_versions)
        self.transcriptions = list(transcriptions)
        self.meta: dict[tuple[str, str], Any] = {}

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.projects.get(project_id)

    def get_asset(self, project_id: str, asset_id: str) -> dict[str, Any] | None:
        return self.assets.get(asset_id)

    def get_asset_path(self, project_id: str, asset_id: str) -> Path | None:
        return self.asset_paths.get(asset_id)

    def list_edl_versions(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.edl_versions)

    def list_transcriptions(
        self, project_id: str, *, asset_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            item
            for source_id, item in self.transcriptions
            if asset_id is None or source_id == asset_id
        ]

    def set_meta(self, project_id: str, key: str, value: Any) -> None:
        self.meta[(project_id, key)] = value

    def get_meta(self, project_id: str, key: str, default: Any = None) -> Any:
        return self.meta.get((project_id, key), default)


def make_brief(**overrides: Any) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "status": "approved",
        "user_constraints": [],
        "editorial": {},
    }
    brief.update(overrides)
    return brief


def make_probe(**overrides: Any) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "probe_ok": True,
        "duration_sec": 15.0,
        "width": 1920,
        "height": 1080,
        "has_video": True,
        "has_audio": True,
        "codec_name": "h264",
        "audio_codec_name": "aac",
    }
    probe.update(overrides)
    return probe


def patch_probe(monkeypatch: pytest.MonkeyPatch, probe: dict[str, Any]) -> list[Path]:
    """Sustituye ffprobe por un dict fijo; devuelve la lista de rutas observadas."""
    seen: list[Path] = []

    def _fake_probe(path: Path | str, **_kwargs: Any) -> dict[str, Any]:
        seen.append(Path(path))
        return dict(probe)

    monkeypatch.setattr(review_module, "probe_media_info", _fake_probe)
    return seen


def make_store(tmp_path: Path | None = None, **kwargs: Any) -> FakeStore:
    store = FakeStore(projects=(PROJECT,), assets=(ASSET,), **kwargs)
    if tmp_path is not None:
        path = tmp_path / "render.mp4"
        path.write_bytes(b"stub-bytes")
        store.asset_paths["a1"] = path
    return store


def run_review(store: FakeStore, brief: dict[str, Any]) -> dict[str, Any]:
    return review_output(store, "p1", brief, "a1")


def issues_of(report: dict[str, Any], check: str) -> list[dict[str, Any]]:
    return [i for i in report["issues"] if i["check"] == check]


# ---------------------------------------------------------------------------
# save_review / latest_review (cero refs previos en tests)
# ---------------------------------------------------------------------------


def test_save_review_persists_report_under_asset_scoped_meta_key() -> None:
    store = FakeStore()
    report = {"schema": REVIEW_SCHEMA, "asset_id": "a1", "summary": {"passed": True}}
    save_review(store, "p1", "a1", report)
    assert store.meta == {("p1", "creative_review:a1"): report}
    # el informe llega intacto a get_meta, como lo leería el router tras un reinicio
    assert store.get_meta("p1", "creative_review:a1") == report


def test_latest_review_roundtrips_saved_report() -> None:
    store = FakeStore()
    report = {"schema": REVIEW_SCHEMA, "asset_id": "a1", "summary": {"passed": False}}
    save_review(store, "p1", "a1", report)
    assert latest_review(store, "p1", "a1") == report


def test_latest_review_returns_none_for_missing_or_non_dict_values() -> None:
    store = FakeStore()
    assert latest_review(store, "p1", "a1") is None
    # valores corruptos/no dict persistidos por una versión previa se descartan
    store.set_meta("p1", "creative_review:a1", ["no", "es", "dict"])
    assert latest_review(store, "p1", "a1") is None
    store.set_meta("p1", "creative_review:a1", "texto suelto")
    assert latest_review(store, "p1", "a1") is None


def test_review_meta_keys_are_scoped_per_project_and_asset() -> None:
    store = FakeStore()
    report_a1 = {"asset_id": "a1", "marker": "p1-a1"}
    report_a2 = {"asset_id": "a2", "marker": "p1-a2"}
    report_p2 = {"asset_id": "a1", "marker": "p2-a1"}
    save_review(store, "p1", "a1", report_a1)
    save_review(store, "p1", "a2", report_a2)
    save_review(store, "p2", "a1", report_p2)
    assert latest_review(store, "p1", "a1") == report_a1
    assert latest_review(store, "p1", "a2") == report_a2
    assert latest_review(store, "p2", "a1") == report_p2


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------


def test_utc_now_iso_is_aware_utc_timestamp() -> None:
    before = datetime.now(UTC)
    stamp = _utc_now_iso()
    after = datetime.now(UTC)
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert before <= parsed <= after


def test_aspect_of_computes_ratio_and_guards_zero_height() -> None:
    assert _aspect_of(16, 9) == 1.7778
    assert _aspect_of(9, 16) == 0.5625
    assert _aspect_of(500, 500) == 1.0
    assert _aspect_of(1920, 0) == 0.0  # sin división por cero
    assert _aspect_of(0, 1080) == 0.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("16:9", 1.7778),
        ("9:16", 0.5625),
        ("1:1", 1.0),
        ("4:3", 1.3333),
        (" 16 : 9 ", 1.7778),  # float() tolera espacios alrededor
        ("16:0", 0.0),  # ZeroDivisionError controlado
        ("16", 0.0),  # sin el separador ":" → ValueError controlado
        ("", 0.0),
        ("ancho:alto", 0.0),
        ("16:9:2", 0.0),  # solo se parte una vez; "9:2" no es número
        ("16x9", 0.0),  # forma común de escribir el aspecto → 0.0 (ver bug abajo)
        ("1.78", 0.0),  # aspecto numérico sin ":" → 0.0 (ver bug abajo)
    ],
)
def test_parse_aspect(text: str, expected: float) -> None:
    assert _parse_aspect(text) == expected


def test_constraint_reads_first_matching_value_and_preserves_falsy() -> None:
    brief = make_brief(
        user_constraints=[
            {"key": "duration_sec", "value": 15},
            {"key": "duration_sec", "value": 30},  # el primero gana
            {"key": "aspect_ratio", "value": "9:16"},
            {"key": "subtitles", "value": False},  # falsy ≠ ausente
        ]
    )
    assert _constraint(brief, "duration_sec") == 15
    assert _constraint(brief, "aspect_ratio") == "9:16"
    assert _constraint(brief, "subtitles") is False
    assert _constraint(brief, "music") is None


def test_constraint_tolerates_missing_or_malformed_constraint_lists() -> None:
    assert _constraint({}, "duration_sec") is None
    assert _constraint({"user_constraints": None}, "duration_sec") is None
    assert _constraint({"user_constraints": ["texto", None, 7]}, "duration_sec") is None
    assert _constraint({"user_constraints": [{"key": "music"}]}, "music") is None


def test_expects_audio_structured_music_policy_wins_over_legacy_text() -> None:
    required = make_brief(
        editorial={"audio_plan": {"music_policy": "required", "music": "sin música"}}
    )
    prohibited = make_brief(
        editorial={"audio_plan": {"music_policy": "prohibited", "music": "music required"}}
    )
    optional = make_brief(
        editorial={"audio_plan": {"music_policy": "optional", "music": "music required"}}
    )
    assert _expects_audio(required) is True
    assert _expects_audio(prohibited) is False
    assert _expects_audio(optional) is False


def test_expects_audio_music_constraint_used_only_when_policy_absent() -> None:
    assert _expects_audio(make_brief(user_constraints=[{"key": "music", "value": True}])) is True
    assert _expects_audio(make_brief(user_constraints=[{"key": "music", "value": False}])) is False
    # política explícita gana sobre la restricción contradictoria
    structured = make_brief(
        user_constraints=[{"key": "music", "value": True}],
        editorial={"audio_plan": {"music_policy": "optional"}},
    )
    assert _expects_audio(structured) is False


@pytest.mark.parametrize(
    ("music_text", "expected"),
    [
        ("sin música", False),
        ("sin musica", False),
        ("no music", False),
        ("música obligatoria", True),
        ("musica obligatoria", True),
        ("music required", True),
        ("banda sonora suave", False),  # legado sin indicio → opcional
        ("", False),
    ],
)
def test_expects_audio_legacy_music_text_resolution(music_text: str, expected: bool) -> None:
    brief = make_brief(editorial={"audio_plan": {"music": music_text}})
    assert _expects_audio(brief) is expected


def test_expects_audio_dialogue_policies_and_negated_labels() -> None:
    required = make_brief(editorial={"audio_plan": {"dialogue_policy": "required"}})
    assert _expects_audio(required) is True
    preserve = make_brief(editorial={"audio_plan": {"dialogue_policy": "preserve_if_present"}})
    assert _expects_audio(preserve) is False
    preserve_with_audio = {
        "dialogue_policy": "preserve_if_present",
        "has_audio_in_sources": True,
    }
    with_source = make_brief(editorial={"audio_plan": preserve_with_audio})
    assert _expects_audio(with_source) is True
    # texto legado "conservar..." ⇒ preserve_if_present con fuentes de audio
    legacy = make_brief(editorial={"audio_plan": {"dialogue": "conservar el audio original"}})
    assert _expects_audio(legacy) is True
    # política "none" explícita gana sobre el texto legado contradictorio
    none_with_legacy = {"dialogue_policy": "none", "dialogue": "conservar el audio original"}
    explicit_none = make_brief(editorial={"audio_plan": none_with_legacy})
    assert _expects_audio(explicit_none) is False
    # la etiqueta negada de música no exige audio por sí sola...
    negated = make_brief(editorial={"audio_plan": {"music": "sin música"}})
    assert _expects_audio(negated) is False
    # ...pero el diálogo sí lo exige aunque la música esté prohibida
    dialogue_wins = make_brief(
        editorial={"audio_plan": {"music_policy": "prohibited", "dialogue_policy": "required"}}
    )
    assert _expects_audio(dialogue_wins) is True
    assert _expects_audio({}) is False
    assert _expects_audio(make_brief()) is False


# ---------------------------------------------------------------------------
# review_output: errores de lookup y de archivo
# ---------------------------------------------------------------------------


def test_review_output_missing_project_raises_lookup_error() -> None:
    store = FakeStore(assets=(ASSET,))
    with pytest.raises(LookupError, match="Proyecto 'p1' no encontrado"):
        review_output(store, "p1", make_brief(), "a1")


def test_review_output_missing_asset_raises_lookup_error() -> None:
    store = FakeStore(projects=(PROJECT,))
    with pytest.raises(LookupError, match="Asset 'a1' no encontrado en el proyecto"):
        review_output(store, "p1", make_brief(), "a1")


def test_review_output_missing_file_reports_existencia_error() -> None:
    store = make_store()  # sin ruta en disco
    report = run_review(store, make_brief())
    issue = issues_of(report, "existencia")[0]
    assert issue["severity"] == "error"
    assert issue["detail"] == "El archivo del asset no existe en disco"
    assert issue["timecode"] is None
    assert issue["correction"] == "re-renderizar desde el EDL correspondiente"
    assert report["technical"] == {"probe_ok": False}
    assert report["summary"]["passed"] is False
    assert report["summary"]["measured"] is False
    assert report["summary"]["errors"] == 1


def test_review_output_probe_failure_reports_integridad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(probe_ok=False, duration_sec=0.0, has_audio=False))
    report = run_review(store, make_brief())
    issue = issues_of(report, "integridad")[0]
    assert issue["severity"] == "error"
    assert "probe_ok=false" in issue["detail"]
    assert issue["correction"] == "re-renderizar con ffmpeg y verificar de nuevo"
    # duración/aspecto/audio se evalúan solo en la rama probe_ok → aquí no corren
    assert issues_of(report, "duración") == []
    assert issues_of(report, "aspect_ratio") == []
    assert issues_of(report, "audio") == []
    assert report["summary"]["measured"] is False
    assert report["summary"]["passed"] is False


def test_review_output_flags_missing_video_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(has_video=False))
    report = run_review(store, make_brief())
    issue = issues_of(report, "video")[0]
    assert issue["severity"] == "error"
    assert issue["correction"] == "re-renderizar como video"
    assert report["summary"]["errors"] == 1


# ---------------------------------------------------------------------------
# review_output: duración vs restricción
# ---------------------------------------------------------------------------


def test_review_output_duration_too_long_warns_with_cut_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(duration_sec=15.0))
    brief = make_brief(user_constraints=[{"key": "duration_sec", "value": 10}])
    report = run_review(store, brief)
    issue = issues_of(report, "duración")[0]
    assert issue["severity"] == "warning"
    assert issue["timecode"] == [14.0, 15.0]
    assert "10s" in issue["detail"] and "±1.00s" in issue["detail"]
    assert "recortar" in issue["correction"]
    # los warnings NO bloquean: passed refleja solo errores
    assert report["summary"]["warnings"] == 1
    assert report["summary"]["errors"] == 0
    assert report["summary"]["passed"] is True
    assert report["summary"]["measured"] is True


def test_review_output_duration_too_short_warns_with_extend_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(duration_sec=5.0))
    brief = make_brief(user_constraints=[{"key": "duration_sec", "value": 10}])
    report = run_review(store, brief)
    issue = issues_of(report, "duración")[0]
    assert issue["severity"] == "warning"
    assert issue["timecode"] == [4.0, 5.0]
    assert "ampliar con material adicional hasta ~10s" in issue["correction"]


def test_review_output_duration_within_tolerance_produces_no_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(duration_sec=10.5))
    brief = make_brief(user_constraints=[{"key": "duration_sec", "value": 10}])
    report = run_review(store, brief)
    assert issues_of(report, "duración") == []
    assert report["summary"]["errors"] == 0
    assert report["summary"]["passed"] is True


def test_review_output_duration_falls_back_to_brief_duration_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(duration_sec=15.0))
    brief = make_brief(duration={"target_sec": 6})  # sin user_constraints
    report = run_review(store, brief)
    issue = issues_of(report, "duración")[0]
    assert issue["severity"] == "warning"
    assert "6s" in issue["detail"] and "±0.60s" in issue["detail"]


def test_review_output_duration_constraint_takes_precedence_over_brief_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(duration_sec=6.5))
    brief = make_brief(
        user_constraints=[{"key": "duration_sec", "value": 20}],
        duration={"target_sec": 6},
    )
    report = run_review(store, brief)
    issue = issues_of(report, "duración")[0]
    # manda la restricción del usuario (20s), no duration.target_sec (6s)
    assert "20s" in issue["detail"]
    assert "6s" not in issue["detail"]


def test_review_output_zero_duration_target_skips_check_current_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # COMPORTAMIENTO ACTUAL (review.py L159): `if target:` descarta targets falsy
    # (p. ej. 0) y la comprobación de duración se omite sin ningún issue.
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(duration_sec=15.0))
    brief = make_brief(user_constraints=[{"key": "duration_sec", "value": 0}])
    report = run_review(store, brief)
    assert issues_of(report, "duración") == []
    assert report["summary"]["passed"] is True


# ---------------------------------------------------------------------------
# review_output: aspect ratio
# ---------------------------------------------------------------------------


def test_review_output_aspect_match_via_user_constraint_produces_no_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())  # 1920x1080 → 1.7778
    brief = make_brief(user_constraints=[{"key": "aspect_ratio", "value": "16:9"}])
    report = run_review(store, brief)
    assert issues_of(report, "aspect_ratio") == []
    assert report["summary"]["errors"] == 0


def test_review_output_aspect_falls_back_to_brief_aspect_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(width=1080, height=1920))  # 0.5625
    brief = make_brief(aspect_ratio="9:16")
    report = run_review(store, brief)
    assert issues_of(report, "aspect_ratio") == []
    assert report["technical"]["aspect"] == 0.5625


def test_review_output_aspect_mismatch_is_error_with_rerender_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())  # 1.7778
    brief = make_brief(user_constraints=[{"key": "aspect_ratio", "value": "9:16"}])
    report = run_review(store, brief)
    issue = issues_of(report, "aspect_ratio")[0]
    assert issue["severity"] == "error"
    assert issue["detail"] == "Aspecto medido 1.7778 no coincide con el pedido 9:16"
    assert issue["correction"] == "re-renderizar el EDL en 9:16 (variante existente)"
    assert report["summary"]["passed"] is False
    assert report["summary"]["errors"] == 1


def test_review_output_unparseable_aspect_skips_validation_current_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # COMPORTAMIENTO ACTUAL (bug): review.py L41-46 devuelve 0.0 para cualquier
    # string sin "w:h" (p. ej. "16x9" o el número 1.78) y el gate de L180-184
    # (`if want and got`) descarta el 0.0 falsy → la validación de aspecto se
    # desactiva en silencio: ni issue ni warning, y passed puede quedar en True
    # aunque el render no coincida con lo pedido.
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(width=500, height=500))  # aspecto 1.0
    brief = make_brief(aspect_ratio="16x9")
    report = run_review(store, brief)
    assert issues_of(report, "aspect_ratio") == []
    assert report["summary"]["errors"] == 0
    assert report["summary"]["passed"] is True
    assert _parse_aspect("16x9") == 0.0
    assert _parse_aspect("1.78") == 0.0


# ---------------------------------------------------------------------------
# review_output: audio
# ---------------------------------------------------------------------------


def test_review_output_audio_required_but_missing_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(has_audio=False))
    brief = make_brief(user_constraints=[{"key": "music", "value": True}])
    report = run_review(store, brief)
    issue = issues_of(report, "audio")[0]
    assert issue["severity"] == "error"
    assert "espera audio" in issue["detail"]
    assert "re-renderizar" in issue["correction"]
    assert report["summary"]["passed"] is False


def test_review_output_audio_not_required_missing_track_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(has_audio=False))
    brief = make_brief(user_constraints=[{"key": "music", "value": False}])
    report = run_review(store, brief)
    assert issues_of(report, "audio") == []
    assert report["summary"]["passed"] is True


def test_review_output_negated_music_label_does_not_require_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(has_audio=False))
    brief = make_brief(editorial={"audio_plan": {"music": "sin música"}})
    report = run_review(store, brief)
    assert issues_of(report, "audio") == []
    assert report["summary"]["passed"] is True


def test_review_output_dialogue_required_missing_track_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe(has_audio=False))
    brief = make_brief(
        editorial={"audio_plan": {"music_policy": "prohibited", "dialogue_policy": "required"}}
    )
    report = run_review(store, brief)
    issue = issues_of(report, "audio")[0]
    assert issue["severity"] == "error"
    assert report["summary"]["passed"] is False


# ---------------------------------------------------------------------------
# review_output: subtítulos
# ---------------------------------------------------------------------------


def linked_store(tmp_path: Path, **kwargs: Any) -> FakeStore:
    return make_store(
        tmp_path,
        edl_versions=({"output_asset_id": "a1", "edl": {"cuts": [{"asset_id": "src1"}]}},),
        transcriptions=(
            ("src1", {"id": "tr1", "segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}),
        ),
        **kwargs,
    )


def test_review_output_subtitles_required_without_linked_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    brief = make_brief(editorial={"audio_plan": {"subtitles_policy": "required"}})
    report = run_review(store, brief)
    subs = report["subtitles"]
    assert subs["required"] is True
    assert subs["available"] is False
    assert subs["transcription_id"] is None
    assert subs["srt_url"] is None
    assert subs["vtt_url"] is None
    issue = issues_of(report, "subtítulos")[0]
    assert issue["severity"] == "error"
    assert (
        issue["correction"] == "transcribir un asset fuente incluido en el EDL y exportar SRT/VTT"
    )
    assert report["summary"]["passed"] is False


def test_review_output_linked_transcription_builds_subtitle_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = linked_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    brief = make_brief(editorial={"audio_plan": {"subtitles_policy": "required"}})
    report = run_review(store, brief)
    subs = report["subtitles"]
    assert subs["required"] is True
    assert subs["available"] is True
    assert subs["transcription_id"] == "tr1"
    assert subs["srt_url"] == "/studio/api/projects/p1/transcriptions/tr1/subtitles?fmt=srt"
    assert subs["vtt_url"] == "/studio/api/projects/p1/transcriptions/tr1/subtitles?fmt=vtt"
    assert issues_of(report, "subtítulos") == []
    assert report["summary"]["passed"] is True


def test_review_output_unexportable_transcription_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # transcripción vinculada pero sin segmentos exportables → no está disponible
    store = make_store(
        tmp_path,
        edl_versions=({"output_asset_id": "a1", "edl": {"cuts": [{"asset_id": "src1"}]}},),
        transcriptions=(("src1", {"id": "tr1", "segments": []}),),
    )
    patch_probe(monkeypatch, make_probe())
    brief = make_brief(editorial={"audio_plan": {"subtitles_policy": "required"}})
    report = run_review(store, brief)
    assert report["subtitles"]["available"] is False
    assert report["subtitles"]["transcription_id"] is None
    assert issues_of(report, "subtítulos")


def test_review_output_subtitles_constraint_false_skips_link_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # aunque exista una transcripción vinculada, la restricción explícita
    # `subtitles: False` desactiva el requisito y no se consulta el vínculo
    store = linked_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    brief = make_brief(
        user_constraints=[{"key": "subtitles", "value": False}],
        editorial={"audio_plan": {"subtitles_policy": "required"}},
    )
    report = run_review(store, brief)
    assert report["subtitles"]["required"] is False
    assert report["subtitles"]["available"] is False
    assert report["subtitles"]["srt_url"] is None
    assert issues_of(report, "subtítulos") == []
    assert report["summary"]["passed"] is True


def test_review_output_legacy_subtitles_text_marks_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    brief = make_brief(editorial={"audio_plan": {"subtitles": "subtítulos obligatorios"}})
    report = run_review(store, brief)
    assert report["subtitles"]["required"] is True
    assert issues_of(report, "subtítulos")


def test_review_output_subtitles_recommended_is_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    brief = make_brief(editorial={"audio_plan": {"subtitles_policy": "recommended"}})
    report = run_review(store, brief)
    assert report["subtitles"]["required"] is False
    assert issues_of(report, "subtítulos") == []
    assert report["summary"]["passed"] is True


# ---------------------------------------------------------------------------
# review_output: checks editoriales, resumen y sobre del informe
# ---------------------------------------------------------------------------


def test_review_output_editorial_checks_stay_pending_human_or_vlm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    report = run_review(store, make_brief())
    checks = report["editorial_checks"]
    assert len(checks) == 5  # sin indicio de música no se añade el check musical
    assert all(c["status"] == "pending" for c in checks)
    assert all(c["requires"] == "human_or_vlm" for c in checks)
    assert all(c["note"] for c in checks)
    names = [c["check"] for c in checks]
    assert "afirmaciones inventadas" in names
    assert "cumplimiento del contenido musical" not in names


def test_review_output_appends_music_editorial_check_when_music_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    patch_probe(monkeypatch, make_probe())
    by_constraint = run_review(
        store, make_brief(user_constraints=[{"key": "music", "value": True}])
    )
    music_checks = [
        c for c in by_constraint["editorial_checks"]
        if c["check"] == "cumplimiento del contenido musical"
    ]
    assert music_checks and music_checks[0]["requires"] == "human_or_vlm"
    # la política estructurada también dispara el check, aunque no haya restricción
    by_policy = run_review(
        store, make_brief(editorial={"audio_plan": {"music_policy": "prohibited"}})
    )
    assert any(
        c["check"] == "cumplimiento del contenido musical"
        for c in by_policy["editorial_checks"]
    )


def test_review_output_report_envelope_and_technical_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    seen = patch_probe(monkeypatch, make_probe())
    report = run_review(store, make_brief())
    assert seen == [tmp_path / "render.mp4"], "el probe recibe la ruta real del asset"
    assert report["schema"] == REVIEW_SCHEMA == "comfy_studio.review.v1"
    assert report["project_id"] == "p1"
    assert report["asset_id"] == "a1"
    assert report["asset_filename"] == "render.mp4"
    assert report["brief_version_status"] == "approved"
    reviewed = datetime.fromisoformat(report["reviewed_at"])
    assert reviewed.tzinfo is not None
    assert reviewed.utcoffset() == timedelta(0)
    assert datetime.now(UTC) - reviewed < timedelta(minutes=5)
    technical = report["technical"]
    assert technical["probe_ok"] is True
    assert technical["duration_sec"] == 15.0
    assert technical["width"] == 1920
    assert technical["height"] == 1080
    assert technical["aspect"] == 1.7778
    assert technical["has_video"] is True
    assert technical["has_audio"] is True
    assert technical["codec_name"] == "h264"
    assert technical["audio_codec_name"] == "aac"
    assert "editorial" in report["summary"]["note"]
