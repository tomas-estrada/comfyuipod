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
| `comfy` / `comfy open [<id>] [home\|comfy\|status\|report\|terminal]` | Abre la vista indicada en el navegador predeterminado (por defecto `/home`). |
| `comfy url [<id>]` | Imprime `http://<ip_publica>:<puerto_mapeado>` en `stdout`. |
| `comfy all` | Lista todas las instancias con su estado, GPU, precio `$/h` y URLs directas. |
| `comfy logs [<id>] [N]` | Muestra las últimas `N` líneas (por defecto `300`) de los logs del contenedor en Vast.ai. |
| `comfy pass [<id>]` | Extrae la contraseña autogenerada desde los logs de arranque si no se definió `COMFY_PASS`. |
| `comfy report [<id>]` | Descarga `/report.md` a `/tmp/comfy-report-<id>.md` y lo copia al portapapeles (`pbcopy`). |
| `comfy watch [<id>]` | Monitoriza `/status/api` en vivo desde la terminal cada 3 segundos. |
| `comfy rent [all\|general\|realistic\|anime\|minimal]` | Busca la mejor oferta RTX 5090 (o RTX 4090) y despliega la imagen en Vast.ai. |
| `comfy destroy <id>` | Destruye la instancia indicada tras pedir confirmación interactiva. |
