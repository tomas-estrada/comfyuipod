//! Tests de `collect::phases` sobre `tests/fixtures/download_models.log` (salida literal del
//! dry-run de `scripts/download-models.sh` con `fake-hf`, regenerable con el paso homónimo del
//! job `validate` de CI) y perfiles O-06 (§8.3, `R-01`, `R-08`, `S-02`).

use comfyctl::collect::models::phase2_expected_count;
use comfyctl::collect::models::EXPECTED_MODELS;
use comfyctl::collect::phases::{add_disk_truth, parse_phases_from_lines, PhasesStatus};

const STATUS_HTML: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/status_html.html"));

#[test]
fn banner_no_inventa_porcentaje_cuando_no_hay_plan() {
    // status_html.html: el progreso de Fase 2 con total 0 no puede fallar a 100%.
    assert!(STATUS_HTML
        .contains("p2.total > 0 ? Math.min(100, Math.round((p2.done / p2.total) * 100)) : 0"));
    assert!(
        !STATUS_HTML.contains(") : 100]"),
        "no se inventa 100 sin denominador: {STATUS_HTML}"
    );
}

#[test]
#[allow(clippy::cast_sign_loss, clippy::cast_possible_truncation)]
fn bytes_de_fase_salen_del_log_y_lo_no_medible_es_no_disponible() {
    // Bytes del plan (INICIANDO FASE 1 «(2.79 GiB)») y de los OK «(4.0K)».
    let log_lines: Vec<String> = vec![
        "[2026-10-19 11:00:00] [MODELS] INICIANDO FASE 1 (2.79 GiB): ReActor, InsightFace, CodeFormer, VAE, Upscalers".to_string(),
        "[2026-10-19 11:00:00] [MODELS] PLAN FASE 1: 12 archivos".to_string(),
        "[2026-10-19 11:00:01] [MODELS] INICIO: sdxl.vae.safetensors (madebyollin/sdxl-vae-fp16-fix/sdxl.vae.safetensors)".to_string(),
        "[2026-10-19 11:00:03] [MODELS] OK: /workspace/comfyui-data/models/vae/sdxl.vae.safetensors (4.0K)".to_string(),
        "[2026-10-19 11:00:04] [MODELS] OK: /workspace/comfyui-data/models/upscale_models/4x-UltraSharp.pth (1.0M)".to_string(),
        "[2026-10-19 11:00:06] [MODELS] FASE 1 COMPLETADA.".to_string(),
        "[2026-10-19 11:00:07] [MODELS] INICIANDO FASE 2 (Segundo Plano — Perfil: all)".to_string(),
        "[2026-10-19 11:00:07] [MODELS] PLAN FASE 2: 14 archivos (all)".to_string(),
    ];
    let parsed = parse_phases_from_lines(&log_lines, false, "all", None);
    // Fase 1 declara 2.79 GiB de plan (medido por download-models.sh) y los dos
    // OK suman sus bytes reales de log.
    let plan_gib: u64 = (2.79_f64 * (1024.0_f64 * 1024.0_f64 * 1024.0_f64)).floor() as u64;
    assert_eq!(parsed.phase1.bytes_total, Some(plan_gib));
    assert_eq!(parsed.phase1.bytes_done, 4 * 1024 + 1024 * 1024);
    // La Fase 2 no declara tamaño en su banner: `no disponible`, nunca 0 «fijo».
    assert_eq!(parsed.phase2.bytes_total, None);
    assert_eq!(parsed.phase2.bytes_done, 0);
}

#[test]
fn bytes_no_disponible_en_arranque_sin_plan() {
    // El arranque (--bootstrap) no declara bytes en el banner ni en el plan.
    let parsed = parse_phases_from_lines(&bootstrap_nothing(), false, "all", None);
    assert_eq!(parsed.phase1.bytes_total, None);
    assert_eq!(parsed.phase2.bytes_total, None);
    let st = PhasesStatus::default();
    assert!(st.phase1.missing.is_empty());
}

fn bootstrap_nothing() -> Vec<String> {
    vec![
        "[2026-11-05 09:00:00] [MODELS] INICIANDO ARRANQUE (solo bootstrap imprescindible)"
            .to_string(),
        "[2026-11-05 09:00:00] [MODELS] PLAN ARRANQUE: 0 archivos bootstrap.".to_string(),
        "[2026-11-05 09:00:00] [MODELS] FASE ARRANQUE COMPLETADA.".to_string(),
    ]
}

#[test]
fn la_verdad_del_disco_pone_la_lista_de_lo_que_falta_en_cada_fase() {
    use comfyctl::collect::models::collect_weight_status;
    // Disco real en temp: el checkpoint de Fase 2 llegó; el de Fase 1 no.
    let tmp = std::env::temp_dir().join("comfyctl_phases_missing_disco");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(tmp.join("models/checkpoints")).unwrap_or_default();
    std::fs::write(
        tmp.join("models/checkpoints/flux1-dev-fp8.safetensors"),
        vec![7_u8; 512],
    )
    .unwrap_or_default();

    let weights = collect_weight_status(&tmp, "all");
    assert!(
        !weights.missing.iter().any(|m| m.contains("flux1-dev-fp8")),
        "flux llegó y no puede figurar como ausente: {}",
        weights.missing.join(", ")
    );
    assert!(weights.missing.iter().any(|m| m.contains("(phase2)")));
    let mut phases = parse_phases_from_lines(&[], false, "all", None);
    add_disk_truth(&mut phases, &weights);
    std::fs::remove_dir_all(&tmp).ok();

    // El log está vacío (diene `pending`), pero la verdad del disco dice qué pesos
    // de cada fase faltan: eso publicará el informe, no el estado del log.
    assert!(phases.phase1.missing.iter().all(|m| m.contains("(phase1)")));
    assert!(!phases.phase1.missing.is_empty());
    assert!(!phases.phase2.missing.is_empty());
    assert!(phases.phase2.missing.iter().all(|m| m.contains("(phase2)")));
}

#[test]
fn phases_parser_and_o06_profile_counts() {
    assert_eq!(EXPECTED_MODELS.len(), 26);
    assert_eq!(phase2_expected_count("all"), 14);
    assert_eq!(phase2_expected_count("general"), 10);
    assert_eq!(phase2_expected_count("realistic"), 10);
    assert_eq!(phase2_expected_count("anime"), 10);
    assert_eq!(phase2_expected_count("minimal"), 0);
    assert_eq!(phase2_expected_count("studio"), 8);
    // M3-D: `unreal` es la familia studio + pipeline 3D, así que su Fase 2 es la de studio.
    assert_eq!(
        phase2_expected_count("unreal"),
        phase2_expected_count("studio")
    );
    assert_eq!(phase2_expected_count("unreal"), 8);

    // 1. Sin log aún (O-06): phase2.total debe reflejar el perfil activo antes de que empiece Fase 2
    let empty_lines: Vec<String> = Vec::new();
    let p_anime_pending = parse_phases_from_lines(&empty_lines, false, "anime", None);
    assert_eq!(p_anime_pending.phase1.state, "pending");
    assert_eq!(p_anime_pending.phase1.total, 12);
    assert!(p_anime_pending.phase1.failed.is_empty());
    assert_eq!(p_anime_pending.phase2.state, "pending");
    assert_eq!(p_anime_pending.phase2.total, 10);

    let p_minimal = parse_phases_from_lines(&empty_lines, false, "minimal", None);
    assert_eq!(p_minimal.phase2.state, "skipped");
    assert_eq!(p_minimal.phase2.total, 0);

    // 2. Con SKIP_MODEL_DOWNLOAD=1
    let p_skipped = parse_phases_from_lines(&empty_lines, true, "all", None);
    assert_eq!(p_skipped.phase1.state, "skipped");
    assert_eq!(p_skipped.phase2.state, "skipped");
    assert_eq!(p_skipped.phase2.total, 14);

    // 3. Fixture real del dry-run (perfil general, --phase1 + --phase2 con re-chequeo idempotente
    //    de Fase 1 dentro de --phase2): 12 ficheros Fase 1 (1 INICIO/OK + 1 "OK: InsightFace
    //    buffalo_l listo en" excluido + 10 INICIO/OK en la 1ª pasada; 12 SKIP en la 2ª) y 10 en Fase 2.
    let fixture = include_str!("fixtures/download_models.log");
    let lines: Vec<String> = fixture.lines().map(ToString::to_string).collect();
    let parsed = parse_phases_from_lines(&lines, false, "general", Some((4242, 128)));
    assert_eq!(parsed.phase1.state, "done");
    assert_eq!(parsed.phase1.done, 12);
    assert_eq!(parsed.phase1.total, 12);
    assert!(parsed.phase1.failed.is_empty());
    // 1ª pasada de Fase 1: de [17:59:55] a [18:00:02] = 7s (la 2ª pasada, 1s, no lo reduce).
    assert_eq!(parsed.phase1.elapsed_s, 7);
    assert_eq!(parsed.phase2.state, "done");
    assert_eq!(parsed.phase2.done, 10);
    assert_eq!(parsed.phase2.total, 10);
    assert!(parsed.phase2.failed.is_empty());
    assert_eq!(parsed.phase2.current_file, None);
    assert_eq!(parsed.phase2.hf_pid, Some(4242));
    assert_eq!(parsed.phase2.hf_rss_mb, 128);
}

#[test]
fn phases_parser_handles_failures_and_minimal_skip_in_real_format() {
    // Trazas literales del formato de download-models.sh: un fallo de Fase 1 y perfil minimal.
    let log_lines: Vec<String> = vec![
        "[2026-10-19 11:00:00] [MODELS] INICIANDO FASE 1 (2.79 GiB): ReActor, InsightFace, CodeFormer, VAE, Upscalers".to_string(),
        "[2026-10-19 11:00:00] [MODELS] PLAN FASE 1: 12 archivos".to_string(),
        "[2026-10-19 11:00:01] [MODELS] INICIO: sdxl.vae.safetensors (madebyollin/sdxl-vae-fp16-fix/sdxl.vae.safetensors)".to_string(),
        "[2026-10-19 11:00:05] [MODELS] ERROR: Falló la descarga de sdxl.vae.safetensors (madebyollin/sdxl-vae-fp16-fix/sdxl.vae.safetensors) tras 3 intentos.".to_string(),
        "[2026-10-19 11:00:06] [MODELS] FASE 1 COMPLETADA.".to_string(),
        "[2026-10-19 11:00:07] [MODELS] INICIANDO FASE 2 (Segundo Plano — Perfil: minimal)".to_string(),
        "[2026-10-19 11:00:07] [MODELS] PLAN FASE 2: 0 archivos (minimal)".to_string(),
        "[2026-10-19 11:00:07] [MODELS] MODEL_PROFILE=minimal activo: omitiendo Fase 2 de checkpoints pesados.".to_string(),
    ];
    let parsed = parse_phases_from_lines(&log_lines, false, "minimal", None);
    assert_eq!(parsed.phase1.state, "done");
    assert_eq!(parsed.phase1.done, 0);
    assert_eq!(
        parsed.phase1.failed,
        vec!["sdxl.vae.safetensors".to_string()]
    );
    assert_eq!(parsed.phase1.elapsed_s, 6);
    assert_eq!(parsed.phase2.state, "skipped");
    assert_eq!(parsed.phase2.total, 0);
    assert_eq!(parsed.phase2.current_file, None);
}
