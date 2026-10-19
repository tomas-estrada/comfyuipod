use comfyctl::config::RuntimeConfig;
use comfyctl::strict::{
    evaluate_strict_checks, probe_nvidia_smi, write_and_print_strict_report, SystemSnapshot,
};
use comfyctl::sys;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::process::Command;
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

    // R-07: Verificar que run_command_with_timeout mata procesos colgados en < 2s
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
    assert!(start.elapsed() < Duration::from_secs(3));
}
