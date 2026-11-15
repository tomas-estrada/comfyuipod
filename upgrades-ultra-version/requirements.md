# Requisitos de Ultra Research

## Propósito y límites de esta misión

Laboratorio audiovisual personal de investigación, orientado a entregar reels/Shorts 2D de calidad profesional y conservar los activos/editables intermedios. La complejidad 3D/4D se justifica por control, edición, continuidad, física o medición de la entrega final, no por añadir una demo desconectada.

Todas las C01–C34 son objetivos obligatorios. P01–P14 son programas de implementación/experimentación que las conectan. N01–N30 son alternativas técnicas: cada una debe recibir una decisión, pero instalar las treinta no demuestra profundidad. `engines.json` también incluye herramientas base y servicios propios; sus contadores no son una lista de modelos que deban descargarse.

El plan original definía un candidato pre-GPU. Desde entonces W26 ejecutó una demo SDXL acotada (USD 0.59), así que el estado real es GPU L3 parcial, no validación integral. La misión vigente es cerrar rutas y pruebas pendientes y ampliar la evidencia por receta; pesos gated, hardware no medido y servicios ausentes deben mostrarse con estados correctos, sin declarar cubiertos los requisitos bloqueados.

## Contrato del producto

| ID | Requisito obligatorio | Evidencia mínima |
| --- | --- | --- |
| R01 | Recibir prompt y assets heterogéneos, incluyendo MP4/MOV/MP3/JPG/PNG y metadatos variables | Ingesta segura, proxies reales, informe de incompatibles y relación original→segmentos |
| R02 | Resolver intención/público/concepto a partir de evidencias y de un perfil de proyecto | Brief tipado con fuentes, incertidumbre y un plan ejecutable; no inventar datos del anunciante |
| R03 | Producir sin preguntas rutinarias, con presupuesto y criterios de calidad definidos | Misión persistente que decide, ejecuta, evalúa, repara o termina con un motivo trazable |
| R04 | Soportar varias rutas LLM/VLM y modelos configurables por rol | Dos adaptadores/perfiles de proveedor ejercitados en pruebas de contrato; nombres de modelo solo en config runtime |
| R05 | Buscar inspiración y señales públicas permitidas sin convertir redes en dependencia obligatoria | Registro de consulta, permiso de acceso/uso, métricas observadas con fecha y receta propia derivada |
| R06 | Compartir estado semántico, temporal, geométrico y de procedencia entre motores | Contratos versionados y validadores; fallos de conversión tipados, no defaults silenciosos |
| R07 | Ensayar en proxy antes de generar caro; preservar identidad, producto y lugar | Animatic visible, constraints y decisión de escalado de fidelidad |
| R08 | Implementar cámaras, reconstrucción, activos, actuación, efectos, edición causal, luz y sonido como producción conectada | Rutas C/P completas accesibles desde API/herramientas/planner/UI, con artifacts reales |
| R09 | Entregar versiones finales 4K con audio/captions/color y proyectos editables | ffprobe, decodificación, máster/manifiesto, OTIO/escena/composición según soporte real |
| R10 | Reparación selectiva con conservación de partes aprobadas | DAG/lineage y pruebas de no recomputación; comparación de frames/pases antes del encode final |
| R11 | Memoria útil entre tareas y un investigador que acumule conocimiento | Recuperación con ámbito correcto, experimentos holdout, Pareto, promoción y rollback de receta/adaptador |
| R12 | UI agéntica con reutilización MIT de OpenCode y dominio audiovisual propio | Sesión, assets, timeline, vistas/evidencia, streaming, cancelación, reanudación y errores visibles |
| R13 | Uso desde terminal de macOS, Windows y Linux | Cliente común/documentado, pruebas de rutas/quoting/SSH/config por OS; sin requisito Bash para Windows |
| R14 | Una imagen/pod Linux del mismo repo y GHCR | Build real y digest, engines preparados, assets de interfaz internos; no sandbox indispensable |
| R15 | Pesos bajo demanda y caché verificable | Manifiestos por componente, revisión inmutable, integridad, adquisición transaccional y ausencia de descargas de catálogo al boot |
| R16 | Una GPU de 96 GB con RAM128/256 separadas | Admisión real y lease entre motores/procesos, medición de peaks y errores; no equivalencia nominal |
| R17 | Almacenamiento derivado y flexible | Cálculo reconciliado build→strict→CLI→adquisición, considerando staging/retención/temporales y no todo el catálogo |
| R18 | Lugares reconocibles y verificados del Aburrá, disponibles offline | Medios presentes, mapa de cobertura, selección estricta, atribución y pruebas de fondos hasta el render |
| R19 | Secretos y medios personales fuera del repo/imagen/artifacts públicos | Config runtime, redacción y pruebas con sentinelas; archivos privados solo en rutas autorizadas |
| R20 | Código abierto o uso gratuito permitido por componente, con redistribución revisada | Registro separado de código/pesos/datos/territorio; rutas pagas no obligatorias |
| R21 | Capacidad de recuperación y observabilidad | Checkpoint/restart/cancel por stage, estados exactos y costes/tiempos por resultado aceptado |
| R22 | Entrega por PR/main/GHCR comprobable | CI semántica, merge, CI posterior, digest y reporte final coherente con esa revisión |

## Hardware y recursos

- El servidor objetivo es RTX PRO 6000 Blackwell de 96 GB, no la RTX 6000 Ada de 48 GB ni una suma ficticia de varias GPU.
- Detectar UUID, arquitectura, driver/runtime, memoria total/libre y límites de procesos/cgroup. Usar bytes/GiB internamente y mostrar unidades.
- Intersectar límites disponibles con el perfil solicitado. Una variable de entorno no puede inventar VRAM/RAM.
- Definir perfiles 128 y 256 GB con reservas y estrategias propias. Comparar RSS/PSS/cgroup cuando aplique para no sumar memoria compartida dos veces.
- Secuenciar generadores, VLM local, upscale, simuladores CUDA y jueces pesados. Un generador no se ejecuta a la vez que un juez consume toda la GPU.
- Aceptar cuantización y offload que un backend soporta realmente; registrar precisión, esquema de cuantización, kernels, resolución, frames y componentes.
- No garantizar que 90 GB de archivos caben en 96 GB de VRAM. Activaciones, KV/contexto, workspace, VAE y compilación también consumen memoria.
- Evitar streaming de cada capa como política universal; usar una receta explícita cuando compense. Un VAE en CPU puede ser una decisión razonable de coste.
- Servicios y dependencias se preparan antes del despliegue. “Instalado” no obliga a tener el proceso o sus pesos residentes.

## Qué no satisface el requisito

- Un registro que dice “installed/integrated” sin binario, entorno y entrada ejecutable en la imagen final.
- Un adaptador que devuelve el input o un placeholder en vez de ejecutar el motor.
- Un test que solo comprueba un string, una importación o el propio mock.
- Una imagen local que no está publicada/reproducible desde el repo.
- Una PR abierta sin revisión, merge o evidencias requeridas.
- Un benchmark de otra GPU/resolución atribuido al pod objetivo.
- Renderizar diez fotos genéricas y llamar a eso “ciudad 3D exacta”.
- Puntuar “viralidad” con un VLM sin resultados sociales observados ni controles.
- Una funcionalidad central que usa una API facturada tras declararse local/gratuita.

## Cumplimiento de capacidades frente a variantes

Una alternativa puede rechazarse por licencia, acceso, incompatibilidad o falta de ventaja. Registrar el motivo, la fecha, la evidencia y la ruta elegida. No es necesario instalar todo lo alternativo. Si una capacidad o programa obligatorio no tiene una ruta real que preserve sus propiedades, permanece bloqueado y no se etiqueta Ultra completo.

Las licencias/admisiones de pesos que necesariamente corresponden al usuario se realizan en runtime cuando exista autorización y sin fingir aceptación. El candidato pre-GPU puede demostrar integración preparada y estados de falta de pesos; no puede declarar ejecución funcional GPU sin esos pesos y pruebas.

