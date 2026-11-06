# Motor `scene-optimizer`

Validación de composición y plan de cámara antes del render costoso (plan Ultra,
W11 / A13 / C20).

## Contrato

| Verbo | Entrada | Salida |
|-------|---------|--------|
| `list` | — | comprobaciones disponibles + ops |
| `verify` | `CameraPlan` dict | `ok=True` si `CameraPlan.validate()` pasa |
| `install` | submotor | `ok=False` (no requiere instalación) |
| `run` | `EngineRunSpec` con `op='check_plan'` | `CompositionReport.to_dict()` |

## Backend real

`comfy_studio.cinematography.check_composition` + `CameraPlan.validate`.

## Ops

- `check_plan`:
  - `job.plan`: dict con `shot_id`, `keyframes`, `focal_mm`, `sensor_mm`.
  - `job.subject_position`: `[x, y, z]`.
  - `job.subject_radius`: float.
  - `job.obstacles` (opcional): lista de `{position, radius}`.
  - `job.min_distance` (opcional): float, default 0.5.

## Esquema de salida (`run`)

```json
{
  "ok": true,
  "simulated": false,
  "payload": {
    "report": {
      "ok": true,
      "collisions": [],
      "occlusions": [],
      "warnings": []
    }
  }
}
```

## Nota de honestidad (A13)

- Fail-closed: plan inválido o con colisiones → `ok=False`.
- `simulated=False` solo cuando `check_composition` ejecutó realmente.
- Nunca finge éxito ni rellena fachadas como observadas.
