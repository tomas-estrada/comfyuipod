# W26 — Plan de validación GPU (alquiler, presupuesto, teardown)

Estado: **NO EJECUTADO**. `pending_separate_gpu_authorization`. Este documento es
el plan; no alquila nada ni ejecuta APIs facturadas. Solo se corre cuando el
operador autorice presupuesto, precio/hora, duración, disco y credenciales (A29).

Anclado a: `upgrades-ultra-version/backlog.json` (W26), `acceptance.md` §A29 y
las 5 demostraciones (D01–D05), y al helper `tools/mac/comfy`.

---

## 0. Qué debe autorizar el operador antes de arrancar (A29)

El plan admite instancia propia ya autorizada; no exige Vast.ai. El operador fija:

| Dato | Para qué | Valor (lo fija el operador) |
|------|----------|------------------------------|
| Presupuesto total (USD) | Tope duro de gasto de toda la campaña | — |
| Precio máximo por hora | Rechazar ofertas caras | — |
| Duración máxima (horas) | Watchdog de teardown | — |
| Disco (GB) | Derivado, no a ojo (ver §2) | derivado |
| GPU | RTX PRO 6000 Blackwell 96 GB | fija |
| Perfil RAM host | 128 y 256 son resultados distintos (A29.9) | 128 y/o 256 |
| Credenciales / instancia | Vast.ai u otra ya autorizada | — |

Regla de oro: si solo hay una oferta de RAM disponible, no afirmar que se
validaron las dos (A29.9).

---

## 1. Preflight local (sin GPU, sin coste) — A29.1

Objetivo: comprobar que se puede alquilar antes de pagar un segundo de GPU.

1. CLI y config presentes: `vastai --version`, `vastai show user`.
2. Saldo y elegibilidad **sin imprimir claves** (`VAST_API_KEY` nunca se imprime
   ni persiste — regla de seguridad del repo).
3. SSH/API responden.
4. Imagen candidata vigente fijada **por digest**, no por tag móvil:
   `ghcr.io/tomas-estrada/comfyuipod@sha256:0eddf85db32ed141da0e77ae636c226a73f5b1e232f646e55c92385affb2b53f`
   (tag de referencia `sha-f5c29dc`).
5. Si la CLI falla: diagnóstico y guía, no reintento ciego.

Salida esperada: checklist verde o motivo terminal honesto. Nada se alquila aún.

---

## 2. Disco derivado (no adivinado) — política de arquitectura

El mínimo de disco sale del cálculo, no de un número a mano:

```
python scripts/storage_requirement.py --profile studio --json
```

Hoy ese cálculo da `start_gate_gb = 63` (puerta de arranque) y
`total_gb ≈ 248` (pico con modelos de tarea). El operador dimensiona el disco del
pod con esos números, no con una estimación.

---

## 3. Alquiler del pod — A29.2

Con el helper del repo (`tools/mac/comfy`), que ya resuelve oferta, arranque,
espera de `/healthz` y portal:

```
comfy rent studio            # perfil studio; --dry-run para ver la oferta sin alquilar
comfy rent studio --dry-run  # inspección previa, cero coste
```

Tras arrancar, **antes de pesos**, verificar hardware real (A29.2):

- driver y arquitectura CUDA (Blackwell),
- VRAM = 96 GB reales,
- RAM del host = perfil elegido (128 o 256),
- límites de cgroup y disco reales.

Si algo no cuadra con lo pedido, destruir y parar (no seguir pagando).

---

## 4. Readiness real de pesos — A29.2/A29.3

No inferir readiness de `/healthz`. Comprobar pesos reales por
ComfyUI `/object_info` **solo de las recetas que se van a correr** (no cargar
todo el catálogo — A29.3). Reservar tiempo/espacio de adquisición por receta.

Skill del repo: `verify-model-availability-before-comfyui-run` (evita el falso
`Value not in list`).

---

## 5. Medición real por receta — sustituye estimaciones

Por cada receta usada en D01–D05, medir y **registrar**:

- pico real de VRAM y de RAM,
- tiempo de carga/descarga (load/unload) y compatibilidad de kernels,
- tiempo total y coste,
- calidad vs baselines.

Estas mediciones sustituyen las estimaciones marcadas `NO-medida` en
`av_recipes` (`recipe_for_template`). Antes de esto, VRAM estimada sigue siendo
estimada.

---

## 6. Las 5 demostraciones — A29.8 + tabla D01–D05

Cada demo conserva: inputs, versiones, recipe, seed, artifacts intermedios y
mediciones. Un montage determinista CPU demuestra la fontanería, **no** sustituye
el desafío neural.

| Demo | Reto neural GPU (resumen) |
|------|---------------------------|
| D01 | Coche en lugar real del Aburrá: reconstrucción→guías→generación→relight→Foley→4K vs baselines |
| D02 | Borrar una causa y sus consecuencias (VOID/omnimatte) vs borrado de máscara; error revelado/residual |
| D03 | Producto articulado, cambio de material, multicámara; heldout multivista y consistencia |
| D04 | Actor que habla español con identidad persistente; retarget/Audio2Face/InfiniteTalk; turnos y sonido |
| D05 | Director autónomo con prompt ambiguo y 100 assets heterogéneos; brief→shots→reparación→máster 4K con budget |

---

## 7. Fallos, parches y reintentos — A29.4/A29.5

- Al **primer fallo estructural**: guardar logs/telemetría/artifacts mínimos,
  **terminar la instancia** y confirmar cese de cobro **antes** de un arreglo
  largo. No mantener GPU inactiva mientras se desarrolla.
- Parche → PR → checks → rebuild con **digest nuevo** → nuevo intento, solo
  dentro del presupuesto compartido. "Try again" no es bucle infinito
  (tope: `max_failed_acceptance_attempts = 3`).

---

## 8. Teardown garantizado — A29.6/A29.10

- Un `finally`/watchdog local cierra **solo** los recursos que creó la campaña.
  No borra pods/volúmenes preexistentes ni mantiene disco cobrado sin política.
- Al terminar: destruir el pod y **verificar 0 instancias**:

```
comfy destroy <id>
comfy status          # o: vastai show instances --raw  -> debe listar 0 de la campaña
```

- Evidencia privada archivada donde corresponda; reporte público **redactado**
  (sin secretos). Solo **después** puede marcarse L3/L4/Ultra validada (A29.10).

---

## 9. Presupuesto de trabajo (del backlog W26)

| Límite | Valor |
|--------|-------|
| Minutos activos recomendados por hoja | 60 |
| Minutos activos máximos por hoja | 90 |
| Intentos fallidos de aceptación máx. | 3 |
| Contadores persisten al reanudar | sí |

No es un tope global de misión; un paquete grande puede necesitar varias hojas/PRs.

---

## 10. Evidencia de cierre exigida (W26)

- Identificadores reales de PR/commit.
- Tests/logs semánticos con datos privados redactados.
- Punteros a artifacts inspeccionados.
- Verificación del conductor y estado de integración mergeado/reconciliado.
- Perfiles 128 vs 256 reportados por separado (A29.9).

---

## Resumen de ejecución (una pasada)

```
# 1. preflight (cero coste)
vastai show user
python scripts/storage_requirement.py --profile studio --json

# 2. alquilar (inspección primero)
comfy rent studio --dry-run
comfy rent studio

# 3. en el pod: verificar hardware real, luego /object_info de las recetas a correr
# 4. correr D01-D05, medir peaks/tiempo/coste/calidad, guardar evidencia
# 5. teardown SIEMPRE
comfy destroy <id>
comfy status   # verificar 0 instancias de la campaña
```

Gate final: solo tras §8 completo y evidencia archivada se pasa de
`BLOCKED_PRE_GPU` a `L3/L4` / `Ultra validada`.
