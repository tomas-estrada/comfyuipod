# Revisión y decisiones de investigación

Fecha: 2026-11-05 UTC. Este archivo describe hechos leídos, reproducciones previas de CPU y propuestas de diseño por separado. Ninguna capacidad neuronal ha sido validada por este trabajo en una GPU.

## Punto de partida verificable

- Repo: https://github.com/tomas-estrada/comfyuipod
- main inspeccionada: `b54195f0976d6c57b0ee04962e3059219db55d57`, PR #62 integrada.
- Workflow posterior a merge: https://github.com/tomas-estrada/comfyuipod/actions/runs/41964177024, completado correctamente al consultar.
- Árbol recursivo: 395 entradas, sin truncamiento. Esto no significa que cada función del árbol haya sido ejecutada.
- Existen API FastAPI, percepción, EDL/FFmpeg, panel, herramientas de agente, presupuestos, memoria, motores y plantillas. Extenderlos evita reconstruir una segunda aplicación.
- `orchestration/state.py`, `planner.py`, `executor.py`, `adapters.py`, `graph.py` y `checkpointer.py` ya proporcionan una base de producción. El comentario de `graph.py` conserva “stub-first”: comprobar nodos y ruta efectivos, no juzgar el estado actual solo por ese comentario.
- `agent/llm.py` ya abstrae proveedores. La evolución debe conservar esa separación.
- PR #58 de UI y #59 de documentación estaban abiertas. Inspeccionar estado y alcance actuales; #59 puede estar absorbida en #62. No cerrar ni duplicar trabajo por su número sin comparar commits.

## Reparaciones de prioridad P0

Los casos siguientes se reprodujeron previamente con módulos exactos de esta misma main; al no cambiar main siguen siendo relevantes. El worker debe reproducirlos de nuevo antes de editar y conservar pruebas que fallen en la versión defectuosa.

| Hallazgo | Código de referencia | Resultado observado | Reparación requerida |
| --- | --- | --- | --- |
| Caché corrupta aceptada | `asset_acquire._acquire_one`, alrededor de L375; `capabilities._asset_need`, L247 | Archivo existente de 7 bytes aceptado frente a tamaño esperado 26 y hash distinto | Comprobar revisión, tamaño e integridad antes de “present/skip”; diferenciar corrupt/staging/verified |
| Pin ignorado por CLI | `asset_acquire.py`, L434 | argv de `hf download` sin `--revision` | Misma revisión inmutable y archivos exactos en todas las rutas |
| Publicación parcial EXDEV | `asset_acquire._move`, L115 | Fallo durante copia deja 1 MiB parcial en destino de 2 MiB y destruye destino previo | Temporal en filesystem de destino, verificación, fsync y replace atómico; conservar versión previa |
| Simulación promovida | `projects.recipe_for_template`, L2264 | Una muestra `simulated=True` produce `measured=True, fits=True` | Tipar origen de medida y excluir simulados de selección/validación |
| Recetas incompatibles agregadas | misma función y llamada desde `capabilities.resolve`, L361 | Mezcla por template_id sin resolución/GPU/perfil; etiquetas engañosas | Clave completa de receta/entorno y consulta compatible |
| Disco calculado sobre catálogo | `scripts/storage_requirement.py` y consumidores strict/helper | 163,71 GB de task models + 17,19 GB bootstrap antiguo; start gate 63 GB y mínimo 248 GB pese a cero bootstrap real | Separar disco instalado, arranque, adquisición de tarea, retención y staging |
| Recursos declarados como reales | `hardware_profile.from_env` | Reservas nominales 94 GB VRAM/120 GB RAM sin acotar por GPU/cgroup disponibles | Intersección entre perfil solicitado, hardware real, uso y reservas |

No se ha demostrado todavía exclusión global de GPU entre todos los motores. La cola de ComfyUI o un semáforo de dos solicitudes no prueban exclusión con Blender, audio o inferencia propia. W03 exige un ensayo multiproceso y evidencia del propietario de cada lease.

Las líneas son orientativas para el SHA indicado. Si cambió el código, localizar símbolos con rg y verificar el caso, sin restaurar errores antiguos ni duplicar arreglos.

## Atlas: avances reales y trabajo restante

PR https://github.com/tomas-estrada/comfyuipod/pull/63, head consultada `c12618815c5e7181bbb69e4ce9d249a0075fd3c4`.

Su manifiesto declara 10.209 lugares de catálogo, 152 con vistas usables, 1.203 referencias totales, 612 referencias/vistas usables, 168 escenarios y cero producciones. Estos son contadores declarados por el catálogo, no un muestreo independiente de exactitud. No llamar a eso “10.209 localizaciones reconstruidas”.

La versión actual mejora respecto a revisiones anteriores: resolución explícita de lugar, detección de rumbo incompatible, validación de hashes y un adaptador que comprueba entradas de imagen. No repetir como actuales los errores que ya resolvió.

Persisten riesgos concretos observables en la lectura:
- `prepare_shot` con `view_id` obtiene limitaciones de `select_view` pero asigna `chosen=view`; verificar que las limitaciones y restricciones pertenezcan a la vista realmente elegida, no a otra.
- `select_view` trata heading/fov incompatibles como duros, pero distancia excesiva y calibración desconocida pueden acompañar un status ok. Para un modo de lugar exacto hace falta un contrato más estricto.
- La preparación automática usa selección activa o cobertura cuando no recibe place explícito. Probar que el lugar nombrado en el prompt llega desde el planner hasta esta llamada y no queda sustituido por la selección anterior.
- La ruta de plantilla sin entrada de imagen ahora rechaza el fondo. Eso evita ignorarlo, pero aún debe existir una receta alternativa real que lo consuma y preserve su geografía.
- Un nombre OSM, una miniatura y una foto real no equivalen a geometría multivista ni a una vista nueva factual.

La cobertura fotográfica declarada es desigual: Medellín 451 referencias, Bello 55, Itagüí 33, Envigado 22, Copacabana 22, Sabaneta 20, Caldas 7 y La Estrella 2. La adquisición debe incrementar cobertura válida en municipios poco representados, no inflar contadores con geotags genéricos.

## Kiro Crew: usar sus mecanismos reales

Código consultado: https://github.com/kirodotdev/KiroCrew/tree/4a546774f478c27058d61e7005ca931fd01b1c97

- `kirocrew-conductor` mantiene objetivo/ítems; `kirocrew-worker` recibe trabajo y reporta.
- El work ledger contiene contratos de aceptación definidos antes del dispatch; un worker no escribe su propio veredicto.
- Orden documentado: create → bind → seed. Herramientas: work_ledger_record/read, work_brief/report.
- Kinds nativos leídos: pr_checks, pr_merged, edition_check, file, human_approval. No hay un kind genérico para ejecutar comandos arbitrarios: pruebas semánticas van en CI o en un mecanismo instalado y comprobado.
- Los tests verdes de una PR no significan que el requisito esté cubierto; el conductor debe inspeccionar que el diff y los checks relevantes pertenecen a ese ítem.
- La coordinación de trabajos independientes no permite despachar dependencias todavía inexistentes como si ya estuvieran resueltas.
- Session ledger, checkpoints y eventos evitan repetir contexto y hacer polling continuo.
- La selección de agente no depende del texto del spec. Dashboard/conductor y Task Runner son rutas distintas, con permisos y límites de concurrencia distintos.
- En Task Runner, `require_verification=true` evita aceptar pasos con pruebas no configuradas o saltadas. No añadir `force_approval` rutinario: este paquete ya autoriza PR/merge/build candidatos; respetar bloqueos de permisos reales sin burlarlos.
- La vista Crew Mode puede estar oculta hasta activar Feature Previews → Ultra Mode. Esto es una opción de la interfaz de desarrollo, ajena al nombre Ultra del estudio.

## Cambios de disponibilidad que afectan al diseño

### LTX-2.5 como base audiovisual principal

El repo Lightricks/LTX-2 se consultó en `c1b4675863c07c59c92e502446cccdaedbe5889c`. La documentación recomienda LTX-2.5, distingue la ruta distilled de DFR y publica Retake/Dub It entre los pipelines.

El encoder fino es un componente específico: no sustituirlo por un modelo de texto con nombre parecido. DFR, detailing IC-LoRA y upscale temporal son componentes por receta. La resolución de ejemplo 3840×2176 requiere un acabado explícito si se entrega 3840×2160. Código y pesos tienen términos distintos. Todo esto requiere medición posterior; la existencia de una ruta 4K no prueba su consumo máximo en esta GPU.

### MiniMax-H3: una alternativa condicional

La documentación oficial separa H3-Base local (768p por lado corto) de H3-Context-IR y H3-Regenerate-2K hospedados, no incluidos en la entrega abierta consultada. No depender de esas APIs para cerrar una función gratuita.

La licencia excluye territorios publicados y restringe uso de outputs/pesos para mejorar modelos ajenos a H3. Evaluar el lugar real de ejecución y los términos, no solo que el usuario esté en Colombia. No usar H3 como teacher de una destilación transversal sin permiso. El contrato del estudio puede efectuar comprensión previa propia con un LLM permitido y acabado 4K con un upscaler local. No es reproducción de los módulos cerrados originales.

### HyperFrames: separar local de cloud

Código Apache-2.0, revisión consultada `7862e271d16baa9052c78d3b85535ebc725cd136`. El renderer HTML, medidas y herramientas locales son útiles. Su CLI también incluye autenticación, fuentes HeyGen con facturación y reverse-any-video vinculado a plan/sesión. En ciertos estados el reverse start sin login/offline conserva el probe; eso no convierte todo su workflow cloud en gratuito.

La receta esencial referencia→montaje se implementa localmente con FFmpeg, ASR, OCR, detección de cortes/ritmo, estimación de trayectorias y el proveedor LLM/VLM del estudio. Exporta un proyecto editable. No activar publish, cloud render, fallback HeyGen/Codex o compras en segundo plano.

### OpenCode: aprovechar código, adaptar el dominio

Repo anomalyco/opencode, revisión consultada `09dcb47861fe9253814dc85b12f31a5b2691eda6`, MIT. `packages/app/package.json` muestra Solid/Vite y dependencias de client/schema/session-ui/UI propios. No es una interfaz HTTP genérica lista para conectar a LangGraph.

Ruta elegida: reutilizar componentes y patrones MIT dentro del mismo repo, mantener avisos y crear StudioClient tipado sobre la API/SSE propia. No incorporar por conveniencia un servidor de coding con shell ilimitada ni emitir eventos falsos del SDK. Conservar el panel actual hasta pasar pruebas de paridad. Assets locales, sin CDN.

### Blackwell exige revisión de kernels, no solo mucha VRAM

Varias instrucciones upstream siguen en Torch 2.1/2.5/2.6 y CUDA 11.8/12.4. PyTorch introdujo soporte Blackwell y ruedas CUDA 12.8 en 2.7; eso es una referencia histórica, no una orden de fijar toda Ultra a esa versión. Elegir combinaciones actuales compatibles por motor, registrar arquitectura real y compilar/extensiones durante build. Un motor que importa en CPU no acredita sus kernels CUDA.

La matriz Cosmos distingue precisión, memoria por dispositivo, memoria host y recetas de offload. Recomienda Nano por defecto en RTX PRO 6000 y reservar Super para necesidades explícitas. No extrapolar requisitos NIM a una implementación propia ni confundir GB nominales con GiB. Cosmos es una ruta experimental adicional para transfer/control y world reasoning; su modo o modelo debe tener una tarea y licencia concretas.

## Decisiones para no convertir el laboratorio en una colección de demos

1. Un generador audiovisual principal y Wan para controles específicos. Alternativas solo si aportan una dimensión de calidad, edición, física o coste distinta.
2. Blender/Chrono/Mitsuba aportan controles, verdad sintética y simulación; el generador neural traduce o refina donde sea útil. La entrega sigue siendo 2D.
3. Geometría y cobertura reales para lugares exactos; mundos generados son activos ficticios separados.
4. VOID/omnimatte/relight no comparten automáticamente masks, cameras, timebase o materiales. El contrato y las conversiones son trabajo propio obligatorio.
5. No intentar entrenar desde cero un generador fundacional. Primero optimizar recetas; LoRA/adaptadores pequeños cuando una hipótesis y datos autorizados lo justifiquen.
6. Capacidad investigadora = hipótesis + intervención + baseline + evaluación independiente + evidencia + promoción/rollback, no una tarea permanente de buscar papers.
7. VBench y jueces visuales aportan dimensiones limitadas. No son prueba de exactitud física ni predictores fiables de viralidad.
8. Un programa no se cierra con enlaces, un endpoint vacío o un PNG de portada. Se exige la ruta real hasta API, planificador, ejecución, artifacts y pruebas pertinentes.

## Avances propios propuestos por este paquete

Estas combinaciones son propuestas de desarrollo, no afirmaciones de novedad académica mundial:
- Compilador de intención a constraints y un grafo causal de escena/montaje/sonido.
- Reparación contrafactual que compare la escena intervenida con ground truth sintético y preserve partes no afectadas.
- Adquisición de la siguiente vista útil del atlas según incertidumbre/cobertura, dentro de fuentes gratuitas permitidas.
- Política multi-fidelidad: ensayar en proxies, medir incertidumbre, refinar solo intervalos fallidos.
- Biblioteca Pareto de recetas condicionada por hardware/escena y contador de fallos real.
- Investigador con dataset/holdout y protocolos prerregistrados que produce PRs de adaptadores o presets verificables; ninguna auto-modificación del pod.

## Infraestructura y costes ocultos

Una sola imagen con docenas de entornos puede superar el disco de los runners gratuitos. No basta con prunear indiscriminadamente. W07 debe medir un build representativo, estimar el seleccionado completo y elegir reutilización compatible de dependencias, locks, etapas intermedias y caché inmutable. No eliminar aislamiento a costa de incompatibilidad, ni enviar al arranque lo que no cupo en build.

Si la infraestructura autorizada no alcanza, demostrarlo con números y dejar un bloqueo de release. No contratar runners ni reducir funcionalidades obligatorias en silencio. Publicar la imagen es parte de la evidencia pre-GPU; un Dockerfile sin build no basta.

