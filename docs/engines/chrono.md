# Motor `chrono`

Proxy cinemático determinista CPU de física/articulaciones (plan Ultra, W12 /
A14 / C18).

## Contrato

| Verbo | Entrada | Salida |
|-------|---------|--------|
| `list` | — | maniobras disponibles + flags de proxy |
| `verify` | `SimSpec` dict | `ok=True` si la spec es válida |
| `install` | submotor | `ok=False` (no requiere instalación) |
| `run` | `EngineRunSpec` con `op='simulate'` | `SimResult.to_dict()` |

## Backend real

`comfy_studio.physics.simulate` — solver cinemático determinista CPU.

**No** es el solver Chrono/Blender real; es un proxy verificable antes del
stage caro (A14).

## Ops

- `simulate`:
  - `job.spec.maneuver`: string (p.ej. `"free_fall"`).
  - `job.spec.seed`: int.
  - `job.spec.fps`: int, default 24.
  - `job.spec.duration_s`: float, default 2.0.
  - `job.spec.initial_height_m`: float.
  - `job.spec.joints`: lista de `{name, axis, min_deg, max_deg}`.

## Esquema de salida (`run`)

```json
{
  "ok": true,
  "simulated": false,
  "payload": {
    "spec_hash": "abc123...",
    "frame_count": 48,
    "is_proxy": true,
    "proxy_note": "proxy cinemático CPU; la fidelidad tras render neural NO se deduce de esto (A14)",
    "contact_frame": 23,
    "frames": [
      {"frame": 0, "t": 0.0, "height_m": 10.0, "velocity_mps": 0.0, "in_contact": false, "joint_angles_deg": {}}
    ]
  }
}
```

## Nota de honestidad (A14)

- `is_proxy=True` siempre presente en el payload.
- `simulated=False` solo cuando `simulate()` ejecutó realmente.
- Nunca se reclama validación GPU/neural.
- Reproducible: misma `(seed, params)` → mismo `spec_hash`.
