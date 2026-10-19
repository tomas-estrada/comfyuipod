//! Punto de entrada de `comfyctl` (Fase 1: `strict-check`, `supervise`, `version`).

use comfyctl::config::RuntimeConfig;
use comfyctl::{strict, supervisor};
use pico_args::Arguments;
use std::process::ExitCode;

fn print_help() {
    println!(
        "comfyctl {} — Control de arranque, supervisión y observabilidad de ComfyUIPod\n\n\
         Subcomandos:\n  \
           comfyctl strict-check                          Valida invariantes S1..S9 y escribe /run/comfyui-strict.json\n  \
           comfyctl supervise [--external-status-server]  Ejecuta strict-check, Fase 1/2 y supervisa ComfyUI\n  \
           comfyctl version                               Muestra versión y metadatos de compilación",
        env!("CARGO_PKG_VERSION")
    );
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
            println!("comfyctl {}", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        "strict-check" => {
            let cfg = RuntimeConfig::from_env();
            let rc = strict::run_strict_check_cmd(&cfg)?;
            Ok(u8::try_from(rc).unwrap_or(1))
        }
        "supervise" => {
            let external_status = args.contains("--external-status-server");
            let cfg = RuntimeConfig::from_env();
            let rc = supervisor::run_supervise_cmd(&cfg, external_status)?;
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
