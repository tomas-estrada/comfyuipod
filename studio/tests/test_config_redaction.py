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
