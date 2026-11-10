# Migración: CUDA 13.0 + PyTorch 2.15

> **Estado: NO IMPLEMENTADA.** Documento de ejecución. Requiere un entorno con
> Docker + GPU NVIDIA para validarse; este cambio no se puede verificar sin
> construir la imagen completa (descarga multi-GB) ni sin GPU para comprobar
> `sm_120`. La rama `upgrade/cuda-13-torch-2.15` existe vacía a propósito.

## Por qué

- `torch==2.9.1+cu128` fija CUDA 12.8. La rama `cu130` de PyTorch es la que
  soporta de forma nativa las GPUs Blackwell más nuevas y recoge las
  correcciones de rendimiento de `sm_120`.
- El resto del stack (onnxruntime-gpu 1.31.1, opencv 4.11) ya está alineado con
  CUDA 12.8; hay que confirmar que sus ruedas `cu130` equivalentes existen.

## Superficie de pines — TODO debe cambiar junto

Si se cambia solo una, el build falla o (peor) instala una build CPU en silencio.

| # | Fichero | Línea(s) | Qué contiene hoy |
|---|---|---|---|
| 1 | `config/constraints.txt` | 10-12 | `torch/torchvision/torchaudio ==2.9.1+cu128` |
| 2 | `Dockerfile` | 111-114 | `pip3 install torch==…+cu128 --index-url …/cu128` |
| 3 | `Dockerfile` | 413 | `printf 'torch==…+cu128…' > /tmp/ns-constraints.txt` |
| 4 | `Dockerfile` | 433 | ídem `/tmp/brain-constraints.txt` |
| 5 | `Dockerfile` | 448 | ídem `/tmp/kokoro-constraints.txt` |
| 6 | `Dockerfile` | 463 | ídem `/tmp/sam3-constraints.txt` |
| 7 | `scripts/triage_patterns.json` | 24 | comando de remediación del patrón de triaje |
| 8 | `comfyctl/tests/fixtures/report_expected.md` | 3, 14 | snapshot con `pytorch=2.9.1+cu128` y `cuda=12.8` |
| 9 | `Dockerfile` | 278, 335-336, 344, 401, 446 | comentarios que citan la versión |

**El sufijo `+cuXXX` es un guardrail, no una decoración** (ver la cabecera de
`config/constraints.txt`): no existe en PyPI, así que un `pip install` en runtime
que ignore las constraints **falla ruidosamente** en vez de instalar torch CPU
sin que nadie lo note. No quitarlo al migrar.

## Procedimiento

1. **Confirmar que la terna objetivo existe** antes de tocar nada:
   ```bash
   pip3 index versions torch --index-url https://download.pytorch.org/whl/cu130
   # o
   curl -s https://download.pytorch.org/whl/cu130/torch/ | grep -o 'torch-[0-9.]*%2Bcu130[^"]*' | sort -u | tail
   ```
   Anotar la terna exacta (`torch`, `torchvision`, `torchaudio`) — las tres
   versiones están acopladas por ABI.

2. **Comprobar el resto del stack** contra CUDA 13:
   - `onnxruntime-gpu`: ¿existe build para CUDA 13? Si no, **parar** y mantener
     `cu128` hasta que la haya.
   - `opencv-python-headless`, `numpy<2.3`: no dependen de CUDA, pero
     re-verificar las cotas de `config/constraints.txt`.

3. **Aplicar los 9 puntos de la tabla** en un solo commit atómico.

4. **Actualizar el snapshot** `report_expected.md` (puntos 3 y 14): el test
   `report_markdown_matches_snapshot_and_redacts_secrets` compara literalmente,
   así que si se cambia el fichero de fixtures sin el código (o al revés) falla.

5. **Validar en un entorno con GPU** (Vast.ai con RTX PRO 6000):
   ```bash
   docker build -t comfyuipod:cu130-test .
   docker run --rm --gpus all comfyuipod:cu130-test \
     python3 -c "import torch; print(torch.__version__, torch.version.cuda); \
                 assert torch.cuda.is_available(); \
                 x=torch.randn(1024,1024,device='cuda'); print((x@x).sum())"
   ```
   Lo crítico es `torch.cuda.is_available()` y que un matmul real corra: sin GPU
   el build puede "pasar" y el pod no arrancar nunca.

6. **Comprobar los custom nodes** que el `Dockerfile` instala con constraints
   (`ns`, `brain`, `kokoro`, `sam3`): sus `requirements.txt` piden torch
   antiguos. Las constraints deben seguir ganando.

7. **Ejecutar la suite completa** (`ruff`, `pytest studio/tests`, y el job de
   Rust) antes de abrir PR.

## Rollback

`git revert` del commit atómico. No hay migración de datos: todo son pines de
imagen. La rama `backup/pre-upgrade-686e0ff` guarda el estado previo.

## Riesgo

**ALTO.** No reversible en caliente: requiere reconstruir y redesplegar la
imagen, y un fallo solo se manifiesta con GPU presente. Por eso no se aplica a
ciegas.
