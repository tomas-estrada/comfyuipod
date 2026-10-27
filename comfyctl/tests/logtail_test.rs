//! Test de rendimiento y exactitud de `logtail::tail_lines` sobre un fichero de 500 MB (< 50 ms, §8.3 / O-04).

use comfyctl::logtail::{resolve_log_path, tail_lines};
use std::fs::OpenOptions;
use std::io::{Seek, SeekFrom, Write};
use std::path::Path;
use std::time::Instant;

#[test]
fn tail_lines_500mb_under_50ms_and_whitelist_security() {
    let tmp_dir = std::env::temp_dir().join("comfyctl_logtail_test");
    let _ = std::fs::create_dir_all(&tmp_dir);
    let big_log = tmp_dir.join("big_comfyui.log");

    // Crear fichero disperso de 500 MiB y escribir 250 líneas conocidas al final
    let mut f = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&big_log)
        .unwrap_or_else(|_| std::process::abort());

    let offset_500mb: u64 = 500 * 1024 * 1024;
    let _ = f.seek(SeekFrom::Start(offset_500mb));
    for idx in 1..=250 {
        let _ = writeln!(f, "LINEA_FINAL_{idx:04}: mensaje de prueba del log");
    }
    let _ = f.flush();
    drop(f);

    let t0 = Instant::now();
    let lines = tail_lines(&big_log, 200);
    let elapsed = t0.elapsed();

    let _ = std::fs::remove_dir_all(&tmp_dir);

    assert_eq!(lines.len(), 200);
    assert_eq!(
        lines.first().map(String::as_str),
        Some("LINEA_FINAL_0051: mensaje de prueba del log")
    );
    assert_eq!(
        lines.last().map(String::as_str),
        Some("LINEA_FINAL_0250: mensaje de prueba del log")
    );
    assert!(
        elapsed.as_millis() < 50,
        "tail_lines sobre 500 MiB tardó {} ms (límite < 50 ms)",
        elapsed.as_millis()
    );

    // Verificar lista blanca estricta de resolve_log_path
    assert!(resolve_log_path("comfyui", Path::new("/var/log")).is_some());
    assert!(resolve_log_path("studio", Path::new("/var/log")).is_some());
    assert!(resolve_log_path("etc-passwd", Path::new("/var/log")).is_none());
    assert!(resolve_log_path("../etc/shadow", Path::new("/var/log")).is_none());
}
