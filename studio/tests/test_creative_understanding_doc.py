"""Tests deterministas de `comfy_studio.creative.understanding` (CPU, sin GPU/red).

Cubre `_sha256_file`, `_asset_facts_for_transcription` y `build_understanding`
con un doble en memoria de `ProjectStore` y un probe fijo (mismo patrón que
`test_creative_review.py`): sin ffmpeg, pesos, red ni LLM. Este paquete solo
añade tests; no parchea producción.

Anclas del comportamiento VIGENTE (documentadas, no arregladas aquí):
- las identidades faciales solo se consultan si hay `vector_store`, aunque el
  store las tenga;
- `counts.identities` cuenta la lista cruda: una entrada no-dict infla el
  contador sin producir fact;
- si `asset_ids` no casa ningún asset, `scope_ids` queda vacío y el guard
  `if scope_ids and ...` deja pasar TODAS las transcripciones;
- el fact de EDL usa `edls[0]` sin reordenar (el contrato de orden "más
  nueva primero" es del store real, ver `test_creative_delivery_provenance.py`).

Gaps no cubiertos por este archivo:
- `probe_ok=True` con ffprobe REAL exige un medio válido (se fija con un dict
  de probe; el camino de fallo REAL sí está en
  `test_real_probe_on_garbage_file_reports_unverified`);
- `DEFAULT_MAX_*` leen env al importar, así que el corte por segmentos se
  prueba en el helper con `max_segments` explícito, no con el default.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from comfy_studio import SIMULATED_ML_NOTICE
from comfy_studio.creative import understanding as understanding_module
from comfy_studio.creative.understanding import (
    UNDERSTANDING_SCHEMA,
    _asset_facts_for_transcription,
    _sha256_file,
    build_understanding,
)

PROJECT_ID = "p1"

#: Probe fijo: ffprobe verificó el medio de verdad.
PROBE_OK: dict[str, Any] = {
    "probe_ok": True,
    "duration_sec": 15.0,
    "width": 1920,
    "height": 1080,
    "has_video": True,
    "has_audio": True,
    "codec_name": "h264",
}

#: Probe fijo: forma del fallback de `probe_media_info` cuando falla.
PROBE_FAIL: dict[str, Any] = {
    "probe_ok": False,
    "duration_sec": 0.0,
    "width": 320,
    "height": 180,
    "has_video": True,
    "has_audio": False,
    "codec_name": "h264",
}


# --------------------------------------------------------------------------- #
# Helpers: dobles en memoria y constructores de datos planos
# --------------------------------------------------------------------------- #


class FakeStore:
    """Subconjunto mínimo de `ProjectStore` que usa `build_understanding`."""

    def __init__(
        self,
        *,
        has_project: bool = True,
        assets: tuple[dict[str, Any], ...] = (),
        asset_paths: dict[str, Path] | None = None,
        transcriptions: tuple[dict[str, Any], ...] = (),
        identities: tuple[Any, ...] = (),
        identities_error: Exception | None = None,
        edls: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.has_project = has_project
        self.assets = list(assets)
        self.asset_paths = dict(asset_paths or {})
        self.transcriptions = list(transcriptions)
        self.identities = list(identities)
        self.identities_error = identities_error
        self.edls = list(edls)
        self.identities_calls = 0

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        if not self.has_project or project_id != PROJECT_ID:
            return None
        return {"id": PROJECT_ID, "name": "Doc de prueba"}

    def list_assets(self, project_id: str) -> list[dict[str, Any]]:
        return [dict(a) for a in self.assets]

    def get_asset_path(self, project_id: str, asset_id: str) -> Path | None:
        return self.asset_paths.get(asset_id)

    def list_transcriptions(
        self, project_id: str, *, asset_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [dict(t) for t in self.transcriptions]
        if asset_id is not None:
            rows = [t for t in rows if t.get("asset_id") == asset_id]
        return rows

    def list_identities(self, project_id: str) -> list[dict[str, Any]]:
        self.identities_calls += 1
        if self.identities_error is not None:
            raise self.identities_error
        return list(self.identities)

    def list_edl_versions(self, project_id: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self.edls]


class FakeVectorStore:
    """Doble de `PerceptionVectorStore` con el único método usado."""

    def __init__(
        self,
        *,
        shots: tuple[dict[str, Any], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.shots = list(shots)
        self.error = error

    def list_clip_shots(self, project_id: str) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        return [dict(s) for s in self.shots]


def patch_probe(
    monkeypatch: pytest.MonkeyPatch, probe: dict[str, Any]
) -> list[Path]:
    """Sustituye ffprobe por un dict fijo; devuelve la lista de rutas observadas."""
    seen: list[Path] = []

    def _fake_probe(path: Path | str, **_kwargs: Any) -> dict[str, Any]:
        seen.append(Path(path))
        return dict(probe)

    monkeypatch.setattr(understanding_module, "probe_media_info", _fake_probe)
    return seen


def _write(tmp_path: Path, name: str, data: bytes = b"stub-bytes") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _asset(asset_id: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": asset_id,
        "kind": "video",
        "category": "inputs",
        "filename": f"{asset_id}.mp4",
        "original_filename": f"{asset_id}.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 9,
        "metadata": {"origen": "upload"},
    }
    base.update(kw)
    return base


def _seg(segment_id: str, text: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": segment_id,
        "start": 0.0,
        "end": 1.0,
        "text": text,
        "speaker": None,
    }
    base.update(kw)
    return base


def _tr(
    tr_id: str,
    asset_id: str | None,
    segments: list[dict[str, Any]],
    *,
    language: str = "es",
    speakers: list[str] | None = None,
    diarized: bool = False,
    backend: str = "whisperx",
) -> dict[str, Any]:
    return {
        "id": tr_id,
        "asset_id": asset_id,
        "language": language,
        "segments": segments,
        "speakers": speakers or [],
        "diarized": diarized,
        "backend": backend,
    }


def _ident(identity_id: str = "i1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": identity_id,
        "name": "Ana",
        "reference_asset_id": "a1",
    }
    base.update(kw)
    return base


def _shot(shot_id: str, source_asset_id: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "shot_id": shot_id,
        "source_asset_id": source_asset_id,
        "keyframe_asset_id": f"kf-{shot_id}",
        "timestamp_sec": 1.0,
        "shot_start_sec": 0.0,
        "shot_end_sec": 2.0,
        "visual_tags": [],
        "caption": None,
    }
    base.update(kw)
    return base


def _edl(
    version: int,
    *,
    output_asset_id: str | None = None,
    cuts: tuple[str, ...] = (),
    with_doc: bool = True,
) -> dict[str, Any]:
    return {
        "id": f"edl-{version}",
        "version": version,
        "edl": {"cuts": list(cuts)} if with_doc else None,
        "output_asset_id": output_asset_id,
    }


def _facts_of_kind(doc: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [f for f in doc["facts"] if f["kind"] == kind]


# --------------------------------------------------------------------------- #
# _sha256_file (hash real con tope y rutas ilegibles)
# --------------------------------------------------------------------------- #


def test_sha256_file_returns_digest_matching_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "mini.bin"
    content = b"contenido pequeno"
    path.write_bytes(content)
    digest, why = _sha256_file(path, max_bytes=1024)
    assert why is None
    assert digest == hashlib.sha256(content).hexdigest()


def test_sha256_file_skips_files_over_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "grande.bin"
    path.write_bytes(b"12345678")  # 8 bytes > 4
    digest, why = _sha256_file(path, max_bytes=4)
    assert digest is None
    assert why == "hash omitido (> 4 bytes)"


def test_sha256_file_hashes_file_exactly_at_cap(tmp_path: Path) -> None:
    """El corte es estricto (`size > max_bytes`): en el límite sí se hashea."""
    content = b"12345678"  # 8 bytes == 8
    path = tmp_path / "limite.bin"
    path.write_bytes(content)
    digest, why = _sha256_file(path, max_bytes=8)
    assert why is None
    assert digest == hashlib.sha256(content).hexdigest()


def test_sha256_file_missing_path_returns_reason(tmp_path: Path) -> None:
    digest, why = _sha256_file(tmp_path / "no-existe.bin", max_bytes=1024)
    assert digest is None
    assert why == "archivo no legible"  # OSError en stat()


def test_sha256_file_directory_returns_reason(tmp_path: Path) -> None:
    """Un directorio pasa stat() pero falla al abrirse -> OSError -> motivo."""
    folder = tmp_path / "carpeta"
    folder.mkdir()
    digest, why = _sha256_file(folder, max_bytes=10**9)
    assert digest is None
    assert why == "archivo no legible"  # OSError en open("rb")


# --------------------------------------------------------------------------- #
# _asset_facts_for_transcription (claims, intervalos y filtros de segmentos)
# --------------------------------------------------------------------------- #


def test_segment_fact_carries_claim_interval_and_evidence() -> None:
    tr = _tr(
        "tr1",
        "a1",
        [_seg("s1", "hola mundo", start=1.5, end=4.25, speaker="S1")],
        language="es",
        diarized=True,
        backend="whisperx",
    )
    facts = _asset_facts_for_transcription(tr, ml_simulated=True, max_segments=10)
    assert len(facts) == 1
    fact = facts[0]
    assert fact["kind"] == "transcript_segment"
    assert fact["asset_id"] == "a1"
    assert fact["interval_sec"] == [1.5, 4.25]
    assert fact["method"] == "whisperx"
    assert fact["simulated"] is True
    assert fact["evidence"] == {
        "transcription_id": "tr1",
        "segment_id": "s1",
        "speaker": "S1",
        "language": "es",
        "backend": "whisperx",
        "diarized": True,
    }
    assert fact["claim"] == "Dice: «hola mundo» (S1)"


def test_segment_without_speaker_has_no_suffix() -> None:
    tr = _tr("tr1", "a1", [_seg("s1", "hola"), _seg("s2", "adiós", speaker="")])
    facts = _asset_facts_for_transcription(tr, ml_simulated=False, max_segments=10)
    assert [f["claim"] for f in facts] == ["Dice: «hola»", "Dice: «adiós»"]
    assert all(f["simulated"] is False for f in facts)
    assert facts[0]["evidence"]["speaker"] is None


def test_segment_facts_skip_non_dict_and_blank_text() -> None:
    tr = _tr(
        "tr1",
        "a1",
        [42, "texto suelto", {"id": "s-blank", "text": "   "}, _seg("s-ok", "ok")],
    )
    facts = _asset_facts_for_transcription(tr, ml_simulated=True, max_segments=10)
    assert len(facts) == 1
    assert facts[0]["evidence"]["segment_id"] == "s-ok"
    assert facts[0]["interval_sec"] == [0.0, 1.0]


def test_segment_interval_clamps_end_and_defaults_start() -> None:
    tr = _tr(
        "tr1",
        "a1",
        [
            _seg("s1", "a", start=5.0, end=2.0),  # end < start -> se recorta
            {"id": "s2", "text": "b", "start": 1.0},  # sin end -> end = start
            {"id": "s3", "text": "c"},  # sin start ni end -> [0.0, 0.0]
            _seg("s4", "d", start="2.5", end="4.0"),  # strings numéricos ok
        ],
    )
    facts = _asset_facts_for_transcription(tr, ml_simulated=True, max_segments=10)
    assert [f["interval_sec"] for f in facts] == [
        [5.0, 5.0],
        [1.0, 1.0],
        [0.0, 0.0],
        [2.5, 4.0],
    ]


def test_segment_claim_truncates_text_to_300_chars() -> None:
    tr = _tr("tr1", "a1", [_seg("s1", "x" * 400, speaker="S1")])
    facts = _asset_facts_for_transcription(tr, ml_simulated=True, max_segments=10)
    assert facts[0]["claim"] == f"Dice: «{'x' * 300}» (S1)"


def test_segment_facts_respect_max_segments_cap() -> None:
    tr = _tr(
        "tr1",
        "a1",
        [_seg("s1", "uno"), _seg("s2", "dos"), _seg("s3", "tres")],
    )
    facts = _asset_facts_for_transcription(tr, ml_simulated=True, max_segments=2)
    assert [f["evidence"]["segment_id"] for f in facts] == ["s1", "s2"]


# --------------------------------------------------------------------------- #
# build_understanding: proyecto, alcance e intención
# --------------------------------------------------------------------------- #


def test_unknown_project_raises_lookup_error() -> None:
    store = FakeStore(has_project=False)
    with pytest.raises(LookupError, match="Proyecto 'fantasma' no encontrado"):
        build_understanding(store, "fantasma")


def test_empty_project_document_shape_and_zero_counts() -> None:
    store = FakeStore()
    doc = build_understanding(store, PROJECT_ID)
    assert doc["schema"] == UNDERSTANDING_SCHEMA == "comfy_studio.understanding.v1"
    assert doc["project_id"] == PROJECT_ID
    generated = datetime.fromisoformat(doc["generated_at"])
    assert generated.tzinfo is not None  # UTC aware
    assert doc["scope"] == {"asset_ids": "all", "prompt": ""}
    assert doc["assets"] == []
    assert doc["facts"] == []
    assert doc["duplicates"] == []
    assert doc["invalid"] == []
    assert doc["perception_available"] is False
    assert doc["intent"] is None
    assert doc["limitations"] == [
        f"Backends ML en modo simulado: {SIMULATED_ML_NOTICE}"
    ]
    assert doc["counts"] == {
        "assets": 0,
        "facts": 0,
        "facts_total": 0,
        "transcriptions": 0,
        "shots": 0,
        "identities": 0,
        "edl_versions": 0,
        "duplicates": 0,
    }


def test_prompt_is_parsed_into_intent() -> None:
    store = FakeStore()
    doc = build_understanding(
        store, PROJECT_ID, prompt="reel de 30 segundos para instagram"
    )
    intent = doc["intent"]
    assert intent is not None
    assert intent["piece_type"] == "reel"
    assert intent["duration_sec"] == 30.0
    assert intent["platform"] == "instagram"
    assert any(c["key"] == "duration_sec" for c in intent["constraints"])
    assert doc["scope"]["prompt"] == "reel de 30 segundos para instagram"


def test_blank_prompt_still_produces_default_intent() -> None:
    """ANCLA: el corte es `if prompt` (truthy), no `prompt.strip()`."""
    store = FakeStore()
    doc = build_understanding(store, PROJECT_ID, prompt="   ")
    assert doc["intent"] is not None
    assert doc["intent"]["piece_type"] == "other"
    assert doc["intent"]["constraints"] == []
    assert doc["scope"]["prompt"] == "   "


def test_asset_ids_filter_scope_missing_and_probe_calls(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = patch_probe(monkeypatch, PROBE_OK)
        p1 = _write(tmp_path, "a1.mp4")
        p2 = _write(tmp_path, "a2.mp4")
        store = FakeStore(
            assets=(_asset("a1"), _asset("a2")),
            asset_paths={"a1": p1, "a2": p2},
        )
        doc = build_understanding(
            store, PROJECT_ID, asset_ids=["a2", "ghost", "  "]
        )
        assert [a["asset_id"] for a in doc["assets"]] == ["a2"]
        assert doc["scope"]["asset_ids"] == ["a2"]
        assert doc["counts"]["assets"] == 1
        assert "asset_ids pedidos no encontrados: ghost" in doc["limitations"]
        # solo a2 entró al alcance, solo a2 se sondeó
        assert seen == [p2]
    finally:
        monkeypatch.undo()


def test_asset_ids_empty_list_means_all_assets(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"), _asset("a2")),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
        )
        doc = build_understanding(store, PROJECT_ID, asset_ids=[])
        assert len(doc["assets"]) == 2
        assert doc["scope"]["asset_ids"] == "all"
    finally:
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# Archivos, hash, duplicados y probe
# --------------------------------------------------------------------------- #


def test_missing_physical_file_is_reported_invalid(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(assets=(_asset("a1", metadata=None),))
        doc = build_understanding(store, PROJECT_ID)
        entry = doc["assets"][0]
        assert entry["status"] == "missing_file"
        assert entry["probe"] is None
        assert entry["sha256"] is None
        assert entry["metadata"] == {}  # metadata None -> {}
        assert doc["invalid"] == [
            {"asset_id": "a1", "reason": "archivo físico ausente"}
        ]
        # sin archivo no hay hecho media_metadata ni llamada a ffprobe
        assert _facts_of_kind(doc, "media_metadata") == []
        assert seen == []
        assert any("1 asset(s) no verificables" in lim for lim in doc["limitations"])
    finally:
        monkeypatch.undo()


def test_directory_path_counts_as_missing_file(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = patch_probe(monkeypatch, PROBE_OK)
        folder = tmp_path / "carpeta"
        folder.mkdir()
        store = FakeStore(
            assets=(_asset("a1"),), asset_paths={"a1": folder}
        )
        doc = build_understanding(store, PROJECT_ID)
        assert doc["assets"][0]["status"] == "missing_file"
        assert doc["invalid"][0]["reason"] == "archivo físico ausente"
        assert seen == []  # is_file() falla -> probe no se invoca
    finally:
        monkeypatch.undo()


def test_probe_ok_keeps_status_ok_and_metadata_claim(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = patch_probe(monkeypatch, PROBE_OK)
        path = _write(tmp_path, "a1.mp4")
        store = FakeStore(assets=(_asset("a1"),), asset_paths={"a1": path})
        doc = build_understanding(store, PROJECT_ID)
        entry = doc["assets"][0]
        assert entry["status"] == "ok"
        assert entry["probe"] == PROBE_OK
        assert doc["invalid"] == []
        fact = _facts_of_kind(doc, "media_metadata")[0]
        assert fact["method"] == "ffprobe"
        assert fact["simulated"] is False  # ffprobe nunca se marca simulado
        assert fact["interval_sec"] is None
        assert fact["evidence"] == {"probe_ok": True}
        assert (
            fact["claim"]
            == "video 1920x1080, duración 15.0s, audio=sí, codec=h264"
        )
        assert seen == [path]
    finally:
        monkeypatch.undo()


def test_probe_failure_marks_probe_unverified(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_FAIL)
        path = _write(tmp_path, "a1.mp4")
        store = FakeStore(assets=(_asset("a1"),), asset_paths={"a1": path})
        doc = build_understanding(store, PROJECT_ID)
        entry = doc["assets"][0]
        assert entry["status"] == "probe_unverified"
        assert doc["invalid"] == [
            {
                "asset_id": "a1",
                "reason": "ffprobe no pudo verificar el archivo (probe_ok=false)",
            }
        ]
        fact = _facts_of_kind(doc, "media_metadata")[0]
        assert fact["evidence"] == {"probe_ok": False}
        assert fact["claim"] == (
            "video 320x180, duración 0.0s, audio=no, codec=h264 "
            "(NO verificado: ffprobe falló)"
        )
        assert any("1 asset(s) no verificables" in lim for lim in doc["limitations"])
    finally:
        monkeypatch.undo()


def test_real_probe_on_garbage_file_reports_unverified(tmp_path: Path) -> None:
    """ffprobe REAL sobre bytes basura: probe_ok=False sin depender de mocks.

    El binario puede existir o no; ambos caminos (sin ffprobe o ffprobe que
    devuelve != 0) producen el fallback 320x180/0.0s y probe_ok=False, así que
    las aserciones son deterministas. Solo se omite el codec exacto, que sí
    cambia entre ambos fallbacks.
    """
    path = _write(tmp_path, "basura.mp4", b"esto no es un archivo multimedia")
    store = FakeStore(assets=(_asset("a1"),), asset_paths={"a1": path})
    doc = build_understanding(store, PROJECT_ID)
    entry = doc["assets"][0]
    assert entry["probe"]["probe_ok"] is False
    assert entry["status"] == "probe_unverified"
    assert doc["invalid"] == [
        {
            "asset_id": "a1",
            "reason": "ffprobe no pudo verificar el archivo (probe_ok=false)",
        }
    ]
    fact = _facts_of_kind(doc, "media_metadata")[0]
    assert fact["evidence"] == {"probe_ok": False}
    claim = fact["claim"]
    assert claim.startswith("video 320x180, duración 0.0s, audio=no")
    assert claim.endswith("(NO verificado: ffprobe falló)")


def test_oversized_hash_is_skipped_but_probe_still_runs(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        seen = patch_probe(monkeypatch, PROBE_OK)
        p1 = _write(tmp_path, "a1.mp4", b"12345678")
        p2 = _write(tmp_path, "a2.mp4", b"12345678")  # idénticos, sin hash
        store = FakeStore(
            assets=(_asset("a1"), _asset("a2")),
            asset_paths={"a1": p1, "a2": p2},
        )
        doc = build_understanding(store, PROJECT_ID, max_hash_bytes=4)
        for entry in doc["assets"]:
            assert entry["sha256"] is None
            assert entry["hash_notice"] == "hash omitido (> 4 bytes)"
            assert entry["duplicate_of"] is None
        # sin hash no hay dedup, pero el probe sigue corriendo
        assert doc["duplicates"] == []
        assert doc["counts"]["duplicates"] == 0
        assert seen == [p1, p2]
        assert len(_facts_of_kind(doc, "media_metadata")) == 2
    finally:
        monkeypatch.undo()


def test_identical_files_grouped_as_duplicates(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        data = b"mismo-bytes"
        store = FakeStore(
            assets=(_asset("a1"), _asset("a2")),
            asset_paths={
                "a1": _write(tmp_path, "a1.mp4", data),
                "a2": _write(tmp_path, "a2.mp4", data),
            },
        )
        doc = build_understanding(store, PROJECT_ID)
        a1, a2 = doc["assets"]
        digest = hashlib.sha256(data).hexdigest()
        assert a1["sha256"] == a2["sha256"] == digest
        assert a1["duplicate_of"] is None
        assert a2["duplicate_of"] == "a1"
        assert doc["duplicates"] == [
            {"sha256": digest, "asset_ids": ["a1", "a2"]}
        ]
        assert doc["counts"]["duplicates"] == 1
    finally:
        monkeypatch.undo()


def test_three_copies_count_two_duplicates(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        data = b"tres-copias"
        store = FakeStore(
            assets=(_asset("a1"), _asset("a2"), _asset("a3")),
            asset_paths={
                "a1": _write(tmp_path, "a1.mp4", data),
                "a2": _write(tmp_path, "a2.mp4", data),
                "a3": _write(tmp_path, "a3.mp4", data),
            },
        )
        doc = build_understanding(store, PROJECT_ID)
        assert doc["duplicates"][0]["asset_ids"] == ["a1", "a2", "a3"]
        # todas las copias apuntan al primer dueño del hash
        assert [e["duplicate_of"] for e in doc["assets"]] == [None, "a1", "a1"]
        assert doc["counts"]["duplicates"] == 2
    finally:
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# Transcripciones (alcance, orden y claims)
# --------------------------------------------------------------------------- #


def test_latest_transcription_wins_and_only_its_segments_emit(
    tmp_path: Path,
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
            transcriptions=(
                # el store real devuelve la más nueva primero (created_at DESC)
                _tr("tr-new", "a1", [_seg("s-new", "texto nuevo")]),
                _tr("tr-old", "a1", [_seg("s-old", "texto viejo")]),
            ),
        )
        doc = build_understanding(store, PROJECT_ID)
        summary = _facts_of_kind(doc, "transcription")[0]
        assert summary["evidence"]["transcription_id"] == "tr-new"
        claims = [f["claim"] for f in _facts_of_kind(doc, "transcript_segment")]
        assert claims == ["Dice: «texto nuevo»"]
        assert doc["counts"]["transcriptions"] == 1
    finally:
        monkeypatch.undo()


def test_transcriptions_out_of_asset_ids_scope_are_dropped(
    tmp_path: Path,
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
            transcriptions=(_tr("tr-a2", "a2", [_seg("s1", "fuera")]),),
        )
        doc = build_understanding(store, PROJECT_ID, asset_ids=["a1"])
        assert _facts_of_kind(doc, "transcription") == []
        assert _facts_of_kind(doc, "transcript_segment") == []
        assert doc["counts"]["transcriptions"] == 0
    finally:
        monkeypatch.undo()


def test_transcription_without_asset_id_is_ignored() -> None:
    store = FakeStore(
        transcriptions=(_tr("tr-orphan", None, [_seg("s1", "sin asset")]),),
    )
    doc = build_understanding(store, PROJECT_ID)
    assert _facts_of_kind(doc, "transcription") == []
    assert doc["counts"]["transcriptions"] == 0


def test_transcription_missing_segments_key_yields_zero_segment_claim() -> None:
    """`tr.get("segments") or []` tolera la clave ausente: resumen con 0
    segmentos, sin facts de segmento y sin parte de hablantes."""
    bare: dict[str, Any] = {
        "id": "tr-bare",
        "asset_id": "a1",
        "language": "en",
        # sin "segments", sin "speakers", sin "diarized"
    }
    store = FakeStore(transcriptions=(bare,))
    doc = build_understanding(store, PROJECT_ID, ml_simulated=False)
    assert _facts_of_kind(doc, "transcript_segment") == []
    summary = _facts_of_kind(doc, "transcription")[0]
    assert summary["claim"] == "Transcripción en con 0 segmentos"
    assert summary["evidence"] == {
        "transcription_id": "tr-bare",
        "language": "en",
        "diarized": False,
        "speakers": [],
        "segments_count": 0,
    }
    assert doc["counts"]["transcriptions"] == 1


def test_empty_asset_scope_leaks_transcriptions_from_any_asset() -> None:
    """ANCLA (comportamiento vigente): si `asset_ids` no casa ningún asset,
    `scope_ids` queda vacío y el guard `if scope_ids and ...` no filtra: se
    emiten hechos de transcripción de assets fuera del alcance pedido."""
    store = FakeStore(
        transcriptions=(_tr("tr1", "a1", [_seg("s1", "leak")]),),
    )
    doc = build_understanding(store, PROJECT_ID, asset_ids=["ghost"])
    assert doc["assets"] == []
    assert doc["scope"]["asset_ids"] == []
    leaked = _facts_of_kind(doc, "transcription")
    assert len(leaked) == 1
    assert leaked[0]["asset_id"] == "a1"
    assert doc["counts"]["transcriptions"] == 1
    assert "asset_ids pedidos no encontrados: ghost" in doc["limitations"]


def test_transcription_claims_reflect_speakers_and_simulation(
    tmp_path: Path,
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
            transcriptions=(
                _tr(
                    "tr1",
                    "a1",
                    [_seg("s1", "hola", speaker="S1")],
                    speakers=["S1"],
                    diarized=True,
                ),
            ),
        )
        doc_sim = build_understanding(store, PROJECT_ID, ml_simulated=True)
        summary = _facts_of_kind(doc_sim, "transcription")[0]
        assert summary["claim"] == (
            "Transcripción es con 1 segmentos, hablantes: S1 (simulada)"
        )
        assert summary["evidence"] == {
            "transcription_id": "tr1",
            "language": "es",
            "diarized": True,
            "speakers": ["S1"],
            "segments_count": 1,
        }
        segment = _facts_of_kind(doc_sim, "transcript_segment")[0]
        assert segment["claim"] == "Dice: «hola» (S1)"
        assert segment["simulated"] is True

        doc_real = build_understanding(store, PROJECT_ID, ml_simulated=False)
        summary_real = _facts_of_kind(doc_real, "transcription")[0]
        assert summary_real["claim"] == (
            "Transcripción es con 1 segmentos, hablantes: S1"
        )
        assert _facts_of_kind(doc_real, "transcript_segment")[0]["simulated"] is False
        assert doc_real["limitations"] == []
    finally:
        monkeypatch.undo()


# --------------------------------------------------------------------------- #
# Percepción: shots CLIP e identidades faciales
# --------------------------------------------------------------------------- #


def test_without_vector_store_identities_are_never_consulted() -> None:
    store = FakeStore(identities=(_ident("i1"),))
    doc = build_understanding(store, PROJECT_ID)  # vector_store=None
    assert doc["perception_available"] is False
    assert doc["counts"]["identities"] == 0
    assert _facts_of_kind(doc, "face_identity") == []
    # ANCLA: sin índice no se consulta la tabla, aunque el store la tenga
    assert store.identities_calls == 0


def test_clip_shots_are_scoped_and_claimed(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
        )
        vec = FakeVectorStore(
            shots=(
                _shot(
                    "sh1",
                    "a1",
                    timestamp_sec=1.0,
                    shot_start_sec=0.0,
                    shot_end_sec=2.5,
                    visual_tags=["producto", "luz"],
                    caption="plano del producto",
                ),
                _shot("sh2", "a2", timestamp_sec=9.0),  # fuera de alcance
            ),
        )
        doc = build_understanding(
            store, PROJECT_ID, vector_store=vec, asset_ids=["a1"]
        )
        shots = _facts_of_kind(doc, "clip_shot")
        assert len(shots) == 1
        shot = shots[0]
        assert shot["asset_id"] == "a1"
        assert shot["interval_sec"] == [0.0, 2.5]
        assert shot["method"] == "clip"
        assert shot["simulated"] is True
        assert shot["evidence"] == {
            "shot_id": "sh1",
            "keyframe_asset_id": "kf-sh1",
            "timestamp_sec": 1.0,
            "caption": "plano del producto",
        }
        assert shot["claim"] == (
            "Plano en t=1.0s con etiquetas producto, luz "
            "— plano del producto (embedding simulado)"
        )
        assert doc["perception_available"] is True
        assert doc["counts"]["shots"] == 1
    finally:
        monkeypatch.undo()


def test_clip_shot_claim_omits_optional_parts(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
        )
        vec = FakeVectorStore(
            shots=(_shot("sh9", "a1", timestamp_sec=2.0, shot_start_sec=1.0,
                         shot_end_sec=3.0),),
        )
        doc = build_understanding(
            store, PROJECT_ID, vector_store=vec, ml_simulated=False
        )
        shot = _facts_of_kind(doc, "clip_shot")[0]
        assert shot["interval_sec"] == [1.0, 3.0]
        # sin tags, sin caption y sin simulación -> solo el claim base
        assert shot["claim"] == "Plano en t=2.0s"
    finally:
        monkeypatch.undo()


def test_vector_store_failure_disables_perception_but_keeps_identities() -> None:
    """ANCLA: si `list_clip_shots` revienta, `perception_available` pasa a
    False pero el bloque de identidades (try separado, mismo `if`) sigue
    ejecutándose y sus facts se emiten igual."""
    store = FakeStore(identities=(_ident("i1", name="Ana"),))
    vec = FakeVectorStore(error=RuntimeError("índice roto"))
    doc = build_understanding(store, PROJECT_ID, vector_store=vec)
    assert doc["perception_available"] is False
    assert doc["counts"]["shots"] == 0
    assert _facts_of_kind(doc, "clip_shot") == []
    assert doc["counts"]["identities"] == 1
    face = _facts_of_kind(doc, "face_identity")[0]
    assert face["asset_id"] == "a1"
    assert face["method"] == "insightface"
    assert face["simulated"] is True
    assert face["evidence"] == {"identity_id": "i1", "name": "Ana"}
    assert face["claim"] == "Identidad registrada: Ana (simulada)"


def test_non_dict_identity_inflates_count_without_fact() -> None:
    """ANCLA: `counts.identities` usa `len(identities)` sobre la lista cruda;
    una entrada no-dict se salta al construir facts pero sigue contando."""
    store = FakeStore(identities=("junk", _ident("i1")))
    vec = FakeVectorStore()
    doc = build_understanding(store, PROJECT_ID, vector_store=vec)
    assert doc["counts"]["identities"] == 2
    faces = _facts_of_kind(doc, "face_identity")
    assert len(faces) == 1
    assert faces[0]["evidence"]["identity_id"] == "i1"


def test_identity_lookup_failure_is_swallowed() -> None:
    store = FakeStore(identities_error=RuntimeError("tabla rota"))
    vec = FakeVectorStore()
    doc = build_understanding(store, PROJECT_ID, vector_store=vec)  # no lanza
    assert doc["counts"]["identities"] == 0
    assert _facts_of_kind(doc, "face_identity") == []


# --------------------------------------------------------------------------- #
# EDL existentes
# --------------------------------------------------------------------------- #


def test_latest_edl_fact_reports_render_and_counts_all_versions() -> None:
    store = FakeStore(
        edls=(
            # el doble respeta el contrato del store real: más nueva primero
            _edl(3, output_asset_id="out-1", cuts=("c1", "c2")),
            _edl(2, output_asset_id="out-2"),
        ),
    )
    doc = build_understanding(store, PROJECT_ID)
    fact = _facts_of_kind(doc, "edl")[0]
    assert fact["asset_id"] == "out-1"
    assert fact["method"] == "edl"
    assert fact["simulated"] is False
    assert fact["evidence"] == {"edl_id": "edl-3", "version": 3}
    assert fact["claim"] == "EDL v3 con 2 cortes, renderizado como asset out-1"
    # el counter incluye todas las versiones; el fact solo toma edls[0]
    assert doc["counts"]["edl_versions"] == 2


def test_edl_without_render_or_document() -> None:
    store = FakeStore(edls=(_edl(1, with_doc=False),))
    doc = build_understanding(store, PROJECT_ID)
    fact = _facts_of_kind(doc, "edl")[0]
    assert fact["asset_id"] is None
    assert fact["claim"] == "EDL v1 con 0 cortes (sin render)"


def test_no_edl_versions_yields_no_fact() -> None:
    doc = build_understanding(FakeStore(), PROJECT_ID)
    assert _facts_of_kind(doc, "edl") == []
    assert doc["counts"]["edl_versions"] == 0


# --------------------------------------------------------------------------- #
# Truncamiento de facts y limitaciones
# --------------------------------------------------------------------------- #


def _store_with_four_facts(tmp_path: Path) -> FakeStore:
    """1 media_metadata + 2 segmentos + 1 resumen = 4 hechos."""
    return FakeStore(
        assets=(_asset("a1"),),
        asset_paths={"a1": _write(tmp_path, "a1.mp4")},
        transcriptions=(
            _tr("tr1", "a1", [_seg("s1", "uno"), _seg("s2", "dos")]),
        ),
    )


def test_max_facts_truncates_renumbers_and_reports_total(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = _store_with_four_facts(tmp_path)
        doc = build_understanding(store, PROJECT_ID, max_facts=3)
        assert [f["kind"] for f in doc["facts"]] == [
            "media_metadata",
            "transcript_segment",
            "transcript_segment",
        ]
        # los ids se asignan DESPUÉS del corte: fact-0001..0003
        assert [f["id"] for f in doc["facts"]] == [
            "fact-0001",
            "fact-0002",
            "fact-0003",
        ]
        assert doc["counts"]["facts"] == 3
        assert doc["counts"]["facts_total"] == 4
        assert "Hechos truncados a 3 (ver counts.facts_total)." in doc["limitations"]
    finally:
        monkeypatch.undo()


def test_max_facts_zero_drops_all_facts_but_keeps_total(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = _store_with_four_facts(tmp_path)
        doc = build_understanding(store, PROJECT_ID, max_facts=0)
        assert doc["facts"] == []
        assert doc["counts"]["facts"] == 0
        assert doc["counts"]["facts_total"] == 4
        assert "Hechos truncados a 0 (ver counts.facts_total)." in doc["limitations"]
    finally:
        monkeypatch.undo()


def test_max_facts_equal_to_total_is_not_truncated(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = _store_with_four_facts(tmp_path)
        doc = build_understanding(store, PROJECT_ID, max_facts=4)
        assert len(doc["facts"]) == 4
        assert doc["counts"]["facts_total"] == 4
        assert not any("truncados" in lim for lim in doc["limitations"])
        assert [f["id"] for f in doc["facts"]][-1] == "fact-0004"
    finally:
        monkeypatch.undo()


def test_limitations_combine_all_four_sources(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_FAIL)  # invalid: probe fallido
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
            transcriptions=(_tr("tr1", "a1", [_seg("s1", "uno")]),),
        )
        # total = media(1) + segmento(1) + resumen(1) = 3 > 1 -> truncado
        doc = build_understanding(
            store, PROJECT_ID, asset_ids=["a1", "ghost"], max_facts=1
        )
        assert doc["limitations"] == [
            f"Backends ML en modo simulado: {SIMULATED_ML_NOTICE}",
            "1 asset(s) no verificables (ver `invalid`); "
            "el proyecto continúa sin ellos.",
            "asset_ids pedidos no encontrados: ghost",
            "Hechos truncados a 1 (ver counts.facts_total).",
        ]
    finally:
        monkeypatch.undo()


def test_ml_simulated_false_clears_flags_and_limitation(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        patch_probe(monkeypatch, PROBE_OK)
        store = FakeStore(
            assets=(_asset("a1"),),
            asset_paths={"a1": _write(tmp_path, "a1.mp4")},
            transcriptions=(
                _tr("tr1", "a1", [_seg("s1", "hola", speaker="S1")],
                    speakers=["S1"]),
            ),
        )
        doc = build_understanding(store, PROJECT_ID, ml_simulated=False)
        assert doc["limitations"] == []
        media = _facts_of_kind(doc, "media_metadata")[0]
        segment = _facts_of_kind(doc, "transcript_segment")[0]
        summary = _facts_of_kind(doc, "transcription")[0]
        # ffprobe y whisperx reales: nada marcado como simulado
        assert media["simulated"] is False
        assert segment["simulated"] is False
        assert "(simulada)" not in summary["claim"]
        assert summary["claim"] == "Transcripción es con 1 segmentos, hablantes: S1"
    finally:
        monkeypatch.undo()
