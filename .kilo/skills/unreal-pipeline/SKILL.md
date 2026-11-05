# Unreal Pipeline Skill

## Usage
Use this skill when working with the 3D/Unreal-grade output pipeline
(`MODEL_PROFILE=unreal`).

## Stack (implemented)
- Hunyuan3D-2.1: image → textured GLB mesh, via the template
  `hunyuan3d_image_to_3d` (weights `hunyuan_3d_v2.1.safetensors`, +7.37 GB).
- Blender 4.x headless engine (`engines/blender.py` + `BlenderRunner`):
  glTF/FBX export, UE Datasmith-compatible paths, ACES / EXR sequence
  rendering. `STUDIO_STUB_BLENDER=1` runs the deterministic stub.
- `capture3d` engine: nerfstudio-based capture/3D reconstruction
  (INSTALLABLE tier).
- `upscaler` engine: Real-ESRGAN ncnn-vulkan 2x/4x for image restoration.
- In-browser GLB preview through the panel gallery.

## Environment Variables
- `MODEL_PROFILE=unreal` (activates Studio + the 3D pipeline)
- `STUDIO_INCLUDE_3D=1` (implied by `MODEL_PROFILE=unreal`)
- `STUDIO_BLENDER_BIN` / `STUDIO_STUB_BLENDER` (binary override / stub mode)
- `STUDIO_STUB_<ENGINE>` per-engine stub switches (see `engines/__init__.py`)

> Not implemented (do not rely on them): TRELLIS / TripoSR, SUPIR, Depth
> Anything V2 and OpenUSD export exist only in
> `docs/propuestas/CATALOGO_MOTORES_OPENSOURCE_2026.md`. There are no
> `STUDIO_ENABLE_TRELLIS`, `STUDIO_INCLUDE_SUPIR`, `STUDIO_COMFY_CUDA_DEVICE`
> or `STUDIO_BLENDER_CUDA_DEVICE` variables in the code.

## Validation
1. `python -m comfy_studio.templates` → `hunyuan3d_image_to_3d` present and
   available (`available: true`)
2. `list_engines` reports `blender`, `capture3d` and `upscaler` with their
   tier and wired state
3. BlenderRunner tests with `STUDIO_STUB_BLENDER=1`
4. `comfyctl strict-check` with `MODEL_PROFILE=unreal` → S5 disk gate is
   210 GB, S14 engine roster and S15/S16 must not be fatal
