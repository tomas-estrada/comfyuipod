use comfyctl::config::RuntimeConfig;
use comfyctl::idle::{
    decide, gather_idle_inputs, IdleAction, IdleDecision, IdleInputs, IdleWatcher,
};
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

fn cfg_from(vars: &[(&str, &str)]) -> RuntimeConfig {
    let map: BTreeMap<String, String> = vars
        .iter()
        .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
        .collect();
    RuntimeConfig::from_map(&map, None)
}

#[test]
fn idle_action_parse() {
    assert_eq!(IdleAction::parse("stop"), IdleAction::Stop);
    assert_eq!(IdleAction::parse(" STOP "), IdleAction::Stop);
    assert_eq!(IdleAction::parse("warn"), IdleAction::Warn);
    assert_eq!(IdleAction::parse("basura"), IdleAction::Warn);
}

#[test]
fn decide_activity_resets() {
    assert_eq!(
        decide(
            true,
            Duration::from_secs(9999),
            Duration::from_secs(60),
            IdleAction::Warn
        ),
        IdleDecision::Active
    );
}

#[test]
fn decide_under_timeout_is_idle_and_zero_disables() {
    assert_eq!(
        decide(
            false,
            Duration::from_secs(10),
            Duration::from_secs(60),
            IdleAction::Warn
        ),
        IdleDecision::Idle
    );
    assert_eq!(
        decide(
            false,
            Duration::from_secs(9999),
            Duration::ZERO,
            IdleAction::Stop
        ),
        IdleDecision::Idle
    );
}

#[test]
fn decide_at_timeout_warn_or_stop() {
    assert_eq!(
        decide(
            false,
            Duration::from_secs(60),
            Duration::from_secs(60),
            IdleAction::Warn
        ),
        IdleDecision::Warn
    );
    assert_eq!(
        decide(
            false,
            Duration::from_secs(120),
            Duration::from_secs(60),
            IdleAction::Stop
        ),
        IdleDecision::Stop
    );
}

#[test]
fn config_defaults_and_env() {
    let cfg = cfg_from(&[]);
    assert_eq!(cfg.comfy_idle_timeout_s, 3600);
    assert_eq!(cfg.comfy_idle_action, "warn");
    let cfg = cfg_from(&[("COMFY_IDLE_TIMEOUT_S", "0"), ("COMFY_IDLE_ACTION", "stop")]);
    assert_eq!(cfg.comfy_idle_timeout_s, 0);
    assert_eq!(cfg.comfy_idle_action, "stop");
    let watcher = IdleWatcher::new(&cfg);
    assert!(!watcher.enabled());
}

#[test]
fn watcher_observe_transitions_and_resets_on_activity() {
    let cfg = cfg_from(&[("COMFY_IDLE_TIMEOUT_S", "1")]);
    let mut w = IdleWatcher::new(&cfg);
    assert!(w.enabled());
    let idle_inputs = IdleInputs::default();
    // Recién observado: todavía bajo el timeout
    assert_eq!(w.observe(&idle_inputs, &cfg), IdleDecision::Idle);
    thread::sleep(Duration::from_millis(1100));
    // Timeout alcanzado: Warn (el side effect de log/bandera se emite una sola vez)
    assert_eq!(w.observe(&idle_inputs, &cfg), IdleDecision::Warn);
    assert_eq!(w.observe(&idle_inputs, &cfg), IdleDecision::Warn);
    // Actividad resetea el contador de inactividad
    let busy = IdleInputs {
        comfyui_queue_len: Some(1),
        studio_jobs_active: Some(0),
        download_active: false,
    };
    assert_eq!(w.observe(&busy, &cfg), IdleDecision::Active);
    assert_eq!(w.observe(&idle_inputs, &cfg), IdleDecision::Idle);
}

#[test]
fn gather_reads_comfyui_queue_over_loopback() {
    let Ok(listener) = TcpListener::bind("127.0.0.1:0") else {
        return;
    };
    let Ok(addr) = listener.local_addr() else {
        return;
    };
    let port = addr.port();
    thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut buf = [0_u8; 4096];
            let _ = stream.read(&mut buf);
            let body = r#"{"queue_running": [[1, "abc", {}, {}, []]], "queue_pending": []}"#;
            let resp = format!(
                "HTTP/1.0 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = stream.write_all(resp.as_bytes());
        }
    });
    let cfg = cfg_from(&[
        ("STUDIO_COMFYUI_URL", &format!("http://127.0.0.1:{port}")),
        ("STUDIO_SERVER_PORT", "1"), // puerto muerto: jobs_active = None
    ]);
    let inputs = gather_idle_inputs(&cfg);
    assert_eq!(inputs.comfyui_queue_len, Some(1));
    assert_eq!(inputs.studio_jobs_active, None);
    assert!(inputs.any_activity());
}
