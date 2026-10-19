//! Snapshot test y verificación de cabeceras y redacción de `/report.md` (§8.3).

use comfyctl::collect::drift::DriftSummary;
use comfyctl::collect::gpu::GpuStatus;
use comfyctl::collect::phases::{Phase1Info, Phase2Info, PhasesStatus};
use comfyctl::collect::procfs::{
    ComfyUiStatus, DiskStatus, ImageMeta, InstanceInfo, MemoryStatus, NginxServiceStatus,
    ServicesStatus, StatusServerServiceStatus, TerminalServiceStatus,
};
use comfyctl::collect::StatusApiPayload;
use comfyctl::redact::SecretRedactor;
use comfyctl::report::{build_report_markdown, HeavyCacheData};
use comfyctl::strict::{StrictCheckItem, StrictReport};
use std::collections::BTreeMap;
use std::fs;

#[test]
fn report_markdown_matches_snapshot_and_redacts_secrets() {
    let tmp_log_dir = std::env::temp_dir().join("comfyctl_report_test_logs");
    let _ = fs::remove_dir_all(&tmp_log_dir);
    let _ = fs::create_dir_all(tmp_log_dir.join("nginx"));

    let _ = fs::write(
        tmp_log_dir.join("diag.txt"),
        "[DIAG][OK] gpu: NVIDIA GeForce RTX 5090\n[DIAG] resumen: 10 ok, 0 warn, 0 fail\n",
    );
    let _ = fs::write(
        tmp_log_dir.join("comfyui.log"),
        "Starting server on 127.0.0.1:18188 (pass=secret-pass-123)\n",
    );

    let mut env_masked = BTreeMap::new();
    env_masked.insert("MODEL_PROFILE".to_string(), "anime".to_string());
    env_masked.insert("COMFY_USER".to_string(), "admin".to_string());
    env_masked.insert(
        "COMFY_PASS".to_string(),
        "<definida por el usuario>".to_string(),
    );
    env_masked.insert("HF_TOKEN".to_string(), "<no definido>".to_string());
    env_masked.insert("COMFYUI_EXTRA_ARGS".to_string(), String::new());
    env_masked.insert(
        "HF_HUB_DISABLE_XET".to_string(),
        "<no definido>".to_string(),
    );
    env_masked.insert("ENABLE_WEB_TERMINAL".to_string(), "0".to_string());

    let api = StatusApiPayload {
        generated_at: "2026-10-19T12:00:00Z".to_string(),
        image: ImageMeta {
            tag: "sha-512e485".to_string(),
            built_at: "2026-10-19T11:00:00Z".to_string(),
            comfyui_sha: "4c62a84068a0".to_string(),
            pytorch: "2.9.1+cu128".to_string(),
            cuda: "12.8".to_string(),
            ubuntu: "24.04".to_string(),
        },
        instance: InstanceInfo {
            hostname: "vast-5090-node".to_string(),
            uptime_s: 120,
            vast_instance_id: "C.998877".to_string(),
            external_port_8188: "41888".to_string(),
        },
        gpu: GpuStatus {
            available: true,
            name: "NVIDIA GeForce RTX 5090".to_string(),
            driver: "570.124.06".to_string(),
            vram_total_mb: 32768,
            vram_used_mb: 1024,
            util_pct: 10,
            temp_c: 42,
            processes: Vec::new(),
            reason: None,
        },
        memory: MemoryStatus {
            total_mb: 64340,
            used_mb: 11410,
            available_mb: 52930,
            swap_used_mb: 0,
        },
        disk: DiskStatus {
            path: "/workspace/comfyui-data".to_string(),
            total_gb: 150.0,
            used_gb: 30.0,
            free_gb: 120.0,
        },
        comfyui: ComfyUiStatus {
            state: "running".to_string(),
            supervisor_state: "running".to_string(),
            pid: Some(1234),
            rss_mb: 2048,
            uptime_s: 110,
            port_open: true,
            restarts: 0,
            healthy_resets: 0,
            max_restarts: 5,
            last_exit: None,
        },
        phases: PhasesStatus {
            phase1: Phase1Info {
                state: "done".to_string(),
                done: 12,
                total: 12,
                failed: Vec::new(),
                elapsed_s: 0,
            },
            phase2: Phase2Info {
                state: "done".to_string(),
                done: 6,
                total: 6,
                failed: Vec::new(),
                current_file: None,
                hf_pid: None,
                hf_rss_mb: 0,
            },
        },
        phase_banner: "ComfyUI listo · Fase 2 completada".to_string(),
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
        },
        errors_recent: Vec::new(),
        triage: Vec::new(),
        env_masked,
        strict: Some(StrictReport {
            checked_at: "2026-10-19T11:58:00Z".to_string(),
            strict_start: true,
            result: "ok".to_string(),
            checks: vec![StrictCheckItem {
                id: "driver_cuda128".to_string(),
                name: "Driver NVIDIA >= 570 (CUDA 12.8)".to_string(),
                status: "ok".to_string(),
                detail: "Driver NVIDIA 570.124.06 compatible con CUDA 12.8".to_string(),
                remediation: None,
            }],
        }),
    };

    let heavy = HeavyCacheData {
        drift: DriftSummary::default(),
        folder_stats: vec!["- `checkpoints`: 3 ficheros (19.50 GB)".to_string()],
        missing_models: Vec::new(),
        refreshed_at_unix: 1_760_000_000,
        cache_age_s: 0,
    };

    let redactor = SecretRedactor::new(Some("secret-pass-123"), None);
    let md = build_report_markdown(&api, &heavy, &redactor, &tmp_log_dir);
    let _ = fs::remove_dir_all(&tmp_log_dir);

    assert!(!md.contains("secret-pass-123"));
    assert!(md.contains("[REDACTED]"));

    for hdr in [
        "## 0. Triage automático",
        "## 1. Contexto operativo para el agente",
        "## 2. Diagnóstico de arranque ([DIAG])",
        "## 3. ComfyUI y supervisor",
        "## 4. GPU, memoria y disco",
        "## 5. Descargas (Fase 1 y Fase 2)",
        "## 6. Log de ComfyUI (últimas 120 líneas)",
        "## 7. Nginx",
        "## 8. Drift del entorno",
        "## 9. Modelos",
        "## 10. Arranque estricto",
    ] {
        assert!(md.contains(hdr), "Falta cabecera contractual: {hdr}");
    }

    let expected_snapshot = include_str!("fixtures/report_expected.md").replace("\r\n", "\n");
    assert_eq!(md, expected_snapshot);
}
