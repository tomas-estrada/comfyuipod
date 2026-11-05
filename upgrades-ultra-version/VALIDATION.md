# Validación de este paquete

Este documento informa comprobaciones de la especificación, no del producto Ultra.

- 34 capacidades C01–C34 obligatorias.
- 14 programas P01–P14.
- 30 opciones adicionales N01–N30, con disposición explícita.
- 27 paquetes W00–W26, con DAG sin ciclos y referencias comprobadas.
- 30 grupos de aceptación A00–A29 y cinco demostraciones D01–D05.
- 114 registros de fuentes primarias/heredadas, con alcance de verificación.
- Goal portable de aproximadamente 1.200 caracteres, separado de la misión extendida.

Comprobación ejecutada: `python3 -W error upgrades-ultra-version/validate_spec.py`.
Resultado: SPEC OK. Links relativos y referencias entre IDs válidos; no se detectaron patrones de credenciales ni rutas personales en los archivos preparados.

El último CI de aplicación verificado pertenece a main `b54195f0976d6c57b0ee04962e3059219db55d57`, run 41964177024, correcto. Eso no acredita tests de código futuro ni resultados GPU. Esta entrega añade documentación y el validador de esa documentación; no cambia la aplicación, Dockerfile, workflow ni modelos.

No se han alquilado pods, descargado pesos ni ejecutado pruebas neuronales/GPU para producir estos documentos. Los endpoints de fuentes externas no se probaron todos en vivo; fuentes heredadas y límites están etiquetados en sources.json.

Durante la implementación, conservar esta diferencia entre validar el plan, verificar la imagen y medir funciones reales. No usar este archivo como una constancia de que Ultra ya fue implementada.

