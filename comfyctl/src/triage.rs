//! `triage` — Motor de triaje automático sobre `/etc/comfyui/triage_patterns.json` (17 patrones)
//! y extracción de errores recientes con sensibilidad a mayúsculas (O-08a, O-08b, O-08c).

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TriagePatternRaw {
    pub id: String,
    pub sources: Vec<String>,
    pub pattern: String,
    pub severity: String,
    pub cause: String,
    pub first_command: String,
}

#[derive(Debug, Clone)]
pub struct CompiledTriagePattern {
    pub raw: TriagePatternRaw,
    pub rx: Regex,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TriageHit {
    pub severity: String,
    pub pattern_id: String,
    pub source: String,
    pub evidence: String,
    pub summary: String,
    pub first_command: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecentError {
    pub ts: String,
    pub source: String,
    pub line: String,
}

#[derive(Debug, Clone)]
pub struct TriageEngine {
    pub patterns: Vec<CompiledTriagePattern>,
    err_rx: Option<Regex>,
    ts_rx: Option<Regex>,
}

impl TriageEngine {
    pub fn load(custom_path: Option<&Path>) -> Self {
        let mut raw_list: Vec<TriagePatternRaw> = Vec::new();
        let candidates = [
            custom_path.map(std::path::Path::to_path_buf),
            Some(Path::new("/etc/comfyui/triage_patterns.json").to_path_buf()),
            Some(Path::new("scripts/triage_patterns.json").to_path_buf()),
            Some(Path::new("../scripts/triage_patterns.json").to_path_buf()),
        ];

        // Degradación observable: si existe un fichero de patrones pero es ilegible o
        // no parsea, se emite un aviso claro por stderr en lugar de fallar en silencio.
        let mut saw_invalid_file = false;
        for cand in candidates.into_iter().flatten() {
            if cand.is_file() {
                match fs::read_to_string(&cand)
                    .map_err(|e| e.to_string())
                    .and_then(|txt| {
                        serde_json::from_str::<Vec<TriagePatternRaw>>(&txt)
                            .map_err(|e| e.to_string())
                    }) {
                    Ok(parsed) => {
                        raw_list = parsed;
                        break;
                    }
                    Err(_) => {
                        saw_invalid_file = true;
                    }
                }
            }
        }
        if raw_list.is_empty() && saw_invalid_file {
            eprintln!(
                "[comfyctl] WARN: triage_patterns.json inválido o patrón ilegible; triage degradado"
            );
        }

        Self::from_raw_patterns(raw_list)
    }

    pub fn from_raw_patterns(raw_list: Vec<TriagePatternRaw>) -> Self {
        let mut patterns = Vec::with_capacity(raw_list.len());
        for raw in raw_list {
            let ci_pat = format!("(?i){}", raw.pattern);
            match Regex::new(&ci_pat) {
                Ok(rx) => patterns.push(CompiledTriagePattern { raw, rx }),
                Err(_) => eprintln!(
                    "[comfyctl] WARN: triage_patterns.json inválido o patrón ilegible; triage degradado"
                ),
            }
        }
        let err_rx = Regex::new(
            r"(\bERROR\b|\bCRITICAL\b|\bFATAL\b|OutOfMemoryError|Traceback|IMPORT FAILED|Exception:|rc=13[479]|\[DIAG\]\[FAIL\])",
        )
        .ok();
        let ts_rx = Regex::new(r"^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})").ok();

        Self {
            patterns,
            err_rx,
            ts_rx,
        }
    }

    pub fn evaluate(
        &self,
        log_buffers: &BTreeMap<String, Vec<String>>,
        comfy_running: bool,
    ) -> (Vec<TriageHit>, Vec<RecentError>) {
        let mut triage_hits: Vec<TriageHit> = Vec::new();

        for item in &self.patterns {
            // O-08a: Suprimir nginx-502 cuando ComfyUI ya está running
            if item.raw.id == "nginx-502" && comfy_running {
                continue;
            }

            let mut matched_line: Option<String> = None;
            let mut matched_src: Option<String> = None;

            for src in &item.raw.sources {
                if let Some(lines) = log_buffers.get(src) {
                    for line in lines.iter().rev() {
                        if item.rx.is_match(line) {
                            matched_line = Some(line.trim().to_string());
                            matched_src = Some(src.clone());
                            break;
                        }
                    }
                }
                if matched_line.is_some() {
                    break;
                }
            }

            if let (Some(line), Some(src)) = (matched_line, matched_src) {
                triage_hits.push(TriageHit {
                    severity: item.raw.severity.clone(),
                    pattern_id: item.raw.id.clone(),
                    source: src,
                    evidence: truncate_chars(&line, 220),
                    summary: item.raw.cause.clone(),
                    first_command: item.raw.first_command.clone(),
                });
            }
        }

        triage_hits.sort_by_key(|h| match h.severity.as_str() {
            "alta" => 0_u8,
            "media" => 1_u8,
            "baja" => 2_u8,
            _ => 3_u8,
        });
        triage_hits.truncate(10);

        let mut errors_recent: Vec<RecentError> = Vec::new();
        let mut seen_errors: BTreeSet<String> = BTreeSet::new();

        if let Some(err_rx) = &self.err_rx {
            for src in ["comfyui", "download", "nginx-error", "supervisor"] {
                if let Some(lines) = log_buffers.get(src) {
                    for line in lines.iter().rev() {
                        if err_rx.is_match(line) {
                            let clean = line.trim();
                            let key = format!("{src}:{}", truncate_chars(clean, 120));
                            if seen_errors.insert(key) {
                                let ts = self
                                    .ts_rx
                                    .as_ref()
                                    .and_then(|rx| rx.captures(clean))
                                    .and_then(|c| c.get(1))
                                    .map_or_else(
                                        || "reciente".to_string(),
                                        |m| m.as_str().to_string(),
                                    );
                                errors_recent.push(RecentError {
                                    ts,
                                    source: src.to_string(),
                                    line: truncate_chars(clean, 300),
                                });
                            }
                            if errors_recent.len() >= 20 {
                                break;
                            }
                        }
                    }
                }
                if errors_recent.len() >= 20 {
                    break;
                }
            }
        }

        (triage_hits, errors_recent)
    }
}

pub fn truncate_chars(s: &str, max_chars: usize) -> String {
    s.chars().take(max_chars).collect()
}
