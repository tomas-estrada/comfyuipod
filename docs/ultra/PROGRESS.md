# ComfyUIPod Ultra — registro de progreso (pre-GPU)

Registro persistente para que otra sesión reanude sin depender de un sandbox
efímero. Fuente de verdad del plan: `upgrades-ultra-version/` (PR #64, merged).

Baseline revisada: `b54195f`, PR #62 integrada. Etapa: **Ultra pre-GPU**.
Sin GPU rentada ni APIs facturadas en esta fase.

## DAG de ejecución (backlog.json)

Camino crítico inicial: **W00 → W01 → W02 → {W03, W04} → W07**, luego fan-out
(W05/W06/W08/W09/W10…). W25 = integración final. W26 = validación GPU (no se
ejecuta ahora, `pending_separate_gpu_authorization`).

## Niveles de evidencia (acceptance.md)

L0 propuesta · L1 código+unit tests · L2 engine preparado+adapter+pruebas CPU ·
L3 ejecución GPU real · L4 reproducido con holdouts. Pre-GPU exige **L2** por
capacidad obligatoria; "Ultra validada" exige L3/L4 posterior.

## Estado por paquete

| W | Título | Estado | Evidencia | Nivel |
|---|--------|--------|-----------|-------|
| W00 | Baseline y reconciliación de PRs | done | baseline `b54195f`; #58 merged; #64 merged; #59 diferida (conflictos); #63 atlas = base W09 | L1 |
| W01 | Integridad transaccional de modelos | in_review | PR `fix/ultra-w01-asset-integrity`: 3 P0 corregidos + regresiones | L1 |

## W01 — detalle (3 defectos P0, research.md)

1. **Caché corrupta aceptada.** El skip inicial aceptaba cualquier fichero no
   vacío. Ahora verifica tamaño (cuando no hay hash) e integridad antes de
   declarar `present`; un fichero que no verifica se descarta y se re-descarga.
2. **Pin ignorado por el CLI.** `hf download` no recibía `--revision`. Ahora el
   argv incluye la revisión fijada del manifiesto en todas las rutas.
3. **Publicación parcial EXDEV.** El fallback copiaba directo sobre el destino
   final y destruía la versión previa. Ahora copia a un temporal en el mismo
   filesystem del destino, `fsync`, `os.replace` atómico y rollback del
   temporal si algo falla: el destino previo nunca se corrompe.

Regresiones añadidas en `studio/tests/test_capability_resolver.py` (fallan en el
código previo, pasan tras el fix). Verificación CPU local: 36 passed, 1 skipped
(un test POSIX-only que corre en CI Linux). Portabilidad: `_terminate_tree` y los
fixtures de test ahora funcionan en Windows/macOS además de Linux.

## Próximo lote

- **W02** — Hardware/sizing/recetas: P0 #4 (simulated→measured), #5 (recetas
  incompatibles agregadas), #6 (disco sobre catálogo), #7 (perfiles sin acotar
  por NVML/cgroup).

## Bloqueos

Ninguno a esta fecha.
