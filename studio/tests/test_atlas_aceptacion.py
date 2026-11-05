"""Casos de aceptación del atlas sobre el bundle real del repo (atlas/).

Se ejecutan contra `atlas/` del checkout (presente en CI y en la imagen). Si el
bundle no está, se omiten. Cubren los recorridos pedidos por la revisión:
ubicación explícita vs selección previa, prompt sin ubicación con elección
compatible con la tarea, asset con alfa sobre fondo real, cámara compatible e
incompatible, archivo ausente/modificado, reanudación sin cambios silenciosos,
y consulta/uso offline de los archivos incluidos.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ATLAS = REPO / "atlas"

pytestmark = pytest.mark.skipif(
    not (ATLAS / "catalog.db").is_file(), reason="bundle atlas/ no presente en el checkout"
)

PRIORITY_MUNICIPALITIES = ("Medellín", "Envigado", "Sabaneta", "Itagüí", "Bello")


@pytest.fixture(scope="module")
def svc(monkeypatch_module=None):
    import os

    os.environ["STUDIO_ATLAS_DIR"] = str(ATLAS)
    from comfy_studio.atlas.agent_tools import reset_shared_atlas_service

    reset_shared_atlas_service()
    from comfy_studio.atlas.service import AtlasService

    return AtlasService(ATLAS)


def test_dos_lugares_verificados_por_municipio_prioritario(svc) -> None:
    for mun in PRIORITY_MUNICIPALITIES:
        places = svc.search_places(municipality=mun, min_views=1, limit=50)
        assert len(places) >= 2, f"{mun}: sólo {len(places)} lugares con vistas"


def test_ubicacion_explicita_prevalece_sobre_seleccion_de_otro_proyecto(svc) -> None:
    # elegir dos lugares reales con vistas, uno por municipio distinto si se puede
    med = svc.search_places(municipality="Medellín", min_views=1, limit=5)
    env = svc.search_places(municipality="Envigado", min_views=1, limit=5)
    assert med and env
    a, b = med[0], env[0]
    # selección previa en el proyecto "p1" -> lugar A
    usable_a = svc._usable_views(a["place_id"])
    assert usable_a, "el lugar A debe tener vistas utilizables"
    svc.set_selection(a["place_id"], usable_a[0].view_id, project_id="p1")
    # prompt con ubicación explícita B en el mismo proyecto: gana B
    shot = svc.prepare_shot(place=b["name"], prompt=f"plano en {b['name']}", project_id="p1")
    assert shot["status"] == "ok"
    assert shot["place_id"] == b["place_id"], (
        f"la ubicación explícita no prevaleció: {shot['place_id']} != {b['place_id']}"
    )
    # el proyecto p2 (sin selección) no hereda la de p1
    sel_p2 = svc.get_selection("p2")
    assert sel_p2.get("place_id") != a["place_id"] or sel_p2.get("project_id") == "p2"


def test_prompt_sin_ubicacion_elige_escena_compatible_con_tarea(svc) -> None:
    shot = svc.prepare_shot(prompt="un carro recorriendo una avenida del Valle de Aburrá")
    assert shot["status"] == "ok"
    kind = shot["place"]["kind"]
    assert kind in ("road_corridor", "bridge", "street", "intersection"), (
        f"para una tarea de carro se eligió entorno incompatible: {kind}"
    )


def test_composicion_asset_alfa_sobre_fondo_real(svc, tmp_path: Path) -> None:
    from PIL import Image

    shot = svc.prepare_shot(prompt="composición de prueba")
    assert shot["status"] == "ok"
    # asset con alfa sintético (círculo rojo semitransparente)
    fg = tmp_path / "fg.png"
    im = Image.new("RGBA", (200, 120), (0, 0, 0, 0))
    for x in range(60, 140):
        for y in range(30, 90):
            im.putpixel((x, y), (200, 30, 30, 200))
    im.save(fg)
    out = tmp_path / "comp.png"
    comp = svc.compose_over_background(
        background_abs=shot["background_abs"], out_path=str(out), foreground_abs=str(fg)
    )
    assert out.is_file()
    assert comp["background_pixels_preserved_outside_foreground"] is True, (
        "los píxeles del fondo fuera del asset deben conservarse idénticos"
    )
    assert comp["background_sha256"] == shot["reference"]["sha256"]


def test_camara_compatible_e_incompatible(svc) -> None:
    place = svc.search_places(min_views=1, limit=1)[0]
    pid = place["place_id"]
    # sin datos de rumbo: pedir rumbo da limitación tipada, no compatibilidad falsa
    res = svc.select_view(pid, heading_deg=90)
    assert res["status"] in ("ok", "unsupported_camera")
    if res["status"] == "ok":
        codes = [lim["code"] for lim in res["limitations"]]
        assert any(c in ("heading_unknown", "heading_mismatch") for c in codes)
    # las vistas del bundle no traen rumbo: pedirlo da limitación tipada
    # (el caso rumbo-conocido 0° vs 180° está en test_atlas_regresiones, sobre
    # un atlas sintético para no mutar el catálogo distribuido)
    res2 = svc.select_view(pid, heading_deg=180.0)
    assert res2["status"] in ("ok", "unsupported_camera")
    codes2 = [lim["code"] for lim in res2["limitations"]]
    assert all(
        c in ("heading_unknown", "heading_mismatch", "camera_far_from_views", "fov_unknown")
        for c in codes2
    )


def test_archivo_modificado_falla_en_verificacion(svc, tmp_path: Path) -> None:
    # copiar un fondo real a un atlas tmp y corromperlo: verify debe fallar

    dst = tmp_path / "atlas"
    shutil.copytree(ATLAS, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("files", ".cache"))
    (dst / "files").mkdir(exist_ok=True)
    store = svc.store
    ref = next(r for r in store.all_references(usable_only=True))
    src_file = ATLAS / "files" / ref.file_path
    (dst / "files" / ref.file_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dst / "files" / ref.file_path)
    # catálogo copiado viene del bundle; corromper el archivo
    with open(dst / "files" / ref.file_path, "ab") as fh:
        fh.write(b"corrupto")
    from comfy_studio.atlas.service import AtlasService

    svc2 = AtlasService(dst)
    report = svc2.verify()
    assert report["ok"] is False
    assert any("sha256" in e["error"] or "tamaño" in e["error"] for e in report["errors"])


def test_reanudacion_estable_mismo_fondo_y_hash(svc) -> None:
    s1 = svc.prepare_shot(place="Puente de la 4 Sur")
    s2 = svc.prepare_shot(place="Puente de la 4 Sur")
    assert s1["status"] == s2["status"] == "ok"
    assert s1["ref_id"] == s2["ref_id"], "reanudar debe recuperar la misma referencia"
    assert s1["reference"]["sha256"] == s2["reference"]["sha256"]
    assert s1["atlas_version"] == s2["atlas_version"]


def test_consulta_y_uso_offline_del_bundle(svc) -> None:
    from comfy_studio.atlas.packaging import verify_bundle

    rep = verify_bundle(ATLAS)
    assert rep["ok"] is True, rep["errors"][:5]
    st = svc.stats()
    assert st["references_usable"] >= 500, f"refs útiles {st['references_usable']} < 500"
    assert st["places_with_usable_views"] >= 50
    assert st["scenarios"] >= 50
    # los archivos están en disco y se sirven sin red
    shot = svc.prepare_shot(prompt="plano offline")
    assert shot["status"] == "ok"
    assert Path(shot["background_abs"]).is_file()


def test_escenarios_registrados_con_capacidades(svc) -> None:
    store = svc.store
    scns = store.scenarios_for_place(svc.search_places(min_views=1, limit=1)[0]["place_id"])
    assert scns, "debe haber escenarios registrados"
    photo = next(s for s in scns if s.kind == "photo_bg")
    assert photo.status == "verified"
    assert photo.capabilities["preserve_background_pixels"] is True
    mv = [s for s in store.all_scenarios() if s.kind == "multiview"]
    for s in mv:
        assert s.status == "draft"
        assert s.capabilities["reconstruction_validated"] is False
