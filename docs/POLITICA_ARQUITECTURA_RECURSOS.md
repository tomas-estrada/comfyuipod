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

Dónde buscar cada uno, si un despliegue necesita moverlos sin reconstruir la
imagen:

| Variable | Catálogo que apunta |
|---|---|
| `STUDIO_MANIFEST` | **solo Fase 3** (`config/models-studio.json`) |
| `STUDIO_CORE_MANIFEST` | **el core** (`config/models-core.json`) |

Ninguna sustituye a la otra: son catálogos disjuntos, así que fijar la de Fase 3
no puede dejar ciego al core ni al revés. Si no se definen se usan las rutas
versionadas del árbol y las horneadas en la imagen
(`/etc/comfyui/models-*.json`). Las leen **cuatro** consumidores por igual —
`scripts/download-models.sh`, `studio/comfy_studio/capabilities.py`,
`scripts/storage_requirement.py` y el planificador de retención
(`studio/comfy_studio/cache_policy.py`)—, de modo que el requisito de disco, la
adquisición a demanda y la expulsión se derivan del mismo manifiesto que acaba
descargando el arranque.

Difieren en dos cosas, y las dos están dichas a propósito:

| Consumidor | Override ausente | Candidatas por omisión |
|---|---|---|
| `scripts/download-models.sh` | cae en `config/models-core.json` | raíz del repo |
| `scripts/storage_requirement.py` | cae en las rutas conocidas | CWD y raíz del repo |
| `capabilities`, `model_assets` | cae en las rutas conocidas | raíz del paquete primero, CWD después |
| `cache_policy` (planificador de retención) | el catálogo queda fuera **declarándolo** (`missing_overrides`) | raíz del paquete primero, CWD después |

Los tres últimos comparten un único orden de bases
(`model_assets.manifest_search_bases()`): si cada resolvedor tuviera el suyo, un
proceso lanzado desde un directorio con su propio `config/` podía leer un
catálogo y el planificador otro, y los dos disentirían sobre cuál estaba en vigor
sin que ninguno lo dijera. El planificador resolvía antes **solo** contra la raíz
del paquete (nunca contra el CWD), para que `prune_model_cache.py --json` desde
cualquier directorio no devolviera un plan vacío que se lee como «el volumen
está bien»; ahora el CWD queda como último recurso, detrás de la raíz del paquete
y de `/etc/comfyui/`.

Sobre el override ausente sí hay una diferencia deliberada: caer en las rutas
conocidas es lo correcto para quien va a **descargar** —abortar no arreglaría el
desacuerdo, solo lo convertiría en un fallo—, pero el planificador no puede
hacerlo en silencio. Si `STUDIO_CORE_MANIFEST` apunta a una ruta que no existe y
el plan cae sin decirlo, se emite con el otro catálogo presente,
`manifests_found: 1`, `unreadable_manifests: []` y un `keep_bytes` que se lee como
un volumen sano corto por el catálogo entero. Por eso lo declara en
`missing_overrides` y lo avisa por log.

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

### El SHA-1 de un blob de Git no es el SHA-1 crudo

`asset_acquire` calculaba el SHA-1 **crudo** de los bytes y lo comparaba con el
hash del manifiesto. Pero HuggingFace publica dos cosas distintas según el
fichero:

- **LFS** (`sha256`, 64 hex): el oid del objeto LFS, que **sí** es el SHA-256
  crudo del contenido.
- **git** (`sha1`, 40 hex): el OID del blob de git, que lleva la cabecera
  `blob <tamaño>\0` antes de hashear. **No** es el SHA-1 crudo.

Verificado contra la API de HuggingFace en vivo (test
`test_etag_is_the_git_blob_sha1_not_the_raw_one`): el `X-Linked-Etag` de un
`README.md` coincide con el OID del blob y no con el SHA-1 crudo.

Consecuencia real: los **29 activos** con hash SHA-1 del manifiesto de studio
fallaban todos la verificación y se borraban de staging. Ahora `_verify_integrity`
calcula el hash del blob para las entradas SHA-1, y el error muestra ambos valores
para que un fijado con la semántica equivocada se diagnostique.

### El driver se mide, no se hereda

La receta publicaba `driver: None` siempre: `_record_render_measurement` lo
pasaba a `None` a mano, así que la máquina descrita era la de `strict-check`
(S2) y no la del render. Ahora el muestreador lo lee de `/system_stats` **en el
mismo host y el mismo momento** que mide VRAM y RAM.

Las versiones de ComfyUI no publican la misma clave, así que se aceptan
`driver_version`, `driver` y `nvidia_driver_version`. Si ninguna está, se queda
a `None` en lugar de inventarlo: `comfyctl strict-check` sigue siendo la puerta
autoritativa del driver, pero la receta ya no afirma uno que no midió.

### Filesystems distintos

Punto 1: «Check usable space on **the actual filesystems** involved». La guarda
de disco medía solo el directorio de staging, pero el destino puede estar en otro
volumen —`custom_nodes/*` vive bajo `COMFY_APP_DIR`, no bajo `COMFY_DATA_DIR`—.
Mirar un solo filesystem permite quedarse sin espacio en el otro justo cuando el
activo ya está descargado. Ahora `_free_bytes_for` toma el más restrictivo de
staging y destino.

Relacionado, y encontrado al arreglarlo: `os.replace` falla con `EXDEV` entre
volúmenes. El movimiento ahora cae a copiar + `fsync` + borrar el original, y
cualquier otro error se propaga en lugar de degradarse en silencio.

### ¿Cabe en el GPU objetivo?

Punto 5: «Before recommending more expensive hardware, investigate supported
combinations of…». Las recetas medidas publican ahora `vram_fit`, que responde
con la medición real:

- `fits` / `headroom_mb` / `utilisation_pct` contra el **perfil seleccionado**
  (`STUDIO_HARDWARE_PROFILE`, por defecto la RTX PRO 6000 Blackwell de 96 GB,
  94 GB utilizables con la reserva de 2 GB). `STUDIO_TARGET_VRAM_GB` sigue
  existiendo como override manual, pero su default sale del perfil;
- si no cabe, la lista ordenada de optimizaciones que hay que **medir** antes de
  recomendar hardware más caro: precisión/cuantización, descarga secuencial,
  offloading, componentes en CPU, VAE tiling y parámetros de carga.

Deliberadamente **ninguna se marca como válida**: son específicas del modelo y la
política prohíbe aplicar flags genéricos a ciegas, tratar la RAM de host como
VRAM o afirmar compatibilidad universal. Cada una lleva por escrito por qué
podría no servir. Sin medición, `fits` es `null` y se dice que hay que ejecutar
una vez para medir.

La RAM de host medida viaja con la receta (`ram_peak_mb`) porque hay que
verificarla en cada oferta; el almacén no sabe cuánta RAM tiene la máquina.

### Cancelación durante la descarga

Punto 4: «Support progress, **cancellation**, recovery and reuse.» La cancelación
solo se comprobaba *entre* activos, y el cliente se lanzaba con
`subprocess.run`, que bloquea hasta que el fichero está completo: cancelar un
activo de 20 GB no tenía efecto hasta el final.

Ahora `_run_hf_download` lanza `hf download` en su propia sesión y lo sondea cada
segundo, así que `should_cancel` surte efecto en ~1 s. Al cancelar se mata el
árbol completo (el cliente delega en hijos) y se borra el parcial de staging,
para que la recuperación no se encuentre un fichero a medias. Cancelar no es un
fallo: no se reintenta.

Detalle de robustez: el `killpg` solo se aplica cuando el cliente liderea su
grupo. Sobre un grupo ajeno mataría procesos que no son nuestros — incluido el
nuestro, si el llamador no usó `start_new_session`.

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

## 5. Hardware objetivo

El perfil principal es **una NVIDIA RTX PRO 6000 Blackwell con 96 GB de VRAM**
(`rtx-pro-6000-blackwell-96-128`), con la RAM de host en perfiles separados de
**128 y 256 GB**. `studio/comfy_studio/hardware_profile.py` es la fuente única:

- `BLACKWELL_96_128` (96 GB VRAM / 128 GB RAM) es el perfil por defecto;
  `BLACKWELL_96_256` es la variante de 256 GB.
- `CONSUMER_32` (32 GB / 64 GB) se conserva por compatibilidad: **no limita el
  perfil principal**.
- `TARGET_VRAM_GB`, `target_vram_gb` y `STUDIO_TARGET_VRAM_GB` dejaron de estar
  clavados en 32: se derivan del perfil (96), y `STUDIO_TARGET_VRAM_GB` /
  `STUDIO_TARGET_RAM_GB` siguen siendo override manual para un operario.
- Reserva operativa explícita: **2 GB de VRAM** y **8 GB de RAM**, así que el
  perfil de 96 publica **94 GB utilizables** y el de 128 publica **120 GB**.
- El perfil se transmite a la API (`/studio/api/status`), al resolvedor de
  capacidades, a las recetas y al ayudante de macOS.

**VRAM y RAM se evalúan por separado.** `assess_ram_fit` juzga la RAM del host
contra el perfil elegido y `assess_vram_fit` la VRAM; la nota de `assess_ram_fit`
dice explícitamente que la RAM del host no es intercambiable por VRAM. Una receta
de 40 GB **cabe** en el perfil de 96 GB (utilización < 50 %, sin optimizaciones
propuestas) y **no cabe** en el de 32 GB, que entonces propone las seis
optimizaciones ordenadas. Antes `recipe_for_template` no propagaba el perfil a
`assess_vram_fit`, así que la API publicaba un número y la receta se juzgaba
contra el default.

**Exclusión mutua del trabajo GPU pesado.** `execute_prompt_job` —el único cuello
de botella— envuelve el envío, el sondeo y la medición en `_acquire_gpu_gate()`.
Un `asyncio.Semaphore` compartido **cuelga** cuando dos bucles de eventos lo
contienden (verificado empíricamente: el worker nunca vuelve), así que la
compuerta es `_GpuGate`, independiente del bucle (`threading.Lock` + sondeo no
bloqueante). La ventana de medición va **dentro** de la compuerta. Hay cuatro
pruebas de control: pico de concurrencia == 1, liberación tras fallo, tras
cancelación, y con semáforo inyectado.

- `comfy rent` filtra `num_gpus=1` (una oferta multi-GPU podía pasar el filtro
  de `gpu_ram` y alquilarse como una sola tarjeta), `cpu_ram>=128` por perfil y
  `cuda_vers>=12.8`.
- La validación en cliente **rechaza** ofertas sin `cpu_ram` o sin `cuda_vers`
  declarados, en lugar de dejarlas pasar por defecto.
- `strict-check` verifica el host real: driver ≥ 570, VRAM ≥ 20 GB, RAM con
  margen de kernel y límite de cgroup, disco y sonda `torch.cuda` real.

Antes de recomendar hardware más caro hay que agotar precision/quantización,
offloading, ejecución en CPU de componentes compatibles, VAE tiling, planificación
secuencial y parámetros de carga. **La RAM no es intercambiable por VRAM** y los
métodos son por modelo.

### Arranque bajo demanda

El arranque principal descarga **solo** bootstrap, y bootstrap es **vacío por
diseño**: las 10 entradas que tenía `config/models-core.json` se reclasificaron a
`task`. Pertenecer a Fase 1 no convierte un VAE ni un restaurador facial en
requisito para abrir la API y descubrir capacidades. `run_bootstrap()` resuelve
las entradas `class == "bootstrap"` del manifiesto; si el manifiesto no se puede
leer, **no descarga nada** (falla cerrado en lugar de llenar el disco con el
catálogo por una ruta mala). Los modos `--phase1/2/3/all` se conservan para
operarios.

La puerta de arranque (S5) se deriva de las descargas bootstrap reales, no de un
número fijo: hoy **11 GB** (0 de descarga + 0 de pico de arranque + 10 GiB de
margen), bajo el umbral declarado de 180 GB. Es la cifra honesta de «cuánto hace
falta tener libre para abrir la API con los pesos vacíos»; el requisito del
volumen es otro número y sigue siendo derivado (`min_volume_gb`: 231 GB para
`studio`, 238 GB para `unreal`, 34 GB para `minimal`).

La puerta **excluye** el pico de staging de la Fase 3: esa descarga corre en
segundo plano *después* de que ComfyUI está listo, así que cobrarla inflaba el
arranque con espacio que todavía no necesita. El volumen total sí reserva el
mayor de los tres picos —arranque, tarea en segundo plano y demanda—, porque no
coinciden en el tiempo.

### Pendiente

- **Mediciones GPU reales**: `assess_vram_fit` devuelve `fits: null` con
  «sin medición de VRAM todavía; ejecuta una vez para medir». **No se declara
  ejecución GPU validada por un import ni por un stub.**
- **Diagnóstico Blackwell específico** en `comfyctl strict-check`: hoy valida
  driver ≥ 570 / CUDA 12.8, pero no reconoce `NVIDIA RTX PRO 6000 Blackwell` con
  96 GB ni confronta VRAM/RAM contra el perfil elegido.
- **Precio real de egress** del proveedor: se sigue usando una cifra por GB
  orientativa.
- Las recetas comparables se agrupan por parámetros, modelo/revisión, precisión,
  offload y hardware; `template_id` por sí solo no basta, y sin evidencia el
  veredicto es desconocido o pendiente.

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

Los siete pasos están implementados y verificados en CI.

### Aceptación previa a GPU (2026-11-05)

| Criterio | Estado | Evidencia |
|---|---|---|
| A. Perfil 96 efectivo, RAM 128/256, admisión con límites y reserva | **Hecho** | `hardware_profile.py`: default 96 GB / 128 GB, 94 GB y 120 GB utilizables; `RAM_OPTIONS_GB=(128,256)`; 4 pruebas de control de exclusión GPU (pico 1, liberación tras fallo y tras cancelación) |
| B. Arranque con pesos vacíos, sin descarga task/optional | **Hecho** | `--bootstrap` con el manifiesto real: 0 archivos, 0 descargas; con una entrada bootstrap: 1 descarga y promoción atómica |
| C. Capacidad elegida: dependencias, reutilización, corrupción, cancelación, disco lleno | **Hecho** | Descarga real de un activo público de 32 B (`ByteDance/LatentSync-1.6 config.json`, pin sha1) → `downloaded`; segundo intento `skipped` con 0 descargas; destino corrupto → se re-descarga y restaura; volumen de 40 MB con margen de 10 GiB → `failed` con «espacio insuficiente» y ningún parcial promocionado |
| D. Grafo final con procesamiento real en CPU | **Pendiente** | Requiere el grafo consumiendo los activos declarados con el stack existente |
| E. GPU pesada serializada, liberación tras fallo, mediciones simuladas separadas | **Hecho** | 4 pruebas de control; `assess_vram_fit` con `fits: null` y nota explícita cuando no hay medición |
| F. Paquete instalado e imagen candidata en CI | **Hecho** | PR #106 fusionado en `main` como `957d6e6`; los tres jobs en verde sobre el SHA final (`63096e0`), sobre el merge (`957d6e6`) y sobre la cabeza de `main` tras el cierre del registro (`7dc6b83`). La build del SHA de la arquitectura (`957d6e6`) publicó `ghcr.io/tomas-estrada/comfyuipod:sha-957d6e6` con digest `sha256:4f99cd5ceb320052262440bb592007be01cf8ed98bc7b97ba59304fe174323a6`. `:latest` es una etiqueta móvil que sigue a la cabeza de `main`, así que su digest cambia con cada commit aunque el contenido ejecutable no cambie: el digest de una build concreta se lee del log del job, no de la API de paquetes. El atlas se verifica **dentro** de la imagen candidata (`verify-bundle` + `serve-check` sobre `/opt/studio-atlas`), así que solo se anuncia incluido si pertenece a ese commit y a esa imagen |

Queda pendiente solo el precio real de egress del proveedor (hoy se usa un precio
por GB orientativo), más las mediciones GPU reales, el diagnóstico Blackwell
específico en `comfyctl strict-check` y el criterio D (grafo final consumiendo
los activos declarados con procesamiento real en CPU). **No hay ejecución GPU
validada**: `assess_vram_fit` sigue devolviendo `fits: null` hasta que exista un
render medido, y ninguna prueba de este lote usó GPU.
