//! Tests de matriz 17×17 de `triage_patterns.json` y regresiones de falsos positivos (O-08a, O-08b, O-08c).

use comfyctl::triage::TriageEngine;
use std::collections::BTreeMap;
use std::path::Path;

#[test]
fn triage_17x17_matrix_and_o08_regressions() {
    let engine = TriageEngine::load(Some(Path::new("../scripts/triage_patterns.json")));
    assert_eq!(engine.patterns.len(), 17, "Se esperaban 17 patrones");

    let cases: [(&str, &str, &str); 17] = [
        (
            "oom-rc137",
            "supervisor",
            "[2026-10-19 10:00:00] [SUPERVISOR] Caída de ComfyUI detectada (rc=137)",
        ),
        (
            "hostbuffer",
            "comfyui",
            "RuntimeError: HostBuffer allocation failed: cudaErrorInvalidValue",
        ),
        (
            "sm120",
            "comfyui",
            "RuntimeError: CUDA error: no kernel image is available for execution on the device (sm_120)",
        ),
        (
            "disk-full",
            "api",
            "disk.free_gb < 10 (libre actual: 7.2 GB en /workspace/comfyui-data)",
        ),
        (
            "cuda-oom",
            "comfyui",
            "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
        ),
        (
            "import-failed",
            "comfyui",
            "   0.2 seconds (IMPORT FAILED): /opt/ComfyUI/custom_nodes/ComfyUI-ReActor",
        ),
        (
            "frontend-mismatch",
            "comfyui",
            "WARNING: comfyui-frontend-package is outdated and required by core",
        ),
        (
            "manager-500",
            "comfyui",
            "aiohttp.web: Server got itself in trouble while handling /api/manager/queue",
        ),
        (
            "nginx-502",
            "nginx-access",
            "127.0.0.1 - admin [19/Oct/2026:12:00:00 +0000] \"GET / HTTP/1.1\" 502 150",
        ),
        (
            "hf-503",
            "download",
            "HTTP 503 Service Unavailable while fetching model",
        ),
        (
            "hf-stuck",
            "processes",
            "hf_stuck_over_30m: pid=4321 sin avance hace 1900s",
        ),
        (
            "nvenc-missing",
            "diag",
            "[DIAG][WARN] nvenc: Cannot load libnvidia-encode.so.1",
        ),
        (
            "ort-cpu",
            "diag",
            "[DIAG][WARN] ort: providers=['CPUExecutionProvider'] (CUDAExecutionProvider ausente)",
        ),
        (
            "value-not-in-list",
            "comfyui",
            "Prompt outputs failed validation: Value not in list: ckpt_name",
        ),
        (
            "nginx-429",
            "nginx-access",
            "127.0.0.1 - - [19/Oct/2026:12:00:00 +0000] \"GET / HTTP/1.1\" 429 162",
        ),
        (
            "nginx-413",
            "nginx-access",
            "127.0.0.1 - admin [19/Oct/2026:12:00:00 +0000] \"POST /upload/image HTTP/1.1\" 413 192",
        ),
        (
            "hf-timeout",
            "download",
            "[2026-10-19 12:15:00] ERROR: Falló la descarga de flux1-dev-fp8.safetensors tras 3 intentos",
        ),
    ];

    // 1. Verificar que cada caso sintético dispara únicamente su patrón esperado (matriz 17x17)
    for (expected_id, src, sample_line) in cases {
        let mut buffers: BTreeMap<String, Vec<String>> = BTreeMap::new();
        buffers.insert(src.to_string(), vec![sample_line.to_string()]);
        let (hits, _) = engine.evaluate(&buffers, false);
        assert_eq!(
            hits.len(),
            1,
            "El caso para {expected_id} disparó {hits:?} en lugar de exactamente 1 patrón"
        );
        assert_eq!(
            hits.first().map(|h| h.pattern_id.as_str()),
            Some(expected_id)
        );
    }

    // 2. O-08a: nginx-502 se suprime cuando comfy_running == true
    let mut buf_502: BTreeMap<String, Vec<String>> = BTreeMap::new();
    buf_502.insert(
        "nginx-access".to_string(),
        vec!["127.0.0.1 - admin \"GET / HTTP/1.1\" 503 1024".to_string()],
    );
    let (hits_when_running, _) = engine.evaluate(&buf_502, true);
    assert!(
        hits_when_running.is_empty(),
        "O-08a: nginx-502 debe suprimirse cuando ComfyUI ya está running"
    );

    // 3. O-08b: Tamaños como "503M" o hashes con "503" no disparan hf-503
    let mut buf_size: BTreeMap<String, Vec<String>> = BTreeMap::new();
    buf_size.insert(
        "download".to_string(),
        vec!["[2026-10-19 12:00:00] OK: modelo_503M_sha503abc.safetensors (503M)".to_string()],
    );
    let (hits_size, _) = engine.evaluate(&buf_size, true);
    assert!(
        hits_size.is_empty(),
        "O-08b: '503M' no debe disparar hf-503: {hits_size:?}"
    );

    // 4. O-08c: "error" en minúsculas dentro de nombre de modelo no entra en errors_recent
    let crash_fixture = include_str!("fixtures/comfyui_crash.log");
    let mut buf_err: BTreeMap<String, Vec<String>> = BTreeMap::new();
    buf_err.insert(
        "comfyui".to_string(),
        crash_fixture.lines().map(ToString::to_string).collect(),
    );
    let (_, recent_errors) = engine.evaluate(&buf_err, false);
    assert!(
        !recent_errors
            .iter()
            .any(|e| e.line.contains("error_diffusion_v1")),
        "O-08c: nombres de archivo con 'error' en minúscula no deben entrar en errors_recent"
    );
    assert!(
        recent_errors
            .iter()
            .any(|e| e.line.contains("OutOfMemoryError")),
        "OutOfMemoryError sí debe capturarse en errors_recent"
    );
}
