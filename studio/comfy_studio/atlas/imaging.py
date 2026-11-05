"""Lectura de dimensiones/MIME de imágenes sin dependencias (stdlib).

Soporta PNG, JPEG, GIF y WebP lo suficiente para verificar que un archivo del
atlas es una imagen decodable y registrar sus dimensiones reales. No hace
upscale ni simula detalle: solo lee encabezados.
"""

from __future__ import annotations

import struct
from pathlib import Path

_RASTER_MIMES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/tiff",
    "image/bmp",
}


def sniff_mime(path: Path) -> str | None:
    """MIME por firma de bytes (no por extensión)."""
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.lstrip()[:5].lower() in (b"<svg ", b"<?xml") and b"<svg" in head.lower():
        return "image/svg+xml"
    return None


def is_raster_mime(mime: str | None) -> bool:
    return mime in _RASTER_MIMES


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """(ancho, alto) leídos del encabezado, o None si no se reconoce/decodifica."""
    mime = sniff_mime(path)
    try:
        if mime == "image/png":
            return _png_dims(path)
        if mime == "image/jpeg":
            return _jpeg_dims(path)
        if mime == "image/gif":
            return _gif_dims(path)
        if mime == "image/webp":
            return _webp_dims(path)
    except (OSError, struct.error, ValueError):
        return None
    return None


def _png_dims(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("png inválido")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _gif_dims(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(10)
    if len(head) < 10:
        raise ValueError("gif inválido")
    width, height = struct.unpack("<HH", head[6:10])
    return width, height


def _jpeg_dims(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        data = fh.read(2)
        if data != b"\xff\xd8":
            raise ValueError("jpeg inválido")
        while True:
            byte = fh.read(1)
            if not byte:
                raise ValueError("jpeg sin SOF")
            if byte != b"\xff":
                continue
            marker = fh.read(1)
            if not marker:
                raise ValueError("jpeg truncado")
            code = marker[0]
            if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                continue
            length_bytes = fh.read(2)
            if len(length_bytes) < 2:
                raise ValueError("jpeg truncado")
            seg_len = struct.unpack(">H", length_bytes)[0]
            sof_codes = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
            if code in sof_codes:
                payload = fh.read(5)
                if len(payload) < 5:
                    raise ValueError("jpeg truncado")
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            fh.seek(seg_len - 2, 1)


def _webp_dims(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(30)
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        raise ValueError("webp inválido")
    chunk = head[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(head[24:27], "little")
        height = 1 + int.from_bytes(head[27:30], "little")
        return width, height
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", head[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(head[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise ValueError("webp chunk desconocido")
