//! `strict` — Validación estricta de arranque (`comfyctl strict-check`, §6).
//! Evalúa las 10 invariantes S1..S10 antes de iniciar Fase 1 o `ComfyUI`,
//! persiste `/run/comfyui-strict.json` de forma atómica e imprime el banner.

use crate::config::{ModelProfile, RuntimeConfig};
use crate::sys;
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
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

/// Datos del sistema sondeados para evaluar S1..S10 (+ S11/S12 en perfil `studio`, inyectables en tests).
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
    pub studio_health_ok: bool,
    pub studio_health_detail: String,
    /// M2 (`M3-IDEA-035`): estado del subsistema de percepción reportado por
    /// `/studio/api/health` (`perception.available` + `perception.reason`).
    /// Es `warn`, no `fatal`: desde M1.2 la percepción es degradable por diseño y el
    /// resto de Studio sigue operativo sin ella.
    pub studio_perception_ok: bool,
    pub studio_perception_detail: String,
    /// M3: backend de percepción reportado por `/studio/api/health` (`perception.backend`:
    /// "sim" | "real"). `warn` si es "sim" (degradado por diseño en M1/M2).
    pub studio_perception_backend: String,
    /// M3: índice de percepción desactualizado (`perception.stale`: bool). `warn` si
    /// true: las dims almacenadas no coinciden con las activas, se requiere `reindex`.
    pub studio_perception_stale: bool,
    pub mcp_health_ok: bool,
    pub mcp_health_detail: String,
}

impl SystemSnapshot {
    /// Recolecta el estado real del contenedor invocando `nvidia-smi`, `/proc/meminfo` + cgroups,
    /// `statvfs`, `nginx -t`, `torch_cuda.py` y salud de `studio-server` (si `MODEL_PROFILE=studio`)
    /// con timeouts estrictos (`R-02`, `R-03`, `R-06`, `R-07`).
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
        let (studio_health_ok, studio_health_detail) = probe_studio_health(cfg);
        let (studio_perception_ok, studio_perception_detail) = probe_studio_perception(cfg);
        let (studio_perception_backend, studio_perception_stale) =
            probe_studio_perception_backend_and_stale(cfg);
        let (mcp_health_ok, mcp_health_detail) = probe_mcp_health(cfg);

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
            studio_health_ok,
            studio_health_detail,
            studio_perception_ok,
            studio_perception_detail,
            studio_perception_backend,
            studio_perception_stale,
            mcp_health_ok,
            mcp_health_detail,
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
                    "Permisos inseguros {:04o} en {} (bits de acceso 'other' activos; se exige sin permisos world-accessible)",
                    mode,
                    path.display()
                ),
            );
        }
    }
    // Validación mínima de formato: al menos una línea con forma `usuario:hash`
    // (acepta `admin:{PLAIN}test` del fixture de arranque y los hashes bcrypt/SHA estándar).
    let Ok(content) = fs::read_to_string(path) else {
        return (false, false, format!("No se pudo leer {}", path.display()));
    };
    let has_valid_line = content.lines().any(|ln| {
        ln.trim().split_once(':').is_some_and(|(user, hash)| {
            !user.is_empty() && !hash.is_empty() && !user.contains(char::is_whitespace)
        })
    });
    if !has_valid_line {
        return (
            false,
            false,
            format!(
                "{} no contiene ninguna línea 'usuario:hash' válida",
                path.display()
            ),
        );
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

fn http_probe_once(port: u16, path: &str) -> Result<u16, String> {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500))
        .map_err(|e| format!("conexión a 127.0.0.1:{port} falló: {e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let req = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("error enviando petición HTTP a 127.0.0.1:{port}: {e}"))?;
    let mut buf = [0_u8; 512];
    let n = stream
        .read(&mut buf)
        .map_err(|e| format!("error leyendo respuesta HTTP de 127.0.0.1:{port}: {e}"))?;
    if n == 0 {
        return Err(format!("respuesta vacía de 127.0.0.1:{port}"));
    }
    let text = String::from_utf8_lossy(&buf);
    let status_line = text.lines().next().unwrap_or("");
    let code = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| format!("línea de estado HTTP inválida: {status_line}"))?;
    Ok(code)
}

/// Lee la respuesta HTTP completa (status + cuerpo acotado) de un endpoint local.
/// Usado por S13 para inspeccionar el JSON de `/studio/api/health`.
fn http_get_body(port: u16, path: &str) -> Result<(u16, String), String> {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500))
        .map_err(|e| format!("conexión a 127.0.0.1:{port} falló: {e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let req = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("error enviando petición HTTP a 127.0.0.1:{port}: {e}"))?;
    let mut raw = Vec::new();
    // 64 KiB: el payload de /health es de unos cientos de bytes; el tope evita que un
    // endpoint inesperadamente verboso bloquee el arranque.
    let mut chunk = [0_u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                raw.extend_from_slice(chunk.get(..n).unwrap_or(&[]));
                if raw.len() > 64 * 1024 {
                    break;
                }
            }
        }
    }
    if raw.is_empty() {
        return Err(format!("respuesta vacía de 127.0.0.1:{port}"));
    }
    let text = String::from_utf8_lossy(&raw).into_owned();
    let code = text
        .lines()
        .next()
        .unwrap_or("")
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| "línea de estado HTTP inválida".to_string())?;
    let body = text
        .split_once("\r\n\r\n")
        .map_or("", |(_, b)| b)
        .to_string();
    Ok((code, body))
}

/// Sondea `perception.available` en `/studio/api/health` (M2, `M3-IDEA-035`).
/// Devuelve `(ok, detalle)`. `ok == false` ⇒ S13 en `warn` (degradable por diseño).
pub fn probe_studio_perception(cfg: &RuntimeConfig) -> (bool, String) {
    if !cfg.model_profile.is_studio_family() {
        return (true, "Omitido fuera del perfil studio".to_string());
    }
    let port = cfg.studio_server_port;
    let (code, body) = match http_get_body(port, "/studio/api/health") {
        Ok(pair) => pair,
        Err(e) => return (false, e),
    };
    if code != 200 {
        return (false, format!("HTTP {code} en /studio/api/health"));
    }
    let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body) else {
        return (
            false,
            "respuesta de /studio/api/health no es JSON válido".to_string(),
        );
    };
    let Some(perception) = parsed.get("perception") else {
        return (
            false,
            "la respuesta de /studio/api/health no incluye `perception`".to_string(),
        );
    };
    let available = perception
        .get("available")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    if available {
        let simulated = perception
            .get("simulated")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        (
            true,
            if simulated {
                "percepción disponible (backend en SIMULACIÓN: `simulated: true`)".to_string()
            } else {
                "percepción disponible".to_string()
            },
        )
    } else {
        let reason = perception
            .get("reason")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("sin motivo reportado");
        (false, format!("percepción deshabilitada: {reason}"))
    }
}

/// Lee `perception.backend` ("sim" | "real") y `perception.stale` (bool) del
/// `/studio/api/health` para el strict-check M3. Nunca fatal: son informativos
/// (warn) porque la percepción es degradable por diseño.
pub fn probe_studio_perception_backend_and_stale(cfg: &RuntimeConfig) -> (String, bool) {
    if !cfg.model_profile.is_studio_family() {
        return ("sim".to_string(), false);
    }
    let port = cfg.studio_server_port;
    let Ok((code, body)) = http_get_body(port, "/studio/api/health") else {
        return ("sim".to_string(), false);
    };
    if code != 200 {
        return ("sim".to_string(), false);
    }
    let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body) else {
        return ("sim".to_string(), false);
    };
    let Some(perception) = parsed.get("perception") else {
        return ("sim".to_string(), false);
    };
    let backend = perception
        .get("backend")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("sim")
        .to_string();
    let stale = perception
        .get("stale")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    (backend, stale)
}

/// Comprueba la salud de `studio-server` en `127.0.0.1:{studio_server_port}` (`/studio/api/health`).
pub fn probe_studio_health(cfg: &RuntimeConfig) -> (bool, String) {
    if !cfg.model_profile.is_studio_family() {
        return (true, "Omitido fuera del perfil studio".to_string());
    }
    let retries = std::env::var("STUDIO_HEALTH_RETRIES")
        .ok()
        .and_then(|s| s.trim().parse::<u32>().ok())
        .unwrap_or(10)
        .max(1);
    let port = cfg.studio_server_port;
    let mut last_err = format!("studio-server no accesible en 127.0.0.1:{port}");
    for attempt in 0..retries {
        match http_probe_once(port, "/studio/api/health") {
            Ok(200) => {
                let msg = format!("studio-server healthy en 127.0.0.1:{port} (HTTP 200)");
                return (true, msg);
            }
            Ok(404) => match http_probe_once(port, "/health") {
                Ok(200) => {
                    let msg = format!("studio-server healthy en 127.0.0.1:{port} (HTTP 200)");
                    return (true, msg);
                }
                Ok(code) => {
                    last_err = format!("HTTP {code} en 127.0.0.1:{port}/health");
                }
                Err(e) => {
                    last_err = e;
                }
            },
            Ok(code) => {
                last_err = format!("HTTP {code} en 127.0.0.1:{port}/studio/api/health");
            }
            Err(e) => {
                last_err = e;
            }
        }
        if attempt + 1 < retries {
            thread::sleep(Duration::from_millis(150));
        }
    }
    (false, last_err)
}

/// Comprueba la salud del endpoint MCP (`/studio/api/mcp/health`) en el `studio-server`
/// (M3-C, S12). No fatal: MCP vive dentro del mismo proceso que S11; un fallo aquí
/// indica router ausente o `STUDIO_ENABLE_MCP` inconsistente, no host inviable.
pub fn probe_mcp_health(cfg: &RuntimeConfig) -> (bool, String) {
    if !cfg.model_profile.is_studio_family() {
        return (true, "Omitido fuera del perfil studio".to_string());
    }
    if !cfg.studio_enable_mcp {
        return (true, "MCP deshabilitado (STUDIO_ENABLE_MCP=0)".to_string());
    }
    let retries = std::env::var("STUDIO_MCP_HEALTH_RETRIES")
        .ok()
        .and_then(|s| s.trim().parse::<u32>().ok())
        .unwrap_or(5)
        .max(1);
    let port = cfg.studio_server_port;
    let mut last_err = format!("MCP no accesible en 127.0.0.1:{port}/studio/api/mcp/health");
    for attempt in 0..retries {
        match http_probe_once(port, "/studio/api/mcp/health") {
            Ok(200) => {
                let msg =
                    format!("MCP healthy en 127.0.0.1:{port}/studio/api/mcp/health (HTTP 200)");
                return (true, msg);
            }
            Ok(code) => {
                last_err = format!("HTTP {code} en 127.0.0.1:{port}/studio/api/mcp/health");
            }
            Err(e) => {
                last_err = e;
            }
        }
        if attempt + 1 < retries {
            thread::sleep(Duration::from_millis(150));
        }
    }
    (false, last_err)
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

/// Evalúa las comprobaciones S1..S10 (+ S11/S12 en perfil `studio`) sobre un `SystemSnapshot` (§6.2, `R-02`, `R-03`, `R-06`).
#[allow(clippy::too_many_lines)]
pub fn evaluate_strict_checks(cfg: &RuntimeConfig, snap: &SystemSnapshot) -> StrictReport {
    let mut checks = Vec::with_capacity(12);

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

    // S5: Espacio libre en disco >= STRICT_MIN_DISK_GB (180 / 75 / 45 / 10 GB o 5 GB con SKIP_MODEL_DOWNLOAD=1)
    #[allow(clippy::cast_precision_loss)]
    let s5_ok = snap.disk_free_gb >= (cfg.strict_min_disk_gb as f64);
    let rec_disk_flag = match cfg.model_profile {
        ModelProfile::Studio => "--disk 220",
        ModelProfile::Unreal => "--disk 250",
        _ => "--disk 120",
    };
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
                "Disco insuficiente para el perfil '{}'. Aumenta el disco en Vast.ai ({rec_disk_flag}) o usa -e MODEL_PROFILE=minimal.",
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
                "Valores válidos de MODEL_PROFILE: all | general | realistic | anime | minimal | studio | unreal."
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

    // S11: Comprobación de salud del servidor de estudio (solo con MODEL_PROFILE=studio/unreal)
    if cfg.model_profile.is_studio_family() {
        let s11_ok = snap.studio_health_ok;
        checks.push(StrictCheckItem {
            id: "S11".to_string(),
            name: "studio_health".to_string(),
            status: status_for(s11_ok, true).to_string(),
            detail: if s11_ok {
                snap.studio_health_detail.clone()
            } else {
                format!(
                    "studio-server no saludable en 127.0.0.1:{}: {}",
                    cfg.studio_server_port, snap.studio_health_detail
                )
            },
            remediation: if s11_ok {
                None
            } else {
                Some(format!(
                    "El servidor del estudio (studio-server en 127.0.0.1:{}) no respondió a la comprobación de salud. Revisa /var/log/studio-server.log y las variables STUDIO_*.",
                    cfg.studio_server_port
                ))
            },
        });

        // S12: Endpoint MCP dentro del studio-server (M3-C). Warn, no fatal:
        // un MCP caído no invalida el host (S11 ya cubre la vida del proceso).
        let s12_ok = snap.mcp_health_ok;
        checks.push(StrictCheckItem {
            id: "S12".to_string(),
            name: "mcp_health".to_string(),
            status: status_for(s12_ok, false).to_string(),
            detail: snap.mcp_health_detail.clone(),
            remediation: if s12_ok {
                None
            } else {
                Some(
                    "El endpoint MCP (/studio/api/mcp/health) no respondió. Verifica STUDIO_ENABLE_MCP y la versión del studio-server; los agentes externos no podrán dirigir el pod hasta que se restablezca."
                        .to_string(),
                )
            },
        });

        // S13: percepción vectorial dentro del studio-server (M2, M3-IDEA-035).
        // Warn, nunca fatal: desde M1.2 la percepción es degradable por diseño y el
        // resto de Studio sigue operativo con `sqlite-vec` ausente.
        let s13_ok = snap.studio_perception_ok;
        checks.push(StrictCheckItem {
            id: "S13".to_string(),
            name: "studio_perception".to_string(),
            status: if s13_ok { "ok" } else { "warn" }.to_string(),
            detail: snap.studio_perception_detail.clone(),
            remediation: if s13_ok {
                None
            } else {
                Some(
                    "La búsqueda semántica y el índice facial no están operativos en este pod. Comprueba que la extensión `sqlite-vec` esté instalada en el entorno del studio-server; el resto de Studio (proyectos, chat, renders, galería) sigue funcionando y los endpoints de percepción devolverán HTTP 503 con el motivo."
                        .to_string(),
                )
            },
        });

        // S13b: backend de percepción (`perception.backend`). Warn si "sim"
        // (modo degradado, sin pesos reales cargados).
        let s13b_sim = snap.studio_perception_backend == "sim";
        checks.push(StrictCheckItem {
            id: "S13b".to_string(),
            name: "studio_perception_backend".to_string(),
            status: if s13b_sim { "warn" } else { "ok" }.to_string(),
            detail: if s13b_sim {
                format!(
                    "percepción en modo SIMULACIÓN (no hay pesos CLIP/InsightFace/ASR/cam++ reales cargados); {}",
                    snap.studio_perception_detail
                )
            } else {
                format!(
                    "percepción en modo REAL ({})",
                    snap.studio_perception_backend
                )
            },
            remediation: if s13b_sim {
                Some(
                    "Los backends de ML reales no están disponibles en este pod. La búsqueda semántica y el índice facial usan embeddings deterministas locales (simulación). Para backends reales, instala `sqlite-vec`, `torch`, `open_clip`, `faster-whisper`, `funasr` e `insightface` en el intérprete del ComfyUI (`STUDIO_ML_PYTHON_BIN`) y los pesos en `COMFY_DATA_DIR`, o deja `STUDIO_PERCEPTION_BACKEND=sim`."
                        .to_string(),
                )
            } else {
                None
            },
        });

        // S13c: índice de percepción desactualizado (`perception.stale`). Warn si true.
        let s13c_stale = snap.studio_perception_stale;
        checks.push(StrictCheckItem {
            id: "S13c".to_string(),
            name: "studio_perception_stale".to_string(),
            status: if s13c_stale { "warn" } else { "ok" }.to_string(),
            detail: if s13c_stale {
                "índice de percepción desactualizado: las dimensiones almacenadas no coinciden con las activas (768/512). Ejecuta POST /studio/api/perception/reindex para reconstruir los embeddings."
                    .to_string()
            } else {
                "índice de percepción al día (dimensiones almacenadas coinciden con las activas).".to_string()
            },
            remediation: if s13c_stale {
                Some(
                    "Ejecuta POST /studio/api/perception/reindex en el studio-server para recrear las tablas vec_* con las dims activas y re-embeder los keyframes existentes. Los archivos PNG y studio.db no se modifican."
                        .to_string(),
                )
            } else {
                None
            },
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
    run_strict_check_cmd_with_format(cfg, false)
}

/// Ejecuta `comfyctl strict-check` (con opción `--json` por stdout) y devuelve `0` o `20`.
pub fn run_strict_check_cmd_with_format(
    cfg: &RuntimeConfig,
    json_stdout: bool,
) -> Result<i32, String> {
    let snap = SystemSnapshot::collect_live(cfg);
    let report = evaluate_strict_checks(cfg, &snap);
    if json_stdout {
        let json_bytes = serde_json::to_vec_pretty(&report)
            .map_err(|e| format!("Error serializando StrictReport: {e}"))?;
        let _ = sys::atomic_write(&cfg.strict_json_path, &json_bytes, 0o644);
        println!("{}", String::from_utf8_lossy(&json_bytes));
    } else {
        write_and_print_strict_report(cfg, &report)?;
    }
    if report.result == "fatal" {
        Ok(20)
    } else {
        Ok(0)
    }
}
