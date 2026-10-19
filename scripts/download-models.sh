#!/usr/bin/env bash
# ==============================================================================
# download-models.sh — Descarga resiliente de modelos populares desde HuggingFace
# ==============================================================================
# Tamaños reales verificados vía cabecera x-linked-size de HuggingFace (2026-10-16):
#   --phase1 : Modelos críticos ligeros (2.79 GiB: InsightFace buffalo_l,
#              inswapper_128, CodeFormer, GFPGAN, facexlib, YOLO face, SAM, VAEs,
#              Upscalers). Se ejecuta antes de abrir ComfyUI.
#   --phase2 : Checkpoints principales, ControlNet Union ProMax e IP-Adapter
#              (60.56 GiB en perfil 'all'). Se ejecuta en segundo plano.
#
# Perfiles soportados vía variable de entorno MODEL_PROFILE (por defecto 'all'):
#   - all       : Fase 1 (2.79 GiB) + Fase 2 (60.56 GiB) = 63.35 GiB total
#   - general   : Fase 1 (2.79 GiB) + Fase 2 (24.96 GiB) = 27.75 GiB total
#   - realistic : Fase 1 (2.79 GiB) + Fase 2 (24.95 GiB) = 27.74 GiB total
#   - anime     : Fase 1 (2.79 GiB) + Fase 2 (29.43 GiB) = 32.22 GiB total
#   - minimal   : Solo Fase 1 (2.79 GiB; omite Fase 2)
#
# Variables de entorno adicionales de control de descarga (§11):
#   - HF_FETCH_TIMEOUT   : Timeout en segundos por archivo e intento (default: 3600).
#   - HF_HUB_DISABLE_XET : Si se define a "1", desactiva el motor Rust hf_xet y usa
#                          descarga HTTP clásica (útil si el host tiene HostBuffer
#                          limitado o si hf_xet consume demasiada RAM).
# ==============================================================================

set -uo pipefail

COMFY_DATA_DIR="${COMFY_DATA_DIR:-/workspace/comfyui-data}"
MODELS_DIR="${COMFY_DATA_DIR}/models"
# STAGE_DIR en el mismo filesystem que MODELS_DIR para que mv sea rename() atómico
STAGE_DIR="${MODELS_DIR}/.hf_stage"
MODEL_PROFILE="${MODEL_PROFILE:-all}"
HF_FETCH_TIMEOUT="${HF_FETCH_TIMEOUT:-3600}"

FAILED_MODELS=()

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [MODELS] $*"
}

# Validar MODEL_PROFILE (BAJA-04)
case "${MODEL_PROFILE}" in
    all|general|realistic|anime|minimal)
        ;;
    *)
        log "WARN: MODEL_PROFILE='${MODEL_PROFILE}' no reconocido; usando 'all'."
        MODEL_PROFILE="all"
        ;;
esac

# Detectar comando CLI de HuggingFace (hf en huggingface_hub >= 1.0, o huggingface-cli legacy).
# Si HF_CLI viene definido en el entorno (p. ej. el fake-hf del dry-run de CI, S-01), se respeta.
HF_CLI="${HF_CLI:-}"
if [ -z "${HF_CLI}" ]; then
    if command -v hf >/dev/null 2>&1; then
        HF_CLI="hf"
    elif command -v huggingface-cli >/dev/null 2>&1; then
        HF_CLI="huggingface-cli"
    else
        echo "[WARN] No se encontró 'hf' ni 'huggingface-cli' en el PATH. Omitiendo descargas."
        exit 0
    fi
elif ! command -v "${HF_CLI}" >/dev/null 2>&1; then
    echo "[WARN] HF_CLI='${HF_CLI}' no es ejecutable. Omitiendo descargas."
    exit 0
fi

# ------------------------------------------------------------------------------
# hf_fetch <repo_id> <remote_path> <target_dir> <local_filename> [repo_type]
# - SEC-04: No pasa HF_TOKEN por argumentos de CLI; huggingface_hub lee HF_TOKEN
#           automáticamente desde la variable de entorno.
# - REL-04: 3 intentos con backoff (2s, 5s, 10s) y registro en FAILED_MODELS.
# - §11   : Registra INICIO y aplica timeout por intento (HF_FETCH_TIMEOUT=3600s).
# ------------------------------------------------------------------------------
hf_fetch() {
    local repo_id="$1"
    local remote_path="$2"
    local target_dir="$3"
    local local_filename="$4"
    local repo_type="${5:-model}"
    local dest_file="${target_dir}/${local_filename}"

    mkdir -p "${target_dir}" "${STAGE_DIR}"

    if [ -s "${dest_file}" ]; then
        log "SKIP (ya existe): ${local_filename}"
        return 0
    fi

    log "INICIO: ${local_filename} (${repo_id}/${remote_path})"

    local attempt=1
    local max_attempts=3
    local backoff=2

    while [ "${attempt}" -le "${max_attempts}" ]; do
        log "Descargando ${local_filename} desde ${repo_id} (intento ${attempt}/${max_attempts})..."

        local dl_rc=0
        timeout "${HF_FETCH_TIMEOUT}" "${HF_CLI}" download "${repo_id}" "${remote_path}" \
            --repo-type "${repo_type}" \
            --local-dir "${STAGE_DIR}" || dl_rc=$?

        if [ "${dl_rc}" -eq 0 ] && [ -s "${STAGE_DIR}/${remote_path}" ]; then
            mv -f "${STAGE_DIR}/${remote_path}" "${dest_file}"
            log "OK: ${dest_file} ($(du -h "${dest_file}" | cut -f1))"
            return 0
        elif [ "${dl_rc}" -eq 124 ]; then
            log "TIMEOUT: La descarga de ${local_filename} (${repo_id}/${remote_path}) superó ${HF_FETCH_TIMEOUT}s (intento ${attempt}/${max_attempts})."
        fi

        rm -f "${STAGE_DIR}/${remote_path}" 2>/dev/null || true

        if [ "${attempt}" -lt "${max_attempts}" ]; then
            log "Reintentando ${local_filename} en ${backoff}s..."
            sleep "${backoff}"
            backoff=$((backoff * 2 + 1))
        fi
        attempt=$((attempt + 1))
    done

    log "ERROR: Falló la descarga de ${local_filename} (${repo_id}/${remote_path}) tras ${max_attempts} intentos."
    FAILED_MODELS+=("${local_filename} (${repo_id}/${remote_path})")
    return 1
}

print_summary() {
    local phase_name="$1"
    if [ "${#FAILED_MODELS[@]}" -eq 0 ]; then
        log "RESUMEN ${phase_name}: Todos los modelos se descargaron correctamente (0 fallos)."
    else
        log "RESUMEN ${phase_name}: ${#FAILED_MODELS[@]} modelo(s) con error de descarga:"
        for item in "${FAILED_MODELS[@]}"; do
            log "  - FALLÓ: ${item}"
        done
    fi
}

# ------------------------------------------------------------------------------
# Fase 1: Modelos críticos ligeros (2.79 GiB)
# ------------------------------------------------------------------------------
run_phase1() {
    FAILED_MODELS=()
    log "======================================================================"
    log "INICIANDO FASE 1 (2.79 GiB): ReActor, InsightFace, CodeFormer, VAE, Upscalers"
    log "PLAN FASE 1: 12 archivos"
    log "======================================================================"

    # 1. InsightFace buffalo_l (0.27 GiB — Lección 5: evita fallo de auto-descarga en Docker)
    local buffalo_dir="${MODELS_DIR}/insightface/models/buffalo_l"
    mkdir -p "${buffalo_dir}" /root/.insightface/models
    if [ ! -s "${buffalo_dir}/det_10g.onnx" ]; then
        if hf_fetch "Gourieff/ReActor" "models/buffalo_l.zip" "${STAGE_DIR}" "buffalo_l.zip" "dataset"; then
            log "Descomprimiendo buffalo_l.zip en ${buffalo_dir}..."
            mkdir -p "${STAGE_DIR}/buffalo_extract"
            unzip -q -o "${STAGE_DIR}/buffalo_l.zip" -d "${STAGE_DIR}/buffalo_extract"
            if [ -d "${STAGE_DIR}/buffalo_extract/buffalo_l" ]; then
                cp -rf "${STAGE_DIR}/buffalo_extract/buffalo_l/"* "${buffalo_dir}/"
            else
                cp -rf "${STAGE_DIR}/buffalo_extract/"* "${buffalo_dir}/"
            fi
            rm -rf "${STAGE_DIR}/buffalo_l.zip" "${STAGE_DIR}/buffalo_extract"
            log "OK: InsightFace buffalo_l listo en ${buffalo_dir}"
        fi
    else
        log "SKIP (ya existe): InsightFace buffalo_l"
    fi
    ln -sfn "${buffalo_dir}" /root/.insightface/models/buffalo_l

    # 2. ReActor FaceSwap (inswapper_128.onnx — 0.52 GiB)
    hf_fetch "Gourieff/ReActor" "models/inswapper_128.onnx" \
        "${MODELS_DIR}/insightface" "inswapper_128.onnx" "dataset" || true

    # 3. Restauración facial (CodeFormer 0.35 GiB y GFPGANv1.4 0.32 GiB)
    hf_fetch "Gourieff/ReActor" "models/facerestore_models/codeformer-v0.1.0.pth" \
        "${MODELS_DIR}/facerestore_models" "codeformer-v0.1.0.pth" "dataset" || true
    hf_fetch "Gourieff/ReActor" "models/facerestore_models/GFPGANv1.4.pth" \
        "${MODELS_DIR}/facerestore_models" "GFPGANv1.4.pth" "dataset" || true

    # 4. Pesos de detección/parsing facial de facexlib (0.18 GiB total)
    hf_fetch "kg-09/face_detection_models" "detection_Resnet50_Final.pth" \
        "${MODELS_DIR}/facedetection" "detection_Resnet50_Final.pth" "model" || true
    hf_fetch "kg-09/face_detection_models" "parsing_parsenet.pth" \
        "${MODELS_DIR}/facedetection" "parsing_parsenet.pth" "model" || true

    # 5. Ultralytics Face Detector (0.05 GiB) y Segment Anything SAM (0.35 GiB)
    hf_fetch "Gourieff/ReActor" "models/detection/bbox/face_yolov8m.pt" \
        "${MODELS_DIR}/ultralytics/bbox" "face_yolov8m.pt" "dataset" || true
    hf_fetch "Gourieff/ReActor" "models/sams/sam_vit_b_01ec64.pth" \
        "${MODELS_DIR}/sams" "sam_vit_b_01ec64.pth" "dataset" || true

    # 6. VAEs universales (SDXL FP16 Fix 0.31 GiB + SD 1.5 EMA Pruned 0.31 GiB)
    hf_fetch "madebyollin/sdxl-vae-fp16-fix" "sdxl.vae.safetensors" \
        "${MODELS_DIR}/vae" "sdxl.vae.safetensors" "model" || true
    hf_fetch "stabilityai/sd-vae-ft-mse-original" "vae-ft-mse-840000-ema-pruned.safetensors" \
        "${MODELS_DIR}/vae" "vae-ft-mse-840000-ema-pruned.safetensors" "model" || true

    # 7. Upscalers (4x-UltraSharp 0.06 GiB y 4x-AnimeSharp 0.06 GiB)
    hf_fetch "Kim2091/UltraSharp" "4x-UltraSharp.pth" \
        "${MODELS_DIR}/upscale_models" "4x-UltraSharp.pth" "model" || true
    hf_fetch "Kim2091/AnimeSharp" "4x-AnimeSharp.pth" \
        "${MODELS_DIR}/upscale_models" "4x-AnimeSharp.pth" "model" || true

    print_summary "FASE 1"
    log "======================================================================"
    log "FASE 1 COMPLETADA."
    log "======================================================================"
}

# ------------------------------------------------------------------------------
# Fase 2: Checkpoints Populares, ControlNet e IP-Adapter (60.56 GiB en 'all')
# ------------------------------------------------------------------------------
run_phase2() {
    FAILED_MODELS=()
    if [ "${MODEL_PROFILE}" = "minimal" ]; then
        log "PLAN FASE 2: 0 archivos (minimal)"
        log "MODEL_PROFILE=minimal activo: omitiendo Fase 2 de checkpoints pesados."
        return 0
    fi

    local phase2_planned=14
    case "${MODEL_PROFILE}" in
        general|realistic|anime)
            phase2_planned=10
            ;;
        *)
            phase2_planned=14
            ;;
    esac

    log "======================================================================"
    log "INICIANDO FASE 2 (Segundo Plano — Perfil: ${MODEL_PROFILE})"
    log "PLAN FASE 2: ${phase2_planned} archivos (${MODEL_PROFILE})"
    log "En perfil 'all' son 60.56 GiB. Puedes usar ComfyUI ya mismo;"
    log "pulsa 'R' en el navegador para refrescar la lista de modelos."
    log "======================================================================"

    # 1. Checkpoints Fotorrealistas / Multipropósito (perfiles: all, general, realistic)
    if [[ "${MODEL_PROFILE}" =~ ^(all|general|realistic)$ ]]; then
        # Juggernaut XI v11 (6.62 GiB)
        hf_fetch "RunDiffusion/Juggernaut-XI-v11" "Juggernaut-XI-byRunDiffusion.safetensors" \
            "${MODELS_DIR}/checkpoints" "Juggernaut-XI-byRunDiffusion.safetensors" "model" || true

        # Realistic Vision V6.0 FP16 SD 1.5 (1.99 GiB)
        hf_fetch "SG161222/Realistic_Vision_V6.0_B1_noVAE" "Realistic_Vision_V6.0_NV_B1_fp16.safetensors" \
            "${MODELS_DIR}/checkpoints" "Realistic_Vision_V6.0_NV_B1_fp16.safetensors" "model" || true
    fi

    if [[ "${MODEL_PROFILE}" =~ ^(all|realistic)$ ]]; then
        # RealVisXL V5.0 FP16 (6.46 GiB)
        hf_fetch "SG161222/RealVisXL_V5.0" "RealVisXL_V5.0_fp16.safetensors" \
            "${MODELS_DIR}/checkpoints" "RealVisXL_V5.0_fp16.safetensors" "model" || true
    fi

    # 2. Checkpoints Anime / Ilustración SDXL (perfiles: all, general, anime)
    if [[ "${MODEL_PROFILE}" =~ ^(all|general|anime)$ ]]; then
        # WAI-Illustrious v160 (6.46 GiB)
        hf_fetch "Ine007/waiIllustriousSDXL_v160" "waiIllustriousSDXL_v160.safetensors" \
            "${MODELS_DIR}/checkpoints" "waiIllustriousSDXL_v160.safetensors" "model" || true
    fi

    if [[ "${MODEL_PROFILE}" =~ ^(all|anime)$ ]]; then
        # Illustrious-XL v2.0 (6.46 GiB)
        hf_fetch "OnomaAIResearch/Illustrious-XL-v2.0" "Illustrious-XL-v2.0.safetensors" \
            "${MODELS_DIR}/checkpoints" "Illustrious-XL-v2.0.safetensors" "model" || true

        # NoobAI-XL v1.1 (6.62 GiB)
        hf_fetch "Laxhar/noobai-XL-1.1" "NoobAI-XL-v1.1.safetensors" \
            "${MODELS_DIR}/checkpoints" "NoobAI-XL-v1.1.safetensors" "model" || true
    fi

    # 3. ControlNet Union ProMax SDXL (2.34 GiB) e IP-Adapter + CLIP Vision (7.55 GiB)
    hf_fetch "xinsir/controlnet-union-sdxl-1.0" "diffusion_pytorch_model_promax.safetensors" \
        "${MODELS_DIR}/controlnet" "controlnet-union-sdxl-promax.safetensors" "model" || true

    hf_fetch "h94/IP-Adapter" "sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors" \
        "${MODELS_DIR}/ipadapter" "ip-adapter-plus_sdxl_vit-h.safetensors" "model" || true
    hf_fetch "h94/IP-Adapter" "sdxl_models/ip-adapter-plus-face_sdxl_vit-h.safetensors" \
        "${MODELS_DIR}/ipadapter" "ip-adapter-plus-face_sdxl_vit-h.safetensors" "model" || true
    hf_fetch "h94/IP-Adapter" "models/ip-adapter-plus_sd15.safetensors" \
        "${MODELS_DIR}/ipadapter" "ip-adapter-plus_sd15.safetensors" "model" || true
    hf_fetch "h94/IP-Adapter" "models/ip-adapter-plus-face_sd15.safetensors" \
        "${MODELS_DIR}/ipadapter" "ip-adapter-plus-face_sd15.safetensors" "model" || true
    hf_fetch "h94/IP-Adapter" "models/image_encoder/model.safetensors" \
        "${MODELS_DIR}/clip_vision" "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors" "model" || true
    hf_fetch "h94/IP-Adapter" "sdxl_models/image_encoder/model.safetensors" \
        "${MODELS_DIR}/clip_vision" "CLIP-ViT-bigG-14-laion2B-39B-b160k.safetensors" "model" || true

    # 4. Next-Gen SOTA: FLUX.1-dev FP8 All-in-One Checkpoint (16.06 GiB — perfil: all)
    if [ "${MODEL_PROFILE}" = "all" ]; then
        hf_fetch "Comfy-Org/flux1-dev" "flux1-dev-fp8.safetensors" \
            "${MODELS_DIR}/checkpoints" "flux1-dev-fp8.safetensors" "model" || true
    fi

    # BAJA-08: Borrar STAGE_DIR solo si no quedaron descargas incompletas por reintentar
    if [ "${#FAILED_MODELS[@]}" -eq 0 ]; then
        rm -rf "${STAGE_DIR}" 2>/dev/null || true
    fi

    print_summary "FASE 2"
    log "======================================================================"
    log "FASE 2 COMPLETADA (Perfil: ${MODEL_PROFILE})."
    log "Pulsa 'R' en la UI de ComfyUI para actualizar los desplegables."
    log "======================================================================"
}

case "${1:---all}" in
    --phase1)
        run_phase1
        ;;
    --phase2)
        # N-02: Completar cualquier modelo de Fase 1 que haya quedado pendiente si se alcanzó
        # el timeout de 600s en start.sh (idempotente: los archivos ya existentes hacen SKIP en <1ms).
        run_phase1
        run_phase2
        ;;
    --all|*)
        run_phase1
        run_phase2
        ;;
esac
