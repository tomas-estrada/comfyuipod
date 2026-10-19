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
