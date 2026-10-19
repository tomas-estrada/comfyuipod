//! `diag` — Implementación en Rust de `comfyctl diag` que reemplaza `scripts/diag.sh` (§4.1 / §10.2).
//! Conserva el contrato exacto `[DIAG][OK|WARN|FAIL] <key>: <detail>` y `[DIAG] resumen: X ok, Y warn, Z fail`.

use crate::collect::procfs::collect_memory_and_disk;
use crate::config::RuntimeConfig;
use crate::redact::SecretRedactor;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

struct DiagAggregator {
    lines: Vec<String>,
    ok_count: u32,
    warn_count: u32,
    fail_count: u32,
    redactor: SecretRedactor,
}

impl DiagAggregator {
    fn new(redactor: SecretRedactor) -> Self {
        Self {
            lines: Vec::new(),
            ok_count: 0,
            warn_count: 0,
            fail_count: 0,
            redactor,
        }
    }

    fn emit(&mut self, level: &str, key: &str, detail: &str) {
        match level {
            "OK" => self.ok_count = self.ok_count.saturating_add(1),
            "WARN" => self.warn_count = self.warn_count.saturating_add(1),
            _ => self.fail_count = self.fail_count.saturating_add(1),
        }
        let clean = self.redactor.redact(detail.trim());
        let line = format!("[DIAG][{level}] {key}: {clean}");
        println!("{line}");
        self.lines.push(line);
    }

    fn emit_probe_line(&mut self, key: &str, raw_out: &str) {
        let first_line = raw_out.lines().next().unwrap_or("").trim();
        if let Some((lvl, msg)) = first_line.split_once('|') {
            let level = match lvl.trim() {
                "OK" => "OK",
                "WARN" => "WARN",
                _ => "FAIL",
            };
            self.emit(level, key, msg);
        } else {
            self.emit("FAIL", key, first_line);
        }
    }

    fn finish(mut self) -> String {
        let summary = format!(
            "[DIAG] resumen: {} ok, {} warn, {} fail",
            self.ok_count, self.warn_count, self.fail_count
        );
        println!("{summary}");
        self.lines.push(summary);
        let mut out = self.lines.join("\n");
        out.push('\n');
        out
    }
}

fn resolve_probe_script(filename: &str) -> PathBuf {
    let etc_path = Path::new("/etc/comfyui/probes").join(filename);
    if etc_path.is_file() {
        return etc_path;
    }
    let local_path = Path::new("config/probes").join(filename);
    if local_path.is_file() {
        return local_path;
    }
    etc_path
}

fn run_python_probe(script_name: &str) -> String {
    let script_path = resolve_probe_script(script_name);
    match Command::new("python3").arg(&script_path).output() {
        Ok(out) => {
            let mut txt = String::from_utf8_lossy(&out.stdout).into_owned();
            let err = String::from_utf8_lossy(&out.stderr);
            if !err.trim().is_empty() {
                if !txt.is_empty() {
                    txt.push(' ');
                }
                txt.push_str(err.trim());
            }
            txt
        }
        Err(e) => format!("FAIL|No se pudo ejecutar {}: {e}", script_path.display()),
    }
}

#[allow(clippy::too_many_lines)]
pub fn run_diag_cmd(cfg: &RuntimeConfig, output_path: Option<&Path>) -> Result<i32, String> {
    let redactor = SecretRedactor::from_config(cfg);
    let mut agg = DiagAggregator::new(redactor);

    // 1. GPU (nvidia-smi)
    if let Ok(out) = Command::new(&cfg.nvidia_smi_bin)
        .args([
            "--query-gpu=name,driver_version,memory.total,compute_cap",
            "--format=csv,noheader",
        ])
        .output()
    {
        let first = String::from_utf8_lossy(&out.stdout)
            .lines()
            .next()
            .unwrap_or("")
            .trim()
            .to_string();
        if out.status.success() && !first.is_empty() {
            agg.emit("OK", "gpu", &first);
        } else {
            agg.emit(
                "WARN",
                "gpu",
                "nvidia-smi presente pero sin GPU accesible (modo CPU o contenedor sin --gpus)",
            );
        }
    } else {
        agg.emit(
            "WARN",
            "gpu",
            "nvidia-smi no instalado o no disponible en PATH (modo CPU)",
        );
    }

    // 2. PyTorch + CUDA + matmul 1024x1024
    let torch_out = run_python_probe("torch_cuda.py");
    agg.emit_probe_line("torch", &torch_out);

    // 3. ONNX Runtime
    let ort_out = run_python_probe("ort_cuda.py");
    agg.emit_probe_line("ort", &ort_out);

    // 4. ffmpeg NVENC (h264_nvenc)
    let nvenc_ok = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=640x360:rate=30",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|st| st.success());
    if nvenc_ok {
        agg.emit(
            "OK",
            "nvenc",
            "Codificación de prueba 640x360@30fps con h264_nvenc exitosa",
        );
    } else {
        agg.emit(
            "WARN",
            "nvenc",
            "h264_nvenc no disponible en este entorno (sin GPU o sin libnvidia-encode.so); fallback a libx264 activo",
        );
    }

    // 5. ffmpeg libx264 (CPU)
    let x264_ok = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=640x360:rate=30",
            "-c:v",
            "libx264",
            "-f",
            "null",
            "-",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|st| st.success());
    if x264_ok {
        agg.emit(
            "OK",
            "libx264",
            "Codificación de prueba 640x360@30fps con libx264 exitosa",
        );
    } else {
        agg.emit("FAIL", "libx264", "Codificación con libx264 falló");
    }

    // 6. Nginx (sintaxis + workers como www-data)
    let nginx_t_ok = Command::new("nginx")
        .arg("-t")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|st| st.success());
    if nginx_t_ok {
        let users = Command::new("ps")
            .args(["-o", "user=", "-C", "nginx"])
            .output()
            .ok()
            .map(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .lines()
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .unwrap_or_default();
        if users.contains("www-data") {
            agg.emit(
                "OK",
                "nginx",
                &format!("nginx -t OK; workers activos bajo usuario www-data ({users})"),
            );
        } else {
            let u_disp = if users.is_empty() { "ninguno" } else { &users };
            agg.emit(
                "WARN",
                "nginx",
                &format!(
                    "nginx -t OK, pero no se detectó worker www-data aún (usuarios: {u_disp})"
                ),
            );
        }
    } else {
        agg.emit("FAIL", "nginx", "nginx -t devolvió error de configuración");
    }

    // 7. Disco, RAM y CPU
    let (mem, disk) = collect_memory_and_disk(&cfg.comfy_data_dir);
    agg.emit(
        "OK",
        "disk",
        &format!(
            "total={}GB usado={}GB libre={}GB ({})",
            disk.total_gb, disk.used_gb, disk.free_gb, disk.path
        ),
    );
    agg.emit(
        "OK",
        "ram",
        &format!(
            "total={}MB usado={}MB disponible={}MB",
            mem.total_mb, mem.used_mb, mem.available_mb
        ),
    );
    let cores = std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get);
    agg.emit("OK", "cpu", &format!("nproc={cores}"));

    // 8. Python imports críticos
    let py_out = run_python_probe("python_imports.py");
    agg.emit_probe_line("python-imports", &py_out);

    // 9. ldconfig (libcudnn / libcublas)
    let ld_count = Command::new("ldconfig")
        .arg("-p")
        .output()
        .ok()
        .map_or(0_usize, |o| {
            String::from_utf8_lossy(&o.stdout)
                .lines()
                .filter(|ln| ln.contains("libcudnn") || ln.contains("libcublas"))
                .count()
        });
    if ld_count > 0 {
        agg.emit(
            "OK",
            "ldconfig",
            &format!("{ld_count} entradas de libcudnn/libcublas registradas en caché dinámica"),
        );
    } else {
        agg.emit(
            "FAIL",
            "ldconfig",
            "0 entradas de libcudnn/libcublas en ldconfig -p",
        );
    }

    // 10. Variables de entorno (enmascaradas)
    let pass_state = if cfg.comfy_pass_autogenerated {
        "<autogenerada>"
    } else if cfg.comfy_pass.is_some() {
        "<definida por el usuario>"
    } else {
        "<no definida>"
    };
    let hf_state = if cfg.hf_token.is_some() {
        "<definido>"
    } else {
        "<no definido>"
    };
    let term_flag = if cfg.enable_web_terminal { "1" } else { "0" };
    let xet_disp = cfg.hf_hub_disable_xet.as_deref().unwrap_or("<no definido>");
    agg.emit(
        "OK",
        "env",
        &format!(
            "MODEL_PROFILE={} COMFY_USER={} COMFY_PASS={pass_state} HF_TOKEN={hf_state} COMFYUI_EXTRA_ARGS='{}' HF_HUB_DISABLE_XET={xet_disp} ENABLE_WEB_TERMINAL={term_flag}",
            cfg.model_profile.as_str(), cfg.comfy_user, cfg.comfy_extra_args
        ),
    );

    let full_text = agg.finish();
    let target = output_path.unwrap_or_else(|| Path::new("/var/log/diag.txt"));
    if let Some(parent) = target.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let _ = fs::write(target, full_text.as_bytes());
    Ok(0)
}
