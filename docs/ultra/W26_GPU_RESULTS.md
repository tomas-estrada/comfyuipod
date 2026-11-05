# W26 — Resultados de la campaña GPU (demo acotada, ejecutada)

Fecha: 2026-11-05. Estado: **demo neural L3 ejecutada y verificada; pod destruido**.

Esta campaña fue acotada por presupuesto: una demostración completa de
generación neural para cruzar la frontera L2→L3, no las cinco demos D01–D05 ni
la validación científica L4 (que exige holdouts y baselines).

## Autorización y presupuesto (A29.0)

| Dato | Valor |
|------|-------|
| GPU | RTX PRO 6000 Blackwell (Max-Q WS) |
| Perfil RAM host | 256 (oferta con 503 GB) |
| Saldo inicial | 17.06 USD |
| Saldo final | 16.47 USD |
| **Coste real de la campaña** | **0.59 USD** |
| Precio del pod | 1.336 USD/h |

## Infraestructura verificada (A29.1–A29.2)

- Imagen candidata fijada **por digest**: `ghcr.io/tomas-estrada/comfyuipod@sha256:0eddf85db32ed141da0e77ae636c226a73f5b1e232f646e55c92385affb2b53f` (tag `sha-f5c29dc`).
- Contrato vast 59289409, oferta 56921481.
- Hardware real medido por SSH, **coincide con la arquitectura**:

| Esperado | Real en el pod |
|----------|----------------|
| GPU RTX PRO 6000 Blackwell 96 GB | RTX PRO 6000 Blackwell Max-Q, 97887 MiB |
| RAM host (perfil 256) | 503 GB |
| Disco 260 GB | overlay 260 GB en `/` (259 GB libres) |
| Driver / CUDA / Torch | 615.71.09 / 12.8 / 2.9.1+cu128 |

- Seguridad confirmada: solo nginx expone 8188; `/object_info` sin auth devuelve **401** (ComfyUI interno en 127.0.0.1:18188). Coincide con la regla del repo.
- ComfyUI sirve **1658 nodos** en `/object_info` (motores horneados presentes).

## Pesos on-demand (A29.3)

El manifiesto `models-studio.json` tiene **87 modelos, todos `task`/`optional`,
cero `bootstrap`**: ningún peso generativo viene pre-bajado, por diseño. Para la
demo se bajó **un solo checkpoint** (receta más barata), no el catálogo.

- Receta: SDXL vía plantilla `sdxl_image` (equivalente a `qwen_image_1024`).
- Peso: `Juggernaut-XI-byRunDiffusion.safetensors`, 6.62 GiB (7.1 GB en disco),
  repo público `RunDiffusion/Juggernaut-XI-v11`, **sin token HF**.
- Adquisición: ~1–2 min.

## Resultado neural real (A29.8) — L3

Grafo SDXL txt2img de 7 nodos ejecutado por `POST /prompt` a ComfyUI real:

| Métrica | Valor **medido** |
|---------|------------------|
| Completada | sí |
| Tiempo de generación | 24.6 s |
| Pico VRAM real | **7684 MB** |
| Resolución | 1024×1024, 25 steps, cfg 7.0, seed 42 |
| Salida | `w26_sdxl_demo.png` (1024×1024, 1.5 MB) |

Prompt: "a red sports car parked on a coastal road at golden hour, cinematic,
highly detailed". Evidencia visual: `docs/ultra/evidence/w26_sdxl_demo.png`.

![Demo SDXL W26](evidence/w26_sdxl_demo.png)

### Medición reemplaza estimación (A10/A29)

La receta estimaba **18000 MB** de VRAM; la medición real fue **7684 MB**
(menos de la mitad). Esto confirma la regla del plan: la estimación filtra
candidatas, pero la **medición en GPU** es la verdad. Primera medición real que
sustituye una estimación declarada.

## Teardown garantizado (A29.6/A29.10)

- `vastai destroy instance 59289409` confirmado (pedía `[y/N]` interactivo).
- Verificado: **0 instancias activas** tras el destroy.
- Saldo dejó de consumirse. Coste total 0.59 USD.

## Hallazgos de operaciones (reales, pendientes de arreglo)

Dos bugs de arranque encontrados en el pod real; ninguno es de la capa de
capacidades, ambos bloquean una campaña autónoma:

1. **El entrypoint no arranca el servicio solo en vast.** Hubo que lanzar
   `/start.sh` manualmente; `onstart.log` quedó en 0 bytes. Impacto: una campaña
   automatizada no levantaría ComfyUI sin intervención.
2. **`require_disk_space` en `download-models.sh` da falso "sin espacio".** Con
   el overlay de vast (259 GB libres reales) el gate aborta todas las descargas
   con "[sin espacio en disco]". Se evitó bajando el checkpoint directo con `hf`.
   Impacto: bloquea la adquisición de pesos por el camino normal. **Release-blocker de ops.**

Ambos deben corregirse antes de una campaña D01–D05 desatendida.

## Alcance y honestidad

- Lo logrado: **L3** para la plataforma (imagen+hardware+servicio+seguridad) y
  **L3** para una generación de imagen SDXL real y medida.
- Lo NO logrado: D01–D05 completas, video (LTX/Wan), voz, 3D, y **L4** (holdouts
  y baselines). La campaña fue deliberadamente acotada a 0.59 USD.
- La VRAM de las demás recetas sigue siendo **estimada**, no medida.

## Próximos pasos para L4 / Ultra validada

1. Arreglar los dos hallazgos de ops (entrypoint y gate de disco).
2. Campaña completa D01–D05 con pesos de video/voz/3D y mediciones por receta.
3. Comparación contra baselines y holdouts (A29.8–A29.9), perfiles 128 y 256 por
   separado.
