# Unreal Pipeline Skill

## Usage
Use this skill when working with the 3D/Unreal-grade output pipeline.

## Stack
- Blender 4.x headless (Cycles/Eevee, OptiX/Blackwell support)
- Hunyuan3D-2.1: image→textured GLB mesh
- TRELLIS/TripoSR: image→3D Gaussian/mesh (opt-in)
- SUPIR: 4K restoration/upscaling
- Depth Anything V2: video depth passes
- OpenUSD/glTF/FBX export (UE Datasmith compatible)
- ACES/EXR sequence rendering
- In-browser GLB preview

## Environment Variables
- `MODEL_PROFILE=unreal`
- `STUDIO_INCLUDE_3D=1`
- `STUDIO_ENABLE_TRELLIS=1` (opt-in)
- `STUDIO_INCLUDE_SUPIR=1` (opt-in)
- `STUDIO_COMFY_CUDA_DEVICE=<gpu-id>`
- `STUDIO_BLENDER_CUDA_DEVICE=<gpu-id>`

## Validation
1. `blender --version` smoke
2. Manifest check for 3D/TRELLIS/SUPIR groups
3. Template validation: `hunyuan3d_image_to_3d`, `supir_upscale_4k`
4. BlenderRunner tests with `STUDIO_STUB_BLENDER=1`
