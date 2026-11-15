# Memoria de producción

`production-memory` ofrece memoria audiovisual duradera dentro del proyecto activo. Usa el
metadata SQLite que ya mantiene `ProjectStore`; no añade paquetes, servicios remotos, modelos ni
descargas.

El agente puede guardar escenas, recetas, preferencias y decisiones; buscar por coincidencia
léxica determinista; listar, corregir con motivo y versión, y borrar con `confirm=true` solo cuando
el usuario pidió explícitamente la eliminación. Los registros quedan acotados por proyecto,
128 elementos, ocho versiones por elemento y 1 MB total. El backend comparte su bloqueo por ruta
de base de datos para evitar escrituras perdidas entre solicitudes concurrentes.

Las herramientas del agente limpian secretos antes de persistir texto o devolverlo al modelo. El
contenido recuperado se marca como dato no confiable y no constituye política ni evidencia factual.
La recuperación determinista no acredita calidad semántica ni compatibilidad creativa; esas
mediciones y la memoria completa del director siguen fuera de esta evidencia CPU.

El código propio está bajo MIT, según el [`LICENSE`](../../LICENSE). La revisión de
redistribución es `allowed`; no hay avisos externos nuevos. El lock
[`production-memory.lock.json`](../../upgrades-ultra-version/locks/production-memory.lock.json)
fija el commit de implementación y los hashes de sus dependencias internas. El hash de ese lock
queda registrado en `upgrades-ultra-version/engines.json`.

Entrypoints: `studio/comfy_studio/production_memory.py` y el registro de herramientas en
`studio/comfy_studio/agent/tools.py`. Los contratos CPU están en `studio/tests/test_production_memory.py`
y `studio/tests/agent/test_tools.py`. La validación de GPU permanece `not_run`.
