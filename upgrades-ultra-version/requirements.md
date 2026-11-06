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
| R14 | Una imagen/pod Linux del mismo repo y GHCR | Build real y digest, engines preparados, assets de interfaz y atlas internos; no sandbox indispensable |
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

## Atlas: fidelidad y cobertura antes que contadores

Municipios: Medellín, Envigado, Itagüí, Sabaneta, Bello, Copacabana, Girardota, Barbosa, La Estrella y Caldas. Prioridades iniciales incluyen Puente de la 4 Sur/Gilberto Echeverri, corredor de Las Palmas, centro de Medellín y parques/espacios públicos de varios municipios. Cada nombre exige desambiguación real; no basta buscar una palabra común.

Mantener cuatro conjuntos separados:
1. Catálogo geográfico: lugares/coordenadas/topología de calles.
2. Evidencia observada: imágenes/video autorizados, fechas, cámara/posición conocidas o desconocidas.
3. Reconstrucción/calibración: geometría con cobertura y errores de reproyección.
4. Inferencias/propuestas: fachadas o vistas sintetizadas y mundos ficticios, nunca evidencia factual nueva.

La meta de ampliación será al menos 200 lugares con evidencia visual aceptada y diversidad municipal, partiendo de 152 declarados; es un objetivo de adquisición, no permiso para bajar calidad ni inventar cobertura si los datos gratuitos no existen. La aceptación documental requiere cobertura visual verificada en los diez municipios o un bloqueo por cada hueco. El objetivo de exactitud 3D se aplica a un conjunto de anclas con material suficiente, no a los diez mil nombres de OSM.

Seleccionar al menos diez anclas reales para diagnóstico multivista, con informe de solape/calibración/poses/escala y split de vistas heldout. Solo las que cumplan datos reales y tolerancias pueden ofrecer nuevas cámaras en modo strict_exact. Si no existe ninguna con evidencia suficiente, P08 queda bloqueado; una malla generada no resuelve ese requisito. No prometer todas las perspectivas de una ciudad sin captura.

En modo strict_exact, la falta de vista/rumbo/calibración requerida produce estado insuficiente y una replanteación compatible con vistas disponibles. El agente puede elegir otra cámara/lugar solo si la misión no fijó ese lugar. Los usuarios ven las limitaciones; no se sustituyen en silencio fachadas o puentes por otros.

## Fuentes gratuitas y resiliencia

Fuentes priorizadas y fijadas por versión:
- OSM/Geofabrik Colombia para geometría geográfica/topología; conservar ODbL y atribución. No usar un mosaico de tiles ni Google Street View como biblioteca libre.
- Wikimedia Commons mediante MediaWiki API/Structured Data para medios por archivo; conservar autor, página, licencia, bytes y hashes.
- KartaView, donde exista cobertura utilizable y acceso público permitido; sus imágenes tienen términos CC BY-SA, no una licencia universal del sitio para todo.
- Datos abiertos institucionales o medios propios únicamente si el archivo/dataset declara permiso y tiene procedencia verificable.
- Assets sintéticos/CC0 propios para fixtures; nunca como referencia factual de un lugar real.

Adquirir durante trabajo/build con caché, reintentos limitados, backoff y validación de contenido. Un dominio caído no rompe el baseline ya incluido. Cambios de fuente generan una nueva versión de bundle por PR y pasan pruebas; no mutan silenciosamente el atlas de un pod existente.

El usuario no compra datos, mapas, imágenes, motores ni suscripciones para cerrar la ruta esencial. Tener API gratuita con registro no implica credenciales presentes: usar fuentes ya disponibles y mostrar qué conectores optativos faltan.

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

