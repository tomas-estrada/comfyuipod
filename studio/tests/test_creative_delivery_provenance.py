"""Cobertura de `creative/delivery.py` y `creative/provenance.py` (CPU).

Objetivo: fijar el comportamiento REAL de dos piezas sin cobertura:

1. `build_delivery_package`: elección del video final, variantes, portada,
   subtítulos y_limitaciones. En particular resuelve la duda "primero vs
   último": `rendered[0]` es correcto porque `ProjectStore.list_edl_versions`
   ordena `ORDER BY version DESC, created_at DESC` (proyectos.py:1303-1306),
   es decir la **más nueva** va primero. `test_list_edl_versions_is_newest_first`
   y `test_final_video_is_the_newest_rendered_edl` blindan ese contrato con el
   store real, no con un doble, para que el día que cambie el ORDER BY salte.

2. `source_asset_ids_for_output` / `linked_transcriptions`: devuelven en la
   PRIMERA versión de EDL que produce ese output y **no** siguen buscando, así
   que versiones posteriores que produzcan el mismo output se ignoran
   silenciosamente. Ese corte temprano (provenance.py:25) se fija a propósito:
   es el comportamiento vigente, no un olvido.

Los dobles del store son solo para los casos de forma del paquete; el contrato
de orden se verifica con SQLite real.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from comfy_studio.creative.delivery import DELIVERY_SCHEMA, build_delivery_package
from comfy_studio.creative.provenance import (
    linked_transcriptions,
    source_asset_ids_for_output,
)
from comfy_studio.projects import ProjectStore

PROJECT_ID = "abc123"


def _now(offset: int = 0) -> str:
    return datetime(2026, 1, 1 + offset, 12, 0, 0, tzinfo=UTC).isoformat()


# ---------------------------------------------------------------------------
# Doble del store: entrega estado determinista sin base de datos.
# ---------------------------------------------------------------------------


#: Centinela: el doble no devuelve ningún proyecto (`get_project` -> None).
NO_PROJECT: Any = object()


class FakeStore:
    """Subconjunto mínimo de `ProjectStore` que usa `build_delivery_package`.

    Respeta el mismo orden descendente por versión que el store real
    (ver `test_list_edl_versions_is_newest_first`).
    """

    def __init__(
        self,
        *,
        project: Any = None,
        edls: list[dict[str, Any]] | None = None,
        assets: list[dict[str, Any]] | None = None,
        transcriptions: list[dict[str, Any]] | None = None,
    ) -> None:
        if project is NO_PROJECT:
            self._project: dict[str, Any] | None = None
        elif project is None:
            self._project = {"id": PROJECT_ID, "name": "P"}
        else:
            self._project = project
        self._edls = sorted(edls or [], key=lambda e: -(int(e.get("version") or 0)))
        self._assets = list(assets or [])
        self._transcriptions = list(transcriptions or [])

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        if self._project is None or project_id != self._project["id"]:
            return None
        return dict(self._project)

    def list_edl_versions(self, project_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(e) for e in self._edls]

    def list_assets(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [a for a in self._assets if a.get("project_id", PROJECT_ID) == project_id]
        if kind is not None:
            rows = [a for a in rows if a.get("kind") == kind]
        if category is not None:
            rows = [a for a in rows if a.get("category") == category]
        return [dict(a) for a in rows]

    def list_transcriptions(
        self, project_id: str, *, asset_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = list(self._transcriptions)
        if asset_id is not None:
            rows = [t for t in rows if t.get("asset_id") == asset_id]
        return [dict(t) for t in rows]


def _edl(version: int, *, output_asset_id: str | None = None, **doc: Any) -> dict[str, Any]:
    payload = {
        "schema": "comfy_studio.edl.v1",
        "title": f"EDL v{version}",
        "aspect_ratio": "16:9",
        "total_duration_sec": 30.0 + version,
        "cuts": [],
    }
    payload.update(doc)
    return {
        "id": f"edl-{version}",
        "project_id": PROJECT_ID,
        "version": version,
        "edl": payload,
        "output_asset_id": output_asset_id,
        "created_at": _now(version),
    }


def _asset(
    asset_id: str,
    *,
    kind: str = "video",
    category: str = "outputs",
    created: int = 0,
    metadata: dict[str, Any] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "project_id": PROJECT_ID,
        "kind": kind,
        "category": category,
        "filename": filename or f"{asset_id}.mp4",
        "original_filename": filename or f"{asset_id}.mp4",
        "created_at": _now(created),
        "metadata": metadata or {},
    }


def _transcription(
    tr_id: str,
    asset_id: str,
    segments: list[dict[str, Any]],
    *,
    language: str = "es",
    diarized: bool = False,
) -> dict[str, Any]:
    return {
        "id": tr_id,
        "project_id": PROJECT_ID,
        "asset_id": asset_id,
        "language": language,
        "segments": segments,
        "diarized": diarized,
    }


def _brief_row(**brief: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"schema": "comfy_studio.brief.v1", "piece_type": "reel"}
    payload.update(brief)
    return {
        "id": "br-1",
        "project_id": PROJECT_ID,
        "version": 2,
        "status": "active",
        "brief": payload,
        "evidence": {"limitations": ["Sin acceso a datos de tendencias."], "sources": []},
    }


# ---------------------------------------------------------------------------
# Contrato de orden del store real: la base de `rendered[0]`.
# ---------------------------------------------------------------------------


def test_list_edl_versions_is_newest_first(tmp_path: Path) -> None:
    """SQLite real: `list_edl_versions` devuelve primero la versión más alta.

    Es el hecho que hace correcto `rendered[0]` en delivery.py:115. Si alguien
    cambia el ORDER BY a ascendente, este test falla antes que cualquier otro.
    """
    store = ProjectStore(tmp_path / "data")
    project = store.create_project("Orden EDL")
    pid = project["id"]

    def _upload(name: str) -> str:
        return store.save_upload(
            pid,
            filename=name,
            content_type="video/mp4",
            data=b"\x00" * 32,
            max_upload_bytes=1024,
            category="outputs",
        )["id"]

    for i, output_id in enumerate([_upload(f"o{i}.mp4") for i in range(3)], start=1):
        store.save_edl_version(
            pid, edl={"title": f"v{i}"}, output_asset_id=output_id
        )

    versions = [v["version"] for v in store.list_edl_versions(pid)]
    titles = [v["edl"]["title"] for v in store.list_edl_versions(pid)]
    assert versions == [3, 2, 1]
    assert titles == ["v3", "v2", "v1"]


def test_rendered_zero_matches_the_last_rendered_edl(tmp_path: Path) -> None:
    """Con el store real, `rendered[0]` es la EDL renderizada más reciente."""
    store = ProjectStore(tmp_path / "data")
    pid = store.create_project("Render final")["id"]

    def _upload(name: str) -> str:
        return store.save_upload(
            pid,
            filename=name,
            content_type="video/mp4",
            data=b"\x00" * 32,
            max_upload_bytes=1024,
            category="outputs",
        )["id"]

    outputs = [_upload(f"o{i}.mp4") for i in range(3)]
    for i, output_id in enumerate(outputs, start=1):
        store.save_edl_version(
            pid,
            edl={"title": f"v{i}", "aspect_ratio": "16:9", "cuts": []},
            output_asset_id=output_id,
        )

    rendered = [e for e in store.list_edl_versions(pid) if e.get("output_asset_id")]
    package = build_delivery_package(store, pid, _brief_row())

    assert rendered[0]["output_asset_id"] == outputs[-1]
    assert package["video"]["asset_id"] == outputs[-1], (
        "el video final debe ser el de la EDL renderizada más reciente (version DESC)"
    )
    assert [v["asset_id"] for v in package["variants"]] == list(reversed(outputs))


# ---------------------------------------------------------------------------
# build_delivery_package: forma del paquete
# ---------------------------------------------------------------------------


def test_final_video_is_the_newest_rendered_edl() -> None:
    """`rendered[0]` == EDL con mayor `version` que tenga output."""
    store = FakeStore(
        edls=[
            _edl(1, output_asset_id="out-a"),
            _edl(2),  # sin render: no compite
            _edl(3, output_asset_id="out-c"),
        ],
        assets=[_asset("out-a", created=1), _asset("out-c", created=3)],
    )
    package = build_delivery_package(store, PROJECT_ID, _brief_row())
    assert package["video"]["asset_id"] == "out-c"
    assert package["video"]["file_url"] == (
        f"/studio/api/projects/{PROJECT_ID}/assets/out-c/file"
    )
    assert package["video"]["download_url"].endswith("?download=1")
    assert package["video"]["view_url"].startswith("/studio/api/view?project_id=")


def test_variants_list_every_rendered_edl_newest_first() -> None:
    """Variantes = todas las EDL con output, mismas URLs y metadatos del EDL."""
    store = FakeStore(
        edls=[
            _edl(1, output_asset_id="out-a", aspect_ratio="9:16", total_duration_sec=12.0),
            _edl(2, output_asset_id="out-b", aspect_ratio="1:1", total_duration_sec=20.0),
        ],
        assets=[_asset("out-a"), _asset("out-b")],
    )
    variants = build_delivery_package(store, PROJECT_ID, _brief_row())["variants"]
    assert [v["edl_version"] for v in variants] == [2, 1]
    assert [v["asset_id"] for v in variants] == ["out-b", "out-a"]
    assert variants[1]["aspect_ratio"] == "9:16"
    assert variants[1]["total_duration_sec"] == 12.0
    assert variants[1]["edl_id"] == "edl-1"


def test_unrendered_sibling_is_not_a_variant() -> None:
    """Una EDL sin output aparece como EDL editable pero no como variante."""
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a"), _edl(2)],
        assets=[_asset("out-a")],
    )
    package = build_delivery_package(store, PROJECT_ID, _brief_row())
    assert [v["edl_version"] for v in package["variants"]] == [1]
    assert package["video"]["asset_id"] == "out-a"
    assert package["edl"]["version"] == 1, "el video final sigue siendo la única con render"


def test_missing_project_raises_lookup_error() -> None:
    store = FakeStore(project=NO_PROJECT)
    with pytest.raises(LookupError, match="no encontrado"):
        build_delivery_package(store, PROJECT_ID, _brief_row())


def test_empty_project_yields_urls_none_and_false_cover() -> None:
    store = FakeStore(edls=[], assets=[])
    package = build_delivery_package(store, PROJECT_ID, _brief_row())
    assert package["schema"] == DELIVERY_SCHEMA
    assert package["video"] == {
        "asset_id": None,
        "file_url": None,
        "download_url": None,
        "view_url": None,
    }
    assert package["variants"] == []
    assert package["preview_asset_ids"] == []
    assert package["cover"] == {
        "asset_id": None,
        "file_url": None,
        "download_url": None,
        "view_url": None,
    }
    assert package["subtitles"] is None
    assert package["edl"] is None
    assert package["publishing"]["published"] is False


def test_final_video_falls_back_to_last_output_without_edl() -> None:
    """Sin ninguna EDL, el video final es el último asset de category=outputs."""
    store = FakeStore(
        edls=[],
        assets=[
            _asset("out-1", created=1),
            _asset("out-2", created=2),
            _asset("out-3", created=3),
        ],
    )
    package = build_delivery_package(store, PROJECT_ID, _brief_row())
    assert package["video"]["asset_id"] == "out-3"
    assert package["edl"] is None


def test_cover_and_preview_come_from_keyframes() -> None:
    """La portada es el primer keyframe; el preview usa hasta 8."""
    kfs = [_asset(f"kf-{i}", kind="image", category="keyframes", created=i) for i in range(9)]
    store = FakeStore(edls=[_edl(1, output_asset_id="out-a")], assets=[_asset("out-a"), *kfs])
    package = build_delivery_package(store, PROJECT_ID, _brief_row())
    assert package["cover"]["asset_id"] == "kf-0"
    assert package["preview_asset_ids"] == [f"kf-{i}" for i in range(8)]


def test_cover_falls_back_to_first_image_when_no_keyframes() -> None:
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a")],
        assets=[_asset("out-a"), _asset("img-1", kind="image", category="uploads")],
    )
    package = build_delivery_package(store, PROJECT_ID, _brief_row())
    assert package["cover"]["asset_id"] == "img-1"
    assert package["preview_asset_ids"] == ["out-a"]


def test_generated_assets_carry_provenance_metadata() -> None:
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a")],
        assets=[_asset("out-a", metadata={"engine": "wan", "simulated": True})],
    )
    generated = build_delivery_package(store, PROJECT_ID, _brief_row())["generated_assets"]
    assert [a["asset_id"] for a in generated] == ["out-a"]
    assert generated[0]["provenance"] == {"engine": "wan", "simulated": True}
    assert generated[0]["filename"] == "out-a.mp4"


def test_edl_summary_exposes_render_url_and_counts() -> None:
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a", cuts=[{"asset_id": "s1"}] * 4)],
        assets=[_asset("out-a")],
    )
    edl = build_delivery_package(store, PROJECT_ID, _brief_row())["edl"]
    assert edl["version"] == 1
    assert edl["schema"] == "comfy_studio.edl.v1"
    assert edl["cuts_count"] == 4
    assert edl["list_url"] == f"/studio/api/projects/{PROJECT_ID}/edl"
    assert edl["render_url"] == f"/studio/api/projects/{PROJECT_ID}/edl/1/render"


def test_subtitles_linked_through_edl_sources() -> None:
    """Los subtítulos solo salen de transcripciones de assets que usa el EDL."""
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a", cuts=[{"asset_id": "src-1"}])],
        assets=[_asset("out-a")],
        transcriptions=[
            _transcription(
                "tr-1",
                "src-1",
                [{"text": "hola", "start": 0.0, "end": 1.0}],
                diarized=True,
            ),
            _transcription(
                "tr-2",
                "src-9",  # no está en el EDL: queda fuera
                [{"text": "adios", "start": 0.0, "end": 1.0}],
            ),
        ],
    )
    subtitles = build_delivery_package(store, PROJECT_ID, _brief_row())["subtitles"]
    assert subtitles["transcription_id"] == "tr-1"
    assert subtitles["source_asset_id"] == "src-1"
    assert subtitles["diarized"] is True
    assert subtitles["language"] == "es"
    assert subtitles["srt_url"].endswith("/subtitles?fmt=srt")
    assert subtitles["vtt_url"].endswith("/subtitles?fmt=vtt")


@pytest.mark.parametrize(
    "brief_override",
    [
        {"user_constraints": [{"key": "subtitles", "value": False}]},
        {"editorial": {"audio_plan": {"subtitles_policy": "disabled"}}},
    ],
)
def test_subtitles_suppressed_by_constraint(brief_override: dict[str, Any]) -> None:
    """Dos vías distintas apagan los subtítulos; la política manda."""
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a", cuts=[{"asset_id": "src-1"}])],
        assets=[_asset("out-a")],
        transcriptions=[
            _transcription("tr-1", "src-1", [{"text": "hola", "start": 0.0, "end": 1.0}])
        ],
    )
    package = build_delivery_package(store, PROJECT_ID, _brief_row(**brief_override))
    assert package["subtitles"] is None


def test_unexportable_transcription_is_dropped() -> None:
    """Sin segmentos con texto + start + end no hay subtítulos exportables."""
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a", cuts=[{"asset_id": "src-1"}])],
        assets=[_asset("out-a")],
        transcriptions=[
            _transcription("tr-1", "src-1", [{"text": "   ", "start": 0.0, "end": 1.0}])
        ],
    )
    assert build_delivery_package(store, PROJECT_ID, _brief_row())["subtitles"] is None


def test_texts_never_claim_trend_data() -> None:
    """Título/caption/hashtags vienen del brief; las tendencias no existen."""
    store = FakeStore(edls=[], assets=[])
    texts = build_delivery_package(
        store,
        PROJECT_ID,
        _brief_row(main_message="Lanzamiento", purpose="vender", piece_type="ad"),
    )["texts"]
    assert texts["title"] == "Lanzamiento"
    assert "vender" in texts["caption"]
    assert texts["trend_data"]["available"] is False
    assert texts["trend_data"]["sources"] == []
    assert texts["evidence_refs"] == []


def test_hashtag_count_follows_platform() -> None:
    keywords = [f"k{i}" for i in range(8)]

    def _tags(platform: str | None) -> list[str]:
        audience = {"platform": platform} if platform else {}
        store = FakeStore(edls=[], assets=[])
        return build_delivery_package(
            store,
            PROJECT_ID,
            _brief_row(keywords=keywords, audience=audience, piece_type="reel"),
        )["texts"]["hashtags"]

    assert len(_tags("instagram")) == 6  # 5 por plataforma + #reel
    assert len(_tags("whatsapp")) == 1  # 0 por plataforma + #reel
    assert len(_tags(None)) == 4  # 3 por defecto + #reel
    assert _tags("instagram")[0] == "#k0"


def test_limitations_include_simulated_facts_and_pending() -> None:
    store = FakeStore(edls=[], assets=[])
    row = _brief_row(
        asset_facts=[
            {"id": "f1", "simulated": True},
            {"id": "f2", "simulated": True},
            {"id": "f3", "simulated": False},
        ],
        pending=[{"question": "¿Qué duración?"}],
    )
    limitations = build_delivery_package(store, PROJECT_ID, row)["limitations"]
    assert "Sin acceso a datos de tendencias." in limitations
    assert any("2 hecho(s)" in msg and "simulados" in msg for msg in limitations)
    assert "Pendiente: ¿Qué duración?" in limitations


def test_decisions_summarise_editorial_counts() -> None:
    store = FakeStore(edls=[], assets=[])
    decisions = build_delivery_package(
        store,
        PROJECT_ID,
        _brief_row(
            assumptions=["audiencia joven"],
            editorial={
                "promise": "tres cortes por minuto",
                "structure": [{"name": "gancho"}, {"name": "cierre"}],
                "material": {"use": ["src-1"], "skip": ["src-2", "src-3"]},
            },
        ),
    )["decisions"]
    assert decisions["assumptions"] == ["audiencia joven"]
    assert decisions["editorial_summary"]["structure_moments"] == 2
    assert decisions["editorial_summary"]["material_used"] == 1
    assert decisions["editorial_summary"]["material_skipped"] == 2
    assert decisions["editorial_summary"]["promise"] == "tres cortes por minuto"


# ---------------------------------------------------------------------------
# provenance.py
# ---------------------------------------------------------------------------


def test_source_ids_deduplicated_in_first_seen_order() -> None:
    store = FakeStore(
        edls=[
            _edl(
                1,
                output_asset_id="out-a",
                cuts=[
                    {"asset_id": "s1"},
                    {"asset_id": "s2"},
                    {"asset_id": "s1"},
                    "no-es-dict",
                    {},
                ],
            )
        ]
    )
    assert source_asset_ids_for_output(store, PROJECT_ID, "out-a") == ["s1", "s2"]


def test_source_ids_fallback_and_empty_cases() -> None:
    store = FakeStore(
        edls=[
            _edl(1, output_asset_id="out-a", cuts=[{"source_asset_id": "legacy-1"}]),
            _edl(2, output_asset_id="out-b", cuts=[]),
        ]
    )
    assert source_asset_ids_for_output(store, PROJECT_ID, "out-a") == ["legacy-1"]
    assert source_asset_ids_for_output(store, PROJECT_ID, "out-b") == []
    assert source_asset_ids_for_output(store, PROJECT_ID, None) == []
    assert source_asset_ids_for_output(store, PROJECT_ID, "out-nope") == []


def test_source_ids_stop_at_first_matching_version() -> None:
    """Pin del corte temprano (provenance.py:25).

    Dos versiones producen el mismo output; solo cuenta la PRIMERA que aparece
    en orden descendente. No es una búsqueda acumulativa y no es un olvido: el
    contrato es "los assets de la versión que produjo este output", y con
    version DESC eso es la más reciente que coincide.
    """
    store = FakeStore(
        edls=[
            _edl(2, output_asset_id="out-a", cuts=[{"asset_id": "nuevo"}]),
            _edl(1, output_asset_id="out-a", cuts=[{"asset_id": "viejo"}]),
        ]
    )
    assert source_asset_ids_for_output(store, PROJECT_ID, "out-a") == ["nuevo"]
    assert "viejo" not in source_asset_ids_for_output(store, PROJECT_ID, "out-a")


def test_linked_transcriptions_filters_unexportable_segments() -> None:
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a", cuts=[{"asset_id": "s1"}, {"asset_id": "s2"}])],
        transcriptions=[
            _transcription("tr-empty", "s1", []),
            _transcription("tr-blank", "s1", [{"text": "", "start": 0.0, "end": 1.0}]),
            _transcription("tr-noend", "s1", [{"text": "a", "start": 0.0}]),
            _transcription("tr-ok", "s2", [{"text": "b", "start": 0.0, "end": 1.0}]),
        ],
    )
    linked = linked_transcriptions(store, PROJECT_ID, "out-a")
    assert [t["id"] for t in linked] == ["tr-ok"]


def test_linked_transcriptions_keep_source_order() -> None:
    store = FakeStore(
        edls=[_edl(1, output_asset_id="out-a", cuts=[{"asset_id": "s1"}, {"asset_id": "s2"}])],
        transcriptions=[
            _transcription("tr-a", "s1", [{"text": "a", "start": 0.0, "end": 1.0}]),
            _transcription("tr-b", "s2", [{"text": "b", "start": 0.0, "end": 1.0}]),
        ],
    )
    assert [t["id"] for t in linked_transcriptions(store, PROJECT_ID, "out-a")] == [
        "tr-a",
        "tr-b",
    ]
    assert linked_transcriptions(store, PROJECT_ID, None) == []


def test_provenance_with_real_store_roundtrip(tmp_path: Path) -> None:
    """Con SQLite real: output -> fuentes -> transcripción exportable."""
    store = ProjectStore(tmp_path / "data")
    pid = store.create_project("Provenance")["id"]
    src = store.save_upload(
        pid,
        filename="clip.mp4",
        content_type="video/mp4",
        data=b"\x00" * 32,
        max_upload_bytes=1024,
    )
    output = store.save_upload(
        pid,
        filename="final.mp4",
        content_type="video/mp4",
        data=b"\x00" * 32,
        max_upload_bytes=1024,
        category="outputs",
    )
    store.save_edl_version(
        pid,
        edl={"title": "v1", "cuts": [{"asset_id": src["id"]}, {"asset_id": src["id"]}]},
        output_asset_id=output["id"],
    )
    tr = store.save_transcription(
        pid,
        asset_id=src["id"],
        language="es",
        segments=[{"text": "hola", "start": 0.0, "end": 1.0}],
    )

    assert source_asset_ids_for_output(store, pid, output["id"]) == [src["id"]]
    linked = linked_transcriptions(store, pid, output["id"])
    assert [t["id"] for t in linked] == [tr["id"]]

    package = build_delivery_package(store, pid, _brief_row())
    assert package["video"]["asset_id"] == output["id"]
    assert package["subtitles"]["transcription_id"] == tr["id"]
    assert package["subtitles"]["source_asset_id"] == src["id"]


def test_unknown_ids_are_stable() -> None:
    """Un proyecto/id inexistente devuelve listas vacías, no lanza."""
    store = FakeStore(edls=[], assets=[])
    assert source_asset_ids_for_output(store, "missing", "x") == []
    assert linked_transcriptions(store, "missing", "x") == []
