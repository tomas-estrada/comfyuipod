# Validación de este paquete

Este documento informa comprobaciones de la especificación, no del producto Ultra.

- 33 capacidades activas obligatorias.
- 14 programas P01–P14.
- 30 opciones adicionales N01–N30, con disposición explícita.
- 26 paquetes de trabajo activos, con DAG sin ciclos y referencias comprobadas.
- 29 grupos de aceptación activos y cinco demostraciones D01–D05.
- 114 registros de fuentes primarias/heredadas, con alcance de verificación.
- Goal portable de aproximadamente 1.200 caracteres, separado de la misión extendida.

Comprobación ejecutada para el paquete: `python3 -W error upgrades-ultra-version/validate_spec.py`.
Resultado en el snapshot de preparación: SPEC OK. Links relativos y referencias entre IDs válidos; no se detectaron patrones de credenciales ni rutas personales en los archivos preparados.

El CI registrado al preparar este paquete fue main `b54195f0976d6c57b0ee04962e3059219db55d57`, run 41964177024; es evidencia histórica y no acredita el código actual. El baseline capturado para el handoff es main `b612fbe7369dd20d66d8933095f23882ade7a2af`, workflow [44033581865](https://github.com/tomas-estrada/comfyuipod/actions/runs/44033581865); consulta [`docs/HANDOFF-17NOV.md`](../docs/HANDOFF-17NOV.md). Esta validación comprueba el paquete de especificación, no la implementación ni los resultados GPU.

No se han alquilado pods, descargado pesos ni ejecutado pruebas neuronales/GPU para producir estos documentos. Los endpoints de fuentes externas no se probaron todos en vivo; fuentes heredadas y límites están etiquetados en sources.json.

Durante la implementación, conservar esta diferencia entre validar el plan, verificar la imagen y medir funciones reales. No usar este archivo como una constancia de que Ultra ya fue implementada.

