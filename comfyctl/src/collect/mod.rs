//! `collect` — Orquestación de recolectores para `/status/api` (conforme a `docs/contratos/status-api.schema.json`).

pub mod drift;
pub mod gpu;
pub mod models;
pub mod phases;
pub mod procfs;

use crate::collect::gpu::{collect_gpu_status, GpuStatus};
use crate::collect::phases::{collect_phases_status, format_phase_banner, PhasesStatus};
use crate::collect::procfs::{
    build_masked_env, collect_comfyui_status, collect_memory_and_disk, collect_services_status,
    find_hf_download_process, get_container_uptime_s, get_hostname, load_image_meta,
    load_strict_report, ComfyUiStatus, DiskStatus, ImageMeta, InstanceInfo, MemoryStatus,
    ServicesStatus,
};
use crate::config::RuntimeConfig;
use crate::logtail::{resolve_log_path, tail_lines};
use crate::strict::StrictReport;
use crate::sys;
use crate::triage::{RecentError, TriageEngine, TriageHit};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::time::SystemTime;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusApiPayload {
    pub generated_at: String,
    pub image: ImageMeta,
    pub instance: InstanceInfo,
    pub gpu: GpuStatus,
    pub memory: MemoryStatus,
    pub disk: DiskStatus,
    pub comfyui: ComfyUiStatus,
    pub phases: PhasesStatus,
    pub phase_banner: String,
    pub services: ServicesStatus,
    pub errors_recent: Vec<RecentError>,
    pub triage: Vec<TriageHit>,
    pub env_masked: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub strict: Option<StrictReport>,
}

pub fn build_status_api_payload(
    cfg: &RuntimeConfig,
    triage_engine: &TriageEngine,
    log_dir: &Path,
) -> StatusApiPayload {
    let image = load_image_meta(&cfg.image_json_path);
    let gpu = collect_gpu_status(&cfg.nvidia_smi_bin);
    let (memory, disk) = collect_memory_and_disk(&cfg.comfy_data_dir);
    let comfyui = collect_comfyui_status(cfg);

    let hf_proc = find_hf_download_process();
    let dl_log_path = resolve_log_path("download", log_dir)
        .unwrap_or_else(|| log_dir.join("download-models.log"));
    let phases = collect_phases_status(
        &dl_log_path,
        cfg.skip_model_download,
        cfg.model_profile.as_str(),
        hf_proc,
    );

    let services = collect_services_status(cfg);
    let strict = load_strict_report(&cfg.strict_json_path);
    let phase_banner = format_phase_banner(&comfyui, &phases, strict.as_ref());

    let mut log_buffers: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (name, max_n) in [
        ("comfyui", 400_usize),
        ("nginx-error", 200),
        ("nginx-access", 400),
        ("download", 300),
        ("supervisor", 200),
        ("diag", 100),
        ("container", 200),
    ] {
        let lines = resolve_log_path(name, log_dir)
            .map(|p| tail_lines(&p, max_n))
            .unwrap_or_default();
        log_buffers.insert(name.to_string(), lines);
    }

    let mut proc_lines = Vec::new();
    if let Some(hf_pid) = phases.phase2.hf_pid {
        if let Ok(meta) = fs::metadata(&dl_log_path) {
            if let Ok(mtime) = meta.modified() {
                if let Ok(elapsed) = SystemTime::now().duration_since(mtime) {
                    if elapsed.as_secs() > 1800 {
                        proc_lines.push(format!(
                            "hf_stuck_over_30m: pid={hf_pid} sin avance hace {}s",
                            elapsed.as_secs()
                        ));
                    }
                }
            }
        }
    }
    log_buffers.insert("processes".to_string(), proc_lines);

    let mut api_lines = Vec::new();
    if disk.total_gb > 0.0 && disk.free_gb < 10.0 {
        api_lines.push(format!(
            "disk.free_gb < 10 (libre actual: {} GB en {})",
            disk.free_gb, disk.path
        ));
    }
    log_buffers.insert("api".to_string(), api_lines);

    let comfy_running = comfyui.state == "running";
    let (triage, errors_recent) = triage_engine.evaluate(&log_buffers, comfy_running);
    let env_masked = build_masked_env(cfg);

    StatusApiPayload {
        generated_at: sys::format_utc_iso8601(sys::now_unix_secs()),
        image,
        instance: InstanceInfo {
            hostname: get_hostname(),
            uptime_s: get_container_uptime_s(&cfg.started_at_path),
            vast_instance_id: cfg.vast_containerlabel.clone(),
            external_port_8188: cfg.vast_tcp_port_8188.clone(),
        },
        gpu,
        memory,
        disk,
        comfyui,
        phases,
        phase_banner,
        services,
        errors_recent,
        triage,
        env_masked,
        strict,
    }
}
