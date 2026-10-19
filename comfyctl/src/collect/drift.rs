//! `collect::drift` — Deriva de paquetes Python (`pip freeze` vs `/etc/comfyui/requirements.lock`)
//! y custom nodes (`git rev-parse HEAD` / `status --porcelain` vs `/etc/comfyui/nodes.lock`).

use crate::redact::SecretRedactor;
use crate::sys;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone, Default)]
pub struct DriftSummary {
    pub pip_added: Vec<String>,
    pub pip_removed: Vec<String>,
    pub node_drift: Vec<String>,
    pub nodes_lock_count: usize,
}

fn run_cmd_capture(args: &[&str], redactor: &SecretRedactor) -> Option<String> {
    let first = args.first()?;
    let rest = args.get(1..).unwrap_or(&[]);
    let mut cmd = Command::new(first);
    cmd.args(rest);
    let (code, stdout, stderr, timed_out) =
        sys::run_command_with_timeout(&mut cmd, Duration::from_secs(10)).ok()?;
    if timed_out || code != Some(0) {
        return None;
    }
    let mut combined = stdout;
    if !stderr.is_empty() {
        combined.push_str(&stderr);
    }
    Some(redactor.redact(combined.trim()))
}

pub fn collect_drift(
    comfy_app_dir: &Path,
    requirements_lock_path: &Path,
    nodes_lock_path: &Path,
    redactor: &SecretRedactor,
) -> DriftSummary {
    let lock_lines: BTreeSet<String> = if requirements_lock_path.is_file() {
        fs::read_to_string(requirements_lock_path)
            .unwrap_or_default()
            .lines()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(ToString::to_string)
            .collect()
    } else {
        BTreeSet::new()
    };

    let curr_lines: BTreeSet<String> = if lock_lines.is_empty() {
        BTreeSet::new()
    } else {
        run_cmd_capture(&["pip3", "freeze"], redactor)
            .unwrap_or_default()
            .lines()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(ToString::to_string)
            .collect()
    };

    let pip_added: Vec<String> = if !lock_lines.is_empty() && !curr_lines.is_empty() {
        curr_lines.difference(&lock_lines).cloned().collect()
    } else {
        Vec::new()
    };
    let pip_removed: Vec<String> = if !lock_lines.is_empty() && !curr_lines.is_empty() {
        lock_lines.difference(&curr_lines).cloned().collect()
    } else {
        Vec::new()
    };

    let mut nodes_lock: BTreeMap<String, String> = BTreeMap::new();
    if nodes_lock_path.is_file() {
        if let Ok(txt) = fs::read_to_string(nodes_lock_path) {
            for line in txt.lines() {
                let mut parts = line.split_whitespace();
                let kind = parts.next();
                let name = parts.next();
                let _url = parts.next();
                let sha = parts.next();
                if let (Some("node"), Some(n), Some(s)) = (kind, name, sha) {
                    nodes_lock.insert(n.to_string(), s.to_string());
                }
            }
        }
    }
    let nodes_lock_count = if nodes_lock.is_empty() {
        10
    } else {
        nodes_lock.len()
    };

    let mut node_drift = Vec::new();
    let custom_nodes_dir = comfy_app_dir.join("custom_nodes");
    if custom_nodes_dir.is_dir() {
        let mut child_dirs = Vec::new();
        if let Ok(entries) = fs::read_dir(&custom_nodes_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if let Some(name) = path.file_name().and_then(|s| s.to_str()) {
                    if path.is_dir() && !name.starts_with('.') && name != "__pycache__" {
                        child_dirs.push((name.to_string(), path));
                    }
                }
            }
        }
        child_dirs.sort_by(|a, b| a.0.cmp(&b.0));

        for (name, path) in child_dirs {
            let path_str = path.to_string_lossy();
            let curr_sha =
                run_cmd_capture(&["git", "-C", &path_str, "rev-parse", "HEAD"], redactor)
                    .unwrap_or_else(|| "no-git".to_string());
            let dirty_out =
                run_cmd_capture(&["git", "-C", &path_str, "status", "--porcelain"], redactor)
                    .unwrap_or_default();
            let dirty = if dirty_out.trim().is_empty() {
                ""
            } else {
                " [DIRTY]"
            };
            let curr_short: String = curr_sha.chars().take(12).collect();

            match nodes_lock.get(&name) {
                None => {
                    node_drift.push(format!(
                        "- `{name}`: `{curr_short}` (EXTRA — instalado fuera de nodes.lock){dirty}"
                    ));
                }
                Some(expected_sha) => {
                    if !curr_sha.starts_with(expected_sha) && !expected_sha.starts_with(&curr_sha) {
                        let exp_short: String = expected_sha.chars().take(12).collect();
                        node_drift.push(format!(
                            "- `{name}`: actual `{curr_short}` != lock `{exp_short}`{dirty}"
                        ));
                    } else if !dirty.is_empty() {
                        node_drift.push(format!("- `{name}`: `{curr_short}`{dirty}"));
                    }
                }
            }
        }
    }

    DriftSummary {
        pip_added,
        pip_removed,
        node_drift,
        nodes_lock_count,
    }
}
