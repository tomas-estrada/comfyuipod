//! `idle` — watchdog de pod inactivo (protección contra quemado de GPU en Vast.ai).
//!
//! Variables de entorno:
//! - `COMFY_IDLE_TIMEOUT_S` (defecto 3600; `0` desactiva el watchdog).
//! - `COMFY_IDLE_ACTION` (`warn` defecto; `stop` detiene la instancia vía `vastai`).
//!
//! El pod se considera inactivo cuando la cola de `ComfyUI` (`/queue`) está vacía,
//! no hay jobs en vuelo en Studio (`jobs_active` en `/studio/api/health`) y no hay
//! ningún proceso `download-models.sh` activo. Al alcanzar el timeout: `warn`
//! escribe `/run/comfyui-idle-warn`; `stop` escribe `/run/comfyui-idle-stop` y, si
//! existen `vastai` + `VAST_API_KEY` + `VAST_INSTANCE_ID`, ejecuta
//! `vastai stop instance <id>` (la key viaja por el entorno del proceso, nunca por
//! argv ni por logs).

use crate::config::RuntimeConfig;
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IdleAction {
    Warn,
    Stop,
}

impl IdleAction {
    pub fn parse(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "stop" => Self::Stop,
            _ => Self::Warn,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IdleDecision {
    Active,
    Idle,
    Warn,
    Stop,
}

/// Decisión pura (testeable sin red ni estado): con actividad → `Active`; sin ella
/// y bajo el timeout (o watchdog off) → `Idle`; alcanzado el timeout → `Warn`/`Stop`.
pub fn decide(
    any_activity: bool,
    idle_for: Duration,
    timeout: Duration,
    action: IdleAction,
) -> IdleDecision {
    if any_activity {
        return IdleDecision::Active;
    }
    if timeout.is_zero() || idle_for < timeout {
        return IdleDecision::Idle;
    }
    match action {
        IdleAction::Warn => IdleDecision::Warn,
        IdleAction::Stop => IdleDecision::Stop,
    }
}

/// Indicadores de actividad recogidos de los servicios locales (loopback).
/// `None` = endpoint inalcanzable; se trata como "sin actividad de esa fuente".
#[derive(Clone, Debug, Default)]
pub struct IdleInputs {
    pub comfyui_queue_len: Option<u64>,
    pub studio_jobs_active: Option<u64>,
    pub download_active: bool,
}

impl IdleInputs {
    pub fn any_activity(&self) -> bool {
        self.download_active
            || self.comfyui_queue_len.is_some_and(|n| n > 0)
            || self.studio_jobs_active.is_some_and(|n| n > 0)
    }
}

fn http_get_json(port: u16, path: &str) -> Option<serde_json::Value> {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let req = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(req.as_bytes()).ok()?;
    let mut raw = Vec::new();
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
    let text = String::from_utf8_lossy(&raw);
    let body = text.split_once("\r\n\r\n").map_or("", |(_, b)| b);
    serde_json::from_str(body).ok()
}

fn comfyui_port(cfg: &RuntimeConfig) -> u16 {
    cfg.studio_comfyui_url
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .split([':', '/'])
        .find_map(|tok| tok.parse::<u16>().ok())
        .unwrap_or(18188)
}

pub fn gather_idle_inputs(cfg: &RuntimeConfig) -> IdleInputs {
    let queue = http_get_json(comfyui_port(cfg), "/queue");
    let comfyui_queue_len = queue.and_then(|v| {
        let running = v.get("queue_running")?.as_array()?.len() as u64;
        let pending = v.get("queue_pending")?.as_array()?.len() as u64;
        Some(running + pending)
    });
    let health = http_get_json(cfg.studio_server_port, "/studio/api/health");
    let studio_jobs_active = health.and_then(|v| v.get("jobs_active")?.as_u64());
    let download_active =
        crate::collect::procfs::find_process_by_cmdline("download-models.sh", "").is_some();
    IdleInputs {
        comfyui_queue_len,
        studio_jobs_active,
        download_active,
    }
}

fn write_flag(path: &str, msg: &str) {
    if let Ok(mut f) = std::fs::File::create(Path::new(path)) {
        let _ = writeln!(f, "{msg}");
    }
}

fn which(bin: &str) -> Option<std::path::PathBuf> {
    let Ok(path_var) = std::env::var("PATH") else {
        return None;
    };
    for dir in path_var.split(':') {
        let candidate = Path::new(dir).join(bin);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// `stop`: detiene la instancia Vast.ai vía CLI. La API key ya está en el entorno
/// (`VAST_API_KEY`, leído por la CLI de vast.ai); NUNCA se pasa por argv ni se loguea.
fn try_vastai_stop(cfg: &RuntimeConfig) -> bool {
    let instance_id = std::env::var("VAST_INSTANCE_ID")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let Some(instance_id) = instance_id else {
        return false;
    };
    if cfg.vast_api_key.is_none() {
        return false;
    }
    let Some(vastai) = which("vastai") else {
        return false;
    };
    match Command::new(vastai)
        .args(["stop", "instance", &instance_id])
        .status()
    {
        Ok(status) if status.success() => true,
        Ok(status) => {
            crate::supervisor::log_sup(&format!(
                "WARN: `vastai stop instance {instance_id}` terminó con {status}; se deja la bandera /run/comfyui-idle-stop."
            ));
            false
        }
        Err(e) => {
            crate::supervisor::log_sup(&format!(
                "WARN: no se pudo ejecutar `vastai stop instance {instance_id}`: {e}; se deja la bandera /run/comfyui-idle-stop."
            ));
            false
        }
    }
}

pub struct IdleWatcher {
    pub timeout: Duration,
    pub action: IdleAction,
    idle_since: Option<Instant>,
    last_check: Instant,
    warned_emitted: bool,
    stopped_emitted: bool,
}

impl IdleWatcher {
    pub fn new(cfg: &RuntimeConfig) -> Self {
        Self {
            timeout: Duration::from_secs(cfg.comfy_idle_timeout_s),
            action: IdleAction::parse(&cfg.comfy_idle_action),
            idle_since: None,
            last_check: Instant::now(),
            warned_emitted: false,
            stopped_emitted: false,
        }
    }

    pub fn enabled(&self) -> bool {
        !self.timeout.is_zero()
    }

    /// Llamar en cada tick del lazo de supervisión; se regula internamente a 5 s.
    pub fn tick(&mut self, cfg: &RuntimeConfig) -> IdleDecision {
        if !self.enabled() || self.last_check.elapsed() < Duration::from_secs(5) {
            return IdleDecision::Idle;
        }
        self.last_check = Instant::now();
        let inputs = gather_idle_inputs(cfg);
        self.observe(&inputs, cfg)
    }

    /// Núcleo testeable: alimenta indicadores ya recogidos y emite warn/stop una vez.
    pub fn observe(&mut self, inputs: &IdleInputs, cfg: &RuntimeConfig) -> IdleDecision {
        if !self.enabled() {
            return IdleDecision::Idle;
        }
        if inputs.any_activity() {
            self.idle_since = None;
            self.warned_emitted = false;
            self.stopped_emitted = false;
            return IdleDecision::Active;
        }
        let since = self.idle_since.get_or_insert_with(Instant::now);
        let decision = decide(false, since.elapsed(), self.timeout, self.action);
        match decision {
            IdleDecision::Warn if !self.warned_emitted => {
                self.warned_emitted = true;
                let secs = self.timeout.as_secs();
                crate::supervisor::log_sup(&format!(
                    "WARN: pod inactivo durante {secs}s (cola ComfyUI vacía, sin jobs de Studio, sin descargas). COMFY_IDLE_ACTION=warn; escribe /run/comfyui-idle-warn. Para auto-stop: COMFY_IDLE_ACTION=stop."
                ));
                write_flag(
                    "/run/comfyui-idle-warn",
                    &format!("idle {secs}s at {}", crate::sys::now_unix_secs()),
                );
                IdleDecision::Warn
            }
            IdleDecision::Stop if !self.stopped_emitted => {
                self.stopped_emitted = true;
                let secs = self.timeout.as_secs();
                crate::supervisor::log_sup(&format!(
                    "WARN: pod inactivo durante {secs}s; COMFY_IDLE_ACTION=stop → /run/comfyui-idle-stop + vastai stop (si aplica)."
                ));
                write_flag(
                    "/run/comfyui-idle-stop",
                    &format!("idle {secs}s at {}", crate::sys::now_unix_secs()),
                );
                let stopped = try_vastai_stop(cfg);
                if stopped {
                    crate::supervisor::log_sup(
                        "Instancia detenida vía `vastai stop instance` (COMFY_IDLE_ACTION=stop).",
                    );
                }
                IdleDecision::Stop
            }
            other => other,
        }
    }
}
