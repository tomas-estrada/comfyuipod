//! `collect::phases` — Parseo de `/var/log/download-models.log` para progreso de Fase 1 y Fase 2 (O-06)
//! y generación de `phase_banner`.

use crate::collect::models::phase2_expected_count;
use crate::collect::procfs::ComfyUiStatus;
use crate::strict::StrictReport;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Phase1Info {
    pub state: String,
    pub done: u32,
    pub total: u32,
    pub failed: u32,
    pub elapsed_s: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Phase2Info {
    pub state: String,
    pub done: u32,
    pub total: u32,
    pub failed: u32,
    pub current_file: Option<String>,
    pub hf_pid: Option<u32>,
    pub hf_rss_mb: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PhasesStatus {
    pub phase1: Phase1Info,
    pub phase2: Phase2Info,
}

pub fn collect_phases_status(
    download_log_path: &Path,
    skip_model_download: bool,
    model_profile: &str,
    hf_proc: Option<(u32, u64)>,
) -> PhasesStatus {
    let lines: Vec<String> = if download_log_path.is_file() {
        fs::read_to_string(download_log_path)
            .unwrap_or_default()
            .lines()
            .map(ToString::to_string)
            .collect()
    } else {
        Vec::new()
    };
    parse_phases_from_lines(&lines, skip_model_download, model_profile, hf_proc)
}

#[allow(clippy::too_many_lines)]
pub fn parse_phases_from_lines(
    lines: &[String],
    skip_model_download: bool,
    model_profile: &str,
    hf_proc: Option<(u32, u64)>,
) -> PhasesStatus {
    let expected_p2_total = phase2_expected_count(model_profile);
    if skip_model_download {
        return PhasesStatus {
            phase1: Phase1Info {
                state: "skipped".to_string(),
                done: 0,
                total: 12,
                failed: 0,
                elapsed_s: 0,
            },
            phase2: Phase2Info {
                state: "skipped".to_string(),
                done: 0,
                total: expected_p2_total,
                failed: 0,
                current_file: None,
                hf_pid: None,
                hf_rss_mb: 0,
            },
        };
    }

    let mut p1_state = "pending".to_string();
    let mut p1_done: u32 = 0;
    let mut p1_total: u32 = 12;
    let mut p1_failed: u32 = 0;

    let mut p2_state = if model_profile == "minimal" {
        "skipped".to_string()
    } else {
        "pending".to_string()
    };
    let mut p2_done: u32 = 0;
    let mut p2_total: u32 = expected_p2_total;
    let mut p2_failed: u32 = 0;
    let mut current_file: Option<String> = None;

    let rx_total = Regex::new(r"\((\d+)\s+(?:archivos|modelos)").ok();
    let rx_done =
        Regex::new(r"completada\s+en\s+\d+s:\s*(\d+)/(\d+)\s+exitosos,\s*(\d+)\s+fallidos").ok();
    let rx_inicio = Regex::new(r"\]\s*INICIO:\s*(\S+)").ok();
    let rx_fin = Regex::new(r"\]\s*(OK|SKIP|ERROR):\s*(\S+)").ok();

    let mut current_section = "";
    for line in lines {
        if line.contains("Iniciando FASE 1") {
            current_section = "phase1";
            p1_state = "running".to_string();
            if let Some(cap) = rx_total.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(v) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_total = v;
                }
            }
        } else if line.contains("Iniciando FASE 2") {
            current_section = "phase2";
            p2_state = "running".to_string();
            if let Some(cap) = rx_total.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(v) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_total = v;
                }
            }
        } else if line.contains("Omitiendo Fase 2") {
            p2_state = "skipped".to_string();
            p2_total = 0;
        } else if line.contains("FASE 1 completada") {
            p1_state = "done".to_string();
            if let Some(cap) = rx_done.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(d) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_done = d;
                }
                if let Some(t) = cap.get(2).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_total = t;
                }
                if let Some(f) = cap.get(3).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_failed = f;
                }
            }
        } else if line.contains("FASE 2 completada") {
            p2_state = "done".to_string();
            current_file = None;
            if let Some(cap) = rx_done.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(d) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_done = d;
                }
                if let Some(t) = cap.get(2).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_total = t;
                }
                if let Some(f) = cap.get(3).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_failed = f;
                }
            }
        } else {
            if let Some(cap) = rx_inicio.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(fname) = cap.get(1) {
                    current_file = Some(fname.as_str().to_string());
                }
            }
            if let Some(cap) = rx_fin.as_ref().and_then(|rx| rx.captures(line)) {
                let kind = cap.get(1).map_or("", |m| m.as_str());
                let fname = cap.get(2).map_or("", |m| m.as_str());
                if current_file.as_deref() == Some(fname) {
                    current_file = None;
                }
                if current_section == "phase1" && p1_state == "running" {
                    if kind == "OK" || kind == "SKIP" {
                        p1_done = p1_done.saturating_add(1);
                    } else {
                        p1_failed = p1_failed.saturating_add(1);
                    }
                } else if current_section == "phase2" && p2_state == "running" {
                    if kind == "OK" || kind == "SKIP" {
                        p2_done = p2_done.saturating_add(1);
                    } else {
                        p2_failed = p2_failed.saturating_add(1);
                    }
                }
            }
        }
    }

    let (hf_pid, hf_rss_mb) = match hf_proc {
        Some((pid, rss)) => (Some(pid), rss),
        None => (None, 0),
    };

    PhasesStatus {
        phase1: Phase1Info {
            state: p1_state,
            done: p1_done,
            total: p1_total,
            failed: p1_failed,
            elapsed_s: 0,
        },
        phase2: Phase2Info {
            state: p2_state,
            done: p2_done,
            total: p2_total,
            failed: p2_failed,
            current_file,
            hf_pid,
            hf_rss_mb,
        },
    }
}

pub fn format_phase_banner(
    comfy: &ComfyUiStatus,
    phases: &PhasesStatus,
    strict: Option<&StrictReport>,
) -> String {
    if let Some(st) = strict {
        if st.result == "fatal" {
            let failed: Vec<&str> = st
                .checks
                .iter()
                .filter(|c| c.status == "fatal")
                .map(|c| c.id.as_str())
                .collect();
            let joined = if failed.is_empty() {
                "strict-check".to_string()
            } else {
                failed.join(", ")
            };
            return format!("FATAL: Host incompatible ({joined}) — destruir instancia");
        }
    }

    let p1 = &phases.phase1;
    let p2 = &phases.phase2;

    if comfy.state == "down" {
        if comfy.supervisor_state == "exhausted" {
            return format!(
                "ComfyUI caído (límite de {} reinicios alcanzado)",
                comfy.max_restarts
            );
        }
        return format!(
            "ComfyUI caído (reinicio {}/{})",
            comfy.restarts, comfy.max_restarts
        );
    }

    if p1.state == "running" {
        return format!("Fase 1: descargando ({}/{})", p1.done, p1.total);
    }

    if comfy.state == "starting" {
        return "Arrancando ComfyUI...".to_string();
    }

    if p2.state == "skipped" {
        return "ComfyUI listo · Descargas omitidas (SKIP_MODEL_DOWNLOAD=1)".to_string();
    }
    if p2.state == "running" {
        let cur = p2
            .current_file
            .as_deref()
            .map_or_else(String::new, |f| format!(" ({f})"));
        return format!(
            "ComfyUI listo · Fase 2: {}/{} modelos{cur}",
            p2.done, p2.total
        );
    }
    if p2.state == "done" {
        return "ComfyUI listo · Fase 2 completada".to_string();
    }
    "ComfyUI listo".to_string()
}
