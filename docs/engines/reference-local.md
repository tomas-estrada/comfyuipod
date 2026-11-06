# Motor `reference-local`

Análisis local de referencias: cortes / beats / OCR / movimiento de cámara →
receta editable (plan Ultra, W06 / A09 / C02).

## Contrato

| Verbo | Entrada | Salida |
|-------|---------|--------|
| `list` | — | catálogo de conectores + ops |
| `verify` | submotor (ignorado) | `ok=True` si ffprobe disponible y ningún conector de pago activo |
| `install` | submotor | `ok=False` (no requiere instalación) |
| `run` | `EngineRunSpec` con `op='analyze_reference'` | `EditableRecipe.to_dict()` |

## Backend real

`comfy_studio.inspiration.derive_recipe` + `connector_catalog` + `assert_no_paid_connector`.

Requisito: `ffprobe` en PATH (vía `media_bins.find_ffprobe`).

## Ops

- `analyze_reference`:
  - `job.cuts`: lista de `{start_s, end_s}`.
  - `job.beats`: lista de segundos.
  - `job.titles`: lista de `{text, start_s, origin?}`.
  - `job.camera_moves`: lista de strings.
  - `job.brief_shots` (opcional): int → dispara adaptación.

## Esquema de salida (`run`)

```json
{
  "ok": true,
  "simulated": false,
  "payload": {
    "recipe": {
      "reference_id": "...",
      "steps": [...],
      "origin_summary": {"observed": N, "inferred": N, "adapted": N},
      "notes": "...",
      "disclaimer": "análisis descriptivo; no predice viralidad ni garantiza resultados"
    },
    "duration_s": 0.003
  }
}
```

## Nota de honestidad (A09)

- `simulated=False` solo cuando `derive_recipe` ejecutó.
- Si falta ffprobe → `ok=False` tipificado, nunca `simulated=True`.
- `heygen_reverse` se declara `FORBIDDEN_PAID` en el catálogo; nunca se invoca.
- El `disclaimer` se conserva siempre; no se promete viralidad.
