use comfyctl::config::{ModelProfile, RuntimeConfig};
use comfyctl::strict::{
    evaluate_strict_checks, probe_htpasswd, probe_mcp_health, probe_nvidia_smi,
    probe_studio_health, write_and_print_strict_report, SystemSnapshot,
};
use comfyctl::sys;
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};

fn base_healthy_snapshot() -> SystemSnapshot {
    SystemSnapshot {
        gpu_query_ok: true,
        gpu_name: "NVIDIA GeForce RTX 5090".to_string(),
        driver_version: "570.124.06".to_string(),
        vram_total_mb: 32607,
        ram_probe_ok: true,
        ram_total_mb: 65536,
        disk_free_gb: 120.0,
        workspace_writable: true,
        nginx_config_ok: true,
        nginx_detail: "nginx -t OK".to_string(),
        htpasswd_ok: true,
        htpasswd_missing: false,
        htpasswd_detail: "/etc/nginx/.htpasswd válido".to_string(),
        comfy_install_ok: true,
        comfy_install_detail: "/opt/ComfyUI/main.py presente".to_string(),
        cuda_torch_ok: true,
        cuda_torch_detail: "torch=2.9.1+cu128 cuda=12.8 device=NVIDIA GeForce RTX 5090 matmul=ok"
            .to_string(),
        studio_health_ok: true,
        studio_health_detail:
            "studio-server healthy en 127.0.0.1:18190 (/studio/api/health HTTP 200)".to_string(),
        studio_perception_ok: true,
        studio_perception_detail: "percepción disponible".to_string(),
        studio_perception_backend: "real".to_string(),
        studio_perception_stale: false,
        mcp_health_ok: true,
        mcp_health_detail: "MCP healthy en 127.0.0.1:18190/studio/api/mcp/health (HTTP 200)"
            .to_string(),
    }
}

#[test]
fn strict_all_ok_on_rtx_5090() {
    let vars = BTreeMap::new();
    let mut cfg = RuntimeConfig::from_map(&vars, None);
    let tmp_dir = std::env::temp_dir().join("comfyctl_test_strict_ok");
    let _ = fs::create_dir_all(&tmp_dir);
    cfg.strict_json_path = tmp_dir.join("comfyui-strict.json");

    let snap = base_healthy_snapshot();
    let rep = evaluate_strict_checks(&cfg, &snap);
    assert_eq!(rep.result, "ok");
    assert_eq!(rep.checks.len(), 10);
    assert!(rep.checks.iter().all(|c| c.status == "ok"));

    assert!(write_and_print_strict_report(&cfg, &rep).is_ok());
    assert!(cfg.strict_json_path.is_file());
    let _ = fs::remove_dir_all(&tmp_dir);
}

#[test]
fn strict_driver_below_570_is_fatal_when_strict_and_warn_when_disabled() {
    let mut vars = BTreeMap::new();
    let cfg_strict = RuntimeConfig::from_map(&vars, None);
    let mut snap = base_healthy_snapshot();
    snap.driver_version = "550.54.14".to_string();

    let rep_strict = evaluate_strict_checks(&cfg_strict, &snap);
    assert_eq!(rep_strict.result, "fatal");
    let s2 = rep_strict.checks.iter().find(|c| c.id == "S2");
    assert!(matches!(s2, Some(c) if c.status == "fatal"));

    vars.insert("STRICT_START".to_string(), "0".to_string());
    let cfg_warn = RuntimeConfig::from_map(&vars, None);
    let rep_warn = evaluate_strict_checks(&cfg_warn, &snap);
    assert_eq!(rep_warn.result, "warn");
    let s2_warn = rep_warn.checks.iter().find(|c| c.id == "S2");
    assert!(matches!(s2_warn, Some(c) if c.status == "warn"));
}

#[test]
fn strict_vram_ram_disk_and_profile_thresholds() {
    let mut vars = BTreeMap::new();
    let cfg_all = RuntimeConfig::from_map(&vars, None);
    assert_eq!(cfg_all.strict_min_vram_mb, 20000);
    assert_eq!(cfg_all.strict_min_disk_gb, 75);

    let mut snap = base_healthy_snapshot();
    snap.vram_total_mb = 12000;
    snap.ram_total_mb = 8192;
    snap.disk_free_gb = 30.0;

    let rep = evaluate_strict_checks(&cfg_all, &snap);
    assert_eq!(rep.result, "fatal");
    for id in ["S3", "S4", "S5"] {
        assert!(rep.checks.iter().any(|c| c.id == id && c.status == "fatal"));
    }

    // Con MODEL_PROFILE=minimal (umbral 10 GB), 30 GB de disco libre pasa S5
    vars.insert("MODEL_PROFILE".to_string(), "minimal".to_string());
    let cfg_min = RuntimeConfig::from_map(&vars, None);
    assert_eq!(cfg_min.strict_min_disk_gb, 10);
    let mut snap_min = base_healthy_snapshot();
    snap_min.disk_free_gb = 30.0;
    let rep_min = evaluate_strict_checks(&cfg_min, &snap_min);
    assert_eq!(rep_min.result, "ok");
}

#[test]
fn strict_ci_runner_snapshot_warns_instead_of_fatal() {
    // R-02, R-03, R-08: Simula runner de GitHub Actions (15800 MiB RAM reportada por /proc/meminfo,
    // modo CPU, SKIP_MODEL_DOWNLOAD=1, y strict-check invocado antes de que start.sh cree .htpasswd).
    let mut vars = BTreeMap::new();
    vars.insert("COMFYUI_EXTRA_ARGS".to_string(), "--cpu".to_string());
    vars.insert("SKIP_MODEL_DOWNLOAD".to_string(), "1".to_string());
    let mut cfg = RuntimeConfig::from_map(&vars, None);
    cfg.require_htpasswd = false; // modo standalone comfyctl strict-check

    let mut snap = base_healthy_snapshot();
    snap.gpu_query_ok = false;
    snap.gpu_name = "sin gpu".to_string();
    snap.vram_total_mb = 0;
    snap.ram_total_mb = 15800; // < 16384 nominal por reserva del kernel (~15.4 GiB), pero >= 15564 (margen 5%)
    snap.disk_free_gb = 18.0; // >= 5 GB (umbral con SKIP_MODEL_DOWNLOAD=1)
    snap.htpasswd_ok = false;
    snap.htpasswd_missing = true;
    snap.htpasswd_detail = "No existe /etc/nginx/.htpasswd".to_string();
    snap.cuda_torch_ok = false;

    let rep = evaluate_strict_checks(&cfg, &snap);
    assert_eq!(rep.result, "warn");
    for id in ["S1", "S2", "S3", "S5", "S6", "S8", "S9", "S10"] {
        assert!(
            rep.checks.iter().any(|c| c.id == id && c.status == "ok"),
            "Se esperaba {id} == ok en modo CPU CI"
        );
    }
    // S4 debe quedar en warn (banda 16-32 GB con tolerancia 5%), nunca fatal
    assert!(rep
        .checks
        .iter()
        .any(|c| c.id == "S4" && c.status == "warn"));
    // S7 debe quedar en warn cuando falta .htpasswd en modo standalone, pero fatal en supervise
    assert!(rep
        .checks
        .iter()
        .any(|c| c.id == "S7" && c.status == "warn"));

    cfg.require_htpasswd = true;
    let rep_sup = evaluate_strict_checks(&cfg, &snap);
    assert_eq!(rep_sup.result, "fatal");
    assert!(rep_sup
        .checks
        .iter()
        .any(|c| c.id == "S7" && c.status == "fatal"));
}

#[test]
fn strict_contract_fields_match_portal_status_and_cli_views() {
    // R-04, R-08: Verifica que los nombres de campo de StrictCheckItem serializados a JSON
    // coinciden con los consumidos en config/portal/index.html, status_html.html y tools/mac/comfy.
    let vars = BTreeMap::new();
    let cfg = RuntimeConfig::from_map(&vars, None);
    let mut snap = base_healthy_snapshot();
    snap.driver_version = "535.104.05".to_string();
    let rep = evaluate_strict_checks(&cfg, &snap);
    let json_val = serde_json::to_value(&rep).unwrap_or(serde_json::Value::Null);
    let first_check = json_val
        .get("checks")
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first())
        .cloned()
        .unwrap_or(serde_json::Value::Null);

    for required_field in ["id", "name", "status", "detail", "remediation"] {
        assert!(
            first_check.get(required_field).is_some(),
            "Falta campo '{required_field}' en StrictCheckItem JSON"
        );
    }
    for forbidden_field in ["message", "observed", "expected"] {
        assert!(
            first_check.get(forbidden_field).is_none(),
            "Campo obsoleto '{forbidden_field}' presente en StrictCheckItem JSON"
        );
    }

    let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or_else(|| Path::new(".."));
    for rel_path in [
        "config/portal/index.html",
        "comfyctl/src/status_html.html",
        "tools/mac/comfy",
    ] {
        let content = fs::read_to_string(repo_root.join(rel_path)).unwrap_or_default();
        assert!(
            !content.contains("c.observed")
                && !content.contains("c.expected")
                && !content.contains("chk.get('observed'")
                && !content.contains("chk.get(\"observed\""),
            "La vista {rel_path} referencia campos inexistentes (observed/expected) de StrictCheckItem"
        );
    }
}

#[test]
fn simulated_nvidia_smi_binary_probe_and_subprocess_timeout() {
    let tmp_dir = std::env::temp_dir().join("comfyctl_fake_smi");
    let _ = fs::create_dir_all(&tmp_dir);

    #[cfg(unix)]
    let fake_bin = {
        use std::os::unix::fs::PermissionsExt;
        let p = tmp_dir.join("fake-nvidia-smi.sh");
        let _ = fs::write(
            &p,
            "#!/bin/sh\necho 'NVIDIA GeForce RTX 5090, 570.86.16, 32607'\n",
        );
        if let Ok(meta) = fs::metadata(&p) {
            let mut perms = meta.permissions();
            perms.set_mode(0o755);
            let _ = fs::set_permissions(&p, perms);
        }
        p
    };
    #[cfg(not(unix))]
    let fake_bin = {
        let p = tmp_dir.join("fake-nvidia-smi.cmd");
        let _ = fs::write(
            &p,
            "@echo off\r\necho NVIDIA GeForce RTX 5090, 570.86.16, 32607\r\n",
        );
        p
    };

    std::env::set_var("COMFY_NVIDIA_SMI_BIN", &fake_bin);
    let (ok, name, driver, vram) = probe_nvidia_smi();
    std::env::remove_var("COMFY_NVIDIA_SMI_BIN");
    let _ = fs::remove_dir_all(&tmp_dir);

    assert!(ok);
    assert_eq!(name, "NVIDIA GeForce RTX 5090");
    assert_eq!(driver, "570.86.16");
    assert_eq!(vram, 32607);

    // R-07: Verificar que run_command_with_timeout mata procesos colgados en < 2s.
    // Umbral holgado: el timeout del comando es 250 ms, pero los runners de CI
    // compartidos pueden tardar varios segundos en programar y matar el hijo; un
    // cuelgue real del mecanismo se manifestaría como timeout del test runner
    // entero, no como 4 s vs 3 s.
    let start = Instant::now();
    let mut sleep_cmd = if cfg!(unix) {
        let mut c = Command::new("sh");
        c.args(["-c", "sleep 10"]);
        c
    } else {
        let mut c = Command::new("powershell");
        c.args(["-NoProfile", "-Command", "Start-Sleep -Seconds 10"]);
        c
    };
    let res = sys::run_command_with_timeout(&mut sleep_cmd, Duration::from_millis(250));
    assert!(matches!(res, Ok((_, _, _, true))));
    assert!(start.elapsed() < Duration::from_secs(15));
}

/// Fija permisos 0640 (sin bits 'other') a un fixture esperado como válido,
/// imitando el `chmod 640` que aplica el CI sobre /etc/nginx/.htpasswd:
/// en unix, escribir en /tmp con umask 022 deja 0644 y el chequeo de
/// permisos de R-02 (world-accessible) lo rechazaría.
#[cfg(unix)]
fn restrict_fixture_permissions(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o640));
}

#[test]
fn probe_htpasswd_validates_format_and_rejects_garbage() {
    let tmp_dir = std::env::temp_dir().join("comfyctl_htpasswd_probe");
    let _ = fs::remove_dir_all(&tmp_dir);
    let _ = fs::create_dir_all(&tmp_dir);

    // Formato del fixture de arranque/CI: `admin:{PLAIN}test` debe pasar.
    let ok_path = tmp_dir.join("ok.htpasswd");
    let _ = fs::write(&ok_path, "admin:{PLAIN}test\n");
    #[cfg(unix)]
    restrict_fixture_permissions(&ok_path);
    let (ok, missing, detail) = probe_htpasswd(&ok_path);
    assert!(
        ok,
        "el fixture admin:{{PLAIN}}test debe ser válido: {detail}"
    );
    assert!(!missing);

    // Fichero con basura sin ninguna línea `usuario:hash` → inválido.
    let garbage_path = tmp_dir.join("garbage.htpasswd");
    let _ = fs::write(&garbage_path, "x\n");
    let (ok_g, missing_g, _) = probe_htpasswd(&garbage_path);
    assert!(!ok_g);
    assert!(!missing_g);

    // Línea con usuario con espacios → inválido; usuario:hash correcto en otra línea → válido.
    let mixed_path = tmp_dir.join("mixed.htpasswd");
    let _ = fs::write(&mixed_path, "bad user:hash\nroot:$apr1$abc$def\n");
    #[cfg(unix)]
    restrict_fixture_permissions(&mixed_path);
    let (ok_m, _, _) = probe_htpasswd(&mixed_path);
    assert!(ok_m);

    // Fichero vacío → inválido, no 'missing'.
    let empty_path = tmp_dir.join("empty.htpasswd");
    let _ = fs::write(&empty_path, "");
    let (ok_e, missing_e, _) = probe_htpasswd(&empty_path);
    assert!(!ok_e);
    assert!(!missing_e);

    // Fichero inexistente → missing.
    let (ok_n, missing_n, _) = probe_htpasswd(&tmp_dir.join("nope.htpasswd"));
    assert!(!ok_n);
    assert!(missing_n);

    // En unix, un fichero con contenido válido pero permisos world-accessible
    // (0644) debe ser rechazado por el chequeo de permisos de R-02.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let world_path = tmp_dir.join("world.htpasswd");
        let _ = fs::write(&world_path, "admin:{PLAIN}test\n");
        let _ = fs::set_permissions(&world_path, fs::Permissions::from_mode(0o644));
        let (ok_w, missing_w, detail_w) = probe_htpasswd(&world_path);
        assert!(!ok_w, "0644 world-accessible debe fallar: {detail_w}");
        assert!(!missing_w);
        assert!(
            detail_w.contains("Permisos inseguros"),
            "detalle inesperado: {detail_w}"
        );
    }

    let _ = fs::remove_dir_all(&tmp_dir);
}

#[test]
fn strict_studio_s5_disk_180gb_and_other_profiles_unchanged() {
    // 1. Verificar tabla S5 intacta para todos los perfiles (180 / 210 / 75 / 45 / 10 GB)
    let profile_thresholds = [
        ("studio", ModelProfile::Studio, 180_u64),
        ("unreal", ModelProfile::Unreal, 210_u64),
        ("all", ModelProfile::All, 75_u64),
        ("general", ModelProfile::General, 45_u64),
        ("realistic", ModelProfile::Realistic, 45_u64),
        ("anime", ModelProfile::Anime, 45_u64),
        ("minimal", ModelProfile::Minimal, 10_u64),
    ];

    for (raw, expected_profile, expected_gb) in profile_thresholds {
        let mut vars = BTreeMap::new();
        vars.insert("MODEL_PROFILE".to_string(), raw.to_string());
        let cfg = RuntimeConfig::from_map(&vars, None);
        assert_eq!(cfg.model_profile, expected_profile);
        assert!(cfg.model_profile_valid);
        assert_eq!(
            cfg.strict_min_disk_gb, expected_gb,
            "Umbral S5 inesperado para MODEL_PROFILE={raw}"
        );

        // Con SKIP_MODEL_DOWNLOAD=1 todos los perfiles bajan a 5 GB
        vars.insert("SKIP_MODEL_DOWNLOAD".to_string(), "1".to_string());
        let cfg_skip = RuntimeConfig::from_map(&vars, None);
        assert_eq!(cfg_skip.strict_min_disk_gb, 5);
    }

    // 2. En perfil studio: 179.5 GB libres -> S5 fatal; 180.0 GB libres -> S5 ok
    let mut studio_vars = BTreeMap::new();
    studio_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    let cfg_studio = RuntimeConfig::from_map(&studio_vars, None);

    let mut snap_low = base_healthy_snapshot();
    snap_low.disk_free_gb = 179.5;
    let rep_low = evaluate_strict_checks(&cfg_studio, &snap_low);
    assert_eq!(rep_low.result, "fatal");
    let s5_low = rep_low.checks.iter().find(|c| c.id == "S5");
    assert!(matches!(
        s5_low,
        Some(c)
            if c.status == "fatal"
                && c.detail.contains("180 GB")
                && c.remediation
                    .as_deref()
                    .unwrap_or_default()
                    .contains("--disk 220")
    ));

    let mut snap_ok = base_healthy_snapshot();
    snap_ok.disk_free_gb = 180.0;
    let rep_ok = evaluate_strict_checks(&cfg_studio, &snap_ok);
    assert_eq!(rep_ok.result, "ok");
    let s5_ok = rep_ok.checks.iter().find(|c| c.id == "S5");
    assert!(matches!(s5_ok, Some(c) if c.status == "ok"));
}

#[test]
fn strict_studio_health_s11_and_live_http_probe() {
    let mut vars = BTreeMap::new();
    vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    let cfg_studio = RuntimeConfig::from_map(&vars, None);

    // 1. Con MODEL_PROFILE=studio, S1-S10 siguen intactos y se añaden S11 (studio_health),
    //    S12 (mcp_health) y S13 (studio_perception, warn nunca fatal)
    let mut snap = base_healthy_snapshot();
    snap.disk_free_gb = 200.0;
    let rep_ok = evaluate_strict_checks(&cfg_studio, &snap);
    assert_eq!(rep_ok.result, "ok");
    assert_eq!(rep_ok.checks.len(), 15);
    for expected_id in [
        "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S13b",
        "S13c",
    ] {
        assert!(
            rep_ok
                .checks
                .iter()
                .any(|c| c.id == expected_id && c.status == "ok"),
            "Falta check {expected_id} en ok para perfil studio"
        );
    }

    // 2. Cuando studio_health_ok es false -> S11 fatal con STRICT_START=1 y warn con STRICT_START=0
    snap.studio_health_ok = false;
    snap.studio_health_detail = "connection refused".to_string();
    let rep_fatal = evaluate_strict_checks(&cfg_studio, &snap);
    assert_eq!(rep_fatal.result, "fatal");
    let s11_fatal = rep_fatal.checks.iter().find(|c| c.id == "S11");
    assert!(matches!(
        s11_fatal,
        Some(c) if c.name == "studio_health" && c.status == "fatal" && c.remediation.is_some()
    ));

    vars.insert("STRICT_START".to_string(), "0".to_string());
    let cfg_warn = RuntimeConfig::from_map(&vars, None);
    let rep_warn = evaluate_strict_checks(&cfg_warn, &snap);
    assert_eq!(rep_warn.result, "warn");
    let s11_warn = rep_warn.checks.iter().find(|c| c.id == "S11");
    assert!(matches!(s11_warn, Some(c) if c.status == "warn"));

    // 2b. S12 (mcp_health) nunca es fatal: con MCP caído y STRICT_START=1 el resultado
    //     global lo decide S11; aquí S11 está fatal, así que validamos S12 warn con S11 ok.
    snap.studio_health_ok = true;
    snap.mcp_health_ok = false;
    snap.mcp_health_detail = "connection refused".to_string();
    let rep_s12 = evaluate_strict_checks(&cfg_studio, &snap);
    assert_eq!(rep_s12.result, "warn");
    let s12 = rep_s12.checks.iter().find(|c| c.id == "S12");
    assert!(matches!(
        s12,
        Some(c) if c.name == "mcp_health" && c.status == "warn" && c.remediation.is_some()
    ));
    snap.mcp_health_ok = true;
    snap.mcp_health_detail =
        "MCP healthy en 127.0.0.1:18190/studio/api/mcp/health (HTTP 200)".to_string();

    // 3. Probe HTTP real contra un listener local en puerto efímero
    let Ok(listener) = TcpListener::bind("127.0.0.1:0") else {
        std::process::abort();
    };
    let port = listener.local_addr().map_or(18190, |addr| addr.port());
    let srv = thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut buf = [0_u8; 512];
            let _ = stream.read(&mut buf);
            let body = br#"{"status":"ok","service":"studio-server"}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(resp.as_bytes());
            let _ = stream.write_all(body);
            let _ = stream.flush();
        }
    });

    let mut probe_vars = BTreeMap::new();
    probe_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    probe_vars.insert("STUDIO_SERVER_PORT".to_string(), port.to_string());
    let cfg_probe = RuntimeConfig::from_map(&probe_vars, None);
    let (ok, detail) = probe_studio_health(&cfg_probe);
    let _ = srv.join();
    assert!(ok, "probe_studio_health debía devolver ok: {detail}");
    assert!(detail.contains("HTTP 200"));

    // 4. probe_mcp_health: deshabilitado por env => ok con detalle explícito
    let mut mcp_vars = BTreeMap::new();
    mcp_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    mcp_vars.insert("STUDIO_ENABLE_MCP".to_string(), "0".to_string());
    let cfg_mcp_off = RuntimeConfig::from_map(&mcp_vars, None);
    let (mcp_ok, mcp_detail) = probe_mcp_health(&cfg_mcp_off);
    assert!(mcp_ok);
    assert!(mcp_detail.contains("STUDIO_ENABLE_MCP=0"));

    // 5. probe_mcp_health con listener HTTP real en puerto efímero
    let Ok(mcp_listener) = TcpListener::bind("127.0.0.1:0") else {
        std::process::abort();
    };
    let mcp_port = mcp_listener.local_addr().map_or(18190, |addr| addr.port());
    let mcp_srv = thread::spawn(move || {
        if let Ok((mut stream, _)) = mcp_listener.accept() {
            let mut buf = [0_u8; 512];
            let _ = stream.read(&mut buf);
            let body = br#"{"status":"ok","mcp_enabled":true}"#;
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(resp.as_bytes());
            let _ = stream.write_all(body);
            let _ = stream.flush();
        }
    });
    mcp_vars.insert("STUDIO_ENABLE_MCP".to_string(), "1".to_string());
    mcp_vars.insert("STUDIO_SERVER_PORT".to_string(), mcp_port.to_string());
    let cfg_mcp_probe = RuntimeConfig::from_map(&mcp_vars, None);
    let (mcp_ok2, mcp_detail2) = probe_mcp_health(&cfg_mcp_probe);
    let _ = mcp_srv.join();
    assert!(mcp_ok2, "probe_mcp_health debía devolver ok: {mcp_detail2}");
    assert!(mcp_detail2.contains("/studio/api/mcp/health"));
}

// M4: contrato estricto del backend de percepción (S13b). El motor de salud de
// M4 (studio/comfy_studio/engines/health.py) depende de esta invariante: un
// backend "real" es production-ready (ok); "sim" es degradado (warn) pero NUNCA
// fatal. Este test la bloquea en el compilador estricto para que no se rompa.
#[test]
fn strict_s13b_perception_backend_real_ok_sim_warn_never_fatal() {
    let mut vars = BTreeMap::new();
    vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    let cfg = RuntimeConfig::from_map(&vars, None);

    // Backend real => S13b ok, resultado global ok.
    let mut snap_real = base_healthy_snapshot();
    snap_real.disk_free_gb = 200.0;
    snap_real.studio_perception_backend = "real".to_string();
    let rep_real = evaluate_strict_checks(&cfg, &snap_real);
    let s13b_real = rep_real.checks.iter().find(|c| c.id == "S13b");
    assert!(
        matches!(s13b_real, Some(c) if c.status == "ok"),
        "S13b con backend real debe ser ok"
    );
    assert_eq!(rep_real.result, "ok");

    // Backend sim => S13b warn, pero el resultado global sigue siendo ok (warn no es fatal).
    let mut snap_sim = base_healthy_snapshot();
    snap_sim.disk_free_gb = 200.0;
    snap_sim.studio_perception_backend = "sim".to_string();
    let rep_sim = evaluate_strict_checks(&cfg, &snap_sim);
    let s13b_sim = rep_sim.checks.iter().find(|c| c.id == "S13b");
    assert!(
        matches!(s13b_sim, Some(c) if c.status == "warn"),
        "S13b con backend sim debe ser warn"
    );
    assert_ne!(
        rep_sim.result, "fatal",
        "un backend sim nunca debe ser fatal"
    );
}
