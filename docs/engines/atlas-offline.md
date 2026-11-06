# Motor `atlas-offline`

Empaquetado y verificación del bundle del atlas del Aburrá (plan Ultra, W07 / C07).

## Contrato

| Verbo | Entrada | Salida |
|-------|---------|--------|
| `list` | — | estadísticas del bundle si `STUDIO_ATLAS_ROOT` está configurado |
| `verify` | submotor (ignorado) | `ok=True` si el bundle es íntegro |
| `install` | submotor | `ok=False` (no requiere instalación) |
| `run` | `EngineRunSpec` con `op='verify_bundle'` | informe de `verify_bundle` |

## Backend real

`comfy_studio.atlas.packaging.verify_bundle(root, require_files=True)`.

Configuración: variable de entorno `STUDIO_ATLAS_ROOT` apuntando al directorio del
bundle.

## Esquema de salida (`run`)

```json
{
  "ok": true,
  "simulated": false,
  "payload": {
    "report": {
      "root": "/opt/studio-atlas",
      "ok": true,
      "errors": [],
      "checks": {
        "catalog_integrity": {"ok": true, "checked": 1, "error_count": 0},
        "manifest_files": {"ok": true, "files": 1, "mismatches": []},
        "licenses_docs": {"attribution": true, "sources": true, "ok": true},
        "osm": {"present": true, "ok": true}
      },
      "stats": {"shipped": 1}
    }
  }
}
```

## Nota de honestidad

- `simulated=False` solo cuando `verify_bundle` devolvió sin excepción.
- Raíz ausente o bundle corrupto → `ok=False` tipificado, nunca `simulated=True`.
- Nunca lanza; errores de `verify_bundle` se capturan y tipifican.
