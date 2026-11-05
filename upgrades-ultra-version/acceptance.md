# Aceptación, regresiones y demostraciones

Cada aceptación produce evidencia inspeccionable en el commit/PR correctos y se registra antes de delegar. Este archivo define pruebas que el worker implementará; no informa resultados ya obtenidos.

## Niveles de evidencia

- L0: propuesta/fuente/enlace.
- L1: código/contrato y unit tests propios.
- L2: engine preparado en imagen y adapter conectado, pruebas CPU/integración reales donde son posibles, estados honestos donde falta hardware/pesos.
- L3: ejecución GPU en hardware objetivo con artifact/telemetría propia y escenario pertinente.
- L4: reproducido con seeds/escenas heldout y comparación de coste/calidad, no solo una muestra.

Una capacidad obligatoria requiere L2 para candidato pre-GPU y L3 para validación funcional GPU. Un programa que afirma una mejora científica requiere L4 para esa afirmación. Una CI verde con mocks no eleva el nivel. L2 no se llama “función GPU validada”.

## Matriz de checks por lote

### A00. Baseline y PRs

- Mostrar remoto/branch/SHA y cambios existentes sin sobrescribirlos.
- Obtener estado actual de PR/CI, no del documento histórico.
- Resumen material inicial en diez minutos activos.
- Evidencia de qué PR existente se reutiliza, repara o queda fuera. No duplicar atlas ni UI.
- No acceder ni publicar medios personales para este inventario.

### A01. Adquisición e integridad

Pruebas obligatorias con fixture pequeño, sin descargar modelos reales:
1. Archivo correcto existente se reutiliza con integridad verificada.
2. Archivo no vacío de tamaño/digest incorrectos no se acepta ni anuncia disponible.
3. CLI/Python reciben revisión fija y archivos exactos; pin omitido produce fallo de prueba.
4. Error a mitad de copia EXDEV conserva destino previo válido y no publica parcial.
5. Dos adquisiciones concurrentes no corrompen destino; cancelación y reanudación respetan locks.
6. Staging/corrupt/verified son distinguibles para el agente y health.
7. Un symlink/path traversal no escapa de la raíz autorizada.
8. Confundir SHA de blob Git y digest de contenido produce fallo, no integridad falsa.

### A02. Hardware real y recetas

- Perfiles 96VRAM/128RAM y 96VRAM/256RAM con detección real y cgroups menores simulados.
- VRAM/RAM nominal nunca excede límites reales por efecto de una variable.
- CPU-only/driver ausente se reporta unknown/unavailable, no GPU emulada compatible.
- Una sample simulated=True no se convierte en measured/validated ni participa en “fits real”.
- Resolución, frames, batch/contexto, quant/offload, código/pesos y GPU/driver distintos no se agrupan como una receta.
- Cambiar perfil seleccionado cambia la selección; no se usa el histórico de otra GPU como garantía.
- Conservación de raw measures y provenance; estimaciones claramente etiquetadas.
- OOM y cancelación cuentan como intentos/coste, sin esconderlos del Pareto.

### A03. Sizing coherente

- Boot sin modelos de tarea no hereda GB de todo el catálogo ni del bootstrap viejo.
- La misma fuente calcula start gate, recomendación de volumen, adquisición por tarea y CLI.
- Configuración con engine grande y cero pesos distingue tamaño del engine de descargas futuras.
- Staging same-FS/cross-FS y rollback se cuentan; retención de caché respeta recursos en uso.
- 180/200 GB no es literal de gate global. Un mínimo de 248 GB solo es válido si lo justifican componentes seleccionados, no por mantener un test antiguo.
- Contrastar reporte con tamaño real de la imagen construida y describir incertidumbre de los restantes.

### A04. Lease global y cleanup

- Dos procesos y al menos dos tipos de engine intentan GPU a la vez; solo uno adquiere heavy lease.
- Un stage CPU puede seguir si cabe su presupuesto, sin cargar CUDA por accidente.
- Un owner huérfano no permite reutilizar GPU mientras su subprocess siga vivo.
- Cancel/timeout/exception libera proceso/grupo, locks y memoria o devuelve cleanup_failed explícito.
- Fencing impide que un owner antiguo libere el lease nuevo.
- Checkpoint/resume no duplica el stage ni reinicia su contador.
- Ensayo CPU usa backend de recursos de test y comprueba exclusión real de procesos; peaks CUDA quedan pendientes hasta A29.

### A05. Persistencia, DAG y semántica del director

- Detener entre dos stages, reiniciar y continuar desde checkpoint durable con artifacts comprobados.
- Idempotencia en submit/side effects y recuperación de un resultado producido antes del crash.
- Cambiar texto/título no recalcula ASR, geometría ni actuación. Cambiar luz solo invalida sus dependientes.
- Replanificación local→receta→shot tiene un presupuesto común persistente.
- El runtime de producción con LangGraph ejecuta la ruta efectiva; tests del fallback por sí solos no bastan.
- Ambigüedad de propósito mantiene evidencia/hipótesis; una selección conservadora produce plan completo o un motivo terminal honesto.

### A06. Proveedores y datos privados

- Dos perfiles de proveedor con distintos model IDs y capacidades de tools/streaming ejercitados por contract server; cambiar config no exige editar código.
- 401, 429, timeout, JSON inválido, respuesta sin tool-call y contexto excesivo tienen errores/reintentos acotados.
- Un provider fixture es simulated en provenance; no acredita conectividad real con API facturada.
- Keys solo runtime. Sentinelas no aparecen en API, SSE, excepciones, logs, checkpoint o export.
- VAST_API_KEY no viaja al pod. Medios personales no se empaquetan ni suben a CI.
- OCR, documentos y respuestas de fuentes se tratan como datos, no instrucciones para shell/git/permisos.

### A07. Engines preparados y build temprano

- Lista seleccionada con code SHA, dependency lock/hashes, avisos y decisión de redistribución por componente.
- Adapter ejecuta un entrypoint real con inputs validados; no input-copy como render ni salida vacía como éxito.
- Código/deps/binarios/browsers/fonts existen en imagen y su inventario corresponde a lo instalado.
- Probes CPU ejecutables y verificación estática de librerías/entrypoints GPU-only. Si falta driver/GPU, estado pending_gpu; no se simula un pass.
- Boot con red cortada no ejecuta pip/npm/git clone ni descarga catálogo/modelos de tareas.
- Extensiones requieren arquitectura/ABI preparada; imports CPU no acreditan kernels Blackwell.
- Medición real del builder; si no cabe el conjunto seleccionado, el release queda bloqueado con números y opciones autorizadas concretas.
- Preservar manager dinámico solo si ya está explícitamente autorizado y deshabilitado por defecto; la ruta esencial no lo usa.

### A08. Ingesta e índice

- Fixture de 100 entradas mixtas: video móvil rotado/VFR, audio, imágenes, duplicados y un corrupto; no 100 copias del mismo PNG.
- Proxies/ASR/segmentos apuntan a fuentes y tiempos verificables; crop/rotation/timebase consistentes.
- Duplicados no multiplican modelos/trabajo; archivo corrupto devuelve diagnóstico sin romper los válidos.
- Búsqueda devuelve archivo+intervalo, no una descripción genérica sin evidencia.
- Límites de cantidad/tamaño configurables y backpressure. 100 no queda hardcodeado como máximo de producto.

### A09. Referencias e inspiración

- Análisis de referencia autorizada produce shot map, beat map, OCR/títulos, movimiento aproximado y receta editable.
- Modo sin red funciona sobre biblioteca propia.
- Conector sin credenciales o con cuota agotada queda opcional/blocked, no inventa datos.
- 404/cambio de enlace no rompe referencias ya cacheadas válidas.
- No se invoca cloud/compra ni una cuenta no concedida. Prueba intercepta requests a endpoints facturados no autorizados.
- El análisis distingue qué observó, qué infirió y qué adaptó al brief. No promete viralidad.

### A10. Generación base y 4K

- Manifests mínimos de LTX/Wan/keyframes/upscale consistentes con los entrypoints reales.
- Selección de encoder/LoRA/VAE/revisión exactos; no descargar todas las variantes.
- Plan con 100 assets selecciona subconjunto pertinente por shot respetando límites del motor.
- Tests validan outputs reales CPU del pipeline determinista y contratos GPU preparados.
- Duración/resolución/fps/alpha/audio reales validados después de cada motor.
- MiniMax cloud o 2K no publicado no cierra la ruta 4K. Upscale local sí forma parte del plan.
- GPU performance y calidad pendientes explícitamente hasta A29.

### A11. Atlas offline y verdad geográfica

- Cortar DNS/red y ejecutar búsqueda/detalle/preview/prepare/render CPU con el bundle incluido.
- Cada referencia usable tiene archivo presente, hash, autor/licencia y metadata de calidad.
- Distinguir 10.209 lugares del catálogo, 152 con vistas y reconstrucciones validadas; publicar conteos nuevos reales por nivel.
- Municipio/lugar nombrado en prompt tiene prioridad sobre selección previa de proyecto.
- Caso de nombre repetido, geotag lejano, 0°/180°, rumbo desconocido, FOV desconocido y view_id incompatible.
- Evaluar exactamente la vista solicitada, no una vista distinta elegida por select_view.
- El fondo del atlas llega al nodo de imagen o a una composición explícita. Una plantilla incompatible devuelve razón y alternativa, sin ignorarlo.
- Muestra visual estratificada por municipio/tipo de lugar detecta thumbnails, comida/insectos/otros municipios y contenido irrelevante.
- Contadores de expansión solo suman evidencia aceptada; reportar datos faltantes y no generar referencias falsas para alcanzar cuota.

### A12. Geometría y ajuste físico

- Malla/partes/materiales/cámaras reabribles, units/transforms explícitos y asset ID persistente.
- Reproyección, silhouette y render de objeto asimétrico prueban convenciones de ejes/poses.
- Fixture Mitsuba CPU optimiza un parámetro identificable y se compara con baseline y heldout.
- Texturas/licencias y regiones inferidas conservadas; no presentar visión de cara oculta como observación.
- GPU adapters de reconstrucción llevan inputs/outputs reales previstos y missing-weights sin pass falso.

### A13. Cámara y multivista

- Animatic visible con cámaras/trayectoria/lente, no solo JSON descriptivo.
- Colisiones/visibilidad/composición verificadas antes de stage caro.
- Calibración con split heldout, error por vista y coverage de la ruta completa.
- Proxy sintético cumple el umbral de reproyección definido; traducción neural tendrá otro criterio en GPU.
- Una foto 2.5D no acredita órbita libre de 360° ni exactitud de fachada oculta.

### A14. Física y articulación

- Chrono/Blender ejecutan headless CPU una maniobra y exportan poses/velocidades/contactos a la escena.
- Joints/limits/unidades/timebase y vuelta al estado inicial comprobados en producto articulado.
- Estados de física/cache y render están vinculados por hash/seed.
- FX al menos en dos casos implementados con parámetros, caché y pases editables.
- Fidelity de movimiento después de traducción neural no se deduce del proxy.

### A15. Cuerpo y movimiento

- Esqueletos/mappings/bind/scale declarados; retarget conserva raíz y tiempos.
- Contactos de pies/manos y joint limits en fixtures; export/import reejecutable.
- Transferencia distingue identidad, actuación, máscara y contexto de cámara.
- Licencias de modelos corporales y referencias presentes; un archivo protegido faltante no se inventa.
- La imposibilidad de usar Isaac Gym o ReMoGen no se oculta bajo un comando nominalmente integrado.

### A16. Cara y avatar

- Audio2Face/morph mapping se integra realmente con el rig; audio/texto/stems por hablante.
- Actuación temporal editable y cambio de cámara/luz sin regenerar audio.
- Gated model requerido devuelve estado claro y no se redistribuye como libre.
- Protocolos de identidad, turnos y lip sync preparados con positivos/negativos.
- Un render Blender de morphs de fixture es evidencia CPU, no prueba de calidad del modelo neural.

### A17. Causalidad, alpha y conservación

- Escenas actor-on/actor-off con shadow/reflection ground truth.
- Modificar una causa actualiza solo efectos relacionados; regiones intactas conservan sus fuentes.
- Alpha/color y tiempo de capas verificados mediante reconstrucción lossless.
- VOID no depende obligatoriamente de Gemini; máscara local/reasoner configurable es real.
- CorridorKey/VideoMaMa++ excluidos de rutas que no estén autorizadas/entregadas.

### A18. Luz y estilo

- Mapping de AOV/roughness/normal/depth probado; validez/unknown explícitos.
- Fixture con dos iluminaciones y logos preservados permite comparación, no solo una puntuación estética.
- Medidas temporales compensan flujo y excluyen oclusiones/cortes.
- Restilización y reiluminación se reportan como tareas distintas.
- Recompilar una luz no invalida campos ajenos a esa intervención.

### A19. Memoria y 4D

- A→B→A conserva anclas y registra drift, con baseline sin memoria.
- Reconstrucción 4D produce objetos/poses/cámaras temporales editables; no solo una carpeta de frames.
- Reencuadre/retiming de fixture sintético desde esa representación, comparado con ground truth.
- Ninguna imagen generada se promociona a evidencia del atlas.
- Límites de escena/duración/contexto por receta y memoria se respetan.

### A20. Sonido y música

- Eventos con onsets, stems válidos, licencias y correspondencia a acciones/cortes.
- Cambio causal de actor elimina su sonido sin perder voz/ambiente.
- Sample rate, clipping, loudness y true peak se miden en salida.
- Beat map y estructura de música editables; stems solo si existen en la ruta.
- El resultado conjunto de Foley-Omni no se anuncia como edición arbitraria de stems ni duración ilimitada.

### A21. Español y pronunciación

- Dataset pequeño de nombres de lugares, cifras, fechas y frases de anuncio, con texto esperado.
- Alineación, turnos y audio por hablante; comparación de Qwen3-TTS/IndexTTS según disponibilidad.
- Métricas ASR son indicadores, no juez perfecto de acento/intención.
- Distinguir variante de pronunciación aprobada de mero soporte de idioma; lexicon persistente corregible.

### A22. Composición y entregables

- HyperFrames/Remotion/Blender/FFmpeg render local, assets y fuentes incluidos.
- Proyecto editable reabre y cambia texto/transición sin regenerar contenido aprobado.
- Exporta 3840×2160 para 16:9 y 2160×3840 para 9:16, según tarea; cambios de aspecto no estiran personas.
- fps/timebase/color/rango/audio/captions/safe areas definidos y verificados.
- Frames/alpha/stems/fuentes conservados antes de encode final; manifest de reproducción y créditos.
- Resolución 3840×2176 de un motor no se disfraza de 3840×2160: transformación/crop explícitos.

### A23. UI y cliente

- Reutilización OpenCode con licencia MIT y StudioClient propio, sin simular SDK ajeno.
- Crear proyecto, subir assets, enviar brief, ver herramientas/preview/timeline, editar, cancelar y reanudar.
- Reconnect no duplica acciones; evento de error no deja spinner infinito ni éxito falso.
- Coverage/limitaciones y pruebas/recursos accesibles sin obligar a leer logs técnicos.
- Pruebas browser con CSP/sin CDN y backend real CPU determinista.
- Cliente terminal probado en Linux/Windows/macOS para paths con espacios/unicode y errores; pod continúa Linux.

### A24. Flujo autónomo de producto

- Prompt ambiguo + assets propios crea brief y plan, ejecuta herramientas y entrega sin aprobaciones rutinarias.
- Restricciones fijas de producto/lugar se conservan incluso si el agente prefiere otra idea.
- Cada salto y reintento tiene razón, contador y presupuesto.
- Una ausencia de capacidad se informa antes de gastar; ruta alternativa conserva requisitos o se detiene honestamente.
- Responder “viral” o generar copy/hashtags no acredita investigación ni éxito social.

### A25. Harness y escenarios

- D01–D05 son ejecutables por CLI/API y dejan manifest, resultados y niveles de evidencia.
- Fixtures CPU son medios/escenas reales, sintéticos o con permisos públicos documentados.
- Cada escenario puede limpiar workspace temporal y reanudar sin duplicar artifacts.
- Jobs GPU preparados con selección exacta de test/recipe y sin ejecución ni descarga masiva por defecto.
- Tests skipped/pending muestran cantidad y razón; no cuentan como passed.
- No se añade un backend de simulación que siempre declare todas las capacidades correctas.

### A26. Ciencia y aprendizaje

- Hipótesis/intervención/métrica/criterio/budget fijados antes de resultados.
- Split desarrollo/validación/test y control de datasets/referencias duplicadas.
- Seeds/escenas múltiples para afirmaciones empíricas; registrar dispersión, raw data, fallos y costes.
- Pareto por resultado aceptado, con identidad/factualidad como restricciones y no solo score de juez.
- Propuesta LoRA incluye datos/licencias/código/hyperparameters/holdout/rollback; training no se ejecuta ahora.
- Restricciones de destilación/modelos se respetan por provenance.
- Una hipótesis negativa termina sin buscar indefinidamente una muestra bonita.

### A27. CI y revisión del proyecto

Ejecutar los comandos vigentes de AGENTS.md. En la base revisada incluyen:
- ruff check studio panel scripts config/probes
- pytest studio/tests -q
- python -m py_compile config/probes/*.py scripts/check_models_manifest.py
- python -m comfy_studio.templates
- python scripts/check_models_manifest.py --fixture --offline
- python scripts/prune_model_cache.py --json (dry-run)
- En comfyctl: cargo fmt --check; cargo clippy --all-targets -- -D warnings; cargo test --locked
- nginx -t -c config/nginx.conf

Usar entorno/fixture real del proyecto; documentar requisitos y verificar en CI si el host local no tiene nginx/Rust. No cambiar checks a always-success ni quitar tests para hacer merge. Si AGENTS cambió, seguir sus comprobaciones actuales.

Cada PR incluye problema/comportamiento, alcance, evidencia pertinente y limitaciones. Después del merge revisar CI de main; no atribuir al merge tests hechos en otro SHA. Desactivar drafts cuando proceda para no bloquear por forma una aceptación pr_checks válida.

### A28. Imagen/release pre-GPU

Para ACCEPTED_PRE_GPU deben cumplirse TODAS:
1. W00–W25 aceptadas con evidencias; ningún C/P obligatorio reducido a enlace o placeholder.
2. PRs integradas/reconciliadas y SHA final exacto en main.
3. CI de esa revisión verde con regresiones semánticas y pruebas pertinentes, sin confundir skips GPU con validación.
4. Imagen de producción del mismo repo construida/publicada con digest real y provenance de código/locks; no solo un tag mutable.
5. Atlas físico, medios/licencias y UI/fonts internos; smoke offline desde la imagen descargada.
6. Sin instalación runtime de engines, sin descarga de catálogo al boot y sin secretos/medios privados.
7. Inventario por engine/recipe y sizing real, con incompatibilidades/condicionales precisos.
8. D01–D05 y campaña GPU preparados, presupuesto actual respetado: cero pod/API facturada nuevos.
9. Matrices C/P/N con nivel de evidencia y pending reales, guía multiplataforma, rollback/migraciones y cierre de misión.
10. Los campos de salud/README no declaran GPU validada ni superioridad empírica que aún no se midió.

Si un punto falta, estado BLOCKED_PRE_GPU con requirement ID, evidencia, causa y siguiente acción concreta. Rechazar una opción N no bloquea si existe ruta obligatoria equivalente real; rechazar una C/P obligatoria sí impide éxito completo.

### A29. Puerta futura GPU — no ejecutar ahora

Requiere autorización explícita de presupuesto total, precio máximo/hora, duración, disco y credenciales/instancia. El plan debe admitir instancia propia ya autorizada, no exigir Vast si el operador elige otra.

1. Preflight local: CLI/config/SSH/API, saldo y elegibilidad sin imprimir keys; diagnóstico y guía si la CLI no funciona.
2. Fijar imagen por digest y comprobar driver/arquitectura/VRAM/RAM/cgroup/disco reales antes de pesos.
3. Reservar tiempo/espacio de adquisición por receta y seleccionar pruebas prioritarias. No cargar todo el catálogo.
4. Al primer fallo estructural, guardar logs/telemetría/artifacts mínimos, eliminar/terminar la instancia y confirmar cese de cobro antes de un arreglo largo. No mantener GPU inactiva durante desarrollo.
5. Parche→PR/checks→rebuild digest nuevo→nuevo intento solo dentro del presupuesto compartido. No convertir “try again” en bucle infinito.
6. Finally/watchdog local cierra los recursos que creó la campaña; no borra pods/volúmenes preexistentes ni mantiene disco cobrado sin política explícita.
7. Model-agent tests usan un proveedor configurable, secretos runtime y presupuesto por misión; no modelo fijo.
8. Medir D01–D05 con inferencia real, compatibilidad kernels, load/unload, peaks, tiempo/coste completo y calidad vs baselines.
9. 128/256 RAM son resultados distintos. Si solo hay una oferta disponible, no afirmar validación de ambas.
10. Archivar evidencias privadas donde corresponda y un reporte público redactado. Solo después puede marcarse L3/L4/Ultra validada.

## Cinco demostraciones de profundidad

| Demo | Entrega | Fixture CPU y ground truth | Validación GPU posterior |
| --- | --- | --- | --- |
| D01 | Coche en lugar real verificable del Aburrá, varias cámaras, luces y sonido | Escena sintética calibrada + vistas reales autorizadas del atlas; ruta/oclusiones/cámara/coverage | Reconstrucción/guías→generación→relight→Foley→4K; logos, fondo, reproyección y movimiento contra baselines |
| D02 | Eliminar una causa y sus consecuencias | Render actor-on/off con sombra/reflejo/sonido y partes intactas | VOID/omnimatte vs borrado solo de máscara, error revelado/residual, invariantes y costes |
| D03 | Producto articulado con cambio de material y varias cámaras | CAD/partes sintéticas, joints/mallas/PBR/convs conocidas, Mitsuba ajuste | Reconstrucción/partes/material y transferencia neural, heldout multivista y consistencia |
| D04 | Actor que interactúa y habla español con identidad persistente | Rig autorizado, contactos, frase/voz fixture, lipsync temporal y reencuadre | Recuperación/generación/retarget/Audio2Face/InfiniteTalk; identidad/contactos/turnos/sonido |
| D05 | Director autónomo con prompt ambiguo y carpeta heterogénea | 100 assets de tipos/tiempos variados, errores parciales y restricciones medibles | Investigación permitida→brief→shots→reparación→máster 4K; budget/checkpoints e inspección de outputs |

Además del resultado, cada demo conserva inputs, versiones, recipe, seed, artifacts intermedios y mediciones. Un montage determinista CPU demuestra la fontanería; no sustituye el desafío neural descrito para A29.

