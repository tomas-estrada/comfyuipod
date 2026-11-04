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

⚠ = el umbral queda por debajo de lo derivado. No es un fallo inmediato: el
cortacircuitos por archivo de `download-models.sh` impide el desborde, pero la
puerta de arranque no garantiza que la descarga completa quepa. **Ajustar
`default_min_disk_gb` en [`comfyctl/src/config.rs`](../comfyctl/src/config.rs)
a estos valores derivados es trabajo pendiente** (requiere toolchain de Rust).

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

### Pendiente

- **Retención de caché**: no hay política de expulsión; hoy todo se conserva
  hasta que el operador interviene.
- **Clasificar las Fases 1/2 por perfil**: el manifiesto core declara `class`,
  pero la selección por `MODEL_PROFILE` sigue viviendo en bash.

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

Los pasos 1, 2, 3, 5 y 6 están implementados. Quedan como trabajo pendiente las
**recetas de ejecución medidas** (paso 4) y la retención de caché con política
explícita (paso 6).
