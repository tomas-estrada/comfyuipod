//! `collect::gpu` — Sonda de estado de GPU (`nvidia-smi`) conforme a `status-api.schema.json`.

use crate::sys;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GpuStatus {
    pub available: bool,
    pub name: String,
    pub driver: String,
    pub vram_total_mb: u64,
    pub vram_used_mb: u64,
    pub util_pct: u32,
    pub temp_c: u32,
    #[serde(default)]
    pub processes: Vec<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// Procesos con contexto CUDA activo, conforme a `gpu.processes` del esquema (S-03).
/// Si la consulta falla o no hay procesos, devuelve una lista vacía.
pub fn collect_gpu_processes(nvidia_smi_bin: &Path) -> Vec<serde_json::Value> {
    let mut cmd = Command::new(nvidia_smi_bin);
    cmd.args([
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ]);
    let Ok((Some(0), txt, _, false)) =
        sys::run_command_with_timeout(&mut cmd, Duration::from_secs(5))
    else {
        return Vec::new();
    };
    txt.lines()
        .filter_map(|ln| {
            let parts: Vec<&str> = ln.split(',').map(str::trim).collect();
            let pid = parts.first().and_then(|s| s.parse::<u64>().ok())?;
            let name = parts.get(1).copied().unwrap_or("unknown").to_string();
            let used_mb = parts
                .get(2)
                .and_then(|s| s.parse::<u64>().ok())
                .unwrap_or(0);
            Some(serde_json::json!({"pid": pid, "name": name, "used_mb": used_mb}))
        })
        .collect()
}

pub fn collect_gpu_status(nvidia_smi_bin: &Path) -> GpuStatus {
    let mut cmd = Command::new(nvidia_smi_bin);
    cmd.args([
        "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]);

    if let Ok((Some(0), txt, _, false)) =
        sys::run_command_with_timeout(&mut cmd, Duration::from_secs(5))
    {
        if let Some(first_line) = txt.lines().next() {
            let parts: Vec<&str> = first_line.split(',').map(str::trim).collect();
            if parts.len() >= 6 {
                let name = parts.first().copied().unwrap_or("Unknown GPU").to_string();
                let driver = parts.get(1).copied().unwrap_or("unknown").to_string();
                let vram_total_mb = parts
                    .get(2)
                    .and_then(|s| s.parse::<u64>().ok())
                    .unwrap_or(0);
                let vram_used_mb = parts
                    .get(3)
                    .and_then(|s| s.parse::<u64>().ok())
                    .unwrap_or(0);
                let util_pct = parts
                    .get(4)
                    .and_then(|s| s.parse::<u32>().ok())
                    .unwrap_or(0);
                let temp_c = parts
                    .get(5)
                    .and_then(|s| s.parse::<u32>().ok())
                    .unwrap_or(0);

                return GpuStatus {
                    available: true,
                    name,
                    driver,
                    vram_total_mb,
                    vram_used_mb,
                    util_pct,
                    temp_c,
                    processes: collect_gpu_processes(nvidia_smi_bin),
                    reason: None,
                };
            }
        }
    }

    GpuStatus {
        available: false,
        name: "N/A (CPU mode)".to_string(),
        driver: "N/A".to_string(),
        vram_total_mb: 0,
        vram_used_mb: 0,
        util_pct: 0,
        temp_c: 0,
        processes: Vec::new(),
        reason: Some("nvidia-smi no disponible o instancia sin GPU".to_string()),
    }
}
