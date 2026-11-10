"""`resolve_dest` respeta destinos absolutos en cualquier plataforma.

En Linux un destino absoluto empieza por `/` y ya se respetaba; en Windows
además lleva unidad de disco (`C:/...`). Antes solo se comprobaba el prefijo
POSIX, así que un manifest con una ruta absoluta del host caía en la rama
relativa y era confinado contra `COMFY_DATA_DIR` (el inventario de
`/studio/api/status` reventaba con `manifest destination escapes allowed root`).
"""

from __future__ import annotations

from pathlib import Path

from comfy_studio.model_assets import resolve_dest


def test_destino_absoluto_del_host_se_respeta_tal_cual(tmp_path: Path) -> None:
    # `str(Path)` es absoluto en la plataforma corriente: `/tmp/...` en Linux,
    # `C:\...` en Windows. Ambos deben volver sin confinar.
    absolute = tmp_path / "comfyui-data" / "checkpoints" / "modelo.safetensors"
    resolved = resolve_dest(str(absolute), env={})
    assert resolved == absolute


def test_destino_absoluto_con_separadores_posix_tambien_se_respeta(
    tmp_path: Path,
) -> None:
    # Un manifest puede declarar la ruta con `/` aunque el host use `\`.
    absolute = tmp_path / "weights" / "voz.pth"
    declared = str(absolute).replace("\\", "/")
    assert resolve_dest(declared, env={}) == absolute


def test_destino_relativo_sigue_confinado_contra_comfy_data_dir(
    tmp_path: Path,
) -> None:
    env = {"COMFY_DATA_DIR": str(tmp_path / "data")}
    resolved = resolve_dest("checkpoints/relativo.safetensors", env=env)
    assert resolved == tmp_path / "data" / "checkpoints" / "relativo.safetensors"
    assert resolved.is_relative_to(tmp_path / "data")


def test_traversia_relativa_sigue_rechazada(tmp_path: Path) -> None:
    env = {"COMFY_DATA_DIR": str(tmp_path / "data")}
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    # `resolve()` pliega `..`, así que el destino queda fuera de la raíz y el
    # confinamiento debe rechazarlo en ambas plataformas.
    try:
        resolve_dest("../fuera.safetensors", env=env)
    except ValueError as exc:
        assert "escapes allowed root" in str(exc)
    else:
        raise AssertionError("la traversia relativa no fue rechazada")
