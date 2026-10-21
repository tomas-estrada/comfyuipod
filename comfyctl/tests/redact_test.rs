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
