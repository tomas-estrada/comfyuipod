"""Adaptador del motor ComfyUI al contrato de motores (M4-SCR-002, F2).

ComfyUI es el motor RESIDENTE (siempre en GPU). Este adaptador lo expone con
los cuatro verbos del contrato, delegando en el `ComfyUIClient` ya existente
(no reimplementa HTTP ni WebSocket):

  - `list`    → GET /object_info: qué node TYPES (submotores) hay instalados.
  - `install` → instala un nodo vía ComfyUI-Manager (bajo PIP_CONSTRAINT), solo
                si STUDIO_DYNAMIC=1 o el nodo ya está horneado. F2 deja el punto
                de cableado del Manager marcado; la política de guarda ya aplica.
  - `verify`  → re-consulta object_info y confirma que el node type existe.
  - `run`     → POST /prompt (+ espera) vía `submit_prompt`/`execute_prompt_job`.

Async porque `ComfyUIClient` lo es. Devuelve el MISMO `EngineResult` tipado que
los motores síncronos: el contrato es la forma de la respuesta, no el color del
método. El agente ve la misma herramienta uniforme.

`build_graph` (que el agente componga grafos no-plantilla y los corra) se apoya
en `list` (para saber qué node types puede referenciar) + `run`.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError
from comfy_studio.engines import (
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_dynamic_enabled,
)


class ComfyUIEngine:
    """Motor ComfyUI (residente). Contrato de 4 verbos, versión async."""

    name = "comfyui"
    tier = EngineTier.RESIDENT

    def __init__(self, client: ComfyUIClient) -> None:
        self._client = client

    # -- list ----------------------------------------------------------------

    async def list(self) -> EngineResult:
        """Node types instalados (submotores disponibles para build_graph)."""
        try:
            info = await self._client.get_object_info()
        except ComfyUIError as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="list", error=f"object_info falló: {exc}"
            )
        node_types = sorted(info.keys()) if isinstance(info, dict) else []
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="list",
            payload={"node_types": node_types, "count": len(node_types)},
        )

    # -- install -------------------------------------------------------------

    async def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        """Instala un custom node vía ComfyUI-Manager.

        Guarda de política: en pod determinista (STUDIO_DYNAMIC off) y nodo no
        horneado, se rechaza de forma tipada — no se instala nada a mitad de
        ejecución sin autorización dinámica. El endpoint del Manager es
        configurable (varía entre versiones) y la instalación queda acotada por
        PIP_CONSTRAINT en el entorno de ComfyUI (AGENTS.md regla 6). Tras
        instalar, se re-verifica con object_info; no se finge éxito.
        """
        if not is_dynamic_enabled():
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="install",
                error=(
                    f"install de '{submotor}' requiere STUDIO_DYNAMIC=1; en pod "
                    "determinista los nodos vienen horneados (nodes.lock)"
                ),
            )

        import os

        from comfy_studio.comfyui_client import ComfyUIError

        endpoint = os.environ.get("STUDIO_COMFY_MANAGER_INSTALL_PATH") or "/customnode/install"
        body: dict = {"id": submotor}
        if ref:
            body["version"] = ref
        try:
            resp = await self._client._request(  # noqa: SLF001 - ruta interna del cliente
                "POST", endpoint, json_body=body, timeout=600.0
            )
        except ComfyUIError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="install",
                error=f"install vía Manager falló: {exc}",
            )
        if resp.status_code >= 400:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="install",
                error=f"Manager respondió HTTP {resp.status_code} al instalar '{submotor}'",
            )
        # Re-verificar: el node type debe aparecer en object_info tras instalar.
        verify = await self.verify(submotor)
        if not verify.ok:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="install",
                error=(
                    f"'{submotor}' instalado pero no visible en object_info todavía; "
                    "puede requerir reinicio de ComfyUI"
                ),
                payload={"installed": submotor, "visible": False},
            )
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="install",
            payload={"installed": submotor, "visible": True},
        )

    # -- verify --------------------------------------------------------------

    async def verify(self, submotor: str) -> EngineResult:
        """Confirma que un node type existe antes de referenciarlo en un grafo."""
        try:
            info = await self._client.get_object_info(node_class=submotor)
        except ComfyUIError as exc:
            return EngineResult(
                ok=False, engine=self.name, verb="verify", error=f"verify falló: {exc}"
            )
        present = isinstance(info, dict) and submotor in info
        return EngineResult(
            ok=present,
            engine=self.name,
            verb="verify",
            payload={"node_type": submotor, "present": present},
            error=None if present else f"node type '{submotor}' no instalado",
        )

    # -- run -----------------------------------------------------------------

    async def run(self, spec: EngineRunSpec) -> EngineResult:
        """Corre un grafo en formato API de ComfyUI (plantilla o build_graph)."""
        graph = spec.job.get("graph") if isinstance(spec.job, dict) else None
        if not isinstance(graph, dict) or not graph:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error="spec.job['graph'] debe ser un grafo API de ComfyUI no vacío",
            )
        job_id = spec.job.get("job_id")
        project_id = spec.job.get("project_id")
        try:
            result = await self._client.execute_prompt_job(
                graph, job_id=job_id, project_id=project_id
            )
        except ComfyUIError as exc:
            return EngineResult(
                ok=False,
                engine=self.name,
                verb="run",
                error=f"run falló: {exc}",
            )
        outputs = _result_outputs(result)
        return EngineResult(
            ok=True,
            engine=self.name,
            verb="run",
            payload={"outputs": outputs, "count": len(outputs)},
        )


def _result_outputs(result: Any) -> list[str]:
    """Extrae rutas de salida de un PromptResult de forma defensiva."""
    files = getattr(result, "outputs", None) or getattr(result, "files", None) or []
    paths: list[str] = []
    for f in files:
        rel = getattr(f, "relative_path", None)
        if callable(rel):
            try:
                paths.append(str(rel()))
                continue
            except Exception:  # noqa: BLE001
                pass
        p = getattr(f, "path", None) or getattr(f, "filename", None)
        if p:
            paths.append(str(p))
    return paths
