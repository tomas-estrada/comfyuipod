"""Invariante: los secretos nunca llegan al proveedor LLM en los mensajes.

Causa (pre-fix): `StudioAgent.stream_turn` construye `messages` con el texto
crudo del usuario y el mensaje de sistema (nombre del proyecto, ficheros,
resumen, plan) sin redactar, y `OpenAICompatibleLLMClient` envía `messages`
tal cual al proveedor (`_http_complete` / `_complete_with_stub`). Un token
`hf_*` o la clave literal del config presente en el mensaje de usuario o en
el nombre del proyecto llegaba intacto al stub/proveedor.

El test usa el doble determinista del proyecto (`StubLLMClient`, el que el
sistema usa en CI con `STUDIO_STUB_LLM=1`) y verifica que los mensajes
recordados no contienen el secreto, nunca el texto exacto de un prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_studio.agent.llm import OpenAICompatibleLLMClient
from comfy_studio.agent.loop import StudioAgent
from comfy_studio.agent.tools import create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import SECRET_PLACEHOLDER, StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.stubs.llm import StubLLMClient, StubLLMstep

HF_TOKEN = "hf_" + "A1b2C3d4E5f6G7h8I9j0"


@pytest.mark.anyio
async def test_outgoing_messages_redact_hf_token_in_user_message(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto redact-outgoing")
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(StubLLMstep(content="Recibido."))
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
        tool_belt=create_default_tool_belt(),
    )

    await agent.run_turn(
        project_id=project["id"],
        user_message=f"Usa este token para el test {HF_TOKEN} por favor",
    )

    assert stub_llm.calls, "el stub debe haber registrado la llamada"
    sent = json.dumps(stub_llm.calls, ensure_ascii=False)
    assert HF_TOKEN not in sent
    assert SECRET_PLACEHOLDER in sent


@pytest.mark.anyio
async def test_outgoing_messages_redact_secret_in_project_name(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name=f"Corto {HF_TOKEN}")
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(StubLLMstep(content="Recibido."))
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
        tool_belt=create_default_tool_belt(),
    )

    await agent.run_turn(project_id=project["id"], user_message="sigue")

    assert stub_llm.calls, "el stub debe haber registrado la llamada"
    sent = json.dumps(stub_llm.calls, ensure_ascii=False)
    assert HF_TOKEN not in sent


@pytest.mark.anyio
async def test_llm_client_redacts_messages_before_stub_provider(
    studio_config: StudioConfig,
) -> None:
    """El cliente LLM redacta los mensajes salientes antes de invocar al modelo."""
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(StubLLMstep(content="ok"))
    client = OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm)
    primary_key = studio_config.get_llm_key()
    assert primary_key

    resp = await client.complete(
        messages=[
            {"role": "system", "content": f"clave {primary_key} y token {HF_TOKEN}"},
            {"role": "user", "content": "hola"},
        ]
    )
    assert resp.content == "ok"
    assert stub_llm.calls
    sent = json.dumps(stub_llm.calls, ensure_ascii=False)
    assert HF_TOKEN not in sent
    assert primary_key not in sent
    assert SECRET_PLACEHOLDER in sent
