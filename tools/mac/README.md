# CLI Helper `comfy` para macOS / Linux

El script `tools/mac/comfy` permite abrir el portal `/home`, el panel `/status`, descargar el informe de diagnóstico `/report.md` al portapapeles de macOS (`pbcopy`), consultar la contraseña autogenerada y gestionar instancias en Vast.ai sin escribir comandos largos.

## Requisitos previos

1. **CLI de Vast.ai** instalada y autenticada con tu API key:
   ```bash
   pipx install vastai          # (fallback: pip3 install --user vastai)
   vastai set api-key <TU_VASTAI_API_KEY>
   ```
2. **Python 3** (incluido en macOS / Xcode Command Line Tools).

## Instalación en macOS

```bash
mkdir -p ~/.local/bin
cp tools/mac/comfy ~/.local/bin/comfy
chmod +x ~/.local/bin/comfy
```

Asegúrate de que `~/.local/bin` esté en tu `PATH` (en `~/.zshrc`):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Subcomandos disponibles

| Comando | Descripción |
|---|---|
| `comfy` / `comfy open [<id>] [home\|comfy\|status\|report\|terminal]` | Espera hasta 10 min si la instancia está arrancando y abre la vista indicada en el navegador (`COMFY_BROWSER`, `/usr/bin/open` en macOS o `xdg-open` en Linux; por defecto `/home`). Si `comfyctl strict-check` detecta un fallo `fatal`, abre `/home` de inmediato y aborta con código `20`. |
| `comfy url [<id>]` | Imprime `http://<ip_publica>:<puerto_mapeado>` en `stdout`. |
| `comfy all` | Lista todas las instancias con su estado, GPU, precio `$/h` y URLs directas. |
| `comfy logs [<id>] [N]` | Muestra las últimas `N` líneas (por defecto `300`) de los logs del contenedor en Vast.ai. |
| `comfy pass [<id>]` | Extrae la contraseña autogenerada desde los logs de arranque si no se definió `COMFY_PASS`. |
| `comfy report [<id>] [--stdout]` | Descarga `/report.md` a `/tmp/comfy-report-<id>.md` y lo copia al portapapeles (`pbcopy`), o lo imprime por `stdout` (con redacción completa de secretos garantizada por el servidor residente `comfyctl`). |
| `comfy watch [<id>]` | Monitoriza `/status/api` en vivo desde la terminal cada 3 segundos. |
| `comfy rent [all\|general\|realistic\|anime\|minimal] [--dry-run] [--yes]` | Muestra el Top 5 de ofertas RTX 5090 (o RTX 4090) con coste total estimado por hora (GPU + 120 GB de disco) y pide confirmación antes de desplegar con `--args` (runtype args: respeta el ENTRYPOINT de la imagen; los puertos van proxy-mapeados, sin `--direct`). Requiere exportar `COMFY_IMAGE` (ej. `export COMFY_IMAGE=ghcr.io/<owner>/comfyuipod:latest`) y reenvía `COMFYUI_EXTRA_ARGS`, `HF_HUB_DISABLE_XET` y `ENABLE_WEB_TERMINAL` si están definidas en la Mac. Filtra por `cuda_vers>=12.8` (campo documentado de `vastai search offers`). |
| `comfy destroy <id>` | Destruye la instancia indicada tras pedir confirmación interactiva. |

> **Nota (`S-07`, verificada para V27)**: el entrecomillado de `comfy rent` usa el contrato de vast-cli: **su parser solo limpia comillas del valor, nunca de la clave, así que se comilla SOLO el valor** (`-e COMFY_PASS="abc def"`, nunca `-e "COMFY_PASS=abc def"`). Esto permite valores con espacios (p. ej. `COMFYUI_EXTRA_ARGS="--fast --cpu"`) sin corromper la clave. Puedes verificar el resultado en `env_masked` de `/status/api` tras un alquiler real.


