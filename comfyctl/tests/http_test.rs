//! Tests del servidor HTTP integrado (`tiny_http`): rutas 200, whitelist `/logs/<name>` -> 404,
//! `POST` -> 405, `HEAD` -> 200 sin cuerpo, acotación de `?n=` (1..=2000) y validación de estructura JSON de `/status/api` (§8.3).

use comfyctl::config::RuntimeConfig;
use comfyctl::http::{handle_request, ServerContext};
use std::fmt::Write as _;
use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tiny_http::Server;

fn send_raw_http(addr: &str, method: &str, path: &str) -> (u16, String, String) {
    let mut stream = TcpStream::connect(addr).unwrap_or_else(|_| std::process::abort());
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let req_str = format!("{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n");
    let _ = stream.write_all(req_str.as_bytes());
    let mut buf = Vec::new();
    let _ = stream.read_to_end(&mut buf);
    let raw = String::from_utf8_lossy(&buf).into_owned();
    let (headers_part, body_part) = raw.split_once("\r\n\r\n").unwrap_or((&raw, ""));
    let first_line = headers_part.lines().next().unwrap_or("");
    let status = first_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    (status, headers_part.to_string(), body_part.to_string())
}

#[test]
fn http_server_endpoints_404_405_head_and_schema() {
    let tmp_dir = std::env::temp_dir().join("comfyctl_http_test");
    let _ = fs::remove_dir_all(&tmp_dir);
    let _ = fs::create_dir_all(&tmp_dir);
    // 2500 líneas: con ellas se distingue el techo de `?n=` (2000) del valor
    // por defecto (200). El secreto va también en la ÚLTIMA línea para que
    // `/logs/comfyui?n=50` (últimas 50) siga ejercitando la redacción. Las
    // líneas benignas son breves a propósito: por encima de 32 KiB tiny_http
    // sirve la respuesta con `Transfer-Encoding: chunked` (su umbral
    // `chunked_threshold`, no un bug del servidor) y el cuerpo deja de poder
    // contarse por líneas sin decodificar el framing.
    let mut comfy_log = String::from("Linea 1 con secreto secret-http-pass\nLinea 2 OK\n");
    for i in 3..=2499 {
        let _ = writeln!(comfy_log, "{i}");
    }
    comfy_log.push_str("Linea final con secreto secret-http-pass\n");
    let _ = fs::write(tmp_dir.join("comfyui.log"), comfy_log);
    let _ = fs::write(
        tmp_dir.join("diag.txt"),
        "[DIAG][OK] cpu: nproc=8\n[DIAG] resumen: 1 ok, 0 warn, 0 fail\n",
    );

    let mut cfg = RuntimeConfig::from_env();
    cfg.comfy_pass = Some("secret-http-pass".to_string());
    cfg.supervisor_json_path = tmp_dir.join("sup.json");
    cfg.strict_json_path = tmp_dir.join("strict.json");

    let server = Server::http("127.0.0.1:0").unwrap_or_else(|_| std::process::abort());
    let addr = server
        .server_addr()
        .to_ip()
        .map(|a| a.to_string())
        .unwrap_or_default();
    let server = Arc::new(server);
    let ctx = Arc::new(ServerContext::new(cfg, tmp_dir.clone()));

    let srv_bg = Arc::clone(&server);
    let ctx_bg = Arc::clone(&ctx);
    let handle = thread::spawn(move || {
        for _ in 0..11 {
            if let Ok(Some(req)) = srv_bg.recv_timeout(Duration::from_secs(5)) {
                handle_request(&ctx_bg, req);
            }
        }
    });

    // 1. GET /status -> 200 HTML
    let (code_status, _, body_status) = send_raw_http(&addr, "GET", "/status");
    assert_eq!(code_status, 200);
    assert!(body_status.contains("Estado del Sistema"));

    // 2. GET /status/api -> 200 JSON con claves obligatorias del contrato
    let (code_api, _, body_api) = send_raw_http(&addr, "GET", "/status/api");
    assert_eq!(code_api, 200);
    let val: serde_json::Value = serde_json::from_str(&body_api).unwrap_or(serde_json::Value::Null);
    for req_key in [
        "generated_at",
        "image",
        "instance",
        "gpu",
        "memory",
        "disk",
        "comfyui",
        "phases",
        "phase_banner",
        "services",
        "errors_recent",
        "triage",
        "env_masked",
    ] {
        assert!(
            val.get(req_key).is_some(),
            "Falta clave obligatoria '{req_key}' en /status/api"
        );
    }

    // 3. GET /report.md -> 200 Markdown
    let (code_rep, _, body_rep) = send_raw_http(&addr, "GET", "/report.md");
    assert_eq!(code_rep, 200);
    assert!(body_rep.contains("## 0. Triage automático"));
    assert!(body_rep.contains("## 10. Arranque estricto"));

    // 4. GET /diag -> 200
    let (code_diag, _, body_diag) = send_raw_http(&addr, "GET", "/diag");
    assert_eq!(code_diag, 200);
    assert!(body_diag.contains("[DIAG] resumen:"));

    // 5. GET /logs/comfyui?n=50 -> 200 con redacción de secretos
    let (code_log, _, body_log) = send_raw_http(&addr, "GET", "/logs/comfyui?n=50");
    assert_eq!(code_log, 200);
    assert!(!body_log.contains("secret-http-pass"));
    assert!(body_log.contains("[REDACTED]"));

    // 6. GET /logs/etc-passwd -> 404 Not Found
    let (code_404, _, _) = send_raw_http(&addr, "GET", "/logs/etc-passwd");
    assert_eq!(code_404, 404);

    // 7. POST /status/api -> 405 Method Not Allowed y HEAD /status/api -> 200 sin body
    let (code_405, hdr_405, _) = send_raw_http(&addr, "POST", "/status/api");
    assert_eq!(code_405, 405);
    assert!(hdr_405.to_ascii_lowercase().contains("allow: get, head"));

    // 8. HEAD /status/api -> 200 sin cuerpo (el nombre del test lo prometía;
    //    antes no se emitía ninguna petición HEAD).
    let (code_head, _, body_head) = send_raw_http(&addr, "HEAD", "/status/api");
    assert_eq!(code_head, 200);
    assert!(
        body_head.is_empty(),
        "HEAD no debe devolver cuerpo: {} bytes",
        body_head.len()
    );

    // 9-11. Acotación de `?n=` (http::parse_query_n): `0` sube a 1, un valor
    // enorme baja a 2000 y un valor no numérico cae al defecto 200. Sin el
    // acotamiento, `?n=999999` serviría las 2500 líneas del fichero.
    let (code_n0, _, body_n0) = send_raw_http(&addr, "GET", "/logs/comfyui?n=0");
    assert_eq!(code_n0, 200);
    assert_eq!(
        body_n0.lines().count(),
        1,
        "?n=0 debe acotarse a la última línea"
    );
    // El conteo por sí solo no discrimina: sin el clamp, `n=0` devuelve un
    // cuerpo con solo `\n`, cuyo `lines().count()` TAMBIÉN es 1. Exigir el
    // contenido de la última línea separa el caso real del vacío.
    assert!(
        body_n0.contains("Linea final"),
        "?n=0 debe devolver la última línea del fichero, no un cuerpo vacío: {body_n0:?}"
    );

    let (code_nhuge, _, body_nhuge) = send_raw_http(&addr, "GET", "/logs/comfyui?n=999999");
    assert_eq!(code_nhuge, 200);
    assert_eq!(
        body_nhuge.lines().count(),
        2000,
        "?n=999999 debe acotarse al techo de 2000 líneas"
    );

    let (code_nbad, _, body_nbad) = send_raw_http(&addr, "GET", "/logs/comfyui?n=abc");
    assert_eq!(code_nbad, 200);
    assert_eq!(
        body_nbad.lines().count(),
        200,
        "?n=abc no numérico debe caer al defecto de 200 líneas"
    );

    let _ = handle.join();
    let _ = fs::remove_dir_all(&tmp_dir);
}

#[test]
fn sanitize_bind_addr_rejects_non_loopback() {
    use comfyctl::http::sanitize_bind_addr;

    // Loopback válido: se conserva (IPv4 e IPv6).
    assert_eq!(
        sanitize_bind_addr("127.0.0.1:18189", 18189),
        "127.0.0.1:18189"
    );
    assert_eq!(
        sanitize_bind_addr("127.0.0.5:9000", 18189),
        "127.0.0.5:9000"
    );
    assert_eq!(sanitize_bind_addr("[::1]:18189", 18189), "[::1]:18189");

    // No loopback: cae al default 127.0.0.1:{puerto}.
    assert_eq!(
        sanitize_bind_addr("0.0.0.0:18189", 18189),
        "127.0.0.1:18189"
    );
    assert_eq!(
        sanitize_bind_addr("192.168.1.10:8080", 18189),
        "127.0.0.1:18189"
    );
    assert_eq!(sanitize_bind_addr("[::]:18189", 18189), "127.0.0.1:18189");

    // Inválido: también cae al default.
    assert_eq!(sanitize_bind_addr("no-es-una-ip", 18189), "127.0.0.1:18189");
    assert_eq!(sanitize_bind_addr("", 18189), "127.0.0.1:18189");
    assert_eq!(sanitize_bind_addr("127.0.0.1", 18189), "127.0.0.1:18189");
}
