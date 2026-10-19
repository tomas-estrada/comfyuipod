#![allow(unsafe_code)]
//! `sys` — Único módulo autorizado a contener bloques `unsafe` (§4.4).
//! Encapsula llamadas POSIX (`statvfs`, `kill`, manejadores `SIGTERM`/`SIGINT`),
//! escritura atómica (`tmp` + `fsync` + `rename`) y formato de fechas UTC (`chrono`).

use chrono::{DateTime, Datelike, Timelike};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

#[cfg(unix)]
extern "C" fn signal_handler(_sig: libc::c_int) {
    SHUTDOWN_REQUESTED.store(true, Ordering::SeqCst);
}

/// Instala manejadores POSIX para `SIGTERM` y `SIGINT`.
pub fn install_signal_handlers() {
    #[cfg(unix)]
    {
        // SAFETY: `signal_handler` es async-signal-safe (solo escribe en un `AtomicBool` estático).
        unsafe {
            let fn_ptr = signal_handler as *const () as libc::sighandler_t;
            let _ = libc::signal(libc::SIGTERM, fn_ptr);
            let _ = libc::signal(libc::SIGINT, fn_ptr);
        }
    }
}

/// Indica si se ha recibido `SIGTERM` o `SIGINT`.
pub fn is_shutdown_requested() -> bool {
    SHUTDOWN_REQUESTED.load(Ordering::SeqCst)
}

/// Permite activar o limpiar la bandera de apagado en tests.
pub fn set_shutdown_requested(val: bool) {
    SHUTDOWN_REQUESTED.store(val, Ordering::SeqCst);
}

/// Envía una señal POSIX a un PID (`SIGTERM` = 15, `SIGKILL` = 9).
pub fn send_signal(pid: u32, sig: i32) -> Result<(), String> {
    #[cfg(unix)]
    {
        let Ok(pid_t) = libc::pid_t::try_from(pid) else {
            return Err(format!("PID fuera de rango: {pid}"));
        };
        // SAFETY: `libc::kill` es seguro para cualquier `pid_t > 0` y señal estándar.
        let rc = unsafe { libc::kill(pid_t, sig) };
        if rc == 0 {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error().to_string())
        }
    }
    #[cfg(not(unix))]
    {
        let _ = (pid, sig);
        Ok(())
    }
}

/// Comprueba si un proceso sigue vivo (`kill(pid, 0)`).
pub fn is_pid_alive(pid: u32) -> bool {
    #[cfg(unix)]
    {
        let Ok(pid_t) = libc::pid_t::try_from(pid) else {
            return false;
        };
        // SAFETY: `kill(pid, 0)` no envía señal; solo verifica existencia/permisos del proceso.
        unsafe { libc::kill(pid_t, 0) == 0 }
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        false
    }
}

/// Obtiene `(total_gb, used_gb, free_gb)` de un punto de montaje usando `libc::statvfs`.
#[allow(clippy::cast_precision_loss)]
pub fn disk_stats_gb(path: &Path) -> Result<(f64, f64, f64), String> {
    #[cfg(unix)]
    {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;

        let c_path = CString::new(path.as_os_str().as_bytes())
            .map_err(|e| format!("Ruta inválida para statvfs: {e}"))?;
        // SAFETY: `statvfs` inicializa completamente la estructura `libc::statvfs` cuando retorna 0.
        unsafe {
            let mut vfs: libc::statvfs = std::mem::zeroed();
            if libc::statvfs(c_path.as_ptr(), &mut vfs) != 0 {
                return Err(std::io::Error::last_os_error().to_string());
            }
            let frsize = if vfs.f_frsize > 0 {
                vfs.f_frsize
            } else {
                vfs.f_bsize
            };
            let total_bytes = u128::from(vfs.f_blocks) * u128::from(frsize);
            let free_avail_bytes = u128::from(vfs.f_bavail) * u128::from(frsize);
            let free_all_bytes = u128::from(vfs.f_bfree) * u128::from(frsize);
            let used_bytes = total_bytes.saturating_sub(free_all_bytes);

            let gib = 1024.0_f64 * 1024.0 * 1024.0;
            let round1 = |b: u128| ((b as f64 / gib) * 10.0).round() / 10.0;
            Ok((
                round1(total_bytes),
                round1(used_bytes),
                round1(free_avail_bytes),
            ))
        }
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok((150.0, 30.0, 120.0))
    }
}

/// Escribe un fichero de forma atómica (`<path>.tmp` + `sync_all` + `rename`).
pub fn atomic_write(path: &Path, contents: &[u8], mode: u32) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("Error creando directorio {}: {e}", parent.display()))?;
        }
    }
    let mut tmp_os = path.as_os_str().to_os_string();
    tmp_os.push(".tmp");
    let tmp_path = PathBuf::from(tmp_os);

    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&tmp_path)
        .map_err(|e| format!("Error abriendo {}: {e}", tmp_path.display()))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = fs::Permissions::from_mode(mode);
        let _ = file.set_permissions(perms);
    }
    #[cfg(not(unix))]
    {
        let _ = mode;
    }

    file.write_all(contents)
        .map_err(|e| format!("Error escribiendo {}: {e}", tmp_path.display()))?;
    file.sync_all()
        .map_err(|e| format!("Error en fsync de {}: {e}", tmp_path.display()))?;
    drop(file);

    fs::rename(&tmp_path, path).map_err(|e| {
        format!(
            "Error renombrando {} -> {}: {e}",
            tmp_path.display(),
            path.display()
        )
    })
}

/// Lee `/run/comfyui-pass` (o ruta equivalente) y lo borra inmediatamente (`unlink`, §9).
pub fn read_and_unlink_secret(path: &Path) -> Option<String> {
    if !path.is_file() {
        return None;
    }
    let raw = fs::read_to_string(path).ok()?;
    let _ = fs::remove_file(path);
    let trimmed = raw.trim().to_string();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    }
}

/// Devuelve el epoch Unix actual en segundos.
pub fn now_unix_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| i64::try_from(d.as_secs()).unwrap_or(0))
}

/// Formatea un timestamp Unix en ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`).
pub fn format_utc_iso8601(epoch_secs: i64) -> String {
    let dt = DateTime::from_timestamp(epoch_secs, 0).unwrap_or(DateTime::UNIX_EPOCH);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

/// Formatea un timestamp Unix para logs (`YYYY-MM-DD HH:MM:SS`).
pub fn format_utc_log_ts(epoch_secs: i64) -> String {
    let dt = DateTime::from_timestamp(epoch_secs, 0).unwrap_or(DateTime::UNIX_EPOCH);
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}
