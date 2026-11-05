# Fuentes y alcance de verificación

Documento preparado el 2026-11-05 UTC para https://github.com/tomas-estrada/comfyuipod.

El marco anterior está incluido como contexto histórico; `GOAL.md` y los anexos actuales prevalecen sobre instrucciones antiguas. Una URL o un commit leído no es una ejecución de un modelo. No se han descargado pesos ni ejecutado pruebas GPU para este paquete.

## Verificaciones actuales

- **R00. ComfyUIPod: main y árbol de partida.** <https://github.com/tomas-estrada/comfyuipod/tree/b54195f0976d6c57b0ee04962e3059219db55d57>
  Alcance: read_repository_files_and_tree. Límite: No GPU execution; baseline source snapshot.

- **R02. Atlas PR#63.** <https://github.com/tomas-estrada/comfyuipod/pull/63> · <https://github.com/tomas-estrada/comfyuipod/tree/c12618815c5e7181bbb69e4ce9d249a0075fd3c4>
  Alcance: read_metadata_manifest_service_acquire_and_adapters. Límite: Counts are manifest declarations, not independent visual audit.

- **R03. CI de main revisada.** <https://github.com/tomas-estrada/comfyuipod/actions/runs/41964177024>
  Alcance: read_workflow_run. Límite: Success applies to baseline SHA, not a future Ultra release.

- **K01. Kiro Crew: agentes.** <https://github.com/kirodotdev/KiroCrew/blob/4a546774f478c27058d61e7005ca931fd01b1c97/src/kiro_crew/docs/agents.md>
  Alcance: read_full_upstream_documentation. Límite: Installed edition/version determines actual tools and UI.

- **K02. Kiro Crew: work ledger.** <https://github.com/kirodotdev/KiroCrew/blob/4a546774f478c27058d61e7005ca931fd01b1c97/src/kiro_crew/docs/work-ledger.md>
  Alcance: read_full_upstream_documentation. Límite: Native acceptance kind and create-bind-seed semantics; not CrewAI framework APIs.

- **K03. Kiro Crew: Task Runner.** <https://kiro.dev/docs/crew/features/task-runner/> · <https://github.com/kirodotdev/KiroCrew/blob/4a546774f478c27058d61e7005ca931fd01b1c97/src/kiro_crew/docs/task-runner.md>
  Alcance: read_official_page_and_source. Límite: Markdown spec and verification; CLI permissions/version-specific.

- **K04. OpenCode frontend y licencia.** <https://github.com/anomalyco/opencode/tree/09dcb47861fe9253814dc85b12f31a5b2691eda6> · <https://github.com/anomalyco/opencode/blob/09dcb47861fe9253814dc85b12f31a5b2691eda6/LICENSE> · <https://github.com/anomalyco/opencode/blob/09dcb47861fe9253814dc85b12f31a5b2691eda6/packages/app/package.json> · <https://opencode.ai/docs/sdk/> · <https://opencode.ai/docs/server/>
  Alcance: read_readme_license_package_and_official_api_docs. Límite: MIT reuse does not make Studio API OpenCode SDK-compatible.

- **K05. HyperFrames CLI local/cloud.** <https://github.com/heygen-com/hyperframes/blob/7862e271d16baa9052c78d3b85535ebc725cd136/docs/packages/cli.mdx> · <https://github.com/heygen-com/hyperframes/blob/7862e271d16baa9052c78d3b85535ebc725cd136/LICENSE>
  Alcance: read_full_official_cli_and_license. Límite: Local rendering separate from hosted/account/billed features.

- **K06. LTX-2.5: selección de pipelines.** <https://github.com/Lightricks/LTX-2/tree/c1b4675863c07c59c92e502446cccdaedbe5889c> · <https://github.com/Lightricks/LTX-2/blob/c1b4675863c07c59c92e502446cccdaedbe5889c/packages/ltx-pipelines/docs/pipeline-selection.md>
  Alcance: read_readme_and_selection_doc. Límite: DFR/distilled and exact encoder/components; no own memory benchmark.

- **K07. MiniMax-H3: alcance y licencia.** <https://design.minimax.io/h3> · <https://github.com/MiniMax-AI/MiniMax-H3> · <https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE>
  Alcance: read_official_guide_and_readme. Límite: 768p local vs hosted Context-IR/2K; territory and training-use restrictions; license URL must be pinned before integration.

- **K08. PyTorch: introducción de soporte Blackwell.** <https://pytorch.org/blog/pytorch-2-7/> · <https://pytorch.org/get-started/previous-versions/>
  Alcance: read_official_release_and_version_matrix. Límite: Historical compatibility milestone, not an instruction to pin Ultra globally to Torch2.7.

- **K09. GitHub: recursos de runners.** <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>
  Alcance: read_official_runner_documentation. Límite: Actual runner/build resources must be measured, not assumed enough for a giant image.

- **K10. Geofabrik Colombia.** <https://download.geofabrik.de/south-america/colombia.html> · <https://www.openstreetmap.org/copyright>
  Alcance: read_official_download_and_license_reference. Límite: Geographic data/topology are not facade photographs.

- **K11. Wikimedia: licencias por medio y metadata.** <https://attribution.wikimedia.org/attribution-signals/license.html> · <https://commons.wikimedia.org/wiki/Help:Machine-readable_data> · <https://www.mediawiki.org/wiki/API:Imageinfo>
  Alcance: read_official_docs. Límite: Each file has its own license/author; site text license is not a media blanket grant.

- **K12. KartaView: imágenes y API.** <https://kartaview.org/terms> · <https://kartaview.org/doc> · <https://kartaview.org/landing/open-imagery>
  Alcance: read_official_terms_and_docs. Límite: Coverage and API accessibility in the actual target area remain to be queried; no city coverage guarantee.

- **K13. YouTube: acceso/quotas.** <https://developers.google.com/youtube/v3/getting-started>
  Alcance: read_official_api_overview. Límite: Optional metadata connector; registration/quota/authorization are real requirements, not free universal video downloads.

- **K14. YouTube: captions download.** <https://developers.google.com/youtube/v3/docs/captions/download>
  Alcance: read_official_method. Límite: Public metadata availability does not imply caption download permission.

- **K15. Remotion: licencia propia.** <https://github.com/remotion-dev/remotion/blob/main/LICENSE.md> · <https://www.remotion.dev/docs/license/faq> · <https://www.remotion.dev/docs/license/>
  Alcance: read_official_license_and_faq. Límite: Personal free eligibility does not relicense the software or all redistributions.

- **K16. GVM original.** <https://github.com/aim-uofa/GVM>
  Alcance: read_full_official_readme. Límite: One-step release differs from paper results; resolve exact code/base/weight licenses before adoption.

- **K17. LangGraph: persistencia.** <https://docs.langchain.com/oss/python/langgraph/persistence>
  Alcance: read_official_docs. Límite: Checkpointer and long-term store have different responsibilities.

- **K18. Cosmos: matriz actual de recetas.** <https://nvidia.github.io/cosmos-framework/skills/cookbooks/cosmos3/nim/support-matrix/>
  Alcance: read_official_matrix. Límite: NIM requirements are recipe-specific. Nano default recommendation on RTX PRO6000; Super is explicit experiment.

- **K19. CrewAI: agentes.** <https://docs.crewai.com/v1.15.35/en/concepts/agents>
  Alcance: read_official_docs. Límite: CrewAI is distinct from Kiro Crew; goal field and actual tool grants/version determine operation.

- **K20. CrewAI: flows/persistencia.** <https://docs.crewai.com/v1.15.35/en/concepts/flows>
  Alcance: read_official_docs. Límite: Use installed APIs; do not import Kiro work-ledger tool names into CrewAI.

## Investigación previa y relecturas

La lista completa heredada (S01–S47, N01–N31 y fuentes parciales) está en sources.json y contexto/Marco_Anterior.md. Los registros indican qué lecturas se renovaron; una referencia heredada no afirma verificación reciente. Los commits de evidencia no se convierten automáticamente en locks de producción: el worker debe fijar y comprobar los seleccionados.


## Herramientas base

- **K21. FFmpeg:** https://github.com/FFmpeg/FFmpeg ; licencia según build/codec.
- **K22. WhisperX:** https://github.com/m-bain/whisperX ; alineación/diarización y componentes separados.
- **K23. Blender:** https://github.com/blender/blender ; renderer/escena headless con addons/assets revisados.
