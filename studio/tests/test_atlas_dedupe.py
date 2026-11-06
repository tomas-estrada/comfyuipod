"""PR-2 atlas-dedupe-fuerte: sha1 persistente y Hamming con borde de umbral."""

from __future__ import annotations

import json
from pathlib import Path

from test_atlas_core import _ref
from test_atlas_resume import _working_pipe

from comfy_studio.atlas.acquire.pipeline import (
    PHASH_HAMMING_THRESHOLD,
    AcquisitionPipeline,
    phash_distance,
)
from comfy_studio.atlas.models import Environment, Place
from comfy_studio.atlas.service import AtlasService
from comfy_studio.atlas.store import AtlasStore


def _place(root: Path, store: AtlasStore) -> None:
    store.upsert_place(Place(place_id="plc_dup", municipality="Medellín",
                             name="Puente Duplicado", kind=Environment.BRIDGE,
                             lat=6.20, lon=-75.58))


def _mkref(store: AtlasStore, ref_id: str, phash: str, quality: float) -> None:
    r = _ref(ref_id, "plc_dup", 6.20, -75.58, title=f"Vista {ref_id}")
    r.phash = phash
    r.quality = quality
    store.upsert_reference(r)


# ---------------------------------------------------------------------------
# distancia Hamming pura
# ---------------------------------------------------------------------------


def test_phash_distance_basics() -> None:
    assert phash_distance("0" * 16, "0" * 16) == 0
    assert phash_distance("0" * 16, "0" * 15 + "f") == 4
    assert phash_distance("0" * 16, "0" * 15 + "1") == 1
    assert phash_distance("0" * 16, "f" * 16) == 64
    assert phash_distance("0" * 16, "0" * 16) == phash_distance("0" * 16, "0" * 16)
    assert phash_distance(None, "0" * 16) is None
    assert phash_distance("0" * 16, None) is None
    assert phash_distance("zz", "0" * 16) is None
    assert phash_distance("", "") is None


def test_umbral_es_cuatro() -> None:
    assert PHASH_HAMMING_THRESHOLD == 4


# ---------------------------------------------------------------------------
# colapso por phash cercano (borde del umbral fijado en ambos lados)
# ---------------------------------------------------------------------------


def test_borde_umbral_cuatro_colapsa_cinco_conserva(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    pipe = AcquisitionPipeline(root, min_interval_s=0.0)
    store = pipe.svc.store
    _place(root, store)
    _mkref(store, "ref_keep", "0" * 16, 0.9)
    _mkref(store, "ref_d4", "0" * 15 + "f", 0.5)  # 0x0f: 4 bits -> colapsa
    _mkref(store, "ref_d5", "0" * 14 + "1f", 0.5)  # 0x1f: 5 bits -> se conserva

    assert pipe._dedupe_phash() == 1
    keep = store.get_reference("ref_keep")
    d4 = store.get_reference("ref_d4")
    d5 = store.get_reference("ref_d5")
    assert keep is not None and keep.usable
    assert d4 is not None and not d4.usable
    assert d4.reject_reason == "duplicado perceptual (phash) de ref_keep"
    assert d5 is not None and d5.usable
    store.close()


def test_exact_duplicate_keeps_best(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    pipe = AcquisitionPipeline(root, min_interval_s=0.0)
    store = pipe.svc.store
    _place(root, store)
    _mkref(store, "ref_low", "ab" * 8, 0.5)
    _mkref(store, "ref_mid", "ab" * 8, 0.7)
    _mkref(store, "ref_top", "ab" * 8, 0.9)

    assert pipe._dedupe_phash() == 2
    assert store.get_reference("ref_top").usable  # type: ignore[union-attr]
    for rid in ("ref_low", "ref_mid"):
        r = store.get_reference(rid)
        assert r is not None and not r.usable
        assert r.reject_reason == "duplicado perceptual (phash) de ref_top"
    store.close()


def test_lugares_distintos_no_colapsan(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    pipe = AcquisitionPipeline(root, min_interval_s=0.0)
    store = pipe.svc.store
    _place(root, store)
    store.upsert_place(Place(place_id="plc_otro", municipality="Bello",
                             name="Otro", kind=Environment.BRIDGE,
                             lat=6.33, lon=-75.55))
    _mkref(store, "ref_a", "0" * 16, 0.9)
    r = _ref("ref_b", "plc_otro", 6.33, -75.55, title="Otra")
    r.phash = "0" * 16
    store.upsert_reference(r)

    assert pipe._dedupe_phash() == 0
    store.close()


# ---------------------------------------------------------------------------
# sha1 del original persistente entre pasadas
# ---------------------------------------------------------------------------


def test_sha1_persistente_rechaza_reempaquetado(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    pipe = _working_pipe(root)
    run1 = pipe.run_commons()
    assert run1["ok"] and run1["downloaded"] == 2, run1
    store = AtlasService(root).store
    sha_a = next(r.sha1_source for r in store.all_references()
                 if "foto A" in r.title)
    titles_a = [c for c in _candidates(root) if "foto A" in c["title"]]
    assert titles_a, "la corrida 1 debió ver foto A"
    store.close()

    # la fuente deja de listar foto A y reaparece el mismo original con otro
    # título: el sha1 persistente debe cazarlo aunque `seen` esté vacío.
    _swap_candidate(root, titles_a[0]["title"], "File:Puente 4 Sur foto E.jpg", sha_a)
    run2 = pipe.run_commons()
    assert run2["ok"] is True, run2
    assert run2["downloaded"] == 0
    assert pipe.svc.store.acq_item_status(
        "main", "commons", "File:Puente 4 Sur foto E.jpg") == "duplicate_sha1"
    store2 = AtlasService(root).store
    assert len(store2.all_references()) == 2
    assert not [r for r in store2.all_references() if "foto E" in r.title]
    store2.close()


def _candidates(root: Path) -> list[dict]:
    return json.loads((root / ".cache" / "candidates.json").read_text(encoding="utf-8"))


def _swap_candidate(root: Path, old_title: str, new_title: str, sha1: str) -> None:
    candidates_path = root / ".cache" / "candidates.json"
    infos_path = root / ".cache" / "infos.json"
    candidates = [c for c in json.loads(candidates_path.read_text(encoding="utf-8"))
                  if c["title"] != old_title]
    infos = json.loads(infos_path.read_text(encoding="utf-8"))
    base = infos[old_title]
    candidates.append({"title": new_title, "lat": 6.2090, "lon": -75.5672})
    infos[new_title] = {**base, "title": new_title,
                        "url": "https://upload.wikimedia.org/fake/" + new_title.replace(" ", "_"),
                        "sha1": sha1}
    del infos[old_title]
    candidates_path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    infos_path.write_text(json.dumps(infos, ensure_ascii=False), encoding="utf-8")
