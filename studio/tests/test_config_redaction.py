"""Tests de configuración por variables de entorno y redacción de secretos."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.testclient import TestClient

from comfy_studio.config import (
    SECRET_PLACEHOLDER,
    RedactingLogFilter,
    SecretRedactor,
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
        "STUDIO_LLM_FALLBACK_BASE_URL": "https://api.deepseek.com",
        "STUDIO_LLM_FALLBACK_KEY": placeholder_fb_key,
        "STUDIO_LLM_FALLBACK_MODEL": "deepseek-chat",
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


