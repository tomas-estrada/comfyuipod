# Diseño y evaluación del agente de producción LangGraph

**Propósito.** Convertir evidencia de investigación normal de Consensus en criterios
prácticos para los flujos de ComfyUIPod: planificar y producir audio/video/3D con
ComfyUI, Blender, FFmpeg y motores auxiliares; reanudar trabajos; y entregar
artefactos verificables.

**Método y límite.** Se hicieron búsquedas normales (no Deep Research) en Consensus
sobre harnesses de agentes, interacción de agentes con herramientas, producción
multimodal y modelado 3D con Blender. Se consultaron los registros/abstracts de los
estudios citados, no una revisión sistemática de todos sus textos completos. Los
benchmarks generales y de creación son evidencia indirecta para ComfyUIPod. No se
encontró evidencia experimental que demuestre que LangGraph, por sí solo, mejora
el resultado frente a otro motor de orquestación en estas tareas.

## Hallazgos de investigación

1. **El harness es parte del sistema evaluado.** Harness-Bench reúne 106 tareas
offline y 5.194 ejecuciones, guardando artefactos, trazas, uso y salidas de
validadores. Reporta variación importante entre combinaciones modelo-harness y
fallos donde el razonamiento se separa de la retroalimentación de herramientas,
el estado del workspace o el contrato del resultado [1]. Esto favorece evaluar
la combinación concreta `modelo + grafo + herramientas + políticas + datos`, no
atribuir el éxito al modelo o a LangGraph aisladamente. Es un preprint.

2. **Una ejecución exitosa no mide fiabilidad.** En τ-bench, el agente interactúa
con usuario, herramientas y reglas de dominio; la evaluación compara el estado
final con el estado objetivo y propone `pass^k` para medir consistencia en varios
intentos. Los agentes de function calling evaluados quedaron por debajo de 50%
de éxito y por debajo de 25% en `pass^8` para retail [2]. Es un benchmark de
dominios distintos, pero el método de verificación por estado final y repetición
es transferible a los flujos de producción.

3. **La creación 3D mejora con estructura y ciclos de corrección verificables.**
SceneCraft separa la descripción en un blueprint/scene graph, traduce relaciones
espaciales a restricciones numéricas para Blender y usa la revisión visual de
renders para refinar; también reutiliza funciones de scripts [3]. En CodeGen-3D,
100 prompts se ejecutan con un protocolo común en Blender. Los enfoques
especializados e iterativos reportan 3–4% de fallos frente a 21–92% para modelos
generales de una sola pasada; la preferencia perceptual del juez no sigue siempre
la puntuación CLIP [4]. CodeGen-3D es una publicación reciente con evidencia
inicial; sus cifras no garantizan el mismo efecto en los motores y escenas de
ComfyUIPod.

4. **Las tareas de postproducción del mundo real siguen siendo difíciles.**
AgenticVBench usa 100 tareas de producción aportadas por 20 profesionales,
combinando verificadores programáticos y rúbricas expertas. El mejor stack
evaluado apenas supera 30%; además, el harness altera conducta, puntuación y
fallos [5]. La lección para ComfyUIPod es conservar evaluación técnica y juicio
visual/editorial como señales diferentes; un MP4 que se renderiza no prueba que
cumpla el brief.

5. **Las limitaciones de planificación y seguimiento de instrucciones deben
medirse explícitamente.** AgentBench evalúa ocho entornos interactivos y señala
razonamiento a largo plazo, toma de decisiones y seguimiento de instrucciones
como fuentes habituales de fallos [6]. Por tanto, separar fases y registrar las
decisiones permite localizar errores; no basta con inspeccionar el texto final.

## Estado observado en ComfyUIPod

- `studio/comfy_studio/orchestration/graph.py` tiene un `ProductionGraph` que
  compila un `StateGraph` de LangGraph si la biblioteca está instalada y ofrece
  un runner stdlib equivalente para entornos mínimos. Los nodos reciben y
  devuelven `ProductionState`; el resultado del grafo se aplica al estado vivo.
- `studio/comfy_studio/orchestration/state.py` ya modela proyecto, secuencias,
  shots, capas, aceptación, reintentos escalonados, gasto reportado y lineage
  serializable. El modelo advierte que el gasto cero no equivale a coste real
  cuando el motor no reporta coste.
- `studio/comfy_studio/orchestration/checkpointer.py` implementa persistencia
  SQLite propia. `graph.py` registra nodo en vuelo/siguiente nodo y recupera
  ejecuciones; la recuperación de efectos externos depende también de claves
  idempotentes y conciliación en los adaptadores.
- La evidencia de integración existente cubre LangGraph ejecutando la ruta de
  producción con SQLite y reinicio bajo fixture CPU. No demuestra calidad de
  generación con GPU real, conectividad de proveedores reales ni cumplimiento
  audiovisual del brief en producción.

## Recomendaciones para el flujo

1. Mantener un grafo de flujo de trabajo explícito para las fases conocidas y
   usar el LLM para proponer decisiones acotadas. Para esta suite, las fronteras
   naturales son: interpretar brief → plan/scene graph → validar plan y recursos
   → ejecutar motores → validar artefactos → replanificar con límites → ensamblar
   y empaquetar. Evitar delegar cada paso determinista a un agente autónomo.
2. Mantener en el estado datos compactos y tipados: IDs, rutas relativas
   controladas, hashes, parámetros, estado de aceptación, procedencia, coste
   medido y resultados de validación. Referenciar los archivos multimedia en
   almacenamiento; no serializar imágenes, audio o video enteros dentro del
   checkpoint.
3. Tratar la respuesta del modelo como propuesta. Los validadores deterministas
   deben comprobar esquema, dependencias, enlaces ComfyUI, límites de duración y
   resolución, presupuesto y existencia/hash de entradas antes de ejecutar o
   aceptar artefactos. Para 3D, validar que Blender abre el archivo, que la escena
   y cámara cumplen restricciones medibles y guardar vistas de revisión; para
   video/audio, comprobar streams, duración, dimensiones, sincronía y EDL contra
   el contrato del brief.
4. Hacer que todo efecto externo sea seguro al reanudar: clave de idempotencia
   estable por operación y versión de entradas, registro de estado antes/después,
   reconsulta al motor tras una respuesta perdida y límites explícitos de
   reintentos/tiempo/coste. Reintentar fallos transitorios; errores de esquema,
   licencia, política o entrada deben convertirse en fallos accionables, no en
   reintentos ciegos.
5. Adjuntar a cada artefacto su linaje: hashes de brief/entradas, modelo y revisión,
   plantilla/grafo, parámetros, versión de herramienta, operaciones realizadas,
   validadores y resultados. Invalidar y repetir solo descendientes afectados
   cuando cambie una entrada.
6. Conservar el mismo contrato de grafo en LangGraph y el fallback. Probar
   equivalencia de estado/efectos entre ambos; verificar en el contenedor de
   Studio que LangGraph está instalado y que la ruta efectiva realmente es la
   esperada. El fallback facilita fixtures; no sustituye una comprobación de la
   ruta de producción.

## Plan de evaluación propuesto

Crear un conjunto fijo derivado de D01–D05 y casos de herramientas: brief ambiguo,
proyecto multimodal mixto, escena 3D con restricciones, fallo/timeout después de
un envío al motor, reinicio entre nodos, replan local y entrega/paquete. Usar
fixtures CPU para repetir con rapidez y reservar afirmaciones de motores/modelos
para ejecuciones reales separadas.

Para cada caso guardar estado inicial/final, artefactos y hashes, traza por nodo,
llamadas y respuestas de herramientas, validadores, uso de tokens/tiempo/cómputo,
reintentos y errores. Reportar como mínimo:

- éxito verificable del artefacto y cumplimiento de cada requisito del brief;
- éxito completo por intento y `pass^k` en ejecuciones repetidas;
- fallos y duplicados tras interrupción, timeout o reanudación;
- calidad técnica automatizable separada de puntuación visual/editorial;
- uso de CPU/RAM/VRAM, latencia, llamadas/coste real cuando el proveedor lo
  reporta, y eficiencia de recuperación;
- violaciones de política, referencias a evidencia y completitud del lineage.

Comparar cambios con modelo, prompt, tareas, límites y recursos fijados. Ablacionar
una decisión cada vez (validadores, refinamiento visual, RAG, reintentos o
paralelismo) y validar en casos retenidos. No tomar un puntaje de CLIP, un único
video exitoso ni una demo de varios agentes como prueba suficiente.

## Referencias

[1] Yilun Yao et al. (2026), *[Harness-Bench: Evaluating Agent Harnesses across
Models and Realistic Workflows](https://consensus.app/papers/yao-tan-harnessbench-evaluating-agent-harnesses-across/w:581160d0-eb72-4eb1-adfc-8b8262be359a/?utm_source=chatgpt)*,
arXiv preprint, 78 citas según Consensus. DOI: 10.48550/arxiv.2605.27922.

[2] Shunyu Yao, Noah Shinn, Pedram Razavi y Karthik Narasimhan (2024),
*[τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World
Domains](https://consensus.app/papers/yao-shinn-τbench-a-benchmark-for-toolagentuser-interaction/w:1d4d6232-f216-4783-97dc-22637ed23004/?utm_source=chatgpt)*,
arXiv preprint, 1.669 citas según Consensus. DOI: 10.48550/arxiv.2406.12045.

[3] Ziniu Hu et al. (2024), *[SceneCraft: An LLM Agent for Synthesizing 3D Scene
as Blender Code](https://consensus.app/papers/hu-i̇şcen-scenecraft-an-llm-agent-for-synthesizing-3d-scene/w:812219e4-03eb-4e62-8183-d82c186436ad/?utm_source=chatgpt)*,
arXiv preprint, 181 citas según Consensus. DOI: 10.48550/arxiv.2403.01248.

[4] Hao Ji, Kotha Aditya, Sebastian Escalante y Yunjian Qiu (2026), *[CodeGen-3D:
A Benchmark for Evaluating LLMs in Zero-Shot and Iterative 3D Modeling in
Blender](https://consensus.app/papers/ji-aditya-codegen3d-a-benchmark-for-evaluating-llms-in/w:646ef985-e982-477c-a213-e464b87fa64c/?utm_source=chatgpt)*,
IEEE Access 14, 18181–18192, 1 cita según Consensus. DOI:
10.1109/ACCESS.2026.3654948.

[5] Zongheng Cao, Yi Zheng, Rui Song y Xinyu Hu (2026), *[AgenticVBench: Can AI
Agents Complete Real-World Post-Production
Tasks?](https://consensus.app/papers/cao-zheng-agenticvbench-can-ai-agents-complete-realworld/w:3180de3c-7d0c-4868-936f-368273f9927a/?utm_source=chatgpt)*,
arXiv preprint, 9 citas según Consensus. DOI: 10.48550/arxiv.2605.27705.

[6] Xiao Liu et al. (2023), *[AgentBench: Evaluating LLMs as
Agents](https://consensus.app/papers/liu-yu-agentbench-evaluating-llms-as-agents/w:a2edda0f-3ff6-4544-98ae-96de4ece60b1/?utm_source=chatgpt)*,
arXiv preprint, 1.574 citas según Consensus. DOI: 10.48550/arxiv.2308.03688.
