# Catálogo Exhaustivo de Motores Open-Source para Agentes de IA (Actualizado a Octubre 2026)
## Estudio Aumentado por IA (ComfyUIPod): Directorio Técnico de Motores y Servicios Headless

> **Propósito del documento:** Referencia técnica completa y auditable para agentes implementadores, auditores y supervisores. Contiene los motores evaluados bajo el filtro **Agent-First** (interfaces programables, deterministas, sin entornos gráficos interactivos para humanos y sin C2PA).
>
> **Estado:** Revisión de verificación v2 (2026-10-29). Las correcciones respecto a la versión anterior están en el [Registro de Verificación](#registro-de-verificación-v2) al final del documento.

---

### Resumen de Criterios de Selección
1. **Modalidad de Control:** Operable 100% mediante CLI con códigos de salida y streams estándar, servidores **Model Context Protocol (MCP)** sobre `stdio`/socket, APIs nativas en Python/Rust/C++, o esquemas declarativos (JSON, YAML, DSL).
2. **Cero Dependencia GUI:** Exclusión de herramientas que requieran ratón, teclado o ventanas interactivas. Si una herramienta tiene GUI pero también un modo headless real (p. ej. Blender `-b`), se cataloga solo su modo headless.
3. **Producción No Comercial:** Exclusión de C2PA (Content Credentials). Las licencias **no comerciales** (CC-BY-NC, S-Lab, MPI, etc.) se aceptan porque el uso es no comercial, pero se indican explícitamente.
4. **Residencia en Hardware:** Mismos niveles que `EngineTier` en `studio/comfy_studio/engines/__init__.py`: `Resident` (siempre en GPU, ComfyUI), `Warm` (instalado y sano, carga al primer uso) e `Installable` (no horneado; el agente lo instala tras la puerta de `install_gate.py`).
5. **Instalación conforme a las reglas del repo:** Los `pip install` que aparecen en este documento describen dependencias para **la imagen de build** (Dockerfile) o para la puerta `install_gate.py`. La regla de seguridad n.º 6 de `AGENTS.md` prohíbe `pip install` en runtime fuera de ComfyUI-Manager con `PIP_CONSTRAINT`.

### Convenciones de este documento
* **Licencia:** Se distingue `código` / `pesos` cuando difieren. "NC" = no comercial.
* **Estrellas de GitHub:** Se omiten a propósito porque cambian semana a semana y en la versión anterior había cifras sin fuente. Para obtener la cifra actual: `gh api repos/<owner>/<repo> --jq .stargazers_count`.
* **VRAM:** Valores aproximados de inferencia en precisión reducida (BF16/FP8) salvo que se indique otra cosa; dependen de resolución, duración y offloading.
* **⚠ Verificar:** Marca datos que no se pudieron confirmar contra fuente primaria durante esta revisión.

---

## 1. Cognición, Memoria y Compresión de Contexto del Agente

### 1.1 Microsoft LLMLingua-2
* **Repositorio:** [`microsoft/LLMLingua`](https://github.com/microsoft/LLMLingua)
* **Licencia:** MIT.
* **Capacidad Núcleo:** Poda de tokens para historiales de chat, descripciones de planos y salidas de herramientas. Compresión típica 2x–5x sin llamada a API externa.
* **Interfaz del Agente:** API Python (`llmlingua`).
* **Invocación Determinista Headless:**
  ```python
  from llmlingua import PromptCompressor

  compressor = PromptCompressor(
      model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
      use_llmlingua2=True,
  )
  res = compressor.compress_prompt(
      [history_raw_text],
      rate=0.4,
      force_tokens=["\n", "User:", "Assistant:", "tool_call:", "status:"],
  )
  pruned_prompt = res["compressed_prompt"]
  ```
* **Huella de Hardware:** CPU o ~0.5–1 GB VRAM (BERT-base multilingüe).

### 1.2 Mem0 (Memoria Persistente para Agentes)
* **Repositorio:** [`mem0ai/mem0`](https://github.com/mem0ai/mem0)
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Memoria persistente por proyecto: extrae hechos (personajes, paletas, acuerdos de estilo) y los recupera por similitud, evitando arrastrar historiales largos.
* **Interfaz del Agente:** Python SDK (`mem0ai`) y servidor MCP (OpenMemory).
* **Huella de Hardware:** **Corrección:** no es "CPU puro + SQLite". Mem0 necesita un **LLM** (para extraer memorias), un **modelo de embeddings** y un **vector store** (Qdrant, Chroma, pgvector…); SQLite solo guarda el historial de cambios. Puede funcionar 100% local configurando Ollama como LLM/embedder; la VRAM la determina ese LLM.

### 1.3 Letta (antes MemGPT)
* **Repositorio:** [`letta-ai/letta`](https://github.com/letta-ai/letta)
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Memoria jerárquica (core / recall / archival) para agentes de larga duración. Solapa parcialmente con Mem0: elegir **uno** de los dos.
* **Interfaz del Agente:** Servidor REST (loopback) y Python SDK.

### 1.4 FlashRank
* **Repositorio:** [`PrithivirajDamodaran/FlashRank`](https://github.com/PrithivirajDamodaran/FlashRank)
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Re-ranking en CPU sobre ONNX Runtime (sin PyTorch) para reordenar candidatos recuperados (p. ej. top-50 de CLIP → top-3).
* **Interfaz del Agente:** Python API (`flashrank`).

### 1.5 BM25s
* **Repositorio:** [`xhluca/bm25s`](https://github.com/xhluca/bm25s)
* **Licencia:** MIT.
* **Capacidad Núcleo:** Búsqueda léxica BM25 en NumPy/SciPy para guiones, diálogos y metadatos. Complementa la búsqueda vectorial existente en `studio/comfy_studio/perception/store.py` (SQLite).

---

## 2. Preproducción, Guion Literario y Desglose

### 2.1 Fountain + screenplain
* **Repositorio:** [`vilcans/screenplain`](https://github.com/vilcans/screenplain)
* **Licencia:** MIT. Formato `.fountain`: especificación abierta.
* **Capacidad Núcleo:** Guion en texto plano (escenas, personajes, diálogos, acotaciones) que el LLM puede escribir de forma determinista y que se compila a PDF/HTML/FDX. El parser permite extraer el desglose (escenas → personajes → localizaciones).
* **Interfaz del Agente:** CLI (`screenplain guion.fountain guion.pdf`) y módulo Python.
* **Alternativa:** `afterwriting` (CLI Node para Fountain → PDF con estadísticas de guion) ⚠ Verificar estado de mantenimiento.

### 2.2 Shot-list / Storyboard en JSON (esquema propio)
* **Referencia de formato:** [`wonderunit/storyboarder`](https://github.com/wonderunit/storyboarder) (MIT). **Nota:** Storyboarder es una aplicación de escritorio Electron con GUI; solo se toma como referencia su formato de proyecto JSON (`.storyboarder`), no la aplicación.
* **Capacidad Núcleo:** Shot-list canónica: número de plano, duración, encuadre, movimiento de cámara, acción y prompt generativo.
* **Relación con el repo (corregido):** `studio/comfy_studio/engines/storyboard_gate.py` **no** valida un esquema JSON; es una **máquina de estados de aprobación** (`DRAFTING → AWAITING_APPROVAL → APPROVED/REJECTED`) que impide entrar en la fase cara (BUILD/POST) sin aprobación humana. El esquema de shot-list debe definirse aparte (p. ej. modelo Pydantic v2) y enlazarse con esa puerta.

### 2.3 Textstat
* **Repositorio:** [`textstat/textstat`](https://github.com/textstat/textstat)
* **Licencia:** MIT.
* **Capacidad Núcleo:** Métricas de legibilidad y conteos (sílabas, palabras) para estimar duración de locución. Soporta español (`textstat.set_lang("es")`).

---

## 3. Generación y Animación Visual (Vídeo Foundation & Control de Cámara)

### 3.0 Generación de Imagen Base (ya presente en el repo)
* **Estado:** Plantilla `sdxl_image` ya validada en `comfy_studio.templates`. Se lista para que el catálogo sea coherente con el repo; no se proponen generadores de imagen adicionales (serían redundantes).

### 3.1 Wan 2.1 & Wan 2.2 (Alibaba)
* **Repositorios:** [`Wan-Video/Wan2.1`](https://github.com/Wan-Video/Wan2.1) / [`Wan-Video/Wan2.2`](https://github.com/Wan-Video/Wan2.2)
* **Licencia:** Apache-2.0 (código y pesos).
* **Capacidad Núcleo:** T2V, I2V (480p/720p) y FLF2V (primer y último fotograma). Plantillas `wan_t2v`, `wan_i2v`, `wan_flf2v` ya presentes en el repo.
* **Interfaz del Agente:** Nodos ComfyUI (vía `ComfyUIEngine`) y CLI `generate.py`.
* **Huella de Hardware:** 14B: ~16–24 GB con FP8/GGUF + offloading; 1.3B: ~8 GB.

### 3.2 Wan2.1-VACE (Edición Todo-en-Uno: Inpainting, Restyle, Referencias)
* **Repositorio:** [`ali-vilab/VACE`](https://github.com/ali-vilab/VACE)
* **Licencia:** Apache-2.0 (variantes Wan2.1-VACE 1.3B/14B). Las variantes sobre LTX-Video heredan la licencia de LTX.
* **Capacidad Núcleo:** Un solo modelo para R2V (vídeo a partir de referencias), V2V y MV2V (vídeo enmascarado): Move/Swap/Reference/Expand/Animate-Anything. **Ya usado en el repo** (plantillas `vace_inpaint`, `vace_restyle`); faltaba en el catálogo.
* **Huella de Hardware:** Igual que el Wan base correspondiente.

### 3.3 Control de Cámara: CameraCtrl y Wan Fun Camera
* **Repositorio:** [`hehao13/CameraCtrl`](https://github.com/hehao13/CameraCtrl)
* **Licencia:** Apache-2.0 ⚠ Verificar.
* **Capacidad Núcleo:** Inyecta trayectorias de cámara (embeddings de Plücker) en modelos de difusión de vídeo.
* **Corrección:** CameraCtrl está implementado sobre **AnimateDiff y Stable Video Diffusion**, no sobre Wan. Para control de cámara en Wan usar las variantes **Wan2.1-Fun Control/Camera** de [`aigc-apps/VideoX-Fun`](https://github.com/aigc-apps/VideoX-Fun) (Apache-2.0), que tienen nodos ComfyUI.
* **Huella de Hardware:** ~2 GB adicionales sobre el modelo base (CameraCtrl).

### 3.4 Tencent HunyuanVideo
* **Repositorio:** [`Tencent/HunyuanVideo`](https://github.com/Tencent/HunyuanVideo)
* **Licencia (corregido):** **Tencent Hunyuan Community License**, no Apache-2.0. **No aplica en la Unión Europea, Reino Unido ni Corea del Sur.** Verificar el país de operación del pod antes de usarlo.
* **Capacidad Núcleo:** Vídeo con buena coherencia física y texto legible en escena.
* **Interfaz del Agente:** CLI (`sample_video.py`) y nodos ComfyUI.

### 3.5 Lightricks LTX-Video & CogVideoX
* **LTX-Video** ([`Lightricks/LTX-Video`](https://github.com/Lightricks/LTX-Video)): borradores muy rápidos (generación cercana a tiempo real en GPU de gama datacenter tipo H100). Código Apache-2.0; pesos bajo licencia propia de LTX (OpenRAIL-M en 0.9.x, "LTXV Open Weights" en versiones posteriores) ⚠ Verificar versión concreta.
* **CogVideoX** ([`THUDM/CogVideo`](https://github.com/THUDM/CogVideo)): código Apache-2.0; CogVideoX-2B Apache-2.0, CogVideoX-5B bajo "CogVideoX License". Es la base de ConsisID (3.7).

### 3.6 LivePortrait & LatentSync
* **LivePortrait** ([`KwaiVGI/LivePortrait`](https://github.com/KwaiVGI/LivePortrait)): reanimación facial de imágenes a partir de vídeo guía. Código MIT; depende de modelos InsightFace (uso no comercial).
* **LatentSync** ([`bytedance/LatentSync`](https://github.com/bytedance/LatentSync)): lip-sync en espacio latente. Apache-2.0.

### 3.7 ConsisID (Identidad Consistente sin Entrenar LoRA)
* **Repositorio:** [`PKU-YuanGroup/ConsisID`](https://github.com/PKU-YuanGroup/ConsisID) · Pesos: `BestWishYsh/ConsisID-preview`
* **Licencia:** Apache-2.0 (código); pesos derivados de CogVideoX-5B (ver licencia CogVideoX).
* **Capacidad Núcleo:** Text-to-video con preservación de identidad (IPT2V) **tuning-free** a partir de una sola foto facial. Descomposición en frecuencias dentro del DiT: extractor global de baja frecuencia (proporciones faciales) y extractor local de alta frecuencia (rasgos finos). Reduce el "drift" facial entre planos.
* **Interfaz del Agente (corregido):** El ejemplo CLI anterior era inventado (`--face_image`, `--output_dir *.mp4`). La vía verificada es el pipeline oficial de `diffusers`:
  ```python
  import torch
  from diffusers import ConsisIDPipeline
  from diffusers.pipelines.consisid.consisid_utils import (
      prepare_face_models,
      process_face_embeddings_infer,
  )
  from diffusers.utils import export_to_video

  model_dir, device, dtype = "./ConsisID-preview", "cuda", torch.bfloat16
  fh1, fh2, clip_m, main_m, eva_mean, eva_std = prepare_face_models(
      model_dir, device=device, dtype=dtype
  )
  pipe = ConsisIDPipeline.from_pretrained(model_dir, torch_dtype=dtype).to(device)
  pipe.enable_model_cpu_offload()
  pipe.vae.enable_tiling()

  id_cond, id_vit_hidden, image, face_kps = process_face_embeddings_infer(
      fh1, clip_m, fh2, eva_mean, eva_std, main_m, device, dtype,
      "assets/actor_ref.png", is_align_face=True,
  )
  video = pipe(
      image=image, prompt=prompt, num_inference_steps=50, guidance_scale=6.0,
      use_dynamic_cfg=False, id_vit_hidden=id_vit_hidden, id_cond=id_cond,
      kps_cond=face_kps, generator=torch.Generator(device).manual_seed(42),
  )
  export_to_video(video.frames[0], "output/shot_04_actor.mp4", fps=8)
  ```
* **Huella de Hardware:** Del orden de CogVideoX-5B; con `enable_model_cpu_offload` + VAE tiling cabe en GPUs de 24 GB ⚠ Verificar cifra exacta. Depende de InsightFace (NC).

### 3.8 StoryDiffusion (Consistencia en Storyboards / Viñetas)
* **Repositorio:** [`HVision-NKU/StoryDiffusion`](https://github.com/HVision-NKU/StoryDiffusion)
* **Licencia:** ⚠ Verificar (Apache-2.0 o MIT según versión del repo).
* **Capacidad Núcleo (corregido):** Usa **Consistent Self-Attention** (no "atención cruzada temporal") para mantener el mismo personaje en una serie de **imágenes** (cómic/storyboard). El componente de vídeo (Motion Predictor) no está liberado completo. Útil en la fase de borrador del `storyboard_gate`, no como generador de vídeo final.

### 3.9 ViewCrafter (Síntesis de Vistas Nuevas con Trayectoria de Cámara)
* **Repositorio:** [`Drexubery/ViewCrafter`](https://github.com/Drexubery/ViewCrafter)
* **Licencia:** Apache-2.0 ⚠ Verificar.
* **Capacidad Núcleo:** A partir de una o pocas imágenes construye una nube de puntos (DUSt3R) y genera vídeo con difusión siguiendo una trayectoria de cámara explícita (órbitas, travellings) sobre un set ya aprobado.
* **Interfaz del Agente:** Scripts Python de inferencia. **Corrección:** no se encontró un nodo ComfyUI oficial (`ComfyUI-ViewCrafter`); retirar esa afirmación hasta verificarla.
* **Huella de Hardware (corregido):** ~23.5 GB solo la difusión a 576×1024 (autores usan GPUs de 32–40 GB); ~13.8 GB en la variante 320×512. Dependencia DUSt3R: licencia CC BY-NC-SA 4.0.

---

## 4. Segmentación de Vídeo, Rotoscopia, Tracking y Flujo Óptico

### 4.1 Meta SAM 2 / SAM 2.1
* **Repositorio:** [`facebookresearch/sam2`](https://github.com/facebookresearch/sam2)
* **Licencia:** Apache-2.0 (código y pesos).
* **Capacidad Núcleo:** Segmentación y rotoscopia de vídeo con prompts visuales (puntos/caja) y propagación con memoria, gestionando oclusiones.
* **Invocación Determinista Headless (corregida):** `build_sam2_video_predictor` requiere **config + checkpoint**; la versión anterior pasaba solo el checkpoint y tenía la indentación del bloque rota.
  ```python
  import torch
  from sam2.build_sam import build_sam2_video_predictor

  predictor = build_sam2_video_predictor(
      "configs/sam2.1/sam2.1_hiera_l.yaml", "checkpoints/sam2.1_hiera_large.pt"
  )
  with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
      state = predictor.init_state(video_path="frames/plano_01/")  # carpeta de JPEG
      predictor.add_new_points_or_box(
          inference_state=state, frame_idx=0, obj_id=1, box=[100, 150, 400, 600]
      )
      for frame_idx, obj_ids, mask_logits in predictor.propagate_in_video(state):
          mask = (mask_logits[0] > 0.0).cpu().numpy()
          save_mask(f"masks/{frame_idx:05d}.png", mask)
  ```
* **Huella de Hardware:** ~4–8 GB según tamaño Hiera.

### 4.2 Meta SAM 3 (Segmentación por Concepto de Texto) — NUEVO
* **Repositorio:** [`facebookresearch/sam3`](https://github.com/facebookresearch/sam3) · Pesos `facebook/sam3` (acceso restringido en Hugging Face).
* **Licencia:** **SAM License** (propia de Meta, no Apache): uso libre de royalties con restricciones (militar, ITAR, etc.). Requiere aceptar la licencia y token HF.
* **Valor complementario:** SAM 2 necesita coordenadas; SAM 3 acepta **frases nominales** ("el robot", "los escombros") y detecta, segmenta y sigue **todas las instancias** en vídeo. Es la forma más agent-friendly de rotoscopia: el LLM no tiene que inventar cajas.
* **Interfaz del Agente:** Python API.

### 4.3 Meta CoTracker3 (Tracking Denso de Puntos)
* **Repositorio:** [`facebookresearch/co-tracker`](https://github.com/facebookresearch/co-tracker)
* **Licencia (corregido):** **CC-BY-NC 4.0** (no Apache).
* **Capacidad Núcleo:** Seguimiento de cuadrículas densas de puntos con robustez a oclusiones; ancla HUDs, decals o heridas CGI a superficies en movimiento.
* **Interfaz del Agente:** `torch.hub.load("facebookresearch/co-tracker", "cotracker3_offline")`.
* **Huella de Hardware:** ~3–4 GB.

### 4.4 Flujo Óptico: torchvision RAFT y UniMatch
* **Repositorios:** RAFT en [`pytorch/vision`](https://github.com/pytorch/vision) (`torchvision.models.optical_flow.raft_large`, BSD-3) / [`princeton-vl/RAFT`](https://github.com/princeton-vl/RAFT) (BSD-3) / [`autonomousvision/unimatch`](https://github.com/autonomousvision/unimatch) (MIT).
* **Capacidad Núcleo:** Campos de movimiento por píxel (forward/backward). Usos en el estudio:
  - *Motion blur vectorial:* pases de velocidad para composición.
  - *Retiming:* el flujo **no genera fotogramas por sí mismo**; se usa para warping. Para interpolación final ya está Practical-RIFE (§15).
  - *Detección de flicker/inestabilidad* como señal de QA (§20).
* **Interfaz del Agente:** torchvision (sin repo extra, recomendado):
  ```python
  from torchvision.models.optical_flow import raft_large, Raft_Large_Weights

  weights = Raft_Large_Weights.DEFAULT
  model = raft_large(weights=weights).eval().cuda()
  img1, img2 = weights.transforms()(frame_a, frame_b)
  flows = model(img1.cuda(), img2.cuda())  # lista de refinamientos; usar flows[-1]
  ```
  UniMatch (corregido): `python main_flow.py --inference_dir frames/ --output_path flow_out/ --resume <ckpt>` (el flag es `--output_path`, no `--output_dir`).
* **Huella de Hardware:** ~2–4 GB.

---

## 5. Espacio 3D, Mallas PBR y Simulación en GPU

### 5.1 Blender Headless (`blender -b` / `bpy`) — NUEVO, faltaba
* **Repositorio:** [`blender/blender`](https://projects.blender.org/blender/blender) (espejo GitHub `blender/blender`).
* **Licencia:** GPL-2.0-or-later.
* **Por qué se añade:** El catálogo citaba Cycles, Mantaflow, Cell Fracture y Cryptomatte, pero no tenía entrada para el propio Blender, que es el motor que los ejecuta. El repo ya tiene `studio/comfy_studio/engines/blender.py` (`BlenderEngine`).
* **Interfaz del Agente:** `blender -b escena.blend -P script.py -- --args` o el módulo `bpy` de PyPI (Blender como librería Python, ligado a una versión concreta de Python).
  ```bash
  blender -b shot_07.blend -P render_shot.py -- --hdri hdr/shot_07.exr --frames 1-120
  ```
* **Huella de Hardware:** Cycles con OptiX/CUDA: 2–20+ GB según escena.

### 5.2 Genesis World (Física Generativa Universal)
* **Repositorio:** [`Genesis-Embodied-AI/genesis-world`](https://github.com/Genesis-Embodied-AI/genesis-world) (paquete PyPI `genesis-world`).
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Física en GPU desde Python: rígidos, líquidos (SPH), telas (PBD), MPM. Render por rasterizador, renderer propio Nyx o trazado de rayos LuisaRender (este último requiere compilación aparte).
* **Interfaz del Agente:** `gs.init(backend=gs.gpu); scene = gs.Scene(show_viewer=False)`.

### 5.3 NVIDIA Warp (Simulación Diferenciable en GPU)
* **Repositorio:** [`NVIDIA/warp`](https://github.com/NVIDIA/warp)
* **Licencia:** Apache-2.0 (desde 2025; versiones antiguas usaban NVIDIA Source Code License).
* **Capacidad Núcleo:** Kernels Python compilados JIT a CUDA; partículas, telas, FEM, con autodiff.
* **Invocación Determinista Headless (corregida: el ejemplo anterior usaba variables no definidas):**
  ```python
  import numpy as np
  import warp as wp

  wp.init()


  @wp.kernel
  def integrate(pos: wp.array(dtype=wp.vec3), vel: wp.array(dtype=wp.vec3), dt: float):
      tid = wp.tid()
      v = vel[tid] + wp.vec3(0.0, -9.8, 0.0) * dt
      vel[tid] = v
      pos[tid] = pos[tid] + v * dt


  n = 1_000_000
  positions = wp.array(np.zeros((n, 3), dtype=np.float32), dtype=wp.vec3, device="cuda")
  velocities = wp.zeros(n, dtype=wp.vec3, device="cuda")
  wp.launch(integrate, dim=n, inputs=[positions, velocities, 0.016], device="cuda")
  ```
* **Huella de Hardware:** 1–10 GB según número de elementos.

### 5.4 Microsoft TRELLIS & TRELLIS.2
* **Repositorios:** [`microsoft/TRELLIS`](https://github.com/microsoft/TRELLIS) / [`microsoft/TRELLIS.2`](https://github.com/microsoft/TRELLIS.2)
* **Licencia (corregido):** **MIT** (no Apache-2.0) ⚠ Verificar TRELLIS.2.
* **Capacidad Núcleo:** Imagen/texto → malla 3D, Gaussian Splatting o radiance field. TRELLIS usa *Structured Latents (SLAT)*; la estructura **O-Voxel** y PBR completo corresponden a TRELLIS.2.

### 5.5 Hunyuan3D-2 / 2.1, Nerfstudio / gsplat y OpenUSD
* **Hunyuan3D-2 / 2.1** ([`Tencent/Hunyuan3D-2`](https://github.com/Tencent/Hunyuan3D-2)): mallas + texturas PBR. **Licencia Tencent Hunyuan Community (excluye UE, Reino Unido y Corea del Sur).** Plantilla `hunyuan3d_image_to_3d` ya presente en el repo.
* **Nerfstudio / gsplat** (Apache-2.0): reconstrucción de sets reales a 3D Gaussian Splatting. Relacionado con `capture3d.py` del repo.
* **OpenUSD** ([`PixarAnimationStudios/OpenUSD`](https://github.com/PixarAnimationStudios/OpenUSD)): composición de escenas. **Corrección:** es de **Pixar** (gobernanza AOUSD), no de la ASWF. Licencia Tomorrow Open Source Technology License (derivada de Apache-2.0).

---

## 6. Suite "VFX Pesados" (Matchmoving, Volúmenes, Look-dev y Pases)

### 6.1 COLMAP / GLOMAP, MASt3R y VGGT (Matchmoving y SfM)
* **COLMAP** ([`colmap/colmap`](https://github.com/colmap/colmap), BSD-3): SfM/MVS clásico; trayectorias de cámara 6-DOF y nubes de puntos. **GLOMAP** ([`colmap/glomap`](https://github.com/colmap/glomap), BSD-3): SfM global mucho más rápido con salida compatible COLMAP.
* **MASt3R** ([`naver/mast3r`](https://github.com/naver/mast3r)): matching y mapas de puntos neuronales. **Licencia corregida: CC BY-NC-SA 4.0** (no Apache).
* **VGGT** ([`facebookresearch/vggt`](https://github.com/facebookresearch/vggt)): predicción feed-forward de cámaras, profundidad y puntos en segundos. ⚠ Verificar licencia de pesos (existe checkpoint comercial separado).
* **Advertencia:** Los planos generados por IA no siempre son geométricamente consistentes; validar el error de reproyección antes de usar la cámara resuelta en Blender.

### 6.2 OpenVDB / NanoVDB (Almacenamiento Volumétrico)
* **Repositorio:** [`AcademySoftwareFoundation/openvdb`](https://github.com/AcademySoftwareFoundation/openvdb)
* **Licencia:** Apache-2.0 (antes MPL-2.0).
* **Capacidad Núcleo (corregido):** OpenVDB es el **formato y librería** de volúmenes dispersos (humo, fuego, polvo); **no simula**. La simulación la hace Blender Mantaflow (headless con `blender -b`) y exporta `.vdb`; NanoVDB permite leerlos en GPU. **fVDB** (NVIDIA) es un framework de deep learning sobre voxels dispersos, no un simulador de pirotecnia.

### 6.3 MaterialX / OpenPBR
* **Repositorio:** [`AcademySoftwareFoundation/MaterialX`](https://github.com/AcademySoftwareFoundation/MaterialX)
* **Licencia:** Apache-2.0.
* **Origen (corregido):** Iniciado en Lucasfilm/ILM (2012) y usado en producciones de Star Wars; la vinculación específica con *Transformers* no está documentada y se retira. OpenPBR es un modelo de superficie de la ASWF (Adobe + Autodesk).
* **Capacidad Núcleo:** Grafos de materiales declarativos (desgaste en aristas, arañazos, grasa) con generación de GLSL, OSL, MDL y MSL.

### 6.4 Cryptomatte
* **Repositorio:** [`Psyop/Cryptomatte`](https://github.com/Psyop/Cryptomatte)
* **Licencia:** BSD-3.
* **Capacidad Núcleo:** Especificación de pases de ID en OpenEXR multicapa, con antialiasing, motion blur y DOF correctos. Blender Cycles/EEVEE lo genera nativamente; el agente extrae mattes por nombre de objeto/material.

### 6.5 Fractura y Dinámica de Vehículos
* **Blender Cell Fracture** (addon incluido, GPL): fractura Voronoi de geometría para destrucción.
* **Project Chrono / PyChrono** ([`projectchrono/chrono`](https://github.com/projectchrono/chrono), BSD-3): dinámica multicuerpo de vehículos, orugas y colisiones.

---

## 7. Efectos Visuales (VFX), Motion Graphics y Composición por Capas

### 7.1 EffectCraft
* **Repositorio:** [`storytold/effectcraft`](https://github.com/storytold/effectcraft)
* **Licencia:** Apache-2.0 ⚠ Verificar (no confirmada en fuente primaria durante esta revisión).
* **Capacidad Núcleo:** Motion graphics y composición por capas escrito en Rust (capas, keyframes, expresiones, cámaras 3D, efectos). Proyectos en JSON versionado (`.ecproj`); todas las acciones pasan por un registro central de comandos.
* **Interfaz del Agente:** CLI `effectcraft-cli`, canal TCP local y servidor MCP (`effectcraft-cli mcp`, `stdio`). Confirmado en fuentes secundarias.
* **Riesgo:** Proyecto joven; validar madurez del render headless antes de pasar a `Warm`.

### 7.2 Natron (`NatronRenderer`)
* **Repositorio:** [`NatronGitHub/Natron`](https://github.com/NatronGitHub/Natron)
* **Licencia:** GPL-2.0. Compatible OpenFX.
* **Capacidad Núcleo:** Composición nodal de EXR multicapa con render por línea de comandos (`NatronRenderer proyecto.ntp`).
* **Riesgo:** Mantenimiento lento (última versión mayor 2.5.x); para composición sencilla puede bastar `oiiotool` + FFmpeg.

### 7.3 OpenImageIO (`oiiotool`) y frei0r
* **OpenImageIO** ([`AcademySoftwareFoundation/OpenImageIO`](https://github.com/AcademySoftwareFoundation/OpenImageIO), Apache-2.0): conversión EXR, manipulación de canales, convoluciones, ruido (`--noise`, base para grano), transformaciones de color OCIO. **Corrección:** no tiene "grano de 35 mm" ni aberración cromática como operaciones nativas; se construyen combinando operaciones (ruido + blur, desplazamiento por canal).
* **frei0r** ([`dyne/frei0r`](https://github.com/dyne/frei0r), GPL-2.0): 100+ efectos usables desde FFmpeg (`-vf frei0r=...`). **Corrección:** se ejecutan en **CPU**; NVENC es solo el codificador de salida, no acelera estos filtros.

---

## 8. Estabilización de Vídeo y Suavizado de Movimiento

### 8.1 FFmpeg vid.stab (opción principal para vídeo generado)
* **Repositorio:** [`georgmartius/vid.stab`](https://github.com/georgmartius/vid.stab)
* **Licencia:** GPL-2.0.
* **Capacidad Núcleo:** Estabilización en dos pasadas (detección de movimiento local por bloques → transformación suavizada). Funciona sin metadatos de giróscopo, que los vídeos generados por IA no tienen. **Corrección:** no usa "Lucas-Kanade ni grafo temporal".
* **Invocación Determinista Headless:**
  ```bash
  ffmpeg -i in.mp4 -vf vidstabdetect=shakiness=5:accuracy=15:result=transforms.trf -f null -
  ffmpeg -i in.mp4 -vf "vidstabtransform=input=transforms.trf:smoothing=30:zoom=0,unsharp=5:5:0.8:3:3:0.4" -c:v libx264 -crf 16 out.mp4
  ```

### 8.2 Gyroflow
* **Repositorio:** [`gyroflow/gyroflow`](https://github.com/gyroflow/gyroflow)
* **Licencia:** GPL-3.0.
* **Capacidad Núcleo:** Estabilización a partir de datos de giróscopo; también tiene estimación de movimiento por flujo óptico como alternativa.
* **Limitación (añadida):** Requiere **perfil de lente**, que no existe para vídeo sintético; su punto fuerte es el metraje real de cámara/dron (útil con `capture3d`). Para planos generados usar vid.stab.
* **Interfaz del Agente:** CLI con archivos de proyecto `.gyroflow`.

### 8.3 OpenCV (estabilización a medida)
* **Corrección:** `cv2.videostab` apenas está expuesto en los bindings de Python y OpenCV no incluye MeshFlow. La vía práctica es un estabilizador propio: `cv2.goodFeaturesToTrack` + `cv2.calcOpticalFlowPyrLK` + `cv2.estimateAffinePartial2D` + suavizado de la trayectoria.

---

## 9. Profundidad, Relighting, Iluminación y Color

### 9.1 Video Depth Anything & Depth Anything V2
* **Repositorios:** [`DepthAnything/Video-Depth-Anything`](https://github.com/DepthAnything/Video-Depth-Anything) / [`DepthAnything/Depth-Anything-V2`](https://github.com/DepthAnything/Depth-Anything-V2)
* **Licencia (añadido):** Modelos *Small* Apache-2.0; *Base/Large* CC-BY-NC-4.0.
* **Capacidad Núcleo:** Profundidad temporalmente consistente en vídeo (relativa; hay variantes métricas) para DOF sintético, niebla por profundidad y conversión estéreo.

### 9.2 IC-Light, color-matcher y OCIO
* **IC-Light** ([`lllyasviel/IC-Light`](https://github.com/lllyasviel/IC-Light), Apache-2.0): relighting de **imágenes** (SD1.5). No es temporalmente consistente en vídeo; usar en fotogramas clave o referencias.
* **color-matcher** ([`hahnec/color-matcher`](https://github.com/hahnec/color-matcher), GPL-3.0): transferencia de color entre planos (Reinhard, MKL, histogram matching) en CPU.
* **OpenColorIO** (BSD-3) y **colour-science** (BSD-3): gestión de color ACES y LUTs.

### 9.3 DiffusionLight (Iluminación HDR 360° desde una Imagen)
* **Repositorios:** [`DiffusionLight/DiffusionLight`](https://github.com/DiffusionLight/DiffusionLight) / [`DiffusionLight/DiffusionLight-Turbo`](https://github.com/DiffusionLight/DiffusionLight-Turbo) / [`DiffusionLight/DiffusionLight-ComfyUI`](https://github.com/DiffusionLight/DiffusionLight-ComfyUI)
* **Licencia:** ⚠ Verificar (depende además de la licencia de SDXL).
* **Capacidad Núcleo:** Pinta una esfera cromada en la imagen con SDXL + ControlNet de profundidad + LoRA de exposición, la desproyecta a un mapa equirrectangular y fusiona varias exposiciones en un **HDR `.exr`**. Sirve para iluminar elementos CGI en Blender con la luz del fondo generado.
* **Invocación Determinista Headless (corregida: el comando `--output_hdr` anterior no existe; son tres pasos):**
  ```bash
  # Entrada: imágenes 1024x1024 (rellenar con negro si no son cuadradas)
  python inpaint.py --dataset input_shots --output_dir output
  python ball2envmap.py --ball_dir output/square --envmap_dir output/envmap
  python exposure2hdr.py --input_dir output/envmap --output_dir output/hdr
  ```
* **Huella de Hardware:** Del orden de SDXL + ControlNet (~10–16 GB) ⚠ Verificar. La variante Turbo reduce el tiempo a <30 s por imagen.

---

## 10. Vídeo Estéreo 3D y Vídeo Espacial

### 10.1 Empaquetado estéreo con FFmpeg
* **Corrección importante:** El ejemplo anterior (`[0:v][1:v]stereo3d=...`) **no funciona**: `stereo3d` tiene **una sola entrada** y convierte entre formatos estéreo. Para unir dos vistas se usa `hstack`/`vstack`:
  ```bash
  # Dos vistas -> Side-by-Side
  ffmpeg -i left.mp4 -i right.mp4 -filter_complex "[0:v][1:v]hstack=inputs=2[v]" -map "[v]" -c:v libx264 -crf 16 sbs.mp4
  # SBS -> anaglifo rojo/cian para revisión rápida
  ffmpeg -i sbs.mp4 -vf stereo3d=sbsl:arcd -c:v libx264 anaglyph.mp4
  ```
* **Generar la segunda vista:** FFmpeg no sintetiza vistas a partir de profundidad. Opciones: DIBR propio (warping con el mapa de Video Depth Anything + inpainting de huecos) o **StereoCrafter** ([`TencentARC/StereoCrafter`](https://github.com/TencentARC/StereoCrafter), licencia **solo investigación/no comercial**, depende de SVD y DepthCrafter).
* **MV-HEVC (Apple Spatial Video):** FFmpeg decodifica MV-HEVC desde la 7.1; la **codificación** requiere x265 ≥ 4.0 con soporte multivista o herramientas de Apple en macOS ⚠ Verificar soporte en la build del pod. La versión anterior afirmaba que FFmpeg lo empaquetaba directamente.

---

## 11. Retoque, Inpainting, Matting y Super-resolución

### 11.1 BiRefNet
* **Repositorio:** [`ZhengPeng7/BiRefNet`](https://github.com/ZhengPeng7/BiRefNet)
* **Licencia:** MIT.
* **Capacidad Núcleo:** Segmentación dicotómica / matting de alta resolución (pelo, transparencias). Por fotograma: combinar con SAM 2/3 para estabilidad temporal.

### 11.2 LaMa & ProPainter
* **LaMa** ([`advimman/lama`](https://github.com/advimman/lama), Apache-2.0): inpainting de imagen con convoluciones de Fourier.
* **ProPainter** ([`sczhou/ProPainter`](https://github.com/sczhou/ProPainter)): inpainting temporal de vídeo guiado por flujo. **Licencia: NTU S-Lab License 1.0 (no comercial).**

### 11.3 Super-resolución
* **Real-ESRGAN** ([`xinntao/Real-ESRGAN`](https://github.com/xinntao/Real-ESRGAN), BSD-3): upscaling por fotograma (puede introducir flicker en vídeo).
* **Spandrel** ([`chaiNNer-org/spandrel`](https://github.com/chaiNNer-org/spandrel), MIT). **Corrección:** no es un modelo de super-resolución; es un **cargador universal** de arquitecturas (ESRGAN, SwinIR, DAT…) usado por ComfyUI.
* **SeedVR2 — NUEVO** ([`ByteDance-Seed/SeedVR`](https://github.com/ByteDance-Seed/SeedVR), Apache-2.0 código y pesos): restauración/upscaling de **vídeo** por difusión en **un solo paso**, temporalmente consistente (3B y 7B; FP8/GGUF disponibles). Nodo ComfyUI `ComfyUI-SeedVR2_VideoUpscaler` (numz). Complementa a Real-ESRGAN, que trabaja por fotograma y parpadea en vídeo. Encaja en `upscaler.py`.

---

## 12. Mocap Headless y Captura Humana 3D

### 12.1 4D-Humans (HMR 2.0)
* **Repositorio:** [`shubham-goel/4D-Humans`](https://github.com/shubham-goel/4D-Humans)
* **Licencia:** MIT (código); modelo SMPL con licencia propia NC.
* **Capacidad Núcleo (corregido):** Malla y pose humana 3D desde vídeo monocámara en topología **SMPL** (no SMPL-X), en coordenadas de cámara.

### 12.2 Motion Diffusion Model (MDM) & DWPose
* **MDM** ([`GuyTevet/motion-diffusion-model`](https://github.com/GuyTevet/motion-diffusion-model), MIT): movimiento esquelético desde texto. Salida en posiciones de articulaciones/SMPL; **`.bvh`/`.fbx` requiere conversión aparte**.
* **DWPose** ([`IDEA-Research/DWPose`](https://github.com/IDEA-Research/DWPose), Apache-2.0): pose 2D de cuerpo completo para condicionamiento.

### 12.3 WHAM y GVHMR (Movimiento en Coordenadas de Mundo)
* **Repositorio:** [`yohanshin/WHAM`](https://github.com/yohanshin/WHAM)
* **Licencia:** ⚠ Verificar (código académico; depende de SMPL, NC).
* **Capacidad Núcleo:** Pose 3D y **trayectoria global** del actor con cámara en movimiento, usando SLAM (DPVO) y refinamiento por contacto con el suelo para reducir el foot-skating.
* **Invocación (corregida: `--save_world_coords` no existe):**
  ```bash
  python demo.py --video plano_accion.mp4 --save_pkl            # trayectoria global en .pkl
  python demo.py --video plano_accion.mp4 --save_pkl --run_smplify   # mayor precisión
  python demo.py --video plano.mp4 --estimate_local_only        # sin SLAM, solo coordenadas de cámara
  ```
  La exportación a `.bvh`/`.fbx` **no es nativa**: se hace con script propio (p. ej. retarget SMPL → armature en Blender headless).
* **Alternativa:** **GVHMR** ([`zju3dv/GVHMR`](https://github.com/zju3dv/GVHMR), SIGGRAPH Asia 2024), sucesor con mejor precisión global ⚠ Verificar licencia.
* **Huella de Hardware:** ~4–8 GB.

### 12.4 Captura Facial: MediaPipe Face Landmarker y EMOCA
* **Corrección importante:** EMOCA **no** produce los 52 blendshapes de ARKit; regresa parámetros **FLAME** (forma, expresión, pose de mandíbula/cuello/ojos). Pasar a ARKit requiere un mapeo adicional. Además su licencia es **no comercial de MPI**, no MIT.
* **MediaPipe Face Landmarker — NUEVO** ([`google-ai-edge/mediapipe`](https://github.com/google-ai-edge/mediapipe), Apache-2.0): produce directamente **52 coeficientes de blendshape compatibles con ARKit** por fotograma en CPU (`output_face_blendshapes=True`). Es la vía más simple para pasar la actuación facial a un rig 3D en Blender.
  ```python
  from mediapipe.tasks import python as mp_tasks
  from mediapipe.tasks.python import vision

  opts = vision.FaceLandmarkerOptions(
      base_options=mp_tasks.BaseOptions(model_asset_path="face_landmarker.task"),
      running_mode=vision.RunningMode.VIDEO,
      output_face_blendshapes=True,
  )
  landmarker = vision.FaceLandmarker.create_from_options(opts)
  # result = landmarker.detect_for_video(mp_image, timestamp_ms)
  # result.face_blendshapes[0] -> 52 categorías {category_name, score}
  ```
* **EMOCA** ([`radekd91/emoca`](https://github.com/radekd91/emoca), licencia NC MPI): reconstrucción facial 3D FLAME con mayor fidelidad emocional; útil si el rig es FLAME. Su sucesor es el framework `inferno`.
* **Huella de Hardware:** MediaPipe: CPU, 0 VRAM. EMOCA: ~3–4 GB.

---

## 13. Montaje por Código, Timelines Declarativos y Render Headless

### 13.1 Remotion
* **Repositorio:** [`remotion-dev/remotion`](https://github.com/remotion-dev/remotion)
* **Licencia (corregido):** **Remotion License**: código disponible, gratis para individuos, organizaciones sin ánimo de lucro y empresas de hasta 3 personas; empresas de 4+ necesitan Company License. No es una licencia OSI.
* **Capacidad Núcleo:** Render de composiciones React a vídeo desde props JSON (`npx remotion render`).
* **Relación con el repo (corregido):** `studio/comfy_studio/engines/editor.py` es *stub-first*: el backend real (**Remotion o OTIO + FFmpeg**) **aún no está decidido**. La versión anterior afirmaba que Remotion ya era el backend.

### 13.2 MLT (`melt`), OpenTimelineIO y PySceneDetect
* **MLT** ([`mltframework/mlt`](https://github.com/mltframework/mlt), LGPL-2.1): edición no lineal sin GUI con XML.
* **OpenTimelineIO** ([`AcademySoftwareFoundation/OpenTimelineIO`](https://github.com/AcademySoftwareFoundation/OpenTimelineIO), Apache-2.0): modelo de timeline intercambiable; FCPXML y otros formatos mediante adaptadores (`otio-fcpx-xml-adapter`, etc.).
* **PySceneDetect** ([`Breakthrough/PySceneDetect`](https://github.com/Breakthrough/PySceneDetect), BSD-3): detección de cortes (`scenedetect -i in.mp4 detect-adaptive list-scenes`).

---

## 14. Tipografía Dinámica y Gráficos Vectoriales

### 14.1 ThorVG
* **Repositorio:** [`thorvg/thorvg`](https://github.com/thorvg/thorvg)
* **Licencia:** MIT. Usado por Godot (importación SVG), LVGL y el reproductor dotLottie.
* **Capacidad Núcleo:** Rasterizado de SVG y Lottie sin navegador ni Node.js, para HUDs, retículas y overlays.
* **Interfaz del Agente (corregido: `thorvg-lottie2png` no existe):**
  ```bash
  tvg-svg2png hud.svg -r 1920x1080            # SVG -> PNG
  tvg-lottie2gif anim.json -r 1920x1080 -f 30 # Lottie -> GIF (solo preview: 256 colores)
  ```
  Para secuencias **RGBA** aptas para composición, usar la API C++ (`tvg::SwCanvas` + `tvg::Animation`) o el binding comunitario `thorvg-python` ⚠ Verificar API del binding.
* **Huella de Hardware:** CPU, 0 VRAM, decenas de MB de RAM.

### 14.2 Tipografía y Formas 2D
* **libass** (ISC) + **HarfBuzz** (MIT) + **FreeType** (FTL/GPL-2.0): subtítulos `.ass` con shaping complejo, karaoke y transformaciones.
* **Samsung rlottie** (MIT): rasterizador Lottie. **Nota:** mantenimiento prácticamente detenido; ThorVG es la opción preferente.
* **skia-python** (BSD-3) y **PyCairo** (LGPL-2.1/MPL-1.1): dibujo 2D programático.

---

## 15. Transiciones Cinemáticas e Interpolación Temporal

* **FFmpeg `xfade`:** ~50 transiciones integradas. **Corrección:** es un filtro de **CPU**; existen variantes `xfade_opencl` y `xfade_vulkan` para GPU. NVENC solo codifica.
* **gl-transitions** ([`gl-transitions/gl-transitions`](https://github.com/gl-transitions/gl-transitions), MIT): transiciones GLSL; requieren un renderizador GL headless (EGL) o un filtro FFmpeg externo.
* **Practical-RIFE** ([`hzwer/Practical-RIFE`](https://github.com/hzwer/Practical-RIFE), MIT): interpolación de fotogramas (24 → 48/60 fps) y transiciones suaves entre planos.

---

## 16. Foley y Efectos de Sonido (SFX)

### 16.1 MMAudio
* **Repositorio:** [`hkchengrex/MMAudio`](https://github.com/hkchengrex/MMAudio)
* **Licencia:** MIT (código); pesos CC-BY-NC 4.0 ⚠ Verificar.
* **Capacidad Núcleo:** Vídeo→audio sincronizado (pasos, impactos, motores) con prompt de texto opcional.

### 16.2 AudioGen & Stable Audio Open
* **AudioGen** (AudioCraft, [`facebookresearch/audiocraft`](https://github.com/facebookresearch/audiocraft)): SFX desde texto. Código MIT, pesos CC-BY-NC 4.0.
* **Stable Audio Open**: ambientes y texturas. Stability AI Community License.

### 16.3 FoleyCrafter
* **Repositorio:** [`open-mmlab/FoleyCrafter`](https://github.com/open-mmlab/FoleyCrafter)
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Vídeo→audio con adaptador semántico y controlador temporal (detector de onsets) para alinear los transitorios con los impactos visuales. Alternativa/segunda opinión a MMAudio; no conviene tener ambos `Warm` a la vez.
* **Huella de Hardware:** ~6–8 GB ⚠ Verificar.

---

## 17. Voz, Doblaje y Avatares

> **Hueco detectado:** La versión anterior no tenía ningún TTS con buena cobertura de **español**: ChatTTS solo soporta chino e inglés y CosyVoice 2 no incluía español. Se añaden opciones verificadas.

### 17.1 Chatterbox Multilingual (TTS + Clonación, incluye español) — NUEVO
* **Repositorio:** [`resemble-ai/chatterbox`](https://github.com/resemble-ai/chatterbox)
* **Licencia:** MIT (código y pesos).
* **Capacidad Núcleo:** TTS de ~0.5B con clonación zero-shot y control de intensidad emocional; incluye español (con variantes latinoamericana y de España).
* **Nota:** Añade una marca de agua perceptual (PerTh) en el audio. No es C2PA, pero es un mecanismo de procedencia; está en el código abierto y se puede tener en cuenta en la política del proyecto.

### 17.2 Kokoro-82M (TTS Ligero, incluye español) — NUEVO
* **Repositorio:** [`hexgrad/kokoro`](https://github.com/hexgrad/kokoro) · Pesos `hexgrad/Kokoro-82M`
* **Licencia:** Apache-2.0 (código y pesos).
* **Capacidad Núcleo:** TTS de 82M parámetros, rápido en CPU, con voces en español (`lang_code="e"`). Sin clonación: ideal para narración temporal, animatics y borradores.
  ```python
  from kokoro import KPipeline
  import soundfile as sf

  pipeline = KPipeline(lang_code="e")
  for i, (_, _, audio) in enumerate(pipeline("Plano uno: el robot despierta.", voice="ef_dora")):
      sf.write(f"vo_{i:03d}.wav", audio, 24000)
  ```

### 17.3 Fun-CosyVoice 3, Fish Speech y RVC
* **CosyVoice 3** ([`FunAudioLLM/CosyVoice`](https://github.com/FunAudioLLM/CosyVoice), Apache-2.0): clonación zero-shot y cross-lingual con instrucciones de emoción/velocidad; **v3 añade español** (es, zh, en, ja, ko, de, fr, it, ru). Actualizado desde CosyVoice 2.
* **Fish Speech** ([`fishaudio/fish-speech`](https://github.com/fishaudio/fish-speech)): TTS multilingüe. Código Apache-2.0; pesos CC-BY-NC-SA 4.0.
* **RVC** ([`RVC-Project/Retrieval-based-Voice-Conversion-WebUI`](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI), MIT): conversión de timbre con modelos `.pth` + índice FAISS. El repo principal es una WebUI; para uso headless se usa su inferencia por CLI o forks como paquete.
* **faster-whisper** (MIT) y **WhisperX** (BSD-2): transcripción, diarización y alineación por palabra (base para subtítulos y lip-sync).

### 17.4 ChatTTS
* **Repositorio:** [`2noise/ChatTTS`](https://github.com/2noise/ChatTTS)
* **Licencia (corregido):** código **AGPL-3.0**, pesos **CC-BY-NC 4.0** (no Apache-2.0).
* **Capacidad Núcleo:** Prosodia conversacional (risas, pausas, respiraciones). **Limitación:** solo chino e inglés.

### 17.5 EchoMimic V2
* **Repositorio:** [`antgroup/echomimic_v2`](https://github.com/antgroup/echomimic_v2) (V1: `antgroup/echomimic`, antes `BadToBest/EchoMimic`).
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Avatares de medio cuerpo animados por audio (labios, manos, cabeza). Relacionado con `lipsync.py`.
* **Huella de Hardware:** ~8–16 GB según resolución ⚠ Verificar.

---

## 18. Música

### 18.1 YuE / YuE2 (M-A-P)
* **Repositorio:** [`multimodal-art-projection/YuE`](https://github.com/multimodal-art-projection/YuE)
* **Licencia:** Apache-2.0.
* **Capacidad Núcleo:** Canciones completas (voz + acompañamiento) desde letra y estilo; YuE2 añade estéreo 48 kHz y condicionamiento simbólico (ABC).
* **Huella de Hardware (corregido):** Oficial ~24 GB recomendados en BF16 (picos ~14–15 GB medidos); versiones cuantizadas comunitarias en ~8–9 GB.

### 18.2 ACE-Step (alternativa rápida) — NUEVO
* **Repositorio:** [`ace-step/ACE-Step`](https://github.com/ace-step/ACE-Step)
* **Licencia:** Apache-2.0 ⚠ Verificar.
* **Valor complementario:** Generación musical por difusión mucho más rápida que los modelos autoregresivos tipo YuE; útil para iterar bandas sonoras en la fase de borrador.

### 18.3 OpenMMLab Amphion
* **Repositorio:** [`open-mmlab/Amphion`](https://github.com/open-mmlab/Amphion)
* **Licencia:** MIT.
* **Capacidad Núcleo:** Toolkit de investigación (TTS, SVC, TTA, vocoders). Para producción se prefieren los motores dedicados anteriores; mantener como `Installable`.

### 18.4 MusicGen & FluidSynth
* **MusicGen** (AudioCraft): instrumentales; código MIT, pesos CC-BY-NC 4.0.
* **FluidSynth** ([`FluidSynth/fluidsynth`](https://github.com/FluidSynth/fluidsynth), LGPL-2.1): síntesis MIDI + SoundFont en CPU (`fluidsynth -ni sf2 tema.mid -F tema.wav`).

---

## 19. Ingeniería de Audio, Acústica y Masterización

### 19.1 Spotify Pedalboard
* **Repositorio:** [`spotify/pedalboard`](https://github.com/spotify/pedalboard)
* **Licencia:** GPL-3.0.
* **Capacidad Núcleo:** Cadena DSP en Python (compresor, EQ, gate, reverb, limitador) y hosting de plugins VST3/AU.
* **Corrección:** Pedalboard **no tiene auto-ducking integrado**. El ducking se implementa en código: envolvente RMS de la voz → curva de ganancia sobre la música (NumPy) o compresor con sidechain externo.

### 19.2 pyroomacoustics
* **Repositorio:** [`LCAV/pyroomacoustics`](https://github.com/LCAV/pyroomacoustics), MIT.
* **Capacidad Núcleo:** Simulación acústica de salas (fuentes y micrófonos en $(X, Y, Z)$) para reverberación coherente con el espacio del plano. La binauralización con HRTF requiere convolución adicional.

### 19.3 Loudness y Masterización
* **Corrección:** **Matchering** ([`sergree/matchering`](https://github.com/sergree/matchering), GPL-3.0) iguala el espectro y el nivel **a una pista de referencia**; no normaliza por sí mismo a EBU R128 / −14 LUFS.
* **Normalización de loudness (añadido):** FFmpeg `loudnorm` (dos pasadas) o **pyloudnorm** (MIT):
  ```bash
  ffmpeg -i mix.wav -af loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json -f null -
  # segunda pasada con measured_I / measured_TP / measured_LRA / measured_thresh
  ```

### 19.4 Separación y Limpieza
* **Demucs v4** ([`adefossez/demucs`](https://github.com/adefossez/demucs), MIT): separación de stems (el repo de Meta está archivado; el fork del autor sigue activo).
* **DeepFilterNet** ([`Rikorose/DeepFilterNet`](https://github.com/Rikorose/DeepFilterNet), MIT/Apache-2.0): supresión de ruido en tiempo real.

### 19.5 VoiceFixer y ClearerVoice-Studio
* **VoiceFixer** ([`haoheliu/voicefixer`](https://github.com/haoheliu/voicefixer)): restauración general de voz (ruido, reverberación, clipping, baja resolución → 44.1 kHz) con vocoder neuronal. ⚠ Verificar licencia.
  ```bash
  voicefixer --infile dialogo.wav --outfile dialogo_clean.wav --mode 0
  voicefixer --infolder raw/ --outfolder clean/
  ```
  **Corrección:** el flag correcto es `--infile/--outfile` (no `-i/-o`), y VoiceFixer **no** usa MossFormer2.
* **ClearerVoice-Studio** ([`modelscope/ClearerVoice-Studio`](https://github.com/modelscope/ClearerVoice-Studio), Apache-2.0): mejora de voz, separación, super-resolución y extracción de hablante objetivo; aquí es donde viven **MossFormer/MossFormer2**.
* **Huella de Hardware:** CPU o ~1–2 GB VRAM.

---

## 20. Control de Calidad (QA) y Telemetría

### 20.1 VBench (Métricas Diseñadas para Vídeo Generativo) — NUEVO
* **Repositorio:** [`Vchitect/VBench`](https://github.com/Vchitect/VBench)
* **Licencia:** Apache-2.0.
* **Valor complementario:** DOVER y FAST-VQA se entrenaron con vídeo de usuarios (UGC), no con vídeo generado. VBench mide los fallos típicos de la generación: *subject consistency*, *background consistency*, *temporal flickering*, *motion smoothness*, *dynamic degree*, *aesthetic quality*, *imaging quality*. Algunas dimensiones funcionan sin prompt (vídeo propio).
* **Uso recomendado:** Puerta de QA por dimensiones con umbrales calibrados con planos aprobados del propio estudio.

### 20.2 DOVER y FAST-VQA (Calidad sin Referencia)
* **Repositorios:** [`VQAssessment/DOVER`](https://github.com/VQAssessment/DOVER) / [`VQAssessment/FAST-VQA-and-FasterVQA`](https://github.com/VQAssessment/FAST-VQA-and-FasterVQA)
* **Licencia:** ⚠ Verificar.
* **Capacidad Núcleo:** Evaluación sin referencia (NR-VQA) con dos perspectivas: estética y técnica.
* **Invocación (corregida: `python -m dover.evaluate_video --input_video` era inventado):**
  ```bash
  python evaluate_one_video.py -v renders/shot_01.mp4 -f        # puntuación fusionada normalizada [0,1]
  python evaluate_one_video.py -v renders/shot_01.mp4           # estética/técnica por separado
  python evaluate_one_video.py -v renders/shot_01.mp4 -d cpu -f # sin GPU
  ```
* **Corrección sobre puntuaciones:** Sin `-f`, las puntuaciones estética/técnica **no** están en 0–1; se dan en escala normalizada respecto a bases UGC. El umbral `technical_score < 0.65` de la versión anterior era arbitrario: hay que calibrarlo con planos propios. El script imprime los resultados; para JSON hay que envolverlo.
* **Huella de Hardware:** ~2–3 GB.

### 20.3 Telemetría Clásica
* **Netflix VMAF** ([`Netflix/vmaf`](https://github.com/Netflix/vmaf), BSD+Patent): calidad con referencia; útil para medir pérdidas por compresión o escalado (original vs. codificado), no para juzgar la generación.
* **Sondas FFmpeg** (`blackdetect`, `freezedetect`, `silencedetect`, `ebur128`): detección de negros, congelados, silencios y loudness.
* **MediaInfo** (BSD-2): `mediainfo --Output=JSON`.

---

## 21. Infraestructura del Pod

### 21.1 Presente en el repo
* **comfyctl** (Rust): supervisor (`tini` → `comfyctl supervise`), strict-check, triage y servidor HTTP de estado en **`127.0.0.1:18189`** (corregido: la versión anterior decía 8189) con caché rápida de 3 s y pesada de 60 s.
* **nginx:** único servicio expuesto, en el puerto **8188** (`config/nginx.conf`). Todos los demás servicios en `127.0.0.1`.

### 21.2 Propuestos (no presentes en el repo; ver `docs/auditorias/AUDITORIA_AGENTE_V1.md`)
* **Valkey** (BSD-3): cola/broker en memoria si el volumen de jobs supera lo que cubre la cola actual.
* **Rclone** (MIT): sincronización con S3/R2. Las credenciales deben pasar por `SecretRedactor`.
* **ntfy** (Apache-2.0/GPL-2.0): notificaciones push. El *topic* funciona como secreto y debe redactarse; si se autoaloja, ligarlo a `127.0.0.1` detrás de nginx.
* **Caddy:** **descartado** como gateway: el repo ya usa nginx y la regla de seguridad n.º 2 fija nginx como único punto expuesto. Solo tendría sentido como sustituto completo, no en paralelo.

---

## Matriz Resumen de Recursos por Motor

| Frente | Motor Principal | Licencia (código / pesos) | Interfaz Agente | Tier sugerido | VRAM aprox. | CPU / RAM |
|---|---|---|---|---|---|---|
| **Compresión de contexto** | LLMLingua-2 | MIT | Python API | Warm | 0–1 GB | 2 cores / 1 GB |
| **Memoria del agente** | Mem0 | Apache-2.0 | Python SDK / MCP | Warm | la del LLM configurado | 1 core / 0.5 GB + vector store |
| **Re-ranking** | FlashRank | Apache-2.0 | Python API | Warm | 0 | 1 core / 0.1 GB |
| **Guion** | Fountain + screenplain | MIT | CLI / Python | Warm | 0 | 1 core / 0.1 GB |
| **Vídeo base** | Wan 2.1 / 2.2 | Apache-2.0 | ComfyUI / CLI | Resident | 16–24 GB (14B) | 8 cores / 32 GB |
| **Edición de vídeo** | Wan2.1-VACE | Apache-2.0 | ComfyUI | Resident/Warm | = Wan base | 8 cores / 32 GB |
| **Control de cámara** | Wan Fun Camera / CameraCtrl | Apache-2.0 | ComfyUI / Python | Warm | +2 GB | 4 cores / 8 GB |
| **Identidad consistente** | ConsisID | Apache-2.0 / CogVideoX | diffusers | Installable | ≤24 GB con offload ⚠ | 4 cores / 16 GB |
| **Vistas nuevas** | ViewCrafter | Apache-2.0 ⚠ / DUSt3R NC | Python | Installable | 14–24 GB | 4 cores / 16 GB |
| **Rotoscopia (visual)** | SAM 2.1 | Apache-2.0 | Python API | Warm | 4–8 GB | 4 cores / 16 GB |
| **Rotoscopia (texto)** | SAM 3 | SAM License | Python API | Warm | ⚠ Verificar | 4 cores / 16 GB |
| **Tracking denso** | CoTracker3 | CC-BY-NC 4.0 | torch.hub | Warm | 3–4 GB | 4 cores / 8 GB |
| **Flujo óptico** | torchvision RAFT / UniMatch | BSD-3 / MIT | Python | Warm | 2–4 GB | 4 cores / 8 GB |
| **3D / render** | Blender headless | GPL-2.0+ | CLI / bpy | Warm | 2–20 GB | 8 cores / 16 GB |
| **Física GPU** | Genesis World | Apache-2.0 | Python API | Installable | 4–12 GB | 8 cores / 16 GB |
| **Simulación diferenciable** | NVIDIA Warp | Apache-2.0 | Python | Installable | 1–10 GB | 4 cores / 8 GB |
| **Imagen → 3D** | TRELLIS.2 / Hunyuan3D-2.x | MIT / Tencent Community | Python / ComfyUI | Installable/Warm | 8–16 GB | 4 cores / 16 GB |
| **Matchmoving** | COLMAP/GLOMAP · MASt3R | BSD-3 · CC BY-NC-SA | CLI / Python | Installable | 0–8 GB | 8 cores / 16 GB |
| **Volúmenes** | OpenVDB + Mantaflow | Apache-2.0 / GPL | Blender CLI | Warm | 2–6 GB | 8 cores / 16 GB |
| **Materiales** | MaterialX | Apache-2.0 | Python API | Warm | 0 | 2 cores / 2 GB |
| **Pases de ID** | Cryptomatte | BSD-3 | EXR (Blender) | Warm | 0 | — |
| **Motion graphics** | EffectCraft | Apache-2.0 ⚠ | CLI / MCP | Installable | 0–1 GB | 4 cores / 4 GB |
| **Composición nodal** | Natron | GPL-2.0 | NatronRenderer | Installable | 0–2 GB | 4 cores / 4 GB |
| **Post imagen** | OpenImageIO | Apache-2.0 | CLI `oiiotool` | Warm | 0 | 2 cores / 1 GB |
| **Estabilización** | vid.stab (Gyroflow para metraje real) | GPL-2.0 / GPL-3.0 | FFmpeg / CLI | Warm | 0 | 4 cores / 2 GB |
| **Profundidad** | Video Depth Anything | Apache (S) / NC (B,L) | Python / ComfyUI | Warm | 4–8 GB | 4 cores / 8 GB |
| **Relighting** | IC-Light | Apache-2.0 | Python / ComfyUI | Installable | ~6 GB | 4 cores / 8 GB |
| **HDRI desde imagen** | DiffusionLight | ⚠ Verificar | Python (3 pasos) / ComfyUI | Installable | 10–16 GB ⚠ | 4 cores / 16 GB |
| **Estéreo** | FFmpeg hstack/stereo3d (+StereoCrafter) | LGPL/GPL (+NC) | CLI | Warm | 0 (+GPU StereoCrafter) | 4 cores / 2 GB |
| **Color** | color-matcher / OCIO | GPL-3.0 / BSD-3 | Python | Warm | 0 | 1 core / 0.5 GB |
| **Matting** | BiRefNet | MIT | Python / ComfyUI | Warm | ~3.5 GB | 2 cores / 4 GB |
| **Upscale de vídeo** | SeedVR2 | Apache-2.0 | ComfyUI | Warm | 8–24 GB (3B/7B, FP8) | 4 cores / 16 GB |
| **Mocap global** | WHAM / GVHMR | ⚠ / SMPL NC | CLI | Installable | 4–8 GB | 4 cores / 8 GB |
| **Mocap facial** | MediaPipe Face Landmarker | Apache-2.0 | Python | Warm | 0 | 2 cores / 1 GB |
| **Montaje** | Remotion u OTIO+FFmpeg (pendiente) | Remotion License / Apache-2.0 | CLI | Warm | 0–2 GB | 8 cores / 8 GB |
| **Vectorial / HUD** | ThorVG | MIT | CLI / C++ | Warm | 0 | 1 core / <0.1 GB |
| **Foley** | MMAudio (o FoleyCrafter) | MIT / NC ⚠ · Apache-2.0 | CLI / ComfyUI | Warm (uno) | 6–8 GB | 4 cores / 16 GB |
| **TTS español + clonación** | Chatterbox Multilingual / CosyVoice 3 | MIT / Apache-2.0 | Python | Warm | 2–6 GB | 2 cores / 4 GB |
| **TTS ligero español** | Kokoro-82M | Apache-2.0 | Python | Warm | 0 (CPU) | 2 cores / 1 GB |
| **Conversión de timbre** | RVC | MIT | CLI | Warm | 2–4 GB | 2 cores / 4 GB |
| **Avatares por audio** | EchoMimic V2 | Apache-2.0 | Python CLI | Installable | 8–16 GB ⚠ | 4 cores / 16 GB |
| **Canción completa** | YuE2 (ACE-Step para borradores) | Apache-2.0 | CLI / Python | Installable | 8–24 GB | 8 cores / 32 GB |
| **DSP de audio** | Pedalboard | GPL-3.0 | Python | Warm | 0 | 2 cores / 0.2 GB |
| **Acústica de sala** | pyroomacoustics | MIT | Python | Warm | 0 | 2 cores / 0.5 GB |
| **Loudness** | loudnorm / pyloudnorm (+Matchering) | LGPL / MIT (+GPL-3.0) | CLI / Python | Warm | 0 | 1 core / 0.2 GB |
| **Restauración de voz** | VoiceFixer / ClearerVoice | ⚠ / Apache-2.0 | CLI / Python | Warm | 0–2 GB | 2 cores / 2 GB |
| **QA vídeo generativo** | VBench (+DOVER) | Apache-2.0 (+⚠) | Python / CLI | Warm | 2–6 GB | 2 cores / 8 GB |
| **Supervisor** | comfyctl | (repo) | CLI / HTTP :18189 | Resident | 0 | 1 core / ~20 MB |

---

## Registro de Verificación (v2)

Revisión hecha el 2026-10-29 contrastando con el código del repositorio y con documentación/READMEs de cada proyecto.

### A. Afirmaciones sobre el repo corregidas
| Elemento | Antes | Ahora (evidencia) |
|---|---|---|
| Puerto de `comfyctl` | 8189 | **18189** (`comfyctl/src/http.rs`, `config.rs`) |
| Sondas "cada 2 s" | Afirmado | Sin evidencia; el servidor HTTP usa caché de 3 s / 60 s (`http.rs`) |
| `storyboard_gate.py` | Validación Pydantic de esquema Storyboarder | Máquina de estados de aprobación |
| `editor.py` | Backend Remotion | Stub-first; Remotion vs OTIO+FFmpeg sin decidir |
| Caddy / Valkey / Rclone / ntfy | Presentados como daemons del pod | No existen en el repo; nginx es el gateway (`config/nginx.conf`) |
| VACE, SDXL, Hunyuan3D | VACE y SDXL ausentes | Añadidos: tienen plantillas validadas en el repo |
| `pip install` | Sin matiz | Aclarado: solo en build o vía `install_gate` (regla n.º 6) |

### B. Licencias corregidas
HunyuanVideo y Hunyuan3D (Tencent Community, excluye UE/UK/KR) · CoTracker3 (CC-BY-NC) · MASt3R (CC BY-NC-SA) · ChatTTS (AGPL-3.0 + CC-BY-NC) · TRELLIS (MIT) · EMOCA (NC de MPI) · Remotion (Remotion License, no OSI) · ProPainter (S-Lab NC) · Video Depth Anything B/L (NC) · OpenUSD (Pixar, no ASWF).

### C. Comandos y código corregidos
SAM 2 (faltaba la config) · Warp (variables sin definir) · ConsisID (CLI inventado → pipeline `diffusers`) · WHAM (`--save_world_coords` no existe → `--save_pkl`) · DOVER (`-m dover.evaluate_video` no existe → `evaluate_one_video.py -v ... -f`) · VoiceFixer (`-i/-o` → `--infile/--outfile`) · ThorVG (`thorvg-lottie2png` → `tvg-svg2png` / `tvg-lottie2gif`) · DiffusionLight (`--output_hdr` → 3 scripts) · UniMatch (`--output_dir` → `--output_path`) · FFmpeg estéreo (`stereo3d` con 2 entradas no funciona → `hstack`).

### D. Errores técnicos corregidos
CameraCtrl no se integra con Wan · OpenVDB no simula · fVDB no es pirotecnia · frei0r/xfade no van en GPU vía NVENC · `oiiotool` no trae grano ni aberración cromática nativos · vid.stab no usa Lucas-Kanade · `cv2.videostab`/MeshFlow no están disponibles en Python · Gyroflow necesita perfil de lente · 4D-Humans usa SMPL (no SMPL-X) · EMOCA no produce blendshapes ARKit · StoryDiffusion usa Consistent Self-Attention sobre imágenes · Spandrel es un cargador, no un modelo · Pedalboard no tiene auto-ducking · Matchering no normaliza a LUFS · VoiceFixer no usa MossFormer2 · Mem0 necesita LLM + embeddings + vector store · DOVER sin `-f` no da 0–1 y el umbral 0.65 no estaba calibrado · MV-HEVC: FFmpeg decodifica, no codifica · VRAM de ViewCrafter (~23.5 GB, no 8–12) y de YuE (24 GB oficial).

### E. Añadidos (huecos reales, no duplicados)
Blender headless (el motor que ejecuta Cycles/Mantaflow/Cryptomatte no tenía entrada) · Wan2.1-VACE · SAM 3 (rotoscopia por texto) · torchvision RAFT · SeedVR2 (upscale de vídeo temporalmente consistente) · MediaPipe Face Landmarker (52 blendshapes ARKit en CPU) · GVHMR · GLOMAP / VGGT · StereoCrafter · Chatterbox Multilingual, Kokoro y CosyVoice 3 (**no había TTS en español**) · ACE-Step · loudnorm/pyloudnorm · VBench (QA diseñado para vídeo generativo).

### F. Eliminado o rebajado
Cifras de estrellas (no verificables; ver convención) · `fastfountain` (no se pudo confirmar) · afirmación "MaterialX desarrollado para Transformers" · nodos ComfyUI no confirmados (`ComfyUI-ViewCrafter`) · Caddy como gateway.

### G. Pendiente de verificación (marcado ⚠ en el texto)
Licencias de EffectCraft, StoryDiffusion, ViewCrafter, DiffusionLight, WHAM, GVHMR, DOVER/FAST-VQA, VoiceFixer, ACE-Step, CameraCtrl, pesos de MMAudio y de VGGT · VRAM exacta de ConsisID, DiffusionLight, SAM 3, FoleyCrafter y EchoMimic V2 · API del binding `thorvg-python` · soporte MV-HEVC en la build de FFmpeg del pod · estado de mantenimiento de `afterwriting`.
