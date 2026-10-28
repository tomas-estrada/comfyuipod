//! `collect::models` — Catálogo de los 26 modelos esperados por fase/perfil (O-06 / O-11)
//! e inventario en disco de `/workspace/comfyui-data/models`.

use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Copy)]
pub struct ExpectedModel {
    pub phase: &'static str,
    pub subdir: &'static str,
    pub filename: &'static str,
    pub profiles: &'static [&'static str],
}

pub const EXPECTED_MODELS: [ExpectedModel; 26] = [
    // --- Fase 1 (12 ficheros esenciales) ---
    ExpectedModel {
        phase: "phase1",
        subdir: "insightface/models/buffalo_l",
        filename: "det_10g.onnx",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "insightface",
        filename: "inswapper_128.onnx",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "facerestore_models",
        filename: "codeformer-v0.1.0.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "facerestore_models",
        filename: "GFPGANv1.4.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "facedetection",
        filename: "detection_Resnet50_Final.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "facedetection",
        filename: "parsing_parsenet.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "ultralytics/bbox",
        filename: "face_yolov8m.pt",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "sams",
        filename: "sam_vit_b_01ec64.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "vae",
        filename: "sdxl.vae.safetensors",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "vae",
        filename: "vae-ft-mse-840000-ema-pruned.safetensors",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "upscale_models",
        filename: "4x-UltraSharp.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    ExpectedModel {
        phase: "phase1",
        subdir: "upscale_models",
        filename: "4x-AnimeSharp.pth",
        profiles: &["all", "general", "realistic", "anime", "minimal", "studio"],
    },
    // --- Fase 2 (14 ficheros pesados en segundo plano) ---
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "Juggernaut-XI-byRunDiffusion.safetensors",
        profiles: &["all", "general", "realistic", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "RealVisXL_V5.0_fp16.safetensors",
        profiles: &["all", "realistic"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "Realistic_Vision_V6.0_NV_B1_fp16.safetensors",
        profiles: &["all", "general", "realistic"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "waiIllustriousSDXL_v160.safetensors",
        profiles: &["all", "general", "anime"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "Illustrious-XL-v2.0.safetensors",
        profiles: &["all", "anime"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "NoobAI-XL-v1.1.safetensors",
        profiles: &["all", "anime"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "controlnet",
        filename: "controlnet-union-sdxl-promax.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "ipadapter",
        filename: "ip-adapter-plus_sdxl_vit-h.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "ipadapter",
        filename: "ip-adapter-plus-face_sdxl_vit-h.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "ipadapter",
        filename: "ip-adapter-plus_sd15.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "ipadapter",
        filename: "ip-adapter-plus-face_sd15.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "clip_vision",
        filename: "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "clip_vision",
        filename: "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors",
        profiles: &["all", "general", "realistic", "anime", "studio"],
    },
    ExpectedModel {
        phase: "phase2",
        subdir: "checkpoints",
        filename: "flux1-dev-fp8.safetensors",
        profiles: &["all"],
    },
];

/// Devuelve el número de modelos esperados en Fase 2 para el perfil activo (O-06).
pub fn phase2_expected_count(profile: &str) -> u32 {
    // `unreal` es una extensión de la familia studio: comparte el plan de Fase 2 (M3-D).
    let effective = match profile {
        "unreal" => "studio",
        other => other,
    };
    let mut count: u32 = 0;
    for item in EXPECTED_MODELS {
        if item.phase == "phase2" && item.profiles.contains(&effective) {
            count = count.saturating_add(1);
        }
    }
    count
}

fn walk_dir_files(dir: &Path) -> (usize, u64) {
    let mut count: usize = 0;
    let mut total_bytes: u64 = 0;
    let Ok(entries) = fs::read_dir(dir) else {
        return (0, 0);
    };
    for entry in entries.flatten() {
        let p = entry.path();
        let name = entry.file_name();
        if name.to_string_lossy().starts_with('.') {
            continue;
        }
        if p.is_dir() {
            let (c, b) = walk_dir_files(&p);
            count = count.saturating_add(c);
            total_bytes = total_bytes.saturating_add(b);
        } else if p.is_file() {
            count = count.saturating_add(1);
            if let Ok(meta) = p.metadata() {
                total_bytes = total_bytes.saturating_add(meta.len());
            }
        }
    }
    (count, total_bytes)
}

#[allow(clippy::cast_precision_loss)]
pub fn collect_model_inventory(comfy_data_dir: &Path, profile: &str) -> (Vec<String>, Vec<String>) {
    let models_root = comfy_data_dir.join("models");
    let mut folder_stats = Vec::new();
    let gib = 1024.0_f64 * 1024.0 * 1024.0;

    if let Ok(entries) = fs::read_dir(&models_root) {
        let mut subdirs: Vec<_> = entries
            .flatten()
            .filter(|e| e.path().is_dir() && !e.file_name().to_string_lossy().starts_with('.'))
            .collect();
        subdirs.sort_by_key(fs::DirEntry::file_name);
        for sub in subdirs {
            let name = sub.file_name().to_string_lossy().to_string();
            let (count, total_b) = walk_dir_files(&sub.path());
            let sz_gb = total_b as f64 / gib;
            folder_stats.push(format!("- `{name}/`: {count} archivo(s) ({sz_gb:.2} GiB)"));
        }
    }

    let valid_profile = match profile {
        "all" | "general" | "realistic" | "anime" | "minimal" | "studio" => profile,
        // M3-D: 'unreal' extiende 'studio' (mismo inventario Fase 1/2 + opt-in 3D)
        "unreal" => "studio",
        _ => "all",
    };

    let mut missing_models = Vec::new();
    for item in EXPECTED_MODELS {
        if item.profiles.contains(&valid_profile) {
            let fpath = models_root.join(item.subdir).join(item.filename);
            let is_present = fpath
                .metadata()
                .map(|m| m.is_file() && m.len() > 0)
                .unwrap_or(false);
            if !is_present {
                missing_models.push(format!(
                    "- `{}/{}` ({})",
                    item.subdir, item.filename, item.phase
                ));
            }
        }
    }

    (folder_stats, missing_models)
}
