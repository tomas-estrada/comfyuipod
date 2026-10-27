//! Tests de `collect::phases` sobre `tests/fixtures/download_models.log` (salida literal del
//! dry-run de `scripts/download-models.sh` con `fake-hf`, regenerable con el paso homónimo del
//! job `validate` de CI) y perfiles O-06 (§8.3, `R-01`, `R-08`, `S-02`).

use comfyctl::collect::models::{phase2_expected_count, EXPECTED_MODELS};
use comfyctl::collect::phases::parse_phases_from_lines;

#[test]
fn phases_parser_and_o06_profile_counts() {
    assert_eq!(EXPECTED_MODELS.len(), 26);
    assert_eq!(phase2_expected_count("all"), 14);
    assert_eq!(phase2_expected_count("general"), 10);
    assert_eq!(phase2_expected_count("realistic"), 10);
    assert_eq!(phase2_expected_count("anime"), 10);
    assert_eq!(phase2_expected_count("minimal"), 0);
    assert_eq!(phase2_expected_count("studio"), 8);

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
