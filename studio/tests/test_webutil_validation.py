"""Validación de peticiones y guardas de subida de `comfy_studio.webutil` (CPU, offline).

Gap medido: ningún test importaba `webutil`; esta cubre de forma unitaria:

- validadores de forma: `AdVariantsRequest`, `EdlBuildRequest`,
  `ReviewPinCreateRequest` (`_validate_aspect_ratios`, `_validate_acceptance`,
  `_check_window` y las ramas de error de `EdlBuildRequest`);
- guardas de subida: `_check_upload_content_length` (rama con Content-Length
  declarado), `_bounded_request_stream`, `_BoundedMultiPartParser`,
  `_check_upload_free_disk` (ramas de omisión y frontera), `_max_upload_request_bytes`
  y `_is_media_upload_request`.

NO se replica aquí (ya cubierto en otros ficheros):
- 413 del techo TOTAL de subida: `test_m1_hardening.py` (M3-SEC-003).
- `_verify_basic_auth` / edge auth: `test_projects_media.py`,
  `test_health_auth.py`, `test_m1_endpoints.py`.
- 507 con disco lleno y pase con holgura: `test_reconcile_disk_guard.py`.

Determinista: sin GPU, red, pesos ni LLM; peticiones ASGI construidas a mano y
`shutil.disk_usage` simulado con monkeypatch. Sin secretos de entorno.
"""

from __future__ import annotations

from contextlib import aclosing

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.requests import Request

from comfy_studio.webutil import (
    AdVariantsRequest,
    EdlBuildRequest,
    ReviewPinCreateRequest,
    _bounded_request_stream,
    _BoundedMultiPartParser,
    _check_upload_content_length,
    _check_upload_free_disk,
    _is_media_upload_request,
    _max_upload_request_bytes,
)

# ---------------------------------------------------------------------------
# Constructores de peticiones ASGI (scope a mano, sin red ni servidor real)
# ---------------------------------------------------------------------------


def _make_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body_chunks: tuple[bytes, ...] = (),
) -> Request:
    """Request de Starlette sobre un scope ASGI mínimo y autocontenido."""
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    messages: list[dict] = []
    for index, chunk in enumerate(body_chunks):
        messages.append(
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(body_chunks) - 1,
            }
        )
    if not messages:
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    queue = list(messages)

    async def receive() -> dict:
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8188),
    }
    return Request(scope, receive)


def _multipart_body(parts: list[tuple[str, str, bytes]], boundary: str) -> bytes:
    out = b""
    for name, filename, content in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += f"--{boundary}\r\n{disposition}\r\n\r\n".encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out


async def _chunked_stream(body: bytes, size: int = 16):
    for offset in range(0, len(body), size):
        yield body[offset : offset + size]


# ---------------------------------------------------------------------------
# AdVariantsRequest._validate_aspect_ratios / _validate_acceptance
# ---------------------------------------------------------------------------


def test_ad_variants_aspect_ratios_strip_and_dedupe_preserve_order() -> None:
    req = AdVariantsRequest(aspect_ratios=[" 9:16 ", "16:9", "9:16", "1:1"])
    # recorta espacios y deduplica conservando la primera aparición
    assert req.aspect_ratios == ["9:16", "16:9", "1:1"]


def test_ad_variants_rejects_unsupported_ratio_with_guidance() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdVariantsRequest(aspect_ratios=["21:9"])
    message = excinfo.value.errors()[0]["msg"]
    assert "Relación de aspecto no soportada: '21:9'" in message
    # el error lista las opciones válidas ordenadas
    assert "['16:9', '1:1', '4:5', '9:16']" in message


def test_ad_variants_rejects_empty_aspect_ratios() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdVariantsRequest(aspect_ratios=[])
    assert "aspect_ratios no puede estar vacío" in excinfo.value.errors()[0]["msg"]


def test_ad_variants_five_raw_entries_with_duplicates_are_accepted() -> None:
    # Contraste documentado con EdlBuildRequest: aquí el Field NO tiene
    # max_length (webutil.py:332), así que la lista RAW de 5 pasa y el
    # validador deduplica a 2.
    req = AdVariantsRequest(aspect_ratios=["16:9", "16:9", "16:9", "16:9", "9:16"])
    assert req.aspect_ratios == ["16:9", "9:16"]


def test_ad_variants_dedupe_is_bounded_by_allowed_set_size() -> None:
    """Ancla el comportamiento actual frente al ramo inalcanzable `len(cleaned) > 4`
    (webutil.py:360-361): `cleaned` solo admite los 4 valores de `allowed` únicos,
    por lo que la rama "Máximo 4 variantes" jamás se ejecuta y repetir el conjunto
    completo N veces siempre deduplica a 4."""
    req = AdVariantsRequest(aspect_ratios=["16:9", "9:16", "1:1", "4:5"] * 3)
    assert len(req.aspect_ratios) == 4
    assert set(req.aspect_ratios) == {"16:9", "9:16", "1:1", "4:5"}


def test_ad_variants_acceptance_rejects_unknown_criteria() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdVariantsRequest(acceptance={"max_black_s": 1.0, "sharpness": 0.5})
    message = excinfo.value.errors()[0]["msg"]
    assert "Criterios QA no soportados" in message
    assert "['sharpness']" in message
    assert "['max_black_s', 'max_freeze_s']" in message


def test_ad_variants_acceptance_passes_allowed_criteria_through() -> None:
    req = AdVariantsRequest(acceptance={"max_black_s": 1, "max_freeze_s": 0.5})
    assert req.acceptance == {"max_black_s": 1.0, "max_freeze_s": 0.5}
    assert AdVariantsRequest(acceptance={}).acceptance == {}


def test_ad_variants_acceptance_accepts_negative_thresholds_current_behavior() -> None:
    # Comportamiento ACTUAL anclado: `acceptance` no aplica ge=0
    # (webutil.py:342), así que umbrales QA negativos pasan validación.
    req = AdVariantsRequest(acceptance={"max_black_s": -1.0, "max_freeze_s": -0.5})
    assert req.acceptance == {"max_black_s": -1.0, "max_freeze_s": -0.5}


def test_ad_variants_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdVariantsRequest(unexpected="x")
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("fps", [11, 61])
def test_ad_variants_rejects_out_of_range_fps(fps: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        AdVariantsRequest(fps=fps)
    assert excinfo.value.errors()[0]["loc"] == ("fps",)


def test_ad_variants_accepts_boundary_fps() -> None:
    assert AdVariantsRequest(fps=12).fps == 12
    assert AdVariantsRequest(fps=60).fps == 60


def test_ad_variants_default_is_valid_without_validator_run() -> None:
    # Los Field defaults no pasan por el validador; el default ya es válido.
    req = AdVariantsRequest()
    assert req.aspect_ratios == ["9:16", "1:1", "16:9"]


# ---------------------------------------------------------------------------
# ReviewPinCreateRequest._check_window
# ---------------------------------------------------------------------------


def test_review_pin_rejects_inverted_window_with_exact_message() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ReviewPinCreateRequest(edl_version=1, t_start=3.0, t_end=2.0, body="ok")
    message = excinfo.value.errors()[0]["msg"]
    assert "t_end (2.0) no puede ser menor que t_start (3.0)" in message


def test_review_pin_accepts_zero_width_window() -> None:
    # Frontera: t_end == t_start está permitida (solo el estrictamente menor falla).
    pin = ReviewPinCreateRequest(edl_version=1, t_start=2.5, t_end=2.5, body="ok")
    assert (pin.t_start, pin.t_end) == (2.5, 2.5)


def test_review_pin_rejects_negative_t_start() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ReviewPinCreateRequest(edl_version=1, t_start=-0.5, t_end=1.0, body="ok")
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("t_start",)
    assert error["type"] == "greater_than_equal"


def test_review_pin_rejects_version_below_one() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ReviewPinCreateRequest(edl_version=0, t_start=0.0, t_end=1.0, body="ok")
    assert excinfo.value.errors()[0]["loc"] == ("edl_version",)


def test_review_pin_rejects_unknown_severity() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ReviewPinCreateRequest(
            edl_version=1, t_start=0.0, t_end=1.0, severity="critical", body="ok"
        )
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("severity",)
    assert error["type"] == "literal_error"


def test_review_pin_rejects_empty_body() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ReviewPinCreateRequest(edl_version=1, t_start=0.0, t_end=1.0, body="")
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("body",)
    assert error["type"] == "string_too_short"


def test_review_pin_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ReviewPinCreateRequest(
            edl_version=1, t_start=0.0, t_end=1.0, body="ok", extra_field=1
        )
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


# ---------------------------------------------------------------------------
# EdlBuildRequest: ramas de error
# ---------------------------------------------------------------------------


def test_edl_build_rejects_version_below_one() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(version=0)
    assert excinfo.value.errors()[0]["loc"] == ("version",)


def test_edl_build_accepts_missing_or_valid_version() -> None:
    assert EdlBuildRequest().version is None
    assert EdlBuildRequest(version=1).version == 1


def test_edl_build_aspect_ratio_pattern_branch() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(aspect_ratio="21:9")
    assert excinfo.value.errors()[0]["loc"] == ("aspect_ratio",)
    assert EdlBuildRequest(aspect_ratio="9:16").aspect_ratio == "9:16"
    assert EdlBuildRequest(aspect_ratio=None).aspect_ratio is None


def test_edl_build_rejects_unsupported_entry_in_aspect_ratios() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(aspect_ratios=["16:9", "21:9"])
    message = excinfo.value.errors()[0]["msg"]
    assert "Relación de aspecto no soportada: '21:9'" in message


def test_edl_build_strips_and_dedupes_aspect_ratios() -> None:
    req = EdlBuildRequest(aspect_ratios=[" 16:9 ", "16:9", "9:16", "16:9"])
    assert req.aspect_ratios == ["16:9", "9:16"]
    assert req.resolved_aspect_ratios() == ["16:9", "9:16"]


def test_edl_build_rejects_five_raw_entries_even_with_duplicates() -> None:
    # Comportamiento ACTUAL documentado: el Field aplica max_length=4 a la lista
    # RAW (webutil.py:247) ANTES del dedupe del validador (webutil.py:259-271),
    # así que 5 entradas con duplicados se rechazan aunque el dedupe dejaría 2.
    # Contrasta con AdVariantsRequest, que acepta el mismo payload.
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(aspect_ratios=["16:9", "16:9", "16:9", "16:9", "9:16"])
    assert excinfo.value.errors()[0]["type"] == "too_long"
    assert EdlBuildRequest(aspect_ratios=["16:9", "16:9", "9:16", "1:1"]).aspect_ratios == [
        "16:9",
        "9:16",
        "1:1",
    ]


@pytest.mark.parametrize("fps", [11, 61])
def test_edl_build_rejects_out_of_range_fps(fps: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(fps=fps)
    assert excinfo.value.errors()[0]["loc"] == ("fps",)


def test_edl_build_accepts_boundary_fps() -> None:
    assert EdlBuildRequest(fps=12).fps == 12
    assert EdlBuildRequest(fps=60).fps == 60


def test_edl_build_rejects_non_positive_cut_interval() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(cuts=[{"in_sec": 5.0, "out_sec": 5.0}])
    errors = excinfo.value.errors()
    assert errors[0]["loc"] == ("cuts", 0)
    assert "Intervalo de corte inválido" in str(errors)


def test_edl_build_rejects_more_than_200_cuts() -> None:
    cuts = [{"in_sec": float(i), "out_sec": float(i) + 1.0} for i in range(201)]
    with pytest.raises(ValidationError) as excinfo:
        EdlBuildRequest(cuts=cuts)
    assert excinfo.value.errors()[0]["type"] == "too_long"


def test_edl_build_allows_empty_cuts_list_current_behavior() -> None:
    # Comportamiento ACTUAL anclado: a diferencia de EdlCreateRequest
    # (min_length=1), EdlBuildRequest no exige cortes (webutil.py:254).
    assert EdlBuildRequest(cuts=[]).cuts == []
    assert EdlBuildRequest().cuts is None


def test_edl_build_resolved_aspect_ratios_returns_independent_copy() -> None:
    assert EdlBuildRequest().resolved_aspect_ratios() == []
    req = EdlBuildRequest(aspect_ratios=["16:9"])
    resolved = req.resolved_aspect_ratios()
    resolved.append("9:16")
    assert req.aspect_ratios == ["16:9"]


# ---------------------------------------------------------------------------
# _max_upload_request_bytes / _is_media_upload_request
# ---------------------------------------------------------------------------


def test_max_upload_request_bytes_adds_multipart_envelope_overhead() -> None:
    # webutil.py:409-410: el techo de petición añade el sobre multipart de 64 KiB.
    assert _max_upload_request_bytes(0) == 64 * 1024
    assert _max_upload_request_bytes(1024) == 1024 + 64 * 1024
    assert _max_upload_request_bytes(1024) > 1024


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/studio/api/projects/p1/assets", True),
        ("POST", "/studio/api/projects/p1/media", True),
        ("POST", "/studio/api/projects/p1/uploads", True),
        ("POST", "/studio/api/projects/p1/assets/", True),  # rstrip de barra final
        ("post", "/studio/api/projects/p1/assets", True),  # método en minúsculas
        ("GET", "/studio/api/projects/p1/assets", False),
        ("PUT", "/studio/api/projects/p1/assets", False),
        ("POST", "/studio/api/projects/p1", False),  # sin segmento de sufijo
        ("POST", "/studio/api/projects/p1/thumbnail", False),  # sufijo no soportado
        ("POST", "/studio/api/projects/p1/assets/thumbs", False),  # 3 segmentos
        ("POST", "/other/api/projects/p1/assets", False),  # prefijo distinto
        ("POST", "/studio/api/projects/p1/assetsx", False),  # sufijo exacto exigido
    ],
)
def test_is_media_upload_request_matches_only_upload_routes(
    method: str, path: str, expected: bool
) -> None:
    request = _make_request(method, path)
    assert _is_media_upload_request(request) is expected


# ---------------------------------------------------------------------------
# _check_upload_content_length: rama con Content-Length declarado
# ---------------------------------------------------------------------------


def test_content_length_absent_skips_check() -> None:
    # Sin cabecera no hay nada que acotar aquí (el parser acotado cubre chunked).
    _check_upload_content_length(Headers({}), 1024)


@pytest.mark.parametrize("raw", ["abc", "12.5", ""])
def test_content_length_invalid_is_400(raw: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _check_upload_content_length(Headers({"content-length": raw}), 1024)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Cabecera Content-Length inválida"


@pytest.mark.parametrize("raw", ["-1", "-999999"])
def test_content_length_negative_is_400(raw: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _check_upload_content_length(Headers({"content-length": raw}), 1024)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Cabecera Content-Length inválida"


def test_content_length_within_limit_passes() -> None:
    _check_upload_content_length(Headers({"content-length": "1024"}), 1024)
    # espacios alrededor: se recorta antes de convertir
    _check_upload_content_length(Headers({"content-length": "  100  "}), 1024)


def test_content_length_exactly_at_ceiling_passes() -> None:
    ceiling = _max_upload_request_bytes(1024)
    _check_upload_content_length(Headers({"content-length": str(ceiling)}), 1024)


def test_content_length_one_byte_over_ceiling_is_413_per_file_detail() -> None:
    ceiling = _max_upload_request_bytes(1024)
    with pytest.raises(HTTPException) as excinfo:
        _check_upload_content_length(Headers({"content-length": str(ceiling + 1)}), 1024)
    assert excinfo.value.status_code == 413
    # sin `max_total_bytes` el límite cruzado es el por archivo
    assert excinfo.value.detail == "El archivo supera el límite permitido (1024 bytes)"


# ---------------------------------------------------------------------------
# _bounded_request_stream
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bounded_stream_yields_body_exactly_at_limit() -> None:
    request = _make_request("POST", "/studio/api/projects/p1/assets",
                            body_chunks=(b"01234", b"56789"))
    received: list[bytes] = []
    stream = _bounded_request_stream(request, max_stream_bytes=10, max_upload_bytes=5)
    async with aclosing(stream) as gen:
        async for chunk in gen:
            received.append(chunk)
    assert b"".join(received) == b"0123456789"


@pytest.mark.anyio
async def test_bounded_stream_413_cites_per_file_limit_and_stops_before_overflow() -> None:
    request = _make_request(
        "POST", "/studio/api/projects/p1/assets", body_chunks=(b"aaaaa", b"bbbbb", b"ccccc")
    )
    received: list[bytes] = []
    stream = _bounded_request_stream(
        request, max_stream_bytes=10, max_upload_bytes=5, total_bound=False
    )
    with pytest.raises(HTTPException) as excinfo:
        async with aclosing(stream) as gen:
            async for chunk in gen:
                received.append(chunk)
    # el chunk que cruza el límite no llega a entregarse
    assert received == [b"aaaaa", b"bbbbb"]
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail == "El archivo supera el límite permitido (5 bytes)"


@pytest.mark.anyio
async def test_bounded_stream_413_total_bound_detail() -> None:
    request = _make_request(
        "POST", "/studio/api/projects/p1/assets", body_chunks=(b"aaaaa", b"bbbbb", b"ccccc")
    )
    stream = _bounded_request_stream(
        request, max_stream_bytes=10, max_upload_bytes=5, total_bound=True
    )
    with pytest.raises(HTTPException) as excinfo:
        async with aclosing(stream) as gen:
            async for _chunk in gen:
                pass
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail == "La petición supera el límite TOTAL de subida (5 bytes)"


# ---------------------------------------------------------------------------
# _BoundedMultiPartParser
# ---------------------------------------------------------------------------

_BOUNDARY = "webutil-test-boundary"


def _multipart_headers() -> Headers:
    return Headers({"content-type": f"multipart/form-data; boundary={_BOUNDARY}"})


@pytest.mark.anyio
async def test_bounded_multipart_parses_two_files_each_under_limit() -> None:
    # Regresión del contador por parte (bug de reutilización de id()): dos partes
    # de 60 B con techo de 100 B deben parsear completo, no fallar en la segunda.
    body = _multipart_body(
        [
            ("files", "a.bin", b"a" * 60),
            ("files", "b.bin", b"b" * 60),
        ],
        _BOUNDARY,
    )
    parser = _BoundedMultiPartParser(
        _multipart_headers(), _chunked_stream(body), max_file_size=100
    )
    form = await parser.parse()
    files = form.getlist("files")
    assert [f.filename for f in files] == ["a.bin", "b.bin"]
    assert [len(f.file.read()) for f in files] == [60, 60]


@pytest.mark.anyio
async def test_bounded_multipart_per_file_413_cites_limit() -> None:
    body = _multipart_body([("files", "big.bin", b"x" * 80)], _BOUNDARY)
    parser = _BoundedMultiPartParser(
        _multipart_headers(), _chunked_stream(body), max_file_size=50
    )
    with pytest.raises(HTTPException) as excinfo:
        await parser.parse()
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail == "El archivo supera el límite permitido (50 bytes)"


@pytest.mark.anyio
async def test_bounded_multipart_parse_oserror_maps_to_413_storage_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN202
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(MultiPartParser, "parse", _boom)
    parser = _BoundedMultiPartParser(
        _multipart_headers(), _chunked_stream(b""), max_file_size=100
    )
    with pytest.raises(HTTPException) as excinfo:
        await parser.parse()
    assert excinfo.value.status_code == 413
    assert "agotó el almacenamiento temporal (100 bytes)" in excinfo.value.detail


def test_bounded_multipart_spool_size_caps_memory() -> None:
    # webutil.py:508-511: spool = min(max(max_file_size, 1 MiB), 8 MiB).
    small = _BoundedMultiPartParser(
        _multipart_headers(), _chunked_stream(b""), max_file_size=1024
    )
    assert small.spool_max_size == 1024 * 1024
    big = _BoundedMultiPartParser(
        _multipart_headers(), _chunked_stream(b""), max_file_size=32 * 1024 * 1024
    )
    assert big.spool_max_size == 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# _check_upload_free_disk: ramas de omisión y frontera
# ---------------------------------------------------------------------------


def _install_disk_probe(
    monkeypatch: pytest.MonkeyPatch, free: int | None, error: type[Exception] | None = None
) -> list[str]:
    calls: list[str] = []

    def _fake(path) -> object:  # noqa: ANN001
        calls.append(str(path))
        if error is not None:
            raise error(f"simulado para {path}")
        from types import SimpleNamespace

        return SimpleNamespace(free=free, total=free + 10**12, used=10**12 - free)

    monkeypatch.setattr("comfy_studio.webutil.shutil.disk_usage", _fake)
    return calls


def test_free_disk_absent_content_length_skips_disk_probe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_disk_probe(monkeypatch, free=0)
    _check_upload_free_disk(Headers({}), tmp_path)
    # sin cabecera no se ni consulta el disco
    assert calls == []


@pytest.mark.parametrize("raw", ["abc", ""])
def test_free_disk_invalid_content_length_skips_check(
    tmp_path, raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_disk_probe(monkeypatch, free=0)
    _check_upload_free_disk(Headers({"Content-Length": raw}), tmp_path)
    assert calls == []


@pytest.mark.parametrize("raw", ["0", "-4096"])
def test_free_disk_non_positive_length_skips_check(
    tmp_path, raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_disk_probe(monkeypatch, free=0)
    _check_upload_free_disk(Headers({"Content-Length": raw}), tmp_path)
    assert calls == []


def test_free_disk_disk_usage_oserror_is_swallowed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_disk_probe(monkeypatch, free=None, error=OSError)
    # fallo de filesystem no debe convertir la subida en error
    _check_upload_free_disk(Headers({"Content-Length": "1000"}), tmp_path)


def test_free_disk_boundary_free_equals_declaration_plus_margin_passes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_disk_probe(monkeypatch, free=100 + 1000)
    # frontera estricta: free < cl + margen lanza; la igualdad pasa
    _check_upload_free_disk(Headers({"Content-Length": "100"}), tmp_path, margin_bytes=1000)


def test_free_disk_one_byte_below_margin_is_507(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_disk_probe(monkeypatch, free=100 + 1000 - 1)
    with pytest.raises(HTTPException) as excinfo:
        _check_upload_free_disk(
            Headers({"Content-Length": "100"}), tmp_path, margin_bytes=1000
        )
    assert excinfo.value.status_code == 507
    detail = excinfo.value.detail
    assert "libres=1099 bytes" in detail
    assert "declara 100 bytes" in detail
    assert "margen 1000" in detail
