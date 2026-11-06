//! Estado de pesos del flujo pedido, leÃ­do de la verdad en disco (no del log de
//! descarga) y mostrado en `/report.md` y en la pÃ¡gina de estado.

use comfyctl::collect::models::{collect_weight_status, WeightStatus};
use comfyctl::report::build_report_markdown;

#[test]
fn peso_status_lista_faltantes_y_llegados_desde_disco() {
    // RaÃ­z de datos real en temp: dos ficheros de Fase 2 del perfil `anime`
    // presentes (con bytes reales) y el resto ausentes.
    let tmp = std::env::temp_dir().join("comfyctl_weights_test_disco");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(tmp.join("models/illegible")).unwrap_or_default();
    std::fs::create_dir_all(tmp.join("models/checkpoints")).unwrap_or_default();
    std::fs::create_dir_all(tmp.join("models/upscale_models")).unwrap_or_default();
    std::fs::write(
        tmp.join("models/checkpoints/Illustrious-XL-v2.0.safetensors"),
        vec![7_u8; 2048],
    )
    .unwrap_or_default();
    std::fs::write(
        tmp.join("models/upscale_models/4x-AnimeSharp.pth"),
        vec![7_u8; 1024],
    )
    .unwrap_or_default();

    let st = collect_weight_status(&tmp, "anime");
    std::fs::remove_dir_all(&tmp).ok();

    assert_eq!(st.source, "disco");
    assert!(st.expected > 0);
    let present: Vec<&str> = st.present.iter().map(|w| w.path.as_str()).collect();
    assert!(present.contains(&"checkpoints/Illustrious-XL-v2.0.safetensors"));
    assert!(present.contains(&"upscale_models/4x-AnimeSharp.pth"));
    assert_eq!(
        st.present.iter().map(|w| w.bytes).sum::<u64>(),
        2048 + 1024,
        "los bytes deben salir de los tamaÃ±os reales en disco"
    );
    // Un fichero presente NO aparece en la lista de faltantes; uno ausente sÃ­.
    assert!(!st.missing.iter().any(|m| m.contains("4x-AnimeSharp")));
    let missing: Vec<&str> = st.missing.iter().map(String::as_str).collect();
    assert!(missing.iter().any(|m| m.contains("checkpoints/")));
    // Los faltantes nombran el fichero y la fase, para saber quÃ© esperar.
    assert!(missing
        .iter()
        .all(|m| m.contains("phase2") || m.contains("phase1")));
}

#[test]
fn peso_status_sin_raiz_legible_declara_no_disponible() {
    // La ruta NO existe: no hay mediciÃ³n, y el diagnÃ³stico no puede fingir datos.
    let tmp = std::env::temp_dir().join("comfyctl_weights_test_sin_raiz");
    let _ = std::fs::remove_dir_all(&tmp);
    let st = collect_weight_status(&tmp, "all");
    std::fs::remove_dir_all(&tmp).ok();

    assert_eq!(st.source, "no disponible");
    assert!(
        st.present.is_empty() && st.missing.is_empty(),
        "sin mediciÃ³n no hay lista de presente/faltante: 0 datos inventados"
    );
}

#[test]
fn weight_status_serializa_campos_basicos() {
    let st = WeightStatus::default();
    let json = serde_json::to_string(&st).unwrap_or_default();
    assert!(json.contains("source"));
    assert!(json.contains("present"));
    assert!(json.contains("missing"));
    assert!(json.contains("expected"));
}

#[test]
fn report_seccion_11_pesos_nombra_lo_que_falta() {
    let tmp = std::env::temp_dir().join("comfyctl_weights_test_report");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(tmp.join("models/void")).unwrap_or_default();

    let st = collect_weight_status(&tmp, "anime");
    std::fs::remove_dir_all(&tmp).ok();

    assert!(st.expected > 0, "el perfil anime declara pesos esperados");
    assert!(!st.missing.is_empty());
    let mut api = example_api_payload(&st);
    let redactor = comfyctl::redact::SecretRedactor::new(None, None);
    let log_dir = std::env::temp_dir().join("comfyctl_weights_test_report_logs");
    let _ = std::fs::remove_dir_all(&log_dir);
    let _ = std::fs::create_dir_all(&log_dir);
    let md = build_report_markdown(&api, &example_heavy(), &redactor, &log_dir);
    let _ = std::fs::remove_dir_all(&log_dir);

    assert!(
        md.contains("## 11. Pesos del flujo pedido"),
        "secciÃ³n nueva: {md}"
    );
    let section11 = md
        .split("## 11. Pesos del flujo pedido")
        .nth(1)
        .unwrap_or_default();
    assert!(
        section11.contains("Faltan en disco"),
        "section11: {section11}"
    );
    // El fichero concreto ausente se nombra, no solo el recuento.
    let faltante = st.missing.first().cloned().unwrap_or_default();
    assert!(!faltante.is_empty());
    assert!(section11.contains(&faltante), "section11: {section11}");
    assert!(
        section11.contains("phase2") || section11.contains("phase1"),
        "section11: {section11}"
    );
    // El payload debe llevar el estado nuevo para /status/api.
    api.weights = st;
    let json = serde_json::to_string(&api).unwrap_or_default();
    assert!(json.contains("\"weights\""), "status/api: {json}");
}

#[test]
fn report_seccion_11_redacta_secretos_en_campos_de_pesos() {
    // La ruta falsificada lleva dos secretos: un token hf_ y una cabecera
    // Authorization. El texto final no puede mostrarlos.
    let tmp = std::env::temp_dir().join("comfyctl_weights_test_redaccion");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(tmp.join("models/void")).unwrap_or_default();

    let mut st = collect_weight_status(&tmp, "anime");
    std::fs::remove_dir_all(&tmp).ok();
    st.missing.insert(
        0,
        "checkpoints/hf_AbCdEfGhIjKlMnOp12 Authorization: Bearer abcd1234efgh5678".to_string(),
    );

    let api = example_api_payload(&st);
    let redactor = comfyctl::redact::SecretRedactor::new(None, None);
    let log_dir = std::env::temp_dir().join("comfyctl_weights_test_redaccion_logs");
    let _ = std::fs::remove_dir_all(&log_dir);
    let _ = std::fs::create_dir_all(&log_dir);
    let md = build_report_markdown(&api, &example_heavy(), &redactor, &log_dir);
    let _ = std::fs::remove_dir_all(&log_dir);

    assert!(!md.contains("hf_AbCdEfGhIjKlMnOp12"), "token hf_ filtrado");
    assert!(!md.contains("abcd1234efgh5678"), "token Bearer filtrado");
    assert!(md.contains("[REDACTED]"));
}

fn example_api_payload(weights: &WeightStatus) -> comfyctl::collect::StatusApiPayload {
    use comfyctl::collect::gpu::GpuStatus;
    use comfyctl::collect::phases::PhasesStatus;
    use comfyctl::collect::procfs::*;
    use std::collections::BTreeMap;

    comfyctl::collect::StatusApiPayload {
        generated_at: "2026-11-06T10:00:00Z".to_string(),
        image: ImageMeta {
            tag: "sha-fixtures".to_string(),
            built_at: "2026-11-06T09:00:00Z".to_string(),
            comfyui_sha: "4c62a84068a0".to_string(),
            pytorch: "2.9.1+cu128".to_string(),
            cuda: "12.8".to_string(),
            ubuntu: "24.04".to_string(),
        },
        instance: InstanceInfo {
            hostname: "vast-prueba".to_string(),
            uptime_s: 90,
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
            used_mb: 1024,
            available_mb: 1024,
            swap_used_mb: 0,
        },
        disk: DiskStatus {
            path: "/workspace/comfyui-data".to_string(),
            total_gb: 100.0,
            used_gb: 20.0,
            free_gb: 80.0,
        },
        comfyui: ComfyUiStatus {
            state: "running".to_string(),
            supervisor_state: "running".to_string(),
            pid: Some(4242),
            rss_mb: 512,
            uptime_s: 60,
            port_open: true,
            restarts: 0,
            healthy_resets: 0,
            max_restarts: 5,
            last_exit: None,
        },
        phases: {
            use comfyctl::collect::phases::Phase1Info;
            PhasesStatus {
                phase1: Phase1Info {
                    state: "not_required".to_string(),
                    done: 0,
                    total: 0,
                    failed: Vec::new(),
                    elapsed_s: 0,
                    ..Phase1Info::default()
                },
                phase2: comfyctl::collect::phases::Phase2Info {
                    state: "not_required".to_string(),
                    done: 0,
                    total: 0,
                    failed: Vec::new(),
                    current_file: None,
                    hf_pid: None,
                    hf_rss_mb: 0,
                    ..comfyctl::collect::phases::Phase2Info::default()
                },
            }
        },
        phase_banner: "ComfyUI listo".to_string(),
        services: ServicesStatus {
            nginx: NginxServiceStatus {
                running: false,
                workers_user: "none".to_string(),
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
        strict: None,
        weights: weights.clone(),
    }
}

fn example_heavy() -> comfyctl::report::HeavyCacheData {
    comfyctl::report::HeavyCacheData::default()
}
