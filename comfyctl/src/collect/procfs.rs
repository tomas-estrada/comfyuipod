//! `collect::procfs` — Lectores de `/proc/meminfo`, `/proc/<pid>/status`, `statvfs`,
//! sondas TCP locales, `/etc/comfyui/image.json` y `/run/comfyui-supervisor.json`.

use crate::config::RuntimeConfig;
use crate::strict::StrictReport;
use crate::supervisor::{LastExitInfo, SupervisorState};
use crate::sys;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImageMeta {
    pub tag: String,
    pub built_at: String,
    pub comfyui_sha: String,
    pub pytorch: String,
    pub cuda: String,
    pub ubuntu: String,
}

impl Default for ImageMeta {
    fn default() -> Self {
        Self {
            tag: "unknown".to_string(),
            built_at: "unknown".to_string(),
            comfyui_sha: "unknown".to_string(),
            pytorch: "unknown".to_string(),
            cuda: "12.8".to_string(),
            ubuntu: "24.04".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstanceInfo {
    pub hostname: String,
    pub uptime_s: u64,
    pub vast_instance_id: String,
    pub external_port_8188: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryStatus {
    pub total_mb: u64,
    pub used_mb: u64,
    pub available_mb: u64,
    pub swap_used_mb: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiskStatus {
    pub path: String,
    pub total_gb: f64,
    pub used_gb: f64,
    pub free_gb: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComfyUiStatus {
    pub state: String,
    pub supervisor_state: String,
    pub pid: Option<u32>,
    pub rss_mb: u64,
    pub uptime_s: u64,
    pub port_open: bool,
    pub restarts: u32,
    pub healthy_resets: u32,
    pub max_restarts: u32,
    pub last_exit: Option<LastExitInfo>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NginxServiceStatus {
    pub running: bool,
    pub workers_user: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusServerServiceStatus {
    pub running: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalServiceStatus {
    pub enabled: bool,
    pub running: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServicesStatus {
    pub nginx: NginxServiceStatus,
    pub status_server: StatusServerServiceStatus,
    pub terminal: TerminalServiceStatus,
}

pub fn load_image_meta(path: &Path) -> ImageMeta {
    if path.is_file() {
        if let Ok(txt) = fs::read_to_string(path) {
            if let Ok(meta) = serde_json::from_str::<ImageMeta>(&txt) {
                return meta;
            }
        }
    }
    ImageMeta::default()
}

pub fn load_strict_report(path: &Path) -> Option<StrictReport> {
    if path.is_file() {
        if let Ok(txt) = fs::read_to_string(path) {
            if let Ok(rep) = serde_json::from_str::<StrictReport>(&txt) {
                return Some(rep);
            }
        }
    }
    None
}

pub fn get_container_uptime_s(started_at_path: &Path) -> u64 {
    if started_at_path.is_file() {
        if let Ok(txt) = fs::read_to_string(started_at_path) {
            if let Ok(started_ts) = txt.trim().parse::<i64>() {
                let now = sys::now_unix_secs();
                return u64::try_from(now.saturating_sub(started_ts)).unwrap_or(0);
            }
        }
    }
    0
}

pub fn get_hostname() -> String {
    if let Ok(txt) = fs::read_to_string("/etc/hostname") {
        let trimmed = txt.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }
    std::env::var("HOSTNAME").unwrap_or_else(|_| "comfyuipod".to_string())
}

pub fn parse_proc_meminfo(txt: &str) -> MemoryStatus {
    let mut mem_total_kb: u64 = 0;
    let mut mem_avail_kb: u64 = 0;
    let mut swap_total_kb: u64 = 0;
    let mut swap_free_kb: u64 = 0;

    for line in txt.lines() {
        let mut parts = line.split_whitespace();
        let Some(key) = parts.next() else { continue };
        let val = parts
            .next()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(0);
        match key {
            "MemTotal:" => mem_total_kb = val,
            "MemAvailable:" => mem_avail_kb = val,
            "SwapTotal:" => swap_total_kb = val,
            "SwapFree:" => swap_free_kb = val,
            _ => {}
        }
    }

    let total_mb = mem_total_kb / 1024;
    let available_mb = mem_avail_kb / 1024;
    let used_mb = total_mb.saturating_sub(available_mb);
    let swap_used_mb = swap_total_kb.saturating_sub(swap_free_kb) / 1024;

    MemoryStatus {
        total_mb,
        used_mb,
        available_mb,
        swap_used_mb,
    }
}

pub fn collect_memory_and_disk(comfy_data_dir: &Path) -> (MemoryStatus, DiskStatus) {
    let memory = fs::read_to_string("/proc/meminfo").ok().map_or(
        MemoryStatus {
            total_mb: 0,
            used_mb: 0,
            available_mb: 0,
            swap_used_mb: 0,
        },
        |txt| parse_proc_meminfo(&txt),
    );

    let target_dir = if comfy_data_dir.exists() {
        comfy_data_dir
    } else if Path::new("/workspace").exists() {
        Path::new("/workspace")
    } else {
        Path::new("/")
    };

    let (total_gb, used_gb, free_gb) = sys::disk_stats_gb(target_dir).unwrap_or((0.0, 0.0, 0.0));
    let disk = DiskStatus {
        path: target_dir.to_string_lossy().into_owned(),
        total_gb,
        used_gb,
        free_gb,
    };

    (memory, disk)
}

pub fn check_tcp_port(port: u16, timeout_ms: u64) -> bool {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms)).is_ok()
}

pub fn read_proc_rss_mb(pid: u32) -> u64 {
    let status_path = format!("/proc/{pid}/status");
    let Ok(txt) = fs::read_to_string(status_path) else {
        return 0;
    };
    for line in txt.lines() {
        if let Some(rest) = line.strip_prefix("VmRSS:") {
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

pub fn find_process_by_cmdline(needle1: &str, needle2: &str) -> Option<(u32, u64)> {
    let Ok(entries) = fs::read_dir("/proc") else {
        return None;
    };
    for entry in entries.flatten() {
        let fname = entry.file_name();
        let Some(pid_str) = fname.to_str() else {
            continue;
        };
        let Ok(pid) = pid_str.parse::<u32>() else {
            continue;
        };
        let cmdline_path = entry.path().join("cmdline");
        let Ok(bytes) = fs::read(cmdline_path) else {
            continue;
        };
        if bytes.is_empty() {
            continue;
        }
        let cmdline: String = bytes
            .into_iter()
            .map(|b| if b == 0 { ' ' } else { char::from(b) })
            .collect();
        if cmdline.contains(needle1) && (needle2.is_empty() || cmdline.contains(needle2)) {
            let rss_mb = read_proc_rss_mb(pid);
            return Some((pid, rss_mb));
        }
    }
    None
}

pub fn collect_comfyui_status(cfg: &RuntimeConfig) -> ComfyUiStatus {
    let sup: SupervisorState = if cfg.supervisor_json_path.is_file() {
        fs::read_to_string(&cfg.supervisor_json_path)
            .ok()
            .and_then(|txt| serde_json::from_str::<SupervisorState>(&txt).ok())
            .unwrap_or_else(|| SupervisorState::new(cfg.comfy_max_restarts))
    } else {
        SupervisorState::new(cfg.comfy_max_restarts)
    };

    let port_open = check_tcp_port(18188, 300);

    let mut pid = sup.pid;
    if let Some(p) = pid {
        if !Path::new(&format!("/proc/{p}")).exists() {
            pid = None;
        }
    }
    if pid.is_none() {
        if let Some((found_pid, _)) = find_process_by_cmdline("main.py", "18188") {
            pid = Some(found_pid);
        }
    }

    let rss_mb = pid.map_or(0, read_proc_rss_mb);
    let uptime_s = if pid.is_some() {
        sup.started_at.map_or(0, |st| {
            u64::try_from(sys::now_unix_secs().saturating_sub(st)).unwrap_or(0)
        })
    } else {
        0
    };

    let state = if port_open {
        "running".to_string()
    } else if sup.state == "exhausted" || sup.state == "fatal" {
        "down".to_string()
    } else if pid.is_some() || sup.state == "starting" {
        "starting".to_string()
    } else {
        "down".to_string()
    };

    ComfyUiStatus {
        state,
        supervisor_state: sup.state,
        pid,
        rss_mb,
        uptime_s,
        port_open,
        restarts: sup.restarts,
        healthy_resets: sup.healthy_resets,
        max_restarts: sup.max,
        last_exit: sup.last_exit,
    }
}

pub fn collect_services_status(cfg: &RuntimeConfig) -> ServicesStatus {
    let mut nginx_running = false;
    let mut workers_user = "none".to_string();

    let mut ps_cmd = Command::new("ps");
    ps_cmd.args(["-o", "user=", "-C", "nginx"]);
    if let Ok((Some(0), txt, _, false)) =
        sys::run_command_with_timeout(&mut ps_cmd, Duration::from_secs(5))
    {
        let users: Vec<&str> = txt
            .lines()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        if !users.is_empty() {
            nginx_running = true;
            if users.contains(&"www-data") {
                workers_user = "www-data".to_string();
            } else if let Some(first) = users.first() {
                workers_user = (*first).to_string();
            }
        }
    }

    let status_srv_running = check_tcp_port(cfg.status_server_port, 100);

    let term_running = if cfg.enable_web_terminal {
        check_tcp_port(7681, 200)
    } else {
        false
    };

    ServicesStatus {
        nginx: NginxServiceStatus {
            running: nginx_running,
            workers_user,
        },
        status_server: StatusServerServiceStatus {
            running: status_srv_running,
        },
        terminal: TerminalServiceStatus {
            enabled: cfg.enable_web_terminal,
            running: term_running,
        },
    }
}

pub fn build_masked_env(cfg: &RuntimeConfig) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    map.insert(
        "MODEL_PROFILE".to_string(),
        cfg.model_profile.as_str().to_string(),
    );
    map.insert("COMFY_USER".to_string(), cfg.comfy_user.clone());
    map.insert("COMFY_PASS".to_string(), cfg.masked_pass_status());
    map.insert("HF_TOKEN".to_string(), cfg.masked_hf_status());
    map.insert(
        "COMFYUI_EXTRA_ARGS".to_string(),
        cfg.comfy_extra_args.clone(),
    );
    map.insert(
        "HF_HUB_DISABLE_XET".to_string(),
        cfg.hf_hub_disable_xet
            .clone()
            .unwrap_or_else(|| "<no definido>".to_string()),
    );
    map.insert(
        "ENABLE_WEB_TERMINAL".to_string(),
        if cfg.enable_web_terminal { "1" } else { "0" }.to_string(),
    );
    map
}
