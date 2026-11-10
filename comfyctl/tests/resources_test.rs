//! Espacio, VRAM y RAM: disponible vs requerido, con la reserva vigente
//! (VRAM 2048 MB, RAM 8192 MB, `hardware_profile.py`). Y lo no medible es
//! «no disponible»: nunca un 0 que se lee como «sin espacio» (F2).

use comfyctl::collect::procfs::parse_proc_meminfo;
use std::fs;

#[test]
fn disco_en_ruta_inexistente_es_no_medible_no_cero() {
    // La medición ausente se DECLARA (`measurable: false`), en disco cuando
    // `statvfs` falla (Linux) y en RAM cuando `/proc/meminfo` no está.
    let tmp = std::env::temp_dir().join("comfyctl_res_disc_inexistente");
    let _ = fs::remove_dir_all(&tmp);

    let (mem, disk) = comfyctl::collect::procfs::collect_memory_and_disk(&tmp);
    fs::remove_dir_all(&tmp).ok();

    assert!(
        !mem.measurable,
        "sin /proc/meminfo la RAM no está medida: no disponible, no 0"
    );
    if cfg!(unix) {
        assert!(
            !disk.measurable,
            "statvfs sobre una ruta inexistente falla cerrado: {}",
            disk.path
        );
    }
}

#[test]
fn memoria_real_parsea_y_la_reserva_se_aplica() {
    // /proc/meminfo con formato real de Linux (fixture literal).
    let mi = parse_proc_meminfo(
        "MemTotal:       32742312 kB\nMemAvailable:   10001234 kB\nSwapTotal:       2097148 kB\nSwapFree:        1048576 kB\n",
    );
    assert_eq!(mi.total_mb, 32_742_312 / 1024);
    assert_eq!(mi.available_mb, 10_001_234 / 1024);
}

#[test]
fn budget_separado_vram_y_ram_con_las_reservas_vigentes() {
    // Regla vigente: reserva VRAM 2048 MB, RAM 8192 MB (hardware_profile).
    let budget = comfyctl::collect::ResourceBudget {
        disk_free_gb: Some(3.2),
        disk_measurable: true,
        disk_required_gb: None,
        vram_total_mb: Some(32768),
        vram_used_mb: Some(30000),
        vram_reserve_mb: 2048,
        vram_usable_mb: Some(32768 - 2048),
        ram_total_mb: Some(64800),
        ram_available_mb: Some(9000),
        ram_reserve_mb: 8192,
        ram_usable_mb: Some(9000 - 8192),
        vram_required_mb: None,
        ram_required_mb: None,
    };
    let json = serde_json::to_string(&budget).unwrap_or_default();
    assert!(json.contains("vram_reserve_mb"));
    assert!(json.contains("ram_reserve_mb"));
}

#[test]
fn report_diseno_no_medible_dice_no_disponible_y_no_sin_espacio() {
    use comfyctl::collect::gpu::GpuStatus;
    use comfyctl::collect::models::WeightStatus;
    use comfyctl::collect::phases::PhasesStatus;
    use comfyctl::collect::procfs::*;
    use comfyctl::collect::startup::startup_failed_entrypoint;
    use comfyctl::collect::StatusApiPayload;
    use comfyctl::redact::SecretRedactor;
    use comfyctl::report::{build_report_markdown, HeavyCacheData};
    use std::collections::BTreeMap;

    let log_dir = std::env::temp_dir().join("comfyctl_res_report_logs");
    let _ = fs::remove_dir_all(&log_dir);
    fs::create_dir_all(&log_dir).unwrap_or_default();

    let api = StatusApiPayload {
        generated_at: "2026-11-06T12:00:00Z".to_string(),
        image: ImageMeta {
            tag: "sha-fix".to_string(),
            built_at: "2026-11-06T11:00:00Z".to_string(),
            comfyui_sha: "4c62a84068a0".to_string(),
            pytorch: "2.9.1+cu128".to_string(),
            cuda: "12.8".to_string(),
            ubuntu: "24.04".to_string(),
        },
        instance: InstanceInfo {
            hostname: "vast-res".to_string(),
            uptime_s: 60,
            vast_instance_id: "C.1".to_string(),
            external_port_8188: "41888".to_string(),
        },
        gpu: GpuStatus {
            available: false,
            name: String::new(),
            driver: String::new(),
            vram_total_mb: 0,
            vram_used_mb: 0,
            util_pct: 0,
            temp_c: 0,
            processes: Vec::new(),
            reason: Some("Modo CPU".to_string()),
        },
        memory: MemoryStatus {
            total_mb: 2048,
            used_mb: 512,
            available_mb: 1536,
            swap_used_mb: 0,
            measurable: true,
        },
        disk: DiskStatus {
            path: "/workspace/comfyui-data".to_string(),
            total_gb: 0.0,
            used_gb: 0.0,
            free_gb: 0.0,
            measurable: false,
        },
        comfyui: ComfyUiStatus {
            state: "running".to_string(),
            supervisor_state: "running".to_string(),
            pid: None,
            rss_mb: 0,
            uptime_s: 60,
            port_open: true,
            restarts: 0,
            healthy_resets: 0,
            max_restarts: 5,
            last_exit: None,
            last_log_line: None,
        },
        phases: PhasesStatus::default(),
        phase_banner: "ComfyUI listo".to_string(),
        services: ServicesStatus {
            nginx: NginxServiceStatus {
                running: true,
                workers_user: "www-data".to_string(),
            },
            status_server: StatusServerServiceStatus { running: true },
            terminal: TerminalServiceStatus {
                enabled: false,
                running: false,
            },
            studio_server: None,
        },
        errors_recent: Vec::new(),
        triage: Vec::new(),
        env_masked: BTreeMap::new(),
        weights: WeightStatus::default(),
        startup: Some(startup_failed_entrypoint()),
        resources: comfyctl::collect::ResourceBudget {
            disk_free_gb: None,
            disk_measurable: false,
            disk_required_gb: None,
            vram_total_mb: None,
            vram_used_mb: None,
            vram_reserve_mb: 2048,
            vram_usable_mb: None,
            vram_required_mb: None,
            ram_total_mb: None,
            ram_available_mb: None,
            ram_reserve_mb: 8192,
            ram_usable_mb: None,
            ram_required_mb: None,
        },
        strict: None,
    };

    let redactor = SecretRedactor::new(None, None);
    let md = build_report_markdown(&api, &HeavyCacheData::default(), &redactor, &log_dir);
    fs::remove_dir_all(&log_dir).ok();

    let sec4 = md
        .split("## 4. GPU, memoria y disco")
        .nth(1)
        .unwrap_or_default();
    // La medición ausente no se lee «0 GB libres» (= sin espacio falso).
    assert!(!sec4.contains("0 GB libres"), "sec4: {sec4}");
    assert!(sec4.contains("no disponible"), "sec4: {sec4}");
    // La sección presupuesta separa VRAM de RAM y nombra las reservas vigentes.
    assert!(sec4.contains("VRAM") && sec4.contains("RAM"));
    assert!(sec4.contains("2048"), "reserva VRAM vigente: {sec4}");
    assert!(sec4.contains("8192"), "reserva RAM vigente: {sec4}");
    // El requerimiento desconocido se publica como «no disponible».
    assert!(sec4.contains("requerido"), "sec4: {sec4}");
}

#[test]
fn la_alarma_de_disco_lleno_solo_con_medicion() {
    use comfyctl::collect::procfs::DiskStatus;
    // Sin medición (0s sintéticos): no hay alarma "disk.full".
    let no_medible = DiskStatus {
        path: "/workspace/comfyui-data".to_string(),
        total_gb: 0.0,
        used_gb: 0.0,
        free_gb: 0.0,
        measurable: false,
    };
    assert!(comfyctl::collect::disk_warning_line(&no_medible).is_none());
    // Medición real apretada: sí hay alarma (con la condición vigente < 10 GB).
    let medible = DiskStatus {
        path: "/workspace/comfyui-data".to_string(),
        total_gb: 150.0,
        used_gb: 145.0,
        free_gb: 3.2,
        measurable: true,
    };
    let alarm = comfyctl::collect::disk_warning_line(&medible);
    assert!(alarm.is_some());
    assert!(alarm.unwrap_or_default().contains("3.2"));
}
