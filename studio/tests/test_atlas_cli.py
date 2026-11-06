"""PR-4 atlas-cli-robusta: el CLI real, con rc y salidas estables."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_atlas_core import _ref, make_png

from comfy_studio.atlas.cli import main
from comfy_studio.atlas.models import Environment, Place, View
from comfy_studio.atlas.store import AtlasStore, sha256_file


@pytest.fixture()
def cli_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_cli", municipality="Medellín",
                             name="Puente CLI", kind=Environment.BRIDGE,
                             aliases=["puente cli"],
                             lat=6.2088, lon=-75.5670))
    r = _ref("ref_cli", "plc_cli", 6.2089, -75.5671, title="Puente CLI")
    fpath = root / "files" / r.file_path
    make_png(fpath, 64, 48)
    r.sha256 = sha256_file(fpath)
    r.bytes = fpath.stat().st_size
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    store.upsert_view(View(view_id="view_cli", place_id="plc_cli",
                           ref_id="ref_cli", lat=6.2089, lon=-75.5671,
                           heading_deg=10.0))
    store.close()
    return root


def run(args: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str]:
    rc = main(args)
    out = capsys.readouterr().out
    return rc, out


def payload(out: str) -> dict:
    return json.loads(out)


def test_stats_y_search(cli_root: Path, capsys: pytest.CaptureFixture) -> None:
    for argv in (["--root", str(cli_root), "stats"],
                 ["stats", "--root", str(cli_root)]):
        rc, out = run(argv, capsys)
        assert rc == 0, argv
        assert payload(out)["places"] == 1
    rc, out = run(["--root", str(cli_root), "search", "puente cli"], capsys)
    assert rc == 0
    body = payload(out)
    assert body["count"] == 1 and body["places"][0]["place_id"] == "plc_cli"
    rc, out = run(["--root", str(cli_root), "search", "luna"], capsys)
    assert rc == 0 and payload(out) == {"count": 0, "places": []}


def test_resolve_place(cli_root: Path, capsys: pytest.CaptureFixture) -> None:
    rc, out = run(["--root", str(cli_root), "resolve", "Puente CLI"], capsys)
    assert rc == 0
    body = payload(out)
    assert body["status"] == "confirmed"
    assert body["place"]["place_id"] == "plc_cli"


def test_place_ok_y_desconocido(cli_root: Path, capsys: pytest.CaptureFixture) -> None:
    rc, out = run(["--root", str(cli_root), "place", "plc_cli"], capsys)
    assert rc == 0
    body = payload(out)
    assert body["place_id"] == "plc_cli"
    assert body["views_usable"] == 1
    assert body["coverage"]["views_observed"] == 1
    rc, _ = run(["--root", str(cli_root), "place", "plc_nope"], capsys)
    assert rc == 3


def test_select_view_ok_y_desconocido(
    cli_root: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, out = run(["--root", str(cli_root), "select-view", "plc_cli",
                   "--heading", "10"], capsys)
    assert rc == 0
    body = payload(out)
    assert body["status"] == "ok"
    assert body["view"]["view_id"] == "view_cli"
    rc, _ = run(["--root", str(cli_root), "select-view", "plc_nope"], capsys)
    assert rc == 3


def test_prepare_shot_ok_y_sin_sustitucion(
    cli_root: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, out = run(["--root", str(cli_root), "prepare-shot",
                   "--place", "Puente CLI", "--prompt", "carro"], capsys)
    assert rc == 0
    body = payload(out)
    assert body["status"] == "ok"
    assert body["place_id"] == "plc_cli"
    assert body["background_path"].endswith(".png")
    assert body["reference"]["license"] == "CC BY-SA 4.0"
    # lugar inexistente: status tipado en JSON, rc 0 (contrato documentado)
    rc, out = run(["--root", str(cli_root), "prepare-shot",
                   "--place", "Luna", "--prompt", "x"], capsys)
    assert rc == 0 and payload(out)["status"] == "place_not_found"


def test_verify_ok_y_fallo(cli_root: Path, capsys: pytest.CaptureFixture) -> None:
    rc, out = run(["--root", str(cli_root), "verify"], capsys)
    assert rc == 0 and payload(out)["ok"] is True
    (cli_root / "files" / "aa" / "ref_cli.png").unlink()
    rc, out = run(["--root", str(cli_root), "verify"], capsys)
    assert rc == 1 and payload(out)["ok"] is False


def test_manifest_y_map(cli_root: Path, capsys: pytest.CaptureFixture) -> None:
    rc, _ = run(["--root", str(cli_root), "manifest"], capsys)
    assert rc == 0 and (cli_root / "manifest.json").is_file()
    rc, out = run(["--root", str(cli_root), "map"], capsys)
    assert rc == 0 and out.lstrip().startswith("<svg")


def test_sin_catalogo_rc2(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc, _ = run(["--root", str(tmp_path / "vacio"), "stats"], capsys)
    assert rc == 2
    rc, _ = run(["--root", str(tmp_path / "vacio"), "place", "x"], capsys)
    assert rc == 2


def test_subcomando_desconocido(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(tmp_path), "noexiste"])
    assert exc.value.code == 2
