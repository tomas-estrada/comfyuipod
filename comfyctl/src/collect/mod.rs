//! `collect` — Orquestación de recolectores para `/status/api` (conforme a `docs/contratos/status-api.schema.json`).

pub mod drift;
pub mod gpu;
pub mod models;
pub mod phases;
pub mod procfs;
pub mod startup;

use crate::collect::gpu::{collect_gpu_status, GpuStatus};
use crate::collect::models::{collect_weight_status, WeightStatus};
use crate::collect::phases::{
    add_disk_truth, collect_phases_status, format_phase_banner, PhasesStatus,
};
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
    /// Estado de pesos del perfil medido en disco (qué falta y qué llegó).
    pub weights: WeightStatus,
    /// Causa de arranque/salida observable sin entrar al pod. `None` = no medido
    /// (el informe dice «no disponible», no inventa).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub startup: Option<crate::collect::startup::StartupStatus>,
    /// Espacio, VRAM y RAM: disponible vs requerido, con las reservas vigentes
    /// (VRAM 2048 MB, RAM 8192 MB, `studio/comfy_studio/hardware_profile.py`).
    /// VRAM y RAM se evalúan por separado: no son intercambiables.
    pub resources: ResourceBudget,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub strict: Option<StrictReport>,
}

/// Presupuesto de recursos separado por tipo, con la reserva vigente aplicada.
/// Las entradas `Option::None` son «no disponible» (sin medición): nunca 0 fijo.
#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct ResourceBudget {
    pub disk_free_gb: Option<f64>,
    pub disk_measurable: bool,
    /// Espacio que la puerta de arranque exige (`storage-requirement.json`);
    /// `None` = no disponible (sin fichero derivado o perfil sin entrada).
    pub disk_required_gb: Option<f64>,
    pub vram_total_mb: Option<u64>,
    pub vram_used_mb: Option<u64>,
    pub vram_reserve_mb: u64,
    pub vram_usable_mb: Option<u64>,
    /// Requerimiento de VRAM del flujo actual: sin workflow cargado no se mide.
    pub vram_required_mb: Option<u64>,
    pub ram_total_mb: Option<u64>,
    pub ram_available_mb: Option<u64>,
    pub ram_reserve_mb: u64,
    pub ram_usable_mb: Option<u64>,
    pub ram_required_mb: Option<u64>,
}

/// Reserva vigente (§ «regla de tarefa»: VRAM 2 GB, RAM 8 GB).
pub const VRAM_RESERVE_MB: u64 = 2048;
pub const RAM_RESERVE_MB: u64 = 8192;

/// Línea de alarma `disk.free_gb < 10` para el triage; `None` cuando la medición
/// no existe (0s sintéticos): el patrón `disk-full` no debe dispararse sin dato.
#[must_use]
pub fn disk_warning_line(disk: &DiskStatus) -> Option<String> {
    if !disk.measurable || disk.total_gb <= 0.0 {
        return None;
    }
    if disk.free_gb < 10.0 {
        Some(format!(
            "disk.free_gb < 10 (libre actual: {} GB en {})",
            disk.free_gb, disk.path
        ))
    } else {
        None
    }
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
    let mut phases = collect_phases_status(
        &dl_log_path,
        cfg.skip_model_download,
        cfg.model_profile.as_str(),
        hf_proc,
    );

    let services = collect_services_status(cfg);
    let strict = load_strict_report(&cfg.strict_json_path);
    let weights = collect_weight_status(&cfg.comfy_data_dir, cfg.model_profile.as_str());
    add_disk_truth(&mut phases, &weights);
    let resources = collect_resource_budget(&memory, &disk, &gpu, cfg);
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
    if let Some(line) = disk_warning_line(&disk) {
        api_lines.push(line);
    }
    log_buffers.insert("api".to_string(), api_lines);

    let comfy_running = comfyui.state == "running";
    // Última línea útil del log del proceso: primero comfyui.log (el traceback
    // real) y como respaldo supervisor.log; el render viaja ya redactado.
    let last_log_line = crate::logtail::last_useful_line(
        &resolve_log_path("comfyui", log_dir).unwrap_or_else(|| log_dir.join("comfyui.log")),
        300,
    )
    .or_else(|| {
        crate::logtail::last_useful_line(
            &resolve_log_path("supervisor", log_dir)
                .unwrap_or_else(|| log_dir.join("supervisor.log")),
            300,
        )
    });
    let mut comfyui = comfyui;
    comfyui.last_log_line = last_log_line;
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
        weights,
        startup: Some(crate::collect::startup::collect_startup_status(
            log_dir,
            &cfg.supervisor_json_path,
        )),
        resources,
        strict,
    }
}

/// Presupuesto separado por tipo; el requerido sale del derivado de
/// `scripts/storage_requirement.py` (`storage-requirement.json`). Sin fichero
/// derivado o sin GPU/meminfo: `None` = «no disponible», nunca 0 inventado.
fn collect_resource_budget(
    memory: &MemoryStatus,
    disk: &DiskStatus,
    gpu: &GpuStatus,
    cfg: &RuntimeConfig,
) -> ResourceBudget {
    let mut budget = build_resource_budget(memory, disk, gpu);
    budget.disk_required_gb = disk_start_gate_gb(cfg, cfg.model_profile.as_str());
    budget
}

/// Presupuesto separado por tipo desde las medidas ya recolectadas. Al ser
/// `build` puro lo ejercitan los tests sin ficheros ni entorno.
fn build_resource_budget(
    memory: &MemoryStatus,
    disk: &DiskStatus,
    gpu: &GpuStatus,
) -> ResourceBudget {
    let disk_free = if disk.measurable {
        Some(disk.free_gb)
    } else {
        None
    };
    let vram_total = if gpu.available && gpu.vram_total_mb > 0 {
        Some(gpu.vram_total_mb)
    } else {
        None
    };
    let vram_used = if gpu.available {
        Some(gpu.vram_used_mb)
    } else {
        None
    };
    let vram_usable = vram_total.map(|total| total.saturating_sub(VRAM_RESERVE_MB));
    let ram_available = if memory.measurable {
        Some(memory.available_mb)
    } else {
        None
    };
    ResourceBudget {
        disk_free_gb: disk_free,
        disk_measurable: disk.measurable,
        disk_required_gb: None,
        vram_total_mb: vram_total,
        vram_used_mb: vram_used,
        vram_reserve_mb: VRAM_RESERVE_MB,
        vram_usable_mb: vram_usable,
        vram_required_mb: None,
        ram_total_mb: memory.measurable.then_some(memory.total_mb),
        ram_available_mb: ram_available,
        ram_reserve_mb: RAM_RESERVE_MB,
        ram_usable_mb: ram_available.map(|a| a.saturating_sub(RAM_RESERVE_MB)),
        ram_required_mb: None,
    }
}

fn disk_start_gate_gb(cfg: &RuntimeConfig, profile: &str) -> Option<f64> {
    let txt = fs::read_to_string(&cfg.storage_requirement_path).ok()?;
    let val: serde_json::Value = serde_json::from_str(&txt).ok()?;
    let start_gate = val
        .get("profiles")?
        .get(profile)?
        .get("start_gate_gb")?
        .as_f64()?;
    (start_gate > 0.0).then_some(start_gate)
}
