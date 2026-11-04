//! Requisito de almacenamiento derivado (política de arquitectura, punto 1).
//!
//! «180 GB is an earlier estimate, not a universal requirement or upper limit.»
//! Los umbrales no se inventan: los calcula
//! `scripts/storage_requirement.py` a partir de los manifiestos de modelos, la
//! imagen y las reservas, y el resultado se **hornea en la imagen** en
//! `/etc/comfyui/storage-requirement.json`.
//!
//! `comfyctl` lo lee al arrancar y S5 comprueba el número derivado. Si el
//! fichero no existe (imagen antigua, desarrollo), se cae a la tabla documentada
//! en [`ModelProfile::default_min_disk_gb`], que sigue siendo el respaldo.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;

/// Ruta donde la imagen publica el requisito derivado.
pub const STORAGE_REQUIREMENT_PATH: &str = "/etc/comfyui/storage-requirement.json";

/// Requisito derivado por perfil.
#[derive(Debug, Clone, Deserialize)]
pub struct ProfileStorage {
    /// Espacio libre mínimo para **arrancar** sin riesgo (lo que comprueba S5).
    /// Entero en GB, ya redondeado al alza por el script.
    pub start_gate_gb: u64,
    /// Tamaño de volumen necesario para guardarlo todo (lo que pide `comfy rent`).
    pub min_volume_gb: u64,
}

/// Requisito derivado completo, tal como lo escribe el script.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct StorageRequirement {
    #[serde(default)]
    pub profiles: BTreeMap<String, ProfileStorage>,
}

impl StorageRequirement {
    /// Carga el requisito derivado. `None` si no existe o no es válido: en ese
    /// caso el llamador usa el respaldo documentado.
    pub fn load(path: &Path) -> Option<Self> {
        let raw = std::fs::read_to_string(path).ok()?;
        serde_json::from_str(&raw).ok()
    }

    /// Carga desde la ruta por defecto de la imagen.
    pub fn load_default() -> Option<Self> {
        Self::load(Path::new(STORAGE_REQUIREMENT_PATH))
    }

    /// Puerta de arranque derivada para un perfil.
    ///
    /// El script ya redondea al alza: un umbral por debajo de lo necesario
    /// dejaría pasar un arranque que se queda sin disco a mitad de descarga.
    pub fn start_gate_gb(&self, profile: &str) -> Option<u64> {
        self.profiles.get(profile).map(|p| p.start_gate_gb)
    }

    /// Volumen derivado para un perfil.
    pub fn min_volume_gb(&self, profile: &str) -> Option<u64> {
        self.profiles.get(profile).map(|p| p.min_volume_gb)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> StorageRequirement {
        StorageRequirement {
            profiles: BTreeMap::from([
                (
                    "studio".to_string(),
                    ProfileStorage {
                        start_gate_gb: 63,
                        min_volume_gb: 248,
                    },
                ),
                (
                    "all".to_string(),
                    ProfileStorage {
                        start_gate_gb: 114,
                        min_volume_gb: 135,
                    },
                ),
            ]),
        }
    }

    #[test]
    fn start_gate_rounds_up() {
        let req = sample();
        assert_eq!(req.start_gate_gb("studio"), Some(63));
        assert_eq!(req.start_gate_gb("all"), Some(114));
    }

    #[test]
    fn unknown_profile_has_no_derived_value() {
        let req = sample();
        assert_eq!(req.start_gate_gb("nonexistent"), None);
        assert_eq!(req.min_volume_gb("nonexistent"), None);
    }

    #[test]
    fn missing_file_yields_none() {
        assert!(StorageRequirement::load(Path::new("/nonexistent/x.json")).is_none());
    }

    #[test]
    fn parses_the_shape_the_script_writes() {
        let json = r#"{"profiles":{"studio":{"start_gate_gb":63,"min_volume_gb":248}}}"#;
        let dir = std::env::temp_dir().join("comfy-storage-test");
        if std::fs::create_dir_all(&dir).is_err() {
            return;
        }
        let path = dir.join("storage-requirement.json");
        if std::fs::write(&path, json).is_err() {
            return;
        }
        if let Some(req) = StorageRequirement::load(&path) {
            assert_eq!(req.start_gate_gb("studio"), Some(63));
            assert_eq!(req.min_volume_gb("studio"), Some(248));
        }
        let _ = std::fs::remove_file(&path);
    }
}
