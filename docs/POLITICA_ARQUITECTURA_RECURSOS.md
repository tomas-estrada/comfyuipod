# Política de Arquitectura y Recursos

Este documento es la **fuente de verdad** de cómo ComfyUIPod decide qué se
prepara, qué se descarga, qué se ejecuta y qué hardware se alquila. Sustituye a
las estimaciones fijas de almacenamiento que aparecían en el README y en el
código, y explica qué está implementado hoy y qué sigue pendiente.

> **Regla de oro:** el pod llega preparado. La instalación y la integración de
> motores pertenecen al build; la selección y carga de modelos pertenecen al
> runtime.

---

## 1. Almacenamiento flexible y derivado

180 GB era una estimación, no un requisito ni un límite. El almacenamiento se
**calcula** con [`scripts/storage_requirement.py`](../scripts/storage_requirement.py)
a partir de componentes reales:

| Componente | Origen |
|---|---|
| Imagen publicada + sobrecarga de filesystem | tamaño medido (`--image-bytes`); se carga al volumen solo si la imagen vive en él (`--image-on-volume`) |
| Arranque (Fase 1 + Fase 2 del perfil) | `PROFILE_BOOTSTRAP_BYTES` del script |
| Modelos de tarea (Fase 3 base + opt-ins pedidos) | `config/models-studio.json` (`meta.default_bytes` + grupos `opt_in`) |
| Pico de staging | archivo más grande × 2 (`.incomplete` + copia final coexisten) |
| Reserva de descarga | `HF_DISK_MARGIN_BYTES` (10 GiB) |
| Reserva de trabajo | media fuente, proxies, salidas y cachés (20 GiB) |

El script distingue dos preguntas que antes se confundían en un único número:

- **Puerta de arranque** (`start_gate`): lo que debe estar **libre** en el
  volumen de datos antes de empezar a descargar. Es lo que corresponde a
  `comfyctl strict-check` S5.
- **Volumen** (`total`): lo que hay que **alquilar** para guardarlo todo. Es lo
  que corresponde a `comfy rent`.

```bash
scripts/storage_requirement.py --profile studio            # desglose completo
scripts/storage_requirement.py --profile studio --check    # falla si un umbral quedó corto
```

`comfy rent studio` deriva su mínimo de disco de este script (busca
`scripts/storage_requirement.py` junto al helper o en `COMFY_REPO_ROOT`) y
`COMFY_DISK_GB` permite subirlo cuando la carga de trabajo lo justifica.

### Hallazgos actuales (verificados el 2026-11-04)

| Perfil | Puerta derivada | Umbral S5 declarado | Volumen derivado | Mínimo `comfy rent` |
|---|---:|---:|---:|---:|
| `minimal` | 14.8 GB | 10 GB ⚠ | 37 GB | 120 GB |
| `general` / `realistic` | 75.0 GB | 45 GB ⚠ | 97 GB | 120 GB |
| `anime` | 79.8 GB | 45 GB ⚠ | 102 GB | 120 GB |
| `all` | 113.2 GB | 75 GB ⚠ | 135 GB | 120 GB ⚠ |
| `studio` | 62.6 GB | 180 GB ✓ | 248 GB | 248 GB (derivado) |
| `unreal` | 62.6 GB | 210 GB ✓ | 256 GB | 256 GB (derivado) |

⚠ = el umbral queda por debajo de lo derivado.

**Estado:** el umbral ya **no está codificado a mano**. El `Dockerfile` ejecuta
`storage_requirement.py --all-profiles` en el build y hornea el resultado en
`/etc/comfyui/storage-requirement.json`; `comfyctl` lo lee al arrancar
([`comfyctl/src/storage.rs`](../comfyctl/src/storage.rs)) y S5 comprueba el
número derivado. Si el fichero no existe (imagen antigua, desarrollo), cae al
respaldo documentado en `default_min_disk_gb`, que sigue siendo la tabla de
siempre. Test de integración: `s5_uses_the_derived_requirement_when_the_image_publishes_it`.

---

## 2. Motores preparados antes del despliegue

Un motor es una aplicación, runtime o servicio que aporta una capacidad. Para
los motores soportados, la imagen hornea:

- fuente o binarios fijados (SHA de `config/nodes.lock`, digests de imagen,
  versiones exactas de paquete);
- dependencias de sistema y de lenguaje compatibles, con `PIP_CONSTRAINT`
  protegiendo la terna `torch==2.9.1+cu128`;
- entornos aislados cuando las dependencias chocan (`/opt/venvs/studio`);
- entrypoints ejecutables en `/usr/local/bin`;
- integración con la API y las herramientas del agente;
- contratos de entrada/salida tipados (`EngineResult`, `*Spec`);
- configuración por variables de entorno con defaults en el `Dockerfile`.

**La instalación ad-hoc en runtime no es el modelo operativo.** El agente no
instala motores: `install_engine` solo *decide* (`engines/install_gate.py`) y
`STUDIO_DYNAMIC` está apagado por defecto. Si una capacidad nueva requiere un
cambio de motor, se cambia el repositorio y el pipeline, se valida y se produce
un artefacto nuevo.

### Excepciones conocidas y honestas

Estas dependencias siguen fuera de la imagen y están **declaradas** en la
taxonomía de preparación (§3), no escondidas:

- `indextts`: el venv aislado se crea en el volumen en el primer uso
  (`studio/engine_bin/indextts25`), con el paquete fijado por SHA.
- `voice_clone`: `fairseq` y los pesos de RVC no se hornean.
- `music_gen`: dependencias pesadas y pesos en runtime.
- `lipsync`: el checkpoint `wav2lip_gan.pth` no se hornea.
- `capture3d`: `tiny-cuda-nn` se compila en el volumen.
- `qa` nivel 2 (VBench): requiere un intérprete que el operador construye.
- `blender`: **no hay binario en la imagen**; el modo 3D con Blender requiere
  instalarlo. Antes el roster lo daba por listo.

---

## 3. Instalado no es «en ejecución»

[`studio/comfy_studio/engines/preparedness.py`](../studio/comfy_studio/engines/preparedness.py)
implementa la taxonomía que exige la política. Cada motor declara qué necesita y
una sonda **barata y determinista** lo comprueba:

| Estado | Significado |
|---|---|
| `installed` | binario/entrypoint y módulos presentes |
| `integration` | variables de configuración resueltas |
| `weights_present` | los pesos de la tarea están en disco (`None` = no aplica) |
| `hardware` | `unchecked` desde salud: lo posee `strict-check` S1–S10 |
| `model_loaded` | **falso** desde salud; nunca se carga un modelo para informar |
| `execution_validated` | solo tras una ejecución real registrada (`note_engine_run`) |

La sonda hace `stat`, `shutil.which` e `importlib.util.find_spec`. **Nunca
descarga pesos ni carga modelos.**

Estados agregados: `ready`, `weights_required` (instalado, pidiendo pesos de
tarea —el caso normal de un motor bien preparado—), `not_installed`,
`integration_missing`.

Se expone en:

- `GET /studio/api/health` → `preparedness` (agregado) y `prep` por motor;
- `GET /studio/api/status` → `model_assets`;
- `assert_production_prepared(registry, required)` para fail-closed en tests.

Ejemplo real de por qué hacia falta: con el roster antiguo, los 16 motores
informaban `ready`; con la taxonomía, en un pod sin pesos se ve qué motores
están instalados, cuáles piden pesos y cuáles no tienen binario.

---

## 4. Los activos de modelo son dependencias de tarea

Dos manifiestos versionados declarán los activos, con la misma forma:

| Manifiesto | Contenido |
|---|---|
| [`config/models-core.json`](../config/models-core.json) | Fases 1 y 2 (26 entradas, 68.02 GB), generado desde `download-models.sh` y verificado por `HEAD` |
| [`config/models-studio.json`](../config/models-studio.json) | Fase 3 del perfil studio (79 + 8 entradas) |

Cada entrada lleva `class` explícita:
| Clase | Significado |
|---|---|
| `bootstrap` | genuinamente necesario para operar |
| `task` | se adquiere para la tarea elegida |
| `optional` | variantes y mejoras (grupos `opt_in`) |

**Una colección grande de modelos no es bootstrap porque un motor la soporte.**
El resultado de clasificar de verdad: de 68.02 GB en el núcleo, solo
**2.86 GB (10 archivos)** son bootstrap —el stack facial/VAE que la Fase 1 trae
antes de abrir ComfyUI—. Los 65.16 GB restantes son selección de modelo. En la
Fase 3, los 163.71 GB son `task` y los tres grupos opt-in `optional`.

### Resolvedor de capacidades

[`studio/comfy_studio/capabilities.py`](../studio/comfy_studio/capabilities.py)
implementa el flujo que define la política. Cada plantilla ya declaraba sus
`models`; el resolvedor une esa declaración con los manifiestos:

```
request_capability("video.face_restore")
  → motor `comfyui` y su estado de preparación
  → receta: plantilla `face_fix`
  → activos: 4 ficheros, con clase y origen (core/studio)
  → faltan 4 (0.92 GB) → se adquieren con el cliente `hf`
  → estado: ready, con `template_id` listo para `enqueue_render`
```

Herramientas del agente:

- `list_capabilities`: catálogo con el estado de preparación de cada capacidad.
- `request_capability`: resuelve y, con `acquire=True`, descarga lo que falta.
- `download_model`: ahora **descarga de verdad** los grupos opt-in pedidos
  (antes solo informaba). Nunca descarga un grupo que no esté pedido
  (`STUDIO_INCLUDE_*=1`): sin descargas especulativas.

[`asset_acquire.py`](../studio/comfy_studio/asset_acquire.py) ejecuta la
adquisición con las mismas reglas que `download-models.sh`: reutiliza lo que ya
está, comprueba espacio libre por intento y falla cerrado, staging aislado con
`rename()` atómico, 3 intentos con backoff, timeout por intento, y admite
progreso y cancelación.

Con ello, «disponible» pasó de variable de entorno a hecho del disco:

- `*_weights_expected()` = se pidió (intención);
- `*_weights_available()` = está en disco (hecho).

Esto corrigió un defecto real: el camino del agente (`enqueue_render`) no pasaba
los flags de disponibilidad, así que podía encolar renders de Animate/3D/MiniMax
sin pesos.

### Integridad y revisión fijada

La política pide verificar «source, revision, license, size and integrity where
available». HuggingFace publica ambos en la respuesta `HEAD` de `resolve`:

- `X-Repo-Commit` → el **commit exacto** del fichero;
- `X-Linked-Etag` → el **SHA-256** del objeto LFS (o el SHA-1 del blob git para
  ficheros pequeños).

[`scripts/pin_manifest_integrity.py`](../scripts/pin_manifest_integrity.py) fija
ambos en las **113 entradas** de los dos manifiestos (58+26 con SHA-256, 29 con
SHA-1) y reescribe cada URL para apuntar a la revisión en lugar de a `main`.
Es idempotente y sigue los renombres de repo aguas arriba (`flux1-dev` →
`Flux-1-Dev`).

Con la revisión fijada, `asset_acquire` **verifica el hash tras descargar** y
rechaza el activo si no cuadra, en lugar de instalarlo. Un activo sin hash
declarado se acepta («where available»): el tamaño exacto ya está verificado.

El validador exige `revision` (SHA-1 de git de 40 hex) y al menos un hash, con
el algoritmo coherente con la longitud.

### Retención de caché decidida a propósito

«Choose cache retention deliberately. Retain reusable weights when doing so is
economical; remove replaceable cached weights under a clear policy. Preserve
user media, project state and outputs.»

[`studio/comfy_studio/cache_policy.py`](../studio/comfy_studio/cache_policy.py)
implementa esa política, y [`scripts/prune_model_cache.py`](../scripts/prune_model_cache.py)
la aplica (**dry-run por defecto**):

1. Solo se consideran rutas **declaradas en los manifiestos**. Media del
   usuario, proyectos, salidas, bases de datos y venvs quedan fuera por
   construcción, no por una lista de exclusiones que haya que mantener.
2. `bootstrap` y `optional` **se conservan siempre**: lo primero es necesario
   para operar, lo segundo lo pidió el operador.
3. `task` se conserva mientras quepa. Solo por encima del presupuesto se
   expulsan, por **último uso real** (LRU).
4. El staging es parcial reanudable: se limpia solo si lleva inactivo más de 7
   días, porque borrarlo antes tira el trabajo de una descarga interrumpida.

El «uso real» viene de un ledger (`${COMFY_DATA_DIR}/.model-usage.json`) que se
escribe cuando un activo se descarga y cuando se renderiza la plantilla que lo
declara — no de `atime`, que con `relatime` no es fiable. El registro falla en
silencio a propósito: una optimización de retención nunca debe tumbar un render.

```bash
scripts/prune_model_cache.py                  # plan, no borra nada
scripts/prune_model_cache.py --budget-gb 120  # con presupuesto
scripts/prune_model_cache.py --apply          # ejecuta
```

### Recetas de ejecución medidas

«Maintain execution recipes containing supported settings, expected resource
requirements and measured results. Reuse them rather than reasoning from scratch
on each deployment.» Antes no se medía nada: las decisiones se tomaban desde las
estimativas estáticas de `performance_defaults`.

- **Medición real**: durante el render se muestrea `/system_stats` de ComfyUI
  cada 2 s (`comfyui_client.sample_system_stats_peak`) y se guarda el **pico** de
  VRAM/RAM, la duración, la GPU y la versión de CUDA. La muestra corre
  concurrente al render: medir después daría siempre el valor en reposo.
- **Almacén**: tabla `render_measurements` (esquema v3 de `studio.db`), con
  `ProjectStore.record_render_measurement()` / `recipe_for_template()`.
- **Reutilización**: `GET /studio/api/status` expone las recetas medidas, la
  herramienta `render_recipes` las devuelve al agente, y el resolvedor de
  capacidades adjunta la receta de la plantilla seleccionada. Es opcional: sin
  mediciones, la resolución funciona igual.
- **Nunca rompe el render**: guardar una medición falla en silencio, y el
  muestreador también si ComfyUI no responde.

### `offload`: eliminado como parámetro de render

`RenderTemplateParams` aceptaba `offload`, pero ese valor **se descartaba antes
de llegar al grafo** de ComfyUI: no había ningún knob por render que lo aplicara.
Anunciar un parámetro sin efecto es peor que no tenerlo, así que se retiró del
cinturón. `offload: true` sigue en los `performance_defaults` de las plantillas
como documentación de lo que el pod necesita; el offloading real lo aplica la
gestión de VRAM de ComfyUI al arrancar (`COMFYUI_EXTRA_ARGS`).

## 6. Coste completo de producción

`scripts/storage_requirement.py --cost` modela el coste de una sesión con los
componentes que la política pide, y hace explícito el que suele ignorarse:
**la GPU se factura mientras se descarga**.

```bash
scripts/storage_requirement.py --profile studio --cost --hours 3
```

```
Coste de sesión (3.0h, GPU $0.55/h, disco 248 GB, 500 Mbps)
  imagen                       15.60 GB
  modelos                     180.90 GB
  transferencia total         196.50 GB ($1.97)
  arranque en frío              0.07 h
  descarga de modelos           0.87 h
  GPU facturada sin producir   0.87 h ($0.48)
  horas productivas            2.13 h
  coste GPU                 $    1.65
  coste disco               $    0.15
  coste transferencia       $    1.97
  TOTAL                     $    3.77
  por hora productiva       $    1.77
```

Dos consecuencias que el modelo hace visibles:

- En una sesión corta, la transferencia manda: bajar 180 GB a 500 Mbps son ~50
  min de GPU cobrando sin producir. **Un render más largo puede ser el mejor
  negocio** si evita una segunda sesión.
- La métrica honesta es el **coste por hora productiva**, no el `$/h` de la GPU.
  Por eso `comfy rent` ordena por precio pero el presupuesto se justifica con
  mediciones y ofertas reales, no con latencia.

La imagen se cuenta como transferencia aunque no ocupe el volumen de datos: se
descarga en cada arranque en frío. Las capases reutilizables (`buildcache`,
`mode=max`) ya están en CI; lo que falta es un modelo de egress, no de disco.

S14 (`engine_roster`) informa además de la taxonomía de preparación: «preparados
N, pendientes de pesos N, sin instalar N, sin integración N». Sigue siendo `warn`
nunca `fatal`, porque un motor instalado que pide pesos de tarea es el caso
normal, no un fallo.

### Liberación de VRAM cuando no queda trabajo

`POST /free` existía en el cliente pero **nunca se llamaba en producción**: los
modelos se quedaban residentes en VRAM tras cada render. Ahora, al terminar un
render y persistir las salidas, `_release_gpu_when_idle()` descarga los modelos
**solo si no queda ningún job en vuelo** — liberar con la cola llena penalizaría
el rendimiento sin ganar nada. Controlable con `STUDIO_UNLOAD_WHEN_IDLE`
(por defecto `1`) y silencioso si ComfyUI no responde: liberar VRAM es una
optimización, nunca debe tumbar un render ya terminado.

### La selección por perfil es dato, no un `case` en bash

Qué descarga cada `MODEL_PROFILE` vivía en guardas `=~ ^(all|general)$` dentro
de `scripts/download-models.sh`. Ahora el manifiesto core declara `profiles` por
entrada (26 activos) y [`scripts/check_profile_parity.py`](../scripts/check_profile_parity.py)
extrae estáticamente esa selección del bash y la compara con el manifiesto — el
mismo patrón que la paridad O-11. Si los dos se separan, CI falla con el detalle.

No se ha refactorizado el bash a un bucle sobre el manifiesto: funciona, está
 cubierto por un fixture de log, y el riesgo no compensaba el beneficio. Lo que
importa es que la decisión sea ahora versionada y verificada.

### Pendiente

- **Egress**: se modela la transferencia de bajada con un precio por GB
  orientativo; falta el precio real del proveedor.
- **Egress**: se modela la transferencia de bajada con un precio por GB
  orientativo; falta el precio real del proveedor.
## 5. Hardware objetivo

El objetivo preferido es **una GPU de consumo con ~32 GB de VRAM** y RAM de host
suficiente para la carga.

- `comfy rent` filtra `num_gpus=1` (una oferta multi-GPU podía pasar el filtro
  de `gpu_ram` y alquilarse como una sola tarjeta), `cpu_ram>=32` por perfil y
  `cuda_vers>=12.8`.
- La validación en cliente **rechaza** ofertas sin `cpu_ram` o sin `cuda_vers`
  declarados, en lugar de dejarlas pasar por defecto.
- `strict-check` verifica el host real: driver ≥ 570, VRAM ≥ 20 GB, RAM con
  margen de kernel y límite de cgroup, disco y sonda `torch.cuda` real.

Antes de recomendar hardware más caro hay que agotar precision/quantización,
offloading, ejecución en CPU de componentes compatibles, VAE tiling, planificación
secuencial y parámetros de carga. **La RAM no es intercambiable por VRAM** y los
métodos son por modelo.

### Pendiente

- **Recetas de ejecución medidas**: `performance_defaults` de las plantillas
  declaran ajustes y presupuesto de VRAM, pero **ningún resultado medido se
  persiste ni se reutiliza**. Falta una tabla de mediciones
  (VRAM/RAM/tiempo por render) que alimente las decisiones siguientes.
- El parámetro `offload` que anuncia la API **no tiene efecto**: se valida y se
  descarta antes de llegar a ComfyUI. Está documentado como no operativo.
- `strict_min_vram_mb`/`strict_min_ram_mb` no derivan del perfil.

---

## 6. Coste completo de producción

Se optimiza el coste de producir un entregable aceptable —arranque, transferencias,
descargas, inferencia, evaluación, almacenamiento y API—, no la latencia. Un
render más largo es aceptable si el balance total es mejor.

Implementado: caché de capas `buildcache` con `mode=max`, bases fijadas por
digest, `provenance`/`sbom` desactivados para no inflar blobs, poda de disco del
runner, y `tools/mac/comfy` sumando el coste de disco al `$/h` de la GPU.

Pendiente: modelo de coste de transferencia/egress y de arranque en frío.

---

## 7. Experiencia objetivo

> «Necesito esta capacidad para mi tarea.»
> → el motor ya está preparado;
> → el resolvedor identifica sus activos soportados;
> → los activos que faltan se descargan;
> → se elige una receta de ejecución apropiada;
> → la tarea se ejecuta;
> → los recursos se liberan y los activos reutilizables se retienen según política.

Los siete pasos están implementados y verificados en CI. Queda pendiente solo el
precio real de egress del proveedor (hoy se usa un precio por GB orientativo) y
tomar el driver de la medición de render en lugar de `strict-check`.
