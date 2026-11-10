# Diagramas de arquitectura — cómo se hacen y cómo se validan

**Rama:** `fix/maad-diagram-legibility` · **Base:** `main` `f07a4ac`
**Fecha:** 2026-11-10

Este directorio contiene la documentación de arquitectura generada en la pasada
MAAD. Este README existe por un motivo concreto: **la primera versión de los dos
diagramas era ilegible**, y la causa fue un error de proceso que cualquier agente
puede repetir.

---

## 1. La regla

> **Ningún `.puml` se commitea sin haber renderizado y visto el resultado.**

"No parsea" y "se puede leer" son cosas distintas. Un diagrama puede tener
sintaxis perfectamente válida y ser inservible.

---

## 2. Lo que pasó (y por qué importa)

Los dos diagramas se generaron con un validador estructural en verde:
referencias sin colgar, sin duplicados, estado inicial presente, coherencia entre
vistas. **Ese validador no renderiza nada.** Cuando finalmente se renderizaron:

| Síntoma | Causa real |
|---|---|
|Texto solapado dentro de los paquetes | Etiquetas de arista con `[ C ] archivo:línea` en cada flecha: ocupaban más que los propios nodos |
|Líneas cruzando por todos lados | `skinparam linetype ortho` |
|Diagrama de 4096×2270, imposible de recorrer | `skinparam wrapWidth 200` |
|Un pragma que no hacía nada | `!pragma teoz true` es de diagramas de **secuencia**, no de componentes ni de estados |
|Render vacío (5 KB, solo el título) | Faltaba Graphviz: PlantUML lo usa para el layout de estos dos tipos de diagrama, y **sale con código 0 aunque no haya dibujado nada** |

El último punto es el más peligroso: **PlantUML no falla cuando no puede
renderizar**. Devuelve un SVG con el título y nada más. Cualquier comprobación
que solo mire "¿salió con 0?" dará verde.

---

## 3. Cómo renderizar (local)

```bash
# Dependencias (una vez)
brew install openjdk graphviz          # o el equivalente en tu sistema

# Render
java -DGRAPHVIZ_DOT="$(command -v dot)" -jar plantuml.jar \
  -tpng -charset UTF-8 -o /tmp/uml \
  docs/architecture/01-components.puml docs/architecture/02-states.puml
```

Notas:
- `-DGRAPHVIZ_DOT` es necesario si `dot` no está en la ruta que PlantUML prueba
  por defecto (prueba `/opt/local/bin/dot`, la de MacPorts).
- Sin JRE no hay render. Sin Graphviz, el render sale **vacío y con código 0**.

---

## 4. Cómo se valida

Dos niveles, ambos obligatorios:

### 4.1 Estructural — `scripts/check_architecture_puml.py`
Detecta: `@startuml`/`@enduml` desbalanceados, referencias a elementos no
declarados, identificadores duplicados, ausencia de estado inicial, estados
inalcanzables y correspondencia entre vistas (declarada en
`PARTICIPANT_TO_COMPONENT`, no inferida por coincidencia de nombres).

**No renderiza.** Por sí solo no garantiza legibilidad.

### 4.2 De render — CI
El job `Validate` de `.github/workflows/docker-publish.yml` renderiza los dos
`.puml` a SVG y **falla si la salida no tiene tamaño real**:

- `< 20000` bytes → salida vacía o truncada
- `< 20` formas dibujadas → diagrama vacío

Esto es lo que atrapa el fallo silencioso de Graphviz.

---

## 5. Reglas de legibilidad

Aprendidas del error original. Todas aplican a los diagramas de este directorio.

1. **La evidencia no va en las etiquetas del grafo.** `archivo:línea` y el nivel
   de confianza van en un bloque `legend`, que PlantUML renderiza aparte y por
   tanto no se solapa con nodos ni flechas. La etiqueta de la arista lleva solo
   el nombre funcional.
2. **Nada de `!pragma teoz true`** salvo en diagramas de secuencia.
3. **Nada de `skinparam linetype ortho`** en diagramas de componentes o estados:
   provoca cruces. Usar el routing por defecto.
4. **`wrapWidth` pequeño** (≤ 60 para estados). Con 200 las etiquetas se comen
   media pantalla.
5. **Un `legend` por diagrama**, no notas largas flotando entre los nodos.
6. **Si el diagrama se pasa de ~2500 px en cualquier dimensión, dividirlo.** Un
   diagrama que hay que recorrer con scroll deja de leerse.
7. **Verlo con los ojos.** `read_file` sobre un PNG devuelve la imagen: es la
   única forma de detectar solape. Un validador en verde no sustituye a esto.

---

## 6. Ficheros

| Fichero | Qué es |
|---|---|
| `00-inventory.md` | Módulos, dependencias y documentación previa |
| `01-components.puml` | Vista estática (29 componentes) |
| `02-states.puml` | Vista de comportamiento (5 máquinas de estado) |
| `03-rules.md` | Reglas arquitectónicas con confianza y huecos |
| `04-atam-report.md` | Análisis de compromisos |
| `05-mismatch-report.md` | Contraste documentación ↔ código |

Los requisitos viven en [`../srs.md`](../srs.md).
