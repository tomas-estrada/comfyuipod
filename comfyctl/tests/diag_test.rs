//! Tests del comando `diag`: contrato `[DIAG][OK|WARN|FAIL] <key>: <detail>`,
//! resumen con conteos y redacción (`§4.1` / `§10.2`).

use comfyctl::config::RuntimeConfig;
use comfyctl::diag::run_diag_cmd;
use std::collections::BTreeMap;
use std::fs;

#[test]
fn diag_emite_el_contrato_con_probes_apuntados_y_redacta() {
    // `comfyctl diag` no tenia NINGUNA prueba: el contrato entero, el parseo
    // de los probes (`OK|...`, `WARN|...`, sin `|` -> FAIL) y la redaccion de
    // su salida eran letra muerta para la suite. Aqui se apuntan los probes de
    // Python a scripts falsos (COMFY_PROBES_DIR) y el binario de GPU a uno
    // inexistente para que el resultado NO dependa de la maquina. Las claves
    // que si dependen del entorno (ffmpeg, nginx, ldconfig) solo se exigen al
    // contrato y al recuento, nunca a su nivel.
    let tmp = std::env::temp_dir().join("comfyctl_diag_test");
    let _ = fs::remove_dir_all(&tmp);
    assert!(
        fs::create_dir_all(&tmp).is_ok(),
        "debe poder crear el directorio temporal"
    );

    // Probes falsos: rama OK (con un secreto que DEBE salir redactado), rama
    // WARN y rama sin `|` (FAIL con la primera linea como detalle).
    let secreto = "diag-secreto-largo-para-el-test-5566";
    assert!(
        fs::write(
            tmp.join("torch_cuda.py"),
            format!("print(\"OK|torch falso COMFY_PASS={secreto} matmul=ok\")\n")
        )
        .is_ok(),
        "debe poder escribir el probe torch"
    );
    assert!(
        fs::write(
            tmp.join("ort_cuda.py"),
            "print(\"WARN|onnx en modo degradado\")\n"
        )
        .is_ok(),
        "debe poder escribir el probe ort"
    );
    assert!(
        fs::write(
            tmp.join("python_imports.py"),
            "print(\"Traceback (most recent call last): ImportError falso\")\n"
        )
        .is_ok(),
        "debe poder escribir el probe de imports"
    );

    std::env::set_var("COMFY_PROBES_DIR", &tmp);
    std::env::set_var(
        "COMFY_PROBE_PYTHON",
        if cfg!(windows) { "python" } else { "python3" },
    );
    std::env::set_var("COMFY_DIAG_PROBE_TIMEOUT_S", "5");

    let mut vars = BTreeMap::new();
    vars.insert("COMFY_PASS".to_string(), secreto.to_string());
    vars.insert(
        "COMFY_NVIDIA_SMI_BIN".to_string(),
        "nvidia-smi-que-no-existe-para-el-test".to_string(),
    );
    let cfg = RuntimeConfig::from_map(&vars, None);

    let out_path = tmp.join("diag.txt");
    let code = run_diag_cmd(&cfg, Some(&out_path)).unwrap_or(-1);
    assert_eq!(code, 0, "diag siempre devuelve 0");

    std::env::remove_var("COMFY_PROBES_DIR");
    std::env::remove_var("COMFY_PROBE_PYTHON");
    std::env::remove_var("COMFY_DIAG_PROBE_TIMEOUT_S");

    let texto = fs::read_to_string(&out_path).unwrap_or_default();
    assert!(!texto.is_empty(), "diag debe escribir su fichero de salida");

    let lineas: Vec<&str> = texto.lines().collect();
    assert!(lineas.len() > 1, "diag debe emitir lineas mas el resumen");
    let resumen = lineas.last().copied().unwrap_or("");

    // 1. Contrato exacto en CADA linea de diagnostico: `[DIAG][NIVEL] key: detail`
    //    con key sin espacios y detail no vacio. La ultima linea (resumen) se
    //    valida en el paso 2.
    let mut n_ok = 0_u32;
    let mut n_warn = 0_u32;
    let mut n_fail = 0_u32;
    for (i, ln) in lineas.iter().enumerate() {
        if i + 1 == lineas.len() {
            continue;
        }
        let cuerpo = ln
            .strip_prefix("[DIAG][OK] ")
            .or_else(|| ln.strip_prefix("[DIAG][WARN] "))
            .or_else(|| ln.strip_prefix("[DIAG][FAIL] "));
        assert!(
            cuerpo.is_some(),
            "linea fuera del contrato [DIAG][NIVEL]: {ln:?}"
        );
        if ln.starts_with("[DIAG][OK] ") {
            n_ok = n_ok.saturating_add(1);
        } else if ln.starts_with("[DIAG][WARN] ") {
            n_warn = n_warn.saturating_add(1);
        } else {
            n_fail = n_fail.saturating_add(1);
        }
        let Some(cuerpo) = cuerpo else {
            continue;
        };
        let partes = cuerpo.split_once(": ");
        assert!(partes.is_some(), "la linea debe ser `key: detail`: {ln:?}");
        let Some((key, detail)) = partes else {
            continue;
        };
        assert!(
            !key.is_empty() && !key.contains(' '),
            "key invalida: {ln:?}"
        );
        assert!(!detail.is_empty(), "detail vacio: {ln:?}");
    }

    // 2. El resumen debe cuadrar exactamente con las lineas emitidas.
    let esperado = format!("[DIAG] resumen: {n_ok} ok, {n_warn} warn, {n_fail} fail");
    assert_eq!(
        resumen, esperado,
        "el resumen debe reflejar los contadores reales"
    );

    // 3. Las tres ramas del parseo de probes, deterministas en cualquier SO.
    assert!(
        texto.contains("[DIAG][OK] torch: torch falso COMFY_PASS=[REDACTED] matmul=ok"),
        "rama OK del probe + redaccion del secreto: {texto}"
    );
    assert!(
        texto.contains("[DIAG][WARN] ort: onnx en modo degradado"),
        "rama WARN del probe: {texto}"
    );
    assert!(
        texto.contains(
            "[DIAG][FAIL] python-imports: Traceback (most recent call last): ImportError falso"
        ),
        "rama sin `|` -> FAIL con la primera linea: {texto}"
    );

    // 4. GPU sin binario -> WARN determinista (no depende de la maquina).
    assert!(
        texto.contains(
            "[DIAG][WARN] gpu: nvidia-smi no instalado o no disponible en PATH (modo CPU)"
        ),
        "gpu con binario inexistente: {texto}"
    );

    // 5. Secciones siempre presentes. El nivel de disco/RAM depende ahora de si la
    //    sonda pudo medir (measurable), que varía según el SO: se exige la sección y
    //    que el nivel sea coherente con ese flag, no un nivel fijo.
    for prefijo in ["[DIAG][OK] cpu: nproc=", "[DIAG][OK] env: MODEL_PROFILE="] {
        assert!(texto.contains(prefijo), "falta {prefijo:?} en: {texto}");
    }
    for clave in ["disk", "ram"] {
        let linea = texto
            .lines()
            .find(|ln| ln.starts_with("[DIAG][") && ln.contains(&format!("] {clave}: ")));
        assert!(linea.is_some(), "falta la sección {clave} en: {texto}");
        let Some(linea) = linea else {
            continue;
        };
        let nivel = linea
            .strip_prefix("[DIAG][")
            .and_then(|r| r.split(']').next())
            .unwrap_or_default();
        assert!(
            nivel == "OK" || nivel == "FAIL",
            "nivel fuera de contrato en {clave}: {linea}"
        );
    }

    // 6. El secreto de configuracion jamas aparece en la salida de diag.
    assert!(
        !texto.contains(secreto),
        "fuga del secreto en la salida de diag: {texto}"
    );
}

// Una sonda que no midió nada no puede anunciarse como OK. `collect_memory_and_disk`
// marca `measurable: false` cuando `/proc/meminfo` es ilegible o `statvfs` falla, y
// antes `diag` emitía `[DIAG][OK]` sobre ceros: un diagnóstico que afirmaba un estado
// no medido y que contradecía a S5.
#[test]
fn diag_marca_fail_cuando_la_sonda_no_midio_nada() {
    use comfyctl::collect::procfs::{DiskStatus, MemoryStatus};
    use comfyctl::diag::resource_diag_levels;

    let mem_cero = MemoryStatus {
        total_mb: 0,
        used_mb: 0,
        available_mb: 0,
        swap_used_mb: 0,
        measurable: false,
    };
    let disk_cero = DiskStatus {
        path: "/".to_string(),
        total_gb: 0.0,
        used_gb: 0.0,
        free_gb: 0.0,
        measurable: false,
    };

    let (disk_level, ram_level) = resource_diag_levels(&mem_cero, &disk_cero);
    assert_eq!(disk_level, "FAIL", "disco sin datos no puede ser OK");
    assert_eq!(ram_level, "FAIL", "RAM sin datos no puede ser OK");

    let (disk_level, ram_level) = resource_diag_levels(
        &MemoryStatus {
            total_mb: 65536,
            used_mb: 32768,
            available_mb: 32768,
            swap_used_mb: 0,
            measurable: true,
        },
        &disk_cero,
    );
    assert_eq!(disk_level, "FAIL");
    assert_eq!(ram_level, "OK");

    let (disk_level, ram_level) = resource_diag_levels(
        &mem_cero,
        &DiskStatus {
            path: "/workspace".to_string(),
            total_gb: 500.0,
            used_gb: 250.0,
            free_gb: 250.0,
            measurable: true,
        },
    );
    assert_eq!(disk_level, "OK");
    assert_eq!(ram_level, "FAIL");
}

#[test]
fn diag_marca_ok_cuando_las_sondas_devolvieron_datos() {
    use comfyctl::collect::procfs::{DiskStatus, MemoryStatus};
    use comfyctl::diag::resource_diag_levels;

    let (disk_level, ram_level) = resource_diag_levels(
        &MemoryStatus {
            total_mb: 65536,
            used_mb: 32768,
            available_mb: 32768,
            swap_used_mb: 0,
            measurable: true,
        },
        &DiskStatus {
            path: "/workspace".to_string(),
            total_gb: 500.0,
            used_gb: 250.0,
            free_gb: 250.0,
            measurable: true,
        },
    );
    assert_eq!(disk_level, "OK");
    assert_eq!(ram_level, "OK");
}
