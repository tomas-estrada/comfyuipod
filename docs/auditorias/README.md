# Auditorías — convención e histórico

Este directorio conserva el registro de auditorías del proyecto. Antes de añadir un archivo
aquí, lee esta página: la convención evita el ciclo de auditorías vacías que ya ocurrió una
vez en este repositorio (149 notas creadas a razón de una por minuto, la mayoría sin hallazgos).

## Cadencia (obligatoria)

- **Máximo una auditoría cada 24 horas o una por Pull Request.**
- **Prohibido** cualquier ciclo automático minuto a minuto. Una tarea programada que genere un
  archivo por ciclo debe desactivarse antes de continuar.
- Una auditoría **vacía no se publica**. Si no hay hallazgos, el resultado correcto es no
  escribir ningún archivo.
- Las auditorías son **de solo lectura**: no modifican código fuente. Los arreglos van en un
  commit separado, con su propio mensaje.

## Formato

1. Fecha y alcance explícitos (qué se revisó, en qué commit).
2. Un **ID único por hallazgo**, siguiendo `AGENTS.md`:
   - `Mx-SCR-nnn` — problemas de producción (seguridad, robustez, corrección).
   - `Mx-AGT-nnn` — gobernanza del agente.
   - `Mx-OPS-nnn` — operaciones, volúmenes, CI.
   - `Mx` es el hito (`M1`, `M2`, `M3`, …). Los IDs no se reutilizan ni se renumeran.
3. Para cada hallazgo: **evidencia** (`archivo:línea`) y **estado** (`abierto`, `corregido`,
   `aceptado`).
4. Matriz de validación ejecutada (comando → resultado) al final.

### Convenciones históricas

Los archivos `AUDITORIA_AGENTE_V1..V9.md` y los informes de implementación usan la numeración
histórica `Vn-NN` y `VAL-*`, anterior a la convención `Mx-*`. **Se conservan tal cual**: son el
registro de decisiones de las rondas anteriores y reescribirlos destruiría la trazabilidad.
Para trabajo nuevo se usa siempre `Mx-*`.

## Índice

| Archivo | Contenido |
|---|---|
| `AUDITORIA_AGENTE_V1..V5.md` | Rondas de auditoría de la imagen base y del arranque estricto. |
| `AUDITORIA_AGENTE_V6.md` | Observabilidad, portal y supervisor. |
| `AUDITORIA_AGENTE_V7.md` | `comfyctl` en Rust, CLI y redacción de secretos. |
| `AUDITORIA_AGENTE_V8.md` | CI y hallazgos latentes de `98867a6`. |
| `AUDITORIA_AGENTE_V9.md` | Cierre post-M2: contratos de plantillas, UTF-8/CRLF, ruff, docs. |
| `IMPLEMENTACION_OBSERVABILIDAD_Y_PORTAL.md` | Informe de implementación de la propuesta de observabilidad. |
| `IMPLEMENTACION_V6_Y_RUST.md` | Informe de implementación de la migración a Rust. |
| `AUDITORIA_M3_SCR_20261028.md` | Auditoría M3 (hallazgos `M3-SCR-001..021`, `M3-AGT-001`). |
| `AUDITORIA_INTEGRAL_FINAL_M3.md` | Verificación integral de cierre de M1–M3. |

## Referencia

- Convención de IDs y mapa de propiedad: [`../../AGENTS.md`](../../AGENTS.md).
- Backlog derivado de las auditorías M3: [`../propuestas/IDEAS_M3_BACKLOG.md`](../propuestas/IDEAS_M3_BACKLOG.md).
