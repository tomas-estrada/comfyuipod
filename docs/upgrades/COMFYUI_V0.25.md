# Migración: ComfyUI core a v0.25.x

> **Estado: NO IMPLEMENTADA.** Documento de ejecución. Requiere construir la
> imagen y ejecutar el warm-up para validarse; no se puede comprobar sin Docker.
> La rama `upgrade/comfyui-v0.25` existe vacía a propósito.

## Dónde está el pin

`config/nodes.lock`, línea 5:

```
core ComfyUI https://github.com/Comfy-Org/ComfyUI.git bdc63e72f5ab29fde448c161db3057827148c0ef
```

El `Dockerfile` (líneas 115-121) lo lee con `awk '$1 == "core"` y hace
`git fetch --depth=1 origin "$sha"`. **No hay variable de versión que cambiar**:
todo el pin es ese SHA.

Para obtener el SHA de un tag:

```bash
git ls-remote https://github.com/Comfy-Org/ComfyUI.git refs/tags/v0.25.0
```

## Por qué es arriesgado

No es un bump de dependencia: es el núcleo que ejecuta todos los grafos. Los
12 custom nodes de `config/nodes.lock` se clonan por SHA suelto, sin verificación
de compatibilidad con la versión de ComfyUI. Una ruptura de API interna
(p. ej. en `nodes.py` o en el registro de `class_type`) solo se manifiesta al
ejecutar un grafo real.

Lo que **sí** protege ya:

- El warm-up del build (`Dockerfile`, `python3 main.py --cpu --quick-test-for-ci`)
  falla si algún custom node da `IMPORT FAILED`. Eso captura rupturas de import,
  no de ejecución.
- `comfyctl/tests/fixtures/report_expected.md` y el validador de plantillas
  (`KNOWN_NODE_SPECS`) fijan el contrato de `class_type` de los nodos usados.
- El smoke test E2E de CI arranca la imagen construida y ejercita Nginx,
  status-server, portal, supervisor y SIGTERM.

## Procedimiento

1. **Leer el changelog** de la versión objetivo y anotar explícitamente:
   - nodos cuyo `class_type` cambió o se renombró;
   - cambios en el formato de guardado de workflows (`.json` de `api`);
   - cambios en `/queue`, `/history` o `/system_stats` (los consume
     `comfy_studio/comfyui_client.py` y `comfyctl`).

2. **Actualizar el SHA** en `config/nodes.lock` (línea 5) y la fecha de la
   cabecera del fichero.

3. **Construir y leer el warm-up**:
   ```bash
   docker build -t comfyuipod:v025-test .
   ```
   Un `IMPORT FAILED` en la salida identifica el custom node roto.

4. **Verificar el validador de plantillas** — si un nodo cambió de nombre, las
   plantillas de `studio/comfy_studio/templates/` quedan inválidas:
   ```bash
   python3 -m pytest studio/tests/test_templates_registry.py -q
   ```

5. **Smoke E2E real** con GPU (Vast.ai): arrancar la imagen, enviar un grafo
   corto por `/prompt` y confirmar que aparece en `/history` con salida. El
   warm-up en CPU no ejercita los caminos GPU.

6. **Actualizar el snapshot** si `comfyui_sha` aparece en
   `comfyctl/tests/fixtures/report_expected.md` (hoy vale `4c62a84068a0`).

7. Abrir PR con el diff del SHA y la lista de nodos verificados.

## Rollback

Revertir el SHA en `config/nodes.lock`. Sin migración de datos.

## Riesgo

**MEDIO-ALTO.** A diferencia de CUDA/PyTorch, el fallo sí puede ser parcial
(un nodo concreto deja de funcionar), lo que es más difícil de detectar que un
fallo total. Requiere validación con GPU.
