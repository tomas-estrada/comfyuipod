#!/usr/bin/env bash
# ==============================================================================
# start.sh — Entrypoint principal del contenedor ComfyUI "Everything-Ready"
# ==============================================================================
# 0. Inicializa logs en /var/log/{start,comfyui,supervisor,download-models}.log y diag.txt
# 1. Muestra banner con metadatos de imagen (/etc/comfyui/image.json, §7.2) y verifica ffmpeg
# 2. Prepara datos en COMFY_DATA_DIR (incluyendo models/configs) y enlaza /opt/ComfyUI
# 3. Configura HTTP Basic Auth en Nginx por stdin y escribe /run/comfyui-pass (0600, §9)
# 4. Arranca Nginx y ttyd opcional (127.0.0.1:7681) antes de ceder el control (§5.3)
# 5. Ejecuta exec /usr/local/bin/comfyctl supervise (servidor HTTP :18189, strict-check,
#    diag, Fase 1/2 y bucle supervisor de ComfyUI en 127.0.0.1:18188)
# ==============================================================================

set -euo pipefail

COMFY_APP_DIR="${COMFY_APP_DIR:-/opt/ComfyUI}"
COMFY_DATA_DIR="${COMFY_DATA_DIR:-/workspace/comfyui-data}"
COMFY_MAX_RESTARTS="${COMFY_MAX_RESTARTS:-5}"
COMFY_RESTART_DELAY="${COMFY_RESTART_DELAY:-5}"
COMFY_HEALTHY_UPTIME="${COMFY_HEALTHY_UPTIME:-600}"
LOG_MAX_MB="${LOG_MAX_MB:-200}"
export COMFY_APP_DIR
export COMFY_DATA_DIR
export COMFY_MAX_RESTARTS
export COMFY_RESTART_DELAY
export COMFY_HEALTHY_UPTIME
export LOG_MAX_MB
export VHS_FORCE_FFMPEG_PATH="/usr/bin/ffmpeg"
export VHS_USE_IMAGEIO_FFMPEG="0"
export NVIDIA_DRIVER_CAPABILITIES="all"

# ------------------------------------------------------------------------------
# 0. Inicializar archivos de log en /var/log y redirigir salida a start.log (§5.1 / O-10)
# ------------------------------------------------------------------------------
mkdir -p /var/log /run
date +%s > /run/comfyui-started-at
touch \
    /var/log/start.log \
    /var/log/comfyui.log \
    /var/log/supervisor.log \
    /var/log/download-models.log \
    /var/log/diag.txt \
    /var/log/studio-server.log
# Permisos: 600 para logs que contienen secretos o solo los lee comfyctl (root) —
# start.log guarda el banner con la contraseña autogenerada; supervisor.log, diag.txt,
# studio-server.log y ttyd.log solo los sirve comfyctl vía /logs/<name> con redacción de secretos.
# 644 para comfyui.log (lo inspecciona CI vía docker exec) y download-models.log
# (nginx lo lee directamente como www-data en /download-status).
chmod 600 \
    /var/log/start.log \
    /var/log/supervisor.log \
    /var/log/diag.txt \
    /var/log/studio-server.log
chmod 644 \
    /var/log/comfyui.log \
    /var/log/download-models.log

exec > >(tee -a /var/log/start.log) 2>&1

echo "======================================================================"
echo "  ComfyUI 'Everything-Ready' Executor (RTX 5090 / 4090) — Iniciando..."
if [ -f /etc/comfyui/image.json ]; then
    img_summary="$(python3 -c '
import json
try:
    d = json.load(open("/etc/comfyui/image.json", encoding="utf-8"))
    print(f"  Imagen     : tag={d.get(\"tag\",\"?\")} | built={d.get(\"built_at\",\"?\")} | comfyui={str(d.get(\"comfyui_sha\",\"?\"))[:10]}")
except Exception:
    pass
' 2>/dev/null || true)"
    [ -n "${img_summary}" ] && echo "${img_summary}"
fi
echo "======================================================================"

# Inicializar /run/comfyui-supervisor.json antes de ceder el control a comfyctl supervise
printf '{"state":"starting","attempt":0,"restarts":0,"healthy_resets":0,"max":%s,"pid":null,"started_at":null,"last_exit":null}\n' "${COMFY_MAX_RESTARTS}" > /run/comfyui-supervisor.json

# ------------------------------------------------------------------------------
# 1. Verificación estricta de ffmpeg sin SIGPIPE (Lección 1 / REL-02)
# ------------------------------------------------------------------------------
echo "[1/7] Verificando instalación de ffmpeg en ${VHS_FORCE_FFMPEG_PATH}..."
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
echo "[2/7] Preparando directorios de datos en ${COMFY_DATA_DIR}..."
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
    "${COMFY_DATA_DIR}/models/liveportrait" \
    "${COMFY_DATA_DIR}/models/whisper/faster-whisper-large-v3" \
    "${COMFY_DATA_DIR}/models/whisper/aligners" \
    "${COMFY_DATA_DIR}/models/funasr/campplus" \
    "${COMFY_DATA_DIR}/models/latentsync_checkpoints" \
    "${COMFY_DATA_DIR}/indextts/checkpoints" \
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

if [ -d "${COMFY_APP_DIR}/custom_nodes/ComfyUI-LatentSyncWrapper" ]; then
    latentsync_ckpt_dir="${COMFY_APP_DIR}/custom_nodes/ComfyUI-LatentSyncWrapper/checkpoints"
    if [ -d "${latentsync_ckpt_dir}" ] && [ ! -L "${latentsync_ckpt_dir}" ]; then
        rm -rf "${latentsync_ckpt_dir}"
    fi
    ln -sfn "${COMFY_DATA_DIR}/models/latentsync_checkpoints" "${latentsync_ckpt_dir}"
fi

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
echo "[3/7] Configurando autenticación básica HTTP en Nginx (puerto 8188)..."
COMFY_USER="${COMFY_USER:-admin}"
COMFY_PASS_AUTOGENERATED="0"

if [ -z "${COMFY_PASS:-}" ]; then
    # BAJA-01: openssl rand -hex 12 genera 24 caracteres alfanuméricos sin tuberías ni riesgo de SIGPIPE
    COMFY_PASS="$(openssl rand -hex 12)"
    COMFY_PASS_AUTOGENERATED="1"
elif [ "${#COMFY_PASS}" -lt 8 ]; then
    echo "[WARN] COMFY_PASS tiene menos de 8 caracteres. Se recomienda usar una contraseña más larga."
fi

export COMFY_USER
export COMFY_PASS
export COMFY_PASS_AUTOGENERATED

# SEC-04: Pasar contraseña por stdin (-i), nunca como argumento de proceso visible en ps
printf '%s\n' "${COMFY_PASS}" | htpasswd -ci /etc/nginx/.htpasswd "${COMFY_USER}" >/dev/null 2>&1
chmod 640 /etc/nginx/.htpasswd
chown root:www-data /etc/nginx/.htpasswd

# §9: Persistir COMFY_PASS en /run/comfyui-pass (0600) para que comfyctl lo lea y borre (unlink) al instante
(
    umask 077
    printf '%s\n' "${COMFY_PASS}" > /run/comfyui-pass
)
chmod 600 /run/comfyui-pass

echo "======================================================================"
echo "  CREDENCIALES DE ACCESO WEB (HTTP Basic Auth en puerto 8188):"
echo "  Usuario    : ${COMFY_USER}"
if [ "${COMFY_PASS_AUTOGENERATED}" = "1" ]; then
    echo "  Contraseña : ${COMFY_PASS}  (AUTOGENERADA para esta sesión)"
    echo "  [AVISO] No definiste COMFY_PASS; se generó esta clave única aleatoria."
    if [ "${ENABLE_WEB_TERMINAL:-0}" = "1" ]; then
        echo "  [WARN] ENABLE_WEB_TERMINAL=1 está activo con contraseña autogenerada."
        echo "         La terminal web da shell root autenticada por Basic Auth; se recomienda"
        echo "         definir COMFY_PASS explícitamente al usar ENABLE_WEB_TERMINAL=1."
    fi
else
    echo "  Contraseña : [OCULTA — definida por el usuario en COMFY_PASS]"
fi
echo "  Portal de inicio : http://<IP_VAST>:<PUERTO>/home"
echo "  Panel de estado  : http://<IP_VAST>:<PUERTO>/status"
echo "  Informe Markdown : http://<IP_VAST>:<PUERTO>/report.md"
echo "======================================================================"

unset COMFY_PASS

# ------------------------------------------------------------------------------
# 4. Iniciar Nginx y ttyd opcional ANTES de ceder el control a comfyctl (§5.3)
# ------------------------------------------------------------------------------
echo "[4/7] Iniciando Nginx y servicios auxiliares..."
nginx -t
nginx

if [ "${ENABLE_WEB_TERMINAL:-0}" = "1" ]; then
    if command -v ttyd >/dev/null 2>&1; then
        echo "[4/7] Iniciando terminal web ttyd en 127.0.0.1:7681 (/terminal/)..."
        touch /var/log/ttyd.log
        chmod 600 /var/log/ttyd.log
        setsid -f env -u COMFY_PASS ttyd --interface 127.0.0.1 --port 7681 --base-path /terminal --writable bash -l >> /var/log/ttyd.log 2>&1 < /dev/null
    else
        echo "[WARN] ENABLE_WEB_TERMINAL=1 pero el binario ttyd no está instalado."
    fi
else
    echo "[4/7] Terminal web desactivada por defecto (ENABLE_WEB_TERMINAL=0)."
fi

# ------------------------------------------------------------------------------
# 5. Ceder control a comfyctl supervise (servidor HTTP :18189, diag, strict-check, Fase 1/2 y supervisor)
# ------------------------------------------------------------------------------
echo "[5/7] Cediendo supervisión principal a /usr/local/bin/comfyctl supervise..."
exec /usr/local/bin/comfyctl supervise

