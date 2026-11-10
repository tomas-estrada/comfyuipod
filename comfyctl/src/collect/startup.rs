//! `collect::startup` — Causa de arranque y de salida, visible por HTTP sin entrar
//! al pod. Casos de campo: el entrypoint nunca ejecutó `start.sh` (`onstart.log`
//! vacío) y deja a `ComfyUI` «caído» sin causa legible; o `start.sh` corrió y no se
//! llegó a escribir `/run/comfyui-supervisor.json`.
//!
//! Todo sale de ficheros reales (log del entrypoint y JSON del supervisor); si no
//! se pudo medir, el campo es «no disponible» y nunca un valor fijo.

use crate::logtail::tail_lines;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq, Eq)]
pub struct StartupStatus {
    /// Estado de `onstart.log`: «vacío», «N líneas» o «no disponible» (ilegible).
    pub onstart: String,
    /// `/run/comfyui-supervisor.json`: «presente» o «ausente»; «no disponible» si
    /// no se pudo comprobar.
    pub supervisor_json: String,
    /// Causa derivada de las dos evidencias anteriores. Sin medición: «no disponible».
    pub cause: String,
}

const SUPERVISOR_JSON_NAME: &str = "comfyui-supervisor.json";
const CAUSE_RUNNING: &str = "arranque en marcha (start.sh dejó supervisor)";

/// Constructor de tests/serie: dibuja el estado medido sin ficheros reales
/// (entrypoint sin start.sh: `onstart.log` vacío y sin supervisor en `/run`).
#[must_use]
pub fn startup_failed_entrypoint() -> StartupStatus {
    StartupStatus {
        onstart: "vacío".to_string(),
        supervisor_json: "ausente".to_string(),
        cause:
            "el entrypoint no ejecutó start.sh: onstart.log vacío y sin /run/comfyui-supervisor.json"
                .to_string(),
    }
}

/// Sin medición posible (p. ej. sin `log_dir`): nada inventado que publicar.
#[must_use]
pub fn startup_unmeasured() -> StartupStatus {
    StartupStatus {
        onstart: "no disponible".to_string(),
        supervisor_json: "no disponible".to_string(),
        cause: "no disponible (sin evidencia medible de arranque)".to_string(),
    }
}

/// Mide las dos evidencias de arrancado y deriva la causa en texto legible.
pub fn collect_startup_status(log_dir: &Path, run_dir: &Path) -> StartupStatus {
    let onstart_path = log_dir.join("onstart.log");
    let onstart = if onstart_path.exists() {
        match fs::read_to_string(&onstart_path) {
            Ok(txt) => {
                let lines = txt.lines().filter(|l| !l.trim().is_empty()).count();
                if lines == 0 {
                    "vacío".to_string()
                } else {
                    format!("{lines} líneas")
                }
            }
            Err(_) => "no disponible".to_string(),
        }
    } else {
        "vacío".to_string()
    };

    let supervisor_path = run_dir.join(SUPERVISOR_JSON_NAME);
    let supervisor_json = if supervisor_path.exists() {
        let probe = tail_lines(&supervisor_path, 1);
        if probe.is_empty() {
            "ausente".to_string()
        } else {
            "presente".to_string()
        }
    } else {
        "ausente".to_string()
    };

    let cause = if supervisor_json == "presente" {
        CAUSE_RUNNING.to_string()
    } else if onstart == "no disponible" {
        "no disponible: onstart.log ilegible y sin /run/comfyui-supervisor.json".to_string()
    } else if onstart == "vacío" {
        "el entrypoint no ejecutó start.sh: onstart.log vacío y sin /run/comfyui-supervisor.json"
            .to_string()
    } else {
        format!(
            "start.sh corrió pero no hay /run/comfyui-supervisor.json ({onstart}); \
             el arranque no llegó a lanzar el supervisor"
        )
    };

    StartupStatus {
        onstart,
        supervisor_json,
        cause,
    }
}
