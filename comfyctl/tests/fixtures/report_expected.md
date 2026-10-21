# Informe de Estado y Diagnóstico — ComfyUIPod
Generado: `2026-10-19T12:00:00Z` · Hostname: `vast-5090-node` · Uptime: `120s` · Instancia Vast: `C.998877` · Puerto externo 8188: `41888`
Imagen: `tag=sha-512e485` · `built_at=2026-10-19T11:00:00Z` · `comfyui_sha=4c62a84068a0` · `pytorch=2.9.1+cu128` · `cuda=12.8` · `ubuntu=24.04`

## 0. Triage automático
- Sin anomalías detectadas en los logs recientes ([DIAG] resumen: 10 ok, 0 warn, 0 fail).

## 1. Contexto operativo para el agente
- **Rutas**: código e instalación en `/opt/ComfyUI` · datos persistentes en `/workspace/comfyui-data` (`models/`, `input/`, `output/`, `user/` son symlinks desde `/opt/ComfyUI`)
- **Árbol de procesos**: `tini (PID 1)` → `comfyctl supervise` → `{nginx (root master + www-data workers), comfyctl HTTP server :18189, ttyd opcional :7681, tee -a /var/log/start.log (huérfano de start.sh), download-models.sh --phase2 (setsid), python3 main.py (supervisado, máx 5 reinicios)}`
- **Puertos**: `8188` público (Nginx Basic Auth) → `18188` ComfyUI · `18189` comfyctl status-server · `7681` ttyd (todos en `127.0.0.1` salvo `8188`)
- **Cómo ejecutar comandos**: terminal web en `/terminal/` (si `ENABLE_WEB_TERMINAL=1`) · SSH si la instancia se creó con SSH · `vastai execute <id> 'ls|du|rm …'`
- **Cómo reiniciar ComfyUI sin reiniciar la instancia**: `pkill -f 'main.py'` (envía `SIGTERM` / `rc=143`; el supervisor `comfyctl supervise` lo relanza en 5 s **sin consumir** el presupuesto `COMFY_MAX_RESTARTS`, y el contador `restarts` se resetea automáticamente a `0` tras `COMFY_HEALTHY_UPTIME=600s` de ejecución estable)
- **Cómo reinstalar la terna PyTorch CUDA 12.8**: `pip3 install --force-reinstall torch==2.9.1+cu128 torchvision==0.24.1+cu128 torchaudio==2.9.1+cu128 --index-url https://download.pytorch.org/whl/cu128`
- **Restricciones**: `PIP_CONSTRAINT=/etc/comfyui/constraints.txt` activo · no usar 'Update ComfyUI' desde Manager · ningún secreto se incluye en este informe

## 2. Diagnóstico de arranque ([DIAG])
```text
[DIAG][OK] gpu: NVIDIA GeForce RTX 5090
[DIAG] resumen: 10 ok, 0 warn, 0 fail
```

## 3. ComfyUI y supervisor
- **Estado**: `running` (supervisor: `running`) · **PID**: `1234` · **RSS**: `2048 MB` · **Uptime proceso**: `110s` · **Reinicios**: `0/5` · **Resets por uptime sano**: `0`
Sin caídas registradas en `/var/log/supervisor.log`.

## 4. GPU, memoria y disco
- **GPU**: `NVIDIA GeForce RTX 5090` · Driver `570.124.06` · VRAM `1024 / 32768 MB` · Uso `10%` · Temp `42°C`
- **Memoria RAM**: `11410` MB usados / `64340` MB totales (`52930` MB disponibles) · Swap usado: `0` MB
- **Disco (`/workspace/comfyui-data`)**: `30` GB usados / `150` GB totales (`120` GB libres)

## 5. Descargas (Fase 1 y Fase 2)
- **Fase 1**: `done` (`12/12` completados, `0` fallidos)
- **Fase 2**: `done` (`6/6` completados, `0` fallidos) · Archivo actual: `ninguno` · Proceso `hf`: PID `None` (`0` MB RSS)
Sin líneas en `/var/log/download-models.log`.

## 6. Log de ComfyUI (últimas 120 líneas)
```text
Starting server on 127.0.0.1:18188 (pass=[REDACTED])
```

## 7. Nginx
- **Estado**: `running=true` · **Workers user**: `www-data`
- **Recuento de códigos en últimas 500 peticiones**: `401=0` · `413=0` · `429=0` · `502=0` · `503=0`
Sin errores en `/var/log/nginx/error.log`.

## 8. Drift del entorno
### Diferencias `pip freeze` vs `/etc/comfyui/requirements.lock`
- Sin cambios respecto a `/etc/comfyui/requirements.lock`.
### Estado de `custom_nodes/` vs `/etc/comfyui/nodes.lock`
- Los 10 custom nodes coinciden exactamente con los SHAs de `/etc/comfyui/nodes.lock`.
### Variables de entorno relevantes (enmascaradas)
- `MODEL_PROFILE` = `anime`
- `COMFY_USER` = `admin`
- `COMFY_PASS` = `<definida por el usuario>`
- `HF_TOKEN` = `<no definido>`
- `COMFYUI_EXTRA_ARGS` = ``
- `HF_HUB_DISABLE_XET` = `<no definido>`
- `ENABLE_WEB_TERMINAL` = `0`

## 9. Modelos
- `checkpoints`: 3 ficheros (19.50 GB)
### Modelos esperados para el perfil `anime` aún ausentes
- Todos los modelos del perfil están presentes en disco.

## 10. Arranque estricto
- **Resultado**: `ok` · **Modo estricto**: `true` · **Evaluado**: `2026-10-19T11:58:00Z`
- `[OK]` **driver_cuda128** (Driver NVIDIA >= 570 (CUDA 12.8)): Driver NVIDIA 570.124.06 compatible con CUDA 12.8
