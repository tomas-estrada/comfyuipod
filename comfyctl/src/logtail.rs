//! `logtail` — Lectura eficiente de las últimas `N` líneas mediante `SeekFrom::End` por bloques de 8 KiB (O-04).

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

/// Lista blanca estricta de nombres de log permitidos en `/logs/<name>` (§5.4 / O-04).
pub const LOG_WHITELIST_NAMES: [&str; 10] = [
    "comfyui",
    "start",
    "download",
    "nginx-error",
    "nginx-access",
    "supervisor",
    "diag",
    "container",
    "ttyd",
    "studio",
];

/// Resuelve un nombre de log de la lista blanca a su ruta bajo `log_dir` (por defecto `/var/log`).
pub fn resolve_log_path(name: &str, log_dir: &Path) -> Option<PathBuf> {
    let rel = match name {
        "comfyui" => "comfyui.log",
        "start" | "container" => "start.log",
        "download" => "download-models.log",
        "nginx-error" => "nginx/error.log",
        "nginx-access" => "nginx/access.log",
        "supervisor" => "supervisor.log",
        "diag" => "diag.txt",
        "ttyd" => "ttyd.log",
        "studio" => "studio-server.log",
        _ => return None,
    };
    Some(log_dir.join(rel))
}

/// Lee las últimas `n` líneas de un fichero buscando hacia atrás en bloques de 8 KiB (O(n), O-04).
#[allow(clippy::naive_bytecount)]
pub fn tail_lines(path: &Path, n: usize) -> Vec<String> {
    if n == 0 || !path.is_file() {
        return Vec::new();
    }
    let Ok(mut file) = File::open(path) else {
        return Vec::new();
    };
    let Ok(file_size) = file.seek(SeekFrom::End(0)) else {
        return Vec::new();
    };
    if file_size == 0 {
        return Vec::new();
    }

    let block_size: u64 = 8192;
    let mut pos = file_size;
    let mut chunks: Vec<Vec<u8>> = Vec::new();
    let mut newline_count: usize = 0;

    while pos > 0 && newline_count <= n {
        let read_len = pos.min(block_size);
        pos = pos.saturating_sub(read_len);
        if file.seek(SeekFrom::Start(pos)).is_err() {
            break;
        }
        let Ok(cap) = usize::try_from(read_len) else {
            break;
        };
        let mut buf = vec![0_u8; cap];
        if file.read_exact(&mut buf).is_err() {
            break;
        }
        newline_count = newline_count.saturating_add(buf.iter().filter(|&&b| b == b'\n').count());
        chunks.push(buf);
    }

    chunks.reverse();
    let total_len: usize = chunks.iter().map(Vec::len).sum();
    let mut combined = Vec::with_capacity(total_len);
    for ch in chunks {
        combined.extend_from_slice(&ch);
    }

    let text = String::from_utf8_lossy(&combined);
    let all_lines: Vec<&str> = text.lines().collect();
    let skip_count = all_lines.len().saturating_sub(n);
    all_lines
        .into_iter()
        .skip(skip_count)
        .map(|s| s.trim_end_matches('\r').to_string())
        .collect()
}
