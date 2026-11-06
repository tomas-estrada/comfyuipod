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
    rx_authorization: Option<Regex>,
    rx_url_auth: Option<Regex>,
    rx_aws_key_id: Option<Regex>,
    rx_cloud_api_key: Option<Regex>,
    rx_vcs_token: Option<Regex>,
    rx_slack_token: Option<Regex>,
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
            // El separador admite la forma JSON con comillas
            // (`"STUDIO_LLM_KEY": "..."`), no solo el `KEY=value` de entorno.
            // Si el valor viene entre comillas se toma ENTERO hasta la de cierre
            // (admite espacios y escapes `\"`); ese caso se re-emite entre
            // comillas para no romper el JSON de /status/api. Sin comillas se
            // conserva la corrida sin espacios de siempre.
            rx_studio_env: Regex::new(
                r#"(?i)((?:STUDIO_LLM_(?:FALLBACK_)?KEY|STUDIO_API_TOKEN|STUDIO_WEBHOOK_SECRET|VAST_API_KEY|COMFY_PASS|STUDIO_AUTH_PASS|HF_TOKEN|AWS_SECRET_ACCESS_KEY|AWS_SECRET_KEY|AWS_ACCESS_KEY_ID|GITHUB_TOKEN|SLACK_TOKEN|OPENAI_API_KEY|GOOGLE_API_KEY|ANTHROPIC_API_KEY)\s*"?\s*[:=]\s*)("((?:[^"\\]|\\.)*)"|\S+)"#,
            )
            .ok(),
            rx_bearer: Regex::new(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{8,}").ok(),
            // `rx_bearer` solo cubría el esquema `Bearer`. Un log de petición
            // (ComfyUI, Studio, vast.ai, proxies) escribe la cabecera completa con
            // otros esquemas: `Basic <b64>`, `token <ghp_...>`, `ApiKey ...`,
            // `Digest ...`. Se conserva el esquema y se enmascara el valor; el
            // umbral de 8 caracteres es el mismo que usa `rx_bearer`, de modo que
            // una cabecera incompleta (`Authorization: Basic realm`) no se destruye.
            rx_authorization: Regex::new(
                r"(?i)(authorization\s*[:=]\s*(?:basic|bearer|token|apikey|key|jwt|pat|digest)\s+)[A-Za-z0-9._\-+/=]{8,}",
            )
            .ok(),
            // Voraz hasta el ÚLTIMO `@` SIN cruzar espacios: así una contraseña que
            // contenga `/` o `@` queda cubierta y el host posterior al último `@`
            // sobrevive. Excluir el espacio es lo que impide confundir los dos
            // puntos de un PUERTO (`host:8443/...`) con los de `user:pass`: si tras
            // el puerto hay un espacio antes del `@`, no hay credenciales que ocultar
            // y la línea benigna se deja intacta en lugar de destruirse.
            rx_url_auth: Regex::new(r"(https?://[^/\s:@]+:)([^\s]*)(@)").ok(),
            // Claves de nube y de proveedores por su FORMA: no hacen falta literales
            // (el proceso que las emite puede no ser el que las configuró) y los
            // prefijos son lo bastante específicos para no comer texto benigno.
            rx_aws_key_id: Regex::new(r"(?:AKIA|ASIA)[0-9A-Z]{16}").ok(),
            rx_cloud_api_key: Regex::new(r"AIza[0-9A-Za-z_\-]{35,}").ok(),
            rx_vcs_token: Regex::new(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,254}").ok(),
            rx_slack_token: Regex::new(r"xox[baprs]-[A-Za-z0-9\-]{10,248}").ok(),
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
            // Rama entre comillas (grupo 3 participó): se re-emite el par de
            // comillas para no romper el JSON de /status/api. Rama sin comillas:
            // se emite el valor desnudo, como siempre.
            out = rx
                .replace_all(&out, |caps: &regex::Captures<'_>| {
                    if caps.get(3).is_some() {
                        format!("{}\"[REDACTED]\"", &caps[1])
                    } else {
                        format!("{}[REDACTED]", &caps[1])
                    }
                })
                .into_owned();
        }
        if let Some(rx) = &self.rx_bearer {
            out = rx.replace_all(&out, "${1}[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_authorization {
            out = rx.replace_all(&out, "${1}[REDACTED]").into_owned();
        }
        if let Some(rx) = &self.rx_url_auth {
            out = rx.replace_all(&out, "${1}[REDACTED]${3}").into_owned();
        }
        // Formas de clave sin esquema: se sustituyen enteras (no hay un separador
        // nombre=valor que conservar).
        for rx in [
            &self.rx_aws_key_id,
            &self.rx_cloud_api_key,
            &self.rx_vcs_token,
            &self.rx_slack_token,
        ]
        .into_iter()
        .flatten()
        {
            out = rx.replace_all(&out, "[REDACTED]").into_owned();
        }
        out
    }
}
