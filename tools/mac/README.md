# CLI Helper `comfy` para macOS / Linux

El script `tools/mac/comfy` permite abrir el portal `/home`, el panel `/status`, descargar el informe de diagnóstico `/report.md` al portapapeles de macOS (`pbcopy`), consultar la contraseña autogenerada y gestionar instancias en Vast.ai sin escribir comandos largos.

## Requisitos previos

1. **CLI de Vast.ai** instalada y autenticada con tu API key:
   ```bash
   pip install --upgrade vastai
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
| `comfy` / `comfy open [<id>] [home\|comfy\|status\|report\|terminal]` | Espera hasta 10 min si la instancia está arrancando y abre la vista indicada en el navegador (`COMFY_BROWSER`, `/usr/bin/open` en macOS o `xdg-open` en Linux; por defecto `/home`). |
| `comfy url [<id>]` | Imprime `http://<ip_publica>:<puerto_mapeado>` en `stdout`. |
| `comfy all` | Lista todas las instancias con su estado, GPU, precio `$/h` y URLs directas. |
| `comfy logs [<id>] [N]` | Muestra las últimas `N` líneas (por defecto `300`) de los logs del contenedor en Vast.ai. |
| `comfy pass [<id>]` | Extrae la contraseña autogenerada desde los logs de arranque si no se definió `COMFY_PASS`. |
| `comfy report [<id>] [--stdout]` | Descarga `/report.md` a `/tmp/comfy-report-<id>.md` y lo copia al portapapeles (`pbcopy`), o lo imprime por `stdout`. |
| `comfy watch [<id>]` | Monitoriza `/status/api` en vivo desde la terminal cada 3 segundos. |
| `comfy rent [all\|general\|realistic\|anime\|minimal] [--dry-run] [--yes]` | Muestra el Top 5 de ofertas RTX 5090 (o RTX 4090) con coste total estimado por hora (GPU + 120 GB de disco) y pide confirmación antes de desplegar con `--direct --args`. Requiere exportar `COMFY_IMAGE` (ej. `export COMFY_IMAGE=ghcr.io/<owner>/comfyuipod:latest`). |
| `comfy destroy <id>` | Destruye la instancia indicada tras pedir confirmación interactiva. |

