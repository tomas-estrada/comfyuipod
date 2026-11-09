"""Tests de configuración por variables de entorno y redacción de secretos."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from comfy_studio.config import (
    SECRET_PLACEHOLDER,
    RedactingLogFilter,
    SecretRedactor,
    SecretStreamRedactor,
    StudioConfig,
    install_log_redaction,
)


def test_config_from_env_parses_defaults_and_overrides(tmp_path: Path) -> None:
    placeholder_key = "YOUR_STUDIO_API_KEY_HERE"
    placeholder_fb_key = "YOUR_STUDIO_FALLBACK_KEY_HERE"
    env = {
        "MODEL_PROFILE": "studio",
        "STUDIO_LLM_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "STUDIO_LLM_KEY": placeholder_key,
        "STUDIO_LLM_MODEL": "qwen3-vl-plus",
        "STUDIO_LLM_SUPPORTS_TOOLS": "0",
        "STUDIO_LLM_SUPPORTS_STREAMING": "yes",
        "STUDIO_LLM_FALLBACK_BASE_URL": "https://api.deepseek.com",
        "STUDIO_LLM_FALLBACK_KEY": placeholder_fb_key,
        "STUDIO_LLM_FALLBACK_MODEL": "deepseek-chat",
        "STUDIO_LLM_FALLBACK_SUPPORTS_TOOLS": "true",
        "STUDIO_LLM_FALLBACK_SUPPORTS_STREAMING": "false",
        "STUDIO_COMFYUI_URL": "http://127.0.0.1:39172",
        "STUDIO_DATA_DIR": str(tmp_path / "data"),
        "STUDIO_SERVER_PORT": "18190",
        "STUDIO_STUB_LLM": "1",
        "COMFY_USER": "admin",
        "COMFY_PASS": "changeme-pod-pass",
    }
    cfg = StudioConfig.from_env(env)
    assert cfg.model_profile == "studio"
    assert cfg.studio_llm_model == "qwen3-vl-plus"
    assert cfg.studio_llm_fallback_model == "deepseek-chat"
    assert cfg.studio_llm_supports_tools is False
    assert cfg.studio_llm_supports_streaming is True
    assert cfg.studio_llm_fallback_supports_tools is True
    assert cfg.studio_llm_fallback_supports_streaming is False
    assert cfg.studio_comfyui_url == "http://127.0.0.1:39172"
    assert cfg.studio_data_dir == tmp_path / "data"
    assert cfg.studio_server_port == 18190
    assert cfg.studio_stub_llm is True
    assert cfg.require_auth is True
    assert cfg.get_llm_key() == placeholder_key
    assert cfg.get_llm_fallback_key() == placeholder_fb_key
    assert cfg.masked_llm_key_status() == "<definida>"
    assert cfg.masked_llm_fallback_key_status() == "<definida>"


def test_secret_never_leaks_in_repr_str_or_public_dict(tmp_path: Path) -> None:
    placeholder_key = "YOUR_STUDIO_API_KEY_HERE"
    placeholder_fb_key = "YOUR_STUDIO_FALLBACK_KEY_HERE"
    placeholder_pass = "changeme-pod-password"
    cfg = StudioConfig(
        studio_data_dir=tmp_path,
        studio_llm_key=placeholder_key,
        studio_llm_fallback_key=placeholder_fb_key,
        comfy_pass=placeholder_pass,
    )
    assert placeholder_key not in repr(cfg)
    assert placeholder_key not in str(cfg)
    assert placeholder_fb_key not in repr(cfg)
    assert placeholder_fb_key not in str(cfg)
    assert placeholder_pass not in repr(cfg)
    assert placeholder_pass not in str(cfg)

    public_dict = cfg.to_public_dict()
    serialized = str(public_dict)
    assert placeholder_key not in serialized
    assert placeholder_fb_key not in serialized
    assert placeholder_pass not in serialized
    assert public_dict["STUDIO_LLM_KEY"] == "<definida>"
    assert public_dict["STUDIO_LLM_FALLBACK_KEY"] == "<definida>"
    assert public_dict["STUDIO_LLM_SUPPORTS_TOOLS"] == "1"
    assert public_dict["STUDIO_LLM_FALLBACK_SUPPORTS_STREAMING"] == "1"


def test_secret_redactor_scrubs_literals_and_patterns() -> None:
    raw_key = "YOUR_STUDIO_API_KEY_HERE"
    raw_fb_key = "YOUR_STUDIO_FALLBACK_KEY_HERE"
    raw_pass = "changeme-pass-888"
    sk_pattern_token = "sk" + "-YOUR_API_KEY_HERE_12345"
    hf_pattern_token = "hf" + "_YOUR_HF_TOKEN_HERE_12345"
    redactor = SecretRedactor(
        studio_llm_key=raw_key,
        studio_llm_fallback_key=raw_fb_key,
        comfy_pass=raw_pass,
        hf_token=hf_pattern_token,
    )

    sample = (
        f"Usando key={raw_key}, fallback={raw_fb_key} y pass={raw_pass}; "
        f"Authorization: Bearer {sk_pattern_token} "
        "STUDIO_LLM_KEY=my_inline_placeholder "
        "STUDIO_LLM_FALLBACK_KEY=my_fallback_inline_placeholder "
        "https://admin:urlpassplaceholder@127.0.0.1:8188/studio"
    )
    redacted = redactor.redact(sample)
    assert raw_key not in redacted
    assert raw_fb_key not in redacted
    assert raw_pass not in redacted
    assert sk_pattern_token not in redacted
    assert hf_pattern_token not in redacted
    assert "my_inline_placeholder" not in redacted
    assert "my_fallback_inline_placeholder" not in redacted
    assert "urlpassplaceholder" not in redacted
    assert SECRET_PLACEHOLDER in redacted


def test_logging_filter_never_emits_studio_llm_key(caplog) -> None:
    placeholder_key = "YOUR_STUDIO_API_KEY_HERE"
    placeholder_fb_key = "YOUR_STUDIO_FALLBACK_KEY_HERE"
    redactor = SecretRedactor(
        studio_llm_key=placeholder_key,
        studio_llm_fallback_key=placeholder_fb_key,
    )
    logger = logging.getLogger("comfy_studio.test_redaction")
    logger.addFilter(RedactingLogFilter(redactor))
    install_log_redaction(redactor, logger=logger)

    with caplog.at_level(logging.INFO, logger="comfy_studio.test_redaction"):
        logger.info(
            "Fallo llamando a LLM con key %s, fallback %s y Bearer %s",
            placeholder_key,
            placeholder_fb_key,
            placeholder_key,
        )

    assert placeholder_key not in caplog.text
    assert placeholder_fb_key not in caplog.text
    assert SECRET_PLACEHOLDER in caplog.text


def test_status_and_health_endpoints_never_expose_studio_llm_key(
    client: TestClient,
    studio_config: StudioConfig,
) -> None:
    raw_key = studio_config.get_llm_key()
    raw_fb_key = studio_config.get_llm_fallback_key()
    raw_pass = studio_config.get_comfy_pass()
    assert raw_key is not None
    assert raw_fb_key is not None
    assert raw_pass is not None

    for endpoint in ("/studio/api/health", "/studio/api/status"):
        resp = client.get(endpoint)
        assert resp.status_code == 200
        text = resp.text
        assert raw_key not in text
        assert raw_fb_key not in text
        assert raw_pass not in text
    status_json = client.get("/studio/api/status").json()
    assert status_json["env_masked"]["STUDIO_LLM_KEY"] == "<definida>"
    assert status_json["env_masked"]["STUDIO_LLM_FALLBACK_KEY"] == "<definida>"
    assert status_json["llm"]["fallback_key_status"] == "<definida>"


def test_stream_redactor_scrubs_secrets_split_across_chunk_boundaries() -> None:
    """M2-SCR-017: El redactor de streaming redacta secretos partidos entre 2+ chunks."""
    primary_key = "PRIMARY_SECRET_KEY_ABCDEF123456"
    fallback_key = "FALLBACK_SECRET_KEY_UVWXYZ789012"
    redactor = SecretRedactor(
        studio_llm_key=primary_key,
        studio_llm_fallback_key=fallback_key,
    )

    stream = redactor.create_stream()

    # El primer chunk tiene texto seguro seguido de un prefijo parcial de la key primaria
    out1 = stream.push("Texto inicial en vivo: " + primary_key[:10])
    assert out1 == "Texto inicial en vivo: "
    assert primary_key[:10] not in out1

    # El segundo chunk sigue a mitad de la key primaria (aún incompleta)
    out2 = stream.push(primary_key[10:20])
    assert out2 == ""

    # El tercer chunk completa la key primaria y empieza un prefijo de la key de fallback
    out3 = stream.push(primary_key[20:] + " y luego " + fallback_key[:12])
    assert out3 == f"{SECRET_PLACEHOLDER} y luego "
    assert primary_key not in out3
    assert fallback_key[:12] not in out3

    # El cuarto chunk completa la key de fallback y añade texto final
    out4 = stream.push(fallback_key[12:] + " listo.")
    out5 = stream.flush()
    combined = out1 + out2 + out3 + out4 + out5

    assert combined == (
        f"Texto inicial en vivo: {SECRET_PLACEHOLDER} y luego {SECRET_PLACEHOLDER} listo."
    )
    assert primary_key not in combined
    assert fallback_key not in combined

    # Prueba adicional: key partida carácter por carácter y falsa alarma de prefijo
    char_stream = redactor.create_stream()
    char_outputs: list[str] = []
    full_input = f"Inicio PRIM_FALSA_ALARMA y {primary_key} y {fallback_key}"
    for ch in full_input:
        piece = char_stream.push(ch)
        if piece:
            char_outputs.append(piece)
    tail = char_stream.flush()
    if tail:
        char_outputs.append(tail)

    reconstructed = "".join(char_outputs)
    assert reconstructed == (
        f"Inicio PRIM_FALSA_ALARMA y {SECRET_PLACEHOLDER} y {SECRET_PLACEHOLDER}"
    )
    assert primary_key not in reconstructed
    assert fallback_key not in reconstructed

    # Patrones heurísticos (sk-, STUDIO_LLM_KEY=) partidos entre chunks también se redactan
    regex_stream = redactor.create_stream()
    r1 = regex_stream.push("Token s")
    r2 = regex_stream.push("k-abcdefgh123456 y STUDIO_LLM_")
    r3 = regex_stream.push("FALLBACK_KEY=valor_secreto_999 fin")
    r4 = regex_stream.flush()
    regex_combined = r1 + r2 + r3 + r4
    assert "sk-abcdefgh123456" not in regex_combined
    assert "valor_secreto_999" not in regex_combined
    assert regex_combined == (
        f"Token {SECRET_PLACEHOLDER} y STUDIO_LLM_FALLBACK_KEY={SECRET_PLACEHOLDER} fin"
    )


def test_redactor_covers_all_sensitive_tokens_and_assignments() -> None:
    api_token = "PLACEHOLDER_API_TOKEN_NO_REAL"
    webhook_secret = "PLACEHOLDER_WEBHOOK_SECRET_NO_REAL"
    vast_key = "PLACEHOLDER_VAST_KEY_NO_REAL"
    auth_pass = "auth_pass_complex_secret"
    redactor = SecretRedactor(
        extra_secrets=[api_token, webhook_secret, vast_key, auth_pass]
    )

    sample = (
        f"Tokens literales: {api_token}, {webhook_secret}, {vast_key}, {auth_pass}. "
        "Y asignaciones: STUDIO_API_TOKEN=tok123 STUDIO_WEBHOOK_SECRET=sec456 "
        "VAST_API_KEY=vast789 STUDIO_AUTH_PASS=pass000 COMFY_PASS=comfypass123"
    )
    redacted = redactor.redact(sample)
    assert api_token not in redacted
    assert webhook_secret not in redacted
    assert vast_key not in redacted
    assert auth_pass not in redacted
    assert "tok123" not in redacted
    assert "sec456" not in redacted
    assert "vast789" not in redacted
    assert "pass000" not in redacted
    assert "comfypass123" not in redacted


def test_loopback_host_enforcement(tmp_path: Path) -> None:
    from comfy_studio.config import sanitize_loopback_host

    assert sanitize_loopback_host("0.0.0.0") == "127.0.0.1"
    assert sanitize_loopback_host("192.168.1.100") == "127.0.0.1"
    assert sanitize_loopback_host("example.com") == "127.0.0.1"
    assert sanitize_loopback_host("127.0.0.1") == "127.0.0.1"
    assert sanitize_loopback_host("localhost") == "localhost"

    cfg = StudioConfig.from_env({
        "STUDIO_SERVER_HOST": "0.0.0.0",
        "STUDIO_DATA_DIR": str(tmp_path),
    })
    assert cfg.studio_server_host == "127.0.0.1"


def test_broker_redacts_published_events() -> None:
    from comfy_studio.progress import ProgressBroker

    secret = "SUPER_SECRET_BROKER_VALUE"
    redactor = SecretRedactor(extra_secrets=[secret])
    broker = ProgressBroker(redactor=redactor)
    event = broker.publish("job", {"job_id": "j1", "error": f"Failed with {secret}"})
    assert secret not in event["error"]
    assert SECRET_PLACEHOLDER in event["error"]


def test_hunyuan3d_template_params_validation() -> None:
    from comfy_studio.agent.tools import EnqueueRenderArgs, RenderTemplateParams

    # 3D template allows prompt to be None, but requires 3D params if passed
    p3d = RenderTemplateParams(
        image_asset_id="aid-123",
        latent_resolution=4096,
        octree_resolution=256,
    )
    args3d = EnqueueRenderArgs(template_id="hunyuan3d_image_to_3d", params=p3d)
    assert args3d.template_id == "hunyuan3d_image_to_3d"
    assert args3d.params.latent_resolution == 4096





def test_redactor_never_breaks_the_json_body_it_runs_on() -> None:
    """Invariante: el redactor corre sobre el cuerpo JSON serializado, que el
    cliente vuelve a parsear. Ningún patrón puede comerse delimitadores.

    Regresión: `_URL_AUTH_RE` con una clase de clave permisiva (`[^\\s]*`)
    abarcaba desde `http://host:` hasta la siguiente `@`, borrando la
    estructura JSON intermedia y devolviendo `Extra data`.
    """
    import json

    redactor = SecretRedactor.from_env({})
    body = json.dumps(
        {
            "comfyui_url": "http://stub-comfyui.local:39172",
            "nested": {"k": 1, "deep": {"a": [1, 2, 3]}},
            # Una `@` muy posterior (ruta del intérprete) es el señuelo clásico.
            "interpreter": "/opt/homebrew/Cellar/python@3.12/bin/python3.12",
        }
    )
    out = redactor.redact(body)
    parsed = json.loads(out)  # no debe lanzar
    assert parsed["nested"]["deep"]["a"] == [1, 2, 3]
    assert parsed["comfyui_url"] == "http://stub-comfyui.local:39172"


def test_redactor_covers_provider_key_shapes() -> None:
    """Paridad de cobertura con `comfyctl/src/redact.rs` para las formas de
    clave que no necesitan literal (el proceso que las emite puede no ser el
    que las configuró)."""
    redactor = SecretRedactor.from_env({})
    cases = {
        "k=AKIAIOSFODNN7EXAMPLE": "AKIAIOSFODNN7EXAMPLE",
        "g=AIzaSyA1234567890abcdefghijklmnopqrstuv": "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "t=ghp_" + "a" * 40: "ghp_" + "a" * 40,
        "s=xoxb-1234567890-abcdefghij": "xoxb-1234567890-abcdefghij",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==": "dXNlcjpwYXNzd29yZA==",
        "postgres://user:secretpw@db.host:5432/x": "secretpw",
        "proxy user:secretpw@host:3128": "secretpw",
    }
    for text, secret in cases.items():
        out = redactor.redact(text)
        assert secret not in out, f"no se redactó {secret!r} en {text!r} -> {out!r}"
        assert SECRET_PLACEHOLDER in out, f"falta el placeholder en {text!r} -> {out!r}"


def test_redactor_leaves_embedded_mailto_and_schemes_intact() -> None:
    """Regresión: la exención de esquemas miraba `group(0)`, que incluye el
    delimitador inicial obligatorio, así que solo funcionaba en la posición 0 y
    todo `mailto:` incrustado en un valor JSON perdía su parte local."""
    redactor = SecretRedactor.from_env({})
    for text in (
        '{"contact": "mailto:soporte@example.com", "ok": 1}',
        "Contact: mailto:user@example.com",
        "ver sip:a@b.com",
        "tel:+15551234@x",
        "xmpp:a@b.com",
        "urn:a@b.com",
        "news:a@b.com",
        "ldap:a@b.com",
        "callto:a@b.com",
    ):
        assert redactor.redact(text) == text, text
    # Una credencial real SÍ se redacta, esquema exento o no.
    assert SECRET_PLACEHOLDER in redactor.redact("proxy user:secretpw@host:3128")


def test_stream_redactor_holds_back_every_pattern_redact_covers() -> None:
    """Invariante: `redact()` y el stream no pueden divergir.

    Regresión: los 7 patrones añadidos para paridad con el redactor de Rust no
    tenían contrapartida en `_INFLIGHT_STREAM_REGEXES`, así que un credencial
    partida entre dos chunks de un stream SSE se emitía en claro.
    """
    redactor = SecretRedactor.from_env({})
    # (chunks, fragmento que NUNCA debe salir emitido)
    cases = [
        (["xoxb-1234", "567890-abcdefghij"], "567890-abcdefghij"),
        (["AKIAIOS", "FODNN7EXAMPLE"], "FODNN7EXAMPLE"),
        (["AIzaSyA1234567890abc", "defghijklmnopqrstuv"], "defghijklmnopqrstuv"),
        (["ghp_" + "a" * 20, "b" * 20], "b" * 20),
        (["postgres://u:sec", "retpw@db:5432/x"], "retpw"),
        (["Authorization: Basic dXNlcjpwYXNz", "d29yZA=="], "d29yZA=="),
        (["STUDIO_HEAVY_VIDEO_KEY=abc", "def123456"], "def123456"),
    ]
    for chunks, secret in cases:
        stream = SecretStreamRedactor(redactor)
        emitted = "".join(stream.push(c) for c in chunks) + stream.flush()
        assert secret not in emitted, f"fuga en stream: {chunks!r} -> {emitted!r}"
        assert SECRET_PLACEHOLDER in emitted, f"sin redactar: {chunks!r} -> {emitted!r}"


def test_redactor_leaves_benign_colons_and_mailto_intact() -> None:
    """Los falsos positivos destruyen el contenido: un puerto y un `mailto:`
    no son credenciales y deben sobrevivir."""
    redactor = SecretRedactor.from_env({})
    for benign in ("connect host:8443/path ok", "mailto:user@example.com", "ratio 16:9"):
        assert redactor.redact(benign) == benign, benign


def test_redaction_filter_covers_propagated_records_from_the_root_logger() -> None:
    """`Logger.callHandlers` ignora los filtros de un logger ancestro para los
    registros propagados: sin el filtro en el root, un
    `getLogger("comfy_studio.x").warning(...)` llegaba sin redactar."""
    import io

    secret = "SUPERSECRETPASS-1234"
    redactor = SecretRedactor.from_env({"COMFY_PASS": secret})
    stream = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    try:
        # Cableado real de create_app: root + comfy_studio.
        install_log_redaction(redactor, logger=logging.getLogger())
        install_log_redaction(redactor, logger=logging.getLogger("comfy_studio"))
        logging.getLogger("comfy_studio.comfyui_client").warning(f"pass={secret}")
    finally:
        root.removeHandler(handler)
    assert secret not in stream.getvalue(), stream.getvalue()
    assert SECRET_PLACEHOLDER in stream.getvalue()


def test_log_redaction_covers_lastresort_with_the_real_uvicorn_boot_order() -> None:
    """Regresión: el filtro en el logger raíz NO bastaba.

    uvicorn no configura handlers en el logger raíz, así que
    `Logger.callHandlers` no encuentra ninguno al subir por la jerarquía y el
    registro de `comfy_studio.*` cae en `logging.lastResort`. Los filtros de un
    logger *ancestro* nunca se consultan para un registro originado en un hijo,
    así que el secreto salía en claro a /var/log/studio-server.log.
    """
    import io
    from logging.config import dictConfig

    import uvicorn

    secret = "SUPERSECRETPASS-1234"
    redactor = SecretRedactor.from_env({"COMFY_PASS": secret})
    # Orden real de server.py: create_app (instala el filtro) y luego
    # uvicorn.run (aplica su LOGGING_CONFIG).
    install_log_redaction(redactor, logger=logging.getLogger("comfy_studio"))
    install_log_redaction(redactor, logger=logging.getLogger())
    dictConfig(uvicorn.config.LOGGING_CONFIG)

    # Dejar la raíz SIN handlers es exactamente la situación de producción
    # (uvicorn no configura ninguno): así `callHandlers` no encuentra nada y el
    # registro cae en `lastResort`. Sin vaciarlos, el plugin de logging de pytest
    # añadiría el suyo y el camino real no se ejercitaría.
    saved_root_handlers = logging.getLogger().handlers[:]
    for handler in saved_root_handlers:
        logging.getLogger().removeHandler(handler)
    captured = io.StringIO()
    original_emit = logging.lastResort.emit
    logging.lastResort.emit = lambda rec: captured.write(
        logging.lastResort.format(rec) + "\n"
    )
    try:
        logging.getLogger("comfy_studio.comfyui_client").warning(f"boot con pass={secret}")
    finally:
        logging.lastResort.emit = original_emit
        for handler in saved_root_handlers:
            logging.getLogger().addHandler(handler)

    emitted = captured.getvalue()
    assert emitted, "el registro no pasó por lastResort; el test no ejercita el camino real"
    assert secret not in emitted, emitted
    assert SECRET_PLACEHOLDER in emitted, emitted
