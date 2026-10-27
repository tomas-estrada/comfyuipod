use comfyctl::config::RuntimeConfig;
use comfyctl::strict::probe_studio_health;
use comfyctl::supervisor::{
    build_studio_server_command, should_start_studio_server, spawn_studio_server_if_enabled,
    stop_studio_server, write_supervisor_json, SupervisorState,
};
use comfyctl::sys;
use std::collections::BTreeMap;
use std::fs;
use std::net::TcpListener;

#[test]
fn supervisor_state_machine_respects_o02_sigterm_and_healthy_uptime() {
    let mut st = SupervisorState::new(3);
    assert_eq!(st.state, "starting");

    // Intento 1: cae tras 10s con rc=137 (OOM) -> restarts=1
    st.on_child_spawned(1001, 1_700_000_000);
    assert_eq!(st.state, "running");
    assert_eq!(st.attempt, 1);
    let cont1 = st.on_child_exit(137, 10, 600, 1_700_000_010);
    assert!(cont1);
    assert_eq!(st.state, "down");
    assert_eq!(st.restarts, 1);
    assert_eq!(st.healthy_resets, 0);

    // Intento 2: recibe SIGTERM manual (rc=143) tras 20s -> NO incrementa restarts (O-02)
    st.on_child_spawned(1002, 1_700_000_015);
    let cont2 = st.on_child_exit(143, 20, 600, 1_700_000_035);
    assert!(cont2);
    assert_eq!(st.restarts, 1);

    // Intento 3: vive 650s (>= 600s) y luego cae con rc=1 -> resetea restarts a 0, incrementa healthy_resets=1, y luego suma 1 por la caída actual
    st.on_child_spawned(1003, 1_700_000_040);
    let cont3 = st.on_child_exit(1, 650, 600, 1_700_000_690);
    assert!(cont3);
    assert_eq!(st.healthy_resets, 1);
    assert_eq!(st.restarts, 1);

    // Intento 4, 5, 6 caen rápido -> alcanza max=3 -> exhausted
    st.on_child_spawned(1004, 1_700_000_700);
    assert!(st.on_child_exit(1, 5, 600, 1_700_000_705));
    assert_eq!(st.restarts, 2);

    st.on_child_spawned(1005, 1_700_000_710);
    assert!(st.on_child_exit(1, 5, 600, 1_700_000_715));
    assert_eq!(st.restarts, 3);

    st.on_child_spawned(1006, 1_700_000_720);
    let cont6 = st.on_child_exit(139, 5, 600, 1_700_000_725);
    assert!(!cont6);
    assert_eq!(st.state, "exhausted");
    assert_eq!(st.restarts, 3);
}

#[test]
fn supervisor_atomic_write_and_secret_unlink() {
    let tmp_dir = std::env::temp_dir().join("comfyctl_sup_atomic");
    let _ = fs::create_dir_all(&tmp_dir);

    let sup_path = tmp_dir.join("comfyui-supervisor.json");
    let mut st = SupervisorState::new(5);
    st.on_child_spawned(4242, 1_700_000_000);
    assert!(write_supervisor_json(&sup_path, &st).is_ok());

    let raw = fs::read_to_string(&sup_path).unwrap_or_default();
    let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap_or_default();
    assert_eq!(
        parsed.get("state").and_then(serde_json::Value::as_str),
        Some("running")
    );
    assert_eq!(
        parsed.get("pid").and_then(serde_json::Value::as_u64),
        Some(4242)
    );

    let pass_path = tmp_dir.join("comfyui-pass");
    assert!(sys::atomic_write(&pass_path, b"super-secret-pass\n", 0o600).is_ok());
    assert!(pass_path.is_file());
    let secret = sys::read_and_unlink_secret(&pass_path);
    assert_eq!(secret.as_deref(), Some("super-secret-pass"));
    assert!(!pass_path.exists());

    let _ = fs::remove_dir_all(&tmp_dir);
}

#[test]
fn supervisor_starts_studio_server_only_in_studio_profile() {
    // 1. En cualquier perfil distinto de 'studio', should_start_studio_server es false y spawn devuelve None
    for non_studio in ["all", "general", "realistic", "anime", "minimal"] {
        let mut vars = BTreeMap::new();
        vars.insert("MODEL_PROFILE".to_string(), non_studio.to_string());
        let cfg = RuntimeConfig::from_map(&vars, None);
        assert!(
            !should_start_studio_server(&cfg),
            "should_start_studio_server debía ser false para {non_studio}"
        );
        assert!(spawn_studio_server_if_enabled(&cfg).is_none());
    }

    // 2. En perfil 'studio', should_start_studio_server es true y el comando propaga STUDIO_* y limpia COMFY_PASS
    let tmp_dir = std::env::temp_dir().join("comfyctl_sup_studio");
    let _ = fs::remove_dir_all(&tmp_dir);
    let _ = fs::create_dir_all(&tmp_dir);

    let ephemeral_port = TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map_or(18190, |a| a.port());

    let mut studio_vars = BTreeMap::new();
    studio_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    studio_vars.insert("STUDIO_SERVER_PORT".to_string(), ephemeral_port.to_string());
    studio_vars.insert(
        "STUDIO_DATA_DIR".to_string(),
        tmp_dir.to_string_lossy().to_string(),
    );
    studio_vars.insert(
        "STUDIO_LLM_KEY".to_string(),
        "YOUR_STUDIO_API_KEY_HERE".to_string(),
    );
    let cfg_studio = RuntimeConfig::from_map(&studio_vars, Some("my-comfy-pass".to_string()));
    assert!(should_start_studio_server(&cfg_studio));
    assert_eq!(cfg_studio.studio_server_port, ephemeral_port);

    let cmd = build_studio_server_command(&cfg_studio);
    let envs: BTreeMap<String, Option<String>> = cmd
        .get_envs()
        .map(|(k, v)| {
            (
                k.to_string_lossy().to_string(),
                v.map(|val| val.to_string_lossy().to_string()),
            )
        })
        .collect();
    assert_eq!(envs.get("COMFY_PASS"), Some(&None));
    assert_eq!(
        envs.get("STUDIO_LLM_KEY"),
        Some(&Some("YOUR_STUDIO_API_KEY_HERE".to_string()))
    );
    assert_eq!(
        envs.get("STUDIO_LLM_MODEL"),
        Some(&Some("qwen3-vl-plus".to_string()))
    );
    assert_eq!(
        envs.get("STUDIO_LLM_FALLBACK_MODEL"),
        Some(&Some("deepseek-chat".to_string()))
    );

    // 3. En entorno con python3 disponible, spawn_studio_server_if_enabled levanta el listener en 127.0.0.1:{port}
    if cfg!(unix) {
        let mut child_opt = spawn_studio_server_if_enabled(&cfg_studio);
        assert!(
            child_opt.is_some(),
            "studio-server debía arrancar en perfil studio"
        );
        let (ok, detail) = probe_studio_health(&cfg_studio);
        stop_studio_server(&mut child_opt);
        assert!(
            ok,
            "studio-server levantado por supervisor debía responder HTTP 200: {detail}"
        );
    }

    let _ = fs::remove_dir_all(&tmp_dir);
}
