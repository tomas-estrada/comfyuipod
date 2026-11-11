# Conduct a comprehensive, evidence-grounded research investigation for the open-s

**Question:** Conduct a comprehensive, evidence-grounded research investigation for the open-source ComfyUIPod project (a containerized creative media pod with a Python FastAPI Studio, an agent loop with a large tool belt, MCP server, orchestration graph/planner/executor, GPU leases, engine registry/adapters, ComfyUI and other image/video/audio/3D engines, Atlas/media retrieval, FFmpeg/Remotion, and a user-requested goal of stronger agentic deployment and use of 3D models/tools). Research current peer-reviewed evidence and foundational/strong empirical papers on: (1) reliable agentic deployment/orchestration of tools and heterogeneous engines in containers/pods (planning, execution, retries, cancellation, resource budgets, artifact provenance/lineage, evals); (2) MCP or comparable tool protocols, interoperability, security boundaries, least privilege, prompt injection/tool output handling, sandboxing; (3) agents that create/use 3D models/assets (text/image-to-3D, 3D scene understanding/editing, Blender/code/API tool use, geometry/motion/render pipelines, evaluation and human review); (4) multimodal creative workflows combining video/audio/3D engines and how to validate outputs, grounding, provenance, and failure states. Identify approaches with evidence of improved reliability, quality, safety or throughput, plus limitations/negative or mixed results. Synthesize a prioritized set of concrete improvement opportunities suitable for this repository, separating changes verifiable with CPU fixtures from those requiring actual GPU/quality measurements. Avoid product/vendor marketing and do not claim the papers directly validate this specific repo. Prefer systematic reviews, benchmark papers, and strong empirical studies; include relevant 2020–2026 work and seminal earlier work where useful. Deliver a cited synthesis with exact paper references, study methods/findings/limitations, and an actionable ranked recommendation list mapped to this project architecture.

# Evidence-Grounded **ComfyUIPod Improvements**

ComfyUIPod’s strongest improvement opportunity is **verified, permission-scoped execution with persistent project state**, followed by bounded 3D authoring and multimodal quality checks—not simply more agents or engines.

The recommendations below map evidence to the architecture described in the request. They are **design proposals, not findings from inspecting or benchmarking this repository**. Supplied records mix peer-reviewed publications, preprints, and reports; their publication metadata and reported results have not been independently verified.

## Execution and Deployment Reliability

**Completion must mean validated artifacts, not successful tool calls.** *DeployBench: Benchmarking LLM Agents for Research Artifact Deployment* evaluated 51 deployments with hidden experiment-specific checks: pass rates were 7.8–51.0%, and 102 of 181 failures involved premature self-stopping against weaker completion criteria [[1.1]](https://consensus.app/papers/wang-qian-deploybench-benchmarking-llm-agents-for-research/w:da607bfe-ff77-4007-abaa-33f7eb684b20/) [[1.2]](https://consensus.app/papers/wang-qian-deploybench-benchmarking-llm-agents-for-research/w:da607bfe-ff77-4007-abaa-33f7eb684b20/). *FDE-Bench: Evaluating LLM Agents for Deployment Environment Configuration* instead rebuilds submitted declarative artifacts in pristine environments and gates success through build, readiness, behavior, and conformance checks [[2.1]](https://consensus.app/papers/ding-zhan-fdebench-evaluating-llm-agents-for-deployment/w:f737bd97-4242-4345-b6c5-f19d33698a53/) [[2.2]](https://consensus.app/papers/ding-zhan-fdebench-evaluating-llm-agents-for-deployment/w:f737bd97-4242-4345-b6c5-f19d33698a53/). CSR-Bench contributes repository-aware, iterative deployment-script generation, but its supplied findings are preliminary rather than a quantified reliability guarantee [[3.1]](https://consensus.app/papers/xiao-wang-csrbench-benchmarking-llm-agents-in-deployment-of/w:4c110a8b-4b3c-4f23-9d8a-8d529c680181/) [[3.2]](https://consensus.app/papers/xiao-wang-csrbench-benchmarking-llm-agents-in-deployment-of/w:4c110a8b-4b3c-4f23-9d8a-8d529c680181/).

**Evaluate the entire model–runtime configuration.** Harness-Bench recorded 5,194 trajectories across 106 sandboxed tasks and found substantial configuration-dependent differences in completion, efficiency, and failure behavior [[4.1]](https://consensus.app/papers/yao-tan-harnessbench-evaluating-agent-harnesses-across/w:581160d0-eb72-4eb1-adfc-8b8262be359a/) [[4.2]](https://consensus.app/papers/yao-tan-harnessbench-evaluating-agent-harnesses-across/w:581160d0-eb72-4eb1-adfc-8b8262be359a/). UniACE’s more than 400,000 rollouts produced ranking reversals when execution conditions changed, while explicitly excluding multimodal coverage from its current scope [[5.1]](https://consensus.app/papers/zhu-li-uniace-a-unified-framework-for-evaluating-llm/w:b19d1f1b-f47d-4934-8a26-290195f2039f/) [[5.2]](https://consensus.app/papers/zhu-li-uniace-a-unified-framework-for-evaluating-llm/w:b19d1f1b-f47d-4934-8a26-290195f2039f/).

### Recovery and State

| Approach | Method and Finding | Important Limitation |
|---|---|---|
| Repeated execution | τ-bench measures final database state and pass^k consistency | Retail pass^8 below 25% in tested agents [[6.1]](https://consensus.app/papers/yao-shinn-τbench-a-benchmark-for-toolagentuser-interaction/w:1d4d6232-f216-4783-97dc-22637ed23004/) [[6.2]](https://consensus.app/papers/yao-shinn-τbench-a-benchmark-for-toolagentuser-interaction/w:1d4d6232-f216-4783-97dc-22637ed23004/) |
| Fault injection | ReliabilityBench tests perturbations, timeouts, rate limits, and schema drift | 1,280 episodes across four non-media domains [[7.1]](https://consensus.app/papers/gupta-reliabilitybench-evaluating-llm-agent-reliability/w:6124c8b3-6f88-49df-badf-3c5442b0c026/) [[7.2]](https://consensus.app/papers/gupta-reliabilitybench-evaluating-llm-agent-reliability/w:6124c8b3-6f88-49df-badf-3c5442b0c026/) |
| Targeted recovery | Self-Healing Agentic Orchestrators reports 98.8% success versus 94.5% retry-only | Controlled 100-task fault benchmark [[8.1]](https://consensus.app/papers/babu-agrawal-selfhealing-agentic-orchestrators-for-reliable/w:8fdf7d60-0911-4354-a92c-5437f5c0cf1e/) [[8.2]](https://consensus.app/papers/babu-agrawal-selfhealing-agentic-orchestrators-for-reliable/w:8fdf7d60-0911-4354-a92c-5437f5c0cf1e/) |
| Compensable workflows | SagaLLM combines persistent memory, checkpoints, compensation, and validation | Four selected planning problems, not media deployment [[9.1]](https://consensus.app/papers/chang-geng-sagallm-context-management-validation-and/w:1f2c9dc4-5725-49f4-820a-27d4ce80a542/) [[9.2]](https://consensus.app/papers/chang-geng-sagallm-context-management-validation-and/w:1f2c9dc4-5725-49f4-820a-27d4ce80a542/) |
| Root-cause debugging | AgentDebug reports up to 26% relative task-success improvement | ALFWorld, GAIA, and WebShop transfer remains untested [[10.1]](https://consensus.app/papers/zhu-zijia-where-llm-agents-fail-and-how-they-can-learn-from/w:8e7be933-cc89-4a59-a700-97fef35178ca/) |

**Figure 1:** Recovery methods, measured findings, and transfer limitations

State-dependent tool tests are necessary because stateless invocation benchmarks omit dependencies between actions [[11.1]](https://consensus.app/papers/lu-holleis-toolsandbox-a-stateful-conversational-interactive/w:22ea42bf-00e5-4c86-a90d-23bad262fb0e/) [[11.2]](https://consensus.app/papers/lu-holleis-toolsandbox-a-stateful-conversational-interactive/w:22ea42bf-00e5-4c86-a90d-23bad262fb0e/). Shared Studio–agent editing also warrants concurrency tests: τ²-bench found substantial degradation when users and agents both modified shared state [[12.1]](https://consensus.app/papers/barres-honghua-τ2bench-evaluating-conversational-agents-in-a/w:5463a1e9-53af-4457-a61b-a6c6a0640383/). Long-horizon MCP benchmarks reinforce the gap: Toolathlon’s best tested system completed only 38.6% of 108 realistic tasks [[13.1]](https://consensus.app/papers/junlong-wenshuo-the-tool-decathlon-benchmarking-language-agents/w:2f115fc0-a4bd-49f1-abbb-5b4fedc0ef8e/) [[13.2]](https://consensus.app/papers/junlong-wenshuo-the-tool-decathlon-benchmarking-language-agents/w:2f115fc0-a4bd-49f1-abbb-5b4fedc0ef8e/).

### GPU Resource Control

Measured scheduling improves serving performance, but published gains are workload-specific.

| Evidence Anchor | Measured Finding | Transfer Boundary |
|---|---|---|
| Clockwork | Production-trace evaluation met 100-ms targets for 99.9999% of requests | Predictable DNN inference, not arbitrary creative engines [[14.1]](https://consensus.app/papers/gujarati-karimi-serving-dnns-like-clockwork-performance/w:b224e2dd-a1b9-4bf6-8907-ba755a2d48e8/) |
| iGniter | Interference-aware provisioning saved up to 25% cost | Four DNN models and datasets [[15.1]](https://consensus.app/papers/xu-xu-igniter-interferenceaware-gpu-resource/w:0842d787-0caf-490c-ac0a-dc7d21a6d107/) |
| InferFair | Up to 25.6% higher goodput | Heterogeneous model-serving experiments [[16.1]](https://consensus.app/papers/peng-peng-inferfair-towards-qosaware-scheduling-for/w:a9222a37-de2d-4571-99ab-0005012f75e8/) |
| GPU spatial partitioning | Average throughput increased 102.6% while satisfying SLOs | Requires compatible partitioning mechanisms [[17.1]](https://consensus.app/papers/choi-lee-multimodel-machine-learning-inference-serving-with/w:768a2778-fff9-4269-98ac-e75693d70202/) [[17.2]](https://consensus.app/papers/choi-lee-multimodel-machine-learning-inference-serving-with/w:768a2778-fff9-4269-98ac-e75693d70202/) |
| Sia | Reduced completion time and GPU-hours across heterogeneous clusters | Elastic training workloads, not single-pod inference [[18.1]](https://consensus.app/papers/subramanya-arfeen-sia-heterogeneityaware-goodputoptimized-mlcluster/w:582738d8-ee92-4399-9738-79ce2e1bb595/) |

**Figure 2:** GPU scheduling findings and relevant workload boundaries

HASE reports 51% lower makespan for CNN inference, whereas OLTunes reports 61% greater throughput with configuration-dependent accuracy trade-offs [[19.1]](https://consensus.app/papers/chen-jiang-hase-hardwareaware-scheduling-for-inference-tasks/w:e8b09c54-67d6-41d7-be36-13ae7faa426a/) [[20.1]](https://consensus.app/papers/kim-ha-oltunes-online-learningbased-autotuning-system-for/w:ab39519f-27c6-467f-ae60-dadf1361970d/) [[20.2]](https://consensus.app/papers/kim-ha-oltunes-online-learningbased-autotuning-system-for/w:ab39519f-27c6-467f-ae60-dadf1361970d/). TokenFlow and prefill–decode control target autoregressive LLM execution, so their mechanisms should not be assumed compatible with diffusion or Blender workloads [[21.1]](https://consensus.app/papers/zeng-huang-tokenflow-tokenlevel-gpu-sharing-and-adaptive/w:91f01a85-ac3b-4deb-972a-01d9218a5fbc/) [[22.1]](https://consensus.app/papers/lin-ding-largescale-llm-inference-with-heterogeneous/w:c857b481-34e1-4489-894d-170a8d005419/). DriftSched identifies workload-estimation drift; TwinSched uses simulated interference-aware placement; Paella requires compiler–scheduler co-design [[23.1]](https://consensus.app/papers/palaniappan-driftsched-adaptive-qosaware-scheduling-under/w:a5febb5c-1676-40e5-a546-ca07826bea14/) [[24.1]](https://consensus.app/papers/xia-huang-coordinating-training-and-inference-in/w:533e3980-774b-4613-8c5a-57c46dc361d8/) [[25.1]](https://consensus.app/papers/ng-demoulin-paella-lowlatency-model-serving-with/w:17c3e535-f3c1-4665-88fc-93c9ba955d12/). Hadar likewise concerns training, while latency-aware agent scheduling explicitly models residency and loading costs [[26.1]](https://consensus.app/papers/sultana-pakka-resource-heterogeneityaware-and/w:ee18c4e8-d4a0-4d00-be5c-a569085e1ffc/) [[27.1]](https://consensus.app/papers/wang-zhang-latencyaware-orchestration-for-multiagent-llm/w:d45cc92b-4779-4079-8570-775c54fac0d8/).

## MCP and Security Boundaries

**MCP standardizes connectivity, not authority.** Its lifecycle analysis identifies risks across creation, deployment, operation, and maintenance [[28.1]](https://consensus.app/papers/hou-zhao-model-context-protocol-mcp-landscape-security/w:f5758b83-5256-47db-a3cd-107f6f93c16e/) [[28.2]](https://consensus.app/papers/hou-zhao-model-context-protocol-mcp-landscape-security/w:f5758b83-5256-47db-a3cd-107f6f93c16e/). Protocol mapping studies also identify unresolved authentication, authorization, sandboxing, interoperability, and governance questions across MCP and comparable protocols [[29.1]](https://consensus.app/papers/umer-nawaz-a-systematic-mapping-study-of-ai-agent–tool/w:73252d44-76cf-471f-9817-71cd3b603cb5/) [[29.2]](https://consensus.app/papers/umer-nawaz-a-systematic-mapping-study-of-ai-agent–tool/w:73252d44-76cf-471f-9817-71cd3b603cb5/).

MCPSecBench found compromises across every tested attack surface; MSB exercised 2,000 attacks across nine agents and 405 tools [[30.1]](https://consensus.app/papers/yixuan-cuifeng-mcpsecbench-a-systematic-security-benchmark-and/w:538acf59-42fc-4d0e-808a-cbbed4e454f5/) [[31.1]](https://consensus.app/papers/zhang-li-mcp-security-bench-msb-benchmarking-attacks/w:80182f6a-cc03-42f9-8ccb-ff739a7dad48/). MCPXkit documents failures distinguishing external data from executable instructions, while malicious-server experiments show multi-component attacks can outperform isolated injections [[32.1]](https://consensus.app/papers/guo-liu-mcpxkit-the-unified-toolkit-for-analyzing-model/w:59f37520-0a82-4442-96ab-1d272ae6c60f/) [[33.1]](https://consensus.app/papers/huang-zhao-from-component-manipulation-to-system-compromise/w:f48cab14-470e-4a32-8b00-bde61583efda/).

### Security Evidence

| Control | Finding | Interpretation for ComfyUIPod |
|---|---|---|
| Host-side authorization | MCP-Secure tested 1,080 executions with scoped access and approval-gated writes | Mediate every tool invocation outside the model [[34.1]](https://consensus.app/papers/singh-madisetti-mcpsecure-a-runtime-access-control-layer-for/w:5da079a1-bb47-4655-929d-10c75377bc46/) [[34.2]](https://consensus.app/papers/singh-madisetti-mcpsecure-a-runtime-access-control-layer-for/w:5da079a1-bb47-4655-929d-10c75377bc46/) |
| Input/output filtering | Interface firewalls achieved strong static-benchmark security–utility results | Useful layer, not proof against adaptation [[35.1]](https://consensus.app/papers/bhagwatkar-kasa-indirect-prompt-injections-are-firewalls-all-you/w:9d36d0a6-5965-48b6-8b36-99be84f209db/) [[35.2]](https://consensus.app/papers/bhagwatkar-kasa-indirect-prompt-injections-are-firewalls-all-you/w:9d36d0a6-5965-48b6-8b36-99be84f209db/) |
| Learned detection | MCP-Guard reports 96.01% adversarial-prompt classification accuracy | Classification accuracy is not end-to-end containment [[36.1]](https://consensus.app/papers/xing-祁中浩-mcpguard-a-multistage-defenseindepth-framework-for/w:8802b379-42ad-4803-b836-052820c78b5b/) |
| Runtime auditing | SandScope observed source-to-sink witnesses in 12 repositories | Audit metadata, files, environment, and visible outputs [[37.1]](https://consensus.app/papers/tan-hao-mcpsandboxscan-wasmbased-secure-execution-and/w:b9949aed-1c76-4ef5-bc75-2ba213587b59/) |
| Adaptive testing | AutoDojo found static evaluations overestimate defense efficacy | Test defense-aware attacks and benign utility jointly [[38.1]](https://consensus.app/papers/ma-li-autodojo-a-generative-benchmark-for-evaluating/w:05dd1c32-fb71-42b1-a151-8a7c71a1790b/) [[38.2]](https://consensus.app/papers/ma-li-autodojo-a-generative-benchmark-for-evaluating/w:05dd1c32-fb71-42b1-a151-8a7c71a1790b/) |

**Figure 3:** Security controls, empirical support, and deployment implications

Code-execution MCP reduces tokens and latency but expands the attack surface; its supplied evaluation includes CPU-reproducible tooling [[39.1]](https://consensus.app/papers/felendler-gandhi-from-tool-orchestration-to-code-execution-a-study/w:251fdae1-0ea6-48ee-ae60-d8db4552f9c8/) [[39.2]](https://consensus.app/papers/felendler-gandhi-from-tool-orchestration-to-code-execution-a-study/w:251fdae1-0ea6-48ee-ae60-d8db4552f9c8/). MCPShield explores runtime trust calibration, and IntentCap proposes short-lived capabilities that only narrow user-authorized authority [[40.1]](https://consensus.app/papers/zhou-zhang-mcpshield-a-security-cognition-layer-for-adaptive/w:05494fdc-cc35-4bc4-b4e3-890fe96f4738/) [[41.1]](https://consensus.app/papers/zheng-zhang-llm-agent-capabilities-should-follow-task-intent/w:611750a0-804b-4fec-a0bd-f410a1198ec1/). A small adaptive reproduction reduced attack success from 25.8% to 4.2%, but used one weak model and one attack template [[42.1]](https://consensus.app/papers/narisetty-kore-adaptive-evaluation-of-outofband-defenses-against/w:8e1ec023-207c-4810-ad17-4e7c4faa834b/) [[42.2]](https://consensus.app/papers/narisetty-kore-adaptive-evaluation-of-outofband-defenses-against/w:8e1ec023-207c-4810-ad17-4e7c4faa834b/).

Container packaging and adversarial isolation require separate decisions: comparative studies document performance–isolation trade-offs, and unikernels exhibited stability limitations [[43.1]](https://consensus.app/papers/anjali-harter-blending-containers-and-virtual-machines/w:a04115da-8ed2-4450-b73e-b9a031784626/) [[44.1]](https://consensus.app/papers/moebius-pfandzelter-are-unikernels-ready-for-serverless-on-the-edge/w:92d5398e-9a2b-4d69-80d7-66eddb38de25/). Sandlock’s approximately 5-ms startup result is promising workstation evidence, not established GPU-engine compatibility [[45.1]](https://consensus.app/papers/wang-zheng-sandlock-confining-ai-agent-code-with-unprivileged/w:aa4daa86-f66d-409d-a269-079605472bd4/).

## Agentic 3D Workflows

**Separate asset generation, scene assembly, editing, articulation, and rendering.** DreamFusion optimizes a radiance field using 2D diffusion supervision; Magic3D’s two-stage mesh pipeline reported approximately 40-minute generation and 61.7% human preference over DreamFusion [[46.1]](https://consensus.app/papers/poole-jain-dreamfusion-textto3d-using-2d-diffusion/w:bd22f250-4874-41c1-956c-24428a141d05/) [[47.1]](https://consensus.app/papers/lin-gao-magic3d-highresolution-textto3d-content-creation/w:0a9baeca-680f-4e21-a960-c8c4b060e21a/) [[47.2]](https://consensus.app/papers/lin-gao-magic3d-highresolution-textto3d-content-creation/w:0a9baeca-680f-4e21-a960-c8c4b060e21a/). TextMesh explicitly addresses mesh extraction and oversaturated textures, illustrating why a renderable representation is not automatically the desired downstream asset [[48.1]](https://consensus.app/papers/tsalicoglou-manhardt-textmesh-generation-of-realistic-3d-meshes-from/w:cf8a440d-3cab-49ad-80f0-7e9503b42ce6/) [[48.2]](https://consensus.app/papers/tsalicoglou-manhardt-textmesh-generation-of-realistic-3d-meshes-from/w:cf8a440d-3cab-49ad-80f0-7e9503b42ce6/).

### Authoring Approaches

| Approach | Evidence and Method | Repository Opportunity |
|---|---|---|
| Procedural modeling | 3D-GPT decomposes instructions into procedural parameters | Typed, parameterized modeling recipes [[49.1]](https://consensus.app/papers/sun-han-3dgpt-procedural-3d-modeling-with-large-language/w:819bffb7-8e26-4e30-a149-c70b854dd0b7/) |
| Scene planning | SceneCraft converts scene graphs into Blender constraints | Explicit scene specification before execution [[50.1]](https://consensus.app/papers/hu-i̇şcen-scenecraft-an-llm-agent-for-synthesizing-3d-scene/w:812219e4-03eb-4e62-8183-d82c186436ad/) [[50.2]](https://consensus.app/papers/hu-i̇şcen-scenecraft-an-llm-agent-for-synthesizing-3d-scene/w:812219e4-03eb-4e62-8183-d82c186436ad/) |
| Visual editing | BlenderAlchemy searches edits using visual generation and evaluation | Bounded preview–inspect–edit loops [[51.1]](https://consensus.app/papers/huang-yang-blenderalchemy-editing-3d-graphics-with/w:77c4d275-93fe-4fb4-81b8-2b0e98708e76/) |
| Documentation grounding | LL3M retrieves version-specific Blender documentation and renders five views | Versioned API retrieval and multiview review [[52.1]](https://consensus.app/papers/sining-guan-ll3m-large-language-3d-modelers/w:71294d1f-53a7-4b8e-94e6-3676654c778c/) [[52.2]](https://consensus.app/papers/sining-guan-ll3m-large-language-3d-modelers/w:71294d1f-53a7-4b8e-94e6-3676654c778c/) |
| Execution evaluation | CodeGen-3D reports 3–4% failures for specialized/iterative systems versus 21–92% for general models | Separate execution, alignment, and preference metrics [[53.1]](https://consensus.app/papers/ji-aditya-codegen3d-a-benchmark-for-evaluating-llms-in/w:646ef985-e982-477c-a213-e464b87fa64c/) |

**Figure 4:** 3D authoring methods mapped to practical integration

Holodeck combines retrieved assets with spatial-constraint optimization; Scenethesis adds vision guidance and penetration/stability correction [[54.1]](https://consensus.app/papers/yang-sun-holodeck-language-guided-generation-of-3d-embodied/w:a49e5299-6ab7-4c0b-9dab-bde0fb654f81/) [[55.1]](https://consensus.app/papers/lu-lin-scenethesis-a-language-and-vision-agentic/w:4b4c2c7a-db6f-4e34-80c6-7542c625c670/). SceneWeaver unifies heterogeneous generation tools through iterative refinement, while spatial-context agents preserve geometry-aware memory [[56.1]](https://consensus.app/papers/yang-jia-sceneweaver-allinone-3d-scene-synthesis-with-an/w:6931686e-fb66-4c53-bee3-780c9436d30c/) [[57.1]](https://consensus.app/papers/liu-tai-agentic-3d-scene-generation-with-spatially/w:3f360f5e-9f84-4241-867a-c0185658bad8/). NaLA directly encodes geometry rather than relying only on lossy textual layouts [[58.1]](https://consensus.app/papers/wan-mao-nala-a-3d-native-llm-layout-agent-for-highquality/w:e77c0c31-07d4-4b95-81f0-fb47de4fe3bf/) [[58.2]](https://consensus.app/papers/wan-mao-nala-a-3d-native-llm-layout-agent-for-highquality/w:e77c0c31-07d4-4b95-81f0-fb47de4fe3bf/).

Localized editing deserves its own acceptance tests. HDSL reported 5.22-fold lower token use and 6.19-fold lower runtime, but only eight paired edits [[59.1]](https://consensus.app/papers/li-shen-hdsl-a-hierarchical-domainspecific-language-for/w:78fbe59e-e83a-4b1f-be63-b69bf2d3a3e2/). MUSE achieved 99.9% preservation yet only 49.6% all-goal editing success, demonstrating that locality and completion are distinct [[60.1]](https://consensus.app/papers/xu-zhu-muse-agentic-3d-scene-authoring-via-memorygrounded/w:e1a800ab-1207-400d-8c37-2145f6f316a7/). SceneAssistant and Holodeck 2.0 support human-guided revision; VIGA remains bounded by VLM spatial perception and context length [[61.1]](https://consensus.app/papers/luo-tang-sceneassistant-a-visual-feedback-agent-for/w:22f09959-244f-46fa-a988-266a99d2e0cc/) [[62.1]](https://consensus.app/papers/bian-ruohan-holodeck-20-visionlanguageguided-3d-world/w:9dcad3de-49a8-4f5b-89fb-53945c41af09/) [[63.1]](https://consensus.app/papers/yin-ge-visionasinversegraphics-agent-via-interleaved/w:78dcdeea-67ef-4371-8a48-5acc73153216/).

Articraft models parts, joints, limits, and tests through a restricted SDK; PADL addresses physics-based character control rather than geometry alone [[64.1]](https://consensus.app/papers/zhou-li-articraft-an-agentic-system-for-scalable/w:c17779f6-8ccb-4ec9-84e7-2a38cb785cfc/) [[65.1]](https://consensus.app/papers/juravsky-guo-padl-languagedirected-physicsbased-character/w:79e4bf3f-aa9f-4f1a-a992-593e9cb216ee/). Real-time rigged-model animation offers qualitative demonstrations, whereas the Blender–MCP animation architecture supplies insufficient quantitative evidence for reliability claims [[66.1]](https://consensus.app/papers/huang-torre-realtime-animation-generation-and-control-on/w:ee599b0a-7af9-4811-b0e8-649eb98c8bee/) [[67.1]](https://consensus.app/papers/shreya-jadhav-3d-animation-generation-using-blender-and-agentic/w:6f166eb4-f040-4156-9b45-d8e401afad38/). SimWorlds adds stage checkpoints and deterministic runtime inspection for dynamic scenes [[68.1]](https://consensus.app/papers/liu-wang-simworlds-a-multiagent-system-for-dynamic-3d-scene/w:aa9b0689-189e-48ab-8505-660b0e80f627/) [[68.2]](https://consensus.app/papers/liu-wang-simworlds-a-multiagent-system-for-dynamic-3d-scene/w:aa9b0689-189e-48ab-8505-660b0e80f627/).

## Multimodal Validation and Provenance

**Evaluate technical validity, semantic fulfillment, continuity, and provenance separately.** VBench provides 16 disentangled dimensions; VBench-2.0 adds physics, commonsense, anatomy, and controllability [[69.1]](https://consensus.app/papers/huang-he-vbench-comprehensive-benchmark-suite-for-video/w:3001b636-bc78-4cb0-9f37-b4c967bf5887/) [[70.1]](https://consensus.app/papers/zheng-huang-vbench20-advancing-video-generation-benchmark/w:70979fc4-6382-4151-a5ec-3ed5aee5c0a6/). VABench evaluates cross-modal similarities and synchronization; FoleyBench found poor audiovisual correspondence in 74% of earlier evaluation videos [[71.1]](https://consensus.app/papers/hua-wang-vabench-a-comprehensive-benchmark-for-audiovideo/w:212819a1-a235-4b05-9ce9-c8e6e415adcd/) [[72.1]](https://consensus.app/papers/dixit-saito-foleybench-a-benchmark-for-videotoaudio-models/w:a85b996c-c9e4-44a1-abf6-50f8f4a12122/).

MSVBench reports 94.4% rank correlation with human judgments, and MSAVBench reports 91.5%, but neither establishes universal evaluator validity [[73.1]](https://consensus.app/papers/shi-li-msvbench-towards-humanlevel-evaluation-of/w:a6316258-a185-467d-8e18-19e9af93069e/) [[74.1]](https://consensus.app/papers/wei-han-msavbench-towards-comprehensive-and-reliable/w:a1a1d7f1-ab78-46aa-9273-337933e23aee/). AVBench trains specialized evaluators on controlled perturbations; DirectorBench instead exposes checkpoint-level failures and logs success, fallback, failure, and latency [[75.1]](https://consensus.app/papers/yang-xia-avbench-humanaligned-and-automated-evaluation/w:aef0a625-6237-41e5-bfe8-db5ec30c1d44/) [[76.1]](https://consensus.app/papers/chen-chen-directorbench-diagnosing-longform-video-generation/w:afad0c5f-1ee0-4aa8-b8eb-84c67ec1b4a9/).

AniMaker uses budget-conscious candidate search and neighboring-clip evaluation; VideoWeaver evaluates traces alongside final videos [[77.1]](https://consensus.app/papers/shi-li-animaker-multiagent-animated-storytelling-with/w:7795715c-af39-4a37-9e06-10cbc483386c/) [[78.1]](https://consensus.app/papers/wei-zhang-videoweaver-evaluating-and-evolving-skills-for/w:309e84e6-57b8-44ec-9768-200dd1e4a959/). FRAMEWORKERS and JarvisHub make persistent workspaces central, but their architectural promise should remain separate from independently established performance [[79.1]](https://consensus.app/papers/li-sun-frameworkers-a-dynamic-multiagent-framework-for/w:48fd9d27-7e61-4de2-b9cd-bd88515b88f3/) [[80.1]](https://consensus.app/papers/lin-lin-jarvishub-an-open-harness-for-canvasnative/w:43ecd99e-a31f-4f37-bf99-adecba52fb7e/). AgenticVBench’s expert-authored production tasks remain difficult: the strongest evaluated stack barely exceeded 30% [[81.1]](https://consensus.app/papers/cao-zheng-agenticvbench-can-ai-agents-complete-realworld/w:3180de3c-7d0c-4868-936f-368273f9927a/) [[81.2]](https://consensus.app/papers/cao-zheng-agenticvbench-can-ai-agents-complete-realworld/w:3180de3c-7d0c-4868-936f-368273f9927a/).

Containers improve environment portability and reproducibility, not the truth of generated content [[82.1]](https://consensus.app/papers/kurtzer-sochat-singularity-scientific-containers-for-mobility-of/w:950f6dab-d7d8-44d9-9ad6-cc6d24377f07/) [[83.1]](https://consensus.app/papers/moreau-wiebels-containers-for-computational-reproducibility/w:6ed9b171-e482-4446-93fe-5a73f0b76202/). Execution provenance should distinguish evidence supporting a judgment from operations influencing an artifact [[84.1]](https://consensus.app/papers/wang-zhang-from-agent-traces-to-trust-a-survey-of-evidence/w:a592c7fe-309a-4dcb-bfec-fb5f6f973062/). C2PA critiques and demonstrated provenance–watermark contradictions caution against displaying signed lineage as proof of authenticity [[85.1]](https://consensus.app/papers/golaszewski-krawetz-verifying-provenance-of-digital-media-why-the-c2pa/w:9be2fa02-6e7d-41a5-8239-bdd1637a2a51/) [[86.1]](https://consensus.app/papers/nemecek-he-authenticated-contradictions-from-desynchronized/w:9769eb4a-0ccd-40dc-b6ec-80f84a9bdc29/). Broader media-integrity analysis likewise distinguishes provenance, watermarking, and fingerprinting threat models [[87.1]](https://consensus.app/papers/young-vaughan-media-integrity-and-authentication-status/w:4be2111b-5b53-4ddf-b8a4-8b2ad4cacb23/).

## Ranked Implementation Opportunities

These rankings prioritize verifiability and containment before expanded autonomy. Acceptance criteria below are **proposed tests**, not reported repository capabilities.

| Rank / Architecture | Concrete Change | CPU-Fixture Verification | GPU / Quality Verification | Evidence |
|---|---|---|---|---|
| **1 — Graph/executor** | Typed node contracts, persistent states, idempotency keys, bounded recovery | Duplicate delivery, restart, timeout, malformed-output tests | Actual engine interruption and recovery | [[8.3]](https://consensus.app/papers/babu-agrawal-selfhealing-agentic-orchestrators-for-reliable/w:8fdf7d60-0911-4354-a92c-5437f5c0cf1e/) |
| **2 — MCP/tool belt** | Central authorization, scoped paths, read-only defaults, explicit elevation | Deny cross-project access and unauthorized writes | Engine-specific sandbox compatibility | [[34.3]](https://consensus.app/papers/singh-madisetti-mcpsecure-a-runtime-access-control-layer-for/w:5da079a1-bb47-4655-929d-10c75377bc46/) |
| **3 — Engine adapters** | Separate installed, healthy, executable, and validated states | Fake adapters and fresh deployment replay | Real checkpoint loading and inference | [[1.3]](https://consensus.app/papers/wang-qian-deploybench-benchmarking-llm-agents-for-research/w:da607bfe-ff77-4007-abaa-33f7eb684b20/) |
| **4 — Studio/artifacts** | Immutable outputs, parent hashes, parameters, versions, validation records | Lineage integrity and stale-artifact rejection | Reproduction tolerance across devices | [[84.2]](https://consensus.app/papers/wang-zhang-from-agent-traces-to-trust-a-survey-of-evidence/w:a592c7fe-309a-4dcb-bfec-fb5f6f973062/) |
| **5 — Blender/3D adapter** | Restricted scene operations, versioned API retrieval, checkpointed edits | Headless primitives, export parsing, edit preservation | Multiview quality and complex geometry | [[52.3]](https://consensus.app/papers/sining-guan-ll3m-large-language-3d-modelers/w:71294d1f-53a7-4b8e-94e6-3676654c778c/) |
| **6 — GPU leases** | Admission limits, residency awareness, bounded queues, measured profiles | Fake-clock lease expiry and accounting | VRAM peaks, cold starts, contention, goodput | [[15.2]](https://consensus.app/papers/xu-xu-igniter-interferenceaware-gpu-resource/w:0842d787-0caf-490c-ac0a-dc7d21a6d107/) |
| **7 — FFmpeg/Remotion** | Technical probes plus shot/audio/semantic validation | Decode, duration, timestamps, missing-stream fixtures | Synchronization, continuity, editorial quality | [[76.2]](https://consensus.app/papers/chen-chen-directorbench-diagnosing-longform-video-generation/w:afad0c5f-1ee0-4aa8-b8eb-84c67ec1b4a9/) |
| **8 — Atlas/retrieval** | Task-scoped discovery, asset metadata, quarantined external instructions | Distractor tools, schema drift, hostile metadata | Asset suitability and grounding accuracy | [[88.1]](https://consensus.app/papers/liu-liu-mcpagentbench-a-realworld-task-benchmark-for/w:343960eb-5882-4576-b041-cc374570fab9/) |
| **9 — Planner/evals** | Fixed-budget baseline against richer orchestration | Replayable task suites and process metrics | Paired human review and cost-adjusted quality | [[89.1]](https://consensus.app/papers/wang-qi-mcpbench-benchmarking-toolusing-llm-agents-with/w:2210aaee-48fe-4f9f-a438-b70e0abd6e07/) |

**Figure 5:** Ranked changes separating fixtures from real measurements

Cancellation should explicitly test queued, running, and completing jobs; retries should distinguish transient failure from invalid parameters, permission denial, and failed semantic validation. These are proposed executor invariants motivated by documented orchestration failures, not directly validated cancellation algorithms [[8.4]](https://consensus.app/papers/babu-agrawal-selfhealing-agentic-orchestrators-for-reliable/w:8fdf7d60-0911-4354-a92c-5437f5c0cf1e/).

### Evidence Strength and Release Gates

| Claim | Evidence Strength | Release Implication | Citations |
|---|---|---|---|
| Measured scheduling improves tested GPU-serving workloads | Evidence strength: Strong (8/10) | Profile this engine mix before enabling concurrency | [[90.1]](https://consensus.app/papers/ye-gao-deep-learning-workload-scheduling-in-gpu/w:608a8c90-415e-4200-b728-1ba8fcc36228/) |
| Verification-guided recovery improves tested agent tasks | Evidence strength: Moderate (7/10) | Require fault-injection comparisons | [[8.5]](https://consensus.app/papers/babu-agrawal-selfhealing-agentic-orchestrators-for-reliable/w:8fdf7d60-0911-4354-a92c-5437f5c0cf1e/) |
| Iterative feedback improves tested 3D authoring | Evidence strength: Moderate (7/10) | Compare single-pass and bounded-refinement variants | [[53.2]](https://consensus.app/papers/ji-aditya-codegen3d-a-benchmark-for-evaluating-llms-in/w:646ef985-e982-477c-a213-e464b87fa64c/) |
| Additional agents inherently improve reliability | Evidence strength: Weak (2/10) | Demand matched-budget ablations | [[91.1]](https://consensus.app/papers/albayaydh-zhao-beyond-the-leaderboard-a-synthesis-of-tooluse/w:d6485fc4-e47c-4bcf-a250-2ece8d2f8658/) |
| Reusable skills guarantee portable reliability | Evidence strength: Weak (2/10) | Version, review, and regression-test recipes | [[92.1]](https://consensus.app/papers/xing-zhuang-recipes-for-agents-understanding-skills-and-their/w:345c6755-2ddb-40ad-b3dc-5e40319d2762/) [[92.2]](https://consensus.app/papers/xing-zhuang-recipes-for-agents-understanding-skills-and-their/w:345c6755-2ddb-40ad-b3dc-5e40319d2762/) |

**Figure 6:** Evidence confidence and corresponding proposed release gates

Tool-learning reviews support explicit retrieval, decomposition, and ordered execution, but do not establish one universally superior orchestration stack [[93.1]](https://consensus.app/papers/xu-huang-llmbased-agents-for-tool-learning-a-survey/w:8807f29c-07ba-47e9-b7a4-982a951652cf/) [[93.2]](https://consensus.app/papers/xu-huang-llmbased-agents-for-tool-learning-a-survey/w:8807f29c-07ba-47e9-b7a4-982a951652cf/). Scientific-workflow terminology similarly distinguishes orchestration, data management, and metadata capture rather than treating them as one capability [[94.1]](https://consensus.app/papers/suter-coleman-a-terminology-for-scientific-workflow-systems/w:8ef48cb9-cba8-453f-a512-e73e5c929f4f/). Aggressive context curation and inspect-before-act behavior address documented distractor and grounding failures [[95.1]](https://consensus.app/papers/jv-how-do-llms-fail-in-agentic-scenarios-a/w:c7f1781c-89f4-45fa-8cf7-183224169781/) [[95.2]](https://consensus.app/papers/jv-how-do-llms-fail-in-agentic-scenarios-a/w:c7f1781c-89f4-45fa-8cf7-183224169781/).

**Bottom line:** first make ComfyUIPod’s execution, permissions, artifacts, and failure states testable on CPU. Then establish real GPU and human-reviewed baselines before claiming improved throughput, autonomous deployment, or production-ready 3D creation.

_These search results were found and analyzed using Consensus, an AI-powered search engine for research. Try it at https://consensus.app. © 2026 Consensus NLP, Inc. Personal, non-commercial use only; redistribution requires copyright holders’ consent._

## References

`1.` Wang Y, Qian Y, Zhang Y, et al. DeployBench : Benchmarking LLM Agents for Research Artifact Deployment. *Findings of the Association for Computational Linguistics: EMNLP (Findings of EMNLP).* 2026;abs/2606.05238:14848-14866. doi:10.18653/2026.findings-emnlp.707

> [1.1] We introduce DeployBench, a multi-domain benchmark of 51 research-artifact deployment tasks spanning AI/ML, computer systems, and scientific computing, covering all these dimensions. (Abstract)

> [1.2] Failures are dominated by a completion-judgment problem: 102 of 181 are agent-terminated self-stops, where the agent's pre-finish checks validate a different or weaker target than the paper-specific task requires. (Abstract)

> [1.3] Each task is verified by a hidden pipeline that executes the paper's designated experiment and checks its outputs. (Abstract)

`2.` Ding W, Zhan J, Li Y, Guo Q. FDE-Bench: Evaluating LLM Agents for Deployment Environment Configuration. *arXiv (Cornell University).* 2026. doi:10.48550/arxiv.2609.27571

> [2.1] Agents submit declarative artifacts that are collected, rebuilt, and redeployed in a pristine environment. (Abstract)

> [2.2] Four gated binary check layers measure build, readiness, behavior, and conformance to the deployment specification, using programmatic checks without an LLM judge. (Abstract)

`3.` Xiao Y, Wang R, Kong L, Golac D, Wang WW. CSR-Bench: Benchmarking LLM Agents in Deployment of Computer Science Research Repositories. *Conference of the North American Chapter of the Association for Computational Linguistics: Human Language Technologies (NAACL-HLT).* 2025. doi:10.18653/v1/2025.naacl-long.633

> [3.1] Specifically, by checking instructions from markdown files and interpreting repository structures, the model generates and iteratively improves bash commands that set up the experimental environments and deploy the code to conduct research tasks. (Abstract)

> [3.2] Preliminary results from CSR-Bench indicate that LLM agents can significantly enhance the workflow of repository deployment, thereby boosting developer productivity and improving the management of developmental workflows. (Abstract)

`4.` Yao Y, Tan X, Liu C, et al. Harness-Bench: Evaluating Agent Harnesses across Models and Realistic Workflows. *arXiv (Cornell University).* 2026;abs/2605.27922. doi:10.48550/arxiv.2605.27922

> [4.1] The benchmark contains 106 sandboxed offline tasks constructed from practical agent-use patterns and manually reviewed for realism, solvability, oracle-checkability, and integrity. (Abstract)

> [4.2] Across 5,194 execution trajectories, we observe substantial variation in completion, process quality, efficiency, and failure behavior across model-harness pairings. (Abstract)

`5.` Zhu P, Li L, Lyu Y, et al. UniACE: A Unified Framework for Evaluating LLM Agentic Capabilities. *arXiv (Cornell University).* 2026;abs/2605.27898. doi:10.48550/arxiv.2605.27898

> [5.1] Comparisons with source implementations show large bidirectional score changes and model-ranking reversals, while matched online and offline runs reveal substantial sensitivity to accessible evidence and its representation. (Abstract)

> [5.2] Coverage is currently limited to text-centric agentic tasks. (Discussion)

`6.` Yao S, Shinn N, Razavi P, Narasimhan K. $τ$-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains. *arXiv (Cornell University).* 2024;abs/2406.12045. doi:10.48550/arxiv.2406.12045

> [6.1] We employ an efficient and faithful evaluation process that compares the database state at the end of a conversation with the annotated goal state. (Abstract)

> [6.2] Our experiments show that even state-of-the-art function calling agents (like gpt-4o) succeed on<50% of the tasks, and are quite inconsistent (pass^8<25% in retail). (Abstract)

`7.` Gupta A. ReliabilityBench: Evaluating LLM Agent Reliability Under Production-Like Stress Conditions. *arXiv (Cornell University).* 2026;abs/2601.06112. doi:10.48550/arxiv.2601.06112

> [7.1] ReliabilityBench contributes a unified reliability surface $R(k,\epsilon,\lambda)$, \textit{action metamorphic relations} that define correctness via end-state equivalence rather than text similarity, and a chaos-engineering-style fault injection framework (timeouts, rate limits, partial responses, schema drift). (Abstract)

> [7.2] We evaluate two models (Gemini 2.0 Flash, GPT-4o) and two agent architectures (ReAct, Reflexion) across four domains (scheduling, travel, customer support, e-commerce) over 1,280 episodes. (Abstract)

`8.` Babu RS, Agrawal A. Self-Healing Agentic Orchestrators for Reliable Tool-Augmented Large Language Model Systems. *arXiv (Cornell University).* 2026;abs/2606.01416. doi:10.48550/arxiv.2606.01416

> [8.1] We evaluate the approach on a 100-task controlled fault-injection benchmark against static workflow, retry-only, ReAct-style, and full-replanning baselines. (Abstract)

> [8.2] Self-healing achieves 98.8\% task success, compared with 94.5\% for retry-only and 93.8\% for full replanning. (Abstract)

> [8.3] The orchestrator maps observable failure signals to inferred failure classes, selects targeted recovery actions under explicit budgets, verifies recovered trajectories, and records observability traces. (Abstract)

> [8.4] In these systems, failures arise not only from model errors, but also from orchestration-level issues such as tool timeouts, malformed arguments, stale context, contradictory evidence, retry loops, and unverified intermediate outputs. (Abstract)

> [8.5] These results provide controlled evidence that failure-aware, budgeted, and verification-guided orchestration improves reliability and diagnosability in tool-augmented LLM systems. (Abstract)

`9.` Chang EY, Geng L. SagaLLM: Context Management, Validation, and Transaction Guarantees for Multi-Agent LLM Planning. *Proceedings of the VLDB Endowment.* 2025;18:4874-4886. doi:10.14778/3750601.3750611

> [9.1] SagaLLM bridges this gap by integrating the Saga transactional pattern with persistent memory, automated compensation, and independent validation agents. (Abstract)

> [9.2] For our experiments, we focused on two medium-tier sequential planning challenges (problems #5 and #6) that test systematic workflow execution and dependency management, and two reactive planning challenges (problems #8 and #9) that evaluate dynamic adaptation and compensation capabilities. (Methods)

`10.` Zhu K, Zijia L, Bingxuan L, et al. Where LLM Agents Fail and How They can Learn From Failures. *arXiv (Cornell University).* 2025;abs/2509.25370. doi:10.48550/arxiv.2509.25370

> [10.1] Beyond detection, the targeted feedback generated by AgentDebug enables LLM agents to iteratively recover from failures, yielding up to 26% relative improvements in task success across ALFWorld, GAIA, and WebShop. (Abstract)

`11.` Lu J, Holleis T, Zhang Y, et al. ToolSandbox: A Stateful, Conversational, Interactive Evaluation Benchmark for LLM Tool Use Capabilities. *Findings of the Association for Computational Linguistics: NAACL.* 2025. doi:10.18653/v1/2025.findings-naacl.65

> [11.1] BFCL (Yan et al., 2024) and ToolEval (Qin et al., 2024) both rely on stateless tools interacting with web services (through RESTful APIs). (Introduction)

> [11.2] API-Bank (Li et al., 2023), ToolTalk (Farn and Shin, 2023) and τ -bench (Yao et al., 2024) does include a set of tools to modify world states, but does not study the impact of state dependencies. (Introduction)

`12.` Barres V, Honghua D, Soham R, Xujie S, Narasimhan K. $τ^2$-Bench: Evaluating Conversational Agents in a Dual-Control Environment. *arXiv (Cornell University).* 2025;abs/2506.07982. doi:10.48550/arxiv.2506.07982

> [12.1] In particular, our experiments show significant performance drops when agents shift from no-user to dual-control, highlighting the challenges of guiding users. (Abstract)

`13.` Junlong L, Wenshuo Z, Jian Z, et al. The Tool Decathlon: Benchmarking Language Agents for Diverse, Realistic, and Long-Horizon Task Execution. *arXiv (Cornell University).* 2025;abs/2510.25726. doi:10.48550/arxiv.2510.25726

> [13.1] This benchmark includes 108 manually sourced or crafted tasks in total, requiring interacting with multiple Apps over around 20 turns on average to complete. (Abstract)

> [13.2] Comprehensive evaluation of SOTA models highlights their significant shortcomings: the best-performing model, Claude-4.5-Sonnet, achieves only a 38.6% success rate with 20.2 tool calling turns on average, while the top open-weights model DeepSeek-V3.2-Exp reaches 20.1%. (Abstract)

`14.` Gujarati A, Karimi RJ, Alzayat S, et al. Serving DNNs like Clockwork: Performance Predictability from the Bottom Up. *arXiv (Cornell University).* 2020. doi:10.48550/arxiv.2006.02464

> [14.1] We evaluate our implementation, Clockwork, using production trace workloads, and show that Clockwork can support thousands of models while simultaneously meeting 100ms latency targets for 99.9999% of requests. (Abstract)

`15.` Xu F, Xu J, Chen J, et al. iGniter: Interference-Aware GPU Resource Provisioning for Predictable DNN Inference in the Cloud. *IEEE Transactions on Parallel and Distributed Systems.* 2022;34:812-827. doi:10.1109/tpds.2022.3232715

> [15.1] Extensive prototype experiments on four representative DNN models and datasets demonstrate that <italic>iGniter</italic> can guarantee the performance SLOs of DNN inference workloads with practically acceptable runtime overhead, while saving the monetary cost by up to <inline-formula><tex-math notation="LaTeX">$25\%$</tex-math><alternatives><mml:math><mml:mrow><mml:mn>25</mml:mn><mml:mo>%</mml:mo></mml:mrow></mml:math><inline-graphic xlink:href="xu-ieq1-3232715.gif"/></alternatives></inline-formula> in comparison to the state-of-the-art GPU resource provisioning strategies. (Abstract)

> [15.2] <italic>iGniter</italic> is comprised of two key components: (1) a <italic>lightweight</italic> DNN inference performance model, which leverages the system and workload metrics that are practically accessible to capture the performance interference; (2) A <italic>cost-efficient</italic> GPU resource provisioning strategy that <italic>jointly</italic> optimizes the GPU resource allocation and adaptive batching based on our inference performance model, with the aim of achieving predictable performance of DNN inference workloads. (Abstract)

`16.` Peng Y, Peng H. InferFair: Towards QoS-aware scheduling for performance isolation guarantee in heterogeneous model serving systems. *Future Generation Computer Systems.* 2023;150:10-20. doi:10.1016/j.future.2023.08.020

> [16.1] Compared to a prior competitor named Clockwork, InferFair alleviates the performance isolation problem by up to 1.7 × , as well as improving the overall goodput by up to 25.6%. (Abstract)

`17.` Choi S, Lee S, Kim Y, Park J, Kwon Y, Huh J. Multi-model Machine Learning Inference Serving with GPU Spatial Partitioning. *arXiv (Cornell University).* 2021;abs/2109.01611. doi:10.48550/arxiv.2109.01611

> [17.1] To maximize the resource efficiency of inference servers, a key mechanism proposed in this paper is to exploit hardware support for spatial partitioning of GPU resources. (Abstract)

> [17.2] Our prototype implementation proves that spatial partitioning enhances throughput by 102.6% on average while satisfying SLOs. (Abstract)

`18.` Subramanya SJ, Arfeen D, Lin S, Qiao A, Jia Z, Ganger GR. Sia: Heterogeneity-aware, goodput-optimized ML-cluster scheduling. *ACM SIGOPS Symposium on Operating Systems Principles (SOSP).* 2023. doi:10.1145/3600006.3613175

> [18.1] For example, even on relatively small 44- to 64-GPU clusters with a mix of three GPU types, Sia reduces average job completion time (JCT) by 30--93%, 99th percentile JCT and makespan by 28--95%, and GPU hours used by 12--55% for workloads derived from 3 real-world environments. (Abstract)

`19.` Chen Y, Jiang C, Wu C, et al. HASE: Hardware-Aware Scheduling for Inference Tasks in Heterogeneous GPU Clusters. *IEEE Transactions on Cloud Computing.* 2026;14:1202-1216. doi:10.1109/tcc.2026.3685862

> [19.1] Moreover, <italic>HASE</italic> reduces 51% total job makespan compared to traditional round-robin scheduling, and maintains per-task scheduling decision time under one second in clusters with up to hundreds of GPUs. (Abstract)

`20.` Kim S, Ha J, Kim Y. Oltunes: Online learning-based auto-tuning system for DL inference in heterogeneous GPU cluster. *Cluster Computing.* 2025;28. doi:10.1007/s10586-025-05216-0

> [20.1] Performance experiments on a heterogeneous GPU cluster demonstrated a 58% average improvement in GPU utilization, up to 49% reduction in p99 tail latency, and a 61% increase in throughput. (Abstract)

> [20.2] It also achieved approximately 84.6% energy savings with a maximum accuracy loss of 4% and reduced latency-sensitive SLO violations by up to 92% compared to other baselines, ensuring end-to-end QoS. (Abstract)

`21.` Zeng K, Huang Z, Yang Y, Zhang X, Huang S, Meng R. TokenFlow: Token-Level GPU Sharing and Adaptive Scheduling for Multi-Model Concurrent LLM Inference. *International Conference on Electronic Technology and Information Science (ICETIS).* 2026. doi:10.1109/icetis70504.2026.11633493

> [21.1] TokenFlow decomposes GPU execution into individual token steps and interleaves tokens from different models within a single scheduling quantum, preserving each model's autoregressive dependency while maximizing compute density. (Abstract)

`22.` Lin R, Ding Z, Han Z, Zhang J. Large-Scale LLM Inference with Heterogeneous Workloads: Prefill-Decode Contention and Asymptotically Optimal Control. *arXiv (Cornell University).* 2026;abs/2602.02987. doi:10.48550/arxiv.2602.02987

> [22.1] We design gate-and-route policies that regulate prefill admission and decode routing, and prove that they are asymptotically optimal in the many-GPU limit under both bundled and separate token-pricing schemes. (Abstract)

`23.` Palaniappan K. DriftSched: Adaptive QoS-Aware Scheduling under Runtime Token Drift for Multi-Tenant GPU Inference. *arXiv (Cornell University).* 2026;abs/2606.02982. doi:10.48550/arxiv.2606.02982

> [23.1] In practice, admission-time workload estimates may deviate from observed execution behavior, leading to workload misclassification, queue imbalance, increased tail latency, and degraded Quality-of-Service (QoS). (Abstract)

`24.` Xia T, Huang X, Zhou Y, Chang F, Ren L. Coordinating Training and Inference in Heterogeneous Cloud GPU Clusters with Learned Interference Models. *International Conference on Internet of Things, Automation and Artificial Intelligence (IoTAAI).* 2026. doi:10.1109/iotaai70471.2026.11607713

> [24.1] Experiments driven by the Alibaba GPU cluster trace on a simulated heterogeneous cluster with three GPU types show that TwinSched reduces inference SLO violations from $\mathbf{1 8. (Abstract)

`25.` Ng KKW, Demoulin HM, Liu V. Paella: Low-latency Model Serving with Software-defined GPU Scheduling. *ACM SIGOPS Symposium on Operating Systems Principles (SOSP).* 2023. doi:10.1145/3600006.3613163

> [25.1] In this paper, we argue for co-designing the model compiler, local clients, and the scheduler to bypass the built-in GPU scheduler and enable software control of kernel execution order. (Abstract)

`26.` Sultana A, Pakka N, Xu F, Yuan X, Chen L, Tzeng N. Resource Heterogeneity-Aware and Utilization-Enhanced Scheduling for Deep Learning Clusters. *IEEE Transactions on Computers.* 2026;75:2169-2184. doi:10.1109/tc.2026.3672471

> [26.1] Our trace-driven simulation with representative DL model training workloads demonstrates that <italic>Hadar</italic> accelerates the total training time duration by 1.20<inline-formula><tex-math notation="LaTeX">$\times$</tex-math><alternatives><mml:math><mml:mo>×</mml:mo></mml:math><graphic position="float" orientation="portrait" xlink:href="chen-ieq1-3672471.gif"/></alternatives></inline-formula> when compared with its state-of-the-art heterogeneity-aware counterpart, Gavel. (Abstract)

`27.` Wang J, Zhang Y, Zhou X, et al. Latency-Aware Orchestration for Multi-Agent LLM Workflows on Heterogeneous GPUs. *arXiv (Cornell University).* 2026. doi:10.48550/arxiv.2609.03335

> [27.1] Predictor estimates device-specific activation latency, peak memory, and model-loading cost, then propagates these predictions through workflow dependencies to forecast activation readiness and future model demand. (Abstract)

`28.` Hou X, Zhao Y, Wang S, Wang H. Model Context Protocol (MCP): Landscape, Security Threats, and Future Research Directions. *ACM Transactions on Software Engineering and Methodology.* 2026;35:1-37. doi:10.1145/3796519

> [28.1] We first define the full lifecycle of an MCP server, comprising four phases (creation, deployment, operation, and maintenance), further decomposed into 16 key activities that capture its functional evolution. (Abstract)

> [28.2] Building on this lifecycle analysis, we construct a comprehensive threat taxonomy that categorizes security and privacy risks across four major attacker types: malicious developers, external attackers, malicious users, and security flaws, encompassing 16 distinct threat scenarios. (Abstract)

`29.` Umer HS, Nawaz NAH, Ishaq K, Ahmad A. A systematic mapping study of AI agent–Tool communication: protocols, security, and orchestration. *Intelligent Systems with Applications.* 2026;32:200733-200733. doi:10.1016/j.iswa.2026.200733

> [29.1] Cross-analyses surface comparisons across open agent–tool communication contracts namely MCP, A2A, ACP and ANP alongside the LangChain and AutoGPT framework family on authentication, authorization, input validation and sandboxing posture, with security threats paired against mitigation strategies reported in the same studies. (Abstract)

> [29.2] Gaps cover protocol standardisation, security-by-design, orchestration formalisation, cross-framework interoperability and lifecycle governance, paired with future directions, two of them carried through to a concrete first step, which may help researchers and practitioners design safer agent–based systems. (Abstract)

`30.` Yixuan Y, Cuifeng G, Wu D, Chen Y, Yingjiu L, Wang S. MCPSecBench: A Systematic Security Benchmark and Playground for Testing Model Context Protocols. *arXiv (Cornell University).* 2025;abs/2508.13220. doi:10.48550/arxiv.2508.13220

> [30.1] Our evaluation across three major MCP platforms reveals that all attack surfaces yield successful compromises. (Abstract)

`31.` Zhang D, Li Z, Xu L, Liu X, Li P, Wenjun X. MCP Security Bench (MSB): Benchmarking Attacks Against Model Context Protocol in LLM Agents. *arXiv (Cornell University).* 2025;abs/2510.15994. doi:10.48550/arxiv.2510.15994

> [31.1] We evaluate nine popular LLM agents across 10 domains and 405 tools, producing 2,000 attack instances. (Abstract)

`32.` Guo Y, Liu P,  W, et al. MCPXkit: The Unified Toolkit for Analyzing Model Context Protocol Security. *IEEE Transactions on Dependable and Secure Computing.* 2026;23:9796-9811. doi:10.1109/tdsc.2026.3695553

> [32.1] Our experiments reveal key insights into MCP vulnerabilities, including agents’ blind reliance on tool descriptions, sensitivity to file-based attacks, chain attacks exploiting shared context, and difficulty distinguishing external data from executable commands, and this situation is even worse in some reasoning models. (Abstract)

`33.` Huang Y, Zhao Z, Chen B, et al. From Component Manipulation to System Compromise: Understanding and Detecting Malicious MCP Servers. *arXiv (Cornell University).* 2026;abs/2604.01905. doi:10.48550/arxiv.2604.01905

> [33.1] We evaluate these attacks'effectiveness across two MCP hosts and five LLMs, and uncover that (1) component position shapes attack success rate; and (2) multi-component compositions often outperform single-component attacks by distributing malicious logic. (Abstract)

`34.` Singh G, Madisetti VK. MCP-Secure: A Runtime Access Control Layer for Privilege-Aware LLM Agent Tooling. *IEEE Open Journal of the Computer Society.* 2026;7:504-513. doi:10.1109/ojcs.2026.3664314

> [34.1] RESULTS We evaluate MCP-Secure across 1,080 controlled executions, where each execution corresponds to a complete end-to-end agent run on a repository-management task under a fixed combination of task type, LLM backend, adversarial attack simulation and enforcement level. (Results)

> [34.2] Results show that scoped access alone blocks most unsafe behaviors, read-only enforcement reliably neutralizes all mutating attack vectors, and approval-gated elevation maintains strong safety while enabling controlled write operations. (Abstract)

> [34.3] This article presents MCP-Secure, a lightweight, host-side enforcement layer for the Model Context Protocol (MCP) that applies scoped access, read-only defaults, and approval-gated privilege elevation at runtime. (Abstract)

`35.` Bhagwatkar R, Kasa K, Puri A, et al. Indirect Prompt Injections: Are Firewalls All You Need, or Stronger Benchmarks?. *arXiv (Cornell University).* 2025;abs/2510.05244. doi:10.48550/arxiv.2510.05244

> [35.1] Inspired by the well-established concept of firewalls, we show that a simple, modular, and model-agnostic defense operating at the agent--tool interface achieves perfect security with high utility across all four public benchmarks: AgentDojo, Agent Security Bench, InjecAgent and tau-Bench, while achieving a state-of-the-art security--utility tradeoff compared to prior results. (Abstract)

> [35.2] Overall, our work shows that existing agentic security benchmarks are easily saturated by a simple approach and highlights the need for stronger benchmarks with carefully chosen evaluation metrics and strong adaptive attacks. (Abstract)

`36.` Xing W,  祁, Qin Y, et al. MCP-Guard: A Multi-Stage Defense-in-Depth Framework for Securing Model Context Protocol in Agentic AI. *Findings of the Association for Computational Linguistics: ACL (Findings of ACL).* 2026. doi:10.18653/v1/2026.findings-acl.240

> [36.1] MCP-GUARD employs a three-stage detection pipeline that balances efficiency with accuracy: it progresses from lightweight static scanning for overt threats and a deep neural detector for semantic attacks, to our fine-tuned E5-based model which achieves 96.01\% accuracy in identifying adversarial prompts. (Abstract)

`37.` Tan ZW, Hao R, Singer J, Tang Y, Anagnostopoulos C. MCP-SandboxScan: WASM-based Secure Execution and Runtime Analysis for MCP Tools. *arXiv (Cornell University).* 2026;abs/2601.01241. doi:10.48550/arxiv.2601.01241

> [37.1] A schema-guided exploration pass over the 35 dynamically scanned repositories re-executes 33 and observes source-to-sink witnesses in 12. (Abstract)

`38.`  X, Li T, Xiao C, Yu Z, Zhang N, Vorobeychik Y. AutoDojo: A Generative Benchmark for Evaluating Prompt Injection Defenses in LLM Agents. *arXiv (Cornell University).* 2026. doi:10.48550/arxiv.2606.15057

> [38.1] Across ten defenses and five target models, AutoDojo demonstrates that standard static benchmarks often significantly overestimate defense efficacy. (Abstract)

> [38.2] Moreover, we show that most existing defenses either sacrifice considerable utility or are insecure. (Abstract)

`39.` Felendler Y, Gandhi PA, Habler I, Elovici Y, Shabtai A. From Tool Orchestration to Code Execution: A Study of MCP Design Choices. *arXiv (Cornell University).* 2026;abs/2602.15945. doi:10.48550/arxiv.2602.15945

> [39.1] Using the MCP-Bench framework across 10 representative servers, we empirically evaluate task behavior, tool utilization patterns, execution latency, and protocol efficiency as the scale of connected MCP servers and available tools increases, demonstrating that while CE-MCP significantly reduces token usage and execution latency, it introduces a vastly expanded attack surface. (Abstract)

> [39.2] No GPU required; standard CPU hardware suffices. (Body)

`40.` Zhou Z, Zhang Y, Cai H, et al. MCPShield: A Security Cognition Layer for Adaptive Trust Calibration in Model Context Protocol Agents. *arXiv (Cornell University).* 2026;abs/2602.14281. doi:10.48550/arxiv.2602.14281

> [40.1] Our method constrains execution within controlled boundaries while cognizing runtime events, and subsequently updates security cognition by reasoning over historical traces after invocation, building on human post-use reflection on tool behavior. (Abstract)

`41.` Zheng Y, Zhang W, Mao Y. LLM Agent Capabilities Should Follow Task Intent and Context Source. *arXiv (Cornell University).* 2026. doi:10.48550/arxiv.2609.14631

> [41.1] Each source contributes specific fields, none can fill another's, and the lease only narrows the user's authorized authority, never widens it. (Abstract)

`42.` Narisetty P, Kore SNB, Kattamanchi UKR, Kumarapu J. Adaptive Evaluation of Out-of-Band Defenses Against Prompt Injection in LLM Agents. *arXiv (Cornell University).* 2026;abs/2606.26479. doi:10.48550/arxiv.2606.26479

> [42.1] Averaged over three runs, the defense held: Progent cut mean attack success roughly sixfold (25.8% to 4.2%), and a hand-crafted adaptive attack did not raise it (2.6%). (Abstract)

> [42.2] This is one small-scale data point on a weak model with a single black-box attack template; a stronger optimized (white-box GCG) attack remains open. (Abstract)

`43.`  A, Harter T, Swift MM. Blending containers and virtual machines. *ACM SIGPLAN/SIGOPS International Conference on Virtual Execution Environments (VEE).* 2020. doi:10.1145/3381052.3381315

> [43.1] In this paper, we perform a comparative study of Linux containers (LXC), gVisor secure containers, and Firecracker microVMs to understand how they use Linux kernel services differently: how much does their use of host kernel functionality vary? (Abstract)

`44.` Moebius F, Pfandzelter T, Bermbach D. Are Unikernels Ready for Serverless on the Edge?. *IEEE International Conference on Cloud Engineering.* 2024. doi:10.1109/ic2e61754.2024.00022

> [44.1] We find that unikernels exhibit desirable cold start performance, yet lag behind Linux microVMs in stability. (Abstract)

`45.` Wang C, Zheng Y. Sandlock: Confining AI Agent Code with Unprivileged Linux Primitives. *arXiv (Cornell University).* 2026;abs/2605.26298. doi:10.48550/arxiv.2605.26298

> [45.1] On our workstation, Sandlock adds roughly 5 ms of startup overhead and runs Redis at bare-metal throughput (within measurement noise); its pipeline operator further supports per-stage confinement for separating data, network, and untrusted-content capabilities. (Abstract)

`46.` Poole B, Jain A, Barron JT, Mildenhall B. DreamFusion: Text-to-3D using 2D Diffusion. *arXiv (Cornell University).* 2022;abs/2209.14988. doi:10.48550/arxiv.2209.14988

> [46.1] Using this loss in a DeepDream-like procedure, we optimize a randomly-initialized 3D model (a Neural Radiance Field, or NeRF) via gradient descent such that its 2D renderings from random angles achieve a low loss. (Abstract)

`47.` Lin C, Gao J, Tang L, et al. Magic3D: High-Resolution Text-to-3D Content Creation. *IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings - IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings.* 2023. doi:10.1109/cvpr52729.2023.00037

> [47.1] Our method, dubbed Magic3D, can create high quality 3D mesh models in 40 minutes, which is 2× faster than DreamFusion (reportedly taking 1.5 hours on average), while also achieving higher resolution. (Abstract)

> [47.2] User studies show 61.7% raters to prefer our approach over DreamFusion. (Abstract)

`48.` Tsalicoglou C, Manhardt F, Tonioni A, Niemeyer M, Tombari F. TextMesh: Generation of Realistic 3D Meshes From Text Prompts. *International Conference on 3D Vision (3DV).* 2024. doi:10.1109/3dv62453.2024.00154

> [48.1] First, rather than commonly used 3D meshes, they instead generate neural radiance fields (NeRFs), making them impractical for most real applications. (Abstract)

> [48.2] In addition, we propose a novel way to finetune the mesh texture, removing the effect of high saturation and improving the details of the output 3D mesh. (Abstract)

`49.` Sun C, Han J, Deng W, Wang X, Qin Z, Gould SJ. 3D-GPT: Procedural 3D Modeling with Large Language Models. *International Conference on 3D Vision (3DV).* 2025. doi:10.1109/3dv66043.2025.00119

> [49.1] Second, they integrate procedural generation, extracting parameter values from enriched text to seamlessly interface with 3D software for asset creation. (Abstract)

`50.` Hu Z, İşcen A, Jain A, et al. SceneCraft: An LLM Agent for Synthesizing 3D Scene as Blender Code. *arXiv (Cornell University).* 2024;abs/2403.01248. doi:10.48550/arxiv.2403.01248

> [50.1] SceneCraft first models a scene graph as a blueprint, detailing the spatial relationships among assets in the scene. (Abstract)

> [50.2] SceneCraft then writes Python scripts based on this graph, translating relationships into numerical constraints for asset layout. (Abstract)

`51.` Huang I, Yang G, Guibas LJ. BlenderAlchemy: Editing 3D Graphics with Vision-Language Models. *Lecture notes in computer science.* 2024. doi:10.1007/978-3-031-73024-5_18

> [51.1] Specifically, we design a vision-based edit generator and state evaluator to work together to find the correct sequence of actions to achieve the goal. (Abstract)

`52.` Sining L, Guan C, Dinh NA, Lang I, Holtzman A, Hanocka R. LL3M: Large Language 3D Modelers. *arXiv (Cornell University).* 2025;abs/2508.08228. doi:10.48550/arxiv.2508.08228

> [52.1] To construct BlenderRAG, we converted 1,729 official Blender 4.4 documentation HTML files into PDFs and injected them into a knowledge base in RAGFlow, enabling the retrieval agent to access version-specific knowledge for the Blender Python module. (Methods)

> [52.2] For the critic and verification agents, we render m = 5 views. (Methods)

> [52.3] In this phase, a critic agent ana- lyzes renders of the asset, identifies discrepancies with the prompt, and feeds targeted improvement suggestions back to the coding agent, while a verification agent ensures that these fixes are correctly applied. (Introduction)

`53.` Ji H, Aditya K, Escalante S, Qiu Y. CodeGen-3D: A Benchmark for Evaluating LLMs in Zero-Shot and Iterative 3D Modeling in Blender. *IEEE Access.* 2026;14:18181-18192. doi:10.1109/access.2026.3654948

> [53.1] Empirically, specialized and iterative systems achieve the lowest failure rates (3-4%), while general LLMs vary widely (21-92%, best 21%); CLIP scores tend to cluster even when perceptual quality differs; and pairwise judge preferences reveal qualitative separations and favor the iterative agent despite mid-pack CLIP. (Results)

> [53.2] This gap between CLIP-based alignment and judge-based preferences underscores the need for feedback-driven, multi-signal evaluation, suggesting that practical text-to-3D systems should combine strong general LLMs with domain specialization or inexpensive self-refinement loops, rather than relying on single-pass code generation. (Abstract)

`54.` Yang Y, Sun F, Weihs L, et al. Holodeck: Language Guided Generation of 3D Embodied AI Environments. *IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings - IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings.* 2024. doi:10.1109/cvpr52733.2024.01536

> [54.1] To address the challenge of positioning objects correctly, we prompt GPT-4 to generate spatial relational constraints between objects and then optimize the layout to satisfy those constraints. (Abstract)

`55.` Lu L, Lin C, Lin T, et al. Scenethesis: A Language and Vision Agentic Framework for 3D Scene Generation. *arXiv (Cornell University).* 2025;abs/2505.02836. doi:10.48550/arxiv.2505.02836

> [55.1] Next, an optimization module iteratively enforces accurate pose alignment and physical plausibility, preventing artifacts like object penetration and instability. (Abstract)

`56.` Yang Y, Jia B, Zhang S, Huang S. SceneWeaver: All-in-One 3D Scene Synthesis with an Extensible and Self-Reflective Agent. *neural information processing systems.* 2025;abs/2509.20414:155685-155717. doi:10.52202/085713-4689

> [56.1] At its core, SceneWeaver employs a language model-based planner to select from a suite of extensible scene generation tools, ranging from data-driven generative models to visual- and LLM-based methods, guided by self-evaluation of physical plausibility, visual realism, and semantic alignment with user input. (Abstract)

`57.` Liu X, Tai Y, Tang C. Agentic 3D Scene Generation with Spatially Contextualized VLMs. *arXiv (Cornell University).* 2025;abs/2505.20129. doi:10.48550/arxiv.2505.20129

> [57.1] Constructed from multimodal input, this context consists of three components: a scene portrait that provides a high-level semantic blueprint, a semantically labeled point cloud capturing object-level geometry, and a scene hypergraph that encodes rich spatial relationships, including unary, binary, and higher-order constraints. (Abstract)

`58.` Wan C, Mao Y, Wu W, et al. NaLA: A 3D Native LLM Layout Agent for High-Quality 3D Scene Generation. *Lecture notes in computer science.* 2026. doi:10.1007/978-3-032-37627-5_16

> [58.1] Existing layout agents still suffer from implausible layout generation because most of them convert 3D assets and 3D layouts into textual descriptions as inputs and outputs, which involves severe information loss due to the modality gap between texts and 3D assets and 3D layouts. (Abstract)

> [58.2] For the inputs, NaLA encodes 3D scene boundaries and 3D assets directly into the LLM, preserving fine-grained geometry and enabling explicit reasoning over relationships like collisions, surface supporting, and containment. (Abstract)

`59.` Li L, Shen C, Xie S, et al. HDSL: A Hierarchical Domain-Specific Language for Structured 3D Indoor Scene Generation and Localized Editing with LLM Agents. *International Joint Conference on Natural Language Processing and Conference of the Asia-Pacific Chapter of the Association for Computational Linguistics (IJCNLP-AACL).* 2026;abs/2606.09738:5519-5538. doi:10.18653/2026.aacl-long.247

> [59.1] In our reproduced benchmark, HDSL improves average object coverage, text-scene alignment, and generation time over full text-to-scene baselines while remaining competitive with recent layout-only reproductions on geometry metrics; for editing, HRAG reduces token use by $5.22\times$ and runtime by $6.19\times$, produces valid DSL for all eight paired edits, and better preserves unrelated scene objects. (Abstract)

`60.` Xu R, Zhu X, Ying J, Dong D, Ji Y, Tan X. MUSE: Agentic 3D Scene Authoring via Memory-Grounded Incremental Requirement Satisfaction. *arXiv (Cornell University).* 2026;abs/2606.14168. doi:10.48550/arxiv.2606.14168

> [60.1] In editing, it achieves 49.6 All-Goal success with near-perfect locality ( 99.9 preservation, 0.6 unintended changes). (Introduction)

`61.` Luo J, Tang J, Lu R, Zeng G. SceneAssistant: A Visual Feedback Agent for Open-Vocabulary 3D Scene Generation. *arXiv (Cornell University).* 2026;abs/2603.12238. doi:10.48550/arxiv.2603.12238

> [61.1] Furthermore, the framework's support for human-agent collaboration provides a flexible interface for creative 3D content creation. (Conclusion)

`62.` Bian Z, Ruohan R, Yang Y, Callison-Burch C. HOLODECK 2.0: Vision-Language-Guided 3D World Generation with Editing. *arXiv (Cornell University).* 2025;abs/2508.05899. doi:10.48550/arxiv.2508.05899

> [62.1] Additionally, HOLODECK 2.0 provides editing capabilities that flexibly adapt to human feedback, supporting layout refinement and style-consistent object edits. (Abstract)

`63.` Yin S, Ge J, Wang ZZ, et al. Vision-as-Inverse-Graphics Agent via Interleaved Multimodal Reasoning. *Lecture notes in computer science.* 2026;abs/2601.11109:557-575. doi:10.1007/978-3-032-37464-6_30

> [63.1] While current performance is bounded by the spatial perception capabilities of underlying VLMs and context window constraints in extremely long sequences, our framework is inherently extensible. (Conclusion)

`64.` Zhou M, Li R, Lyu X, et al. Articraft: An Agentic System for Scalable Articulated 3D Asset Generation. *arXiv (Cornell University).* 2026;abs/2605.15187. doi:10.48550/arxiv.2605.15187

> [64.1] The LLM writes code against a domain-specific SDK for defining parts, composing geometry, specifying joints, and writing tests to validate the resulting assets. (Abstract)

`65.` Juravsky J, Guo Y, Fidler S, Peng XB. PADL: Language-Directed Physics-Based Character Control. *SIGGRAPH Asia Conference Papers.* 2022. doi:10.1145/3550469.3555391

> [65.1] We present an adversarial imitation learning approach for training policies to map high-level language commands to low-level controls that enable a character to perform the desired task and skill specified by a user’s commands. (Abstract)

`66.` Huang H, De La Torre F, Fang CM, Banburski A, Amores J, Lanier J. Real-time Animation Generation and Control on Rigged Models via Large Language Models. *arXiv (Cornell University).* 2023;abs/2310.17838. doi:10.48550/arxiv.2310.17838

> [66.1] We showcase the robustness of our approach through qualitative results on various rigged models and motions. (Abstract)

`67.` Shreya ., Jadhav A, Sinhasane S, Vharkal P, Borde A. 3D Animation Generation Using Blender and Agentic AI. *International Conference on Interdisciplinary Technology & Science Convergence (FusionX Global).* 2025. doi:10.5220/0014628000004852

> [67.1] CONCLUSION This paper presented a structured multi-agent framework for converting natural language prompts into high-quality 3D models using an MCPenabled Blender execution environment.The proposed methodology employs specialized agents to support deterministic task decomposition, controlled execution, validation, and iterative refinement. (Conclusion)

`68.` Liu C, Wang X, Chen H, Zhao Y, Yang M, Jeni LA. SimWorlds: A Multi-Agent System for Dynamic 3D Scene Creation. *arXiv (Cornell University).* 2026;abs/2607.01766. doi:10.48550/arxiv.2607.01766

> [68.1] Checkpointing saves the live .blend after every closed stage, so a coder failure on stage s i +1 rolls back to the checkpoint written at the close of s i . (Body)

> [68.2] We present SimWorlds: a multi-agent framework that produces dynamic, editable 4D scenes from text, with Blender-specific procedural knowledge, a planner-coder-reviewer workflow driving a fixed ordered sequence of construction stages, a layered scene protocol enforced by a deterministic verifier, and a runtime-state inspection tool suite that catches mechanism failures the rendered image cannot reveal. (Abstract)

`69.` Huang Z, He Y, Yu J, et al. VBench: Comprehensive Benchmark Suite for Video Generative Models. *IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings - IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings.* 2024. doi:10.1109/cvpr52733.2024.02060

> [69.1] 1) Comprehensive Dimensions: VBench comprises 16 dimensions in video generation (e.g., subject identity in-consistency, motion smoothness, temporal flickering, and spatial relationship, etc.). (Abstract)

`70.` Zheng D, Huang Z, Hongbo L, et al. VBench-2.0: Advancing Video Generation Benchmark Suite for Intrinsic Faithfulness. *arXiv (Cornell University).* 2025;abs/2503.21755. doi:10.48550/arxiv.2503.21755

> [70.1] VBench-2.0 assesses five key dimensions: Human Fidelity, Controllability, Creativity, Physics, and Commonsense, each further broken down into fine-grained capabilities. (Abstract)

`71.` Hua DM, Wang X, Zeng B, et al. VABench: A Comprehensive Benchmark for Audio-Video Generation. *arXiv (Cornell University).* 2025;abs/2512.09299. doi:10.48550/arxiv.2512.09299

> [71.1] These dimensions specifically assess pairwise similarities (text-video, text-audio, video-audio), audio-video synchronization, lip-speech consistency, and carefully curated audio and video question-answering (QA) pairs, among others. (Abstract)

`72.` Dixit S, Saito K, Zhi Z, Mitsufuji Y, Donahue C. FoleyBench: A Benchmark for Video-to-Audio Models. *IEEE International Conference on Acoustics Speech and Signal Processing.* 2026;abs/2511.13219:14512-14516. doi:10.1109/icassp55912.2026.11461920

> [72.1] We find that 74% of videos from past evaluation datasets have poor audio-visual correspondence. (Abstract)

`73.` Shi H, Li Y, Deng N, et al. MSVBench: Towards Human-Level Evaluation of Multi-Shot Video Generation. *arXiv (Cornell University).* 2026;abs/2602.23969. doi:10.48550/arxiv.2602.23969

> [73.1] We further validate the reliability of our benchmark by demonstrating a state-of-the-art Spearman's rank correlation of 94.4% with human judgments. (Abstract)

`74.` Wei Y, Han Y, Chen Z, et al. MSAVBench: Towards Comprehensive and Reliable Evaluation of Multi-Shot Audio-Video Generation. *arXiv (Cornell University).* 2026;abs/2605.20183. doi:10.48550/arxiv.2605.20183

> [74.1] Furthermore, MSAVBench achieves high alignment with human judgments, reaching a Spearman rank correlation of 91.5%. (Abstract)

`75.` Yang J, Xia B, Chu R, et al. AVBench: Human-Aligned and Automated Evaluation Benchmark for Audio-Video Generative Models. *arXiv (Cornell University).* 2026;abs/2605.24652. doi:10.48550/arxiv.2605.24652

> [75.1] To address the lack of specialized training data, we construct large-scale supervision by transforming real-world videos into diverse training pairs with controlled perturbations. (Abstract)

`76.` Chen J, Chen Q, Zhang J, et al. DirectorBench: Diagnosing Long-Form Video Generation with Personalized Multi-Agent Evaluation. *arXiv (Cornell University).* 2026;abs/2605.30090. doi:10.48550/arxiv.2605.30090

> [76.1] All tool outcomes (success/fallback/failure with latency) are logged and injected into agent prompts to calibrate confidence, and full execution artifacts are saved as per-run debug logs and tool traces for auditability. (Methods)

> [76.2] The preprocessing toolchain combines ffprobe/ffmpeg video probing and audio extraction, PySceneDetect shot segmentation [4], representative-frame extraction, and OpenCV [3]-based boundary metrics (SSIM [31], color-histogram shift [16], optical-flow magnitude [33]). (Methods)

`77.` Shi H, Li Y, Chen X, Wang L, Hu B, Zhang M. AniMaker: Multi-Agent Animated Storytelling with MCTS-Driven Clip Generation. *SIGGRAPH Asia Conference Papers.* 2025. doi:10.1145/3757377.3764009

> [77.1] Central to AniMaker’s approach are two key technical components: MCTS-Gen in Photography Agent, an efficient Monte Carlo Tree Search (MCTS)-inspired strategy that intelligently navigates the candidate space to generate high-potential clips while optimizing resource usage; and AniEval in Reviewer Agent, the first framework specifically designed for multi-shot animation evaluation, which assesses critical aspects such as story-level consistency, action completion, and animation-specific features by considering each clip in the context of its preceding and succeeding clips. (Abstract)

`78.` Wei J, Zhang Y, Tan J, et al. VideoWeaver: Evaluating and Evolving Skills for Agentic Long Video Generation. *arXiv (Cornell University).* 2026;abs/2606.08091. doi:10.48550/arxiv.2606.08091

> [78.1] Given the user request, reference assets, execution trace, intermediate artifacts, and final video, the evaluation agent conducts a multi-turn evaluation. (Body)

`79.` Li Z, Sun L, Shi L, et al. FRAMEWORKERS: A Dynamic Multi-Agent Framework for AI-Generated Video Production. *arXiv (Cornell University).* 2026;abs/2608.29814. doi:10.48550/arxiv.2608.29814

> [79.1] An Assistant serves as the execution layer, grounding each selected task in a shared Workspace, retrieving the required assets and context, invoking the assigned sub-agent, and persisting the resulting artifacts. (Abstract)

`80.` Lin Y, Lin Z, Xing Z, et al. JarvisHub: An Open Harness for Canvas-Native Multimodal Creative Agents. *arXiv (Cornell University).* 2026;abs/2607.23588. doi:10.48550/arxiv.2607.23588

> [80.1] JarvisHub treats an editable canvas as the user workspace, the agent's external memory, action space, and shared project state, representing multimodal artifacts, dependencies, versions, and feedback as typed canvas nodes and links. (Abstract)

`81.` Cao Z, Zheng Y, Song R, Hu X. AgenticVBench: Can AI Agents Complete Real-World Post-Production Tasks?. *arXiv (Cornell University).* 2026;abs/2605.27705. doi:10.48550/arxiv.2605.27705

> [81.1] Both the tasks and their evaluation specifications are authored by these experts, combining programmatic verifiers and expert rubrics. (Abstract)

> [81.2] The best evaluated agent stack barely crosses 30%, far below human expert performance on the same tasks. (Abstract)

`82.` Kurtzer GM, Sochat V, Bauer MW. Singularity: Scientific containers for mobility of compute. *PLoS ONE.* 2017;12:e0177459-e0177459. doi:10.1371/journal.pone.0177459

> [82.1] Using Singularity containers, developers can work in reproducible environments of their choosing and design, and these complete environments can easily be copied and executed on other platforms. (Abstract)

`83.` Moreau D, Wiebels K, Boettiger C. Containers for computational reproducibility. *Nature Reviews Methods Primers.* 2023;3. doi:10.1038/s43586-023-00236-9

> [83.1] Advanced workflows in processing or analysis often require specific software versions and operating systems to run smoothly, and discrepancies across machines and researchers can impede reproducibility and efficient collaboration. (Abstract)

`84.` Wang Y, Zhang J, Wu Z, et al. From Agent Traces to Trust: A Survey of Evidence Tracing and Execution Provenance in LLM Agents. *arXiv (Cornell University).* 2026;abs/2606.04990. doi:10.48550/arxiv.2606.04990

> [84.1] This survey brings them into a common framework through execution provenance, a typed graph of an agent run, and evidence tracing, its projection onto evidence-support relations, distinguishing evidence that supports a conclusion from execution that influences it. (Abstract)

> [84.2] Applying this framework to the surveyed systems and benchmarks reveals three gaps: limited integration of fine-grained provenance with multiple trust functions, no benchmark in our catalog that evaluates the complete provenance chain, and little reporting of tamper-evident or attested traces. (Abstract)

`85.` Golaszewski E, Krawetz N, Sherman AT, et al. Verifying Provenance of Digital Media: Why the C2PA Specifications Fall Short. *Digital Repository at the University of Maryland (University of Maryland College Park).* 2026;abs/2604.24890. doi:10.13016/m2eogt-k80f

> [85.1] We find that the current C2PA specifications fail to achieve their claimed security goals. (Abstract)

`86.` Nemecek A, He H, Cheng G, Ayday E. Authenticated Contradictions from Desynchronized Provenance and Watermarking. *arXiv (Cornell University).* 2026;abs/2603.02378. doi:10.48550/arxiv.2603.02378

> [86.1] This work formalizes and empirically demonstrates the $\textit{Integrity Clash}$, a condition in which a digital asset carries a cryptographically valid C2PA manifest asserting human authorship while its pixels simultaneously carry a watermark identifying it as AI-generated, with both signals passing their respective verification checks in isolation. (Abstract)

`87.` Young J, Vaughan S, Jenks A, et al. Media Integrity and Authentication: Status, Directions, and Futures. *arXiv (Cornell University).* 2026;abs/2602.18681. doi:10.48550/arxiv.2602.18681

> [87.1] After defining each method, we analyze three representative technologies: cryptographically secured provenance, imperceptible watermarking, and soft-hash fingerprinting. (Abstract)

`88.` Liu WP, Liu Z, Dai E, et al. MCPAgentBench: A Real-world Task Benchmark for Evaluating LLM Agent MCP Tool Use. *Workshop for Research on Agent Language Models (REALM).* 2026;abs/2512.24565:764-773. doi:10.18653/2026.realm-1.51

> [88.1] The evaluation employs a dynamic sandbox environment that presents agents with candidate tool lists containing distractors, thereby testing their tool selection and discrimination abilities. (Abstract)

`89.` Wang Z, Qi C, Hemani P, et al. MCP-Bench: Benchmarking Tool-Using LLM Agents with Complex Real-World Tasks via MCP Servers. *arXiv (Cornell University).* 2025;abs/2508.20453. doi:10.48550/arxiv.2508.20453

> [89.1] We propose a multi-faceted evaluation framework covering tool-level schema understanding and usage, trajectory-level planning, and task completion. (Abstract)

`90.` Ye Z, Gao W, Hu Q, et al. Deep Learning Workload Scheduling in GPU Datacenters: A Survey. *ACM Computing Surveys.* 2023;56:1-38. doi:10.1145/3638757

> [90.1] This article surveys existing research efforts for both training and inference workloads. (Abstract)

`91.` Albayaydh WS, Zhao RC, Fléchais I. Beyond the Leaderboard: A Synthesis of Tool-Use, Planning, and Reasoning Failures in Large Language Model Agents. *arXiv (Cornell University).* 2026;abs/2607.05775. doi:10.48550/arxiv.2607.05775

> [91.1] Across the literature, we find that failures compound nonlinearly with task length, that strong performance on individual sub-tasks does not reliably translate into end-to-end success, and that additional scaffolding does not consistently improve reliability. (Abstract)

`92.` Xing H, Zhuang H, Zhao X, Huang Y, Tang Z, Zhang X. Recipes for Agents: Understanding Skills and Their Open Questions. *ACM SIGKDD Conference on Knowledge Discovery and Data Mining (KDD).* 2026. doi:10.1145/3770855.3818652

> [92.1] However, their value and limits remain open questions. (Abstract)

> [92.2] We then outline open questions in skill construction, composition, evaluation, portability, governance, and security, and conclude with a call for contribution. (Abstract)

`93.` Xu W, Huang C, Gao S, Shang S. LLM-Based Agents for Tool Learning: A Survey. *Data Science and Engineering.* 2025;10:533-563. doi:10.1007/s41019-025-00296-9

> [93.1] Thus, LLMs should first find appropriate tools and split the tool retrieval methods into two categories: training-based and non-training-based. (Abstract)

> [93.2] To accurately complete the user task, it is important to decompose the task into several sub-tasks and execute them in the correct order. (Abstract)

`94.` Suter F, Coleman T, Altintaş İ, et al. A terminology for scientific workflow systems. *Future Generation Computer Systems.* 2025;174:107974-107974. doi:10.1016/j.future.2025.107974

> [94.1] This terminology is composed of fives axes: workflow characteristics, composition, orchestration, data management, and metadata capture. (Abstract)

`95.` Jv R. How Do LLMs Fail In Agentic Scenarios? A Qualitative Analysis of Success and Failure Scenarios of Various LLMs in Agentic Simulations. *arXiv (Cornell University).* 2025;abs/2512.07497. doi:10.48550/arxiv.2512.07497

> [95.1] The observation that models frequently bypass grounding steps - guessing schemas rather than inspecting them - indicates that tool descriptions and system prompts should explicitly mandate verification before action, assuming use cases where not all schemas can be known in advance and loaded into the system prompt. (Discussion)

> [95.2] Providing agents with access to all available data sources may paradoxically reduce reliability by introducing distractor information that triggers semantic overreach. (Discussion)
