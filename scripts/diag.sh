#!/usr/bin/env bash
# ==============================================================================
# scripts/diag.sh — Diagnóstico de arranque no bloqueante (§7.1)
# Escribe resultados en stdout y en /var/log/diag.txt (cuando es invocado desde start.sh).
# Formato: [DIAG][OK|WARN|FAIL] <clave>: <detalle>
# ==============================================================================
set -uo pipefail

OK_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

emit() {
    local level="$1"
    local key="$2"
    local detail="$3"
    case "$level" in
        OK)   OK_COUNT=$((OK_COUNT + 1)) ;;
        WARN) WARN_COUNT=$((WARN_COUNT + 1)) ;;
        FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
    esac
    echo "[DIAG][${level}] ${key}: ${detail}"
}

# 1. GPU (nvidia-smi)
if command -v nvidia-smi >/dev/null 2>&1; then
    gpu_info="$(timeout 10 nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader 2>/dev/null | head -n 1 || true)"
    if [ -n "$gpu_info" ]; then
        emit "OK" "gpu" "$gpu_info"
    else
        emit "WARN" "gpu" "nvidia-smi presente pero sin GPU accesible (modo CPU o contenedor sin --gpus)"
    fi
else
    emit "WARN" "gpu" "nvidia-smi no instalado o no disponible en PATH (modo CPU)"
fi

# 2. PyTorch + CUDA + matmul 1024x1024
torch_out="$(timeout 15 python3 - << 'PYEOF' 2>&1
import time
try:
    import torch
    ver = torch.__version__
    cu = torch.version.cuda
    avail = torch.cuda.is_available()
    if avail:
        dev = torch.cuda.get_device_name(0)
        t0 = time.perf_counter()
        a = torch.randn((1024, 1024), device="cuda", dtype=torch.float16)
        b = torch.randn((1024, 1024), device="cuda", dtype=torch.float16)
        _ = torch.matmul(a, b)
        torch.cuda.synchronize()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        print(f"OK|torch={ver} cuda={cu} cuda.is_available=True device='{dev}' matmul_1024x1024={dt_ms:.2f}ms")
    else:
        print(f"WARN|torch={ver} cuda={cu} cuda.is_available=False (ejecutando en modo CPU)")
except Exception as e:
    print(f"FAIL|Error importando o probando torch: {e}")
PYEOF
)" || torch_out="FAIL|Timeout (15s) probando PyTorch"
torch_lvl="${torch_out%%|*}"
torch_msg="${torch_out#*|}"
case "$torch_lvl" in
    OK|WARN|FAIL) emit "$torch_lvl" "torch" "$torch_msg" ;;
    *)            emit "FAIL" "torch" "$torch_out" ;;
esac

# 3. ONNX Runtime (InferenceSession real con modelo Identity en memoria o det_10g.onnx)
ort_out="$(timeout 15 python3 - << 'PYEOF' 2>&1
import os
try:
    import numpy as np
    import onnxruntime as ort
    avail = ort.get_available_providers()
    model_bytes = None
    model_source = "onnx.helper Identity"
    try:
        import onnx
        from onnx import helper, TensorProto
        X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 4])
        Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 4])
        node = helper.make_node("Identity", ["X"], ["Y"])
        graph = helper.make_graph([node], "diag_identity", [X], [Y])
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7
        model_bytes = model.SerializeToString()
    except Exception:
        det_path = os.path.join(os.environ.get("COMFY_DATA_DIR", "/workspace/comfyui-data"), "models/insightface/models/buffalo_l/det_10g.onnx")
        if os.path.isfile(det_path):
            model_bytes = det_path
            model_source = "det_10g.onnx"

    if model_bytes is None:
        print(f"WARN|onnx.helper no disponible y det_10g.onnx aún no descargado; providers declarados={avail}")
    else:
        sess = ort.InferenceSession(model_bytes, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        active = sess.get_providers()
        if active and active[0] == "CUDAExecutionProvider":
            print(f"OK|InferenceSession ({model_source}) activa con CUDAExecutionProvider; ORT providers activos: {active}")
        else:
            print(f"WARN|InferenceSession ({model_source}) sin CUDA activa; ORT providers activos: {active}")
except Exception as e:
    print(f"FAIL|Error en prueba de ONNX Runtime: {e}")
PYEOF
)" || ort_out="FAIL|Timeout (15s) probando ONNX Runtime"
ort_lvl="${ort_out%%|*}"
ort_msg="${ort_out#*|}"
case "$ort_lvl" in
    OK|WARN|FAIL) emit "$ort_lvl" "ort" "$ort_msg" ;;
    *)            emit "FAIL" "ort" "$ort_out" ;;
esac

# 4. ffmpeg NVENC (h264_nvenc)
if timeout 15 ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc=duration=1:size=640x360:rate=30 -c:v h264_nvenc -f null - >/dev/null 2>&1; then
    emit "OK" "nvenc" "Codificación de prueba 640x360@30fps con h264_nvenc exitosa"
else
    emit "WARN" "nvenc" "h264_nvenc no disponible en este entorno (sin GPU o sin libnvidia-encode.so); fallback a libx264 activo"
fi

# 5. ffmpeg libx264 (CPU)
if timeout 15 ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc=duration=1:size=640x360:rate=30 -c:v libx264 -f null - >/dev/null 2>&1; then
    emit "OK" "libx264" "Codificación de prueba 640x360@30fps con libx264 exitosa"
else
    emit "FAIL" "libx264" "Codificación con libx264 falló"
fi

# 6. Nginx (sintaxis + workers como www-data)
if timeout 10 nginx -t >/dev/null 2>&1; then
    nginx_users="$(ps -o user= -C nginx 2>/dev/null | tr '\n' ' ' || true)"
    if echo "$nginx_users" | grep -q "www-data"; then
        emit "OK" "nginx" "nginx -t OK; workers activos bajo usuario www-data (${nginx_users})"
    else
        emit "WARN" "nginx" "nginx -t OK, pero no se detectó worker www-data aún (usuarios: ${nginx_users:-ninguno})"
    fi
else
    emit "FAIL" "nginx" "nginx -t devolvió error de configuración"
fi

# 7. Disco, RAM y CPU
disk_target="/workspace"
[ -d "$disk_target" ] || disk_target="/"
disk_summary="$(timeout 5 df -h "$disk_target" 2>/dev/null | awk 'NR==2 {printf "total=%s usado=%s libre=%s uso=%s (%s)", $2, $3, $4, $5, $6}' || echo "sin datos")"
ram_summary="$(timeout 5 free -m 2>/dev/null | awk '/^Mem:/ {printf "total=%sMB usado=%sMB disponible=%sMB", $2, $3, $7}' || echo "sin datos")"
cpu_cores="$(nproc 2>/dev/null || echo "?")"
emit "OK" "disk" "${disk_summary}"
emit "OK" "ram" "${ram_summary}"
emit "OK" "cpu" "nproc=${cpu_cores}"

# 8. Python imports críticos
py_out="$(timeout 15 python3 - << 'PYEOF' 2>&1
try:
    import cv2, insightface, huggingface_hub, hf_xet
    xet_ver = getattr(hf_xet, "__version__", "installed")
    print(f"OK|cv2={cv2.__version__} insightface={insightface.__version__} huggingface_hub={huggingface_hub.__version__} hf_xet={xet_ver}")
except Exception as e:
    print(f"FAIL|Error importando módulos críticos: {e}")
PYEOF
)" || py_out="FAIL|Timeout (15s) importando módulos Python"
py_lvl="${py_out%%|*}"
py_msg="${py_out#*|}"
case "$py_lvl" in
    OK|WARN|FAIL) emit "$py_lvl" "python-imports" "$py_msg" ;;
    *)            emit "FAIL" "python-imports" "$py_out" ;;
esac

# 9. ldconfig (libcudnn / libcublas)
ld_count="$(timeout 10 ldconfig -p 2>/dev/null | grep -c -E 'libcudnn|libcublas' || true)"
if [ "${ld_count:-0}" -gt 0 ]; then
    emit "OK" "ldconfig" "${ld_count} entradas de libcudnn/libcublas registradas en caché dinámica"
else
    emit "FAIL" "ldconfig" "0 entradas de libcudnn/libcublas en ldconfig -p"
fi

# 10. Variables de entorno (enmascaradas — nunca se imprimen secretos)
if [ "${COMFY_PASS_AUTOGENERATED:-0}" = "1" ]; then
    pass_state="<autogenerada>"
elif [ -n "${COMFY_PASS:-}" ]; then
    pass_state="<definida por el usuario>"
else
    pass_state="<no definida>"
fi

if [ -n "${HF_TOKEN:-}" ]; then
    hf_state="<definido>"
else
    hf_state="<no definido>"
fi

emit "OK" "env" "MODEL_PROFILE=${MODEL_PROFILE:-all} COMFY_USER=${COMFY_USER:-admin} COMFY_PASS=${pass_state} HF_TOKEN=${hf_state} COMFYUI_EXTRA_ARGS='${COMFYUI_EXTRA_ARGS:-}' HF_HUB_DISABLE_XET=${HF_HUB_DISABLE_XET:-<no definido>} ENABLE_WEB_TERMINAL=${ENABLE_WEB_TERMINAL:-0}"

echo "[DIAG] resumen: ${OK_COUNT} ok, ${WARN_COUNT} warn, ${FAIL_COUNT} fail"
exit 0
