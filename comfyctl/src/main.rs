//! Punto de entrada de `comfyctl` (§5.2: `strict-check`, `supervise`, `serve`, `report`, `diag`, `version`).

use comfyctl::config::RuntimeConfig;
use comfyctl::http::ServerContext;
use comfyctl::report::build_report_markdown;
use comfyctl::{diag, http, strict, supervisor};
use pico_args::Arguments;
use std::path::PathBuf;
use std::process::ExitCode;

fn print_help() {
    println!(
        "comfyctl {} — Control de arranque, supervisión y observabilidad de ComfyUIPod\n\n\
         Subcomandos:\n  \
           comfyctl strict-check                   Valida invariantes S1..S10 y escribe /run/comfyui-strict.json\n  \
           comfyctl supervise                      Ejecuta strict-check, servidor HTTP :18189, Fase 1/2 y supervisa ComfyUI\n  \
           comfyctl serve [--bind 127.0.0.1:18189] Ejecuta el servidor HTTP de observabilidad en solitario\n  \
           comfyctl report                         Genera y vuelca /report.md por stdout\n  \
           comfyctl diag                           Ejecuta las 10 sondas de arranque y escribe /var/log/diag.txt\n  \
           comfyctl version                        Muestra versión y metadatos de compilación",
        env!("CARGO_PKG_VERSION")
    );
}

/// Convierte los argumentos sobrantes en un error de uso claro (exit 2).
fn ensure_no_leftover_args(args: Arguments) -> Result<(), String> {
    let leftovers = args.finish();
    if leftovers.is_empty() {
        Ok(())
    } else {
        let joined = leftovers
            .iter()
            .map(|a| a.to_string_lossy())
            .collect::<Vec<_>>()
            .join(" ");
        Err(format!(
            "Argumentos no reconocidos: {joined}. Ejecuta 'comfyctl --help' para ver el uso correcto."
        ))
    }
}

fn run() -> Result<u8, String> {
    let mut args = Arguments::from_env();
    if args.contains(["-h", "--help"]) {
        print_help();
        return Ok(0);
    }

    let Some(subcmd) = args.subcommand().map_err(|e| e.to_string())? else {
        print_help();
        return Ok(0);
    };

    match subcmd.as_str() {
        "version" | "--version" | "-V" => {
            ensure_no_leftover_args(args)?;
            println!("comfyctl {}", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        "strict-check" => {
            let json_out = args.contains("--json");
            ensure_no_leftover_args(args)?;
            let cfg = RuntimeConfig::from_env();
            let rc = strict::run_strict_check_cmd_with_format(&cfg, json_out)?;
            Ok(u8::try_from(rc).unwrap_or(1))
        }
        "supervise" => {
            ensure_no_leftover_args(args)?;
            let cfg = RuntimeConfig::from_env_supervise();
            let rc = supervisor::run_supervise_cmd(&cfg)?;
            Ok(u8::try_from(rc).unwrap_or(1))
        }
        "serve" => {
            let cfg = RuntimeConfig::from_env();
            // STATUS_SERVER_PORT (config) fija el puerto por defecto; --bind explícito gana.
            let bind_addr: String = args
                .opt_value_from_str("--bind")
                .map_err(|e| e.to_string())?
                .unwrap_or_else(|| format!("127.0.0.1:{}", cfg.status_server_port));
            ensure_no_leftover_args(args)?;
            let rc = http::run_serve_cmd(&cfg, &bind_addr)?;
            Ok(u8::try_from(rc).unwrap_or(1))
        }
        "report" => {
            ensure_no_leftover_args(args)?;
            let cfg = RuntimeConfig::from_env();
            let log_dir = std::env::var("COMFY_LOG_DIR")
                .map_or_else(|_| PathBuf::from("/var/log"), PathBuf::from);
            let ctx = ServerContext::new(cfg, log_dir);
            let mut api = ctx.get_status_api();
            // Igual que en el servidor HTTP (/status/api): si este comando corre,
            // el status-server es observable por definición.
            http::apply_status_server_override(&mut api);
            let heavy = ctx.compute_heavy_now();
            let md = build_report_markdown(&api, &heavy, &ctx.redactor, &ctx.log_dir);
            print!("{md}");
            Ok(0)
        }
        "diag" => {
            ensure_no_leftover_args(args)?;
            let cfg = RuntimeConfig::from_env();
            let rc = diag::run_diag_cmd(&cfg, None)?;
            Ok(u8::try_from(rc).unwrap_or(1))
        }
        other => Err(format!(
            "Subcomando no reconocido: '{other}'. Usa 'comfyctl --help'."
        )),
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(code),
        Err(msg) => {
            eprintln!("[comfyctl] ERROR: {msg}");
            ExitCode::from(2)
        }
    }
}
