//! Pruebas del sondeo de disco: la ruta que se mide debe ser el sistema de
//! ficheros que va a alojar `COMFY_DATA_DIR`, no uno arbitrario.

use comfyctl::collect::procfs::collect_memory_and_disk;
use comfyctl::sys;
use std::path::{Path, PathBuf};

fn tmp_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("comfyctl_disk_probe_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::create_dir_all(&dir);
    dir
}

#[test]
fn first_existing_ancestor_sube_hasta_el_primer_directorio_real() {
    let base = tmp_dir("ancestor");
    let real = base.join("volumen");
    assert!(std::fs::create_dir_all(&real).is_ok());

    // La ruta objetivo NO existe todavía (primer arranque, volumen sin crear aún):
    // se mide `real`, el primer ancestro que existe.
    let missing = real.join("comfyui-data").join("models");
    assert!(!missing.exists());
    assert_eq!(sys::first_existing_ancestor(&missing), real);

    // Un directorio que sí existe se mide a sí mismo.
    assert_eq!(sys::first_existing_ancestor(&real), real);

    // Propiedad general: lo devuelto existe y es ancestro (o la misma ruta).
    let picked = sys::first_existing_ancestor(&missing);
    assert!(picked.exists(), "lo medido debe existir: {picked:?}");
    assert!(missing.starts_with(&picked));

    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn first_existing_ancestor_sin_ancestros_existentes_cae_en_la_raiz() {
    if !cfg!(unix) {
        return;
    }
    let impossible = Path::new("/comfyctl-no-existe-xyz/a/b/c");
    assert!(!impossible.exists());
    assert_eq!(sys::first_existing_ancestor(impossible), Path::new("/"));
}

#[test]
fn disk_stats_gb_sobre_ruta_inexistente_falla_de_forma_cerrada() {
    if !cfg!(unix) {
        return;
    }
    // `statvfs` sobre una ruta que no existe devuelve error: el llamador debe
    // traducirlo en 0 GB libres (S5 fatal), nunca en un número optimista.
    let res = sys::disk_stats_gb(Path::new("/comfyctl-no-existe-xyz/a/b"));
    assert!(res.is_err(), "statvfs sobre ruta inexistente debe fallar");
}

#[test]
fn collect_memory_and_disk_mide_el_filesystem_de_comfy_data_dir() {
    let base = tmp_dir("data_dir");
    let data_dir = base.join("comfyui-data");
    // Aún no existe: es el caso real del primer arranque.
    assert!(!data_dir.exists());

    let (_mem, disk) = collect_memory_and_disk(&data_dir);

    assert_eq!(
        Path::new(&disk.path),
        base,
        "el disco medido debe ser el ancestro existente de COMFY_DATA_DIR ({base:?}), no un filesystem ajeno ({})",
        disk.path
    );

    let _ = std::fs::remove_dir_all(&base);
}
