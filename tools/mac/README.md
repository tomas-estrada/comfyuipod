# `comfy` — CLI Helper para macOS / Linux (Vast.ai)

Pequeño script en Bash + Python 3 (stdlib) para controlar instancias de **ComfyUIPod** en Vast.ai desde la terminal de macOS o Linux sin copiar y pegar puertos efímeros manualmente.

---

## 1. Requisitos previos

1. Tener instalado el cliente oficial de Vast.ai y configurada tu API key:
   ```bash
   pip install --upgrade vastai
   vastai set api-key <TU_VAST_API_KEY>
   ```
2. Contar con `python3` y `curl` (incluidos por defecto en macOS y Linux).

---

## 2. Instalación

```bash
chmod +x tools/mac/comfy
mkdir -p ~/.local/bin
ln -sf "$(pwd)/tools/mac/comfy" ~/.local/bin/comfy
```

Asegúrate de que `~/.local/bin` esté en tu `PATH` (`export PATH="$HOME/.local/bin:$PATH"` en `~/.zshrc`) y define tu imagen publicada en GHCR (`COMFY_IMAGE` es obligatorio para `comfy rent`, `O-12`):

```bash
export COMFY_IMAGE="ghcr.io/<tu-usuario>/comfyuipod:latest"
# Opcional: si defines tu propia contraseña al crear instancias
# export COMFY_PASS="<tu-contraseña>"
```

---

## 3. Subcomandos disponibles

| Comando | Descripción |
|---|---|
| `comfy` / `comfy open [id] [vista]` | Espera hasta 10 min (`O-14`, configurable con `COMFY_OPEN_TIMEOUT`) a que la instancia responda en `/healthz` y abre el navegador en la vista indicada: `home` (por defecto, Portal `/home`), `comfy` (ComfyUI `/`), `status` (`/status`), `report` (`/report.md`), `terminal` (`/terminal/`) o `studio` (`/studio/`). Si detecta un fallo de arranque estricto (`strict.result == "fatal"`), avisa inmediatamente con el detalle de los checks fallidos, abre `/home` y sale con código `20` (`R-10`). |
| `comfy studio [id]` | Resuelve la URL del portal de la instancia (`/home`), verifica que `GET /studio/api/health` responda HTTP 200 con `status="ok"` y `server="ok"` (usando Basic Auth por `stdin`, `curl -K -`), imprime el resumen de salud (`server`, `comfyui`, `model_profile`, `llm_key`) y abre el navegador en `/studio/`. |
| `comfy studio report [id] [--stdout]` | Descarga `/report.md`, `/studio/api/health`, `/studio/api/status` y `/status/api`, añade la sección `## 11. Estado de Comfy Studio (/studio/api)` (salud de `studio-server` y ComfyUI, base SQLite, disco, modelos de Fase 3, custom nodes en `nodes.lock`, checks `S1..S17` y `env_masked`) y aplica redacción defensiva en cliente garantizando que `STUDIO_LLM_KEY`, `COMFY_PASS`, `HF_TOKEN` y tokens `sk-*` / `hf_*` / `Bearer *` aparezcan **siempre enmascarados**. Guarda en `/tmp/comfy-studio-report-<id>.md` (`0600`, y copia a `pbcopy` en macOS) o imprime con `--stdout`. |
| `comfy home` / `comfy status` / `comfy terminal` | Atajos directos para abrir `/home`, `/status` o `/terminal/` de la instancia activa. |
| `comfy url [id]` | Imprime la URL base `http://<ip>:<port>` de la instancia activa. |
| `comfy all` | Lista todas las instancias de tu cuenta de Vast.ai con estado, GPU, `$/h` y enlaces directos a `/home` y `/status`. |
| `comfy logs [id] [N]` | Muestra las últimas `N` líneas (por defecto `300`) de los logs del contenedor vía `vastai logs`. |
| `comfy pass [id]` | Extrae la contraseña autogenerada en el primer arranque desde los logs de la instancia. |
| `comfy report [id] [--stdout]` | Descarga `/report.md` (con redacción defensiva de secretos), lo guarda en `/tmp/comfy-report-<id>.md` (`0600`) y en macOS lo copia automáticamente al portapapeles (`pbcopy`). Con `--stdout` lo imprime en la salida estándar. |
| `comfy watch [id]` | Monitoriza `/status/api` en la terminal refrescando cada 3 segundos (GPU, VRAM, RAM, disco, progreso de adquisiciones de modelos y alertas de triaje). Si `strict.result == "fatal"`, muestra los checks fallidos con su remediación y sale con código `20` (`R-04`). |
| `comfy rent [all\|general\|realistic\|anime\|minimal\|studio] [--dry-run] [--yes]` | Busca ofertas en Vast.ai ordenadas por `$/h`, muestra una tabla comparativa con las **5 ofertas más económicas** (incluyendo VRAM, RAM del host, disco disponible, `inet_down`, `$/GB·mes` de disco y coste total estimado por hora), solicita confirmación interactiva `[y/N]` (omitible con `--yes`) y crea la instancia con `--args`. Con `--dry-run` muestra la oferta elegida y las variables que se reenviarían (con `STUDIO_LLM_KEY` y `COMFY_PASS` enmascaradas) sin crear la instancia. |
| `comfy fetch-outputs [id] [dest-dir]` | Rescata los renders de la instancia **antes** de destruirla (`M3-E`, tarea 31b): ejecuta `vastai ssh <id> "tar czf - -C /workspace/comfyui-data output \| base64"`, decodifica y extrae localmente en `dest-dir` (por defecto `~/comfy-outputs/<id>`) conservando la estructura `output/…`. Es re-ejecutable: los archivos ya presentes con el mismo tamaño se omiten (descarga resumible), y las rutas del tar que escapen del destino (`../`) se rechazan. Data dir remoto configurable con `COMFY_REMOTE_DATA_DIR`. |
| `comfy destroy <id>` | Solicita confirmación y destruye la instancia indicada (`vastai destroy instance <id>`). |

---

## 4. Flujo de Comfy Studio y ciclo seguro de `STUDIO_LLM_KEY` (Runbooks S1 / S2)

### 4.1 Filtros y requisitos de disco de `comfy rent studio`
Cuando se invoca `comfy rent studio`, el helper obtiene los filtros de [`studio/comfy_studio/hardware_profile.py`](../../studio/comfy_studio/hardware_profile.py) y el tamaño de disco de [`scripts/storage_requirement.py`](../../scripts/storage_requirement.py). El perfil por defecto apunta a una **RTX PRO 6000 Blackwell con 96 GB de VRAM y host RAM de 128 GB**; existe un perfil RAM de 256 GB. Son requisitos para seleccionar ofertas, no validación de todas las recetas.

- **GPU y host**: el helper toma los mínimos del perfil elegido (por defecto, 96 GB VRAM y 128 GB RAM; el perfil de RAM de 256 GB puede seleccionarse explícitamente). CUDA debe ser compatible con la imagen fijada.
- **Red y fiabilidad**: `inet_down>=5000` Mbps, `reliability>=0.98`, `verified=true`, `rentable=true`.
- **Disco (`disk_space`)**:
  - Se deriva del manifiesto y de los modelos de tarea, no de la suma de todo el catálogo al arrancar. Para `studio`, el mínimo actual es **231 GB**; `STUDIO_INCLUDE_ANIMATE=1` lo eleva a **250 GB**, `STUDIO_INCLUDE_3D=1` a **238 GB** y `STUDIO_INCLUDE_MINIMAX=1` a **294 GB**. Los opt-ins combinados se vuelven a calcular. `COMFY_DISK_GB` puede aumentar el tamaño, pero un override por debajo del mínimo derivado se rechaza antes de buscar ofertas o crear la instancia. Confirma siempre el valor actual con `python3 scripts/storage_requirement.py --profile studio --json`.

### 4.2 Lectura oculta de `STUDIO_LLM_KEY` y credencial aislada `STUDIO_LLM_FALLBACK_KEY` (`VAL-SERVER-005` / `VAL-CROSS-004` / `M2-SCR-004`)
Para proteger tus API keys de los proveedores LLM (primario DashScope / Qwen y fallback secundario DeepSeek):

1. El script desactiva el historial de Bash en el proceso (`HISTFILE=/dev/null`, `unset HISTFILE`, `set +o history`) y retira `STUDIO_LLM_KEY`, `STUDIO_LLM_FALLBACK_KEY`, `COMFY_PASS` y `HF_TOKEN` del entorno exportado para que ningún subproceso auxiliar (`vastai search offers`, `python3`) los herede.
2. Si `STUDIO_LLM_KEY` no fue pasada previamente en el entorno (y no estás en modo `STUDIO_STUB_LLM=1`), `comfy rent studio` la solicita mediante **`read -r -s`** (lectura silenciosa sin eco en la terminal, sin guardarla en disco ni en `~/.zsh_history` / `~/.bash_history`).
3. La llave primaria se inyecta únicamente en el flag `--env` de `vastai create instance` (`-e STUDIO_LLM_KEY="<valor>"`) junto con `-e MODEL_PROFILE="studio"` y las variables opcionales definidas en tu shell (`STUDIO_LLM_BASE_URL`, `STUDIO_LLM_MODEL`, `STUDIO_LLM_SUPPORTS_TOOLS`, `STUDIO_LLM_SUPPORTS_STREAMING`, `STUDIO_LLM_FALLBACK_BASE_URL`, `STUDIO_LLM_FALLBACK_KEY`, `STUDIO_LLM_FALLBACK_MODEL`, `STUDIO_LLM_FALLBACK_SUPPORTS_TOOLS`, `STUDIO_LLM_FALLBACK_SUPPORTS_STREAMING`, `STUDIO_INCLUDE_ANIMATE`, `STUDIO_STUB_LLM`).
4. **Aislamiento fail-closed de credenciales (`M2-SCR-004`)**: `STUDIO_LLM_KEY` se usa exclusivamente contra el proveedor primario y `STUDIO_LLM_FALLBACK_KEY` exclusivamente contra el proveedor de fallback (`STUDIO_LLM_FALLBACK_BASE_URL`). Si el primario falla y `STUDIO_LLM_FALLBACK_KEY` no está definida (o coincide con la primaria), el cliente LLM aborta **fail-closed** (`LLMProviderError`) sin reenviar jamás `STUDIO_LLM_KEY` a otro proveedor.
5. En `--dry-run`, en `/status/api`, en `/studio/api/status` y en `comfy studio report`, tanto `STUDIO_LLM_KEY` como `STUDIO_LLM_FALLBACK_KEY` se muestran siempre como `<definida>` (o `<no definida>`), nunca en texto claro.

### 4.3 Ejemplo rápido (Runbooks S1 y S2)

```bash
# 1. Verificar en seco qué oferta cumple el perfil actual (el disco se deriva):
comfy rent studio --dry-run

# 2. Alquilar la instancia Studio (pedirá STUDIO_LLM_KEY con read -s sin eco):
comfy rent studio

# 3. Verificar /studio/api/health y abrir el panel web de Comfy Studio en el navegador:
comfy studio

# 4. Generar informe diagnóstico redactado (modelos Fase 3, custom nodes, disco, S1..S16 y envs enmascaradas):
comfy studio report
# O imprimirlo directamente en stdout:
comfy studio report --stdout

# 5. Rescatar tus renders/outputs a tu Mac antes de destruir (re-ejecutable, omite ya descargados):
comfy fetch-outputs <id> ~/Downloads/comfy-renders

# 6. Al finalizar la sesión, destruir la instancia para detener el cobro de GPU y disco:
comfy destroy <id>
```

---

## 5. Seguridad de credenciales (`O-13` / `R-11` / `R-14`)

- `comfy` nunca pasa la contraseña en la línea de comandos de `curl` (`-u user:pass`), evitando que sea visible en `ps aux` para otros procesos locales. En su lugar, inyecta la credencial mediante `stdin` usando `curl -K -`.
- En `comfy rent`, las variables opcionales definidas en tu Mac (`COMFYUI_EXTRA_ARGS`, `HF_HUB_DISABLE_XET`, `ENABLE_WEB_TERMINAL`, `STUDIO_LLM_*`, `STUDIO_INCLUDE_ANIMATE`, `STUDIO_RESUME_JOBS`, `STUDIO_RESUME_DELAY_S`, `STUDIO_WEBHOOK_URL`, `STUDIO_WEBHOOK_SECRET`) se reenvían automáticamente con **solo el valor** entrecomillado (`-e CLAVE="VALOR"`). La CLI de Vast.ai (`vast.py`) solo limpia comillas del valor tras separar por `=`, nunca de la clave, por lo que `-e "CLAVE=VALOR"` crearía una variable con comilla literal en el nombre (`S-07` / V27). `comfy rent --dry-run` solo informa si la URL/secret de webhook está definida; nunca imprime sus valores.
- Dentro del contenedor, `scripts/start.sh` entrega `COMFY_PASS` a `comfyctl` mediante `/run/comfyui-pass` (`0600`); `comfyctl supervise` lo lee en memoria y elimina el fichero antes de lanzar subprocesos (`0` ocurrencias de `COMFY_PASS=` en `/proc/<pid>/environ`), mientras que las invocaciones puntuales (`comfyctl report`, `comfyctl diag`, `comfyctl strict-check`) lo leen sin borrarlo si existe (`R-11`).
