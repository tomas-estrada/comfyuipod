"""Registro de plantillas ComfyUI, validación de parámetros, renderizado de grafos y submit."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ..comfyui_client import ComfyUIClient
from ..progress import ProgressBroker
from ..projects import ProjectStore
from .validator import (
    EXACT_PLACEHOLDER_RE,
    PLACEHOLDER_RE,
    validate_all_templates,
)

EXPECTED_M2_TEMPLATE_IDS: tuple[str, ...] = (
    "vace_inpaint",
    "vace_restyle",
    "vace_swap",
    "wan_flf2v",
    "wan_i2v",
    "wan_t2v",
    "sdxl_image",
    # M3-D: pipeline Hunyuan3D 2.1 nativo (imagen -> malla GLB)
    "hunyuan3d_image_to_3d",
)

# Mapa de parámetro *_asset_id -> parámetro de archivo en nodo ComfyUI (*_input)
ASSET_PARAM_TO_INPUT_PARAM: dict[str, tuple[str, str]] = {
    "video_asset_id": ("video_input", "video"),
    "mask_asset_id": ("mask_input", "image"),
    "image_asset_id": ("image_input", "image"),
    "first_frame_asset_id": ("first_frame_input", "image"),
    "last_frame_asset_id": ("last_frame_input", "image"),
}


# Plantillas cuyo grafo exige el grupo de pesos 3D (Hunyuan3D, opt-in vía
# STUDIO_INCLUDE_3D=1 o MODEL_PROFILE=unreal). Sin esos pesos no se ofrecen ni
# aceptan submits (VAL-UNREAL-004).
THREE_D_TEMPLATE_IDS: frozenset[str] = frozenset({"hunyuan3d_image_to_3d"})
THREE_D_WEIGHTS_UNAVAILABLE = (
    "La plantilla 'hunyuan3d_image_to_3d' requiere los pesos de Hunyuan3D 2.1, "
    "que no están presentes en este pod. Descárgalos con STUDIO_INCLUDE_3D=1 "
    "(o MODEL_PROFILE=unreal) y reinicia el pod."
)


def template_requires_three_d(tpl: dict[str, Any]) -> bool:
    """¿La plantilla necesita el grupo 3D de pesos (categoría `three_d`)?"""
    return str(tpl.get("id")) in THREE_D_TEMPLATE_IDS or str(
        tpl.get("category")
    ) == "three_d"


class TemplateNotFoundError(LookupError):
    """Error cuando se solicita una plantilla inexistente."""


class TemplateParamError(ValueError):
    """Error cuando los parámetros suministrados para una plantilla son inválidos."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        status_code: int = 422,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or [{"message": message}]
        self.status_code = status_code


def build_json_schema_from_parameters(
    template_id: str,
    parameters: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Construye un JSON Schema estándar (draft 2020-12 / OpenAPI) para los parámetros."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, spec in parameters.items():
        prop: dict[str, Any] = {
            "type": spec["type"],
            "description": spec.get("description", ""),
        }
        if "default" in spec:
            prop["default"] = spec["default"]
        if "min" in spec and spec["min"] is not None:
            prop["minimum"] = spec["min"]
        if "max" in spec and spec["max"] is not None:
            prop["maximum"] = spec["max"]
        if "min_length" in spec and spec["min_length"] is not None:
            prop["minLength"] = spec["min_length"]
        if "asset_kind" in spec and spec["asset_kind"]:
            prop["x-asset-kind"] = spec["asset_kind"]

        properties[name] = prop
        if spec.get("required", False):
            required.append(name)

    return {
        "title": f"TemplateParams_{template_id}",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _coerce_and_validate_param_value(
    param_name: str,
    raw_val: Any,
    spec: dict[str, Any],
) -> Any:
    ptype = spec["type"]
    if ptype == "string":
        if not isinstance(raw_val, str):
            raise TemplateParamError(
                f"El parámetro {param_name!r} debe ser una cadena de texto (string).",
                errors=[{"field": param_name, "reason": "type_error.string"}],
            )
        val_str = raw_val.strip() if spec.get("required") else raw_val
        min_len = spec.get("min_length")
        if spec.get("required") and not val_str:
            raise TemplateParamError(
                f"El parámetro obligatorio {param_name!r} no puede estar vacío.",
                errors=[{"field": param_name, "reason": "value_error.blank"}],
            )
        if min_len is not None and len(val_str) < int(min_len):
            raise TemplateParamError(
                f"El parámetro {param_name!r} debe tener al menos {min_len} caracteres.",
                errors=[{"field": param_name, "reason": "value_error.min_length"}],
            )
        return val_str

    if ptype == "boolean":
        if not isinstance(raw_val, bool):
            raise TemplateParamError(
                f"El parámetro {param_name!r} debe ser booleano (true/false).",
                errors=[{"field": param_name, "reason": "type_error.boolean"}],
            )
        return raw_val

    if ptype == "integer":
        if isinstance(raw_val, bool) or not isinstance(raw_val, int):
            raise TemplateParamError(
                f"El parámetro {param_name!r} debe ser un entero (integer).",
                errors=[{"field": param_name, "reason": "type_error.integer"}],
            )
        min_v = spec.get("min")
        max_v = spec.get("max")
        if min_v is not None and raw_val < int(min_v):
            raise TemplateParamError(
                f"El parámetro {param_name!r} ({raw_val}) es menor que el "
                f"mínimo permitido ({min_v}).",
                errors=[{"field": param_name, "reason": "value_error.number.not_ge"}],
            )
        if max_v is not None and raw_val > int(max_v):
            raise TemplateParamError(
                f"El parámetro {param_name!r} ({raw_val}) supera el "
                f"máximo permitido ({max_v}).",
                errors=[{"field": param_name, "reason": "value_error.number.not_le"}],
            )
        return raw_val

    if ptype == "number":
        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, float)):
            raise TemplateParamError(
                f"El parámetro {param_name!r} debe ser numérico (number).",
                errors=[{"field": param_name, "reason": "type_error.number"}],
            )
        val_num = float(raw_val)
        min_v = spec.get("min")
        max_v = spec.get("max")
        if min_v is not None and val_num < float(min_v):
            raise TemplateParamError(
                f"El parámetro {param_name!r} ({val_num}) es menor que el "
                f"mínimo permitido ({min_v}).",
                errors=[{"field": param_name, "reason": "value_error.number.not_ge"}],
            )
        if max_v is not None and val_num > float(max_v):
            raise TemplateParamError(
                f"El parámetro {param_name!r} ({val_num}) supera el "
                f"máximo permitido ({max_v}).",
                errors=[{"field": param_name, "reason": "value_error.number.not_le"}],
            )
        return val_num

    return raw_val


def _substitute_placeholders(value: Any, context: dict[str, Any]) -> Any:
    """Reemplaza placeholders {{param}} preservando el tipo nativo cuando es exacto."""
    if isinstance(value, str):
        exact = EXACT_PLACEHOLDER_RE.match(value)
        if exact is not None:
            key = exact.group(1)
            if key in context:
                return context[key]
        return PLACEHOLDER_RE.sub(
            lambda m: str(context.get(m.group(1), m.group(0))),
            value,
        )
    if isinstance(value, dict):
        return {k: _substitute_placeholders(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_placeholders(item, context) for item in value]
    return value


class TemplateRegistry:
    """Catálogo en memoria de plantillas curadas en formato API de ComfyUI."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = (
            Path(data_dir).resolve()
            if data_dir is not None
            else (Path(__file__).resolve().parent / "data").resolve()
        )
        self._templates: dict[str, dict[str, Any]] = validate_all_templates(self.data_dir)

    def reload(self) -> None:
        self._templates = validate_all_templates(self.data_dir)

    def list_ids(self) -> list[str]:
        ordered: list[str] = [
            tid for tid in EXPECTED_M2_TEMPLATE_IDS if tid in self._templates
        ]
        for tid in sorted(self._templates.keys()):
            if tid not in ordered:
                ordered.append(tid)
        return ordered

    def get_raw_definition(self, template_id: str) -> dict[str, Any]:
        cleaned = (template_id or "").strip()
        tpl = self._templates.get(cleaned)
        if tpl is None:
            allowed = ", ".join(self.list_ids())
            raise TemplateNotFoundError(
                f"Plantilla desconocida {cleaned!r}. Plantillas disponibles: {allowed}."
            )
        return tpl

    def _format_entry(
        self,
        tpl: dict[str, Any],
        *,
        include_workflow: bool = False,
        three_d_available: bool = True,
    ) -> dict[str, Any]:
        params_schema = build_json_schema_from_parameters(tpl["id"], tpl["parameters"])
        available = not template_requires_three_d(tpl) or three_d_available
        entry: dict[str, Any] = {
            "id": tpl["id"],
            "name": tpl["name"],
            "category": tpl["category"],
            "output_kind": tpl["output_kind"],
            "summary": tpl["summary"],
            "description": tpl["description"],
            "performance_defaults": copy.deepcopy(tpl["performance_defaults"]),
            "models": list(tpl.get("models") or []),
            "required_nodes": list(tpl.get("required_nodes") or []),
            "parameters": copy.deepcopy(tpl["parameters"]),
            "params_schema": params_schema,
            "docs": copy.deepcopy(tpl["docs"]),
            "available": available,
            "unavailable_reason": None if available else THREE_D_WEIGHTS_UNAVAILABLE,
        }
        if include_workflow:
            entry["workflow"] = copy.deepcopy(tpl["workflow"])
        return entry

    def list_templates(
        self,
        *,
        include_workflow: bool = False,
        three_d_available: bool = True,
        include_unavailable: bool = True,
    ) -> list[dict[str, Any]]:
        entries = [
            self._format_entry(
                self._templates[tid],
                include_workflow=include_workflow,
                three_d_available=three_d_available,
            )
            for tid in self.list_ids()
        ]
        if include_unavailable:
            return entries
        return [entry for entry in entries if entry["available"]]

    def get_template(
        self,
        template_id: str,
        *,
        include_workflow: bool = True,
        three_d_available: bool = True,
    ) -> dict[str, Any]:
        raw = self.get_raw_definition(template_id)
        return self._format_entry(
            raw,
            include_workflow=include_workflow,
            three_d_available=three_d_available,
        )

    def validate_params(
        self,
        template_id: str,
        raw_params: dict[str, Any] | None,
        *,
        strict: bool = True,
    ) -> dict[str, Any]:
        """Valida y completa con defaults los parámetros para una plantilla."""
        tpl = self.get_raw_definition(template_id)
        param_specs: dict[str, dict[str, Any]] = tpl["parameters"]

        if raw_params is None:
            provided: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            provided = dict(raw_params)
        else:
            raise TemplateParamError(
                "El bloque 'params' debe ser un objeto JSON.",
                errors=[{"field": "params", "reason": "type_error.object"}],
            )

        if strict:
            unknown_keys = [k for k in provided if k not in param_specs]
            if unknown_keys:
                unknown_str = ", ".join(repr(k) for k in unknown_keys)
                raise TemplateParamError(
                    f"Parámetro(s) no reconocido(s) para la plantilla "
                    f"{template_id!r}: {unknown_str}.",
                    errors=[
                        {"field": k, "reason": "value_error.extra_forbidden"}
                        for k in unknown_keys
                    ],
                )

        resolved: dict[str, Any] = {}
        for param_name, spec in param_specs.items():
            if param_name in provided and provided[param_name] is not None:
                resolved[param_name] = _coerce_and_validate_param_value(
                    param_name,
                    provided[param_name],
                    spec,
                )
            elif spec.get("required", False):
                raise TemplateParamError(
                    f"Falta el parámetro obligatorio {param_name!r} "
                    f"para la plantilla {template_id!r}.",
                    errors=[{"field": param_name, "reason": "value_error.missing"}],
                )
            else:
                resolved[param_name] = copy.deepcopy(spec.get("default"))

        return resolved

    def render_workflow(
        self,
        template_id: str,
        params: dict[str, Any] | None = None,
        *,
        project_id: str | None = None,
        job_id: str | None = None,
        strict: bool = False,
    ) -> dict[str, Any]:
        """Renderiza el grafo ComfyUI API-format sustituyendo todos los placeholders."""
        tpl = self.get_raw_definition(template_id)
        validated_params = self.validate_params(
            template_id,
            params,
            strict=strict,
        )

        eff_project = (project_id or "default").strip() or "default"
        eff_job = (job_id or "render").strip() or "render"
        output_prefix = f"projects/{eff_project}/outputs/{template_id}_{eff_job}"

        context: dict[str, Any] = {
            **validated_params,
            "project_id": eff_project,
            "job_id": eff_job,
            "template_id": template_id,
            "output_prefix": output_prefix,
        }

        return _substitute_placeholders(copy.deepcopy(tpl["workflow"]), context)

    async def submit_render(
        self,
        template_id: str,
        raw_params: dict[str, Any] | None,
        *,
        project_id: str | None,
        store: ProjectStore,
        comfyui_client: ComfyUIClient,
        broker: ProgressBroker | None = None,
        strict: bool = True,
        three_d_available: bool = True,
    ) -> dict[str, Any]:
        """Valida params, sube assets de entrada, encola job y registra salidas en galería."""
        tpl = self.get_raw_definition(template_id)
        if template_requires_three_d(tpl) and not three_d_available:
            raise TemplateParamError(
                THREE_D_WEIGHTS_UNAVAILABLE,
                errors=[
                    {"field": "template_id", "reason": "value_error.weights_missing"}
                ],
            )
        validated_params = self.validate_params(template_id, raw_params, strict=strict)

        # Resolver proyecto destino (o crear el proyecto por defecto si no se especificó)
        if project_id:
            project = store.get_project(project_id)
            if project is None:
                raise LookupError(f"Proyecto {project_id!r} no encontrado")
            eff_project_id = project["id"]
        else:
            default_proj = store.get_project("default")
            if default_proj is None:
                default_proj = store.create_project(
                    name="Proyecto por defecto",
                    description="Proyecto automático para renders directos de plantillas",
                    project_id="default",
                )
            eff_project_id = default_proj["id"]

        # Validar que cualquier *_asset_id referenciado exista en el proyecto antes de ComfyUI
        assets_to_upload: list[tuple[str, dict[str, Any], Path]] = []
        for asset_param, (input_param, expected_kind) in ASSET_PARAM_TO_INPUT_PARAM.items():
            asset_id = str(validated_params.get(asset_param) or "").strip()
            if not asset_id:
                continue
            asset = store.get_asset(eff_project_id, asset_id)
            asset_path = store.get_asset_path(eff_project_id, asset_id)
            if asset is None or asset_path is None:
                raise TemplateParamError(
                    f"El asset referenciado {asset_id!r} en {asset_param!r} no existe "
                    f"en el proyecto {eff_project_id!r}.",
                    errors=[{"field": asset_param, "reason": "value_error.asset_not_found"}],
                )
            if expected_kind and asset.get("kind") != expected_kind:
                raise TemplateParamError(
                    f"El asset {asset_id!r} ({asset_param}) es de tipo {asset.get('kind')!r}, "
                    f"pero la plantilla {template_id!r} requiere {expected_kind!r}.",
                    errors=[{"field": asset_param, "reason": "value_error.asset_wrong_kind"}],
                )
            assets_to_upload.append((input_param, asset, asset_path))

        # Subir assets de entrada validados a ComfyUI
        for input_param, asset, asset_path in assets_to_upload:
            if asset["kind"] == "video":
                up_res = await comfyui_client.upload_video(
                    asset_path,
                    filename=asset["filename"],
                    subfolder=eff_project_id,
                    mime_type=asset["mime_type"],
                )
            else:
                up_res = await comfyui_client.upload_image(
                    asset_path,
                    filename=asset["filename"],
                    subfolder=eff_project_id,
                    mime_type=asset["mime_type"],
                )
            validated_params[input_param] = up_res.relative_path

        # Crear el job en SQLite y publicar evento inicial en SSE
        job = store.create_job(
            kind="render",
            project_id=eff_project_id,
            template_id=tpl["id"],
            params=validated_params,
        )
        if broker is not None:
            broker.publish(
                "job",
                {
                    "job_id": job["id"],
                    "project_id": eff_project_id,
                    "kind": "render",
                    "status": job["status"],
                    "template_id": tpl["id"],
                },
            )

        workflow = self.render_workflow(
            tpl["id"],
            validated_params,
            project_id=eff_project_id,
            job_id=job["id"],
            strict=False,
        )

        exec_result = await comfyui_client.execute_prompt_job(
            workflow,
            job_id=job["id"],
            project_id=eff_project_id,
            template_id=tpl["id"],
        )

        updated_job = store.get_job(job["id"]) or job
        gallery_assets = [
            a
            for a in store.list_assets(eff_project_id, category="outputs")
            if a.get("job_id") == job["id"]
        ]
        outputs_list = [
            o.model_dump() if hasattr(o, "model_dump") else dict(o)
            for o in exec_result.outputs
        ]
        return {
            "status": updated_job["status"],
            "job_id": job["id"],
            "prompt_id": exec_result.prompt_id,
            "project_id": eff_project_id,
            "template_id": tpl["id"],
            "params": validated_params,
            "workflow": workflow,
            "outputs": outputs_list,
            "gallery_assets": gallery_assets,
        }


_DEFAULT_REGISTRY: TemplateRegistry | None = None


def get_default_registry() -> TemplateRegistry:
    """Devuelve la instancia singleton del registro de plantillas."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = TemplateRegistry()
    return _DEFAULT_REGISTRY


def render_template_workflow(
    template_id: str,
    params: dict[str, Any] | None = None,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Helper de módulo para renderizar el workflow API de una plantilla."""
    return get_default_registry().render_workflow(
        template_id,
        params,
        project_id=project_id,
        job_id=job_id,
        strict=strict,
    )
