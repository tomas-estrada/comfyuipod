use comfyctl::config::RuntimeConfig;
use comfyctl::strict::{evaluate_strict_checks, probe_studio_health, SystemSnapshot};
use comfyctl::supervisor::{
    build_studio_server_command, render_crash_diagnostics, run_supervision_loop,
    should_start_studio_server, spawn_studio_server_if_enabled, stop_studio_server,
    write_supervisor_json, SupervisorState,
};
use comfyctl::sys;
use std::collections::BTreeMap;
use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

fn allocate_ephemeral_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map_or(18190, |a| a.port())
}

fn write_studio_test_stub_script(dir: &Path, pid_file: Option<&Path>) -> PathBuf {
    let script_path = dir.join("studio_test_stub.py");
    let pid_record_snippet = pid_file.map_or_else(String::new, |p| {
        format!(
            "with open({:?}, 'w', encoding='utf-8') as f:\n    f.write(str(os.getpid()))\n",
            p.to_string_lossy()
        )
    });
    let script = format!(
        r#"import http.server, json, os

{pid_record_snippet}port = int(os.environ.get("STUDIO_SERVER_PORT", "18190"))

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/studio/api/health") or self.path.startswith("/health"):
            body = json.dumps({{
                "status": "ok",
                "service": "studio-server",
                "profile": "studio",
                "pid": os.getpid(),
            }}).encode()
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

http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"#
    );
    let _ = fs::write(&script_path, script);
    script_path
}

fn base_healthy_studio_snapshot(studio_ok: bool, studio_detail: &str) -> SystemSnapshot {
    SystemSnapshot {
        gpu_query_ok: true,
        gpu_name: "NVIDIA GeForce RTX 5090".to_string(),
        driver_version: "570.124.06".to_string(),
        vram_total_mb: 32607,
        ram_probe_ok: true,
        ram_total_mb: 65536,
        disk_free_gb: 200.0,
        workspace_writable: true,
        nginx_config_ok: true,
        nginx_detail: "nginx -t OK".to_string(),
        htpasswd_ok: true,
        htpasswd_missing: false,
        htpasswd_detail: "/etc/nginx/.htpasswd válido".to_string(),
        comfy_install_ok: true,
        comfy_install_detail: "/opt/ComfyUI/main.py presente".to_string(),
        cuda_torch_ok: true,
        cuda_torch_detail: "torch=2.9.1+cu128 cuda=12.8 device=NVIDIA GeForce RTX 5090 matmul=ok"
            .to_string(),
        studio_health_ok: studio_ok,
        studio_health_detail: studio_detail.to_string(),
        mcp_health_ok: true,
        mcp_health_detail: "MCP healthy en 127.0.0.1:18190/studio/api/mcp/health (HTTP 200)"
            .to_string(),
    }
}

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
fn ephemeral_api_token_file_is_consumed_even_when_env_provides_it() {
    // M3-C / R-11: /run/comfyui-api-token se borra SIEMPRE al supervisar; con la env definida
    // gana su valor, pero el fichero no debe sobrevivir al arranque.
    use comfyctl::config::take_ephemeral_api_token;

    let tmp_dir = std::env::temp_dir().join("comfyctl_ephemeral_token");
    let _ = fs::remove_dir_all(&tmp_dir);
    let _ = fs::create_dir_all(&tmp_dir);
    let token_path = tmp_dir.join("comfyui-api-token");

    let _ = fs::write(&token_path, "from-file-token\n");
    let from_file = take_ephemeral_api_token(&token_path, None);
    assert_eq!(from_file.as_deref(), Some("from-file-token"));
    assert!(
        !token_path.exists(),
        "el token efímero debía borrarse al consumirse"
    );

    let _ = fs::write(&token_path, "from-file-token-2\n");
    let from_env = take_ephemeral_api_token(&token_path, Some("from-env-token".to_string()));
    assert_eq!(from_env.as_deref(), Some("from-env-token"));
    assert!(
        !token_path.exists(),
        "el fichero del token debía borrarse aunque la env traiga STUDIO_API_TOKEN"
    );

    assert_eq!(take_ephemeral_api_token(&token_path, None), None);

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

    // 2. En perfil 'studio', el comando por defecto invoca '-m comfy_studio.server' (sin stub '-c'),
    //    propaga STUDIO_* y elimina COMFY_PASS
    let tmp_dir = std::env::temp_dir().join("comfyctl_sup_studio");
    let _ = fs::remove_dir_all(&tmp_dir);
    let _ = fs::create_dir_all(&tmp_dir);

    let ephemeral_port = allocate_ephemeral_port();

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
    // M3-C/M3-D: token MCP, secret de webhook y toggle MCP se propagan a studio-server.
    studio_vars.insert(
        "STUDIO_API_TOKEN".to_string(),
        "token-placeholder-no-real".to_string(),
    );
    studio_vars.insert(
        "STUDIO_WEBHOOK_SECRET".to_string(),
        "webhook-placeholder-no-real".to_string(),
    );
    studio_vars.insert("STUDIO_ENABLE_MCP".to_string(), "0".to_string());
    let cfg_studio = RuntimeConfig::from_map(&studio_vars, Some("my-comfy-pass".to_string()));
    assert!(should_start_studio_server(&cfg_studio));
    assert_eq!(cfg_studio.studio_server_port, ephemeral_port);

    let cmd = build_studio_server_command(&cfg_studio);
    let args: Vec<String> = cmd
        .get_args()
        .map(|a| a.to_string_lossy().to_string())
        .collect();
    let has_module_flag = args.iter().any(|a| a == "-m");
    let has_module_name = args.iter().any(|a| a == "comfy_studio.server");
    assert!(
        has_module_flag && has_module_name,
        "El comando de producción debe ejecutar '-m comfy_studio.server', args={args:?}"
    );
    assert!(
        !args.iter().any(|a| a == "-c"),
        "El comando de producción no debe inyectar un stub '-c' en línea: args={args:?}"
    );

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
    assert_eq!(
        envs.get("STUDIO_API_TOKEN"),
        Some(&Some("token-placeholder-no-real".to_string()))
    );
    assert_eq!(
        envs.get("STUDIO_WEBHOOK_SECRET"),
        Some(&Some("webhook-placeholder-no-real".to_string()))
    );
    assert_eq!(envs.get("STUDIO_ENABLE_MCP"), Some(&Some("0".to_string())));

    // 2b. Sin token/secret configurados, el comando los ELIMINA del entorno heredado
    //     (nunca heredar valores de un padre que los tuviera definidos).
    let mut bare_vars = BTreeMap::new();
    bare_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    let cfg_bare = RuntimeConfig::from_map(&bare_vars, None);
    let bare_envs: BTreeMap<String, Option<String>> = build_studio_server_command(&cfg_bare)
        .get_envs()
        .map(|(k, v)| {
            (
                k.to_string_lossy().to_string(),
                v.map(|val| val.to_string_lossy().to_string()),
            )
        })
        .collect();
    assert_eq!(bare_envs.get("STUDIO_API_TOKEN"), Some(&None));
    assert_eq!(bare_envs.get("STUDIO_WEBHOOK_SECRET"), Some(&None));
    // STUDIO_ENABLE_MCP por defecto activo (1)
    assert_eq!(
        bare_envs.get("STUDIO_ENABLE_MCP"),
        Some(&Some("1".to_string()))
    );
    assert_eq!(bare_envs.get("COMFY_PASS"), Some(&None));

    // 2c. `unreal` pertenece a la familia studio: también levanta el servidor.
    let mut unreal_vars = BTreeMap::new();
    unreal_vars.insert("MODEL_PROFILE".to_string(), "unreal".to_string());
    assert!(should_start_studio_server(&RuntimeConfig::from_map(
        &unreal_vars,
        None
    )));

    // 3. Con opt-in explícito de fixture de test (STUDIO_SERVER_SCRIPT), spawn_studio_server_if_enabled
    //    levanta el listener en 127.0.0.1:{port} y probe_studio_health responde HTTP 200.
    if cfg!(unix) {
        let stub_script = write_studio_test_stub_script(&tmp_dir, None);
        studio_vars.insert(
            "STUDIO_SERVER_SCRIPT".to_string(),
            stub_script.to_string_lossy().to_string(),
        );
        let pass = Some("my-comfy-pass".to_string());
        let cfg_with_stub = RuntimeConfig::from_map(&studio_vars, pass);
        let mut child_opt = spawn_studio_server_if_enabled(&cfg_with_stub);
        assert!(
            child_opt.is_some(),
            "studio-server debía arrancar con fixture de test explícito"
        );
        let (ok, detail) = probe_studio_health(&cfg_with_stub);
        stop_studio_server(&mut child_opt);
        assert!(
            ok,
            "studio-server levantado con fixture de test debía responder HTTP 200: {detail}"
        );
    }

    let _ = fs::remove_dir_all(&tmp_dir);
}

#[test]
fn supervisor_studio_broken_import_fails_closed_and_marks_s11_unhealthy() {
    if !cfg!(unix) {
        return;
    }

    let tmp_dir = std::env::temp_dir().join("comfyctl_sup_broken_import");
    let _ = fs::remove_dir_all(&tmp_dir);
    let empty_py_dir = tmp_dir.join("empty_site");
    let broken_py_dir = tmp_dir.join("broken_site");
    let pkg_dir = broken_py_dir.join("comfy_studio");
    let _ = fs::create_dir_all(&empty_py_dir);
    let _ = fs::create_dir_all(&pkg_dir);

    // Módulo comfy_studio.server con ImportError interno simulando instalación rota
    let _ = fs::write(pkg_dir.join("__init__.py"), "");
    let _ = fs::write(
        pkg_dir.join("server.py"),
        "import nonexistent_studio_internal_dep\n",
    );

    // 1. Sin módulo comfy_studio.server y sin opt-in de test -> fail-closed (None + S11 unhealthy)
    let port_missing = allocate_ephemeral_port();
    let mut missing_vars = BTreeMap::new();
    missing_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    missing_vars.insert("STUDIO_SERVER_PORT".to_string(), port_missing.to_string());
    missing_vars.insert(
        "STUDIO_DATA_DIR".to_string(),
        tmp_dir.join("data_missing").to_string_lossy().to_string(),
    );
    missing_vars.insert(
        "STUDIO_PYTHONPATH".to_string(),
        empty_py_dir.to_string_lossy().to_string(),
    );
    let cfg_missing = RuntimeConfig::from_map(&missing_vars, None);
    let mut child_missing = spawn_studio_server_if_enabled(&cfg_missing);
    assert!(
        child_missing.is_none(),
        "spawn_studio_server_if_enabled debe ser fail-closed (None) cuando falta comfy_studio.server"
    );
    stop_studio_server(&mut child_missing);

    // 2. Con comfy_studio.server presente pero lanzando ImportError interno -> fail-closed:
    //    NO levanta ningún stub HTTP, devuelve None, probe_studio_health devuelve false y S11 es fatal
    let port_broken = allocate_ephemeral_port();
    let mut broken_vars = BTreeMap::new();
    broken_vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    broken_vars.insert("STUDIO_SERVER_PORT".to_string(), port_broken.to_string());
    broken_vars.insert(
        "STUDIO_DATA_DIR".to_string(),
        tmp_dir.join("data_broken").to_string_lossy().to_string(),
    );
    broken_vars.insert(
        "STUDIO_PYTHONPATH".to_string(),
        broken_py_dir.to_string_lossy().to_string(),
    );
    let cfg_broken = RuntimeConfig::from_map(&broken_vars, None);
    let mut child_broken = spawn_studio_server_if_enabled(&cfg_broken);
    assert!(
        child_broken.is_none(),
        "spawn_studio_server_if_enabled debe ser fail-closed (None) ante ImportError en comfy_studio.server"
    );
    stop_studio_server(&mut child_broken);

    let (health_ok, health_detail) = probe_studio_health(&cfg_broken);
    assert!(
        !health_ok,
        "probe_studio_health debe devolver false cuando comfy_studio.server falla con ImportError: {health_detail}"
    );

    let snap = base_healthy_studio_snapshot(health_ok, &health_detail);
    let rep = evaluate_strict_checks(&cfg_broken, &snap);
    assert_eq!(rep.result, "fatal");
    let s11 = rep.checks.iter().find(|c| c.id == "S11");
    assert!(
        matches!(s11, Some(c) if c.name == "studio_health" && c.status == "fatal"),
        "S11 debe marcarse fatal ante arranque roto de studio-server: {s11:?}"
    );

    let _ = fs::remove_dir_all(&tmp_dir);
}

fn read_pid_from_file(path: &Path) -> Option<u32> {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| s.trim().parse::<u32>().ok())
}

fn read_supervisor_state_file(path: &Path) -> Option<SupervisorState> {
    let raw = fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

#[test]
#[allow(clippy::too_many_lines)]
fn supervisor_respawns_studio_server_independently_while_comfyui_healthy_and_exhausted() {
    if !cfg!(unix) {
        return;
    }

    sys::set_shutdown_requested(false);

    let tmp_dir = std::env::temp_dir().join("comfyctl_sup_studio_respawn");
    let _ = fs::remove_dir_all(&tmp_dir);
    let comfy_app_dir = tmp_dir.join("comfy_app");
    let comfy_data_dir = tmp_dir.join("comfy_data");
    let studio_data_dir = tmp_dir.join("studio_data");
    let _ = fs::create_dir_all(&comfy_app_dir);
    let _ = fs::create_dir_all(&comfy_data_dir);
    let _ = fs::create_dir_all(&studio_data_dir);

    // Dummy ComfyUI main.py que permanece vivo hasta recibir señal
    let dummy_comfy_main = r"import signal, sys, time
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(143))
while True:
    time.sleep(0.1)
";
    let _ = fs::write(comfy_app_dir.join("main.py"), dummy_comfy_main);
    let _ = fs::write(comfy_app_dir.join("extra_model_paths.yaml"), "{}\n");

    let studio_pid_file = tmp_dir.join("studio.pid");
    let stub_script = write_studio_test_stub_script(&tmp_dir, Some(&studio_pid_file));
    let studio_port = allocate_ephemeral_port();
    let sup_json_path = tmp_dir.join("comfyui-supervisor.json");

    let mut vars = BTreeMap::new();
    vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    vars.insert("COMFY_MAX_RESTARTS".to_string(), "0".to_string());
    vars.insert("COMFY_RESTART_DELAY".to_string(), "1".to_string());
    vars.insert(
        "COMFY_APP_DIR".to_string(),
        comfy_app_dir.to_string_lossy().to_string(),
    );
    vars.insert(
        "COMFY_DATA_DIR".to_string(),
        comfy_data_dir.to_string_lossy().to_string(),
    );
    vars.insert(
        "STUDIO_DATA_DIR".to_string(),
        studio_data_dir.to_string_lossy().to_string(),
    );
    vars.insert("STUDIO_SERVER_PORT".to_string(), studio_port.to_string());
    vars.insert(
        "STUDIO_SERVER_SCRIPT".to_string(),
        stub_script.to_string_lossy().to_string(),
    );
    vars.insert(
        "COMFY_SUPERVISOR_JSON".to_string(),
        sup_json_path.to_string_lossy().to_string(),
    );

    let cfg = RuntimeConfig::from_map(&vars, None);
    let mut studio_child = spawn_studio_server_if_enabled(&cfg);
    assert!(
        studio_child.is_some(),
        "studio-server inicial debía arrancar"
    );
    let studio_pid_1 = studio_child.as_ref().map_or(0, std::process::Child::id);
    assert!(studio_pid_1 > 0);
    let (ok_initial, detail_initial) = probe_studio_health(&cfg);
    assert!(
        ok_initial,
        "studio-server inicial debía estar healthy: {detail_initial}"
    );

    let cfg_loop = cfg.clone();
    let loop_handle = thread::spawn(move || {
        let mut sup_state = SupervisorState::new(cfg_loop.comfy_max_restarts);
        let _ = write_supervisor_json(&cfg_loop.supervisor_json_path, &sup_state);
        let res = run_supervision_loop(&cfg_loop, &mut sup_state, &mut studio_child);
        stop_studio_server(&mut studio_child);
        res
    });

    // Esperar a que ComfyUI esté en estado 'running'
    let mut comfy_pid_1: u32 = 0;
    let wait_start = Instant::now();
    while wait_start.elapsed() < Duration::from_secs(10) {
        if let Some(st) = read_supervisor_state_file(&sup_json_path) {
            if st.state == "running" {
                if let Some(pid) = st.pid {
                    comfy_pid_1 = pid;
                    break;
                }
            }
        }
        thread::sleep(Duration::from_millis(50));
    }
    assert!(
        comfy_pid_1 > 0 && sys::is_pid_alive(comfy_pid_1),
        "ComfyUI debía estar corriendo con un PID válido (comfy_pid_1={comfy_pid_1})"
    );

    // Fase A: Matar studio-server mientras ComfyUI permanece sano -> debe relanzarse con nuevo PID
    let _ = sys::send_signal(studio_pid_1, 9);
    let mut studio_pid_2: u32 = 0;
    let respawn_start = Instant::now();
    while respawn_start.elapsed() < Duration::from_secs(10) {
        if let Some(pid) = read_pid_from_file(&studio_pid_file) {
            if pid != studio_pid_1 && sys::is_pid_alive(pid) {
                studio_pid_2 = pid;
                break;
            }
        }
        thread::sleep(Duration::from_millis(50));
    }
    assert!(
        studio_pid_2 > 0 && studio_pid_2 != studio_pid_1,
        "studio-server debía relanzarse con nuevo PID mientras ComfyUI sigue sano (pid_1={studio_pid_1}, pid_2={studio_pid_2})"
    );
    let (ok_after_respawn, detail_after_respawn) = probe_studio_health(&cfg);
    assert!(
        ok_after_respawn,
        "studio-server relanzado debía responder HTTP 200: {detail_after_respawn}"
    );
    // Verificar que ComfyUI no se interrumpió ni reinició
    assert!(
        sys::is_pid_alive(comfy_pid_1),
        "ComfyUI debía seguir vivo con el mismo PID {comfy_pid_1}"
    );
    let st_after = read_supervisor_state_file(&sup_json_path);
    let comfy_unchanged = st_after
        .as_ref()
        .is_some_and(|st| st.state == "running" && st.pid == Some(comfy_pid_1) && st.attempt == 1);
    assert!(
        comfy_unchanged,
        "El estado de ComfyUI debía permanecer running/attempt=1: {st_after:?}"
    );

    // Fase B: Llevar ComfyUI a estado 'exhausted' (COMFY_MAX_RESTARTS=0) y verificar que
    // studio-server sigue supervisado durante la espera agotada de ComfyUI
    let _ = sys::send_signal(comfy_pid_1, 9);
    let exhausted_start = Instant::now();
    let mut reached_exhausted = false;
    while exhausted_start.elapsed() < Duration::from_secs(15) {
        if let Some(st) = read_supervisor_state_file(&sup_json_path) {
            if st.state == "exhausted" {
                reached_exhausted = true;
                break;
            }
        }
        thread::sleep(Duration::from_millis(50));
    }
    assert!(
        reached_exhausted,
        "ComfyUI debía entrar en estado 'exhausted' tras caer con COMFY_MAX_RESTARTS=0"
    );

    // Matar studio-server mientras ComfyUI está en 'exhausted' -> debe volver a relanzarse
    let _ = sys::send_signal(studio_pid_2, 9);
    let mut studio_pid_3: u32 = 0;
    let exhausted_respawn_start = Instant::now();
    while exhausted_respawn_start.elapsed() < Duration::from_secs(10) {
        if let Some(pid) = read_pid_from_file(&studio_pid_file) {
            if pid != studio_pid_2 && pid != studio_pid_1 && sys::is_pid_alive(pid) {
                studio_pid_3 = pid;
                break;
            }
        }
        thread::sleep(Duration::from_millis(50));
    }
    assert!(
        studio_pid_3 > 0 && studio_pid_3 != studio_pid_2,
        "studio-server debía relanzarse también durante el estado exhausted de ComfyUI (pid_2={studio_pid_2}, pid_3={studio_pid_3})"
    );
    let (ok_exhausted_respawn, detail_exhausted_respawn) = probe_studio_health(&cfg);
    assert!(
        ok_exhausted_respawn,
        "studio-server relanzado en exhausted debía responder HTTP 200: {detail_exhausted_respawn}"
    );

    // Solicitar apagado limpio y esperar al hilo supervisor
    sys::set_shutdown_requested(true);
    let join_res = loop_handle.join();
    sys::set_shutdown_requested(false);
    assert!(
        matches!(join_res, Ok(Ok(()))),
        "run_supervision_loop debía terminar limpiamente al solicitar apagado"
    );

    let _ = fs::remove_dir_all(&tmp_dir);
}

#[test]
fn crash_diagnostics_dump_is_redacted_before_print_and_log() {
    // M3-SCR-005: el volcado de crash (stdout + /var/log/supervisor.log) pasa por SecretRedactor.
    use comfyctl::redact::SecretRedactor;

    let api_token = "token-placeholder-no-real";
    let comfy_pass = "comfy-pass-placeholder-no-real";
    let mut vars = BTreeMap::new();
    vars.insert("MODEL_PROFILE".to_string(), "studio".to_string());
    vars.insert("STUDIO_API_TOKEN".to_string(), api_token.to_string());
    vars.insert("COMFY_PASS".to_string(), comfy_pass.to_string());
    let cfg = RuntimeConfig::from_map(&vars, None);
    let redactor = SecretRedactor::from_config(&cfg);

    let reason = format!(
        "token literal {api_token} y asignación STUDIO_API_TOKEN={api_token} / COMFY_PASS={comfy_pass}"
    );
    let dump = render_crash_diagnostics(2, -9, 42, &reason, &redactor);

    assert!(!dump.contains(api_token), "fuga del token: {dump}");
    assert!(!dump.contains(comfy_pass), "fuga de COMFY_PASS: {dump}");
    assert!(dump.contains("[REDACTED]"), "{dump}");
    assert!(dump.contains("intento #2"), "cabecera ausente: {dump}");
}
