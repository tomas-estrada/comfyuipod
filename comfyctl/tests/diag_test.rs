//! Pruebas de `comfyctl diag`: una sonda que no midió nada no puede anunciarse como OK.

use comfyctl::collect::procfs::{DiskStatus, MemoryStatus};
use comfyctl::diag::resource_diag_levels;

fn mem(total_mb: u64) -> MemoryStatus {
    MemoryStatus {
        total_mb,
        used_mb: total_mb / 2,
        available_mb: total_mb / 2,
        swap_used_mb: 0,
    }
}

fn disk(total_gb: f64) -> DiskStatus {
    DiskStatus {
        path: "/workspace".to_string(),
        total_gb,
        used_gb: total_gb / 2.0,
        free_gb: total_gb / 2.0,
    }
}

#[test]
fn diag_marca_fail_cuando_la_sonda_no_midio_nada() {
    // `/proc/meminfo` ilegible o `statvfs` fallido devuelven ceros. Anunciar eso como
    // `[DIAG][OK]` contradice a S5, que marca fatal justamente por esos 0 GB, y deja
    // al operador sin la pista de que la sonda —no el disco— es lo que falló.
    let (disk_level, ram_level) = resource_diag_levels(&mem(0), &disk(0.0));
    assert_eq!(disk_level, "FAIL", "disco sin datos no puede ser OK");
    assert_eq!(ram_level, "FAIL", "RAM sin datos no puede ser OK");

    // Cero en un campo y dato real en el otro: cada sonda se juzga por separado.
    let (disk_level, ram_level) = resource_diag_levels(&mem(65536), &disk(0.0));
    assert_eq!(disk_level, "FAIL");
    assert_eq!(ram_level, "OK");

    let (disk_level, ram_level) = resource_diag_levels(&mem(0), &disk(500.0));
    assert_eq!(disk_level, "OK");
    assert_eq!(ram_level, "FAIL");
}

#[test]
fn diag_marca_ok_cuando_las_sondas_devolvieron_datos() {
    let (disk_level, ram_level) = resource_diag_levels(&mem(65536), &disk(500.0));
    assert_eq!(disk_level, "OK");
    assert_eq!(ram_level, "OK");

    // Un disco genuinamente pequeño sigue siendo una medición válida: OK.
    let (disk_level, ram_level) = resource_diag_levels(&mem(2048), &disk(0.5));
    assert_eq!(disk_level, "OK");
    assert_eq!(ram_level, "OK");
}
