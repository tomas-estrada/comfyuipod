//! `http` — Servidor HTTP embebido (`tiny_http`) en `127.0.0.1:18189` con caché rápida (3s) y pesada (60s, O-03).

use crate::collect::drift::collect_drift;
use crate::collect::models::collect_model_inventory;
use crate::collect::{build_status_api_payload, StatusApiPayload};
use crate::config::RuntimeConfig;
use crate::logtail::{resolve_log_path, tail_lines};
use crate::redact::SecretRedactor;
use crate::report::{build_report_markdown, HeavyCacheData};
use crate::sys;
use crate::triage::TriageEngine;
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};

const STATUS_HTML: &str = include_str!("status_html.html");

pub struct ServerContext {
    pub cfg: RuntimeConfig,
    pub triage_engine: TriageEngine,
    pub redactor: SecretRedactor,
    pub log_dir: PathBuf,
    pub fast_cache: Mutex<Option<(Instant, StatusApiPayload)>>,
    pub heavy_cache: Mutex<Option<(Instant, HeavyCacheData)>>,
}

impl ServerContext {
    pub fn new(cfg: RuntimeConfig, log_dir: PathBuf) -> Self {
        let triage_engine = TriageEngine::load(None);
        let redactor = SecretRedactor::from_config(&cfg);
        Self {
            cfg,
            triage_engine,
            redactor,
            log_dir,
            fast_cache: Mutex::new(None),
            heavy_cache: Mutex::new(None),
        }
    }

    pub fn get_status_api(&self) -> StatusApiPayload {
        if let Ok(guard) = self.fast_cache.lock() {
            if let Some((ts, cached)) = guard.as_ref() {
                if ts.elapsed() < Duration::from_secs(3) {
                    return cached.clone();
                }
            }
        }
        let fresh = build_status_api_payload(&self.cfg, &self.triage_engine, &self.log_dir);
        if let Ok(mut guard) = self.fast_cache.lock() {
            *guard = Some((Instant::now(), fresh.clone()));
        }
        fresh
    }

    pub fn compute_heavy_now(&self) -> HeavyCacheData {
        let drift = collect_drift(
            &self.cfg.comfy_app_dir,
            Path::new("/etc/comfyui/requirements.lock"),
            Path::new("/etc/comfyui/nodes.lock"),
            &self.redactor,
        );
        let (folder_stats, missing_models) =
            collect_model_inventory(&self.cfg.comfy_data_dir, self.cfg.model_profile.as_str());
        let data = HeavyCacheData {
            drift,
            folder_stats,
            missing_models,
            refreshed_at_unix: sys::now_unix_secs(),
            cache_age_s: 0,
        };
        if let Ok(mut guard) = self.heavy_cache.lock() {
            *guard = Some((Instant::now(), data.clone()));
        }
        data
    }

    /// Devuelve la caché pesada si tiene <= 300s de antigüedad; si expiró (> 300s) o no existe, recalcula (`R-16`).
    pub fn get_heavy_cached(&self) -> HeavyCacheData {
        if let Ok(guard) = self.heavy_cache.lock() {
            if let Some((ts, cached)) = guard.as_ref() {
                let age_s = ts.elapsed().as_secs();
                if age_s <= 300 {
                    let mut out = cached.clone();
                    out.cache_age_s = age_s;
                    return out;
                }
            }
        }
        self.compute_heavy_now()
    }
}

fn make_header(name: &[u8], value: &[u8]) -> Option<Header> {
    Header::from_bytes(name, value).ok()
}

fn send_http_response(
    req: Request,
    status: u16,
    content_type: &str,
    body_bytes: Vec<u8>,
    extra_headers: &[(&str, &str)],
    is_head: bool,
) {
    let len = body_bytes.len();
    let payload = if is_head { Vec::new() } else { body_bytes };
    let mut headers = Vec::new();
    if let Some(h) = make_header(b"Content-Type", content_type.as_bytes()) {
        headers.push(h);
    }
    if let Some(h) = make_header(b"Cache-Control", b"no-store") {
        headers.push(h);
    }
    for (k, v) in extra_headers {
        if let Some(h) = make_header(k.as_bytes(), v.as_bytes()) {
            headers.push(h);
        }
    }
    let resp = Response::new(
        StatusCode(status),
        headers,
        Cursor::new(payload),
        Some(len),
        None,
    );
    let _ = req.respond(resp);
}

fn parse_query_n(query: &str) -> usize {
    for pair in query.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            if k == "n" {
                if let Ok(parsed) = v.parse::<usize>() {
                    return parsed.clamp(1, 2000);
                }
            }
        }
    }
    200
}

pub fn handle_request(ctx: &ServerContext, req: Request) {
    let method = req.method().clone();
    let is_head = method == Method::Head;
    if method != Method::Get && method != Method::Head {
        let body = br#"{"error": "Method not allowed"}"#.to_vec();
        send_http_response(
            req,
            405,
            "application/json; charset=utf-8",
            body,
            &[("Allow", "GET, HEAD")],
            is_head,
        );
        return;
    }

    let raw_url = req.url().to_string();
    let (raw_path, query) = raw_url.split_once('?').unwrap_or((&raw_url, ""));
    let path = if raw_path.len() > 1 {
        raw_path.trim_end_matches('/')
    } else {
        raw_path
    };

    match path {
        "/status" => {
            send_http_response(
                req,
                200,
                "text/html; charset=utf-8",
                STATUS_HTML.as_bytes().to_vec(),
                &[],
                is_head,
            );
        }
        "/status/api" => {
            let mut api = ctx.get_status_api();
            api.services.status_server.running = true;
            let raw_json = serde_json::to_string_pretty(&api).unwrap_or_else(|_| "{}".to_string());
            let clean_json = ctx.redactor.redact(&raw_json);
            send_http_response(
                req,
                200,
                "application/json; charset=utf-8",
                clean_json.into_bytes(),
                &[],
                is_head,
            );
        }
        "/report.md" => {
            let api = ctx.get_status_api();
            let heavy = ctx.get_heavy_cached();
            let md = build_report_markdown(&api, &heavy, &ctx.redactor, &ctx.log_dir);
            send_http_response(
                req,
                200,
                "text/markdown; charset=utf-8",
                md.into_bytes(),
                &[(
                    "Content-Disposition",
                    "inline; filename=\"comfyui-report.md\"",
                )],
                is_head,
            );
        }
        "/diag" => {
            let diag_lines = resolve_log_path("diag", &ctx.log_dir)
                .map(|p| tail_lines(&p, 300))
                .unwrap_or_default();
            let text = if diag_lines.is_empty() {
                "Diagnóstico aún no disponible (/var/log/diag.txt vacío).\n".to_string()
            } else {
                let mut joined = diag_lines.join("\n");
                joined.push('\n');
                ctx.redactor.redact(&joined)
            };
            send_http_response(
                req,
                200,
                "text/plain; charset=utf-8",
                text.into_bytes(),
                &[],
                is_head,
            );
        }
        _ if path.starts_with("/logs/") => {
            let log_name = path.strip_prefix("/logs/").unwrap_or("");
            let Some(log_path) = resolve_log_path(log_name, &ctx.log_dir) else {
                send_http_response(
                    req,
                    404,
                    "application/json; charset=utf-8",
                    br#"{"error": "Log not in whitelist"}"#.to_vec(),
                    &[],
                    is_head,
                );
                return;
            };
            let n = parse_query_n(query);
            let lines = tail_lines(&log_path, n);
            let mut joined = lines.join("\n");
            joined.push('\n');
            let redacted = ctx.redactor.redact(&joined);
            send_http_response(
                req,
                200,
                "text/plain; charset=utf-8",
                redacted.into_bytes(),
                &[],
                is_head,
            );
        }
        _ => {
            send_http_response(
                req,
                404,
                "application/json; charset=utf-8",
                br#"{"error": "Not found"}"#.to_vec(),
                &[],
                is_head,
            );
        }
    }
}

pub fn spawn_background_status_server(cfg: RuntimeConfig) -> Result<(), String> {
    let bind_addr =
        std::env::var("COMFY_STATUS_BIND").unwrap_or_else(|_| "127.0.0.1:18189".to_string());
    let log_dir =
        std::env::var("COMFY_LOG_DIR").map_or_else(|_| PathBuf::from("/var/log"), PathBuf::from);
    let server = Server::http(&bind_addr)
        .map_err(|e| format!("No se pudo enlazar servidor HTTP en {bind_addr}: {e}"))?;
    let server = Arc::new(server);
    let ctx = Arc::new(ServerContext::new(cfg, log_dir));

    // Hilo de refresco pesado cada 60s (O-03)
    let ctx_heavy = Arc::clone(&ctx);
    thread::spawn(move || loop {
        let _ = ctx_heavy.compute_heavy_now();
        for _ in 0..120 {
            if sys::is_shutdown_requested() {
                return;
            }
            thread::sleep(Duration::from_millis(500));
        }
    });

    // Pool de 4 workers HTTP para atender peticiones concurrentes sin bloquear
    for _ in 0..4 {
        let srv = Arc::clone(&server);
        let ctx_w = Arc::clone(&ctx);
        thread::spawn(move || {
            while !sys::is_shutdown_requested() {
                if let Ok(Some(req)) = srv.recv_timeout(Duration::from_millis(250)) {
                    handle_request(&ctx_w, req);
                }
            }
        });
    }

    Ok(())
}

pub fn run_serve_cmd(cfg: &RuntimeConfig, bind_addr: &str) -> Result<i32, String> {
    sys::install_signal_handlers();
    let log_dir =
        std::env::var("COMFY_LOG_DIR").map_or_else(|_| PathBuf::from("/var/log"), PathBuf::from);
    let server = Server::http(bind_addr)
        .map_err(|e| format!("No se pudo enlazar servidor HTTP en {bind_addr}: {e}"))?;
    let ctx = Arc::new(ServerContext::new(cfg.clone(), log_dir));

    let ctx_heavy = Arc::clone(&ctx);
    thread::spawn(move || loop {
        let _ = ctx_heavy.compute_heavy_now();
        for _ in 0..120 {
            if sys::is_shutdown_requested() {
                return;
            }
            thread::sleep(Duration::from_millis(500));
        }
    });

    while !sys::is_shutdown_requested() {
        if let Ok(Some(req)) = server.recv_timeout(Duration::from_millis(250)) {
            handle_request(&ctx, req);
        }
    }
    Ok(0)
}
