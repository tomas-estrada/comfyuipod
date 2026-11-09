#!/usr/bin/env bash
# ==============================================================================
# download-models.sh — Descarga resiliente de modelos populares desde HuggingFace
# ==============================================================================
# Tamaños reales verificados vía cabecera x-linked-size de HuggingFace (2026-10-16):
#   --phase1 : Modelos críticos ligeros (2.79 GiB: InsightFace buffalo_l,
#              inswapper_128, CodeFormer, GFPGAN, facexlib, YOLO face, SAM, VAEs,
#              Upscalers). Se ejecuta antes de abrir ComfyUI.
#   --phase2 : Checkpoints principales, ControlNet Union ProMax e IP-Adapter
#              (60.56 GiB en perfil 'all'; 13.22 GiB / 8 archivos en 'studio').
#              Se ejecuta en segundo plano.
#   --phase3 : Inventario de estudio de vídeo y voz desde config/models-studio.json
#              (163.71 GB = 152.46 GiB por defecto en 79 archivos; +18.41 GB =
#              +17.15 GiB opt-in con STUDIO_INCLUDE_ANIMATE=1; +7.37 GB opt-in con
#              STUDIO_INCLUDE_3D=1 — Hunyuan3D 2.1, M3-D). Perfiles 'studio'/'unreal'.
#
# Desglose de Fase 3 por categoría (config/models-studio.json, x-linked-size):
#   - wan_core          : 14 archivos |  83,184,917,475 B ( 83.18 GB /  77.47 GiB) | Apache-2.0
#   - vace_fp8          :  2 archivos |  34,692,112,208 B ( 34.69 GB /  32.31 GiB) | Apache-2.0
#   - lora_turbo_5b     :  1 archivo  |     332,348,584 B (  0.33 GB /   0.31 GiB) | Apache-2.0
#   - latentsync        : 12 archivos |   9,970,426,719 B (  9.97 GB /   9.29 GiB) | openrail++ / MIT
#   - liveportrait      :  6 archivos |     635,366,851 B (  0.64 GB /   0.59 GiB) | MIT
#   - indextts          : 22 archivos |   5,491,182,363 B (  5.49 GB /   5.11 GiB) | ULA bilibili
#   - faster_whisper    :  5 archivos |   3,090,835,702 B (  3.09 GB /   2.88 GiB) | MIT
#   - wav2vec2_aligners :  2 archivos |   2,524,171,054 B (  2.52 GB /   2.35 GiB) | Apache-2.0
#   - clip_vit_l14      :  7 archivos |   1,712,163,490 B (  1.71 GB /   1.59 GiB) | MIT (caveats card)
#   - ipadapter_sdxl    :  3 archivos |   4,388,305,741 B (  4.39 GB /   4.09 GiB) | Apache-2.0
#   - campplus          :  3 archivos |      28,037,453 B (  0.03 GB /   0.03 GiB) | Apache-2.0
#   - TOTAL FASE 3 BASE : 77 archivos | 146,049,867,640 B (146.05 GB / 136.02 GiB)
#   - animate_opt_in    : +1 archivo  | +18,413,068,672 B (+18.41 GB / +17.15 GiB) | Apache-2.0 (STUDIO_INCLUDE_ANIMATE=1)
#   - minimax_h3_opt_in : +5 archivos | +42,030,035,159 B (+42.03 GB / +39.13 GiB) | minimax-h3-community (STUDIO_INCLUDE_MINIMAX=1)
#   - three_d_opt_in    : +1 archivo  | +7,365,943,290 B (+7.37 GB / +6.86 GiB) | Tencent Hunyuan Community License (STUDIO_INCLUDE_3D=1 o MODEL_PROFILE=unreal)
#
# Perfiles soportados vía variable de entorno MODEL_PROFILE (por defecto 'all'):
#   - all       : Fase 1 (2.79 GiB) + Fase 2 (60.56 GiB) = 63.35 GiB total
#   - general   : Fase 1 (2.79 GiB) + Fase 2 (24.96 GiB) = 27.75 GiB total
#   - realistic : Fase 1 (2.79 GiB) + Fase 2 (24.95 GiB) = 27.74 GiB total
#   - anime     : Fase 1 (2.79 GiB) + Fase 2 (29.43 GiB) = 32.22 GiB total
#   - minimal   : Solo Fase 1 (2.79 GiB; omite Fase 2)
#   - studio    : Fase 1 (2.79 GiB) + Fase 2 SDXL/ControlNet (13.22 GiB) +
#                 Fase 3 (136.02 GiB / 146.05 GB) ≈ 152.03 GiB (≈163.24 GB en disco)
#   - unreal    : 'studio' + Hunyuan3D 2.1 implícito (+7.37 GB; STUDIO_INCLUDE_3D=0
#                 renuncia) ≈ 158.89 GiB (≈170.61 GB en disco; alquiler ≥ 210 GB)
#
# Variables de entorno adicionales de control de descarga (§11):
#   - HF_FETCH_TIMEOUT       : Timeout en segundos por archivo e intento (default: 3600).
#   - HF_HUB_DISABLE_XET     : Si se define a "1", desactiva el motor Rust hf_xet y usa
#                              descarga HTTP clásica (útil si el host tiene HostBuffer
#                              limitado o si hf_xet consume demasiada RAM).
#   - STUDIO_INCLUDE_MINIMAX  : Si se define a "1", añade MiniMax H3 (Hailuo 3.0)
#                               Ref2VA pruned-int8 + Qwen3-VL nvfp4 + VAVs + LoRA turbo
#                               (+42.03 GB) para generación local con audio estéreo.
#   - STUDIO_INCLUDE_ANIMATE : Si se define a "1" en perfil 'studio', añade el modelo
#                              base wan2.2_animate_14B_int8_convrot (+18.41 GB; requiere
#                              alquiler de disco ≥ 250 GB).
#   - STUDIO_INCLUDE_3D      : Si se define a "1", añade Hunyuan3D 2.1 (+7.37 GB). El
#                              perfil 'unreal' lo activa implícitamente; definirlo a "0"
#                              en 'unreal' renuncia explícitamente al grupo 3D (M3-D).
#
# Modos de ejecución:
#   --bootstrap : lo que corre el pod al arrancar (timeout 600 en start.sh).
#   --phase1/--phase2/--phase3 : descarga explícita de cada fase.
#   --all       : por defecto (Fase 1 + Fase 2 + Fase 3 si aplica el perfil).
#   --selftest  : comprobaciones internas sin red ni descargas reales. Valida la
#                 resolución de `dest_path` del arranque (F22) contra un manifiesto
#                 sembrado y un HF_CLI simulado. Sale 0 solo si todo cuadra.
# ==============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFY_DATA_DIR="${COMFY_DATA_DIR:-/workspace/comfyui-data}"
MODELS_DIR="${COMFY_DATA_DIR}/models"
if [ -z "${COMFY_APP_DIR:-}" ]; then
    if [ -d "/opt/ComfyUI/custom_nodes" ] && [ -w "/opt/ComfyUI/custom_nodes" ]; then
        COMFY_APP_DIR="/opt/ComfyUI"
    else
        COMFY_APP_DIR="${COMFY_DATA_DIR}"
    fi
fi
# Exportados a propósito: la resolución de `dest_path` del arranque (F22) y la de
# run_phase3 tienen que ver LOS MISMOS directorios, y la primera vive en Python.
export COMFY_DATA_DIR COMFY_APP_DIR
STUDIO_MANIFEST="${STUDIO_MANIFEST:-}"
if [ -z "${STUDIO_MANIFEST}" ]; then
    if [ -f "${SCRIPT_DIR}/../config/models-studio.json" ]; then
        STUDIO_MANIFEST="${SCRIPT_DIR}/../config/models-studio.json"
    elif [ -f "/etc/comfyui/models-studio.json" ]; then
        STUDIO_MANIFEST="/etc/comfyui/models-studio.json"
    else
        STUDIO_MANIFEST="config/models-studio.json"
    fi
fi
# STAGE_DIR en el mismo filesystem que MODELS_DIR para que mv sea rename() atómico
STAGE_DIR="${MODELS_DIR}/.hf_stage"
# STAGE_DIR_PHASE3 aislado para que la limpieza de Fase 2 en el arranque normal (--phase2)
# no destruya el estado parcial/caché HF de una descarga de Fase 3 interrumpida
STAGE_DIR_PHASE3="${MODELS_DIR}/.hf_stage_phase3"
MODEL_PROFILE="${MODEL_PROFILE:-all}"
HF_FETCH_TIMEOUT="${HF_FETCH_TIMEOUT:-3600}"

FAILED_MODELS=()

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [MODELS] $*"
}

# ------------------------------------------------------------------------------
# Cortacircuitos de disco (disk circuit-breaker): la Fase 3 stinea hasta 146 GB
# en STAGE_DIR_PHASE3 sobre el MISMO volumen /workspace que studio.db, index.db,
# los spools de subida y las salidas de ComfyUI. Antes de cada descarga se exige
# espacio libre >= tamaño esperado + margen, para fallar seguro (y reintentable)
# en vez de quedarse a mitad de un rename() o de un WAL checkpoint de SQLite.
# ------------------------------------------------------------------------------
# Margen configurable: HF_DISK_MARGIN_BYTES (bytes). HF_DISK_GUARD=0 desactiva el
# cortacircuitos por completo (dry-runs de CI con HF_CLI simulado, que no escriben
# datos reales y no deben depender del espacio libre del runner).
DISK_SAFETY_MARGIN_BYTES="${HF_DISK_MARGIN_BYTES:-$((10 * 1024 * 1024 * 1024))}"  # 10 GiB por defecto
HF_DISK_GUARD="${HF_DISK_GUARD:-1}"

disk_free_bytes() {
    local path="$1"
    while [ -n "${path}" ] && [ ! -e "${path}" ]; do
        local parent
        parent="$(dirname "${path}")"
        [ "${parent}" = "${path}" ] && break
        path="${parent}"
    done
    # POSIX df -Pk reports 1024-byte blocks. Convert explicitly: callers
    # compare this value with manifest byte counts and the byte safety margin.
    df -Pk "${path}" 2>/dev/null | awk 'NR==2 {printf "%.0f\n", $4 * 1024}'
}

# require_disk_space <path> [expected_bytes] -> 0 si hay sitio, 1 si no.
require_disk_space() {
    [ "${HF_DISK_GUARD}" = "1" ] || return 0
    local path="$1"
    local expected="${2:-0}"
    local free
    free="$(disk_free_bytes "${path}")"
    if ! [[ "${free}" =~ ^[0-9]+$ ]]; then
        # M7-OPS-007: fail-closed. Sin lectura fiable del espacio libre no se puede
        # garantizar el margen; abortar es reintentable en el siguiente arranque.
        log "ERROR: no se pudo leer el espacio libre de ${path}; se aborta la descarga (fail-closed)."
        return 1
    fi
    local needed=$(( expected + DISK_SAFETY_MARGIN_BYTES ))
    if [ "${free}" -lt "${needed}" ]; then
        log "ERROR: espacio insuficiente en el volumen de ${path}: libres=${free} bytes, necesarios=${needed} bytes (descarga=${expected} + margen=${DISK_SAFETY_MARGIN_BYTES}). Se aborta esta descarga para no corromper el staging ni el estado."
        return 1
    fi
    return 0
}

# Validar MODEL_PROFILE (BAJA-04)
case "${MODEL_PROFILE}" in
    all|general|realistic|anime|minimal|studio|unreal)
        ;;
    *)
        log "WARN: MODEL_PROFILE='${MODEL_PROFILE}' no reconocido; usando 'all'."
        MODEL_PROFILE="all"
        ;;
esac

# Detectar comando CLI de HuggingFace (hf en huggingface_hub >= 1.0, o huggingface-cli legacy).
# Si HF_CLI viene definido en el entorno (p. ej. el fake-hf del dry-run de CI, S-01), se respeta.
# --selftest no descarga nada, así que puede ejecutarse en un host sin el CLI.
HF_CLI="${HF_CLI:-}"
if [ "${1:-}" = "--selftest" ]; then
    :
elif [ -z "${HF_CLI}" ]; then
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
# hf_fetch <repo_id> <remote_path> <target_dir> <local_filename> [repo_type] [expected_bytes]
# - SEC-04: No pasa HF_TOKEN por argumentos de CLI; huggingface_hub lee HF_TOKEN
#           automáticamente desde la variable de entorno.
# - REL-04: 3 intentos con backoff (2s, 5s, 10s) y registro en FAILED_MODELS.
# - §11   : Registra INICIO y aplica timeout por intento (HF_FETCH_TIMEOUT=3600s).
# - DISK  : Cortacircuitos de espacio libre antes de cada intento (expected_bytes +
#           DISK_SAFETY_MARGIN_BYTES); falla seguro en vez de quedarse a mitad.
# ------------------------------------------------------------------------------
hf_fetch() {
    local repo_id="$1"
    local remote_path="$2"
    local target_dir="$3"
    local local_filename="$4"
    local repo_type="${5:-model}"
    local expected_bytes="${6:-0}"
    local dest_file="${target_dir}/${local_filename}"

    mkdir -p "${target_dir}" "${STAGE_DIR}"

    log "INICIO: ${local_filename} (${repo_id}/${remote_path})"

    local attempt=1
    local max_attempts=3
    local backoff=2

    while [ "${attempt}" -le "${max_attempts}" ]; do
        if ! require_disk_space "${STAGE_DIR}" "${expected_bytes}"; then
            log "ERROR: ${local_filename} omitido por falta de espacio en disco (reintentable en el siguiente arranque)."
            FAILED_MODELS+=("${local_filename} (${repo_id}/${remote_path}) [sin espacio en disco]")
            return 1
        fi
        log "Descargando ${local_filename} desde ${repo_id} (intento ${attempt}/${max_attempts})..."

        local dl_rc=0
        local python_bin="${STUDIO_PYTHON:-/opt/venvs/studio/bin/python}"
        if [ ! -x "${python_bin}" ]; then
            python_bin="${PYTHON_BIN:-python3}"
        fi
        local acquire_output=""
        acquire_output="$(env \
            "HF_CLI=${HF_CLI}" "HF_FETCH_TIMEOUT=${HF_FETCH_TIMEOUT}" \
            "HF_DISK_MARGIN_BYTES=${DISK_SAFETY_MARGIN_BYTES}" \
            "HF_DISK_GUARD=${HF_DISK_GUARD}" \
            "STUDIO_CORE_MANIFEST=${STUDIO_CORE_MANIFEST:-${SCRIPT_DIR}/../config/models-core.json}" \
            "STUDIO_MANIFEST=${STUDIO_MANIFEST}" \
            "${python_bin}" -m comfy_studio.asset_acquire_cli \
                --repo "${repo_id}" --remote "${remote_path}" \
                --dest "${dest_file}" --repo-type "${repo_type}" \
                --stage "${STAGE_DIR}" --timeout "${HF_FETCH_TIMEOUT}" \
                --expected-bytes "${expected_bytes}" 2>&1)" || dl_rc=$?

        if [ "${dl_rc}" -eq 0 ]; then
            if [[ "${acquire_output}" == skipped:* ]]; then
                log "SKIP (ya existe): ${local_filename}"
            else
                log "OK: ${dest_file} ($(du -h "${dest_file}" | cut -f1))"
            fi
            return 0
        elif [ "${dl_rc}" -eq 124 ]; then
            log "TIMEOUT: La descarga de ${local_filename} (${repo_id}/${remote_path}) superó ${HF_FETCH_TIMEOUT}s (intento ${attempt}/${max_attempts})."
        elif [ -n "${acquire_output}" ]; then
            log "ERROR adquisición ${local_filename}: ${acquire_output}"
        fi

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
# F22: UNA sola regla de resolución de `dest_path` para el arranque.
#
# El manifiesto declara `dest_path` COMPLETO (target_dir + "/" + filename) y el
# prefijo indica la raíz: `models/*` vive bajo ${COMFY_DATA_DIR}/models,
# `custom_nodes/*` bajo ${COMFY_APP_DIR}, lo absoluto se respeta y
# ${COMFY_DATA_DIR}/${COMFY_APP_DIR} se expanden ANTES de partir la ruta. Es la
# misma regla que el `case` de run_phase3 y que
# studio/comfy_studio/model_assets.py::resolve_dest.
#
# Antes se partía `dest_path` por "/" y solo se quitaba el nombre de archivo
# cuando el primer componente era "models": una entrada custom_nodes/* acababa
# en <COMFY_DATA_DIR>/custom_nodes/Foo/checkpoints/p2.bin (directorio con el
# nombre del archivo dentro) y ${COMFY_DATA_DIR}/... se quedaba literal. Ambas
# se reportaban como OK: escritura silenciosa en una ruta no verificada.
#
# El programa emite por entrada bootstrap válida una línea TAB de 6 campos (los
# argumentos de hf_fetch) y, además, una línea `AVISO<TAB>motivo` cuando la
# entrada cae fuera de models/ o cuando no se puede resolver: ninguna ruta se
# resuelve en silencio. `--selftest` ejecuta este mismo programa contra un
# manifiesto sembrado, así que la regla se comprueba donde vive.
# ------------------------------------------------------------------------------
BOOTSTRAP_PLANNER_PY="$(cat <<'PYBOOTSTRAP'
import json
import os
import sys

DEFAULT_ENV = {"COMFY_DATA_DIR": "/workspace/comfyui-data", "COMFY_APP_DIR": "/opt/ComfyUI"}


def env_dir(name):
    value = (os.environ.get(name) or "").strip()
    return value or DEFAULT_ENV[name]


def resolve_dest(dest_path):
    """Resuelve un dest_path a (directorio, filename).

    (None, motivo) cuando el destino no es verificable y no se debe escribir.
    """
    raw = str(dest_path).strip()
    if not raw:
        return None, "dest_path vacio"
    for key in ("COMFY_DATA_DIR", "COMFY_APP_DIR"):
        raw = raw.replace("${" + key + "}", env_dir(key)).replace("$" + key, env_dir(key))
    raw = os.path.expanduser(raw)
    if ".." in raw.split("/"):
        return None, "dest_path con .. : " + repr(dest_path)
    if raw.startswith("/"):
        base = ""
    elif raw == "models" or raw.startswith("models/"):
        base = os.path.join(env_dir("COMFY_DATA_DIR"), "models")
        raw = raw[len("models/"):] if raw.startswith("models/") else ""
    elif raw.startswith("custom_nodes/"):
        base = env_dir("COMFY_APP_DIR")
    else:
        base = env_dir("COMFY_DATA_DIR")
    directory, filename = os.path.split(raw)
    if not filename:
        return None, "dest_path sin nombre de archivo: " + repr(dest_path)
    target = os.path.join(base, directory) if directory else base
    return os.path.normpath(target), filename


manifest = os.environ.get("BOOTSTRAP_MANIFEST") or ""
try:
    with open(manifest, encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    sys.exit(0)

for entry in data.get("models", []) or []:
    if not isinstance(entry, dict) or entry.get("class") != "bootstrap":
        continue
    repo_id = str(entry.get("repo_id") or "?")
    dest = str(entry.get("dest_path") or "").strip()
    if not dest:
        print("AVISO\tentrada bootstrap sin dest_path, se omite (" + repo_id + ")")
        continue
    target, detail = resolve_dest(dest)
    if target is None:
        print("AVISO\tentrada bootstrap sin destino verificable, se omite (" + repo_id + "): " + detail)
        continue
    if not dest.startswith("models/"):
        print("AVISO\tbootstrap fuera de models/: " + dest + " -> " + target + "/" + detail)
    print("\t".join([
        repo_id,
        str(entry.get("remote_path") or ""),
        target,
        detail,
        str(entry.get("repo_type") or "model"),
        str(entry.get("bytes") or 0),
    ]))
PYBOOTSTRAP
)"

# Plan de arranque: manifiesto -> líneas TAB de hf_fetch + líneas AVISO (F22).
bootstrap_plan() {
    local manifest="$1"
    BOOTSTRAP_MANIFEST="${manifest}" python3 -c "${BOOTSTRAP_PLANNER_PY}" 2>/dev/null || true
}

# Descargas reales del plan: las líneas AVISO no son archivos (F22).
bootstrap_plan_count() {
    printf '%s\n' "$1" | awk -F'\t' '$1 != "AVISO" && $1 != ""' | grep -c . || true
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
        if hf_fetch "Gourieff/ReActor" "models/buffalo_l.zip" "${buffalo_dir}" "buffalo_l.zip" "dataset"; then
            log "Descomprimiendo buffalo_l.zip en ${buffalo_dir}..."
            mkdir -p "${STAGE_DIR}/buffalo_extract"
            local buffalo_ok=0
            if unzip -q -o "${buffalo_dir}/buffalo_l.zip" -d "${STAGE_DIR}/buffalo_extract"; then
                if [ -d "${STAGE_DIR}/buffalo_extract/buffalo_l" ]; then
                    cp -rf "${STAGE_DIR}/buffalo_extract/buffalo_l/"* "${buffalo_dir}/" && buffalo_ok=1
                else
                    cp -rf "${STAGE_DIR}/buffalo_extract/"* "${buffalo_dir}/" && buffalo_ok=1
                fi
            fi
            rm -f "${buffalo_dir}/buffalo_l.zip"
            rm -rf "${STAGE_DIR}/buffalo_extract"
            if [ "${buffalo_ok}" -eq 1 ]; then
                log "OK: InsightFace buffalo_l listo en ${buffalo_dir}"
            else
                log "ERROR: Falló la descompresión/copia de buffalo_l.zip en ${buffalo_dir}."
                FAILED_MODELS+=("buffalo_l.zip (Gourieff/ReActor/models/buffalo_l.zip)")
            fi
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
        studio|unreal)
            phase2_planned=8
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

    # 1. Checkpoints Fotorrealistas / Multipropósito (perfiles: all, general, realistic, studio, unreal)
    if [[ "${MODEL_PROFILE}" =~ ^(all|general|realistic|studio|unreal)$ ]]; then
        # Juggernaut XI v11 (6.62 GiB)
        hf_fetch "RunDiffusion/Juggernaut-XI-v11" "Juggernaut-XI-byRunDiffusion.safetensors" \
            "${MODELS_DIR}/checkpoints" "Juggernaut-XI-byRunDiffusion.safetensors" "model" || true
    fi

    if [[ "${MODEL_PROFILE}" =~ ^(all|general|realistic)$ ]]; then
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
        hf_fetch "Comfy-Org/Flux-1-Dev" "flux1-dev-fp8.safetensors" \
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

# ------------------------------------------------------------------------------
# Fase 3: Inventario de Estudio de Vídeo y Voz (146.05 GB base / 77 archivos)
# ------------------------------------------------------------------------------
run_phase3() {
    FAILED_MODELS=()
    local STAGE_DIR="${STAGE_DIR_PHASE3}"
    if [ "${MODEL_PROFILE}" != "studio" ] && [ "${MODEL_PROFILE}" != "unreal" ]; then
        log "PLAN FASE 3: 0 archivos (${MODEL_PROFILE})"
        log "MODEL_PROFILE=${MODEL_PROFILE} activo: omitiendo Fase 3 (exclusiva de los perfiles 'studio'/'unreal')."
        return 0
    fi

    if [ ! -f "${STUDIO_MANIFEST}" ]; then
        log "ERROR: No se encontró el manifiesto de Fase 3 en ${STUDIO_MANIFEST}"
        return 1
    fi

    local include_animate="${STUDIO_INCLUDE_ANIMATE:-0}"
    local include_minimax="${STUDIO_INCLUDE_MINIMAX:-0}"
    # M3-D: el perfil 'unreal' implica el grupo 3D (Hunyuan3D 2.1) salvo renuncia explícita
    local include_3d="${STUDIO_INCLUDE_3D:-0}"
    if [ "${MODEL_PROFILE}" = "unreal" ] && [ -z "${STUDIO_INCLUDE_3D:-}" ]; then
        include_3d=1
    fi
    local phase3_planned
    # Recuento y tamaños del plan DERIVADOS del manifiesto (STUDIO_MANIFEST es el origen de verdad):
    # base = entradas sin opt-in; Animate/3D se suman solo con su flag activo. Evita el literal
    # acoplado que ya obligó a tocar el build al crecer el inventario.
    local phase3_plan_meta phase3_base phase3_sizes
    phase3_plan_meta="$(
        STUDIO_MANIFEST_PATH="${STUDIO_MANIFEST}" python3 -c '
import json, os
data = json.load(open(os.environ["STUDIO_MANIFEST_PATH"], encoding="utf-8"))
models = data.get("models") or []
meta = data.get("meta") or {}
cats = meta.get("categories") or {}
anim = (cats.get("animate_opt_in") or {}).get("bytes", 0)
three_d = (cats.get("three_d_opt_in") or {}).get("bytes", 0)
mmx = (cats.get("minimax_h3_opt_in") or {}).get("bytes", 0)
mmx_n = (cats.get("minimax_h3_opt_in") or {}).get("entries", 0)
base_bytes = meta.get("default_bytes", 0)
print(sum(1 for m in models if not m.get("opt_in")))
print(mmx_n)
print("%.2f GB base; +%.2f GB Animate opt-in; +%.2f GB 3D opt-in; +%.2f GB MiniMax-H3 opt-in" % (base_bytes / 1e9, anim / 1e9, three_d / 1e9, mmx / 1e9))
' | tr -d '\r'
    )"
    phase3_base="$(printf '%s\n' "${phase3_plan_meta}" | sed -n '1p')"
    phase3_mmx_n="$(printf '%s\n' "${phase3_plan_meta}" | sed -n '2p')"
    phase3_sizes="$(printf '%s\n' "${phase3_plan_meta}" | sed -n '3p')"
    if ! [[ "${phase3_base}" =~ ^[0-9]+$ ]]; then
        log "ERROR: No se pudo derivar el plan de Fase 3 de ${STUDIO_MANIFEST}"
        return 1
    fi
    phase3_planned="${phase3_base}"
    if [ "${include_animate}" = "1" ]; then
        phase3_planned=$((phase3_planned + 1))
    fi
    if [ "${include_3d}" = "1" ]; then
        phase3_planned=$((phase3_planned + 1))
    fi
    if [ "${include_minimax}" = "1" ]; then
        phase3_planned=$((phase3_planned + phase3_mmx_n))
    fi

    log "======================================================================"
    log "INICIANDO FASE 3 (Estudio de Vídeo — Perfil: ${MODEL_PROFILE})"
    log "PLAN FASE 3: ${phase3_planned} archivos (${MODEL_PROFILE}, STUDIO_INCLUDE_ANIMATE=${include_animate}, STUDIO_INCLUDE_3D=${include_3d}, STUDIO_INCLUDE_MINIMAX=${include_minimax})"
    log "Manifiesto: ${STUDIO_MANIFEST} (${phase3_sizes})."
    log "======================================================================"

    while IFS=$'\t' read -r repo_id remote_path target_dir filename _expected_bytes; do
        [ -z "${repo_id}" ] && continue
        local resolved_dir
        case "${target_dir}" in
            models/*)
                resolved_dir="${MODELS_DIR}/${target_dir#models/}"
                ;;
            models)
                resolved_dir="${MODELS_DIR}"
                ;;
            \$\{COMFY_DATA_DIR\}/*)
                resolved_dir="${COMFY_DATA_DIR}/${target_dir#*/}"
                ;;
            custom_nodes/*)
                resolved_dir="${COMFY_APP_DIR}/${target_dir}"
                ;;
            /*)
                resolved_dir="${target_dir}"
                ;;
            *)
                resolved_dir="${COMFY_DATA_DIR}/${target_dir}"
                ;;
        esac

        hf_fetch "${repo_id}" "${remote_path}" "${resolved_dir}" "${filename}" model "${_expected_bytes}" || true
    done < <(
        STUDIO_MANIFEST_PATH="${STUDIO_MANIFEST}" STUDIO_INCLUDE_ANIMATE_FLAG="${include_animate}" STUDIO_INCLUDE_3D_FLAG="${include_3d}" STUDIO_INCLUDE_MINIMAX_FLAG="${include_minimax}" \
        python3 -c '
import json, os
data = json.load(open(os.environ["STUDIO_MANIFEST_PATH"], encoding="utf-8"))
inc_anim = os.environ.get("STUDIO_INCLUDE_ANIMATE_FLAG", "0") == "1"
inc_3d = os.environ.get("STUDIO_INCLUDE_3D_FLAG", "0") == "1"
inc_mmx = os.environ.get("STUDIO_INCLUDE_MINIMAX_FLAG", "0") == "1"
for m in data["models"]:
    opt = m.get("opt_in")
    if opt == "STUDIO_INCLUDE_ANIMATE" and not inc_anim:
        continue
    if opt == "STUDIO_INCLUDE_3D" and not inc_3d:
        continue
    if opt == "STUDIO_INCLUDE_MINIMAX" and not inc_mmx:
        continue
    print("\t".join([m["repo_id"], m["remote_path"], m["target_dir"], m["filename"], str(m["bytes"])]))
'
    )

    if [ "${#FAILED_MODELS[@]}" -eq 0 ]; then
        rm -rf "${STAGE_DIR}" 2>/dev/null || true
    fi

    print_summary "FASE 3"
    log "======================================================================"
    log "FASE 3 COMPLETADA (Perfil: ${MODEL_PROFILE})."
    log "======================================================================"
}

run_bootstrap() {
    FAILED_MODELS=()
    # Arranque: SOLO lo que el manifiesto declara bootstrap genuino.
    #
    # La decisión de qué es bootstrap está versionada en el manifiesto, no en
    # este script. Hoy el conjunto está vacío a propósito: ni un VAE ni un
    # restaurador facial son requisito para abrir la API y descubrir
    # capacidades (política, punto 4). Un modelo task/optional no se descarga
    # sin petición explícita de capacidad.
    local manifest="${STUDIO_CORE_MANIFEST:-/etc/comfyui/models-core.json}"
    if [ ! -f "${manifest}" ]; then
        manifest="${SCRIPT_DIR}/../config/models-core.json"
    fi

    log "======================================================================"
    log "INICIANDO ARRANQUE (solo bootstrap imprescindible)"
    log "Manifiesto: ${manifest}"
    log "======================================================================"

    # El manifiesto emite exactamente los argumentos de hf_fetch. Si no se puede
    # leer, no se descarga nada: fallar cerrado es mejor que llenar el disco con
    # el catálogo completo por una ruta mal resuelta.
    #
    # F22: la resolución de `dest_path` vive en BOOTSTRAP_PLANNER_PY (una sola
    # regla, compartida con --selftest) y emite además líneas AVISO para las
    # entradas que caen fuera de models/ o que no se pueden resolver.
    local plan
    plan="$(bootstrap_plan "${manifest}")"

    local planned=0 bytes_total=0
    if [ -n "${plan}" ]; then
        planned="$(bootstrap_plan_count "${plan}")"
    fi
    log "PLAN ARRANQUE: ${planned} archivos bootstrap."

    if [ "${planned}" = "0" ]; then
        log "Nada que descargar para abrir la API: el arranque no arrastra modelos."
        log "Las capacidades se precargan a petición (dependencias y bytes visibles)."
        log "======================================================================"
        log "FASE ARRANQUE COMPLETADA."
        log "======================================================================"
        print_summary "ARRANQUE"
        return 0
    fi

    local repo remote target fname repo_type expected extra
    local -a fetch_args
    while IFS=$'\t' read -r repo remote target fname repo_type expected extra; do
        [ -n "${repo}" ] || continue
        if [ "${repo}" = "AVISO" ]; then
            # F22: el aviso viaja en el propio plan para que salga en el log
            # aunque `dest_path` caiga fuera de models/ o no sea resoluble.
            log "AVISO: ${remote}"
            continue
        fi
        if [ -n "${extra}" ]; then
            # Una línea con campos de más es un manifiesto corrupto: mejor no
            # descargar nada de ella que adivinar qué campo es cada cosa.
            log "AVISO: línea de manifiesto con campos extra, se ignora (${repo})."
            continue
        fi
        bytes_total=$((bytes_total + ${expected:-0}))
        # Se pasa por array a propósito: la paridad O-11 de CI extrae los
        # nombres de activo de las llamadas literales `hf_fetch "..." "..." ...`
        # y una llamada con variables se colaría en esa lista como si fuera un
        # modelo más.
        fetch_args=("${repo}" "${remote}" "${target}" "${fname}" "${repo_type}" "${expected:-0}")
        hf_fetch "${fetch_args[@]}" || true
    done <<< "${plan}"

    log "======================================================================"
    log "FASE ARRANQUE COMPLETADA (${planned} archivos bootstrap)."
    log "======================================================================"
    print_summary "ARRANQUE"
}

# ------------------------------------------------------------------------------
# --selftest : comprobaciones internas. Sin red y sin descargas reales (HF_CLI
# simulado); valida la regla de resolución de `dest_path` del arranque (F22).
#
# Siembra un manifiesto bootstrap con los cuatro prefijos que puede declarar el
# manifiesto (models/*, custom_nodes/*, ${COMFY_DATA_DIR}/${COMFY_APP_DIR} y
# absoluto) más las entradas que deben omitirse, y comprueba:
#   1. que BOOTSTRAP_PLANNER_PY resuelve los cuatro con LA MISMA regla y que el
#      nombre del archivo nunca queda colgado como directorio;
#   2. que las líneas AVISO no cuentan como descargas en el plan;
#   3. que un `--bootstrap` real contra ese manifiesto deja cada archivo en su
#      sitio (y NO en <ruta>/<archivo>/<archivo>) sin avisos silenciosos.
# ------------------------------------------------------------------------------
selftest_bootstrap_dest() {
    local tmp data app manifest rc=0
    tmp="$(mktemp -d)" || {
        log "ERROR (selftest): no se pudo crear el directorio temporal."
        return 1
    }
    data="${tmp}/data"
    app="${tmp}/app"
    manifest="${tmp}/models-core.json"
    mkdir -p "${data}" "${app}"

    # ${COMFY_DATA_DIR}/${COMFY_APP_DIR} se escriben escapados para que el
    # manifiesto los contenga literales (es lo que declaran los manifiestos reales).
    cat > "${manifest}" <<JSON
{
 "version": 1,
 "models": [
  {"class": "bootstrap", "repo_id": "org/models-vae", "remote_path": "p1.bin", "dest_path": "models/vae/p1.bin", "repo_type": "model", "bytes": 16, "revision": "0123456789abcdef0123456789abcdef01234567", "sha256": "1f47fea1a5a0d11f806ba37a292b6f1e7694c75b391843f2852f65a5b43ef64a"},
  {"class": "bootstrap", "repo_id": "org/models-root", "remote_path": "p5.bin", "dest_path": "models/p5.bin", "repo_type": "model", "bytes": 16, "revision": "0123456789abcdef0123456789abcdef01234567", "sha256": "1f47fea1a5a0d11f806ba37a292b6f1e7694c75b391843f2852f65a5b43ef64a"},
  {"class": "bootstrap", "repo_id": "org/custom", "remote_path": "p2.bin", "dest_path": "custom_nodes/Foo/checkpoints/p2.bin", "repo_type": "model", "bytes": 16, "revision": "0123456789abcdef0123456789abcdef01234567", "sha256": "1f47fea1a5a0d11f806ba37a292b6f1e7694c75b391843f2852f65a5b43ef64a"},
  {"class": "bootstrap", "repo_id": "org/data-var", "remote_path": "p3.bin", "dest_path": "\${COMFY_DATA_DIR}/indextts/checkpoints/p3.bin", "repo_type": "model", "bytes": 16, "revision": "0123456789abcdef0123456789abcdef01234567", "sha256": "1f47fea1a5a0d11f806ba37a292b6f1e7694c75b391843f2852f65a5b43ef64a"},
  {"class": "bootstrap", "repo_id": "org/app-var", "remote_path": "p6.bin", "dest_path": "\${COMFY_APP_DIR}/custom_nodes/Bar/x/p6.bin", "repo_type": "model", "bytes": 16, "revision": "0123456789abcdef0123456789abcdef01234567", "sha256": "1f47fea1a5a0d11f806ba37a292b6f1e7694c75b391843f2852f65a5b43ef64a"},
  {"class": "bootstrap", "repo_id": "org/abs", "remote_path": "p4.bin", "dest_path": "${tmp}/extra/p4.bin", "repo_type": "model", "bytes": 16, "revision": "0123456789abcdef0123456789abcdef01234567", "sha256": "1f47fea1a5a0d11f806ba37a292b6f1e7694c75b391843f2852f65a5b43ef64a"},
  {"class": "bootstrap", "repo_id": "org/broken", "remote_path": "p7.bin", "dest_path": "models", "repo_type": "model", "bytes": 16},
  {"class": "bootstrap", "repo_id": "org/escape", "remote_path": "p8.bin", "dest_path": "models/../../etc/p8.bin", "repo_type": "model", "bytes": 17},
  {"class": "bootstrap", "repo_id": "org/nodest", "remote_path": "p9.bin", "repo_type": "model", "bytes": 18},
  {"class": "task", "repo_id": "org/not-bootstrap", "remote_path": "x.bin", "dest_path": "models/task/x.bin", "repo_type": "model", "bytes": 99}
 ]
}
JSON

    local got want planned
    # Mismo programa que usa el arranque (bootstrap_plan), con los directorios de
    # la siembra y sin tocar las variables del script.
    got="$(env COMFY_DATA_DIR="${data}" COMFY_APP_DIR="${app}" BOOTSTRAP_MANIFEST="${manifest}" \
        python3 -c "${BOOTSTRAP_PLANNER_PY}" 2>/dev/null || true)"
    want="$(cat <<EOF
org/models-vae	p1.bin	${data}/models/vae	p1.bin	model	16
org/models-root	p5.bin	${data}/models	p5.bin	model	16
AVISO	bootstrap fuera de models/: custom_nodes/Foo/checkpoints/p2.bin -> ${app}/custom_nodes/Foo/checkpoints/p2.bin
org/custom	p2.bin	${app}/custom_nodes/Foo/checkpoints	p2.bin	model	16
AVISO	bootstrap fuera de models/: \${COMFY_DATA_DIR}/indextts/checkpoints/p3.bin -> ${data}/indextts/checkpoints/p3.bin
org/data-var	p3.bin	${data}/indextts/checkpoints	p3.bin	model	16
AVISO	bootstrap fuera de models/: \${COMFY_APP_DIR}/custom_nodes/Bar/x/p6.bin -> ${app}/custom_nodes/Bar/x/p6.bin
org/app-var	p6.bin	${app}/custom_nodes/Bar/x	p6.bin	model	16
AVISO	bootstrap fuera de models/: ${tmp}/extra/p4.bin -> ${tmp}/extra/p4.bin
org/abs	p4.bin	${tmp}/extra	p4.bin	model	16
AVISO	entrada bootstrap sin destino verificable, se omite (org/broken): dest_path sin nombre de archivo: 'models'
AVISO	entrada bootstrap sin destino verificable, se omite (org/escape): dest_path con .. : 'models/../../etc/p8.bin'
AVISO	entrada bootstrap sin dest_path, se omite (org/nodest)
EOF
)"
    if [ "${got}" != "${want}" ]; then
        log "ERROR (selftest/F22): BOOTSTRAP_PLANNER_PY no resuelve dest_path con la regla compartida."
        diff <(printf '%s\n' "${want}") <(printf '%s\n' "${got}") || true
        rc=1
    fi

    planned="$(bootstrap_plan_count "${got}")"
    if [ "${planned}" != "6" ]; then
        log "ERROR (selftest/F22): el plan debe tener 6 descargas (las líneas AVISO no cuentan); hay ${planned}."
        rc=1
    fi

    local bin bootstrap_log f
    bin="${tmp}/bin"
    mkdir -p "${bin}"
    cat > "${bin}/fake-hf" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${HF_FIXTURE_ARGS}"
repo_path="${3:?}"
stage_dir=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--local-dir" ]; then
        stage_dir="$2"
        shift 2
        continue
    fi
    shift
done
[ -n "${stage_dir}" ] || { echo "fake-hf: falta --local-dir" >&2; exit 1; }
mkdir -p "$(dirname "${stage_dir}/${repo_path}")"
printf 'fake-model-bytes' > "${stage_dir}/${repo_path}"
FAKE
    chmod +x "${bin}/fake-hf"

    # hf_fetch usa `timeout N <cmd>` (GNU coreutils), que no existe en macOS. El
    # fake-hf es instantáneo, así que el envoltorio solo descarta los segundos.
    cat > "${bin}/timeout" <<'SHIM'
#!/usr/bin/env bash
set -euo pipefail
[ "$#" -gt 0 ] && shift
exec "$@"
SHIM
    chmod +x "${bin}/timeout"

    bootstrap_log="${tmp}/bootstrap.log"
    if ! COMFY_DATA_DIR="${data}" COMFY_APP_DIR="${app}" STUDIO_CORE_MANIFEST="${manifest}" \
        HF_FIXTURE_ARGS="${tmp}/hf-args.log" \
        HF_CLI="${bin}/fake-hf" HF_DISK_GUARD=0 MODEL_PROFILE=minimal \
        PATH="${bin}:${PATH}" \
        bash "${BASH_SOURCE[0]}" --bootstrap > "${bootstrap_log}" 2>&1; then
        log "ERROR (selftest/F22): --bootstrap falló contra el manifiesto sembrado."
        rc=1
    fi

    for f in \
        "${data}/models/vae/p1.bin" \
        "${data}/models/p5.bin" \
        "${app}/custom_nodes/Foo/checkpoints/p2.bin" \
        "${data}/indextts/checkpoints/p3.bin" \
        "${app}/custom_nodes/Bar/x/p6.bin" \
        "${tmp}/extra/p4.bin"; do
        if [ ! -s "${f}" ]; then
            log "ERROR (selftest/F22): --bootstrap no dejó el archivo en ${f}."
            rc=1
        fi
    done

    if ! grep -q -- '--revision 0123456789abcdef0123456789abcdef01234567' "${tmp}/hf-args.log"; then
        log "ERROR (selftest/A01): el camino legacy no pasó la revisión fijada del manifiesto al CLI."
        rc=1
    fi

    for f in \
        "${data}/models/vae/p1.bin/p1.bin" \
        "${app}/custom_nodes/Foo/checkpoints/p2.bin/p2.bin" \
        "${data}/indextts/checkpoints/p3.bin/p3.bin"; do
        if [ -e "${f}" ]; then
            log "ERROR (selftest/F22): regresión F22: se creó el directorio anidado ${f}."
            rc=1
        fi
    done

    if grep -F "INICIO: " "${bootstrap_log}" | grep -qF "\${COMFY_DATA_DIR}"; then
        log "ERROR (selftest/F22): \${COMFY_DATA_DIR} quedó sin expandir en la ruta descargada."
        rc=1
    fi
    if ! grep -qF "AVISO: bootstrap fuera de models/: custom_nodes/Foo/checkpoints/p2.bin" "${bootstrap_log}"; then
        log "ERROR (selftest/F22): falta el AVISO explícito para la entrada fuera de models/."
        rc=1
    fi
    if ! grep -qF "PLAN ARRANQUE: 6 archivos bootstrap." "${bootstrap_log}"; then
        log "ERROR (selftest/F22): el plan de arranque sembrado no contó 6 archivos."
        rc=1
    fi
    if ! grep -qF "RESUMEN ARRANQUE: Todos los modelos se descargaron correctamente (0 fallos)." "${bootstrap_log}"; then
        log "ERROR (selftest/F22): el arranque sembrado no terminó limpio."
        tail -n 25 "${bootstrap_log}" || true
        rc=1
    fi

    rm -rf "${tmp}"
    if [ "${rc}" = "0" ]; then
        log "SELFTEST OK: resolución de dest_path del arranque (F22) verificada."
    fi
    return "${rc}"
}

selftest_disk_free_bytes() {
    local tmp fake_df got expected seen
    tmp="$(mktemp -d)" || return 1
    fake_df="${tmp}/df"
    seen="${tmp}/df-path"
    cat > "${fake_df}" <<'DF'
#!/usr/bin/env bash
set -eu
printf '%s\n' "${@: -1}" > "${DF_PATH_RECORD}"
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
printf '/dev/mock 300000 41000 259000 14%% %s\n' "${@: -1}"
DF
    chmod +x "${fake_df}"
    expected=$((259000 * 1024))
    got="$(PATH="${tmp}:${PATH}" DF_PATH_RECORD="${seen}" disk_free_bytes "${tmp}/not-yet/created")"
    if [ "${got}" != "${expected}" ]; then
        log "ERROR (selftest/disk): df -Pk KiB no se convirtió a bytes (got=${got}, expected=${expected})."
        rm -rf "${tmp}"
        return 1
    fi
    if [ "$(cat "${seen}")" != "${tmp}" ]; then
        log "ERROR (selftest/disk): el path inexistente no usó su padre existente."
        rm -rf "${tmp}"
        return 1
    fi
    rm -rf "${tmp}"
    log "SELFTEST OK: conversión df -Pk a bytes y resolución de padre existente."
}

case "${1:---all}" in
    --selftest)
        # Comprobaciones internas sin red: válvula de escape para la regla de
        # resolución de dest_path del arranque (F22).
        selftest_disk_free_bytes && selftest_bootstrap_dest
        ;;
    --bootstrap)
        # Arranque normal del pod: solo bootstrap. Nada de task/optional sin
        # petición, y nada de Fase 2/3 en segundo plano.
        run_bootstrap
        ;;
    --phase1)
        # Se mantiene por compatibilidad para operadores que quieran el paquete
        # histórico de Fase 1 explícitamente. El arranque NO lo usa.
        run_phase1
        ;;
    --phase2)
        # N-02: Completar cualquier modelo de Fase 1 que haya quedado pendiente si se alcanzó
        # el timeout de 600s en start.sh (idempotente: los archivos ya existentes hacen SKIP en <1ms).
        run_phase1
        run_phase2
        if [ "${MODEL_PROFILE}" = "studio" ] || [ "${MODEL_PROFILE}" = "unreal" ]; then
            run_phase3
        fi
        ;;
    --phase3)
        run_phase3
        ;;
    --all|*)
        run_phase1
        run_phase2
        if [ "${MODEL_PROFILE}" = "studio" ] || [ "${MODEL_PROFILE}" = "unreal" ]; then
            run_phase3
        fi
        ;;
esac
