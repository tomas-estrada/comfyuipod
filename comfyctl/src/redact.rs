//! `redact` — Redacción centralizada de secretos (`COMFY_PASS`, `HF_TOKEN`, `STUDIO_LLM_KEY`, `hf_*`, `sk-*`, `Bearer *`, URLs con usuario:clave, §9).

use crate::config::RuntimeConfig;
use regex::Regex;

#[derive(Debug, Clone)]
pub struct SecretRedactor {
    literals: Vec<String>,
    rx_hf: Option<Regex>,
    rx_sk: Option<Regex>,
    rx_studio_env: Option<Regex>,
    rx_bearer: Option<Regex>,
    rx_url_auth: Option<Regex>,
}

impl SecretRedactor {
    pub fn new(comfy_pass: Option<&str>, hf_token: Option<&str>) -> Self {
        Self::new_with_studio(comfy_pass, hf_token, None)
    }

    pub fn new_with_studio(
        comfy_pass: Option<&str>,
        hf_token: Option<&str>,
        studio_llm_key: Option<&str>,
    ) -> Self {
        Self::new_with_studio_keys(comfy_pass, hf_token, studio_llm_key, None)
    }

    pub fn new_with_studio_keys(
        comfy_pass: Option<&str>,
        hf_token: Option<&str>,
        studio_llm_key: Option<&str>,
        studio_llm_fallback_key: Option<&str>,
    ) -> Self {
        let mut literals = Vec::new();
        for candidate in [
            comfy_pass,
            hf_token,
            studio_llm_key,
            studio_llm_fallback_key,
        ]
        .into_iter()
        .flatten()
        {
            let trimmed = candidate.trim();
            if trimmed.len() >= 2 && !literals.iter().any(|s| s == trimmed) {
                literals.push(trimmed.to_string());
            }
        }
        Self {
            literals,
            rx_hf: Regex::new(r"hf_[A-Za-z0-9]{12,}").ok(),
            rx_sk: Regex::new(r"sk-[A-Za-z0-9._\-]{12,}").ok(),
            rx_studio_env: Regex::new(r"(?i)(STUDIO_LLM_(?:FALLBACK_)?KEY\s*[:=]\s*)(\S+)").ok(),
            rx_bearer: Regex::new(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{8,}").ok(),
            // `.*` voraz: redacta las credenciales hasta el ÚLTIMO `@` de la línea,
            // de modo que una contraseña que contenga `/` o `@` también queda cubierta
            // y la parte de host posterior al último `@` sobrevive.
            rx_url_auth: Regex::new(r"(https?://[^/\s:@]+:)(.*)(@)").ok(),
        }
    }

    /// Añade literales secretos adicionales (p. ej. `STUDIO_API_TOKEN`, `STUDIO_WEBHOOK_SECRET`).
    #[must_use]
    pub fn with_extra_literals(mut self, extra: impl IntoIterator<Item = Option<String>>) -> Self {
        for candidate in extra.into_iter().flatten() {
            let trimmed = candidate.trim();
            if trimmed.len() >= 2 && !self.literals.iter().any(|s| s == trimmed) {
                self.literals.push(trimmed.to_string());
            }
        }
        self
    }

    pub fn from_config(cfg: &RuntimeConfig) -> Self {
        Self::new_with_studio_keys(
            cfg.comfy_pass.as_deref(),
            cfg.hf_token.as_deref(),
            cfg.studio_llm_key.as_deref(),
            cfg.studio_llm_fallback_key.as_deref(),
        )
        // M3-SCR-005: el conjunto de literales también cubre VAST_API_KEY,
        // STUDIO_API_TOKEN y STUDIO_WEBHOOK_SECRET (valores nunca logueados).
        .with_extra_literals([
            cfg.vast_api_key.clone(),
            cfg.studio_api_token.clone(),
            cfg.studio_webhook_secret.clone(),
        ])
    }

    pub fn redact(&self, text: &str) -> String {
        if text.is_empty() {
            return String::new();
        }
        let mut out = text.to_string();
        for lit in &self.literals {
            if out.contains(lit) {
                out = out.replace(lit, "[REDACTED]");
            }
        }
        if let Some(rx) = &self.rx_hf {
            out = rx.replace_all(&out, "[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_sk {
            out = rx.replace_all(&out, "[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_studio_env {
            out = rx.replace_all(&out, "${1}[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_bearer {
            out = rx.replace_all(&out, "${1}[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_url_auth {
            out = rx.replace_all(&out, "${1}[REDACTED]${3}").into_owned();
        }
        out
    }
}
