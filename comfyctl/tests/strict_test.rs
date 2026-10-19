use comfyctl::config::RuntimeConfig;
use comfyctl::strict::{
    evaluate_strict_checks, probe_nvidia_smi, write_and_print_strict_report, SystemSnapshot,
};
use std::collections::BTreeMap;
use std::fs;

fn base_healthy_snapshot() -> SystemSnapshot {
    SystemSnapshot {
        gpu_query_ok: true,
        gpu_name: "NVIDIA GeForce RTX 5090".to_string(),
        driver_version: "570.124.06".to_string(),
        vram_total_mb: 32607,
        ram_total_mb: 65536,
        disk_free_gb: 120.0,
        workspace_writable: true,
        nginx_config_ok: true,
        nginx_detail: "nginx -t OK".to_string(),
        htpasswd_ok: true,
        htpasswd_detail: "/etc/nginx/.htpasswd válido".to_string(),
        comfy_install_ok: true,
        comfy_install_detail: "/opt/ComfyUI/main.py presente".to_string(),
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
    assert_eq!(rep.checks.len(), 9);
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

    let mut snap = base_healthy_snapshot();
    snap.vram_total_mb = 12000;
    snap.ram_total_mb = 8192;
    snap.disk_free_gb = 30.0;

    let rep = evaluate_strict_checks(&cfg_all, &snap);
    assert_eq!(rep.result, "fatal");
    for id in ["S3", "S4", "S5"] {
        assert!(rep.checks.iter().any(|c| c.id == id && c.status == "fatal"));
    }

    // Con MODEL_PROFILE=minimal (umbral 15 GB), 30 GB de disco libre pasa S5
    vars.insert("MODEL_PROFILE".to_string(), "minimal".to_string());
    let cfg_min = RuntimeConfig::from_map(&vars, None);
    let mut snap_min = base_healthy_snapshot();
    snap_min.disk_free_gb = 30.0;
    let rep_min = evaluate_strict_checks(&cfg_min, &snap_min);
    assert_eq!(rep_min.result, "ok");
}

#[test]
fn strict_cpu_mode_skips_gpu_checks_and_invalid_profile_warns() {
    let mut vars = BTreeMap::new();
    vars.insert("COMFYUI_EXTRA_ARGS".to_string(), "--cpu".to_string());
    vars.insert("MODEL_PROFILE".to_string(), "invalido".to_string());
    let cfg = RuntimeConfig::from_map(&vars, None);
    assert!(!cfg.strict_require_gpu);

    let mut snap = base_healthy_snapshot();
    snap.gpu_query_ok = false;
    snap.gpu_name = "sin gpu".to_string();
    snap.driver_version = String::new();
    snap.vram_total_mb = 0;

    let rep = evaluate_strict_checks(&cfg, &snap);
    assert_eq!(rep.result, "warn");
    for id in ["S1", "S2", "S3"] {
        assert!(rep.checks.iter().any(|c| c.id == id && c.status == "ok"));
    }
    assert!(rep
        .checks
        .iter()
        .any(|c| c.id == "S9" && c.status == "warn"));
}

#[test]
fn simulated_nvidia_smi_binary_probe() {
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
}
