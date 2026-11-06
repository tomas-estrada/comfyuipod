//! `collect::phases` — Parseo de `/var/log/download-models.log` para progreso de Fase 1 y Fase 2 (O-06)
//! y generación de `phase_banner`.
//!
//! El arranque real del pod ejecuta `download-models.sh --bootstrap`, cuyo banner es
//! `ARRANQUE` y no `FASE 1`/`FASE 2`: sin reconocerlo, `/status/api` y `/report.md`
//! publicarían `pending` para siempre tras un arranque que no descarga nada (F20).
//!
//! Avance en bytes: sale del banner «INICIANDO FASE n (X GiB)» (plan medido por el
//! script) y de las líneas «OK: <ruta> (Y G)» (tamaño real del fichero bajado). Si
//! el log no declara tamaños, `bytes_total`/`bytes_done` quedan en «no disponible».
//! Lo que falta en disco se cruza con el catálogo del perfil (`add_disk_truth`).

use crate::collect::models::phase2_expected_count;
use crate::collect::models::WeightStatus;
use crate::collect::procfs::ComfyUiStatus;
use crate::strict::StrictReport;
use chrono::NaiveDateTime;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct Phase1Info {
    pub state: String,
    pub done: u32,
    pub total: u32,
    /// Nombres de fichero con `ERROR: Falló la descarga de <nombre>` (S-03: array, como en status-server.py).
    pub failed: Vec<String>,
    /// Ficheros del catálogo del perfil de esta fase que NO están en disco
    /// (verdad medida, no el log de descarga). Vacío = nada faltante o sin medir.
    pub missing: Vec<String>,
    /// Bytes ya completados, medidos en las líneas `OK: <ruta> (<tam>)` del log.
    pub bytes_done: u64,
    /// Bytes del plan que declara el banner `INICIANDO FASE n (X GiB)`; `None`
    /// cuando el log no lo declara: se publica `no disponible`, nunca un 0 fijo.
    pub bytes_total: Option<u64>,
    pub elapsed_s: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct Phase2Info {
    pub state: String,
    pub done: u32,
    pub total: u32,
    /// Nombres de fichero con `ERROR: Falló la descarga de <nombre>` (S-03: array, como en status-server.py).
    pub failed: Vec<String>,
    /// Ficheros del catálogo del perfil de esta fase que NO están en disco.
    pub missing: Vec<String>,
    /// Bytes ya completados según las líneas `OK: <ruta> (<tam>)` del log.
    pub bytes_done: u64,
    /// Bytes del plan del banner de Fase 2; `None` = `no disponible`.
    pub bytes_total: Option<u64>,
    pub current_file: Option<String>,
    pub hf_pid: Option<u32>,
    pub hf_rss_mb: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct PhasesStatus {
    pub phase1: Phase1Info,
    pub phase2: Phase2Info,
}

/// Estado honesto cuando el arranque (`download-models.sh --bootstrap`) no declara
/// trabajo de descarga: ni Fase 1 ni Fase 2 son pendientes, así que `pending` mentiría.
/// Se distingue de `skipped` (que reserva `SKIP_MODEL_DOWNLOAD=1` y el perfil `minimal`).
pub const STATE_NOT_REQUIRED: &str = "not_required";

/// Unidades de tamaño del log de `download-models.sh`: `du -h` (binarias) y el
/// banner con `KiB`/`MiB`/`GiB`. Sin marca, la letra suelta se toma binaria.
#[allow(clippy::cast_sign_loss, clippy::cast_possible_truncation)]
fn unit_to_bytes(unit: &str, value: f64) -> u64 {
    let mult: f64 = match unit {
        "B" => 1.0,
        "K" | "KiB" | "kB" => 1024.0,
        "M" | "MiB" | "MB" => 1024.0 * 1024.0,
        "G" | "GiB" | "GB" => 1024.0 * 1024.0 * 1024.0,
        _ => return 0,
    };
    (value * mult) as u64
}

/// `OK: <ruta> (4.0K)` -> bytes; `Some(bytes)` solo si la línea declara tamaño.
fn parse_ok_line_bytes(line: &str) -> Option<u64> {
    let paren = line.rfind('(')?;
    let inner = line.get(paren + 1..)?.trim_end_matches(')').trim();
    let num_end = inner
        .find(|c: char| !(c.is_ascii_digit() || c == '.'))
        .unwrap_or(inner.len());
    let value: f64 = inner.get(..num_end)?.parse().ok()?;
    let unit = inner.get(num_end..)?.trim();
    Some(unit_to_bytes(unit, value))
}

/// `INICIANDO FASE 1 (2.79 GiB): ...` -> bytes del plan; `None` sin declaración.
fn parse_phase_plan_bytes(line: &str, phase: u8) -> Option<u64> {
    let needle = format!("INICIANDO FASE {phase} (");
    let at = line.find(&needle)?;
    let rest = line.get(at + needle.len()..)?;
    let paren = rest.find(')')?;
    let inner = rest.get(..paren)?.trim();
    let num_end = inner
        .find(|c: char| !(c.is_ascii_digit() || c == '.'))
        .unwrap_or(inner.len());
    let value: f64 = inner.get(..num_end)?.parse().ok()?;
    let unit = inner.get(num_end..)?.trim();
    Some(unit_to_bytes(unit, value))
}

/// Cruza el progreso del log con la verdad del disco: qué pesos de cada fase
/// faltan, según el catálogo del perfil, se publican en la fase correspondiente.
/// Un log que dice `done` con pesos ausentes se contradice con la realidad, y el
/// informe muestra la contradicción en vez de repetir el estado del log.
pub fn add_disk_truth(phases: &mut PhasesStatus, weights: &WeightStatus) {
    phases.phase1.missing = weights
        .missing
        .iter()
        .filter(|m| m.contains("(phase1)"))
        .cloned()
        .collect();
    phases.phase2.missing = weights
        .missing
        .iter()
        .filter(|m| m.contains("(phase2)"))
        .cloned()
        .collect();
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
                total: 12,
                ..Phase1Info::default()
            },
            phase2: Phase2Info {
                state: "skipped".to_string(),
                total: expected_p2_total,
                ..Phase2Info::default()
            },
        };
    }

    let mut p1_state = "pending".to_string();
    let mut p1_done: u32 = 0;
    let mut p1_total: u32 = 12;
    let mut p1_failed: Vec<String> = Vec::new();
    let mut p1_start_ts: Option<i64> = None;
    let mut p1_elapsed_s: u64 = 0;
    let mut p1_bytes_done: u64 = 0;
    let mut p1_bytes_total: Option<u64> = None;

    let mut p2_state = if model_profile == "minimal" {
        "skipped".to_string()
    } else {
        "pending".to_string()
    };
    let mut p2_done: u32 = 0;
    let mut p2_total: u32 = expected_p2_total;
    let mut p2_failed: Vec<String> = Vec::new();
    let mut p2_bytes_done: u64 = 0;
    let mut p2_bytes_total: Option<u64> = None;
    let mut current_file: Option<String> = None;
    let mut last_ts: Option<i64> = None;

    // Formato real de scripts/download-models.sh (verificado con dry-run en CI, S-01/S-02):
    //   PLAN FASE 1: 12 archivos / PLAN FASE 2: N archivos (<perfil>)
    //   INICIANDO FASE n (...) · FASE n COMPLETADA. · omitiendo Fase 2
    //   INICIO: <nombre> (<repo>/<path>) · OK: <ruta_abs> (<tam>) · SKIP (ya existe): <nombre>
    //   ERROR: Falló la descarga de <nombre> (<repo>/<path>) tras 3 intentos.
    // Banner de `run_bootstrap` (--bootstrap, el camino que usa el arranque del pod):
    //   INICIANDO ARRANQUE (solo bootstrap imprescindible)
    //   PLAN ARRANQUE: N archivos bootstrap.
    //   Nada que descargar para abrir la API: el arranque no arrastra modelos.
    //   FASE ARRANQUE COMPLETADA. / FASE ARRANQUE COMPLETADA (N archivos bootstrap).
    let rx_plan = Regex::new(r"(?i)PLAN\s+FASE\s+([12]):\s*(\d+)\s+archivos").ok();
    let rx_plan_arranque = Regex::new(r"(?i)PLAN\s+ARRANQUE:\s*(\d+)\s+archivos").ok();
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

        if let Some(cap) = rx_plan_arranque.as_ref().and_then(|rx| rx.captures(line)) {
            // El plan de bootstrap manda sobre cualquier FASE anterior del mismo log:
            // la última ejecución es el estado actual.
            if let Some(v) = cap.get(1).and_then(|m| m.as_str().parse::<u32>().ok()) {
                p1_total = v;
                p1_done = 0;
                p1_failed = Vec::new();
                p2_total = 0;
                p2_failed = Vec::new();
                current_file = None;
                // --bootstrap nunca baja la Fase 2 del perfil.
                p2_state = STATE_NOT_REQUIRED.to_string();
                if v == 0 {
                    p1_state = STATE_NOT_REQUIRED.to_string();
                    current_section = "";
                } else {
                    p1_state = "running".to_string();
                    current_section = "phase1";
                    p1_start_ts = last_ts;
                }
            }
            continue;
        }

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
            p1_failed = Vec::new();
            p1_start_ts = last_ts;
            // El tamaño del plan que el banner declara (2.79 GiB en el formato real).
            p1_bytes_total = parse_phase_plan_bytes(line, 1);
        } else if upper.contains("INICIANDO FASE 2") {
            current_section = "phase2";
            p2_state = "running".to_string();
            p2_done = 0;
            p2_failed = Vec::new();
            p2_bytes_total = parse_phase_plan_bytes(line, 2);
        } else if upper.contains("OMITIENDO FASE 2") {
            p2_state = "skipped".to_string();
            p2_total = 0;
            current_file = None;
        } else if upper.contains("INICIANDO ARRANQUE") {
            // Arranque real (--bootstrap): solo bootstrap del manifiesto. Ni Fase 1
            // (paquete histórico de 12 archivos) ni Fase 2 del perfil se ejecutan.
            current_section = "phase1";
            p1_state = "running".to_string();
            p1_done = 0;
            p1_failed = Vec::new();
            p1_total = 0;
            p1_start_ts = last_ts;
            p2_state = STATE_NOT_REQUIRED.to_string();
            p2_total = 0;
            p2_failed = Vec::new();
            current_file = None;
        } else if upper.contains("NADA QUE DESCARGAR") {
            // El arranque lo dice sin ambigüedad: no arrastra modelos. Cubre el log
            // truncado o rotado donde el `PLAN ARRANQUE: 0` ya no está, que si no
            // quedaría en `running` con 0/0 (o en `pending` si falta el INICIANDO).
            p1_state = STATE_NOT_REQUIRED.to_string();
            p1_total = 0;
            p1_done = 0;
            p1_failed = Vec::new();
            p2_state = STATE_NOT_REQUIRED.to_string();
            p2_total = 0;
            p2_failed = Vec::new();
            current_file = None;
            current_section = "";
        } else if upper.contains("FASE ARRANQUE COMPLETADA") {
            // "FASE ARRANQUE COMPLETADA." (plan vacío) o "… (N archivos bootstrap)."
            if p1_total == 0 {
                p1_state = STATE_NOT_REQUIRED.to_string();
            } else {
                p1_state = "done".to_string();
            }
            if current_section == "phase1" {
                current_file = None;
            }
            if let (Some(st), Some(et)) = (p1_start_ts, last_ts) {
                let dur = u64::try_from(et.saturating_sub(st)).unwrap_or(0);
                p1_elapsed_s = p1_elapsed_s.max(dur);
            }
        } else if upper.contains("FASE 1 COMPLETADA") {
            p1_state = "done".to_string();
            if current_section == "phase1" {
                current_file = None;
            }
            if let (Some(st), Some(et)) = (p1_start_ts, last_ts) {
                let dur = u64::try_from(et.saturating_sub(st)).unwrap_or(0);
                p1_elapsed_s = p1_elapsed_s.max(dur);
            }
        } else if upper.contains("FASE 2 COMPLETADA") {
            p2_state = "done".to_string();
            current_file = None;
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
                    p1_bytes_done =
                        p1_bytes_done.saturating_add(parse_ok_line_bytes(line).unwrap_or(0));
                } else if current_section == "phase2" && p2_state == "running" {
                    p2_done = p2_done.saturating_add(1);
                    p2_bytes_done =
                        p2_bytes_done.saturating_add(parse_ok_line_bytes(line).unwrap_or(0));
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
                let base = basename_of(raw_target).to_string();
                if current_file.as_deref() == Some(base.as_str())
                    || current_file.as_deref() == Some(raw_target)
                {
                    current_file = None;
                }
                if current_section == "phase1" && p1_state == "running" {
                    if !p1_failed.contains(&base) {
                        p1_failed.push(base);
                    }
                } else if current_section == "phase2"
                    && p2_state == "running"
                    && !p2_failed.contains(&base)
                {
                    p2_failed.push(base);
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
            missing: Vec::new(),
            bytes_done: p1_bytes_done,
            bytes_total: p1_bytes_total,
            elapsed_s: p1_elapsed_s,
        },
        phase2: Phase2Info {
            state: p2_state,
            done: p2_done,
            total: p2_total,
            failed: p2_failed,
            missing: Vec::new(),
            bytes_done: p2_bytes_done,
            bytes_total: p2_bytes_total,
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

    if p1.state == STATE_NOT_REQUIRED && p2.state == STATE_NOT_REQUIRED {
        return "ComfyUI listo · Descargas no requeridas (el arranque no arrastra modelos)"
            .to_string();
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

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(raw: &[&str]) -> Vec<String> {
        raw.iter().map(|s| (*s).to_string()).collect()
    }

    /// Salida literal de `download-models.sh --bootstrap` con el manifiesto real
    /// (`config/models-core.json`, cuyo conjunto bootstrap está vacío a propósito).
    fn bootstrap_log_nothing_to_download() -> Vec<String> {
        lines(&[
            "[2026-11-05 09:00:00] [MODELS] ======================================================================",
            "[2026-11-05 09:00:00] [MODELS] INICIANDO ARRANQUE (solo bootstrap imprescindible)",
            "[2026-11-05 09:00:00] [MODELS] Manifiesto: /etc/comfyui/models-core.json",
            "[2026-11-05 09:00:00] [MODELS] ======================================================================",
            "[2026-11-05 09:00:00] [MODELS] PLAN ARRANQUE: 0 archivos bootstrap.",
            "[2026-11-05 09:00:00] [MODELS] Nada que descargar para abrir la API: el arranque no arrastra modelos.",
            "[2026-11-05 09:00:00] [MODELS] Las capacidades se precargan a petición (dependencias y bytes visibles).",
            "[2026-11-05 09:00:00] [MODELS] ======================================================================",
            "[2026-11-05 09:00:00] [MODELS] FASE ARRANQUE COMPLETADA.",
            "[2026-11-05 09:00:00] [MODELS] ======================================================================",
            "[2026-11-05 09:00:00] [MODELS] RESUMEN ARRANQUE: Todos los modelos se descargaron correctamente (0 fallos).",
        ])
    }

    fn comfy_running() -> ComfyUiStatus {
        ComfyUiStatus {
            state: "running".to_string(),
            supervisor_state: "running".to_string(),
            pid: Some(4242),
            rss_mb: 2048,
            uptime_s: 60,
            port_open: true,
            restarts: 0,
            healthy_resets: 0,
            max_restarts: 5,
            last_exit: None,
        }
    }

    #[test]
    fn arranque_banner_marks_both_phases_not_required() {
        let parsed = parse_phases_from_lines(
            &bootstrap_log_nothing_to_download(),
            false,
            "all",
            Some((999, 64)),
        );
        assert_eq!(parsed.phase1.state, STATE_NOT_REQUIRED);
        assert_eq!(parsed.phase1.done, 0);
        assert_eq!(parsed.phase1.total, 0);
        assert!(parsed.phase1.failed.is_empty());
        assert_eq!(parsed.phase2.state, STATE_NOT_REQUIRED);
        assert_eq!(parsed.phase2.done, 0);
        assert_eq!(parsed.phase2.total, 0);
        assert!(parsed.phase2.failed.is_empty());
        assert_eq!(parsed.phase2.current_file, None);
        // El proceso hf vivo sigue reportándose aunque no haya fases pendientes.
        assert_eq!(parsed.phase2.hf_pid, Some(999));
        assert_eq!(parsed.phase2.hf_rss_mb, 64);
    }

    #[test]
    fn arranque_banner_is_not_mistaken_for_a_pending_phase1() {
        // Regresión F20: el banner ARRANQUE no debe dejar `pending`/12 ficheros.
        let parsed =
            parse_phases_from_lines(&bootstrap_log_nothing_to_download(), false, "all", None);
        assert_ne!(parsed.phase1.state, "pending");
        assert_ne!(parsed.phase1.total, 12);
        assert_ne!(parsed.phase2.state, "pending");
        assert_ne!(parsed.phase2.total, 14);
    }

    #[test]
    fn arranque_banner_with_planned_bootstrap_files_counts_them() {
        let log = lines(&[
            "[2026-11-05 09:00:00] [MODELS] INICIANDO ARRANQUE (solo bootstrap imprescindible)",
            "[2026-11-05 09:00:00] [MODELS] PLAN ARRANQUE: 2 archivos bootstrap.",
            "[2026-11-05 09:00:01] [MODELS] INICIO: buffalo_l.zip (Gourieff/ReActor/models/buffalo_l.zip)",
            "[2026-11-05 09:00:02] [MODELS] OK: /workspace/comfyui-data/models/insightface/buffalo_l.zip (1.0K)",
            "[2026-11-05 09:00:03] [MODELS] INICIO: inswapper_128.onnx (Gourieff/ReActor/models/inswapper_128.onnx)",
            "[2026-11-05 09:00:04] [MODELS] ERROR: Falló la descarga de inswapper_128.onnx (Gourieff/ReActor/models/inswapper_128.onnx) tras 3 intentos.",
            "[2026-11-05 09:00:05] [MODELS] FASE ARRANQUE COMPLETADA (2 archivos bootstrap).",
        ]);
        let parsed = parse_phases_from_lines(&log, false, "all", None);
        assert_eq!(parsed.phase1.state, "done");
        assert_eq!(parsed.phase1.done, 1);
        assert_eq!(parsed.phase1.total, 2);
        assert_eq!(parsed.phase1.failed, vec!["inswapper_128.onnx".to_string()]);
        assert_eq!(parsed.phase1.elapsed_s, 5);
        // El bootstrap nunca baja la Fase 2 del perfil.
        assert_eq!(parsed.phase2.state, STATE_NOT_REQUIRED);
        assert_eq!(parsed.phase2.total, 0);
        assert_eq!(parsed.phase2.current_file, None);
    }

    #[test]
    fn arranque_nothing_to_download_survives_a_truncated_plan_line() {
        // Log truncado o rotado: sigue el "Nada que descargar" del arranque pero se
        // perdió el `PLAN ARRANQUE: 0`. Sin reconocer esa línea el estado se quedaría
        // en `running` con 0/0 (o en `pending` si también falta el INICIANDO).
        let log = lines(&[
            "[2026-11-05 09:00:00] [MODELS] INICIANDO ARRANQUE (solo bootstrap imprescindible)",
            "[2026-11-05 09:00:00] [MODELS] Nada que descargar para abrir la API: el arranque no arrastra modelos.",
            "[2026-11-05 09:00:00] [MODELS] Las capacidades se precargan a petición (dependencias y bytes visibles).",
        ]);
        let parsed = parse_phases_from_lines(&log, false, "all", None);
        assert_eq!(parsed.phase1.state, STATE_NOT_REQUIRED);
        assert_eq!(parsed.phase1.total, 0);
        assert_eq!(parsed.phase1.done, 0);
        assert_eq!(parsed.phase2.state, STATE_NOT_REQUIRED);
        assert_eq!(parsed.phase2.total, 0);
        assert_eq!(parsed.phase2.current_file, None);
        let banner = format_phase_banner(&comfy_running(), &parsed, None);
        assert!(!banner.contains("pending"), "banner: {banner}");
        assert!(!banner.contains("descargando (0/0)"), "banner: {banner}");
    }

    #[test]
    fn last_execution_wins_between_arranque_and_legacy_phases() {
        // Un --phase2 manual posterior al arranque debe reflejarse, no el bootstrap.
        let mut log = bootstrap_log_nothing_to_download();
        log.extend(lines(&[
            "[2026-11-05 10:00:00] [MODELS] INICIANDO FASE 1 (2.79 GiB): ReActor, InsightFace, CodeFormer, VAE, Upscalers",
            "[2026-11-05 10:00:00] [MODELS] PLAN FASE 1: 12 archivos",
            "[2026-11-05 10:00:01] [MODELS] FASE 1 COMPLETADA.",
            "[2026-11-05 10:00:02] [MODELS] INICIANDO FASE 2 (Segundo Plano — Perfil: all)",
            "[2026-11-05 10:00:02] [MODELS] PLAN FASE 2: 14 archivos (all)",
            "[2026-11-05 10:00:03] [MODELS] INICIO: flux1-dev-fp8.safetensors (Comfy-Org/flux1-dev/flux1-dev-fp8.safetensors)",
            "[2026-11-05 10:00:04] [MODELS] OK: /workspace/comfyui-data/models/checkpoints/flux1-dev-fp8.safetensors (8.9G)",
            "[2026-11-05 10:00:05] [MODELS] FASE 2 COMPLETADA (Perfil: all).",
        ]));
        let parsed = parse_phases_from_lines(&log, false, "all", None);
        assert_eq!(parsed.phase1.state, "done");
        assert_eq!(parsed.phase2.state, "done");
        assert_eq!(parsed.phase2.done, 1);
        assert_eq!(parsed.phase2.total, 14);
    }

    #[test]
    fn banner_reports_not_required_instead_of_a_forever_pending_phase() {
        let log = bootstrap_log_nothing_to_download();
        let parsed = parse_phases_from_lines(&log, false, "all", None);
        let banner = format_phase_banner(&comfy_running(), &parsed, None);
        assert_eq!(
            banner,
            "ComfyUI listo · Descargas no requeridas (el arranque no arrastra modelos)"
        );
        assert!(!banner.contains("pending"));
    }

    #[test]
    fn banner_still_prefers_phase2_progress_over_not_required() {
        let mut log = bootstrap_log_nothing_to_download();
        log.extend(lines(&[
            "[2026-11-05 10:00:00] [MODELS] INICIANDO FASE 2 (Segundo Plano — Perfil: all)",
            "[2026-11-05 10:00:00] [MODELS] PLAN FASE 2: 14 archivos (all)",
            "[2026-11-05 10:00:01] [MODELS] INICIO: flux1-dev-fp8.safetensors (Comfy-Org/flux1-dev/flux1-dev-fp8.safetensors)",
        ]));
        let parsed = parse_phases_from_lines(&log, false, "all", None);
        assert_eq!(parsed.phase2.state, "running");
        let banner = format_phase_banner(&comfy_running(), &parsed, None);
        assert_eq!(
            banner,
            "ComfyUI listo · Fase 2: 0/14 modelos (flux1-dev-fp8.safetensors)"
        );
    }
}
