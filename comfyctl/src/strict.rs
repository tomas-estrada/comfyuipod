//! `strict` — Validación estricta de arranque (`comfyctl strict-check`, §6).
//! Evalúa las 10 invariantes S1..S10 antes de iniciar Fase 1 o `ComfyUI`,
//! persiste `/run/comfyui-strict.json` de forma atómica e imprime el banner.

use crate::config::RuntimeConfig;
use crate::sys;
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

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

/// Datos del sistema sondeados para evaluar S1..S10 (inyectables en tests).
#[derive(Debug, Clone)]
#[allow(clippy::struct_excessive_bools)]
pub struct SystemSnapshot {
    pub gpu_query_ok: bool,
    pub gpu_name: String,
    pub driver_version: String,
    pub vram_total_mb: u64,
    pub ram_probe_ok: bool,
    pub ram_total_mb: u64,
    pub disk_free_gb: f64,
    pub workspace_writable: bool,
    pub nginx_config_ok: bool,
    pub nginx_detail: String,
    pub htpasswd_ok: bool,
    pub htpasswd_missing: bool,
    pub htpasswd_detail: String,
    pub comfy_install_ok: bool,
    pub comfy_install_detail: String,
    pub cuda_torch_ok: bool,
    pub cuda_torch_detail: String,
}

impl SystemSnapshot {
    /// Recolecta el estado real del contenedor invocando `nvidia-smi`, `/proc/meminfo` + cgroups,
    /// `statvfs`, `nginx -t` y `torch_cuda.py` con timeouts estrictos (`R-02`, `R-03`, `R-06`, `R-07`).
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    pub fn collect_live(cfg: &RuntimeConfig) -> Self {
        let (gpu_query_ok, gpu_name, driver_version, vram_total_mb) = probe_nvidia_smi();
        let (ram_probe_ok, ram_total_mb) = probe_mem_total_mb();
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
        let (htpasswd_ok, htpasswd_missing, htpasswd_detail) = probe_htpasswd(&cfg.htpasswd_path);
        let (comfy_install_ok, comfy_install_detail) = probe_comfy_install(&cfg.comfy_app_dir);
        let (cuda_torch_ok, cuda_torch_detail) = probe_cuda_torch(cfg.strict_require_gpu);

        Self {
            gpu_query_ok,
            gpu_name,
            driver_version,
            vram_total_mb,
            ram_probe_ok,
            ram_total_mb,
            disk_free_gb,
            workspace_writable,
            nginx_config_ok,
            nginx_detail,
            htpasswd_ok,
            htpasswd_missing,
            htpasswd_detail,
            comfy_install_ok,
            comfy_install_detail,
            cuda_torch_ok,
            cuda_torch_detail,
        }
    }
}

pub fn probe_nvidia_smi() -> (bool, String, String, u64) {
    let bin = std::env::var("COMFY_NVIDIA_SMI_BIN").unwrap_or_else(|_| "nvidia-smi".to_string());
    let mut cmd = Command::new(&bin);
    cmd.args([
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    ]);

    let Ok((code, stdout, stderr, timed_out)) =
        sys::run_command_with_timeout(&mut cmd, Duration::from_secs(10))
    else {
        return (
            false,
            "nvidia-smi no encontrado".to_string(),
            String::new(),
            0,
        );
    };
    if timed_out {
        return (
            false,
            "timeout (>10s) ejecutando nvidia-smi".to_string(),
            String::new(),
            0,
        );
    }
    if code != Some(0) {
        let err_trim = stderr.trim();
        let out_trim = stdout.trim();
        let msg = if !err_trim.is_empty() {
            err_trim.to_string()
        } else if !out_trim.is_empty() {
            out_trim.to_string()
        } else {
            format!("rc={}", code.unwrap_or(1))
        };
        return (false, msg, String::new(), 0);
    }

    let first_line = stdout.lines().next().unwrap_or("").trim();
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

/// Lee la RAM disponible en MiB desde `/proc/meminfo` y aplica el límite de cgroup v2/v1 si existe (`R-03`).
/// Nunca devuelve un valor ficticio silencioso: si `/proc/meminfo` no es legible devuelve `(false, 0)`.
pub fn probe_mem_total_mb() -> (bool, u64) {
    let Ok(content) = fs::read_to_string("/proc/meminfo") else {
        return (false, 0);
    };
    let mut mem_mb = 0_u64;
    let mut found = false;
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            if let Some(kb) = rest
                .split_whitespace()
                .next()
                .and_then(|s| s.parse::<u64>().ok())
            {
                mem_mb = kb / 1024;
                found = true;
                break;
            }
        }
    }
    if !found || mem_mb == 0 {
        return (false, 0);
    }

    for cg_path in [
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ] {
        if let Ok(raw) = fs::read_to_string(cg_path) {
            if let Ok(bytes) = raw.trim().parse::<u64>() {
                // Ignorar "max" o límites ilimitados (~PAGE_COUNTER_MAX >= 1 PiB)
                if bytes > 0 && bytes < (1_u64 << 50) {
                    let cg_mb = bytes / (1024 * 1024);
                    if cg_mb > 0 && cg_mb < mem_mb {
                        mem_mb = cg_mb;
                    }
                }
            }
        }
    }

    (true, mem_mb)
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
    let mut cmd = Command::new(bin);
    cmd.arg("-t");
    match sys::run_command_with_timeout(&mut cmd, Duration::from_secs(5)) {
        Ok((_, _, _, true)) => (false, "timeout (>5s) ejecutando nginx -t".to_string()),
        Ok((Some(0), _, _, false)) => (true, "nginx -t OK".to_string()),
        Ok((_, _, stderr, false)) => {
            let err = stderr.trim().to_string();
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

/// Devuelve `(ok, missing, detail)` para `/etc/nginx/.htpasswd` (`R-02`).
pub fn probe_htpasswd(path: &Path) -> (bool, bool, String) {
    let Ok(meta) = fs::metadata(path) else {
        return (false, true, format!("No existe {}", path.display()));
    };
    if meta.len() == 0 {
        return (
            false,
            false,
            format!("{} está vacío (0 bytes)", path.display()),
        );
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = meta.permissions().mode() & 0o777;
        if (mode & 0o007) != 0 {
            return (
                false,
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
        false,
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

fn resolve_torch_probe_path() -> PathBuf {
    if let Ok(dir) = std::env::var("COMFY_PROBES_DIR") {
        let p = Path::new(&dir).join("torch_cuda.py");
        if p.is_file() {
            return p;
        }
    }
    let etc_p = Path::new("/etc/comfyui/probes/torch_cuda.py");
    if etc_p.is_file() {
        return etc_p.to_path_buf();
    }
    let local_p = Path::new("config/probes/torch_cuda.py");
    if local_p.is_file() {
        return local_p.to_path_buf();
    }
    etc_p.to_path_buf()
}

/// Ejecuta la sonda `torch_cuda.py` con timeout de 20s para validar `S10` (`cuda_torch`, `R-06` y `R-07`).
pub fn probe_cuda_torch(require_gpu: bool) -> (bool, String) {
    if !require_gpu {
        return (true, "Omitido en modo CPU".to_string());
    }
    let py_bin = std::env::var("COMFY_PROBE_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let script = resolve_torch_probe_path();
    let mut cmd = Command::new(py_bin);
    cmd.arg(&script);
    match sys::run_command_with_timeout(&mut cmd, Duration::from_secs(20)) {
        Ok((_, _, _, true)) => (
            false,
            "timeout (>20s) ejecutando sonda torch_cuda.py".to_string(),
        ),
        Ok((_, stdout, stderr, false)) => {
            let line = stdout.lines().next().unwrap_or("").trim();
            if let Some((lvl, msg)) = line.split_once('|') {
                if lvl.trim() == "OK" {
                    return (true, msg.trim().to_string());
                }
                return (false, msg.trim().to_string());
            }
            let err_txt = if line.is_empty() {
                stderr.trim().to_string()
            } else {
                line.to_string()
            };
            (
                false,
                if err_txt.is_empty() {
                    "torch_cuda.py falló sin salida".to_string()
                } else {
                    err_txt
                },
            )
        }
        Err(e) => (
            false,
            format!("No se pudo ejecutar {}: {e}", script.display()),
        ),
    }
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

/// Evalúa las 10 comprobaciones S1..S10 sobre un `SystemSnapshot` (§6.2, `R-02`, `R-03`, `R-06`).
#[allow(clippy::too_many_lines)]
pub fn evaluate_strict_checks(cfg: &RuntimeConfig, snap: &SystemSnapshot) -> StrictReport {
    let mut checks = Vec::with_capacity(10);

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

    // S3: VRAM total >= STRICT_MIN_VRAM_MB (20000 MB por defecto; WARN si < 22000 MB, §6.2 / R-06)
    if cfg.strict_require_gpu {
        let s3_fatal = !snap.gpu_query_ok || snap.vram_total_mb < cfg.strict_min_vram_mb;
        // S-08: la banda WARN se aplica siempre que el umbral WARN supere al mínimo,
        // sin gate sobre el valor absoluto del mínimo.
        let s3_warn = !s3_fatal
            && cfg.strict_warn_vram_mb > cfg.strict_min_vram_mb
            && snap.vram_total_mb < cfg.strict_warn_vram_mb;
        let s3_status = if s3_fatal {
            status_for(false, true)
        } else if s3_warn {
            "warn"
        } else {
            "ok"
        };
        checks.push(StrictCheckItem {
            id: "S3".to_string(),
            name: "vram_min".to_string(),
            status: s3_status.to_string(),
            detail: if s3_warn {
                format!(
                    "{} MB VRAM detectados (>= {} MB mínimo, pero < {} MB recomendados)",
                    snap.vram_total_mb, cfg.strict_min_vram_mb, cfg.strict_warn_vram_mb
                )
            } else {
                format!(
                    "{} MB VRAM detectados (mínimo requerido: {} MB)",
                    snap.vram_total_mb, cfg.strict_min_vram_mb
                )
            },
            remediation: if s3_fatal {
                Some(format!(
                    "VRAM insuficiente para los checkpoints SDXL/FLUX configurados. Alquila una GPU con >= {} MB o ajusta STRICT_MIN_VRAM_MB.",
                    cfg.strict_min_vram_mb
                ))
            } else {
                None
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

    // S4: RAM del host >= STRICT_MIN_RAM_MB (16384 MB por defecto con 5% tolerancia de reserva kernel; WARN en 16-32 GB, R-03 y R-06)
    if snap.ram_probe_ok {
        let min_ram_eff = cfg.strict_min_ram_mb.saturating_mul(95) / 100;
        let warn_ram_eff = cfg.strict_warn_ram_mb.saturating_mul(95) / 100;
        let s4_fatal = snap.ram_total_mb < min_ram_eff;
        // S-08: banda WARN siempre que STRICT_WARN_RAM_MB > STRICT_MIN_RAM_MB.
        let s4_warn = !s4_fatal
            && cfg.strict_warn_ram_mb > cfg.strict_min_ram_mb
            && snap.ram_total_mb < warn_ram_eff;
        let s4_status = if s4_fatal {
            status_for(false, true)
        } else if s4_warn {
            "warn"
        } else {
            "ok"
        };
        checks.push(StrictCheckItem {
            id: "S4".to_string(),
            name: "ram_min".to_string(),
            status: s4_status.to_string(),
            detail: if s4_warn {
                format!(
                    "{} MB RAM detectados (>= {} MB mínimo con margen kernel, pero < {} MB recomendados para perfil all/FLUX)",
                    snap.ram_total_mb, cfg.strict_min_ram_mb, cfg.strict_warn_ram_mb
                )
            } else {
                format!(
                    "{} MB RAM detectados (mínimo requerido: {} MB)",
                    snap.ram_total_mb, cfg.strict_min_ram_mb
                )
            },
            remediation: if s4_fatal {
                Some(format!(
                    "RAM del host insuficiente (< {} MB); provocará muertes SIGKILL (rc=137) al cargar modelos. Alquila un host con cpu_ram>=32.",
                    cfg.strict_min_ram_mb
                ))
            } else {
                None
            },
        });
    } else {
        checks.push(StrictCheckItem {
            id: "S4".to_string(),
            name: "ram_min".to_string(),
            status: "warn".to_string(),
            detail: "No se pudo leer /proc/meminfo (entorno no-Linux o procfs no disponible)"
                .to_string(),
            remediation: None,
        });
    }

    // S5: Espacio libre en disco >= STRICT_MIN_DISK_GB (75 / 45 / 10 GB o 5 GB con SKIP_MODEL_DOWNLOAD=1)
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

    // S7: Configuración de Nginx válida y .htpasswd presente (R-02: si se invoca fuera de start.sh y aún no existe .htpasswd, degrada a WARN)
    let (s7_status, s7_detail, s7_rem) = if !snap.nginx_config_ok {
        (
            status_for(false, true),
            format!("{}; {}", snap.nginx_detail, snap.htpasswd_detail),
            Some("Revisa la sintaxis de /etc/nginx/nginx.conf.".to_string()),
        )
    } else if snap.htpasswd_ok {
        (
            "ok",
            format!("{}; {}", snap.nginx_detail, snap.htpasswd_detail),
            None,
        )
    } else if snap.htpasswd_missing && !cfg.require_htpasswd {
        (
            "warn",
            format!(
                "{}; {} (aún no generado; se crea en start.sh)",
                snap.nginx_detail, snap.htpasswd_detail
            ),
            None,
        )
    } else {
        (
            status_for(false, true),
            format!("{}; {}", snap.nginx_detail, snap.htpasswd_detail),
            Some(
                "Revisa /etc/nginx/nginx.conf y permisos 0640 de /etc/nginx/.htpasswd.".to_string(),
            ),
        )
    };
    checks.push(StrictCheckItem {
        id: "S7".to_string(),
        name: "nginx_and_htpasswd".to_string(),
        status: s7_status.to_string(),
        detail: s7_detail,
        remediation: s7_rem,
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

    // S10: PyTorch CUDA funcional (torch.cuda.is_available() + matmul 1024x1024, §6.2 / R-06)
    if cfg.strict_require_gpu {
        let s10_ok = snap.cuda_torch_ok;
        checks.push(StrictCheckItem {
            id: "S10".to_string(),
            name: "cuda_torch".to_string(),
            status: status_for(s10_ok, true).to_string(),
            detail: snap.cuda_torch_detail.clone(),
            remediation: if s10_ok {
                None
            } else {
                Some(
                    "PyTorch no pudo inicializar el contexto CUDA o ejecutar matmul en la GPU. Destruye esta instancia (comfy destroy <id>) y alquila otro host."
                        .to_string(),
                )
            },
        });
    } else {
        checks.push(StrictCheckItem {
            id: "S10".to_string(),
            name: "cuda_torch".to_string(),
            status: "ok".to_string(),
            detail: "Omitido en modo CPU".to_string(),
            remediation: None,
        });
    }

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
