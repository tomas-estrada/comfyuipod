//! Causa de arranque y de salida del supervisor, visible sin entrar al pod:
//! qué proceso murió, con qué código, la última línea útil del log (ya
//! redactada) y el fallo del entrypoint que nunca llegó a `start.sh`.

use comfyctl::collect::startup::{collect_startup_status, StartupStatus};
use comfyctl::logtail::last_useful_line;

#[test]
fn sin_evidencia_de_arranque_la_causa_es_el_entrypoint() {
    let log_dir = std::env::temp_dir().join("comfyctl_startup_vacio");
    let _ = std::fs::remove_dir_all(&log_dir);
    let run_dir = std::env::temp_dir().join("comfyctl_startup_vacio_run");
    let _ = std::fs::remove_dir_all(&run_dir);

    let st = collect_startup_status(&log_dir, &run_dir);
    std::fs::remove_dir_all(&log_dir).ok();
    std::fs::remove_dir_all(&run_dir).ok();

    assert!(st.cause.contains("start.sh"), "cause: {st:?}");
    assert!(
        st.cause.contains("entrypoint") || st.cause.contains("onstart"),
        "cause: {st:?}"
    );
    // Aquí sí se midió: vacío, no «no disponible».
    assert_eq!(st.onstart, "vacío");
    assert_eq!(st.supervisor_json, "ausente");
}

#[test]
fn onstart_con_lineas_sin_supervisor_dice_la_causa_intermedia() {
    let log_dir = std::env::temp_dir().join("comfyctl_startup_ons");
    let _ = std::fs::remove_dir_all(&log_dir);
    std::fs::create_dir_all(&log_dir).unwrap_or_default();
    let _ = std::fs::write(
        log_dir.join("onstart.log"),
        "iniciando imagen…\nopcionando volumen…\n",
    );
    let run_dir = std::env::temp_dir().join("comfyctl_startup_ons_run");
    let _ = std::fs::remove_dir_all(&run_dir);

    let st = collect_startup_status(&log_dir, &run_dir);
    std::fs::remove_dir_all(&log_dir).ok();
    std::fs::remove_dir_all(&run_dir).ok();

    assert!(st.onstart.starts_with("2 "), "onstart: {st:?}");
    assert!(st.cause.contains("supervisor"), "cause: {st:?}");
}

#[test]
fn supervisor_y_onstart_buenos_no_alarman() {
    let log_dir = std::env::temp_dir().join("comfyctl_startup_ok");
    let _ = std::fs::remove_dir_all(&log_dir);
    std::fs::create_dir_all(log_dir.join("nginx")).unwrap_or_default();
    let _ = std::fs::write(log_dir.join("supervisor.log"), "sup arrancó\n");
    let run_dir = std::env::temp_dir().join("comfyctl_startup_ok_run");
    std::fs::create_dir_all(&run_dir).unwrap_or_default();
    let _ = std::fs::write(run_dir.join("comfyui-supervisor.json"), "{}");

    let st = collect_startup_status(&log_dir, &run_dir);
    std::fs::remove_dir_all(&log_dir).ok();
    std::fs::remove_dir_all(&run_dir).ok();

    assert_eq!(st.supervisor_json, "presente");
    assert_eq!(st.cause, "arranque en marcha (start.sh dejó supervisor)");
}

#[test]
fn last_useful_line_devuelve_la_ultima_linea_y_la_recorta() {
    let log_dir = std::env::temp_dir().join("comfyctl_startup_lll");
    let _ = std::fs::remove_dir_all(&log_dir);
    std::fs::create_dir_all(&log_dir).unwrap_or_default();
    let _ = std::fs::write(
        log_dir.join("comfyui.log"),
        "linea vieja\n\n   \nultíma línea útil con datos\n",
    );
    let got = last_useful_line(&log_dir.join("comfyui.log"), 180);
    std::fs::remove_dir_all(&log_dir).ok();
    assert_eq!(got.as_deref(), Some("ultíma línea útil con datos"));
    // Sin fichero: no disponible (Option None), nunca un valor fijo.
    assert_eq!(last_useful_line(&log_dir.join("comfyui.log"), 180), None);
}

#[test]
fn report_nombra_proceso_rc_y_ultima_linea_ja_redactados() {
    use comfyctl::collect::gpu::GpuStatus;
    use comfyctl::collect::models::WeightStatus;
    use comfyctl::collect::phases::PhasesStatus;
    use comfyctl::collect::procfs::{
        ComfyUiStatus, DiskStatus, ImageMeta, InstanceInfo, MemoryStatus, NginxServiceStatus,
        ServicesStatus, StatusServerServiceStatus, TerminalServiceStatus,
    };
    use comfyctl::collect::startup::{startup_failed_entrypoint, startup_unmeasured};
    use comfyctl::collect::StatusApiPayload;
    use comfyctl::redact::SecretRedactor;
    use comfyctl::report::{build_report_markdown, HeavyCacheData};
    use comfyctl::supervisor::LastExitInfo;
    use std::collections::BTreeMap;
    use std::fs;

    let log_dir = std::env::temp_dir().join("comfyctl_startup_report_logs");
    let _ = fs::remove_dir_all(&log_dir);
    fs::create_dir_all(&log_dir).unwrap_or_default();
    // El log real lleva un secreto: la última línea útil debe salir redactada.
    let _ = fs::write(
        log_dir.join("supervisor.log"),
        "ComfyUI terminó (rc=137) con Authorization: Bearer abcd1234efgh5678 remanente\n",
    );

    let compose = |startup: StartupStatus| -> String {
        let api = StatusApiPayload {
            generated_at: "2026-11-06T10:00:00Z".to_string(),
            image: ImageMeta {
                tag: "sha-fixture".to_string(),
                built_at: "2026-11-06T09:00:00Z".to_string(),
                comfyui_sha: "4c62a84068a0".to_string(),
                pytorch: "2.9.1+cu128".to_string(),
                cuda: "12.8".to_string(),
                ubuntu: "24.04".to_string(),
            },
            instance: InstanceInfo {
                hostname: "vast-fixture".to_string(),
                uptime_s: 60,
                vast_instance_id: "C.000000".to_string(),
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
            },
            disk: DiskStatus {
                path: "/workspace/comfyui-data".to_string(),
                total_gb: 100.0,
                used_gb: 10.0,
                free_gb: 90.0,
            },
            comfyui: ComfyUiStatus {
                state: "down".to_string(),
                supervisor_state: "fatal".to_string(),
                pid: None,
                rss_mb: 0,
                uptime_s: 0,
                port_open: false,
                restarts: 5,
                healthy_resets: 0,
                max_restarts: 5,
                last_exit: Some(LastExitInfo {
                    rc: 137,
                    code: 137,
                    reason: "OOM de RAM (SIGKILL)".to_string(),
                    at: 1_760_000_000,
                    process: "python3 main.py (ComfyUI)".to_string(),
                    last_log_line: Some(
                        "ComfyUI terminó (rc=137) con Authorization: Bearer abcd1234efgh5678 remanente".to_string(),
                    ),
                }),
                last_log_line: None,
            },
            phases: PhasesStatus::default(),
            phase_banner: "ComfyUI caído".to_string(),
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
            startup: Some(startup),
            strict: None,
        };
        let redactor = SecretRedactor::new(None, None);
        let heavy = HeavyCacheData::default();
        build_report_markdown(&api, &heavy, &redactor, &log_dir)
    };

    // (a) Con startup medido: la sección 3 dice proceso, rc y la última línea.
    let md = compose(startup_failed_entrypoint());
    assert!(md.contains("## 3. ComfyUI y supervisor"));
    let sec3 = md
        .split("## 3. ComfyUI y supervisor")
        .nth(1)
        .unwrap_or_default();
    assert!(sec3.contains("`python3 main.py (ComfyUI)`"), "sec3: {sec3}");
    assert!(sec3.contains("rc=137"), "sec3: {sec3}");
    assert!(sec3.contains("nea útil"), "sec3: {sec3}");
    assert!(!md.contains("abcd1234efgh5678"), "secreto filtrado");
    assert!(md.contains("[REDACTED]"), "la línea útil sale redactada");

    // (b) Sin medición de startup: el informe no inventa, dice no disponible.
    let md2 = compose(startup_unmeasured());
    let sec3b = md2
        .split("## 3. ComfyUI y supervisor")
        .nth(1)
        .unwrap_or_default();
    assert!(sec3b.contains("no disponible"), "sec3b: {sec3b}");
    fs::remove_dir_all(&log_dir).ok();
}
