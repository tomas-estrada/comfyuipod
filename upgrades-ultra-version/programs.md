# Catorce programas de investigación implementable

Todos son propuestas de integración/experimentación, no benchmarks ya obtenidos. Cada programa debe dejar una ruta real en la aplicación, contratos, artifacts y protocolo; las inferencias GPU se validan después. Las tolerancias numéricas propuestas se fijan antes del experimento y no se rebajan después de ver un resultado. Distinguir checks exactos de fixtures sintéticos de objetivos empíricos de contenido real.

## P01. Compilador cinematográfico por restricciones

Capacidades: C01, C02, C03, C04, C08, C20, C23, C31, C32. Trabajo: W04, W06, W11, W20, W22.

- **Ruta elegida:** MissionCompiler + ProductionState/LangGraph + Blender animatic + LTX/Wan + editor local.
- **Hipótesis:** Un plan con constraints verificables y ensayo proxy reduce rechazos/reintentos frente a prompt→generación directa.
- **Inputs:** Brief ambiguo, carpeta heterogénea y perfil del público; producto/lugar/duración/identidad opcionalmente fijados.
- **Outputs:** Hipótesis con evidencia, storyboard tipado, DAG, animatic, decisiones de receta, QA localizado y montaje editable.
- **Experimento y controles:** Comparar baseline de plan fijo y baseline de prompting directo con el compilador, usando los mismos assets y presupuesto. Ablación sin animatic y sin memoria. Medir violaciones de restricciones, assets relevantes utilizados, intentos y coste por resultado aceptado.
- **Aceptación:** Ningún dato de fecha/precio/logo aparece sin fuente. Una cámara físicamente imposible se corrige antes de inferencia. Una prueba modifica un título y verifica que geometría/ASR/actuación no se recalculan.
- **Límite que no se puede ocultar:** El VLM no se otorga la razón a sí mismo por declarar “coherente”. Deben existir comprobaciones numéricas y evidencia temporal.

## P02. Edición de consecuencias causales

Capacidades: C24. Trabajo: W15.

- **Ruta elegida:** VOID con preparador de máscaras local/proveedor configurable; VACE/Retake como baseline espacial/temporal y rerender Blender como verdad sintética.
- **Hipótesis:** Eliminar actor y efectos dependientes mejora consistencia causal frente a borrar solo su máscara.
- **Inputs:** Clip, máscara/track del actor, causal graph de sombra/reflejo/contacto/sonido y escena sintética con versión actor-on/actor-off.
- **Outputs:** Video intervenido, máscaras de efectos, artifacts originales preservados, región/intervalo de cambio y reporte causal.
- **Experimento y controles:** Dos escenas sintéticas con sombra/reflejo y una referencia real autorizada. Ground truth se renderiza sin el objeto. Comparar región revelada, sombra residual, reflejo residual y errores fuera de área; ablación de cada pase de VOID y del grafo causal.
- **Aceptación:** Las regiones intactas mantienen sus fuentes antes del encode. La ruta ejecuta un motor real cuando sus pesos están disponibles; sin pesos informa missing. La prueba CPU verifica la intervención causal sobre renderer/fixtures sin simular calidad neural.
- **Límite que no se puede ocultar:** Una escena real oculta no tiene ground truth recuperable por intuición; reportar plausibilidad y evidencia, no exactitud de lo nunca observado.

## P03. Producción por capas de objeto y efecto

Capacidades: C19, C24, C28, C29, C31. Trabajo: W15, W20.

- **Ruta elegida:** Generative Omnimatte público + VideoMaMa + Qwen-Image-Layered + Blender/FFmpeg. VACE repara donde conviene.
- **Hipótesis:** Representar objetos, sombras, reflejos y fondos como capas reduce regeneración y permite cambios localizados coherentes.
- **Inputs:** Clips con pelo/motion blur, oclusiones y una escena sintética con alpha/depth/efectos conocidos.
- **Outputs:** Capas RGBA/AOV, masks de validez, timebase y color, proyecto reeditable y reconstrucción compuesta.
- **Experimento y controles:** Comparar recompuesto con input/ground truth, error de borde alpha, flujo temporal y coste de cambiar solo una capa. Desactivar capas de efecto como ablación para mostrar sombras huérfanas.
- **Aceptación:** En fixture sin pérdida, composición lineal reproduce el original con error definido previamente (por ejemplo MAE<=1e-4 en float). Export/reimport preserva alpha y alineación. La capa de sombra no se confunde con alpha del objeto.
- **Límite que no se puede ocultar:** Casper público no es Lumiere original; una imagen multicapa no acredita video multicapa. No usar CorridorKey para eludir su licencia.

## P04. Iluminación y materiales temporales

Capacidades: C21, C22, C30. Trabajo: W16.

- **Ruta elegida:** V-RGBX y DiffusionRenderer con mappings explícitos; UniRelight/Light-X como comparadores; ID-V2V para restilización distinta.
- **Hipótesis:** Atributos temporales y guías de escena conservan estructura mejor que prompts de cambio de luz independientes por frame.
- **Inputs:** Objeto/fachada con logos, clip con cambios de exposición y escena sintética renderizada bajo dos HDRI autorizados.
- **Outputs:** AOVs válidos, materiales/luces editables o aproximados explícitamente, clip reluminado, máscaras de identidad y reporte temporal.
- **Experimento y controles:** Comparar con rerender de la escena, relight framewise y ruta temporal. Medir identidad/OCR, geometría visible, flicker compensado por flujo/oclusiones y error de luz/material en parámetros identificables.
- **Aceptación:** Cambiar una luz no invalida poses/transcripción. Los AOV inexistentes quedan unknown. Logos y texto protegidos conservan fuente o pasan validación explícita, no se aceptan por score estético.
- **Límite que no se puede ocultar:** Albedo/material/luz son ambiguos sin evidencia. No se promete recuperar roughness/refraction exacta desde un video cualquiera.

## P05. Reconstrucción y ajuste físico de activos

Capacidades: C09, C17, C20. Trabajo: W10, W17.

- **Ruta elegida:** TRELLIS.2 para inicialización; MapAnything para cámara; Mitsuba/Dr.Jit y DiffReg-PBIR para ajuste. OVOW extiende el caso temporal.
- **Hipótesis:** Separar geometría, material y luz y optimizar parámetros observables mejora consistencia multivista frente a una reconstrucción generativa única.
- **Inputs:** Producto con referencias autorizadas y, para benchmark, objeto sintético conocido con vistas train/heldout y parámetros perturbados.
- **Outputs:** Malla/transform/PBR/cámaras, regiones inferidas, trazas de optimización, export USD/glTF/Blender según soporte.
- **Experimento y controles:** Optimizar grupos identificables por etapas; comparar silueta/reproyección/material en vistas no usadas y medir sensibilidad a sombras/focal. Ablación sin ajuste físico y sin calibración.
- **Aceptación:** El activo es único y reutilizable entre cámaras. El ajuste debe mejorar su criterio predefinido heldout frente a inicialización o registrarse como resultado negativo sin promoción. Un parámetro no identificable permanece incierto.
- **Límite que no se puede ocultar:** No inventar caras ocultas como medidas. DiffReg requiere preparar patches Blackwell y licencias NC; Mitsuba CPU mantiene el ensayo verificable.

## P06. Óptica y cámara controlables

Capacidades: C05, C08, C15, C20. Trabajo: W11.

- **Ruta elegida:** Blender para trayectoria/proyección/visibilidad; geometría MapAnything/DA3 y guías Wan-Move; CameraAnything/WorldForge solo con gates resueltos.
- **Hipótesis:** Proyectar trayectorias y comprobar visibilidad antes de generar reduce error de cámara y alteraciones del producto/lugar.
- **Inputs:** Escena/cámara calibradas, objetivos de plano, obstáculos y trayectoria o intención (dolly/orbit/seguimiento).
- **Outputs:** Camera path, focal/FOV, constraints, proxy render y guías de depth/flow/puntos con correspondencia de frames.
- **Experimento y controles:** Trayectorias con objeto asimétrico y puntos conocidos; medir error de reproyección, violaciones de encuadre/colisión y preservación en traducción neural. Comparar prompt solo vs controles.
- **Aceptación:** Convención de ejes y sensor probada con fixture conocido; p95 reprojection de fixture <=2 px a resolución proxy fijada. Ninguna ruta atraviesa geometría sólida sin declararlo como efecto ficticio.
- **Límite que no se puede ocultar:** No transformar movimiento de un frame o texto de cámara en geometría métrica no observada. FOV desconocido no equivale a un lente por defecto.

## P07. Memoria de escena, identidad y retorno

Capacidades: C04, C06, C14, C17, C22, C23, C28. Trabajo: W17, W22.

- **Ruta elegida:** Memoria estructurada del estudio + StoryMem/Wan + AlayaWorld; OVOW para escenas temporales observadas.
- **Hipótesis:** Referencias ancladas a IDs/cámaras/tiempos reducen drift entre planos y al volver a un lugar frente a repetir el mismo prompt.
- **Inputs:** Guion multicámara con actor/producto y secuencia A→B→A; anclas de identidad y espacio.
- **Outputs:** Memoria de shots, mapas de anclas/cámaras, referencias elegidas por modelo, clip por plano y métricas de drift.
- **Experimento y controles:** Ablaciones sin memoria y con simple keyframe previo; comparación con negativos parecidos pero incorrectos. Separar drift de identidad, color, geometría y hechos del lugar.
- **Aceptación:** Producto y lugar tienen IDs y fuentes estables entre tres planos. Se respetan límites de referencias del motor. Volver a A usa anclas verificables, no solo parecido global del embedding.
- **Límite que no se puede ocultar:** Mundo generado permanece ficticio. HoloCine/ShotStream/WorldCrafter son comparadores condicionados, no reemplazos automáticos de una referencia exacta.

## P08. Lugares verificables y adquisición activa

Capacidades: C07, C08. Trabajo: W09, W11.

- **Ruta elegida:** Atlas offline del mismo GHCR + Commons/OSM/KartaView permitidos + MapAnything/DA3 + geometría y coverage del estudio.
- **Hipótesis:** Seleccionar vistas por cobertura y buscar la siguiente observación útil aumenta fidelidad de fondos frente a generar una ciudad por nombre.
- **Inputs:** Lugar/municipio/coords opcionales, vista real, cámara deseada y referencias con fechas/licencias.
- **Outputs:** Mapa observado/inferido, calibración/solape, candidatos de cámara, atlas bundle inmutable y gaps/next-best-view justificados.
- **Experimento y controles:** Nombres ambiguos, rumbo 0/180, vista explícita incompatible, municipio equivocado, imágenes sin EXIF y cambios de fachada. Reconstrucción de anclas con vistas heldout; contraste visual y reproyección.
- **Aceptación:** Bundle funciona sin red; cada vista usada existe y coincide con su hash. Strict_exact rechaza cámara sin evidencia suficiente. Imagen de un parque distinto dentro de 150 m no pasa solo por cercanía.
- **Límite que no se puede ocultar:** Meta de 200 lugares no permite fotos irrelevantes. Sin material suficiente no existe obligación de inventar una reconstrucción total; registrar bloqueo cuantificado y conservar ruta de cámara observada.

## P09. Rodaje de vehículos con dinámica

Capacidades: C15, C18, C19. Trabajo: W12.

- **Ruta elegida:** Chrono/PyChrono + escena/vehículo Blender + trayectoria/guías para Wan-Move y composición.
- **Hipótesis:** Controlar trayectoria/dinámica antes de la traducción neural produce frenado, apoyo y cámara más consistentes que pedir un coche circulando.
- **Inputs:** Activos de vehículo, carretera/escala, velocidad/maniobra, cámara y condiciones físicas supuestas.
- **Outputs:** Estados temporales (poses/velocidades/contactos), simulación cacheada, cámaras/pases, render proxy y shot final.
- **Experimento y controles:** Frenado recto, giro y irregularidad de terreno sobre escenarios sintéticos conocidos; comparar contacto/velocidad y target de trayectoria. Transferencia neural se evalúa por separado.
- **Aceptación:** Unidades y timebase coherentes entre Chrono y Blender, export visible y estado de contacto. Target de ruta comparado con centro/puntos visibles del vehículo y máscaras de oclusión.
- **Límite que no se puede ocultar:** No afirmar un neumático/motor real recuperados desde una foto. Física válida del proxy no acredita física del render neural si este cambia el movimiento.

## P10. Personas que actúan con escena y objetos

Capacidades: C11, C12, C13, C14, C18. Trabajo: W13.

- **Ruta elegida:** GVHMR/SAM3D Body + HY-Motion/MotionBricks + retargeting/contactos Blender + Wan-Animate. ReMoGen/InterMimic solo con gates concretos.
- **Hipótesis:** Explicitar apoyos, root motion y targets de contacto reduce deslizamiento/interpenetración frente a transferencia directa sin escena.
- **Inputs:** Video autorizado o acción textual, rig compatible, geometría de escena y objeto de interacción.
- **Outputs:** Motion library con licencias, retarget mapping, actuación con contactos, animatic y versión neural opcional.
- **Experimento y controles:** Caminar/parar, tomar/soltar un objeto y un tramo de baile. Comparar foot skate, penetración, joint limits y error de timing; comprobar invariantes de raíz/escala.
- **Aceptación:** En fixture sintético, pies de contacto permanecen en tolerancia fijada respecto a altura del personaje; no se usan body assets no autorizados. Cambiar avatar no cambia intención/tiempos.
- **Límite que no se puede ocultar:** SOMA/SMPL-X y esqueletos personalizados necesitan conversiones reales. Un entorno Isaac Gym antiguo que no corre en Blackwell es bloqueo, no un benchmark pasado.

## P11. Productos articulados y montaje mecánico

Capacidades: C09, C10, C11, C18. Trabajo: W10, W12.

- **Ruta elegida:** PartCrafter + jerarquía/pivotes propios + Blender/Chrono; PhysX-Anything condicional como propuesta de estructura.
- **Hipótesis:** Representar partes y límites explícitos permite abrir/cerrar/ensamblar con continuidad superior a clips independientes.
- **Inputs:** Producto, referencias de sus partes o fixture CAD sintético, articulaciones y secuencia.
- **Outputs:** Activos por parte, joints/limits, animación reversible, cámaras y renders con IDs persistentes.
- **Experimento y controles:** Caja articulada, producto con pieza deslizante y despiece; ground truth de joints/colisiones y secuencia inversa. Ablación que omite jerarquía para cuantificar penetración.
- **Aceptación:** Abrir y cerrar vuelve al estado inicial dentro de tolerancia; no excede joints; parts se conservan entre shots y export/import. Despiece inventado se identifica como conceptual.
- **Límite que no se puede ocultar:** El acceso no verificado a un checkpoint no paraliza la ruta con partes conocidas. No llamar ingeniería exacta a la articulación inferida.

## P12. Actuación facial editable y voz persistente

Capacidades: C16, C27. Trabajo: W14, W19.

- **Ruta elegida:** Audio2Face-3D + mapeo de rig Blender, Qwen3-TTS/IndexTTS e InfiniteTalk para salida neural; MARCUS/GTAvatar condicionales para captura de identidad.
- **Hipótesis:** Separar voz, actuación facial y render permite cambiar encuadre/luz sin rehacer identidad o sincronía.
- **Inputs:** Texto/audio y actor/voz autorizados, rig/morph targets y referencias por hablante.
- **Outputs:** Stems/texto/alineación, blendshapes/poses temporales editables, identidad persistente y render.
- **Experimento y controles:** Nombres del Aburrá, cifras y dos hablantes; cambiar voz, cámara o luz por separado. Medir timing labial, turnos, ASR/lexicon e identidad con positivos/negativos.
- **Aceptación:** La actuación facial puede reabrirse y cambiarse sin regenerar audio. Mapeo de morphs verificado; no simular compatibilidad universal. Target inicial de sincronía: retraso dentro de dos frames al fps declarado, sujeto a protocolo validado.
- **Límite que no se puede ocultar:** Gates de FLAME/Audio2Emotion se respetan. El fallback de rig manual es una ruta funcional distinta y se identifica; no acredita recuperación neural de una persona.

## P13. Sonido dirigido por causas y referencias

Capacidades: C25, C26, C27. Trabajo: W18, W19.

- **Ruta elegida:** ControlFoley + MMAudio/SAM-Audio + ACE-Step + Qwen3-TTS + mezcla FFmpeg; HunyuanVideo-Foley/Foley-Omni como comparadores.
- **Hipótesis:** Eventos y referencias explícitos mejoran sincronía/control frente a una sola generación de soundtrack indiferenciado.
- **Inputs:** Timeline de acciones/cortes, texto, música/referencia autorizada, posiciones/acústica si existen.
- **Outputs:** Stems de eventos/voz/música cuando la ruta los produce, beat map, mezcla/máster, créditos y versiones.
- **Experimento y controles:** Pasos/frenado/golpe musical con onset conocido; retirar una causa y solo su stem; comparar modelo sin referencia vs condicionado. Estimar lag y evaluar clipping/loudness.
- **Aceptación:** Onsets dentro de tolerancia (objetivo dos frames), mezcla sin clipping, sample rate y true peak medidos. Eliminar coche elimina su sonido asociado, conservando ambiente y voz. No anunciar stems que el modelo conjunto no genera.
- **Límite que no se puede ocultar:** Foley-Omni público limita duración; sostener calidad mediante montaje y stems es desarrollo propio, no soporte nativo de clips ilimitados.

## P14. Investigador autónomo con conocimiento acumulable

Capacidades: C01, C33, C34. Trabajo: W23, W24.

- **Ruta elegida:** Experiment registry + Optuna + métricas de geometría/audio/temporalidad + VBench2/VideoReward pertinentes + promoción por PR.
- **Hipótesis:** Experimentos con holdouts y coste total producen recetas transferibles mejores que escoger la muestra más bonita.
- **Inputs:** Familia de tareas, baseline fijada, hipótesis/intervención, datos permitidos, constraints y presupuesto autorizado.
- **Outputs:** Protocolo prerregistrado, trials con fallos, medidas por hardware, Pareto, recomendación y PR de receta/adaptador o resultado negativo.
- **Experimento y controles:** A/B de cuantización/tiling/steps/control y ablación; separar desarrollo/validación/test, bloquear contaminación y sesgo de juez. Ponderar calidad/tiempo/coste por output aceptado y reportar dispersión.
- **Aceptación:** Nunca promueve una muestra simulada o otra GPU como evidencia real. Reanudar no reinicia budget. Una variante con peor identidad no gana por score estético; una hipótesis negativa se cierra sin bucle infinito.
- **Límite que no se puede ocultar:** No entrenar modelos fundacionales ni destilar outputs con licencia que lo prohíba. El código se promueve desde desarrollo, no se instala en el pod en caliente.


