#!/usr/bin/env bash
# ==============================================================================
# start.sh — Entrypoint principal del contenedor ComfyUI "Everything-Ready"
# ==============================================================================
# 1. Verifica ffmpeg + libx264 + h264_nvenc sin SIGPIPE (Lección 1 / REL-02)
# 2. Prepara datos en COMFY_DATA_DIR (incluyendo models/configs) y enlaza /opt/ComfyUI
# 3. Configura HTTP Basic Auth en Nginx por stdin y sin exponer clave (SEC-01/02/04)
# 4. Arranca Nginx antes de Fase 1 con página de espera _starting.html (REL-03 / BAJA-07)
# 5. Ejecuta Fase 1 (con timeout no-fatal) y lanza Fase 2 con setsid en background (INF-02)
# 6. Inicia ComfyUI en 127.0.0.1:18188 (sin CORS abierto, SEC-03)
# ==============================================================================

set -euo pipefail

COMFY_APP_DIR="${COMFY_APP_DIR:-/opt/ComfyUI}"
COMFY_DATA_DIR="${COMFY_DATA_DIR:-/workspace/comfyui-data}"
export COMFY_DATA_DIR
export VHS_FORCE_FFMPEG_PATH="/usr/bin/ffmpeg"
export VHS_USE_IMAGEIO_FFMPEG="0"
export NVIDIA_DRIVER_CAPABILITIES="all"

echo "======================================================================"
echo "  ComfyUI 'Everything-Ready' Executor (RTX 5090 / 4090) — Iniciando..."
echo "======================================================================"

# ------------------------------------------------------------------------------
# 1. Verificación estricta de ffmpeg sin SIGPIPE (Lección 1 / REL-02)
# ------------------------------------------------------------------------------
echo "[1/6] Verificando instalación de ffmpeg en ${VHS_FORCE_FFMPEG_PATH}..."
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[FATAL] ffmpeg no está instalado en el contenedor. Abortando."
    exit 1
fi

ffmpeg_ver="$(ffmpeg -version 2>/dev/null || true)"
printf '%s\n' "${ffmpeg_ver}" | sed -n '1,3p'

ffmpeg_encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
if grep -q "libx264" <<<"${ffmpeg_encoders}"; then
    echo "[OK] Encoder CPU libx264 disponible."
else
    echo "[WARN] libx264 no detectado en ffmpeg -encoders."
fi

if grep -q "h264_nvenc" <<<"${ffmpeg_encoders}"; then
    echo "[OK] Encoder GPU NVIDIA h264_nvenc disponible."
else
    echo "[WARN] h264_nvenc no detectado en ffmpeg -encoders."
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    gpu_info="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null || true)"
    [ -n "${gpu_info}" ] && echo "[GPU] ${gpu_info}"
fi

# ------------------------------------------------------------------------------
# 2. Preparar estructura en COMFY_DATA_DIR y enlazar /opt/ComfyUI (CFG-01 / MED-04 / BAJA-05)
# ------------------------------------------------------------------------------
echo "[2/6] Preparando directorios de datos en ${COMFY_DATA_DIR}..."
mkdir -p \
    "${COMFY_DATA_DIR}/models/checkpoints" \
    "${COMFY_DATA_DIR}/models/configs" \
    "${COMFY_DATA_DIR}/models/diffusion_models" \
    "${COMFY_DATA_DIR}/models/unet" \
    "${COMFY_DATA_DIR}/models/vae" \
    "${COMFY_DATA_DIR}/models/loras" \
    "${COMFY_DATA_DIR}/models/clip" \
    "${COMFY_DATA_DIR}/models/text_encoders" \
    "${COMFY_DATA_DIR}/models/clip_vision" \
    "${COMFY_DATA_DIR}/models/controlnet" \
    "${COMFY_DATA_DIR}/models/upscale_models" \
    "${COMFY_DATA_DIR}/models/embeddings" \
    "${COMFY_DATA_DIR}/models/hypernetworks" \
    "${COMFY_DATA_DIR}/models/ipadapter" \
    "${COMFY_DATA_DIR}/models/insightface/models/buffalo_l" \
    "${COMFY_DATA_DIR}/models/facerestore_models" \
    "${COMFY_DATA_DIR}/models/facedetection" \
    "${COMFY_DATA_DIR}/models/ultralytics/bbox" \
    "${COMFY_DATA_DIR}/models/ultralytics/segm" \
    "${COMFY_DATA_DIR}/models/sams" \
    "${COMFY_DATA_DIR}/user/__manager" \
    "${COMFY_DATA_DIR}/user/default/ComfyUI-Manager" \
    "${COMFY_DATA_DIR}/user/default/workflows" \
    "${COMFY_DATA_DIR}/input" \
    "${COMFY_DATA_DIR}/output"

# MED-04: Restaurar archivos YAML de arquitectura en models/configs si el directorio está vacío
if [ -d /etc/comfyui/models_configs ]; then
    cp -rn /etc/comfyui/models_configs/. "${COMFY_DATA_DIR}/models/configs/" 2>/dev/null || true
fi

for subdir in models input output user; do
    if [ -d "${COMFY_APP_DIR}/${subdir}" ] && [ ! -L "${COMFY_APP_DIR}/${subdir}" ]; then
        rm -rf "${COMFY_APP_DIR:?}/${subdir}"
    fi
    ln -sfn "${COMFY_DATA_DIR}/${subdir}" "${COMFY_APP_DIR}/${subdir}"
done

if [ ! -e "/workspace/ComfyUI" ]; then
    ln -sfn "${COMFY_APP_DIR}" /workspace/ComfyUI
fi

# CFG-04: Copiar configuración de ComfyUI-Manager sin pisar ediciones en volumen persistente
if [ ! -f "${COMFY_DATA_DIR}/user/__manager/config.ini" ]; then
    cp -f /etc/comfyui/manager-config.ini "${COMFY_DATA_DIR}/user/__manager/config.ini"
fi
if [ ! -f "${COMFY_DATA_DIR}/user/default/ComfyUI-Manager/config.ini" ]; then
    cp -f /etc/comfyui/manager-config.ini "${COMFY_DATA_DIR}/user/default/ComfyUI-Manager/config.ini"
fi

# BAJA-05: Ajustar base_path dinámicamente según COMFY_DATA_DIR en extra_model_paths.yaml
sed "s#/workspace/comfyui-data/#${COMFY_DATA_DIR%/}/#g" /etc/comfyui/extra_model_paths.yaml > "${COMFY_APP_DIR}/extra_model_paths.yaml"

# ------------------------------------------------------------------------------
# 3. Configuración segura de HTTP Basic Auth en Nginx (SEC-01, SEC-02, SEC-04, BAJA-01)
# ------------------------------------------------------------------------------
echo "[3/6] Configurando autenticación básica HTTP en Nginx (puerto 8188)..."
COMFY_USER="${COMFY_USER:-admin}"
PASS_GENERATED="0"

if [ -z "${COMFY_PASS:-}" ]; then
    # BAJA-01: openssl rand -hex 12 genera 24 caracteres alfanuméricos sin tuberías ni riesgo de SIGPIPE
    COMFY_PASS="$(openssl rand -hex 12)"
    PASS_GENERATED="1"
elif [ "${#COMFY_PASS}" -lt 8 ]; then
    echo "[WARN] COMFY_PASS tiene menos de 8 caracteres. Se recomienda usar una contraseña más larga."
fi

# SEC-04: Pasar contraseña por stdin (-i), nunca como argumento de proceso visible en ps
printf '%s\n' "${COMFY_PASS}" | htpasswd -ci /etc/nginx/.htpasswd "${COMFY_USER}" >/dev/null 2>&1
chmod 640 /etc/nginx/.htpasswd
chown root:www-data /etc/nginx/.htpasswd

echo "======================================================================"
echo "  CREDENCIALES DE ACCESO WEB (HTTP Basic Auth en puerto 8188):"
echo "  Usuario    : ${COMFY_USER}"
if [ "${PASS_GENERATED}" = "1" ]; then
    echo "  Contraseña : ${COMFY_PASS}  (AUTOGENERADA para esta sesión)"
    echo "  [AVISO] No definiste COMFY_PASS; se generó esta clave única aleatoria."
else
    echo "  Contraseña : [OCULTA — definida por el usuario en COMFY_PASS]"
fi
echo "  Progreso de modelos en vivo: http://<IP_VAST>:<PUERTO>/download-status"
echo "======================================================================"

# ------------------------------------------------------------------------------
# 4. Iniciar Nginx ANTES de Fase 1 para servir /download-status y _starting.html (REL-03 / BAJA-07)
# ------------------------------------------------------------------------------
echo "[4/6] Iniciando reverse proxy Nginx en 0.0.0.0:8188..."
touch /var/log/download-models.log
chmod 644 /var/log/download-models.log
nginx -t
nginx

# ------------------------------------------------------------------------------
# 5. Ejecutar Fase 1 (con timeout no-fatal, REL-03) y lanzar Fase 2 con setsid -f (INF-02 / L-03)
# ------------------------------------------------------------------------------
if [ "${SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
    echo "[5/6] Ejecutando Fase 1 de descarga de modelos críticos (2.79 GiB)..."
    timeout 600 /usr/local/bin/download-models.sh --phase1 2>&1 | tee -a /var/log/download-models.log || \
        echo "[WARN] Fase 1 terminó con advertencias o timeout; continuando con el arranque de ComfyUI..." | tee -a /var/log/download-models.log

    echo "[5/6] Lanzando Fase 2 de descarga de checkpoints en segundo plano (Perfil: ${MODEL_PROFILE:-all})..."
    setsid -f /usr/local/bin/download-models.sh --phase2 >> /var/log/download-models.log 2>&1 < /dev/null
else
    echo "[5/6] [SKIP] SKIP_MODEL_DOWNLOAD=1 activo; omitiendo descarga automática." | tee -a /var/log/download-models.log
fi

# ------------------------------------------------------------------------------
# 6. Iniciar ComfyUI en 127.0.0.1:18188 (sin --enable-cors-header abierto, SEC-03)
# ------------------------------------------------------------------------------
echo "[6/6] Iniciando ComfyUI en 127.0.0.1:18188 (accesible vía Nginx en puerto 8188)..."
cd "${COMFY_APP_DIR}"

# shellcheck disable=SC2086
exec python3 main.py \
    --listen 127.0.0.1 \
    --port 18188 \
    --extra-model-paths-config "${COMFY_APP_DIR}/extra_model_paths.yaml" \
    ${COMFYUI_EXTRA_ARGS:-}
