"""Tests de la validación de URLs de visión del LLM (`validate_DataUrl_only`).

Regla de seguridad 5 del AGENTS.md: nunca se aceptan URLs provistas por el usuario que
apunten a servicios internos de loopback. Estas pruebas son deterministas y CPU-only
(sin GPU, sin red, sin LLM, sin descargas de pesos).
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from comfy_studio.agent.llm import (
    encode_image_to_data_url,
    validate_DataUrl_only,
    validate_no_auth_urls_in_messages,
)

_PNG_1x1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")
_JPEG_1x1 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 60).decode("ascii")
_VALID_JPEG_URL = f"data:image/jpeg;base64,{_JPEG_1x1}"
_WEBP_1x1 = base64.b64encode(b"RIFF" + b"\x00" * 60).decode("ascii")
_VALID_WEBP_URL = f"data:image/webp;base64,{_WEBP_1x1}"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

VALID_PNG_URL = f"data:image/png;base64,{_PNG_1x1}"

# Payload adversarial: una URL de loopback con credenciales incrustada tras `;base64,`.
# El validador debe rechazarla tanto por el `=` (no es alfabeto base64) como por tamaño.
_LOOPBACK_SSRF_PAYLOAD = "https://user:not-a-real-token@127.0.0.1:8188/view?filename=keyframe.png"


def _accepts(value: str | None) -> bool:
    try:
        validate_DataUrl_only(value)
    except ValueError:
        return False
    return True


# --- `validate_DataUrl_only` -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Loopback / SSRF directo.
        "http://127.0.0.1:8188/view?filename=keyframe.png",
        "https://127.0.0.1:8188/view?filename=keyframe.png",
        "https://user:not-a-real-token@127.0.0.1:8188/view?filename=keyframe.png",
        "http://localhost:8188/view",
        "http://[::1]:8188/view",
        # Protocol-relative y otros esquemas.
        "//127.0.0.1:8188/view",
        "file:///etc/passwd",
        "ftp://127.0.0.1/x.png",
        "data://127.0.0.1:8188/x",
        # MIME no imagen.
        "data:text/html;base64,PGgxPmhpPC9oMT4=" + "A" * 40,
        "data:application/octet-stream;base64," + "A" * 40,
        "data:image/svg+xml;base64," + "A" * 40,
        # Sin marcador `;base64,` (percent-encoded permite esconder una URL completa).
        "data:image/png,http%3A%2F%2F127.0.0.1%3A8188%2Fview",
        "data:image/png," + _LOOPBACK_SSRF_PAYLOAD,
        # Parámetros de metadata antes de la coma.
        "data:image/png;charset=utf-8," + "A" * 40,
        "data:image/png;base64,charset=" + _LOOPBACK_SSRF_PAYLOAD,
        "data:image/png;name=" + _LOOPBACK_SSRF_PAYLOAD + ";base64," + "A" * 40,
        "data:image/png;",
        "data:image/png",
        "data:image/",
        # Payload inválido.
        "data:image/png;base64," + _LOOPBACK_SSRF_PAYLOAD,
        "data:image/png;base64,***not-base64***",
        "data:image/png;base64,",
        "data:image/png;base64," + "ñ",
        "data:image/png;base64,AA=A",
        "data:image/png;base64,A",
        "data:image/png;base64,YQ==\n" + _LOOPBACK_SSRF_PAYLOAD,
        # Payload excesivo: protege a la petición de un data URL gigante (>20 MB).
        "data:image/png;base64," + "A" * (28 * 1024 * 1024 // 3 * 4),
        # Vacío / None / whitespace.
        "",
        "   ",
        "\t\n",
        None,
    ],
)
def test_validate_data_url_only_rejects(url: str | None) -> None:
    with pytest.raises(ValueError, match="base64"):
        validate_DataUrl_only(url)


@pytest.mark.parametrize(
    "url",
    [
        VALID_PNG_URL,
        f"  {VALID_PNG_URL}  ",
        _VALID_JPEG_URL,
        _VALID_WEBP_URL,
    ],
)
def test_validate_data_url_only_accepts(url: str) -> None:
    validate_DataUrl_only(url)


def test_validate_data_url_only_accepts_output_of_encoder() -> None:
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    url = encode_image_to_data_url(data, "image/png")
    validate_DataUrl_only(url)
    assert url.startswith("data:image/png;base64,")


def test_validate_data_url_only_rejects_unpadded_payload() -> None:
    # Un payload sin padding `=` no es base64 canónico y ayudaría a evadir listas negras.
    with pytest.raises(ValueError, match="base64"):
        validate_DataUrl_only("data:image/png;base64," + _PNG_1x1.rstrip("=") + "x")


def test_validate_data_url_only_rejects_payload_beyond_size_cap() -> None:
    giant = base64.b64encode(b"\x00" * (20 * 1024 * 1024 + 4096)).decode("ascii")
    with pytest.raises(ValueError, match="base64"):
        validate_DataUrl_only(f"data:image/png;base64,{giant}")


# --- `encode_image_to_data_url` ----------------------------------------------


def test_encode_image_to_data_url_normalizes_jpg_to_jpeg() -> None:
    url = encode_image_to_data_url(b"\xff\xd8\xff\xe0" + b"\x00" * 60, "image/jpg")
    assert url.startswith("data:image/jpeg;base64,")
    validate_DataUrl_only(url)


def test_encode_image_to_data_url_preserves_png_and_webp() -> None:
    png_url = encode_image_to_data_url(_PNG_BYTES)
    assert png_url.startswith("data:image/png;base64,")
    webp_url = encode_image_to_data_url(b"RIFF" + b"\x00" * 40, "IMAGE/WEBP")
    assert webp_url.startswith("data:image/webp;base64,")


def test_encode_image_to_data_url_strips_mime_parameters() -> None:
    url = encode_image_to_data_url(_PNG_BYTES, "image/png; charset=utf-8")
    assert url.startswith("data:image/png;base64,")


def test_encode_image_to_data_url_defaults_to_png() -> None:
    url = encode_image_to_data_url(_PNG_BYTES, "")
    assert url.startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "mime",
    [
        "text/html",
        "application/pdf",
        "image/svg+xml",
        "image/gif",
        "video/mp4",
        "data:image/png",
    ],
)
def test_encode_image_to_data_url_rejects_disallowed_mime(mime: str) -> None:
    with pytest.raises(ValueError, match="MIME"):
        encode_image_to_data_url(_PNG_BYTES, mime)


@pytest.mark.parametrize("bad", [b"", None, "not-bytes", bytearray()])
def test_encode_image_to_data_url_rejects_empty_payload(bad: Any) -> None:
    with pytest.raises(ValueError):
        encode_image_to_data_url(bad)  # type: ignore[arg-type]


# --- `validate_no_auth_urls_in_messages` -------------------------------------


def _vision_part(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url, "detail": "auto"}}


def test_validate_no_auth_urls_in_messages_accepts_nested_base64() -> None:
    text_part = {"type": "text", "text": "mira"}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Eres un asistente."},
        {"role": "user", "content": "texto plano"},
        {"role": "user", "content": [text_part, _vision_part(VALID_PNG_URL)]},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [_vision_part(VALID_PNG_URL)]},
        {"role": "user", "content": ["ignorado", 42, None]},
    ]
    validate_no_auth_urls_in_messages(messages)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url"},
        {"type": "image_url", "image_url": "no-es-dict"},
        {"type": "image_url", "image_url": {}},
        {"type": "image_url", "image_url": None},
    ],
)
def test_validate_no_auth_urls_in_messages_rejects_malformed_part(part: Any) -> None:
    # Una parte `image_url` sin URL vÃ¡lida no es una imagen en base64: se rechaza.
    with pytest.raises(ValueError, match="base64"):
        validate_no_auth_urls_in_messages([{"role": "user", "content": [part]}])


@pytest.mark.parametrize(
    "url",
    [
        "https://user:not-a-real-token@127.0.0.1:8188/view?filename=k.png",
        "http://127.0.0.1:8188/view?filename=k.png",
        "//127.0.0.1:8188/view",
        "file:///etc/passwd",
        "data:text/html;base64,PGgxPmhpPC9oMT4=" + "A" * 40,
        "data:image/png;base64," + _LOOPBACK_SSRF_PAYLOAD,
        "data:image/png;charset=utf-8," + "A" * 40,
        "",
    ],
)
def test_validate_no_auth_urls_in_messages_rejects_bad_nested_url(url: str) -> None:
    content = [{"type": "text", "text": "mira"}, _vision_part(VALID_PNG_URL), _vision_part(url)]
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    with pytest.raises(ValueError, match="base64"):
        validate_no_auth_urls_in_messages(messages)


def test_validate_no_auth_urls_in_messages_handles_empty_and_scalar_content() -> None:
    validate_no_auth_urls_in_messages([])
    validate_no_auth_urls_in_messages([{"role": "user"}])
    validate_no_auth_urls_in_messages([{"role": "user", "content": _LOOPBACK_SSRF_PAYLOAD}])
