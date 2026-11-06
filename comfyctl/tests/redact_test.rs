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

#[test]
fn redactor_redacts_env_assignment_forms_without_configured_literals() {
    // M3-D: el patrón de asignaciones cubre STUDIO_API_TOKEN, STUDIO_WEBHOOK_SECRET,
    // VAST_API_KEY, COMFY_PASS, STUDIO_AUTH_PASS y HF_TOKEN aunque el redactor se
    // construya sin literales (p. ej. un dump de entorno o una línea de log suelta).
    let redactor = SecretRedactor::new(None, None);
    let sample = "env STUDIO_API_TOKEN=inline-token-1 STUDIO_WEBHOOK_SECRET=inline-secret-2 \
                  VAST_API_KEY=inline-vast-3 COMFY_PASS=inline-pass-4 \
                  STUDIO_AUTH_PASS=inline-auth-5 HF_TOKEN=inline-hf-6 PORT=8188";
    let cleaned = redactor.redact(sample);
    for leaked in [
        "inline-token-1",
        "inline-secret-2",
        "inline-vast-3",
        "inline-pass-4",
        "inline-auth-5",
        "inline-hf-6",
    ] {
        assert!(!cleaned.contains(leaked), "fuga de {leaked}: {cleaned}");
    }
    assert!(cleaned.contains("STUDIO_API_TOKEN=[REDACTED]"), "{cleaned}");
    assert!(
        cleaned.contains("STUDIO_WEBHOOK_SECRET=[REDACTED]"),
        "{cleaned}"
    );
    assert!(cleaned.contains("VAST_API_KEY=[REDACTED]"), "{cleaned}");
    assert!(cleaned.contains("COMFY_PASS=[REDACTED]"), "{cleaned}");
    assert!(cleaned.contains("STUDIO_AUTH_PASS=[REDACTED]"), "{cleaned}");
    assert!(cleaned.contains("HF_TOKEN=[REDACTED]"), "{cleaned}");
    // Variables no secretas sobreviven intactas.
    assert!(cleaned.contains("PORT=8188"), "{cleaned}");
}

#[test]
fn redactor_from_config_covers_vast_api_token_and_webhook_secret() {
    // M3-SCR-005: VAST_API_KEY, STUDIO_API_TOKEN y STUDIO_WEBHOOK_SECRET deben
    // formar parte del conjunto de literales del redactor construido desde config.
    use comfyctl::config::RuntimeConfig;
    use std::collections::BTreeMap;

    let mut vars = BTreeMap::new();
    vars.insert(
        "VAST_API_KEY".to_string(),
        "vast-secret-key-value-123".to_string(),
    );
    vars.insert(
        "STUDIO_API_TOKEN".to_string(),
        "studio-token-value-456".to_string(),
    );
    vars.insert(
        "STUDIO_WEBHOOK_SECRET".to_string(),
        "webhook-secret-value-789".to_string(),
    );
    let cfg = RuntimeConfig::from_map(&vars, None);
    let redactor = SecretRedactor::from_config(&cfg);

    let sample = "curl -H 'Authorization: vast-secret-key-value-123' token=studio-token-value-456 \
                  webhook studio-secret=webhook-secret-value-789";
    let cleaned = redactor.redact(sample);
    assert!(!cleaned.contains("vast-secret-key-value-123"), "{cleaned}");
    assert!(!cleaned.contains("studio-token-value-456"), "{cleaned}");
    assert!(!cleaned.contains("webhook-secret-value-789"), "{cleaned}");
    assert!(cleaned.contains("[REDACTED]"));
}

#[test]
fn redactor_does_not_mangle_port_urls_when_line_contains_later_email() {
    // rx_url_auth debe cubrir `user:pass@host`, pero NO debe confundir los dos
    // puntos de un PUERTO (`host:8443`) con los de unas credenciales: antes del
    // fix, el `.*` voraz tragaba hasta el último `@` de la línea y destruía el
    // puerto, el path y el email benignos (falsa redacción de texto no secreto).
    let redactor = SecretRedactor::new(None, None);
    let input = "GET https://api.example.com:8443/v1/models notify user@example.com ok";
    let out = redactor.redact(input);
    assert_eq!(
        out, input,
        "rx_url_auth mangled a benign URL+email line: {out}"
    );
}

#[test]
fn redactor_redacts_secrets_in_json_quoted_form() {
    // Un dump de config en JSON escribe comillas entre el nombre y los dos
    // puntos: `"STUDIO_LLM_KEY": "..."`. rx_studio_env exige `[:=]` justo tras
    // el nombre, asi que antes del fix el valor se filtraba entero.
    let redactor = SecretRedactor::new(None, None);
    // El valor con espacios es la forma real de env_masked
    // (`"<oculto: definido por el usuario>"`): tomarse solo hasta el primer
    // espacio dejaba texto suelto y rompia el JSON de /status/api. El valor
    // con escape `\"` cubre las contrasenas que contienen comilla.
    let input = r#"{"COMFY_PASS": "<oculto: definido por el usuario>", "STUDIO_LLM_KEY": "json-secret-value-1", "STUDIO_API_TOKEN": "pa\"ss"}"#;
    let out = redactor.redact(input);
    assert!(!out.contains("json-secret-value-1"), "fuga: {out}");
    assert!(!out.contains("definido por el usuario"), "fuga: {out}");
    assert!(!out.contains(r#"pa\"ss"#), "fuga: {out}");
    assert!(out.contains("[REDACTED]"), "{out}");
    // El JSON sigue siendo JSON: las comillas del valor se re-emiten. Sin
    // esto, /status/api deja de parsear y todas sus claves parecen faltar.
    let parsed: serde_json::Value = serde_json::from_str(&out).unwrap_or(serde_json::Value::Null);
    for key in ["COMFY_PASS", "STUDIO_LLM_KEY", "STUDIO_API_TOKEN"] {
        assert!(
            parsed.get(key).is_some_and(|v| v == "[REDACTED]"),
            "JSON roto o valor '{key}' sin redactar: {out}"
        );
    }
    // La forma de entorno sin comillas sigue comportandose como antes.
    let env = redactor.redact("env STUDIO_API_TOKEN=inline-token-1 PORT=8188");
    assert!(env.contains("STUDIO_API_TOKEN=[REDACTED]"), "{env}");
    assert!(env.contains("PORT=8188"), "{env}");
}

#[test]
fn redactor_redacts_bare_hf_tokens_without_eating_hf_prefixed_log_ids() {
    // rx_hf (`hf_[A-Za-z0-9]{12,}`) cubre el token de Hugging Face suelto en
    // cualquier linea de log. Ninguna prueba existente la ejercitaba: las demas
    // solo cubren la forma de asignacion `HF_TOKEN=...` (rx_studio_env), cuyo
    // valor de ejemplo ni siquiera cumple el umbral de esta expresion.
    let redactor = SecretRedactor::new(None, None);

    // Los tokens se arman con format! (mismo idiom que `raw_key` mas abajo) para
    // que el fuente no contenga cadenas con forma de secreto real; en tiempo de
    // ejecucion el texto es identico, asi que rx_hf se ejercita igual.
    let token = format!("{}{}", "hf_", "abcdefghijklmnopqrstuvwxyz1234");
    let out = redactor.redact(&format!(
        "401 Unauthorized: invalid token {token} for repo org/model"
    ));
    assert!(!out.contains(&token), "fuga de token hf: {out}");
    assert!(out.contains("[REDACTED]"), "{out}");

    // Umbral: 12 caracteres despues de `hf_` si se redactan...
    let min_token = format!("{}{}", "hf_", "123456789012");
    assert_eq!(redactor.redact(&format!("id {min_token}")), "id [REDACTED]");
    // ...11 no (guarda contra falsos positivos sobre identificadores cortos).
    let below_token = format!("{}{}", "hf_", "12345678901");
    assert_eq!(
        redactor.redact(&format!("id {below_token}")),
        format!("id {below_token}")
    );

    // Identificadores reales del proyecto con prefijo `hf_` NO son tokens y deben
    // sobrevivir intactos (linea que emite collect/mod.rs en una descarga estancada).
    let log_id = "hf_stuck_over_30m: pid=4321 sin avance hace 1900s";
    assert_eq!(redactor.redact(log_id), log_id);

    // rx_hf corre ANTES de rx_studio_env: la composicion de ambas reglas deja la
    // asignacion igual, sin duplicar ni romper el enmascarado.
    assert_eq!(
        redactor.redact(&format!("env HF_TOKEN={min_token} PORT=8188")),
        "env HF_TOKEN=[REDACTED] PORT=8188"
    );

    // Forma JSON: el valor se enmascara y el documento sigue parseando.
    let json = format!(r#"{{"note": "visto token {token}"}}"#);
    let red_json = redactor.redact(&json);
    assert!(!red_json.contains(&token), "fuga en JSON: {red_json}");
    let parsed: serde_json::Value =
        serde_json::from_str(&red_json).unwrap_or(serde_json::Value::Null);
    assert!(
        parsed
            .get("note")
            .and_then(|v| v.as_str())
            .is_some_and(|v| v.contains("[REDACTED]")),
        "JSON roto o valor sin redactar: {red_json}"
    );
}

#[test]
fn redactor_cubre_claves_de_nube_y_cabeceras_authorization() {
    // Las cadenas con forma de secreto se arman con format! para que el fuente no
    // las contenga literales (mismo idiom que el test de hf_*).
    let aws_key = format!("{}IOSFODNN7EXAMPLE", "AKIA");
    let gcp_key = format!("AIz{}", "aSyA1234567890abcdefghijklmnopqrstuvw");
    let gh_pat = format!("ghp_{}9012345678", "AbCdEfGhIjKlMnOpQrStUvWxYz0");
    // También montado con format!: un literal con forma de token de Slack hace que
    // el escaneo de secretos de GitHub rechace el push.
    let slack_tok = format!("xox{}-1234567890-abcdefghijklmnop", "b");
    let aws_secret = format!("wJalrXUtnFEMI/K7MDENG/bPxRfiCY{}", "EXAMPLEKEY");
    let redactor = SecretRedactor::new(None, None);

    // 1. Cabeceras Authorization: antes solo se cubria el esquema `Bearer`.
    for header in [
        format!("Authorization: Basic dXNlcjpwYXNz{aws_secret}"),
        "authorization=bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0".to_string(),
        format!("Authorization: token {gh_pat}"),
        format!("Authorization: ApiKey {gcp_key}"),
    ] {
        let cleaned = redactor.redact(&header);
        assert!(
            !cleaned.contains("dXNlcjpwYXNz")
                && !cleaned.contains("eyJhbGciOiJIUzI1NiJ9")
                && !cleaned.contains(&gh_pat)
                && !cleaned.contains(&gcp_key),
            "fuga en cabecera Authorization: {cleaned}"
        );
        assert!(cleaned.contains("[REDACTED]"), "{cleaned}");
    }

    // 2. Claves de nube y de proveedores por su forma, sin necesidad de literal.
    let cloud_line = format!("aws {aws_key} gcp {gcp_key} gh {gh_pat} slack {slack_tok}");
    let cleaned_cloud = redactor.redact(&cloud_line);
    for leaked in [
        aws_key.as_str(),
        gcp_key.as_str(),
        gh_pat.as_str(),
        slack_tok.as_str(),
    ] {
        assert!(
            !cleaned_cloud.contains(leaked),
            "fuga de clave de nube {leaked}: {cleaned_cloud}"
        );
    }

    // 3. Asignacion de secreto de nube por nombre de variable (AWS_SECRET_ACCESS_KEY
    //    no estaba en la lista de rx_studio_env).
    let env_line = format!("AWS_SECRET_ACCESS_KEY={aws_secret} AWS_REGION=us-east-1");
    let cleaned_env = redactor.redact(&env_line);
    assert!(!cleaned_env.contains(&aws_secret), "fuga: {cleaned_env}");
    assert!(
        cleaned_env.contains("AWS_REGION=us-east-1"),
        "la linea benigna debe sobrevivir: {cleaned_env}"
    );

    // 4. Sin falsos positivos: texto sin secretos queda intacto.
    let benign = "GET /studio/api/health HTTP/1.1 host=127.0.0.1:18190 port=8443 ok";
    assert_eq!(redactor.redact(benign), benign);
    // Un esquema sin valor (por debajo del umbral de 8) no se come el resto.
    let benign_auth = "Authorization: Basic realm";
    assert_eq!(redactor.redact(benign_auth), benign_auth);
}
