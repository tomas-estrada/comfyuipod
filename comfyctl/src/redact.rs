//! `redact` — Redacción centralizada de secretos (`COMFY_PASS`, `HF_TOKEN`, `hf_*`, `Bearer *`, URLs con usuario:clave, §9).

use crate::config::RuntimeConfig;
use regex::Regex;

#[derive(Debug, Clone)]
pub struct SecretRedactor {
    literals: Vec<String>,
    rx_hf: Option<Regex>,
    rx_bearer: Option<Regex>,
    rx_url_auth: Option<Regex>,
}

impl SecretRedactor {
    pub fn new(comfy_pass: Option<&str>, hf_token: Option<&str>) -> Self {
        let mut literals = Vec::new();
        for candidate in [comfy_pass, hf_token].into_iter().flatten() {
            let trimmed = candidate.trim();
            if trimmed.len() >= 2 && !literals.iter().any(|s| s == trimmed) {
                literals.push(trimmed.to_string());
            }
        }
        Self {
            literals,
            rx_hf: Regex::new(r"hf_[A-Za-z0-9]{12,}").ok(),
            rx_bearer: Regex::new(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{8,}").ok(),
            // `.*` voraz: redacta las credenciales hasta el ÚLTIMO `@` de la línea,
            // de modo que una contraseña que contenga `/` o `@` también queda cubierta
            // y la parte de host posterior al último `@` sobrevive.
            rx_url_auth: Regex::new(r"(https?://[^/\s:@]+:)(.*)(@)").ok(),
        }
    }

    pub fn from_config(cfg: &RuntimeConfig) -> Self {
        Self::new(cfg.comfy_pass.as_deref(), cfg.hf_token.as_deref())
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
        if let Some(rx) = &self.rx_bearer {
            out = rx.replace_all(&out, "${1}[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_url_auth {
            out = rx.replace_all(&out, "${1}[REDACTED]${3}").into_owned();
        }
        out
    }
}
