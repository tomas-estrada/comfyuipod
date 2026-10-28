"""Tests para VAL-SERVER-006 y VAL-CROSS-002: Registro de plantillas, validador y submit."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.stubs.comfyui import ComfyUIStubState
from comfy_studio.templates import (
    EXPECTED_M2_TEMPLATE_IDS,
    TemplateValidationError,
    get_default_registry,
    render_template_workflow,
    validate_all_templates,
    validate_template_definition,
    validate_templates_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_STUDIO_MANIFEST = REPO_ROOT / "config" / "models-studio.json"


def test_list_templates_endpoint_returns_six_m2_templates_with_schemas_and_defaults(
    client: TestClient,
) -> None:
    """GET /studio/api/templates lista las 6 plantillas M2 con schemas y docs en español."""
    resp = client.get("/studio/api/templates")
    assert resp.status_code == 200
    body = resp.json()
    assert "templates" in body
    templates = body["templates"]
    assert len(templates) == 7

    by_id = {tpl["id"]: tpl for tpl in templates}
    assert set(by_id.keys()) == set(EXPECTED_M2_TEMPLATE_IDS)

    # 1. Verificar defaults de rendimiento VACE: fp8 + 480p (832x480) + offload
    for vace_id in ("vace_inpaint", "vace_restyle"):
        tpl = by_id[vace_id]
        perf = tpl["performance_defaults"]
        assert perf["resolution"] == "480p"
        assert perf["width"] == 832
        assert perf["height"] == 480
        assert perf["offload"] is True
        assert "fp8" in perf["precision"]
        assert tpl["output_kind"] == "video"

    # 2. Verificar defaults de rendimiento Wan 14B: LoRA Lightning 4-step + 480p + offload
    for wan_id in ("wan_flf2v", "wan_i2v", "wan_t2v"):
        tpl = by_id[wan_id]
        perf = tpl["performance_defaults"]
        assert perf["steps"] == 4
        assert perf["lora_lightning_4step"] is True
        assert perf["width"] == 832
        assert perf["height"] == 480
        assert perf["offload"] is True
        assert tpl["output_kind"] == "video"

    # 3. Verificar defaults de SDXL image
    sdxl = by_id["sdxl_image"]
    assert sdxl["output_kind"] == "image"
    assert sdxl["performance_defaults"]["width"] == 1024
    assert sdxl["performance_defaults"]["height"] == 1024
    assert sdxl["performance_defaults"]["steps"] == 25
    assert "Juggernaut-XI-byRunDiffusion.safetensors" in sdxl["models"]

    # 4. Verificar schema de parámetros y documentación en español en cada plantilla
    for tpl_id, tpl in by_id.items():
        schema = tpl["params_schema"]
        assert schema["type"] == "object"
        if tpl_id == "hunyuan3d_image_to_3d":
            # M3-D: imagen->3D no usa prompt textual; su entrada obligatoria es la imagen
            assert "image_asset_id" in schema["properties"]
            assert "image_asset_id" in schema["required"]
        else:
            assert "prompt" in schema["properties"]
            assert "prompt" in schema["required"]
        assert schema.get("additionalProperties") is False

        docs = tpl["docs"]
        assert isinstance(docs.get("uso"), str) and len(docs["uso"]) >= 25
        assert isinstance(docs.get("inputs"), list) and len(docs["inputs"]) >= 1
        assert isinstance(docs.get("defaults"), dict) and len(docs["defaults"]) >= 1
        notas_rend = docs.get("notas_rendimiento")
        assert isinstance(notas_rend, str) and len(notas_rend) >= 20

        # Detalle individual GET /studio/api/templates/{id}
        detail_resp = client.get(f"/studio/api/templates/{tpl_id}")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["id"] == tpl_id
        assert isinstance(detail["workflow"], dict) and len(detail["workflow"]) >= 4

    # 5. Plantilla inexistente devuelve 404
    missing_resp = client.get("/studio/api/templates/plantilla_inexistente")
    assert missing_resp.status_code == 404


def test_structural_validator_passes_all_builtin_templates() -> None:
    """Todas las plantillas en comfy_studio/templates/data/ pasan el validador estructural."""
    registry = get_default_registry()
    results = validate_all_templates(registry.data_dir)
    assert len(results) == 7
    assert set(results.keys()) == set(EXPECTED_M2_TEMPLATE_IDS)

    # Verificar que todos los modelos referenciados existen en el catálogo del pod
    manifest_data = json.loads(MODELS_STUDIO_MANIFEST.read_text(encoding="utf-8"))
    manifest_filenames = {item["filename"] for item in manifest_data["models"]}
    # Juggernaut-XI se descarga en Fase 2 de download-models.sh
    known_pod_models = manifest_filenames | {"Juggernaut-XI-byRunDiffusion.safetensors"}

    for tpl_id in EXPECTED_M2_TEMPLATE_IDS:
        raw_def = registry.get_raw_definition(tpl_id)
        validate_template_definition(raw_def)
        for model_name in raw_def.get("models", []):
            assert model_name in known_pod_models, (
                f"Modelo {model_name!r} de plantilla {tpl_id!r} no está en el catálogo del pod"
            )


def test_structural_validator_rejects_corrupted_templates(tmp_path: Path) -> None:
    """El validador estructural falla ante corrupción de grafo, nodos, enlaces o params."""
    registry = get_default_registry()
    base_def = copy.deepcopy(registry.get_raw_definition("wan_t2v"))

    # 1. Nodo con class_type inexistente / desconocido
    corrupt_class = copy.deepcopy(base_def)
    first_node_id = next(iter(corrupt_class["workflow"].keys()))
    corrupt_class["workflow"][first_node_id]["class_type"] = "NodoComfyInexistenteXYZ"
    with pytest.raises(TemplateValidationError, match="class_type|nodo|desconocido|requerido"):
        validate_template_definition(corrupt_class)

    # 2. Enlace roto que apunta a un nodo inexistente ["999", 0]
    corrupt_link = copy.deepcopy(base_def)
    for node in corrupt_link["workflow"].values():
        if node["class_type"] == "VAEDecode":
            node["inputs"]["samples"] = ["999", 0]
    with pytest.raises(TemplateValidationError, match="999|inexistente|enlace"):
        validate_template_definition(corrupt_link)

    # 3. Entrada desconectada (valor None o lista mal formada)
    corrupt_disconnected = copy.deepcopy(base_def)
    for node in corrupt_disconnected["workflow"].values():
        if node["class_type"] == "CLIPTextEncode":
            node["inputs"]["clip"] = None
            break
    with pytest.raises(TemplateValidationError, match="desconectad|nulo|clip"):
        validate_template_definition(corrupt_disconnected)

    # 4. Placeholder usado en el workflow pero no declarado en parameters
    corrupt_placeholder = copy.deepcopy(base_def)
    for node in corrupt_placeholder["workflow"].values():
        if node["class_type"] == "CLIPTextEncode":
            node["inputs"]["text"] = "{{parametro_no_declarado}}"
            break
    with pytest.raises(TemplateValidationError, match="parametro_no_declarado|placeholder"):
        validate_template_definition(corrupt_placeholder)

    # 5. Sin nodo de salida (SaveImage / VHS_VideoCombine eliminado)
    corrupt_no_output = copy.deepcopy(base_def)
    corrupt_no_output["workflow"] = {
        nid: n
        for nid, n in corrupt_no_output["workflow"].items()
        if n["class_type"] not in ("SaveImage", "VHS_VideoCombine")
    }
    with pytest.raises(
        TemplateValidationError,
        match="salida|SaveImage|VHS_VideoCombine|requerido",
    ):
        validate_template_definition(corrupt_no_output)

    # 6. Salida sin redirección al proyecto (filename_prefix sin {{output_prefix}})
    corrupt_prefix = copy.deepcopy(base_def)
    for node in corrupt_prefix["workflow"].values():
        if node["class_type"] == "VHS_VideoCombine":
            node["inputs"]["filename_prefix"] = "salida_suelta_sin_proyecto"
    with pytest.raises(TemplateValidationError, match="filename_prefix|output_prefix|proyecto"):
        validate_template_definition(corrupt_prefix)

    # 7. Ciclo en el grafo de nodos
    corrupt_cycle = copy.deepcopy(base_def)
    corrupt_cycle["workflow"]["1"]["inputs"]["cycle_ref"] = ["2", 0]
    corrupt_cycle["workflow"]["2"]["inputs"]["cycle_back"] = ["1", 0]
    with pytest.raises(TemplateValidationError, match="ciclo|circular"):
        validate_template_definition(corrupt_cycle)

    # 8. Directorio con un archivo de plantilla corrupto falla validate_templates_dir
    bad_file = tmp_path / "corrupt_template.json"
    bad_file.write_text(json.dumps(corrupt_link), encoding="utf-8")
    with pytest.raises(TemplateValidationError):
        validate_templates_dir(tmp_path)

    # 9. (M2-SCR-008) VHS_VideoCombine sin el input requerido 'pingpong' falla
    corrupt_no_pingpong = copy.deepcopy(base_def)
    for node in corrupt_no_pingpong["workflow"].values():
        if node["class_type"] == "VHS_VideoCombine":
            node["inputs"].pop("pingpong", None)
    with pytest.raises(TemplateValidationError, match="pingpong"):
        validate_template_definition(corrupt_no_pingpong)

    # 10. (M2-SCR-009 / M2-SCR-010) Input inexistente / no soportado en el nodo falla
    corrupt_unknown_input = copy.deepcopy(base_def)
    for node in corrupt_unknown_input["workflow"].values():
        if node["class_type"] == "VHS_VideoCombine":
            node["inputs"]["input_inventado_no_pinneado"] = True
    with pytest.raises(
        TemplateValidationError,
        match="input_inventado_no_pinneado|inexistente|no soportad",
    ):
        validate_template_definition(corrupt_unknown_input)

    # 11. (M2-SCR-009) WanVaceToVideo con 'control_mask' (singular) o máscara alfa falla
    vace_inpaint_def = copy.deepcopy(registry.get_raw_definition("vace_inpaint"))
    corrupt_vace_singular = copy.deepcopy(vace_inpaint_def)
    for node in corrupt_vace_singular["workflow"].values():
        if node["class_type"] == "WanVaceToVideo":
            mask_ref = node["inputs"].pop("control_masks", ["6", 0])
            node["inputs"]["control_mask"] = mask_ref
    with pytest.raises(
        TemplateValidationError,
        match="control_mask|inexistente|no soportad",
    ):
        validate_template_definition(corrupt_vace_singular)

    corrupt_vace_alpha = copy.deepcopy(vace_inpaint_def)
    load_img_id = next(
        nid
        for nid, n in corrupt_vace_alpha["workflow"].items()
        if n["class_type"] == "LoadImage"
    )
    # Eliminar nodos ImageToMask y RepeatImageBatch para no dejarlos huérfanos
    # y conectar slot 1 de LoadImage directo
    corrupt_vace_alpha["workflow"] = {
        nid: n
        for nid, n in corrupt_vace_alpha["workflow"].items()
        if n["class_type"] not in ("ImageToMask", "RepeatImageBatch")
    }
    corrupt_vace_alpha["required_nodes"] = [
        rc
        for rc in corrupt_vace_alpha.get("required_nodes", [])
        if rc not in ("ImageToMask", "RepeatImageBatch")
    ]
    for node in corrupt_vace_alpha["workflow"].values():
        if node["class_type"] == "WanVaceToVideo":
            node["inputs"]["control_masks"] = [load_img_id, 1]
    with pytest.raises(
        TemplateValidationError,
        match="alpha|alfa|ImageToMask|LoadImageMask|control_masks",
    ):
        validate_template_definition(corrupt_vace_alpha)

    # 12. (M2-SCR-010) WanVaceToVideo con 'clip_vision_output' falla por contrato pinneado
    vace_restyle_def = copy.deepcopy(registry.get_raw_definition("vace_restyle"))
    corrupt_vace_clip = copy.deepcopy(vace_restyle_def)
    for node in corrupt_vace_clip["workflow"].values():
        if node["class_type"] == "WanVaceToVideo":
            node["inputs"]["clip_vision_output"] = ["7", 0]
    with pytest.raises(
        TemplateValidationError,
        match="clip_vision_output|inexistente|no soportad",
    ):
        validate_template_definition(corrupt_vace_clip)

    # 13. (M2-SCR-018) Máscara única LoadImage -> ImageToMask -> WanVaceToVideo sin
    # RepeatImageBatch falla validación estructural (evita padding blanco 1.0 en frames 1..N-1)
    corrupt_vace_single_mask = copy.deepcopy(vace_inpaint_def)
    corrupt_vace_single_mask["workflow"] = {
        nid: n
        for nid, n in corrupt_vace_single_mask["workflow"].items()
        if n["class_type"] != "RepeatImageBatch"
    }
    corrupt_vace_single_mask["required_nodes"] = [
        rc for rc in corrupt_vace_single_mask.get("required_nodes", []) if rc != "RepeatImageBatch"
    ]
    for node in corrupt_vace_single_mask["workflow"].values():
        if node["class_type"] == "ImageToMask":
            node["inputs"]["image"] = [load_img_id, 0]
    with pytest.raises(
        TemplateValidationError,
        match="RepeatImageBatch|repetición temporal|1\\.0|blanco",
    ):
        validate_template_definition(corrupt_vace_single_mask)

    # 14. (M2-SCR-018) RepeatImageBatch con amount distinto de WanVaceToVideo.length falla
    corrupt_vace_bad_amount = copy.deepcopy(vace_inpaint_def)
    for node in corrupt_vace_bad_amount["workflow"].values():
        if node["class_type"] == "RepeatImageBatch":
            node["inputs"]["amount"] = 1
    with pytest.raises(
        TemplateValidationError,
        match="RepeatImageBatch|amount|length",
    ):
        validate_template_definition(corrupt_vace_bad_amount)


def test_video_templates_adhere_to_pinned_node_contracts() -> None:
    """Regresión M2-SCR-008, M2-SCR-009 y M2-SCR-010: contratos pinneados de VHS y VACE."""
    registry = get_default_registry()
    video_template_ids = (
        "wan_t2v",
        "wan_i2v",
        "wan_flf2v",
        "vace_inpaint",
        "vace_restyle",
    )

    # M2-SCR-008: Las 5 plantillas de vídeo deben declarar pingpong (bool) en VHS_VideoCombine
    # y KSamplerAdvanced no debe pasar 'offload' como input del nodo.
    for tpl_id in video_template_ids:
        raw_def = registry.get_raw_definition(tpl_id)
        workflow = raw_def["workflow"]
        vhs_combine_nodes = [
            n for n in workflow.values() if n["class_type"] == "VHS_VideoCombine"
        ]
        assert len(vhs_combine_nodes) == 1, f"{tpl_id} debe tener un nodo VHS_VideoCombine"
        vhs_inputs = vhs_combine_nodes[0]["inputs"]
        for req_key in (
            "images",
            "frame_rate",
            "loop_count",
            "filename_prefix",
            "format",
            "pingpong",
            "save_output",
        ):
            assert req_key in vhs_inputs, (
                f"{tpl_id}: VHS_VideoCombine omite input requerido {req_key!r}"
            )
        assert isinstance(vhs_inputs["pingpong"], bool)

        for node in workflow.values():
            if node["class_type"] == "KSamplerAdvanced":
                assert "offload" not in node["inputs"], (
                    f"{tpl_id}: KSamplerAdvanced no acepta 'offload' en ComfyUI pinneado"
                )

    # M2-SCR-009 + M2-SCR-018: vace_inpaint repite la máscara espacial a {{frames}} con
    # RepeatImageBatch antes de extraer el canal RGB con ImageToMask hacia control_masks
    inpaint_def = registry.get_raw_definition("vace_inpaint")
    assert "RepeatImageBatch" in inpaint_def.get("required_nodes", [])
    inpaint_wf = inpaint_def["workflow"]
    vace_inpaint_nodes = [
        n for n in inpaint_wf.values() if n["class_type"] == "WanVaceToVideo"
    ]
    assert len(vace_inpaint_nodes) == 1
    inpaint_inputs = vace_inpaint_nodes[0]["inputs"]
    assert "control_mask" not in inpaint_inputs
    assert "control_masks" in inpaint_inputs
    mask_link = inpaint_inputs["control_masks"]
    assert isinstance(mask_link, list) and len(mask_link) == 2
    mask_src_node = inpaint_wf[mask_link[0]]
    assert mask_src_node["class_type"] == "ImageToMask"
    assert mask_link[1] == 0
    assert mask_src_node["inputs"]["channel"] in ("red", "green", "blue")
    repeat_link = mask_src_node["inputs"]["image"]
    assert isinstance(repeat_link, list) and repeat_link[1] == 0
    repeat_node = inpaint_wf[repeat_link[0]]
    assert repeat_node["class_type"] == "RepeatImageBatch"
    assert repeat_node["inputs"]["amount"] == inpaint_inputs["length"] == "{{frames}}"
    img_link = repeat_node["inputs"]["image"]
    assert isinstance(img_link, list) and img_link[1] == 0
    assert inpaint_wf[img_link[0]]["class_type"] == "LoadImage"

    # M2-SCR-010: vace_restyle usa reference_image y NO pasa clip_vision_output a WanVaceToVideo
    restyle_def = registry.get_raw_definition("vace_restyle")
    restyle_wf = restyle_def["workflow"]
    vace_restyle_nodes = [
        n for n in restyle_wf.values() if n["class_type"] == "WanVaceToVideo"
    ]
    assert len(vace_restyle_nodes) == 1
    restyle_inputs = vace_restyle_nodes[0]["inputs"]
    assert "clip_vision_output" not in restyle_inputs
    assert "reference_image" in restyle_inputs
    ref_link = restyle_inputs["reference_image"]
    assert isinstance(ref_link, list) and ref_link[1] == 0
    assert restyle_wf[ref_link[0]]["class_type"] == "LoadImage"
    restyle_classes = {n["class_type"] for n in restyle_wf.values()}
    assert "CLIPVisionEncode" not in restyle_classes
    assert "CLIPVisionLoader" not in restyle_classes
    assert "clip_vision_h.safetensors" not in restyle_def.get("models", [])


def test_submit_template_valid_params_enqueues_job_and_registers_gallery_outputs(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
    studio_data_dir: Path,
) -> None:
    """Submit con params válidos encola job en stub ComfyUI y registra salidas en galería."""
    # Crear proyecto y subir vídeo + máscara para probar también vace_inpaint con assets reales
    proj_resp = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Plantillas M2", "aspect_ratio": "16:9"},
    )
    assert proj_resp.status_code == 201
    project_id = proj_resp.json()["id"]

    vid_bytes = (
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        b"\x00\x00\x00\x08free\x00\x00\x00\x20mdatFAKEVIDEOFRAME"
    )
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    vid_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("toma_original.mp4", vid_bytes, "video/mp4")},
    ).json()
    mask_asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("mascara_sujeto.png", png_bytes, "image/png")},
    ).json()

    # 1. Submit de vace_inpaint con assets del proyecto y defaults 480p+offload
    submit_resp = client.post(
        "/studio/api/templates/vace_inpaint/submit",
        json={
            "project_id": project_id,
            "params": {
                "prompt": "Reemplazar fondo por estudio futurista iluminado con neones",
                "video_asset_id": vid_asset["id"],
                "mask_asset_id": mask_asset["id"],
            },
        },
    )
    assert submit_resp.status_code in (200, 201), submit_resp.text
    sub_body = submit_resp.json()
    assert sub_body["template_id"] == "vace_inpaint"
    assert sub_body["project_id"] == project_id
    assert sub_body["status"] == "done"
    assert sub_body["prompt_id"].startswith("stub-")
    assert sub_body["params"]["width"] == 832
    assert sub_body["params"]["height"] == 480
    assert sub_body["params"]["offload"] is True

    # Verificar que el prompt se envió a ComfyUI con los valores tipados y sin placeholders {{...}}
    assert len(stub_comfyui_state.prompts) == 1
    submitted_graph = stub_comfyui_state.prompts[0]["prompt"]
    serialized_graph = json.dumps(submitted_graph)
    assert "{{" not in serialized_graph
    assert project_id in serialized_graph

    # Verificar que las salidas quedaron registradas en la galería del proyecto
    assert len(sub_body["gallery_assets"]) >= 1
    gallery_resp = client.get(f"/studio/api/projects/{project_id}/gallery")
    assert gallery_resp.status_code == 200
    gallery_items = gallery_resp.json()["assets"]
    assert len(gallery_items) >= 1

    first_out = gallery_items[0]
    assert first_out["category"] == "outputs"
    assert first_out["job_id"] == sub_body["job_id"]
    assert first_out["metadata"].get("template_id") == "vace_inpaint"
    assert first_out["preview_url"]
    assert first_out["download_url"]
    assert first_out["view_url"]

    # Verificar preview vía /studio/api/view
    view_resp = client.get(first_out["view_url"])
    assert view_resp.status_code == 200
    assert len(view_resp.content) > 0
    assert "inline" in view_resp.headers.get("content-disposition", "")

    # Verificar descarga con Content-Disposition: attachment
    dl_resp = client.get(first_out["download_url"])
    assert dl_resp.status_code == 200
    assert dl_resp.content == view_resp.content
    assert "attachment" in dl_resp.headers.get("content-disposition", "")

    # Verificar que el archivo existe físicamente en STUDIO_DATA_DIR/projects/<id>/outputs/
    out_disk_path = studio_data_dir / first_out["rel_path"]
    assert out_disk_path.is_file()
    assert "outputs" in first_out["rel_path"]


def test_submit_template_invalid_params_returns_4xx_without_enqueuing(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    """Params inválidos devuelven 4xx con detalle y NUNCA envían un prompt a ComfyUI."""
    proj = client.post("/studio/api/projects", json={"name": "Proyecto Invalid Params"}).json()
    project_id = proj["id"]

    # 1. Prompt vacío / en blanco
    r_blank = client.post(
        "/studio/api/templates/wan_t2v/submit",
        json={"project_id": project_id, "params": {"prompt": "   "}},
    )
    assert 400 <= r_blank.status_code < 500
    assert "prompt" in r_blank.text.lower()
    assert len(stub_comfyui_state.prompts) == 0

    # 2. Dimensiones o pasos fuera de rango permitido
    r_range = client.post(
        "/studio/api/templates/wan_t2v/submit",
        json={
            "project_id": project_id,
            "params": {
                "prompt": "Plano válido",
                "width": 64,
                "steps": 0,
            },
        },
    )
    assert 400 <= r_range.status_code < 500
    assert len(stub_comfyui_state.prompts) == 0

    # 3. Parámetro desconocido no declarado en el schema de la plantilla
    r_extra = client.post(
        "/studio/api/templates/sdxl_image/submit",
        json={
            "project_id": project_id,
            "params": {
                "prompt": "Retrato en estudio",
                "parametro_inventado": 123,
            },
        },
    )
    assert 400 <= r_extra.status_code < 500
    assert "parametro_inventado" in r_extra.text.lower()
    assert len(stub_comfyui_state.prompts) == 0

    # 4. Asset referenciado inexistente en el proyecto
    r_missing_asset = client.post(
        "/studio/api/templates/vace_inpaint/submit",
        json={
            "project_id": project_id,
            "params": {
                "prompt": "Inpaint de fondo",
                "video_asset_id": "asset_que_no_existe",
            },
        },
    )
    assert 400 <= r_missing_asset.status_code < 500
    assert "asset_que_no_existe" in r_missing_asset.text.lower()
    assert len(stub_comfyui_state.prompts) == 0

    # 5. Plantilla desconocida
    r_unknown_tpl = client.post(
        "/studio/api/templates/plantilla_falsa/submit",
        json={"project_id": project_id, "params": {"prompt": "Hola"}},
    )
    assert r_unknown_tpl.status_code == 404
    assert len(stub_comfyui_state.prompts) == 0


def test_render_template_workflow_helper_for_all_six_templates() -> None:
    """render_template_workflow renderiza las 7 plantillas con defaults sin dejar placeholders."""
    for tpl_id in EXPECTED_M2_TEMPLATE_IDS:
        params: dict = {"prompt": f"Prueba de render para {tpl_id}"}
        if tpl_id == "hunyuan3d_image_to_3d":
            # M3-D: la entrada obligatoria es la imagen de referencia, no un prompt
            params = {"image_asset_id": "asset_img_01"}
        graph = render_template_workflow(
            tpl_id,
            params,
            project_id="proj_test_01",
        )
        assert isinstance(graph, dict) and len(graph) >= 4
        raw = json.dumps(graph)
        assert "{{" not in raw
        assert "proj_test_01" in raw


def test_spanish_docs_file_exists_and_documents_all_six_templates() -> None:
    """El documento docs/plantillas.md documenta uso, inputs y defaults de las 6 plantillas."""
    docs_path = REPO_ROOT / "docs" / "plantillas.md"
    assert docs_path.is_file()
    content = docs_path.read_text(encoding="utf-8")
    for tpl_id in EXPECTED_M2_TEMPLATE_IDS:
        assert tpl_id in content
    assert "480p" in content
    assert "Lightning" in content
    assert "offload" in content.lower()


def test_submit_template_history_succeeds_view_fails_marks_job_error_and_emits_sse_error(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    """Regresión M2-SCR-002 en registry/submit: si /history completa pero GET /view falla,
    el job queda en status=error (nunca done), SSE emite error y galería queda vacía.
    """
    proj = client.post("/studio/api/projects", json={"name": "Registry View Fail"}).json()
    project_id = proj["id"]

    stub_comfyui_state.fail_view_status = 404
    stub_comfyui_state.fail_view_error = "Output video missing on ComfyUI /view"

    sub_resp = client.post(
        "/studio/api/templates/wan_t2v/submit",
        json={
            "project_id": project_id,
            "params": {
                "prompt": "Faro en tormenta nocturna",
                "width": 832,
                "height": 480,
                "frames": 81,
                "steps": 4,
            },
        },
    )
    assert sub_resp.status_code == 404
    assert "missing" in sub_resp.text.lower() or "404" in sub_resp.text.lower()

    # El job creado en el proyecto debe estar en status=error y jamás en done
    jobs_resp = client.get(f"/studio/api/jobs?project_id={project_id}")
    assert jobs_resp.status_code == 200
    jobs = jobs_resp.json()["jobs"]
    assert len(jobs) == 1
    failed_job = jobs[0]
    assert failed_job["status"] == "error"
    assert failed_job["error"]
    assert failed_job["prompt_id"] is not None

    # El stream SSE debe reflejar status=error para ese job y ningún evento status=done
    sse_resp = client.get("/studio/api/progress?replay=1&max_events=6")
    assert sse_resp.status_code == 200
    broker_events = client.app.state.broker.recent_events()  # type: ignore[union-attr]
    job_events_for_id = [
        payload
        for ev_type, payload in broker_events
        if ev_type in ("job", "progress") and payload.get("job_id") == failed_job["id"]
    ]
    assert any(p.get("status") == "error" for p in job_events_for_id)
    assert not any(p.get("status") == "done" for p in job_events_for_id)

    # Verificar que la galería del proyecto no debe tener salidas huérfanas ni silenciosas
    gallery_resp = client.get(f"/studio/api/projects/{project_id}/gallery")
    assert gallery_resp.status_code == 200
    assert gallery_resp.json()["assets"] == []


def test_vace_inpaint_temporal_mask_broadcast_data_path() -> None:
    """Regresión M2-SCR-018: el workflow renderizado de vace_inpaint propaga la máscara espacial
    a los N fotogramas solicitados mediante RepeatImageBatch -> ImageToMask -> WanVaceToVideo,
    preservando las regiones negras protegidas (0.0) en todos los frames sin caer en el padding
    blanco (1.0) de WanVaceToVideo.
    """
    for target_frames in (81, 49):
        graph = render_template_workflow(
            "vace_inpaint",
            {
                "prompt": "Reemplazar cartel luminoso por ventana gótica",
                "frames": target_frames,
                "mask_input": "mascara_ventana.png",
            },
            project_id="proj_vace_temporal",
        )

        by_class = {node["class_type"]: (nid, node) for nid, node in graph.items()}
        assert "LoadImage" in by_class
        assert "RepeatImageBatch" in by_class
        assert "ImageToMask" in by_class
        assert "WanVaceToVideo" in by_class

        load_id, load_node = by_class["LoadImage"]
        repeat_id, repeat_node = by_class["RepeatImageBatch"]
        mask_id, mask_node = by_class["ImageToMask"]
        _, vace_node = by_class["WanVaceToVideo"]

        assert load_node["inputs"]["image"] == "mascara_ventana.png"
        assert repeat_node["inputs"]["image"] == [load_id, 0]
        assert repeat_node["inputs"]["amount"] == target_frames
        assert mask_node["inputs"]["image"] == [repeat_id, 0]
        assert mask_node["inputs"]["channel"] == "red"
        assert vace_node["inputs"]["control_masks"] == [mask_id, 0]
        assert vace_node["inputs"]["length"] == target_frames

        # Simular la semántica exacta de los nodos pinneados en ComfyUI bdc63e7:
        # 1) LoadImage entrega 1 fotograma RGB (B=1, H=2, W=2, C=3) con mitad izquierda negra
        #    protegida (0.0) y mitad derecha blanca editable (1.0).
        single_rgb_image = [
            [
                [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
            ]
        ]
        # 2) RepeatImageBatch.execute: image.repeat((amount, 1, 1, 1))
        amount = int(repeat_node["inputs"]["amount"])
        repeated_images = [copy.deepcopy(single_rgb_image[0]) for _ in range(amount)]
        assert len(repeated_images) == target_frames

        # 3) ImageToMask.execute: image[:, :, :, channels.index(channel)] -> (B, H, W)
        channels = ["red", "green", "blue", "alpha"]
        ch_idx = channels.index(mask_node["inputs"]["channel"])
        control_masks = [
            [[pixel[ch_idx] for pixel in row] for row in frame]
            for frame in repeated_images
        ]
        assert len(control_masks) == target_frames

        # 4) WanVaceToVideo.execute: si control_masks.shape[0] < length, rellena con 1.0
        vace_length = int(vace_node["inputs"]["length"])
        vace_masks = list(control_masks[:vace_length])
        if len(vace_masks) < vace_length:
            white_frame = [[1.0, 1.0], [1.0, 1.0]]
            vace_masks.extend(
                copy.deepcopy(white_frame) for _ in range(vace_length - len(vace_masks))
            )

        assert len(vace_masks) == target_frames
        # Todos los fotogramas 0..target_frames-1 conservan 0.0 en la región protegida
        for frame_idx, mask_frame in enumerate(vace_masks):
            assert mask_frame == [[0.0, 1.0], [0.0, 1.0]], (
                f"Frame {frame_idx} perdió la máscara espacial protegida: {mask_frame}"
            )


