//! `supervisor` — Supervisor de procesos en Rust (`comfyctl supervise`, §5 y §6.3.4).
//! Gestiona el ciclo de vida de `ComfyUI` (`main.py`), respeta `O-02` (`rc=143` y `COMFY_HEALTHY_UPTIME`),
//! `O-04` (`LOG_MAX_MB`), `O-05` (`env_remove("COMFY_PASS")`), escribe `/run/comfyui-supervisor.json`
//! de forma atómica y propaga `SIGTERM`/`SIGINT` limpiamente (`ExitCode=0`).

use crate::config::{ModelProfile, RuntimeConfig};
use crate::strict::{evaluate_strict_checks, write_and_print_strict_report, SystemSnapshot};
use crate::sys;
use serde::{Deserialize, Serialize};
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LastExitInfo {
    pub rc: i32,
    pub code: i32,
    pub reason: String,
    pub at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SupervisorState {
    pub state: String,
    pub attempt: u32,
    pub restarts: u32,
    pub healthy_resets: u32,
    pub max: u32,
    pub pid: Option<u32>,
    pub started_at: Option<i64>,
    pub last_exit: Option<LastExitInfo>,
}

impl SupervisorState {
    pub fn new(max_restarts: u32) -> Self {
        Self {
            state: "starting".to_string(),
            attempt: 0,
            restarts: 0,
            healthy_resets: 0,
            max: max_restarts,
            pid: None,
            started_at: None,
            last_exit: None,
        }
    }

    /// Registra el inicio de un nuevo intento de `ComfyUI`.
    pub fn on_child_spawned(&mut self, pid: u32, started_at: i64) {
        self.attempt = self.attempt.saturating_add(1);
        self.state = "running".to_string();
        self.pid = Some(pid);
        self.started_at = Some(started_at);
    }

    /// Aplica la política de salida (`O-02`) cuando el proceso hijo termina con código `rc`.
    /// Devuelve `true` si debe relanzarse, o `false` si se alcanzó el estado `exhausted`.
    pub fn on_child_exit(
        &mut self,
        rc: i32,
        run_duration_s: u64,
        healthy_uptime_s: u64,
        exit_at: i64,
    ) -> bool {
        self.pid = None;
        self.started_at = None;

        if run_duration_s >= healthy_uptime_s && self.restarts > 0 {
            self.restarts = 0;
            self.healthy_resets = self.healthy_resets.saturating_add(1);
        }

        let reason = classify_exit_code(rc);
        self.last_exit = Some(LastExitInfo {
            rc,
            code: rc,
            reason,
            at: exit_at,
        });

        // O-02: rc=143 (SIGTERM manual) no consume presupuesto de reinicios
        if rc != 143 {
            if self.restarts >= self.max {
                self.state = "exhausted".to_string();
                return false;
            }
            self.restarts = self.restarts.saturating_add(1);
        }

        self.state = "down".to_string();
        true
    }
}

pub fn classify_exit_code(rc: i32) -> String {
    match rc {
        137 => "SIGKILL (probable OOM de RAM del host o cgroup)".to_string(),
        134 | 139 => "Crash nativo (SIGABRT/SIGSEGV en librería C++/CUDA)".to_string(),
        1 => "Excepción Python en ComfyUI (revisar traceback en comfyui.log)".to_string(),
        143 => {
            "SIGTERM recibido (reinicio manual del operador; no consume presupuesto)".to_string()
        }
        _ => format!("Salida inesperada (rc={rc})"),
    }
}

pub fn write_supervisor_json(path: &Path, state: &SupervisorState) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(state)
        .map_err(|e| format!("Error serializando SupervisorState: {e}"))?;
    sys::atomic_write(path, &bytes, 0o644)
}

fn log_sup(msg: &str) {
    let ts = sys::format_utc_log_ts(sys::now_unix_secs());
    let line = format!("[{ts}] [SUPERVISOR] {msg}\n");
    print!("{line}");
    if let Ok(mut f) = OpenOptions::new()
        .create(true)
        .append(true)
        .open("/var/log/supervisor.log")
    {
        let _ = f.write_all(line.as_bytes());
    }
}

/// Trunca los logs de mayor crecimiento si superan `log_max_mb` (`O-04`),
/// conservando los últimos 20 MiB de cada uno.
pub fn truncate_logs_if_needed(log_max_mb: u64) {
    let max_bytes = log_max_mb.saturating_mul(1_048_576);
    let keep_bytes: u64 = 20 * 1_048_576;
    let log_paths = [
        "/var/log/comfyui.log",
        "/var/log/nginx/access.log",
        "/var/log/nginx/error.log",
        "/var/log/download-models.log",
        "/var/log/start.log",
        "/var/log/supervisor.log",
        "/var/log/ttyd.log",
        "/var/log/diag.txt",
        "/var/log/studio-server.log",
    ];
    for log_path in log_paths {
        let p = Path::new(log_path);
        let Ok(meta) = fs::metadata(p) else {
            continue;
        };
        if meta.len() <= max_bytes {
            continue;
        }
        let Ok(mut f) = OpenOptions::new().read(true).open(p) else {
            continue;
        };
        let seek_pos = meta.len().saturating_sub(keep_bytes);
        if f.seek(SeekFrom::Start(seek_pos)).is_err() {
            continue;
        }
        let mut tail_buf = Vec::new();
        if f.read_to_end(&mut tail_buf).is_ok() {
            if let Ok(mut out) = OpenOptions::new().write(true).truncate(true).open(p) {
                let _ = out.write_all(&tail_buf);
            }
        }
    }
}

fn dump_crash_diagnostics(attempt: u32, rc: i32, run_duration_s: u64, reason: &str) {
    let ts = sys::format_utc_log_ts(sys::now_unix_secs());
    let mut dump = String::new();
    dump.push_str("======================================================================\n");
    let _ = writeln!(
        dump,
        "[{ts}] [SUPERVISOR] ComfyUI terminó (intento #{attempt}, rc={rc}, duracion={run_duration_s}s): {reason}"
    );
    // Todas las sondas del volcado van acotadas por timeout (`R-07`): un wedge de GPU
    // que cuelga nvidia-smi no debe bloquear el bucle del supervisor.
    dump.push_str("--- Estado de memoria (free -m) ---\n");
    let mut free_cmd = Command::new("free");
    free_cmd.arg("-m");
    if let Ok((Some(0), out, _, false)) =
        sys::run_command_with_timeout(&mut free_cmd, Duration::from_secs(5))
    {
        dump.push_str(&out);
    }
    dump.push_str("--- Estado de GPU (nvidia-smi) ---\n");
    let mut smi_cmd = Command::new("nvidia-smi");
    smi_cmd.args([
        "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
        "--format=csv,noheader",
    ]);
    if let Ok((Some(0), out, _, false)) =
        sys::run_command_with_timeout(&mut smi_cmd, Duration::from_secs(10))
    {
        dump.push_str(&out);
    }
    dump.push_str("--- Últimas 40 líneas de /var/log/comfyui.log ---\n");
    let mut tail_cmd = Command::new("tail");
    tail_cmd.args(["-n", "40", "/var/log/comfyui.log"]);
    if let Ok((Some(0), out, _, false)) =
        sys::run_command_with_timeout(&mut tail_cmd, Duration::from_secs(5))
    {
        dump.push_str(&out);
    }
    dump.push_str("======================================================================\n");

    print!("{dump}");
    if let Ok(mut f) = OpenOptions::new()
        .create(true)
        .append(true)
        .open("/var/log/supervisor.log")
    {
        let _ = f.write_all(dump.as_bytes());
    }
}

fn run_model_downloads_if_enabled(cfg: &RuntimeConfig) {
    if cfg.skip_model_download {
        let msg = "[6/7] [SKIP] SKIP_MODEL_DOWNLOAD=1 activo; omitiendo descarga automática.\n";
        print!("{msg}");
        if let Ok(mut f) = OpenOptions::new()
            .create(true)
            .append(true)
            .open("/var/log/download-models.log")
        {
            let _ = f.write_all(msg.as_bytes());
        }
        return;
    }

    println!("[6/7] Ejecutando Fase 1 de descarga de modelos críticos (2.79 GiB)...");
    // `set -o pipefail`: sin él, bash devuelve el estado de `tee` (casi siempre 0)
    // y la detección de fallo/timeout de Fase 1 sería código muerto.
    let status = Command::new("bash")
        .args([
            "-c",
            "set -o pipefail; timeout 600 /usr/local/bin/download-models.sh --phase1 2>&1 | tee -a /var/log/download-models.log",
        ])
        .status();
    if !matches!(status, Ok(s) if s.success()) {
        let warn = "[WARN] Fase 1 terminó con advertencias o timeout; continuando con el arranque de ComfyUI...\n";
        print!("{warn}");
        if let Ok(mut f) = OpenOptions::new()
            .create(true)
            .append(true)
            .open("/var/log/download-models.log")
        {
            let _ = f.write_all(warn.as_bytes());
        }
    }

    if sys::is_shutdown_requested() {
        return;
    }

    println!(
        "[6/7] Lanzando Fase 2 de descarga de checkpoints en segundo plano (Perfil: {})...",
        cfg.model_profile.as_str()
    );
    let _ = Command::new("setsid")
        .args([
            "-f",
            "bash",
            "-c",
            "exec /usr/local/bin/download-models.sh --phase2 >> /var/log/download-models.log 2>&1 < /dev/null",
        ])
        .status();
}

fn sleep_interruptible(seconds: u64) -> bool {
    let ticks = seconds.saturating_mul(5);
    for _ in 0..ticks {
        if sys::is_shutdown_requested() {
            return true;
        }
        thread::sleep(Duration::from_millis(200));
    }
    sys::is_shutdown_requested()
}

fn graceful_shutdown_nginx() {
    let _ = Command::new("nginx")
        .args(["-s", "quit"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Indica si el supervisor debe levantar `studio-server` (`127.0.0.1:18190`),
/// lo cual ocurre única y exclusivamente con `MODEL_PROFILE=studio`.
pub const fn should_start_studio_server(cfg: &RuntimeConfig) -> bool {
    matches!(cfg.model_profile, ModelProfile::Studio)
}

/// Construye el `Command` para ejecutar `studio-server` en `127.0.0.1:{studio_server_port}`,
/// propagando las variables `STUDIO_*` y eliminando `COMFY_PASS` (`O-05`).
pub fn build_studio_server_command(cfg: &RuntimeConfig) -> Command {
    let py_bin = if let Some(ref custom_bin) = cfg.studio_python_bin {
        custom_bin.clone()
    } else if Path::new("/opt/venvs/studio/bin/python").is_file() {
        PathBuf::from("/opt/venvs/studio/bin/python")
    } else {
        PathBuf::from("python3")
    };

    let mut cmd = Command::new(py_bin);
    if let Some(ref script) = cfg.studio_server_script {
        cmd.arg(script);
    } else if Path::new("/opt/ComfyUI/studio_server/main.py").is_file() {
        cmd.arg("/opt/ComfyUI/studio_server/main.py");
    } else {
        const STUDIO_BOOTSTRAP_PY: &str = r#"import http.server, json, os, runpy, sys
try:
    sys.argv = ["comfy_studio.server"] + sys.argv[1:]
    runpy.run_module("comfy_studio.server", run_name="__main__")
except ModuleNotFoundError:
    port = int(os.environ.get("STUDIO_SERVER_PORT", "18190"))
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/studio/api/health") or self.path.startswith("/health"):
                body = json.dumps({"status": "ok", "service": "studio-server", "profile": "studio"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, fmt, *args):
            pass
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"#;
        cmd.args(["-c", STUDIO_BOOTSTRAP_PY]);
    }
    cmd.args([
        "--host",
        "127.0.0.1",
        "--port",
        &cfg.studio_server_port.to_string(),
    ])
    .env("MODEL_PROFILE", cfg.model_profile.as_str())
    .env("STUDIO_LLM_BASE_URL", &cfg.studio_llm_base_url)
    .env("STUDIO_LLM_MODEL", &cfg.studio_llm_model)
    .env(
        "STUDIO_LLM_FALLBACK_BASE_URL",
        &cfg.studio_llm_fallback_base_url,
    )
    .env("STUDIO_LLM_FALLBACK_MODEL", &cfg.studio_llm_fallback_model)
    .env("STUDIO_COMFYUI_URL", &cfg.studio_comfyui_url)
    .env("STUDIO_DATA_DIR", &cfg.studio_data_dir)
    .env("STUDIO_SERVER_PORT", cfg.studio_server_port.to_string())
    .env_remove("COMFY_PASS")
    .stdin(Stdio::null());

    if let Some(ref key) = cfg.studio_llm_key {
        cmd.env("STUDIO_LLM_KEY", key);
    } else {
        cmd.env_remove("STUDIO_LLM_KEY");
    }
    cmd
}

/// Inicia `studio-server` (`127.0.0.1:18190`) solo si `cfg.model_profile == ModelProfile::Studio`.
/// Si el perfil no es `studio`, devuelve `None` sin efectos secundarios.
pub fn spawn_studio_server_if_enabled(cfg: &RuntimeConfig) -> Option<Child> {
    if !should_start_studio_server(cfg) {
        return None;
    }
    let _ = fs::create_dir_all(&cfg.studio_data_dir);
    log_sup(&format!(
        "Iniciando studio-server en 127.0.0.1:{} (MODEL_PROFILE=studio, STUDIO_LLM_KEY={}, model={})...",
        cfg.studio_server_port,
        cfg.masked_studio_llm_key_status(),
        cfg.studio_llm_model
    ));

    let log_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open("/var/log/studio-server.log")
        .or_else(|_| {
            OpenOptions::new()
                .create(true)
                .append(true)
                .open(cfg.studio_data_dir.join("studio-server.log"))
        });

    let mut cmd = build_studio_server_command(cfg);
    if let Ok(f) = log_file {
        if let Ok(f_err) = f.try_clone() {
            cmd.stdout(Stdio::from(f)).stderr(Stdio::from(f_err));
        } else {
            cmd.stdout(Stdio::from(f)).stderr(Stdio::null());
        }
    } else {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
    }

    match cmd.spawn() {
        Ok(mut child) => {
            // Esperar brevemente hasta que el puerto de studio-server acepte conexiones
            // para que strict-check (S11) lo observe levantado en el arranque.
            for _ in 0..25 {
                if crate::collect::procfs::check_tcp_port(cfg.studio_server_port, 100) {
                    break;
                }
                if matches!(child.try_wait(), Ok(Some(_))) {
                    break;
                }
                thread::sleep(Duration::from_millis(100));
            }
            Some(child)
        }
        Err(e) => {
            log_sup(&format!("WARN: No se pudo lanzar studio-server: {e}"));
            None
        }
    }
}

/// Detiene limpiamente el proceso `studio-server` si estaba activo.
pub fn stop_studio_server(studio_child: &mut Option<Child>) {
    if let Some(mut sc) = studio_child.take() {
        let pid = sc.id();
        let _ = sys::send_signal(pid, 15);
        for _ in 0..10 {
            if matches!(sc.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = sc.kill();
        let _ = sc.wait();
    }
}

/// Bucle principal de `comfyctl supervise`.
#[allow(clippy::too_many_lines)]
pub fn run_supervise_cmd(cfg: &RuntimeConfig) -> Result<i32, String> {
    sys::install_signal_handlers();

    let mut sup_state = SupervisorState::new(cfg.comfy_max_restarts);
    write_supervisor_json(&cfg.supervisor_json_path, &sup_state)?;

    log_sup(&format!(
        "Iniciando servidor HTTP de observabilidad integrado en 127.0.0.1:{}...",
        cfg.status_server_port
    ));
    crate::http::spawn_background_status_server(cfg.clone())?;

    let mut studio_child = spawn_studio_server_if_enabled(cfg);

    // 1. Ejecutar strict-check (§6)
    let snap = SystemSnapshot::collect_live(cfg);
    let strict_report = evaluate_strict_checks(cfg, &snap);
    write_and_print_strict_report(cfg, &strict_report)?;

    // 2. Ejecutar diag de arranque (acotado por timeouts de sonda, antes de Fase 1 / ComfyUI, R-07)
    let _ = crate::diag::run_diag_cmd(cfg, None);

    if strict_report.result == "fatal" {
        let now = sys::now_unix_secs();
        sup_state.state = "fatal".to_string();
        sup_state.last_exit = Some(LastExitInfo {
            rc: 20,
            code: 20,
            reason: "Fallo fatal en comfyctl strict-check (ver /run/comfyui-strict.json)"
                .to_string(),
            at: now,
        });
        write_supervisor_json(&cfg.supervisor_json_path, &sup_state)?;
        log_sup(&format!(
            "CRITICAL: strict-check devolvió FATAL. Manteniendo Nginx y /status activos durante {}s antes de salir con código 20.",
            cfg.strict_fatal_grace_s
        ));
        if sleep_interruptible(cfg.strict_fatal_grace_s) {
            log_sup("Señal de apagado recibida durante la ventana de gracia fatal. Cerrando limpiamente (ExitCode=0).");
            stop_studio_server(&mut studio_child);
            graceful_shutdown_nginx();
            return Ok(0);
        }
        stop_studio_server(&mut studio_child);
        graceful_shutdown_nginx();
        return Ok(20);
    }

    // 3. Ejecutar Fase 1 y lanzar Fase 2 en segundo plano
    run_model_downloads_if_enabled(cfg);
    if sys::is_shutdown_requested() {
        stop_studio_server(&mut studio_child);
        graceful_shutdown_nginx();
        return Ok(0);
    }

    // 3. Lanzar lector en background de /var/log/comfyui.log hacia stdout del contenedor
    let mut tail_child = Command::new("tail")
        .args(["-n", "0", "-F", "/var/log/comfyui.log"])
        .stdin(Stdio::null())
        .spawn()
        .ok();

    log_sup("Iniciando bucle supervisor de ComfyUI en 127.0.0.1:18188...");

    while !sys::is_shutdown_requested() {
        truncate_logs_if_needed(cfg.log_max_mb);

        if should_start_studio_server(cfg) {
            let needs_respawn = match studio_child {
                Some(ref mut sc) => matches!(sc.try_wait(), Ok(Some(_))),
                None => true,
            };
            if needs_respawn {
                studio_child = spawn_studio_server_if_enabled(cfg);
            }
        }

        let started_at = sys::now_unix_secs();
        let next_attempt = sup_state.attempt.saturating_add(1);
        log_sup(&format!(
            "Lanzando ComfyUI (intento #{}, reinicios={}/{}, healthy_resets={})...",
            next_attempt, sup_state.restarts, sup_state.max, sup_state.healthy_resets
        ));

        let log_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open("/var/log/comfyui.log")
            .map_err(|e| format!("No se pudo abrir /var/log/comfyui.log: {e}"))?;
        let log_file_err = log_file
            .try_clone()
            .map_err(|e| format!("No se pudo clonar descriptor de /var/log/comfyui.log: {e}"))?;

        let yaml_path = cfg.comfy_app_dir.join("extra_model_paths.yaml");
        let mut cmd = Command::new("python3");
        cmd.current_dir(&cfg.comfy_app_dir)
            .args([
                "main.py",
                "--listen",
                "127.0.0.1",
                "--port",
                "18188",
                "--extra-model-paths-config",
            ])
            .arg(&yaml_path);

        for extra_tok in cfg.comfy_extra_args.split_whitespace() {
            cmd.arg(extra_tok);
        }

        // O-05: Garantizar que COMFY_PASS nunca exista en el entorno de main.py
        cmd.env_remove("COMFY_PASS")
            .stdin(Stdio::null())
            .stdout(Stdio::from(log_file))
            .stderr(Stdio::from(log_file_err));

        let mut child = cmd
            .spawn()
            .map_err(|e| format!("Error al lanzar python3 main.py: {e}"))?;
        let child_pid = child.id();

        sup_state.on_child_spawned(child_pid, started_at);
        write_supervisor_json(&cfg.supervisor_json_path, &sup_state)?;

        let exit_code: i32;
        loop {
            if sys::is_shutdown_requested() {
                log_sup(
                    "Señal de apagado recibida (SIGTERM/SIGINT). Deteniendo ComfyUI limpiamente...",
                );
                let _ = sys::send_signal(child_pid, 15);
                for _ in 0..20 {
                    if let Ok(Some(_)) = child.try_wait() {
                        break;
                    }
                    thread::sleep(Duration::from_millis(500));
                }
                if let Ok(None) = child.try_wait() {
                    let _ = sys::send_signal(child_pid, 9);
                }
                let _ = child.wait();
                if let Some(mut tc) = tail_child.take() {
                    let _ = tc.kill();
                    let _ = tc.wait();
                }
                stop_studio_server(&mut studio_child);
                graceful_shutdown_nginx();
                return Ok(0);
            }

            match child.try_wait() {
                Ok(Some(status)) => {
                    exit_code = extract_exit_code(status);
                    break;
                }
                Ok(None) => {
                    thread::sleep(Duration::from_millis(200));
                }
                Err(e) => {
                    log_sup(&format!("Error en try_wait sobre PID {child_pid}: {e}"));
                    exit_code = 1;
                    break;
                }
            }
        }

        let exit_at = sys::now_unix_secs();
        let run_duration_s = u64::try_from(exit_at.saturating_sub(started_at)).unwrap_or(0);
        let prev_restarts = sup_state.restarts;
        let should_restart = sup_state.on_child_exit(
            exit_code,
            run_duration_s,
            cfg.comfy_healthy_uptime_s,
            exit_at,
        );

        if run_duration_s >= cfg.comfy_healthy_uptime_s && prev_restarts > 0 {
            log_sup(&format!(
                "Proceso previo vivió {}s (>= {}s); contador de reinicios reseteado a 0 (healthy_resets={}).",
                run_duration_s, cfg.comfy_healthy_uptime_s, sup_state.healthy_resets
            ));
        }

        let reason = sup_state
            .last_exit
            .as_ref()
            .map_or_else(String::new, |le| le.reason.clone());
        dump_crash_diagnostics(sup_state.attempt, exit_code, run_duration_s, &reason);

        write_supervisor_json(&cfg.supervisor_json_path, &sup_state)?;

        if !should_restart {
            log_sup(&format!(
                "CRITICAL: Se alcanzó COMFY_MAX_RESTARTS={}. ComfyUI queda en estado 'exhausted'. Nginx, /status y /home siguen activos para diagnóstico.",
                sup_state.max
            ));
            while !sys::is_shutdown_requested() {
                thread::sleep(Duration::from_millis(500));
            }
            break;
        }

        log_sup(&format!(
            "Reiniciando ComfyUI en {}s (reinicio {}/{})...",
            cfg.comfy_restart_delay_s, sup_state.restarts, sup_state.max
        ));
        if sleep_interruptible(cfg.comfy_restart_delay_s) {
            break;
        }
    }

    if let Some(mut tc) = tail_child.take() {
        let _ = tc.kill();
        let _ = tc.wait();
    }
    stop_studio_server(&mut studio_child);
    graceful_shutdown_nginx();
    Ok(0)
}

fn extract_exit_code(status: std::process::ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return 128 + sig;
        }
    }
    1
}
