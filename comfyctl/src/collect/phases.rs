//! `collect::phases` — Parseo de `/var/log/download-models.log` para progreso de Fase 1 y Fase 2 (O-06)
//! y generación de `phase_banner`.

use crate::collect::models::phase2_expected_count;
use crate::collect::procfs::ComfyUiStatus;
use crate::strict::StrictReport;
use chrono::NaiveDateTime;
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

fn parse_log_line_timestamp(line: &str) -> Option<i64> {
    let rest = line.strip_prefix('[')?;
    let (ts_str, _) = rest.split_once(']')?;
    NaiveDateTime::parse_from_str(ts_str.trim(), "%Y-%m-%d %H:%M:%S")
        .ok()
        .map(|dt| dt.and_utc().timestamp())
}

fn basename_of(raw: &str) -> &str {
    Path::new(raw)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(raw)
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
    let mut p1_start_ts: Option<i64> = None;
    let mut p1_elapsed_s: u64 = 0;

    let mut p2_state = if model_profile == "minimal" {
        "skipped".to_string()
    } else {
        "pending".to_string()
    };
    let mut p2_done: u32 = 0;
    let mut p2_total: u32 = expected_p2_total;
    let mut p2_failed: u32 = 0;
    let mut current_file: Option<String> = None;
    let mut last_ts: Option<i64> = None;

    let rx_plan = Regex::new(r"(?i)PLAN\s+FASE\s+([12]):\s*(\d+)\s+archivos").ok();
    let rx_total_inline = Regex::new(r"(?i)\((\d+)\s+(?:archivos|modelos)").ok();
    let rx_done_inline =
        Regex::new(r"(?i)completada\s+en\s+(\d+)s:\s*(\d+)/(\d+)\s+exitosos,\s*(\d+)\s+fallidos")
            .ok();
    let rx_inicio = Regex::new(r"\]\s*INICIO:\s*(\S+)").ok();
    let rx_ok = Regex::new(r"\]\s*OK:\s*(\S+)").ok();
    let rx_skip = Regex::new(r"\]\s*SKIP(?:\s*\(ya existe\))?:\s*(.+)$").ok();
    let rx_err = Regex::new(r"\]\s*ERROR:\s*(?:Falló la descarga de\s+(\S+)|(\S+))").ok();

    let mut current_section = "";
    for line in lines {
        if let Some(ts) = parse_log_line_timestamp(line) {
            last_ts = Some(ts);
        }
        let upper = line.to_uppercase();

        if let Some(cap) = rx_plan.as_ref().and_then(|rx| rx.captures(line)) {
            let phase_num = cap.get(1).map_or("", |m| m.as_str());
            if let Some(v) = cap.get(2).and_then(|m| m.as_str().parse::<u32>().ok()) {
                if phase_num == "1" {
                    p1_total = v;
                } else if phase_num == "2" {
                    p2_total = v;
                    if v == 0 {
                        p2_state = "skipped".to_string();
                        current_file = None;
                    }
                }
            }
            continue;
        }

        if upper.contains("INICIANDO FASE 1") {
            current_section = "phase1";
            p1_state = "running".to_string();
            p1_done = 0;
            p1_failed = 0;
            p1_start_ts = last_ts;
            if let Some(cap) = rx_total_inline.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(v) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_total = v;
                }
            }
        } else if upper.contains("INICIANDO FASE 2") {
            current_section = "phase2";
            p2_state = "running".to_string();
            p2_done = 0;
            p2_failed = 0;
            if let Some(cap) = rx_total_inline.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(v) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_total = v;
                }
            }
        } else if upper.contains("OMITIENDO FASE 2") {
            p2_state = "skipped".to_string();
            p2_total = 0;
            current_file = None;
        } else if upper.contains("FASE 1 COMPLETADA") {
            p1_state = "done".to_string();
            if current_section == "phase1" {
                current_file = None;
            }
            if let (Some(st), Some(et)) = (p1_start_ts, last_ts) {
                let dur = u64::try_from(et.saturating_sub(st)).unwrap_or(0);
                p1_elapsed_s = p1_elapsed_s.max(dur);
            }
            if let Some(cap) = rx_done_inline.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(el) = cap.get(1).and_then(|m| m.as_str().parse::<u64>().ok()) {
                    p1_elapsed_s = p1_elapsed_s.max(el);
                }
                if let Some(d) = cap.get(2).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_done = d;
                }
                if let Some(t) = cap.get(3).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_total = t;
                }
                if let Some(f) = cap.get(4).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p1_failed = f;
                }
            }
        } else if upper.contains("FASE 2 COMPLETADA") {
            p2_state = "done".to_string();
            current_file = None;
            if let Some(cap) = rx_done_inline.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(d) = cap.get(2).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_done = d;
                }
                if let Some(t) = cap.get(3).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_total = t;
                }
                if let Some(f) = cap.get(4).and_then(|m| m.as_str().parse::<u32>().ok()) {
                    p2_failed = f;
                }
            }
        } else {
            if let Some(cap) = rx_inicio.as_ref().and_then(|rx| rx.captures(line)) {
                if let Some(fname) = cap.get(1) {
                    current_file = Some(basename_of(fname.as_str()).to_string());
                }
                continue;
            }

            if let Some(cap) = rx_ok.as_ref().and_then(|rx| rx.captures(line)) {
                // Evitar doble conteo de buffalo_l: hf_fetch ya emite "OK: .../buffalo_l.zip"
                // antes de que ensure_insightface_buffalo_l emita "OK: InsightFace buffalo_l listo en ..."
                if line.contains("InsightFace buffalo_l listo en") {
                    continue;
                }
                let raw_target = cap.get(1).map_or("", |m| m.as_str());
                let base = basename_of(raw_target);
                if current_file.as_deref() == Some(base)
                    || current_file.as_deref() == Some(raw_target)
                {
                    current_file = None;
                }
                if current_section == "phase1" && p1_state == "running" {
                    p1_done = p1_done.saturating_add(1);
                } else if current_section == "phase2" && p2_state == "running" {
                    p2_done = p2_done.saturating_add(1);
                }
                continue;
            }

            if let Some(cap) = rx_skip.as_ref().and_then(|rx| rx.captures(line)) {
                let raw_target = cap.get(1).map_or("", |m| m.as_str().trim());
                let first_tok = raw_target.split_whitespace().next().unwrap_or("");
                let base = basename_of(first_tok);
                if current_file.as_deref() == Some(base)
                    || current_file.as_deref() == Some(raw_target)
                {
                    current_file = None;
                }
                if current_section == "phase1" && p1_state == "running" {
                    p1_done = p1_done.saturating_add(1);
                } else if current_section == "phase2" && p2_state == "running" {
                    p2_done = p2_done.saturating_add(1);
                }
                continue;
            }

            if let Some(cap) = rx_err.as_ref().and_then(|rx| rx.captures(line)) {
                let raw_target = cap.get(1).or_else(|| cap.get(2)).map_or("", |m| m.as_str());
                let base = basename_of(raw_target);
                if current_file.as_deref() == Some(base)
                    || current_file.as_deref() == Some(raw_target)
                {
                    current_file = None;
                }
                if current_section == "phase1" && p1_state == "running" {
                    p1_failed = p1_failed.saturating_add(1);
                } else if current_section == "phase2" && p2_state == "running" {
                    p2_failed = p2_failed.saturating_add(1);
                }
            }
        }
    }

    if p1_state == "running" {
        if let (Some(st), Some(et)) = (p1_start_ts, last_ts) {
            p1_elapsed_s = u64::try_from(et.saturating_sub(st)).unwrap_or(0);
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
            elapsed_s: p1_elapsed_s,
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
