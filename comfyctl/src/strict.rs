//! `strict` — Validación estricta de arranque (`comfyctl strict-check`, §6).
//! Evalúa las 9 invariantes S1..S9 antes de iniciar Fase 1 o `ComfyUI`,
//! persiste `/run/comfyui-strict.json` de forma atómica e imprime el banner.

use crate::config::RuntimeConfig;
use crate::sys;
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::process::Command;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrictCheckItem {
    pub id: String,
    pub name: String,
    pub status: String,
    pub detail: String,
    pub remediation: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrictReport {
    pub strict_start: bool,
    pub checked_at: String,
    pub result: String,
    pub checks: Vec<StrictCheckItem>,
}

/// Datos del sistema sondeados para evaluar S1..S9 (inyectables en tests).
#[derive(Debug, Clone)]
#[allow(clippy::struct_excessive_bools)]
pub struct SystemSnapshot {
    pub gpu_query_ok: bool,
    pub gpu_name: String,
    pub driver_version: String,
    pub vram_total_mb: u64,
    pub ram_total_mb: u64,
    pub disk_free_gb: f64,
    pub workspace_writable: bool,
    pub nginx_config_ok: bool,
    pub nginx_detail: String,
    pub htpasswd_ok: bool,
    pub htpasswd_detail: String,
    pub comfy_install_ok: bool,
    pub comfy_install_detail: String,
}

impl SystemSnapshot {
    /// Recolecta el estado real del contenedor invocando `nvidia-smi`, `/proc/meminfo`, `statvfs`, etc.
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    pub fn collect_live(cfg: &RuntimeConfig) -> Self {
        let (gpu_query_ok, gpu_name, driver_version, vram_total_mb) = probe_nvidia_smi();
        let ram_total_mb = probe_mem_total_mb();
        let disk_path = if cfg.comfy_data_dir.exists() {
            cfg.comfy_data_dir.as_path()
        } else if Path::new("/workspace").exists() {
            Path::new("/workspace")
        } else {
            Path::new("/")
        };
        let disk_free_gb = sys::disk_stats_gb(disk_path).map_or(0.0, |(_, _, free)| free);
        let workspace_writable = probe_dir_writable(&cfg.comfy_data_dir);
        let (nginx_config_ok, nginx_detail) = probe_nginx_t();
        let (htpasswd_ok, htpasswd_detail) = probe_htpasswd(Path::new("/etc/nginx/.htpasswd"));
        let (comfy_install_ok, comfy_install_detail) = probe_comfy_install(&cfg.comfy_app_dir);

        Self {
            gpu_query_ok,
            gpu_name,
            driver_version,
            vram_total_mb,
            ram_total_mb,
            disk_free_gb,
            workspace_writable,
            nginx_config_ok,
            nginx_detail,
            htpasswd_ok,
            htpasswd_detail,
            comfy_install_ok,
            comfy_install_detail,
        }
    }
}

pub fn probe_nvidia_smi() -> (bool, String, String, u64) {
    let bin = std::env::var("COMFY_NVIDIA_SMI_BIN").unwrap_or_else(|_| "nvidia-smi".to_string());
    let output = Command::new(&bin)
        .args([
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output();

    let Ok(out) = output else {
        return (
            false,
            "nvidia-smi no encontrado".to_string(),
            String::new(),
            0,
        );
    };
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let msg = if !stderr.is_empty() {
            stderr
        } else if !stdout.is_empty() {
            stdout
        } else {
            format!("rc={}", out.status.code().unwrap_or(1))
        };
        return (false, msg, String::new(), 0);
    }

    let text = String::from_utf8_lossy(&out.stdout);
    let first_line = text.lines().next().unwrap_or("").trim();
    let mut parts = first_line.split(',').map(str::trim);
    let Some(name) = parts.next() else {
        return (
            false,
            "salida vacía de nvidia-smi".to_string(),
            String::new(),
            0,
        );
    };
    let Some(driver) = parts.next() else {
        return (
            false,
            format!("formato inesperado: {first_line}"),
            String::new(),
            0,
        );
    };
    let Some(vram_str) = parts.next() else {
        return (
            false,
            format!("formato inesperado: {first_line}"),
            String::new(),
            0,
        );
    };
    let vram_mb = vram_str
        .split('.')
        .next()
        .unwrap_or("0")
        .trim()
        .parse::<u64>()
        .unwrap_or(0);

    if name.is_empty() {
        return (false, "nombre de GPU vacío".to_string(), String::new(), 0);
    }
    (true, name.to_string(), driver.to_string(), vram_mb)
}

fn probe_mem_total_mb() -> u64 {
    let Ok(content) = fs::read_to_string("/proc/meminfo") else {
        return 32768;
    };
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            let kb = rest
                .split_whitespace()
                .next()
                .and_then(|s| s.parse::<u64>().ok())
                .unwrap_or(0);
            return kb / 1024;
        }
    }
    0
}

fn probe_dir_writable(dir: &Path) -> bool {
    if fs::create_dir_all(dir).is_err() {
        return false;
    }
    let probe_path = dir.join(".comfyctl_write_probe");
    let res = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&probe_path)
        .and_then(|mut f| {
            f.write_all(b"ok")?;
            f.sync_all()
        });
    let _ = fs::remove_file(&probe_path);
    res.is_ok()
}

fn probe_nginx_t() -> (bool, String) {
    let bin = std::env::var("COMFY_NGINX_BIN").unwrap_or_else(|_| "nginx".to_string());
    match Command::new(bin).arg("-t").output() {
        Ok(out) if out.status.success() => (true, "nginx -t OK".to_string()),
        Ok(out) => {
            let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
            (
                false,
                if err.is_empty() {
                    "nginx -t falló".to_string()
                } else {
                    err
                },
            )
        }
        Err(e) => (false, format!("No se pudo ejecutar nginx -t: {e}")),
    }
}

fn probe_htpasswd(path: &Path) -> (bool, String) {
    let Ok(meta) = fs::metadata(path) else {
        return (false, format!("No existe {}", path.display()));
    };
    if meta.len() == 0 {
        return (false, format!("{} está vacío (0 bytes)", path.display()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = meta.permissions().mode() & 0o777;
        if (mode & 0o007) != 0 {
            return (
                false,
                format!(
                    "Permisos inseguros {:04o} en {} (esperado 0640)",
                    mode,
                    path.display()
                ),
            );
        }
    }
    (
        true,
        format!("{} válido ({} bytes)", path.display(), meta.len()),
    )
}

fn probe_comfy_install(app_dir: &Path) -> (bool, String) {
    let main_py = app_dir.join("main.py");
    let yaml = app_dir.join("extra_model_paths.yaml");
    if !main_py.is_file() {
        return (false, format!("Falta {}", main_py.display()));
    }
    if !yaml.is_file() {
        return (false, format!("Falta {}", yaml.display()));
    }
    (
        true,
        format!("{} y extra_model_paths.yaml presentes", main_py.display()),
    )
}

fn parse_driver_major(driver: &str) -> u32 {
    driver
        .split('.')
        .next()
        .unwrap_or("0")
        .trim()
        .parse::<u32>()
        .unwrap_or(0)
}

/// Evalúa las 9 comprobaciones S1..S9 sobre un `SystemSnapshot`.
#[allow(clippy::too_many_lines)]
pub fn evaluate_strict_checks(cfg: &RuntimeConfig, snap: &SystemSnapshot) -> StrictReport {
    let mut checks = Vec::with_capacity(9);

    let status_for = |passed: bool, is_fatal_when_strict: bool| -> &'static str {
        if passed {
            "ok"
        } else if is_fatal_when_strict && cfg.strict_start {
            "fatal"
        } else {
            "warn"
        }
    };

    // S1: GPU accesible
    if cfg.strict_require_gpu {
        let s1_ok = snap.gpu_query_ok;
        checks.push(StrictCheckItem {
            id: "S1".to_string(),
            name: "gpu_present".to_string(),
            status: status_for(s1_ok, true).to_string(),
            detail: if s1_ok {
                snap.gpu_name.clone()
            } else {
                format!("GPU no accesible: {}", snap.gpu_name)
            },
            remediation: if s1_ok {
                None
            } else {
                Some(
                    "El contenedor arrancó sin GPU NVIDIA accesible. Destruye esta instancia (comfy destroy <id>) y alquila otro host, o usa COMFYUI_EXTRA_ARGS='--cpu' para modo CPU."
                        .to_string(),
                )
            },
        });
    } else {
        checks.push(StrictCheckItem {
            id: "S1".to_string(),
            name: "gpu_present".to_string(),
            status: "ok".to_string(),
            detail: "Modo CPU autorizado (STRICT_REQUIRE_GPU=0 o --cpu)".to_string(),
            remediation: None,
        });
    }

    // S2: Driver NVIDIA >= STRICT_MIN_DRIVER (570 para CUDA 12.8)
    if cfg.strict_require_gpu {
        let drv_major = parse_driver_major(&snap.driver_version);
        let s2_ok = snap.gpu_query_ok && drv_major >= cfg.strict_min_driver;
        checks.push(StrictCheckItem {
            id: "S2".to_string(),
            name: "driver_min".to_string(),
            status: status_for(s2_ok, true).to_string(),
            detail: if s2_ok {
                format!(
                    "driver NVIDIA {} >= {} requerido por CUDA 12.8",
                    snap.driver_version, cfg.strict_min_driver
                )
            } else {
                format!(
                    "driver NVIDIA {} < {} requerido por CUDA 12.8",
                    if snap.driver_version.is_empty() {
                        "N/A"
                    } else {
                        &snap.driver_version
                    },
                    cfg.strict_min_driver
                )
            },
            remediation: if s2_ok {
                None
            } else {
                Some(
                    "El host de Vast.ai tiene un driver incompatible con PyTorch cu128. Destruye esta instancia (comfy destroy <id>) y alquila otra con cuda_max_good>=12.8."
                        .to_string(),
                )
            },
        });
    } else {
        checks.push(StrictCheckItem {
            id: "S2".to_string(),
            name: "driver_min".to_string(),
            status: "ok".to_string(),
            detail: "Omitido en modo CPU".to_string(),
            remediation: None,
        });
    }

    // S3: VRAM total >= STRICT_MIN_VRAM_MB (16000 MB por defecto)
    if cfg.strict_require_gpu {
        let s3_ok = snap.gpu_query_ok && snap.vram_total_mb >= cfg.strict_min_vram_mb;
        checks.push(StrictCheckItem {
            id: "S3".to_string(),
            name: "vram_min".to_string(),
            status: status_for(s3_ok, true).to_string(),
            detail: format!(
                "{} MB VRAM detectados (mínimo requerido: {} MB)",
                snap.vram_total_mb, cfg.strict_min_vram_mb
            ),
            remediation: if s3_ok {
                None
            } else {
                Some(format!(
                    "VRAM insuficiente para los checkpoints SDXL/FLUX configurados. Alquila una GPU con >= {} MB o ajusta STRICT_MIN_VRAM_MB.",
                    cfg.strict_min_vram_mb
                ))
            },
        });
    } else {
        checks.push(StrictCheckItem {
            id: "S3".to_string(),
            name: "vram_min".to_string(),
            status: "ok".to_string(),
            detail: "Omitido en modo CPU".to_string(),
            remediation: None,
        });
    }

    // S4: RAM del host >= STRICT_MIN_RAM_MB (16384 MB por defecto)
    let s4_ok = snap.ram_total_mb >= cfg.strict_min_ram_mb;
    checks.push(StrictCheckItem {
        id: "S4".to_string(),
        name: "ram_min".to_string(),
        status: status_for(s4_ok, true).to_string(),
        detail: format!(
            "{} MB RAM detectados (mínimo requerido: {} MB)",
            snap.ram_total_mb, cfg.strict_min_ram_mb
        ),
        remediation: if s4_ok {
            None
        } else {
            Some(format!(
                "RAM del host insuficiente (< {} MB); provocará muertes SIGKILL (rc=137) al cargar modelos. Alquila un host con cpu_ram>=32.",
                cfg.strict_min_ram_mb
            ))
        },
    });

    // S5: Espacio libre en disco >= STRICT_MIN_DISK_GB
    #[allow(clippy::cast_precision_loss)]
    let s5_ok = snap.disk_free_gb >= (cfg.strict_min_disk_gb as f64);
    checks.push(StrictCheckItem {
        id: "S5".to_string(),
        name: "disk_min".to_string(),
        status: status_for(s5_ok, true).to_string(),
        detail: format!(
            "{:.1} GB libres en {} (mínimo requerido para perfil '{}': {} GB)",
            snap.disk_free_gb,
            cfg.comfy_data_dir.display(),
            cfg.model_profile.as_str(),
            cfg.strict_min_disk_gb
        ),
        remediation: if s5_ok {
            None
        } else {
            Some(format!(
                "Disco insuficiente para el perfil '{}'. Aumenta el disco en Vast.ai (--disk 120) o usa -e MODEL_PROFILE=minimal.",
                cfg.model_profile.as_str()
            ))
        },
    });

    // S6: /workspace/comfyui-data escribible
    let s6_ok = snap.workspace_writable;
    checks.push(StrictCheckItem {
        id: "S6".to_string(),
        name: "workspace_writable".to_string(),
        status: status_for(s6_ok, true).to_string(),
        detail: if s6_ok {
            format!("{} escribible", cfg.comfy_data_dir.display())
        } else {
            format!("No se puede escribir en {}", cfg.comfy_data_dir.display())
        },
        remediation: if s6_ok {
            None
        } else {
            Some(format!(
                "El volumen {} está montado en solo lectura o con permisos rotos. Revisa el montaje del volumen en Vast.ai.",
                cfg.comfy_data_dir.display()
            ))
        },
    });

    // S7: Configuración de Nginx válida y .htpasswd presente
    let s7_ok = snap.nginx_config_ok && snap.htpasswd_ok;
    checks.push(StrictCheckItem {
        id: "S7".to_string(),
        name: "nginx_and_htpasswd".to_string(),
        status: status_for(s7_ok, true).to_string(),
        detail: format!("{}; {}", snap.nginx_detail, snap.htpasswd_detail),
        remediation: if s7_ok {
            None
        } else {
            Some(
                "Revisa /etc/nginx/nginx.conf y permisos 0640 de /etc/nginx/.htpasswd.".to_string(),
            )
        },
    });

    // S8: Instalación de ComfyUI (/opt/ComfyUI/main.py y extra_model_paths.yaml)
    let s8_ok = snap.comfy_install_ok;
    checks.push(StrictCheckItem {
        id: "S8".to_string(),
        name: "comfyui_install".to_string(),
        status: status_for(s8_ok, true).to_string(),
        detail: snap.comfy_install_detail.clone(),
        remediation: if s8_ok {
            None
        } else {
            Some(
                "La instalación en /opt/ComfyUI está incompleta; reconstruye la imagen Docker."
                    .to_string(),
            )
        },
    });

    // S9: MODEL_PROFILE válido (siempre WARN, nunca FATAL, hace fallback a 'all')
    let s9_ok = cfg.model_profile_valid;
    checks.push(StrictCheckItem {
        id: "S9".to_string(),
        name: "model_profile_valid".to_string(),
        status: if s9_ok { "ok" } else { "warn" }.to_string(),
        detail: if s9_ok {
            format!("MODEL_PROFILE='{}'", cfg.model_profile.as_str())
        } else {
            format!(
                "MODEL_PROFILE='{}' desconocido; usando fallback 'all'",
                cfg.model_profile_raw
            )
        },
        remediation: if s9_ok {
            None
        } else {
            Some(
                "Valores válidos de MODEL_PROFILE: all | general | realistic | anime | minimal."
                    .to_string(),
            )
        },
    });

    let has_fatal = checks.iter().any(|c| c.status == "fatal");
    let has_warn = checks.iter().any(|c| c.status == "warn");
    let result = if has_fatal {
        "fatal"
    } else if has_warn {
        "warn"
    } else {
        "ok"
    }
    .to_string();

    StrictReport {
        strict_start: cfg.strict_start,
        checked_at: sys::format_utc_iso8601(sys::now_unix_secs()),
        result,
        checks,
    }
}

/// Persiste el informe en `/run/comfyui-strict.json` de forma atómica e imprime el banner.
pub fn write_and_print_strict_report(
    cfg: &RuntimeConfig,
    report: &StrictReport,
) -> Result<(), String> {
    let json_bytes = serde_json::to_vec_pretty(report)
        .map_err(|e| format!("Error serializando StrictReport: {e}"))?;
    sys::atomic_write(&cfg.strict_json_path, &json_bytes, 0o644)?;

    for chk in &report.checks {
        let tag = match chk.status.as_str() {
            "ok" => "[STRICT][OK]",
            "warn" => "[STRICT][WARN]",
            _ => "[STRICT][FATAL]",
        };
        println!("{tag:<16} {} ({}) — {}", chk.id, chk.name, chk.detail);
    }

    if report.result == "fatal" {
        println!("========================================================================");
        println!("[FATAL] COMFYCTL STRICT-CHECK FALLÓ — ComfyUI NO se iniciará");
        println!("========================================================================");
        for chk in report.checks.iter().filter(|c| c.status == "fatal") {
            println!(" Fallo {} ({}): {}", chk.id, chk.name, chk.detail);
            if let Some(rem) = &chk.remediation {
                println!(" Causa/Acción: {rem}");
            }
        }
        println!(" Acción  : Destruye esta instancia (no servirá con esta imagen) y alquila");
        println!("           otra con cuda_max_good>=12.8:");
        println!("             comfy destroy <id>");
        println!("             comfy rent");
        println!(
            " Nota    : El portal /home, /status y /report.md seguirán activos {}s",
            cfg.strict_fatal_grace_s
        );
        println!("           para inspección; después el contenedor terminará con código 20.");
        println!("           Para degradar a advertencia en el futuro: -e STRICT_START=0");
        println!("========================================================================");
    }

    Ok(())
}

/// Ejecuta `comfyctl strict-check` y devuelve el código de salida (`0` o `20`).
pub fn run_strict_check_cmd(cfg: &RuntimeConfig) -> Result<i32, String> {
    let snap = SystemSnapshot::collect_live(cfg);
    let report = evaluate_strict_checks(cfg, &snap);
    write_and_print_strict_report(cfg, &report)?;
    if report.result == "fatal" {
        Ok(20)
    } else {
        Ok(0)
    }
}
