#!/usr/bin/env python3
"""Sonda de ONNX Runtime (InferenceSession real con modelo Identity) invocada por `comfyctl diag` (§4.1)."""

import os


def main() -> None:
    try:
        import onnxruntime as ort

        avail = ort.get_available_providers()
        model_bytes = None
        model_source = "onnx.helper Identity"
        try:
            from onnx import TensorProto, helper

            x_info = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 4])
            y_info = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 4])
            node = helper.make_node("Identity", ["X"], ["Y"])
            graph = helper.make_graph([node], "diag_identity", [x_info], [y_info])
            model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
            model.ir_version = 7
            model_bytes = model.SerializeToString()
        except Exception:
            det_path = os.path.join(
                os.environ.get("COMFY_DATA_DIR", "/workspace/comfyui-data"),
                "models/insightface/models/buffalo_l/det_10g.onnx",
            )
            if os.path.isfile(det_path):
                model_bytes = det_path
                model_source = "det_10g.onnx"

        if model_bytes is None:
            print(
                f"WARN|onnx.helper no disponible y det_10g.onnx aún no descargado; providers declarados={avail}"
            )
        else:
            sess = ort.InferenceSession(
                model_bytes, providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
            active = sess.get_providers()
            if active and active[0] == "CUDAExecutionProvider":
                print(
                    f"OK|InferenceSession ({model_source}) activa con CUDAExecutionProvider; ORT providers activos: {active}"
                )
            else:
                print(
                    f"WARN|InferenceSession ({model_source}) sin CUDA activa; ORT providers activos: {active}"
                )
    except Exception as exc:
        print(f"FAIL|Error en prueba de ONNX Runtime: {exc}")


if __name__ == "__main__":
    main()
