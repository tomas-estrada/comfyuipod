//! Tests de redacción de secretos (`COMFY_PASS`, `HF_TOKEN`, `hf_*`, `Bearer *`, URLs con credenciales, §8.3 / §9).

use comfyctl::redact::SecretRedactor;

#[test]
fn redactor_removes_all_secret_patterns() {
    let redactor = SecretRedactor::new(Some("ci-secret-9f8e7d"), Some("hf_SuperSecretToken123456"));
    let sample = "Login con ci-secret-9f8e7d, token hf_SuperSecretToken123456, otro hf_AbCdEfGhIjKlMnOpQrStUvWxYz, \
                  header Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 y URL https://admin:myUrlPass123@example.com/api";

    let cleaned = redactor.redact(sample);
    assert!(!cleaned.contains("ci-secret-9f8e7d"));
    assert!(!cleaned.contains("hf_SuperSecretToken123456"));
    assert!(!cleaned.contains("hf_AbCdEfGhIjKlMnOpQrStUvWxYz"));
    assert!(!cleaned.contains("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"));
    assert!(!cleaned.contains("myUrlPass123"));
    assert!(cleaned.contains("[REDACTED]"));
}

#[test]
fn redactor_redacts_short_literals_and_url_passwords_with_special_chars() {
    // Un COMFY_PASS de 2-3 caracteres también debe redactarse (umbral >= 2).
    let redactor = SecretRedactor::new(Some("ab"), None);
    let cleaned = redactor.redact("login failed for pass=ab on host");
    assert!(!cleaned.contains("pass=ab"));
    assert!(cleaned.contains("[REDACTED]"));

    // Un secreto de 1 solo carácter NO se redacta (demasiados falsos positivos).
    let single = SecretRedactor::new(Some("a"), None);
    let out = single.redact("banana");
    assert_eq!(out, "banana");

    // URL con contraseña que contiene '/' y '@': se redacta hasta el ÚLTIMO '@' de la línea;
    // el host posterior al último '@' sobrevive.
    let url_redactor = SecretRedactor::new(None, None);
    let cleaned_url = url_redactor.redact("fetch https://user:p@ss/werd@example.com/repo.git ok");
    assert!(!cleaned_url.contains("p@ss/werd"), "{cleaned_url}");
    assert!(
        cleaned_url.contains("[REDACTED]@example.com/repo.git"),
        "{cleaned_url}"
    );
}

#[test]
fn redactor_and_status_env_never_leak_studio_llm_key() {
    use comfyctl::collect::procfs::build_masked_env;
    use comfyctl::config::RuntimeConfig;
    use std::collections::BTreeMap;

    let raw_key = format!("{}{}", "sk-", "YOUR_API_KEY_HERE_12345");
    let custom_key = "YOUR_STUDIO_API_KEY_HERE";
    let custom_fb_key = "YOUR_STUDIO_FALLBACK_KEY_HERE";

    let mut vars = BTreeMap::new();
    vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    vars.insert("STUDIO_LLM_KEY".to_string(), custom_key.to_string());
    vars.insert(
        "STUDIO_LLM_FALLBACK_KEY".to_string(),
        custom_fb_key.to_string(),
    );
    let cfg = RuntimeConfig::from_map(&vars, None);

    // 1. En build_masked_env (/status/api y /report.md), STUDIO_LLM_KEY y STUDIO_LLM_FALLBACK_KEY aparecen como "<definida>" y jamás en claro
    let masked = build_masked_env(&cfg);
    assert_eq!(
        masked.get("STUDIO_LLM_KEY").map(String::as_str),
        Some("<definida>")
    );
    assert_eq!(
        masked.get("STUDIO_LLM_FALLBACK_KEY").map(String::as_str),
        Some("<definida>")
    );
    assert_eq!(
        masked.get("STUDIO_LLM_BASE_URL").map(String::as_str),
        Some("https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    );
    assert_eq!(
        masked.get("STUDIO_LLM_MODEL").map(String::as_str),
        Some("qwen3-vl-plus")
    );
    assert_eq!(
        masked
            .get("STUDIO_LLM_FALLBACK_BASE_URL")
            .map(String::as_str),
        Some("https://api.deepseek.com")
    );
    assert_eq!(
        masked.get("STUDIO_LLM_FALLBACK_MODEL").map(String::as_str),
        Some("deepseek-chat")
    );
    for val in masked.values() {
        assert!(!val.contains(custom_key), "Leak detectado en env_masked");
        assert!(!val.contains(custom_fb_key), "Leak detectado en env_masked");
    }

    // 2. SecretRedactor::from_config redacta tanto el valor literal como patrones sk-* y STUDIO_LLM_(FALLBACK_)KEY=*
    let redactor = SecretRedactor::from_config(&cfg);
    let log_sample = format!(
        "Calling DashScope with key={custom_key}, fallback_lit={custom_fb_key}, fallback {raw_key} and env STUDIO_LLM_KEY=inline-secret-value-42 STUDIO_LLM_FALLBACK_KEY=inline-fb-secret-99"
    );
    let redacted = redactor.redact(&log_sample);
    assert!(!redacted.contains(custom_key), "{redacted}");
    assert!(!redacted.contains(custom_fb_key), "{redacted}");
    assert!(!redacted.contains(&raw_key), "{redacted}");
    assert!(!redacted.contains("inline-secret-value-42"), "{redacted}");
    assert!(!redacted.contains("inline-fb-secret-99"), "{redacted}");
    assert!(redacted.contains("STUDIO_LLM_KEY=[REDACTED]"), "{redacted}");
    assert!(
        redacted.contains("STUDIO_LLM_FALLBACK_KEY=[REDACTED]"),
        "{redacted}"
    );

    // 3. Sin STUDIO_LLM_KEY ni STUDIO_LLM_FALLBACK_KEY configuradas, el estado enmascarado es "<no definida>"
    let empty_cfg = RuntimeConfig::from_map(&BTreeMap::new(), None);
    let empty_masked = build_masked_env(&empty_cfg);
    assert_eq!(
        empty_masked.get("STUDIO_LLM_KEY").map(String::as_str),
        Some("<no definida>")
    );
    assert_eq!(
        empty_masked
            .get("STUDIO_LLM_FALLBACK_KEY")
            .map(String::as_str),
        Some("<no definida>")
    );
}
