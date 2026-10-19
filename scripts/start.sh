#!/usr/bin/env bash
# ==============================================================================
# start.sh — Entrypoint principal del contenedor ComfyUI "Everything-Ready"
# ==============================================================================
# 1. Inicializa logs en /var/log/{start,comfyui,supervisor,status-server,diag}.log
# 2. Muestra banner con metadatos de imagen (/etc/comfyui/image.json, §7.2)
# 3. Verifica ffmpeg + libx264 + h264_nvenc sin SIGPIPE (Lección 1 / REL-02)
# 4. Prepara datos en COMFY_DATA_DIR (incluyendo models/configs) y enlaza /opt/ComfyUI
# 5. Configura HTTP Basic Auth en Nginx por stdin y sin exponer clave (SEC-01/02/04)
# 6. Arranca Nginx + status-server.py (127.0.0.1:18189) + ttyd opcional (§5.4 / §8.1)
# 7. Ejecuta autodiagnóstico no bloqueante /usr/local/bin/diag.sh (§7.1)
# 8. Ejecuta Fase 1 (con timeout no-fatal) y lanza Fase 2 con setsid en background (INF-02)
# 9. Supervisa ComfyUI en 127.0.0.1:18188 con clasificación de salidas y reinicio (§5)
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
    /var/log/status-server.log \
    /var/log/download-models.log \
    /var/log/diag.txt
chmod 644 \
    /var/log/start.log \
    /var/log/comfyui.log \
    /var/log/supervisor.log \
    /var/log/status-server.log \
    /var/log/download-models.log \
    /var/log/diag.txt

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

# Helper para truncar logs que superen LOG_MAX_MB conservando los últimos 20 MiB (O-04)
truncate_logs_if_needed() {
    local max_bytes=$(( ${LOG_MAX_MB:-200} * 1048576 ))
    local keep_bytes=$(( 20 * 1048576 ))
    local log_file size_bytes
    for log_file in /var/log/comfyui.log /var/log/nginx/access.log; do
        if [ -f "${log_file}" ]; then
            size_bytes="$(stat -c%s "${log_file}" 2>/dev/null || echo 0)"
            if [ "${size_bytes}" -gt "${max_bytes}" ]; then
                tail -c "${keep_bytes}" "${log_file}" > "${log_file}.trunc" 2>/dev/null && \
                    cat "${log_file}.trunc" > "${log_file}" 2>/dev/null || true
                rm -f "${log_file}.trunc"
            fi
        fi
    done
}

# Helper para escribir /run/comfyui-supervisor.json de forma atómica (§5.2 / O-02)
write_supervisor_state() {
    local state="$1"
    local attempt="$2"
    local restarts="$3"
    local max_r="$4"
    local pid_val="$5"
    local started_val="$6"
    local exit_code="$7"
    local exit_reason="$8"
    local exit_at="$9"
    local healthy_resets="${10:-0}"

    python3 - "$state" "$attempt" "$restarts" "$max_r" "$pid_val" "$started_val" "$exit_code" "$exit_reason" "$exit_at" "$healthy_resets" << 'PYEOF'
import json, os, sys

state, attempt, restarts, max_r, pid_s, started_s, code_s, reason, at_s, hr_s = sys.argv[1:11]
data = {
    "state": state,
    "attempt": int(attempt),
    "restarts": int(restarts),
    "healthy_resets": int(hr_s),
    "max": int(max_r),
    "pid": int(pid_s) if pid_s and pid_s != "null" else None,
    "started_at": int(started_s) if started_s and started_s != "null" else None,
    "last_exit": (
        {"rc": int(code_s), "code": int(code_s), "reason": reason, "at": int(at_s)}
        if code_s and code_s != "null"
        else None
    ),
}
tmp_path = "/run/comfyui-supervisor.json.tmp"
final_path = "/run/comfyui-supervisor.json"
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
os.replace(tmp_path, final_path)
PYEOF
}

LAST_EXIT_CODE="null"
LAST_EXIT_REASON=""
LAST_EXIT_AT="null"
HEALTHY_RESETS=0
write_supervisor_state "starting" 0 0 "${COMFY_MAX_RESTARTS}" "null" "null" "null" "" "null" "${HEALTHY_RESETS}"

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

# ------------------------------------------------------------------------------
# 4. Iniciar Nginx, status-server.py y ttyd opcional ANTES de Fase 1 (§5.4 / §8.1)
# ------------------------------------------------------------------------------
echo "[4/7] Iniciando Nginx, status-server.py (127.0.0.1:18189) y servicios auxiliares..."
nginx -t
nginx

# shellcheck disable=SC2016
setsid -f bash -c '
    fail_times=()
    while true; do
        /usr/local/bin/status-server.py >> /var/log/status-server.log 2>&1
        rc=$?
        now=$(date +%s)
        echo "[$(date "+%Y-%m-%d %H:%M:%S")] [STATUS-SUPERVISOR] status-server.py terminó (rc=${rc})." >> /var/log/status-server.log
        kept=()
        for t in "${fail_times[@]:-}"; do
            [ -n "$t" ] && [ $((now - t)) -lt 60 ] && kept+=("$t")
        done
        kept+=("$now")
        fail_times=("${kept[@]}")
        if [ "${#fail_times[@]}" -ge 10 ]; then
            echo "[$(date "+%Y-%m-%d %H:%M:%S")] [STATUS-SUPERVISOR] CRITICAL: 10 fallos en <60s. Deteniendo reintentos." >> /var/log/status-server.log
            exit 1
        fi
        sleep 2
    done
' >/dev/null 2>&1 < /dev/null

if [ "${ENABLE_WEB_TERMINAL:-0}" = "1" ]; then
    if command -v ttyd >/dev/null 2>&1; then
        echo "[4/7] Iniciando terminal web ttyd en 127.0.0.1:7681 (/terminal/)..."
        touch /var/log/ttyd.log
        chmod 644 /var/log/ttyd.log
        setsid -f env -u COMFY_PASS ttyd --interface 127.0.0.1 --port 7681 --base-path /terminal --writable bash -l >> /var/log/ttyd.log 2>&1 < /dev/null
    else
        echo "[WARN] ENABLE_WEB_TERMINAL=1 pero el binario ttyd no está instalado."
    fi
else
    echo "[4/7] Terminal web desactivada por defecto (ENABLE_WEB_TERMINAL=0)."
fi

# ------------------------------------------------------------------------------
# 5. Ejecutar autodiagnóstico no bloqueante /usr/local/bin/diag.sh (§7.1)
#    y limpiar COMFY_PASS del entorno antes de Fase 1/2 y ComfyUI (O-05)
# ------------------------------------------------------------------------------
echo "[5/7] Ejecutando autodiagnóstico de subsistemas (/usr/local/bin/diag.sh)..."
if [ -x /usr/local/bin/diag.sh ]; then
    timeout 90 /usr/local/bin/diag.sh 2>&1 | tee /var/log/diag.txt || true
fi
unset COMFY_PASS

# ------------------------------------------------------------------------------
# 6. Ejecutar Fase 1 (con timeout no-fatal, REL-03) y lanzar Fase 2 con setsid -f (INF-02 / L-03)
# ------------------------------------------------------------------------------
if [ "${SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
    echo "[6/7] Ejecutando Fase 1 de descarga de modelos críticos (2.79 GiB)..."
    timeout 600 /usr/local/bin/download-models.sh --phase1 2>&1 | tee -a /var/log/download-models.log || \
        echo "[WARN] Fase 1 terminó con advertencias o timeout; continuando con el arranque de ComfyUI..." | tee -a /var/log/download-models.log

    echo "[6/7] Lanzando Fase 2 de descarga de checkpoints en segundo plano (Perfil: ${MODEL_PROFILE:-all})..."
    setsid -f /usr/local/bin/download-models.sh --phase2 >> /var/log/download-models.log 2>&1 < /dev/null
else
    echo "[6/7] [SKIP] SKIP_MODEL_DOWNLOAD=1 activo; omitiendo descarga automática." | tee -a /var/log/download-models.log
fi

# ------------------------------------------------------------------------------
# 7. Supervisor de ComfyUI en 127.0.0.1:18188 (§5.1, §5.2, §5.3, O-02, O-04)
# ------------------------------------------------------------------------------
echo "[7/7] Iniciando supervisor de ComfyUI en 127.0.0.1:18188 (accesible vía Nginx en puerto 8188)..."
cd "${COMFY_APP_DIR}"

SHUTTING_DOWN=0
COMFY_PID=""
SLEEP_PID=""

# Único lector en background para enviar comfyui.log al stdout del contenedor sin duplicar procesos tee
tail -n 0 -F /var/log/comfyui.log &
TAIL_PID=$!

shutdown() {
    SHUTTING_DOWN=1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUPERVISOR] Señal de apagado recibida (SIGTERM/SIGINT). Deteniendo servicios limpiamente..." | tee -a /var/log/supervisor.log
    if [ -n "${SLEEP_PID}" ] && kill -0 "${SLEEP_PID}" 2>/dev/null; then
        kill -TERM "${SLEEP_PID}" 2>/dev/null || true
    fi
    if [ -n "${COMFY_PID}" ] && kill -0 "${COMFY_PID}" 2>/dev/null; then
        kill -TERM "${COMFY_PID}" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "${COMFY_PID}" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "${COMFY_PID}" 2>/dev/null; then
            kill -KILL "${COMFY_PID}" 2>/dev/null || true
        fi
        wait "${COMFY_PID}" 2>/dev/null || true
    fi
    nginx -s quit 2>/dev/null || true
    if [ -n "${TAIL_PID:-}" ] && kill -0 "${TAIL_PID}" 2>/dev/null; then
        kill -TERM "${TAIL_PID}" 2>/dev/null || true
    fi
    exit 0
}

trap shutdown TERM INT

attempt=0
restarts=0

while [ "${SHUTTING_DOWN}" -eq 0 ]; do
    truncate_logs_if_needed
    attempt=$((attempt + 1))
    started_at="$(date +%s)"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUPERVISOR] Lanzando ComfyUI (intento #${attempt}, reinicios=${restarts}/${COMFY_MAX_RESTARTS}, healthy_resets=${HEALTHY_RESETS})..." | tee -a /var/log/supervisor.log

    # shellcheck disable=SC2086
    python3 main.py \
        --listen 127.0.0.1 \
        --port 18188 \
        --extra-model-paths-config "${COMFY_APP_DIR}/extra_model_paths.yaml" \
        ${COMFYUI_EXTRA_ARGS:-} < /dev/null >> /var/log/comfyui.log 2>&1 &
    COMFY_PID=$!

    write_supervisor_state "running" "${attempt}" "${restarts}" "${COMFY_MAX_RESTARTS}" "${COMFY_PID}" "${started_at}" "${LAST_EXIT_CODE}" "${LAST_EXIT_REASON}" "${LAST_EXIT_AT}" "${HEALTHY_RESETS}"

    rc=0
    wait "${COMFY_PID}" || rc=$?
    COMFY_PID=""

    if [ "${SHUTTING_DOWN}" -eq 1 ]; then
        break
    fi

    exit_at="$(date +%s)"
    run_duration=$((exit_at - started_at))
    if [ "${run_duration}" -ge "${COMFY_HEALTHY_UPTIME}" ] && [ "${restarts}" -gt 0 ]; then
        restarts=0
        HEALTHY_RESETS=$((HEALTHY_RESETS + 1))
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUPERVISOR] Proceso previo vivió ${run_duration}s (>= ${COMFY_HEALTHY_UPTIME}s); contador de reinicios reseteado a 0 (healthy_resets=${HEALTHY_RESETS})." | tee -a /var/log/supervisor.log
    fi

    case "${rc}" in
        137)
            reason="SIGKILL (probable OOM de RAM del host o cgroup)"
            ;;
        134|139)
            reason="Crash nativo (SIGABRT/SIGSEGV en librería C++/CUDA)"
            ;;
        1)
            reason="Excepción Python en ComfyUI (revisar traceback en comfyui.log)"
            ;;
        143)
            reason="SIGTERM recibido (reinicio manual del operador; no consume presupuesto)"
            ;;
        *)
            reason="Salida inesperada (rc=${rc})"
            ;;
    esac

    LAST_EXIT_CODE="${rc}"
    LAST_EXIT_REASON="${reason}"
    LAST_EXIT_AT="${exit_at}"

    {
        echo "======================================================================"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUPERVISOR] ComfyUI terminó (intento #${attempt}, rc=${rc}, duracion=${run_duration}s): ${reason}"
        echo "--- Estado de memoria (free -m) ---"
        free -m 2>/dev/null || true
        if command -v nvidia-smi >/dev/null 2>&1; then
            echo "--- Estado de GPU (nvidia-smi) ---"
            nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader 2>/dev/null || true
        fi
        echo "--- Últimas 40 líneas de /var/log/comfyui.log ---"
        tail -n 40 /var/log/comfyui.log 2>/dev/null || true
        echo "======================================================================"
    } | tee -a /var/log/supervisor.log

    if [ "${rc}" -ne 143 ]; then
        if [ "${restarts}" -ge "${COMFY_MAX_RESTARTS}" ]; then
            write_supervisor_state "exhausted" "${attempt}" "${restarts}" "${COMFY_MAX_RESTARTS}" "null" "null" "${LAST_EXIT_CODE}" "${LAST_EXIT_REASON}" "${LAST_EXIT_AT}" "${HEALTHY_RESETS}"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUPERVISOR] CRITICAL: Se alcanzó COMFY_MAX_RESTARTS=${COMFY_MAX_RESTARTS}. ComfyUI queda en estado 'exhausted'. Nginx, /status y /home siguen activos para diagnóstico." | tee -a /var/log/supervisor.log
            while [ "${SHUTTING_DOWN}" -eq 0 ]; do
                sleep 5 &
                SLEEP_PID=$!
                wait "${SLEEP_PID}" 2>/dev/null || true
                SLEEP_PID=""
            done
            break
        fi
        restarts=$((restarts + 1))
    fi

    write_supervisor_state "down" "${attempt}" "${restarts}" "${COMFY_MAX_RESTARTS}" "null" "null" "${LAST_EXIT_CODE}" "${LAST_EXIT_REASON}" "${LAST_EXIT_AT}" "${HEALTHY_RESETS}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUPERVISOR] Reiniciando ComfyUI en ${COMFY_RESTART_DELAY}s (reinicio ${restarts}/${COMFY_MAX_RESTARTS})..." | tee -a /var/log/supervisor.log

    sleep "${COMFY_RESTART_DELAY}" &
    SLEEP_PID=$!
    wait "${SLEEP_PID}" 2>/dev/null || true
    SLEEP_PID=""
done
