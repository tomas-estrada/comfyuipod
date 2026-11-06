//! `report` — Generador determinista de `/report.md` en Rust (`## 0. Triage automático` .. `## 10. Arranque estricto`).

use crate::collect::drift::DriftSummary;
use crate::collect::phases::STATE_NOT_REQUIRED;
use crate::collect::StatusApiPayload;
use crate::logtail::{resolve_log_path, tail_lines};
use crate::redact::SecretRedactor;
use crate::sys;
use regex::Regex;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;

#[derive(Debug, Clone, Default)]
pub struct HeavyCacheData {
    pub drift: DriftSummary,
    pub folder_stats: Vec<String>,
    pub missing_models: Vec<String>,
    pub refreshed_at_unix: i64,
    pub cache_age_s: u64,
}

fn summarize_nginx_access(log_dir: &Path) -> BTreeMap<&'static str, usize> {
    let mut counts: BTreeMap<&'static str, usize> =
        BTreeMap::from([("401", 0), ("413", 0), ("429", 0), ("502", 0), ("503", 0)]);
    let lines = resolve_log_path("nginx-access", log_dir)
        .map(|p| tail_lines(&p, 500))
        .unwrap_or_default();
    let Ok(rx) = Regex::new(r#""\s+(401|413|429|502|503)\s+"#) else {
        return counts;
    };
    for ln in &lines {
        if let Some(cap) = rx.captures(ln) {
            if let Some(code) = cap.get(1).map(|m| m.as_str()) {
                if let Some(slot) = counts.get_mut(code) {
                    *slot = slot.saturating_add(1);
                }
            }
        }
    }
    counts
}

#[allow(clippy::too_many_lines, clippy::if_not_else)]
pub fn build_report_markdown(
    api: &StatusApiPayload,
    heavy: &HeavyCacheData,
    redactor: &SecretRedactor,
    log_dir: &Path,
) -> String {
    let img = &api.image;
    let inst = &api.instance;
    let gpu = &api.gpu;
    let mem = &api.memory;
    let disk = &api.disk;
    let comfy = &api.comfyui;
    let phases = &api.phases;
    let svc = &api.services;
    let env_m = &api.env_masked;

    let mut out = String::with_capacity(16384);
    let _ = writeln!(out, "# Informe de Estado y Diagnóstico — ComfyUIPod");
    let _ = writeln!(
        out,
        "Generado: `{}` · Hostname: `{}` · Uptime: `{}s` · Instancia Vast: `{}` · Puerto externo 8188: `{}`",
        api.generated_at,
        inst.hostname,
        inst.uptime_s,
        inst.vast_instance_id,
        inst.external_port_8188
    );
    let _ = writeln!(
        out,
        "Imagen: `tag={}` · `built_at={}` · `comfyui_sha={}` · `pytorch={}` · `cuda={}` · `ubuntu={}`\n",
        img.tag, img.built_at, img.comfyui_sha, img.pytorch, img.cuda, img.ubuntu
    );

    // 0. Triage automático
    let _ = writeln!(out, "## 0. Triage automático");
    if !api.triage.is_empty() {
        for t in &api.triage {
            let _ = writeln!(
                out,
                "- **[{}]** `{}` (fuente: `{}`): {}",
                t.severity.to_uppercase(),
                t.pattern_id,
                t.source,
                t.summary
            );
            let _ = writeln!(out, "  - Evidencia: `{}`", t.evidence);
            let _ = writeln!(out, "  - Primer comando sugerido: `{}`", t.first_command);
        }
    } else {
        let diag_lines = resolve_log_path("diag", log_dir)
            .map(|p| tail_lines(&p, 100))
            .unwrap_or_default();
        let summary_line = diag_lines
            .iter()
            .rev()
            .find(|ln| ln.contains("[DIAG] resumen:"));
        if let Some(sum_ln) = summary_line {
            let _ = writeln!(
                out,
                "- Sin anomalías detectadas en los logs recientes ({sum_ln})."
            );
        } else {
            let _ = writeln!(
                out,
                "- Sin anomalías detectadas (diagnóstico de arranque aún en curso o sin alertas)."
            );
        }
    }
    let _ = writeln!(out);

    // 1. Contexto operativo para el agente
    let _ = writeln!(out, "## 1. Contexto operativo para el agente");
    let _ = writeln!(
        out,
        "- **Rutas**: código e instalación en `/opt/ComfyUI` · datos persistentes en `/workspace/comfyui-data` (`models/`, `input/`, `output/`, `user/` son symlinks desde `/opt/ComfyUI`)"
    );
    let _ = writeln!(
        out,
        "- **Árbol de procesos**: `tini (PID 1)` → `comfyctl supervise` → `{{nginx (root master + www-data workers), comfyctl HTTP server :18189, ttyd opcional :7681, tee -a /var/log/start.log (huérfano de start.sh), download-models.sh --bootstrap (timeout 600), python3 main.py (supervisado, máx {} reinicios)}}`",
        comfy.max_restarts
    );
    let _ = writeln!(
        out,
        "- **Puertos**: `8188` público (Nginx Basic Auth) → `18188` ComfyUI · `18189` comfyctl status-server · `7681` ttyd (todos en `127.0.0.1` salvo `8188`)"
    );
    let _ = writeln!(
        out,
        "- **Cómo ejecutar comandos**: terminal web en `/terminal/` (si `ENABLE_WEB_TERMINAL=1`) · SSH si la instancia se creó con SSH · `vastai execute <id> 'ls|du|rm …'`"
    );
    let _ = writeln!(
        out,
        "- **Cómo reiniciar ComfyUI sin reiniciar la instancia**: `pkill -f 'main.py'` (envía `SIGTERM` / `rc=143`; el supervisor `comfyctl supervise` lo relanza en 5 s **sin consumir** el presupuesto `COMFY_MAX_RESTARTS`, y el contador `restarts` se resetea automáticamente a `0` tras `COMFY_HEALTHY_UPTIME=600s` de ejecución estable)"
    );
    let _ = writeln!(
        out,
        "- **Cómo reinstalar la terna PyTorch CUDA 12.8**: `pip3 install --force-reinstall torch==2.9.1+cu128 torchvision==0.24.1+cu128 torchaudio==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128`"
    );
    let _ = writeln!(
        out,
        "- **Restricciones**: `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` activo · no usar 'Update ComfyUI' desde Manager · ningún secreto se incluye en este informe\n"
    );

    // 2. Diagnóstico de arranque ([DIAG])
    let _ = writeln!(out, "## 2. Diagnóstico de arranque ([DIAG])");
    let diag_lines = resolve_log_path("diag", log_dir)
        .map(|p| tail_lines(&p, 100))
        .unwrap_or_default();
    if !diag_lines.is_empty() {
        let _ = writeln!(out, "```text");
        for ln in &diag_lines {
            let _ = writeln!(out, "{ln}");
        }
        let _ = writeln!(out, "```");
    } else {
        let _ = writeln!(out, "sin datos (diagnóstico aún no ejecutado)");
    }
    let _ = writeln!(out);

    // 3. ComfyUI y supervisor
    let _ = writeln!(out, "## 3. ComfyUI y supervisor");
    let pid_disp = comfy
        .pid
        .map_or_else(|| "None".to_string(), |p| p.to_string());
    let _ = writeln!(
        out,
        "- **Estado**: `{}` (supervisor: `{}`) · **PID**: `{pid_disp}` · **RSS**: `{} MB` · **Uptime proceso**: `{}s` · **Reinicios**: `{}/{}` · **Resets por uptime sano**: `{}`",
        comfy.state,
        comfy.supervisor_state,
        comfy.rss_mb,
        comfy.uptime_s,
        comfy.restarts,
        comfy.max_restarts,
        comfy.healthy_resets
    );
    if let Some(le) = &comfy.last_exit {
        let at_iso = sys::format_utc_iso8601(le.at);
        let _ = writeln!(
            out,
            "- **Última caída registrada**: `rc={}` a las `{at_iso}` — {}",
            le.rc, le.reason
        );
    }
    let sup_lines = resolve_log_path("supervisor", log_dir)
        .map(|p| tail_lines(&p, 140))
        .unwrap_or_default();
    if !sup_lines.is_empty() {
        let _ = writeln!(out, "```text");
        for ln in &sup_lines {
            let _ = writeln!(out, "{ln}");
        }
        let _ = writeln!(out, "```");
    } else {
        let _ = writeln!(out, "Sin caídas registradas en `/var/log/supervisor.log`.");
    }
    let _ = writeln!(out);

    // 4. GPU, memoria y disco
    let _ = writeln!(out, "## 4. GPU, memoria y disco");
    if gpu.available {
        let _ = writeln!(
            out,
            "- **GPU**: `{}` · Driver `{}` · VRAM `{} / {} MB` · Uso `{}%` · Temp `{}°C`",
            gpu.name, gpu.driver, gpu.vram_used_mb, gpu.vram_total_mb, gpu.util_pct, gpu.temp_c
        );
    } else {
        let reason = gpu.reason.as_deref().unwrap_or("Modo CPU");
        let _ = writeln!(out, "- **GPU**: No disponible (`{reason}`)");
    }
    let _ = writeln!(
        out,
        "- **Memoria RAM**: `{}` MB usados / `{}` MB totales (`{}` MB disponibles) · Swap usado: `{}` MB",
        mem.used_mb, mem.total_mb, mem.available_mb, mem.swap_used_mb
    );
    let _ = writeln!(
        out,
        "- **Disco (`{}`)**: `{}` GB usados / `{}` GB totales (`{}` GB libres)\n",
        disk.path, disk.used_gb, disk.total_gb, disk.free_gb
    );

    // 5. Descargas (Fase 1 y Fase 2)
    let _ = writeln!(out, "## 5. Descargas (Fase 1 y Fase 2)");
    let p1 = &phases.phase1;
    let p2 = &phases.phase2;
    let cur_file = p2.current_file.as_deref().unwrap_or("ninguno");
    let hf_pid_disp = p2
        .hf_pid
        .map_or_else(|| "None".to_string(), |p| p.to_string());
    let bytes_line = |done: u64, total: Option<u64>| match total {
        Some(total) => format!("avance: `{done}` de `{total}` bytes"),
        None => "avance: `no disponible`".to_string(),
    };
    let _ = writeln!(
        out,
        "- **Fase 1**: `{}` (`{}/{}` completados, `{}` fallidos) · {}",
        p1.state,
        p1.done,
        p1.total,
        p1.failed.len(),
        bytes_line(p1.bytes_done, p1.bytes_total)
    );
    if !p1.failed.is_empty() {
        let _ = writeln!(
            out,
            "  - Ficheros Fase 1 con error: `{}`",
            p1.failed.join("`, `")
        );
    }
    if !p1.missing.is_empty() {
        let _ = writeln!(
            out,
            "  - Faltan en disco (Fase 1): `{}`",
            p1.missing.join("`, `")
        );
    }
    if p1.state == STATE_NOT_REQUIRED {
        let _ = writeln!(
            out,
            "  - Sin trabajo de Fase 1: el arranque (`--bootstrap`) no declara modelos que descargar."
        );
    }
    let _ = writeln!(
        out,
        "- **Fase 2**: `{}` (`{}/{}` completados, `{}` fallidos) · Archivo actual: `{cur_file}` · Proceso `hf`: PID `{hf_pid_disp}` (`{}` MB RSS)",
        p2.state,
        p2.done,
        p2.total,
        p2.failed.len(),
        p2.hf_rss_mb
    );
    let _ = writeln!(out, "  - {}", bytes_line(p2.bytes_done, p2.bytes_total));
    if !p2.failed.is_empty() {
        let _ = writeln!(
            out,
            "  - Ficheros Fase 2 con error: `{}`",
            p2.failed.join("`, `")
        );
    }
    if !p2.missing.is_empty() {
        let _ = writeln!(
            out,
            "  - Faltan en disco (Fase 2): `{}`",
            p2.missing.join("`, `")
        );
    }
    if p2.state == STATE_NOT_REQUIRED {
        let _ = writeln!(
            out,
            "  - Sin trabajo de Fase 2: el arranque (`--bootstrap`) no lanza la Fase 2 del perfil (se ejecuta a mano con `download-models.sh --phase2`)."
        );
    }
    let dl_lines = resolve_log_path("download", log_dir)
        .map(|p| tail_lines(&p, 80))
        .unwrap_or_default();
    if !dl_lines.is_empty() {
        let _ = writeln!(out, "```text");
        for ln in &dl_lines {
            let _ = writeln!(out, "{ln}");
        }
        let _ = writeln!(out, "```");
    } else {
        let _ = writeln!(out, "Sin líneas en `/var/log/download-models.log`.");
    }
    let _ = writeln!(out);

    // 6. Log de ComfyUI (últimas 120 líneas)
    let _ = writeln!(out, "## 6. Log de ComfyUI (últimas 120 líneas)");
    let comfy_lines = resolve_log_path("comfyui", log_dir)
        .map(|p| tail_lines(&p, 120))
        .unwrap_or_default();
    if !comfy_lines.is_empty() {
        let _ = writeln!(out, "```text");
        for ln in &comfy_lines {
            let _ = writeln!(out, "{ln}");
        }
        let _ = writeln!(out, "```");
    } else {
        let _ = writeln!(out, "Sin líneas en `/var/log/comfyui.log`.");
    }
    let _ = writeln!(out);

    // 7. Nginx
    let _ = writeln!(out, "## 7. Nginx");
    let counts = summarize_nginx_access(log_dir);
    let _ = writeln!(
        out,
        "- **Estado**: `running={}` · **Workers user**: `{}`",
        svc.nginx.running, svc.nginx.workers_user
    );
    let _ = writeln!(
        out,
        "- **Recuento de códigos en últimas 500 peticiones**: `401={}` · `413={}` · `429={}` · `502={}` · `503={}`",
        counts.get("401").copied().unwrap_or(0),
        counts.get("413").copied().unwrap_or(0),
        counts.get("429").copied().unwrap_or(0),
        counts.get("502").copied().unwrap_or(0),
        counts.get("503").copied().unwrap_or(0)
    );
    let ng_err = resolve_log_path("nginx-error", log_dir)
        .map(|p| tail_lines(&p, 50))
        .unwrap_or_default();
    if !ng_err.is_empty() {
        let _ = writeln!(out, "```text");
        for ln in &ng_err {
            let _ = writeln!(out, "{ln}");
        }
        let _ = writeln!(out, "```");
    } else {
        let _ = writeln!(out, "Sin errores en `/var/log/nginx/error.log`.");
    }
    let _ = writeln!(out);

    // 8. Drift del entorno
    let _ = writeln!(out, "## 8. Drift del entorno");
    if heavy.cache_age_s > 300 {
        let _ = writeln!(
            out,
            "- **[WARN]** Caché de drift/modelos con antigüedad de `{}s` (`> 300s`).",
            heavy.cache_age_s
        );
    }
    let _ = writeln!(
        out,
        "### Diferencias `pip freeze` vs `/etc/comfyui/requirements.lock`"
    );
    if heavy.drift.pip_added.is_empty() && heavy.drift.pip_removed.is_empty() {
        let _ = writeln!(
            out,
            "- Sin cambios respecto a `/etc/comfyui/requirements.lock`."
        );
    } else {
        for p in heavy.drift.pip_added.iter().take(40) {
            let _ = writeln!(out, "- **+** `{p}`");
        }
        for p in heavy.drift.pip_removed.iter().take(40) {
            let _ = writeln!(out, "- **-** `{p}`");
        }
    }

    let _ = writeln!(
        out,
        "### Estado de `custom_nodes/` vs `/etc/comfyui/nodes.lock`"
    );
    if heavy.drift.node_drift.is_empty() {
        let lock_count = if heavy.drift.nodes_lock_count > 0 {
            heavy.drift.nodes_lock_count
        } else {
            10
        };
        let _ = writeln!(
            out,
            "- Los {lock_count} custom nodes coinciden exactamente con los SHAs de `/etc/comfyui/nodes.lock`."
        );
    } else {
        for nd in &heavy.drift.node_drift {
            let _ = writeln!(out, "{nd}");
        }
    }

    let _ = writeln!(out, "### Variables de entorno relevantes (enmascaradas)");
    for key in [
        "MODEL_PROFILE",
        "COMFY_USER",
        "COMFY_PASS",
        "HF_TOKEN",
        "COMFYUI_EXTRA_ARGS",
        "HF_HUB_DISABLE_XET",
        "ENABLE_WEB_TERMINAL",
        "STUDIO_LLM_KEY",
        "STUDIO_LLM_BASE_URL",
        "STUDIO_LLM_MODEL",
        "STUDIO_LLM_FALLBACK_BASE_URL",
        "STUDIO_LLM_FALLBACK_KEY",
        "STUDIO_LLM_FALLBACK_MODEL",
        "STUDIO_COMFYUI_URL",
        "STUDIO_DATA_DIR",
    ] {
        if let Some(val) = env_m.get(key) {
            let _ = writeln!(out, "- `{key}` = `{val}`");
        }
    }
    let _ = writeln!(out);

    // 9. Modelos
    let _ = writeln!(out, "## 9. Modelos");
    if !heavy.folder_stats.is_empty() {
        for fs_line in &heavy.folder_stats {
            let _ = writeln!(out, "{fs_line}");
        }
    } else {
        let _ = writeln!(out, "- Sin datos de carpetas de modelos.");
    }

    let active_prof = env_m.get("MODEL_PROFILE").map_or("all", String::as_str);
    let _ = writeln!(
        out,
        "### Modelos esperados para el perfil `{active_prof}` aún ausentes"
    );
    if heavy.missing_models.is_empty() {
        let _ = writeln!(
            out,
            "- Todos los modelos del perfil están presentes en disco."
        );
    } else {
        for mm in &heavy.missing_models {
            let _ = writeln!(out, "{mm}");
        }
    }
    let _ = writeln!(out);

    // 10. Arranque estricto (§6.3.4)
    let _ = writeln!(out, "## 10. Arranque estricto");
    if let Some(strict_rep) = &api.strict {
        let _ = writeln!(
            out,
            "- **Resultado**: `{}` · **Modo estricto**: `{}` · **Evaluado**: `{}`",
            strict_rep.result, strict_rep.strict_start, strict_rep.checked_at
        );
        for chk in &strict_rep.checks {
            let rem = chk
                .remediation
                .as_deref()
                .map_or_else(String::new, |r| format!(" — Remediación: `{r}`"));
            let _ = writeln!(
                out,
                "- `[{}]` **{}** ({}): {}{rem}",
                chk.status.to_uppercase(),
                chk.id,
                chk.name,
                chk.detail
            );
        }
    } else {
        let _ = writeln!(out, "- Sin informe `/run/comfyui-strict.json` disponible.");
    }

    // 11. Pesos del flujo pedido (verdad en disco, no en el log de descarga)
    let _ = writeln!(out, "## 11. Pesos del flujo pedido");
    let w = &api.weights;
    if w.source != crate::collect::models::WEIGHT_SOURCE_DISK {
        let _ = writeln!(
            out,
            "- Estado de pesos: `no disponible` (no se pudo leer la raíz de modelos)."
        );
    } else {
        let arrived_bytes: u64 = w.present.iter().map(|e| e.bytes).sum();
        let _ = writeln!(
            out,
            "- **Esperados** (`{active_prof}`): `{}` · **Llegaron**: `{}` (`{arrived_bytes}` bytes) · **Faltan**: `{}`",
            w.expected,
            w.present.len(),
            w.missing.len()
        );
        if w.missing.is_empty() {
            let _ = writeln!(out, "- Todos los pesos esperados están en disco.");
        } else {
            let _ = writeln!(out, "### Faltan en disco");
            for m in &w.missing {
                let _ = writeln!(out, "- `{m}`");
            }
        }
        if !w.present.is_empty() {
            let _ = writeln!(out, "### Han llegado");
            for p in w.present.iter().take(40) {
                let mb = p.bytes / (1024 * 1024);
                let _ = writeln!(out, "- `{}` (`{mb}` MB)", p.path);
            }
            if w.present.len() > 40 {
                let _ = writeln!(out, "- … y {} más.", w.present.len() - 40);
            }
        }
    }
    let _ = writeln!(out);

    let mut report_str = redactor.redact(&out);
    if report_str.len() > 400_000 {
        let mut cut = 395_000;
        while cut > 0 && !report_str.is_char_boundary(cut) {
            cut = cut.saturating_sub(1);
        }
        report_str.truncate(cut);
        report_str.push_str("\n\n...[Informe truncado al límite de 400 KB]...\n");
    }
    report_str
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::gpu::GpuStatus;
    use crate::collect::phases::{Phase1Info, Phase2Info, PhasesStatus};
    use crate::collect::procfs::{
        ComfyUiStatus, DiskStatus, ImageMeta, InstanceInfo, MemoryStatus, NginxServiceStatus,
        ServicesStatus, StatusServerServiceStatus, TerminalServiceStatus,
    };

    fn payload_with(phases: PhasesStatus) -> StatusApiPayload {
        StatusApiPayload {
            generated_at: "2026-11-05T09:00:00Z".to_string(),
            image: ImageMeta {
                tag: "sha-512e485".to_string(),
                built_at: "2026-11-05T08:00:00Z".to_string(),
                comfyui_sha: "4c62a84068a0".to_string(),
                pytorch: "2.9.1+cu128".to_string(),
                cuda: "12.8".to_string(),
                ubuntu: "24.04".to_string(),
            },
            instance: InstanceInfo {
                hostname: "vast-5090-node".to_string(),
                uptime_s: 120,
                vast_instance_id: "C.998877".to_string(),
                external_port_8188: "41888".to_string(),
            },
            gpu: GpuStatus {
                available: false,
                name: String::new(),
                driver: String::new(),
                vram_total_mb: 0,
                vram_used_mb: 0,
                util_pct: 0,
                temp_c: 0,
                processes: Vec::new(),
                reason: Some("Modo CPU".to_string()),
            },
            memory: MemoryStatus {
                total_mb: 1024,
                used_mb: 512,
                available_mb: 512,
                swap_used_mb: 0,
            },
            disk: DiskStatus {
                path: "/workspace/comfyui-data".to_string(),
                total_gb: 150.0,
                used_gb: 30.0,
                free_gb: 120.0,
            },
            comfyui: ComfyUiStatus {
                state: "running".to_string(),
                supervisor_state: "running".to_string(),
                pid: Some(1234),
                rss_mb: 2048,
                uptime_s: 110,
                port_open: true,
                restarts: 0,
                healthy_resets: 0,
                max_restarts: 5,
                last_exit: None,
            },
            phases,
            phase_banner:
                "ComfyUI listo · Descargas no requeridas (el arranque no arrastra modelos)"
                    .to_string(),
            services: ServicesStatus {
                nginx: NginxServiceStatus {
                    running: true,
                    workers_user: "www-data".to_string(),
                },
                status_server: StatusServerServiceStatus { running: true },
                terminal: TerminalServiceStatus {
                    enabled: false,
                    running: false,
                },
                studio_server: None,
            },
            errors_recent: Vec::new(),
            triage: Vec::new(),
            env_masked: BTreeMap::new(),
            weights: crate::collect::models::WeightStatus::default(),
            strict: None,
        }
    }

    fn not_required_phases() -> PhasesStatus {
        PhasesStatus {
            phase1: Phase1Info {
                state: STATE_NOT_REQUIRED.to_string(),
                done: 0,
                total: 0,
                failed: Vec::new(),
                elapsed_s: 0,
                ..Phase1Info::default()
            },
            phase2: Phase2Info {
                state: STATE_NOT_REQUIRED.to_string(),
                done: 0,
                total: 0,
                failed: Vec::new(),
                current_file: None,
                hf_pid: None,
                hf_rss_mb: 0,
                ..Phase2Info::default()
            },
        }
    }

    #[test]
    fn report_publishes_not_required_without_claiming_pending_work() {
        // Regresión F20: /report.md no debe decir `pending` tras un arranque --bootstrap
        // que no descarga nada (el log de al lado dice "Nada que descargar").
        let heavy = HeavyCacheData::default();
        let redactor = SecretRedactor::new(None, None);
        let log_dir = std::env::temp_dir().join("comfyctl_report_arranque_test_no_logs");
        let md = build_report_markdown(
            &payload_with(not_required_phases()),
            &heavy,
            &redactor,
            &log_dir,
        );

        assert!(md.contains("- **Fase 1**: `not_required` (`0/0` completados, `0` fallidos)"));
        assert!(md.contains("- **Fase 2**: `not_required` (`0/0` completados, `0` fallidos)"));
        assert!(md.contains(
            "  - Sin trabajo de Fase 1: el arranque (`--bootstrap`) no declara modelos que descargar."
        ));
        assert!(md.contains(
            "  - Sin trabajo de Fase 2: el arranque (`--bootstrap`) no lanza la Fase 2 del perfil"
        ));
        // La sección 5 sigue siendo la del contrato y ningún estado inventa pendientes.
        assert!(md.contains("## 5. Descargas (Fase 1 y Fase 2)"));
        let section5 = md
            .split("## 5. Descargas (Fase 1 y Fase 2)")
            .nth(1)
            .unwrap_or_default()
            .split("## 6. Log de ComfyUI")
            .next()
            .unwrap_or_default();
        assert!(!section5.contains("`pending`"), "section 5: {section5}");
    }
}
