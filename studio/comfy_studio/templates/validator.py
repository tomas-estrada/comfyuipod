"""Validador estructural para las plantillas de workflow ComfyUI en formato API."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
EXACT_PLACEHOLDER_RE = re.compile(r"^\{\{\s*([a-zA-Z0-9_]+)\s*\}\}$")

BUILTIN_CONTEXT_PLACEHOLDERS: frozenset[str] = frozenset(
    {"output_prefix", "project_id", "job_id", "template_id"}
)

TERMINAL_OUTPUT_CLASSES: frozenset[str] = frozenset({"SaveImage", "VHS_VideoCombine"})

VALID_PARAM_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean"}
)

# Catálogo de nodos ComfyUI permitidos en plantillas según contratos pinneados
# (ComfyUI bdc63e72f5ab29fde448c161db3057827148c0ef / VHS 4d907bee61e92c2e65af3bd6383a4e4d356126d1)
KNOWN_NODE_SPECS: dict[str, dict[str, Any]] = {
    "UNETLoader": {
        "required_inputs": ("unet_name", "weight_dtype"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "CLIPLoader": {
        "required_inputs": ("clip_name", "type"),
        "optional_inputs": ("device",),
        "outputs": 1,
    },
    "CLIPVisionLoader": {
        "required_inputs": ("clip_name",),
        "optional_inputs": (),
        "outputs": 1,
    },
    "VAELoader": {
        "required_inputs": ("vae_name",),
        "optional_inputs": (),
        "outputs": 1,
    },
    "CheckpointLoaderSimple": {
        "required_inputs": ("ckpt_name",),
        "optional_inputs": (),
        "outputs": 3,  # MODEL, CLIP, VAE
    },
    "LoraLoaderModelOnly": {
        "required_inputs": ("model", "lora_name", "strength_model"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "LoadImage": {
        "required_inputs": ("image",),
        "optional_inputs": (),
        "outputs": 2,  # IMAGE (slot 0), MASK alpha-derived (slot 1)
    },
    "LoadImageMask": {
        "required_inputs": ("image", "channel"),
        "optional_inputs": (),
        "outputs": 1,  # MASK
    },
    "RepeatImageBatch": {
        "required_inputs": ("image", "amount"),
        "optional_inputs": (),
        "outputs": 1,  # IMAGE (batch repetido temporalmente)
    },
    "ImageToMask": {
        "required_inputs": ("image", "channel"),
        "optional_inputs": (),
        "outputs": 1,  # MASK
    },
    "VHS_LoadVideo": {
        "required_inputs": (
            "video",
            "force_rate",
            "custom_width",
            "custom_height",
            "frame_load_cap",
            "skip_first_frames",
            "select_every_nth",
        ),
        "optional_inputs": ("meta_batch", "vae", "format"),
        "outputs": 4,  # IMAGE, INT, AUDIO, VHS_VIDEOINFO
    },
    "CLIPTextEncode": {
        "required_inputs": ("text", "clip"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "CLIPVisionEncode": {
        "required_inputs": ("clip_vision", "image", "crop"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "EmptyLatentImage": {
        "required_inputs": ("width", "height", "batch_size"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "EmptyHunyuanLatentVideo": {
        "required_inputs": ("width", "height", "length", "batch_size"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "WanVaceToVideo": {
        "required_inputs": (
            "positive",
            "negative",
            "vae",
            "width",
            "height",
            "length",
            "batch_size",
            "strength",
        ),
        "optional_inputs": ("control_video", "control_masks", "reference_image"),
        "outputs": 4,  # positive, negative, latent, trim_latent
    },
    "WanImageToVideo": {
        "required_inputs": (
            "positive",
            "negative",
            "vae",
            "width",
            "height",
            "length",
            "batch_size",
        ),
        "optional_inputs": ("clip_vision_output", "start_image", "ref_pad_image"),
        "outputs": 3,  # positive, negative, latent
    },
    "WanFirstLastFrameToVideo": {
        "required_inputs": (
            "positive",
            "negative",
            "vae",
            "width",
            "height",
            "length",
            "batch_size",
        ),
        "optional_inputs": (
            "clip_vision_start_image",
            "clip_vision_end_image",
            "start_image",
            "end_image",
        ),
        "outputs": 3,  # positive, negative, latent
    },
    "KSampler": {
        "required_inputs": (
            "seed",
            "steps",
            "cfg",
            "sampler_name",
            "scheduler",
            "denoise",
            "model",
            "positive",
            "negative",
            "latent_image",
        ),
        "optional_inputs": (),
        "outputs": 1,
    },
    "KSamplerAdvanced": {
        "required_inputs": (
            "add_noise",
            "noise_seed",
            "steps",
            "cfg",
            "sampler_name",
            "scheduler",
            "start_at_step",
            "end_at_step",
            "return_with_leftover_noise",
            "model",
            "positive",
            "negative",
            "latent_image",
        ),
        "optional_inputs": (),
        "outputs": 1,
    },
    "VAEDecode": {
        "required_inputs": ("samples", "vae"),
        "optional_inputs": (),
        "outputs": 1,
    },
    "SaveImage": {
        "required_inputs": ("images", "filename_prefix"),
        "optional_inputs": (),
        "outputs": 0,
    },
    "VHS_VideoCombine": {
        "required_inputs": (
            "images",
            "frame_rate",
            "loop_count",
            "filename_prefix",
            "format",
            "pingpong",
            "save_output",
        ),
        "optional_inputs": ("audio", "meta_batch", "vae"),
        "outputs": 1,
    },
}


class TemplateValidationError(ValueError):
    """Error de validación estructural de una plantilla ComfyUI."""


def extract_placeholders(value: Any) -> set[str]:
    """Extrae recursivamente todos los nombres {{placeholder}} presentes en un valor."""
    found: set[str] = set()
    if isinstance(value, str):
        for match in PLACEHOLDER_RE.finditer(value):
            found.add(match.group(1))
    elif isinstance(value, dict):
        for v in value.values():
            found.update(extract_placeholders(v))
    elif isinstance(value, list):
        for item in value:
            found.update(extract_placeholders(item))
    return found


def _validate_parameter_declarations(
    template_id: str,
    parameters: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(parameters, dict) or not parameters:
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: 'parameters' debe ser un objeto no vacío."
        )

    for param_name, spec in parameters.items():
        if not isinstance(param_name, str) or not param_name.strip():
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: nombre de parámetro vacío o inválido."
            )
        if not isinstance(spec, dict):
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: definición de parámetro {param_name!r} inválida."
            )
        ptype = spec.get("type")
        if ptype not in VALID_PARAM_TYPES:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: parámetro {param_name!r} tiene tipo "
                f"inválido {ptype!r} (permitidos: {sorted(VALID_PARAM_TYPES)})."
            )
        desc = spec.get("description")
        if not isinstance(desc, str) or len(desc.strip()) < 5:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: parámetro {param_name!r} "
                "requiere descripción en español."
            )
        required = bool(spec.get("required", False))
        if not required and "default" not in spec:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: parámetro opcional {param_name!r} "
                "debe declarar 'default'."
            )
        if "default" in spec and spec["default"] is not None:
            default_val = spec["default"]
            if ptype == "string" and not isinstance(default_val, str):
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: default de {param_name!r} debe ser string."
                )
            if ptype == "boolean" and not isinstance(default_val, bool):
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: default de {param_name!r} debe ser boolean."
                )
            if ptype == "integer" and (
                isinstance(default_val, bool) or not isinstance(default_val, int)
            ):
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: default de {param_name!r} debe ser integer."
                )
            if ptype == "number" and (
                isinstance(default_val, bool)
                or not isinstance(default_val, (int, float))
            ):
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: default de {param_name!r} debe ser number."
                )
            if ptype in ("integer", "number"):
                min_v = spec.get("min")
                max_v = spec.get("max")
                if min_v is not None and default_val < min_v:
                    raise TemplateValidationError(
                        f"Plantilla {template_id!r}: default de {param_name!r} "
                        f"({default_val}) < min ({min_v})."
                    )
                if max_v is not None and default_val > max_v:
                    raise TemplateValidationError(
                        f"Plantilla {template_id!r}: default de {param_name!r} "
                        f"({default_val}) > max ({max_v})."
                    )

    if "prompt" not in parameters or not parameters["prompt"].get("required"):
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: debe declarar 'prompt' como parámetro obligatorio."
        )

    return parameters


def _detect_cycle(
    workflow: dict[str, dict[str, Any]],
    edges: dict[str, set[str]],
) -> list[str] | None:
    """Detecta si existe algún ciclo en el grafo dirigido de nodos."""
    state: dict[str, int] = {nid: 0 for nid in workflow}  # 0=unvisited, 1=visiting, 2=done
    stack: list[str] = []

    def dfs(node_id: str) -> list[str] | None:
        state[node_id] = 1
        stack.append(node_id)
        for dep in edges.get(node_id, ()):
            if state.get(dep) == 1:
                idx = stack.index(dep)
                return [*stack[idx:], dep]
            if state.get(dep) == 0:
                cyc = dfs(dep)
                if cyc is not None:
                    return cyc
        stack.pop()
        state[node_id] = 2
        return None

    for nid in workflow:
        if state[nid] == 0:
            cycle = dfs(nid)
            if cycle is not None:
                return cycle
    return None


def validate_template_definition(raw_def: dict[str, Any]) -> dict[str, Any]:
    """Valida estructuralmente la definición completa de una plantilla ComfyUI."""
    if not isinstance(raw_def, dict):
        raise TemplateValidationError("La definición de la plantilla debe ser un objeto JSON.")

    template_id = str(raw_def.get("id") or "").strip()
    if not template_id:
        raise TemplateValidationError("La plantilla carece de campo 'id' válido.")

    for req_top in (
        "name",
        "category",
        "output_kind",
        "summary",
        "description",
        "performance_defaults",
        "parameters",
        "docs",
        "workflow",
    ):
        if req_top not in raw_def or raw_def[req_top] is None:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: falta el campo requerido {req_top!r}."
            )

    if raw_def["output_kind"] not in ("video", "image"):
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: 'output_kind' debe ser 'video' o 'image'."
        )

    docs = raw_def["docs"]
    if not isinstance(docs, dict):
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: 'docs' debe ser un objeto con documentación en español."
        )
    for doc_key in ("uso", "inputs", "defaults", "notas_rendimiento"):
        if not docs.get(doc_key):
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: falta sección documental {doc_key!r} en 'docs'."
            )

    parameters = _validate_parameter_declarations(template_id, raw_def["parameters"])

    workflow = raw_def["workflow"]
    if not isinstance(workflow, dict) or not workflow:
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: 'workflow' debe ser un "
            "diccionario no vacío en formato API."
        )

    present_classes: set[str] = set()
    terminal_nodes: list[tuple[str, dict[str, Any]]] = []
    # edges[node_id] = conjunto de nodos fuente de los que depende node_id
    dependencies: dict[str, set[str]] = {str(nid): set() for nid in workflow}
    consumed_nodes: set[str] = set()

    for node_id, node_data in workflow.items():
        if not isinstance(node_id, str) or not node_id.strip():
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: clave de nodo inválida {node_id!r}."
            )
        if not isinstance(node_data, dict):
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: el nodo {node_id!r} debe ser un objeto JSON."
            )

        class_type = node_data.get("class_type")
        if not isinstance(class_type, str) or not class_type.strip():
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: el nodo {node_id!r} carece de 'class_type'."
            )
        if class_type not in KNOWN_NODE_SPECS:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: el nodo {node_id!r} declara un "
                f"class_type desconocido {class_type!r}."
            )
        present_classes.add(class_type)

        inputs = node_data.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: el nodo {node_id!r} ({class_type}) "
                "tiene 'inputs' vacío o inválido."
            )

        node_spec = KNOWN_NODE_SPECS[class_type]
        for req_input in node_spec["required_inputs"]:
            if req_input not in inputs:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: al nodo {node_id!r} ({class_type}) "
                    f"le falta la entrada requerida {req_input!r}."
                )

        for input_name, input_val in inputs.items():
            if input_val is None:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: entrada desconectada o nula "
                    f"{input_name!r} en nodo {node_id!r} ({class_type})."
                )
            if isinstance(input_val, str) and not input_val.strip():
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: entrada {input_name!r} vacía "
                    f"en nodo {node_id!r} ({class_type})."
                )
            if isinstance(input_val, list):
                if (
                    len(input_val) != 2
                    or not isinstance(input_val[0], str)
                    or isinstance(input_val[1], bool)
                    or not isinstance(input_val[1], int)
                    or input_val[1] < 0
                ):
                    raise TemplateValidationError(
                        f"Plantilla {template_id!r}: enlace mal formado en "
                        f"nodo {node_id!r}.{input_name}: {input_val!r} "
                        "(se esperaba [source_node_id, slot_index])."
                    )
                src_node_id, slot_idx = input_val[0], input_val[1]
                if src_node_id == node_id:
                    raise TemplateValidationError(
                        f"Plantilla {template_id!r}: ciclo directo en nodo {node_id!r}."
                    )
                if src_node_id not in workflow:
                    raise TemplateValidationError(
                        f"Plantilla {template_id!r}: enlace roto en nodo "
                        f"{node_id!r}.{input_name} hacia nodo inexistente {src_node_id!r}."
                    )
                src_class = workflow[src_node_id].get("class_type")
                src_spec = KNOWN_NODE_SPECS.get(str(src_class or ""))
                if src_spec is not None and slot_idx >= src_spec["outputs"]:
                    raise TemplateValidationError(
                        f"Plantilla {template_id!r}: enlace en nodo {node_id!r}.{input_name} "
                        f"usa slot {slot_idx} pero el nodo fuente {src_node_id!r} ({src_class}) "
                        f"solo expone {src_spec['outputs']} salida(s)."
                    )
                dependencies[node_id].add(src_node_id)
                consumed_nodes.add(src_node_id)

        if class_type in TERMINAL_OUTPUT_CLASSES:
            terminal_nodes.append((node_id, node_data))

    # Verificar que todos los required_nodes declarados en la plantilla estén presentes
    declared_required_nodes = raw_def.get("required_nodes") or []
    for req_class in declared_required_nodes:
        if req_class not in present_classes:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: falta el tipo de nodo requerido {req_class!r}."
            )

    # Verificar al menos un nodo terminal de salida y que redirija al directorio del proyecto
    if not terminal_nodes:
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: el workflow debe incluir al menos un nodo "
            "de salida (SaveImage o VHS_VideoCombine)."
        )

    for term_id, term_node in terminal_nodes:
        prefix_val = str(term_node["inputs"].get("filename_prefix") or "")
        prefix_placeholders = extract_placeholders(prefix_val)
        if not ({"output_prefix", "project_id"} & prefix_placeholders):
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: el nodo de salida {term_id!r} "
                f"({term_node['class_type']}) debe redirigir 'filename_prefix' al "
                "directorio del proyecto mediante '{{output_prefix}}'."
            )

    # Verificar ausencia de ciclos en el grafo
    cycle = _detect_cycle(workflow, dependencies)
    if cycle is not None:
        cycle_str = " -> ".join(cycle)
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: se detectó un ciclo circular en el grafo: {cycle_str}."
        )

    # Verificar que ningún nodo declare entradas inexistentes en el contrato pinneado
    for node_id, node_data in workflow.items():
        class_type = node_data["class_type"]
        node_spec = KNOWN_NODE_SPECS[class_type]
        allowed_inputs = set(node_spec["required_inputs"]) | set(
            node_spec.get("optional_inputs", ())
        )
        inputs = node_data["inputs"]
        for input_name in inputs:
            if input_name not in allowed_inputs:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {node_id!r} ({class_type}) "
                    f"declara una entrada inexistente o no soportada {input_name!r} "
                    f"según el contrato pinneado (permitidas: {sorted(allowed_inputs)})."
                )
        if class_type == "WanVaceToVideo" and "control_masks" in inputs:
            mask_ref = inputs["control_masks"]
            if not isinstance(mask_ref, list) or len(mask_ref) != 2:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {node_id!r} (WanVaceToVideo) "
                    "debe conectar 'control_masks' mediante un enlace [node_id, slot_idx]."
                )
            mask_src_id, mask_src_slot = mask_ref[0], mask_ref[1]
            mask_src_node = workflow.get(mask_src_id, {})
            mask_src_class = mask_src_node.get("class_type")
            if mask_src_class == "LoadImage" and mask_src_slot == 1:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {node_id!r} (WanVaceToVideo) "
                    "conecta 'control_masks' al slot 1 (canal alfa) de LoadImage; "
                    "para máscaras RGB blanco/negro debe convertir el canal mediante "
                    "ImageToMask tras repetir temporalmente con RepeatImageBatch."
                )
            if mask_src_class != "ImageToMask" or mask_src_slot != 0:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {node_id!r} (WanVaceToVideo) "
                    "debe recibir 'control_masks' desde 'ImageToMask' (slot 0) precedido por "
                    "'RepeatImageBatch'; una máscara única se rellena con 1.0 (blanco/editable) "
                    "en los fotogramas posteriores."
                )
            mask_channel = mask_src_node.get("inputs", {}).get("channel")
            if mask_channel not in ("red", "green", "blue"):
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {mask_src_id!r} (ImageToMask) "
                    f"debe extraer un canal RGB ('red', 'green' o 'blue') en lugar de "
                    f"{mask_channel!r}."
                )
            img_ref = mask_src_node.get("inputs", {}).get("image")
            if not isinstance(img_ref, list) or len(img_ref) != 2:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {mask_src_id!r} (ImageToMask) "
                    "debe recibir 'image' desde un nodo 'RepeatImageBatch'."
                )
            repeat_id, repeat_slot = img_ref[0], img_ref[1]
            repeat_node = workflow.get(repeat_id, {})
            repeat_class = repeat_node.get("class_type")
            if repeat_class != "RepeatImageBatch" or repeat_slot != 0:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: la máscara hacia {node_id!r} (WanVaceToVideo) "
                    f"proviene de {repeat_class!r} sin repetición temporal 'RepeatImageBatch'; "
                    "una máscara espacial única se rellena con 1.0 (blanco/editable) en el "
                    "resto de frames."
                )
            vace_length = inputs.get("length")
            repeat_amount = repeat_node.get("inputs", {}).get("amount")
            if repeat_amount != vace_length:
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {repeat_id!r} (RepeatImageBatch) "
                    f"tiene amount={repeat_amount!r} distinto de length={vace_length!r} en "
                    f"WanVaceToVideo ({node_id!r}); deben coincidir para cubrir todos los frames "
                    "sin padding blanco."
                )
            repeat_img_ref = repeat_node.get("inputs", {}).get("image")
            if (
                not isinstance(repeat_img_ref, list)
                or len(repeat_img_ref) != 2
                or workflow.get(repeat_img_ref[0], {}).get("class_type") != "LoadImage"
                or repeat_img_ref[1] != 0
            ):
                raise TemplateValidationError(
                    f"Plantilla {template_id!r}: el nodo {repeat_id!r} (RepeatImageBatch) "
                    "debe recibir 'image' desde el slot 0 (IMAGE) de 'LoadImage'."
                )

    # Verificar que ningún nodo no-terminal esté desconectado del resto del grafo
    for node_id, node_data in workflow.items():
        if node_data["class_type"] not in TERMINAL_OUTPUT_CLASSES and node_id not in consumed_nodes:
            raise TemplateValidationError(
                f"Plantilla {template_id!r}: el nodo {node_id!r} "
                f"({node_data['class_type']}) está desconectado (su salida no se usa)."
            )

    # Verificar correspondencia entre placeholders {{param}} en el workflow y parameters declarados
    workflow_placeholders = extract_placeholders(workflow)
    allowed_placeholders = set(parameters.keys()) | BUILTIN_CONTEXT_PLACEHOLDERS
    undeclared = workflow_placeholders - allowed_placeholders
    if undeclared:
        raise TemplateValidationError(
            f"Plantilla {template_id!r}: el workflow usa placeholder(s) no declarados "
            f"en 'parameters': {sorted(undeclared)}."
        )

    return raw_def


def validate_template_file(path: Path | str) -> dict[str, Any]:
    """Lee y valida un archivo JSON de plantilla."""
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
        raw_def = json.loads(raw_text)
    except Exception as exc:
        raise TemplateValidationError(
            f"No se pudo parsear el archivo de plantilla {file_path.name}: {exc}"
        ) from exc

    if isinstance(raw_def, dict) and raw_def.get("id") and file_path.stem != raw_def.get("id"):
        raise TemplateValidationError(
            f"El archivo {file_path.name!r} no coincide con el id interno {raw_def.get('id')!r}."
        )
    return validate_template_definition(raw_def)


def validate_templates_dir(data_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Valida todos los archivos *.json en un directorio de plantillas."""
    folder = Path(data_dir)
    if not folder.is_dir():
        raise TemplateValidationError(f"Directorio de plantillas no encontrado: {folder}")

    json_files = sorted(folder.glob("*.json"))
    if not json_files:
        raise TemplateValidationError(
            f"No se encontraron archivos JSON de plantillas en {folder}"
        )

    validated: dict[str, dict[str, Any]] = {}
    for jf in json_files:
        tpl_def = validate_template_file(jf)
        tpl_id = tpl_def["id"]
        if tpl_id in validated:
            raise TemplateValidationError(f"ID de plantilla duplicado: {tpl_id!r}")
        validated[tpl_id] = tpl_def
    return validated


def validate_all_templates(
    data_dir: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Valida todas las plantillas horneadas en studio/comfy_studio/templates/data/."""
    target_dir = (
        Path(data_dir)
        if data_dir is not None
        else Path(__file__).resolve().parent / "data"
    )
    return validate_templates_dir(target_dir)
