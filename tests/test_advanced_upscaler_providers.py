"""Check the shipped GPU provider policy without optional ONNX Runtime wheels."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "scm_workbench/builtin_processors/advanced_upscaler.py"


def _namespace(platform, available, *, fail_gpu=False):
    source = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_providers", "_model"}]
    attempts = []

    class Options:
        def __init__(self):
            self.enable_mem_pattern = True
            self.execution_mode = "default"

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="input")]

        def get_outputs(self):
            return [SimpleNamespace(name="output")]

    def inference_session(path, *, sess_options, providers):
        attempts.append((path, tuple(providers), sess_options.enable_mem_pattern,
                         sess_options.execution_mode))
        if fail_gpu and len(providers) > 1:
            raise RuntimeError("GPU driver is unavailable")
        return Session()

    ort = SimpleNamespace(SessionOptions=Options, InferenceSession=inference_session,
                          get_available_providers=lambda: available,
                          ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"))
    namespace = {"_session": None, "_input_name": None, "ort": ort, "os": os,
                 "sys": SimpleNamespace(platform=platform)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace, attempts


class AdvancedUpscalerProviderTests(unittest.TestCase):
    def test_each_platform_prefers_its_available_gpu_then_cpu(self):
        for platform, gpu in (("darwin", "CoreMLExecutionProvider"),
                              ("win32", "DmlExecutionProvider"),
                              ("linux", "CUDAExecutionProvider")):
            with self.subTest(platform=platform):
                namespace, attempts = _namespace(platform, [gpu, "CPUExecutionProvider"])
                self.assertEqual(namespace["_model"]("fixed-model.onnx").get_inputs()[0].name,
                                 "input")
                self.assertEqual(attempts[0][1], (gpu, "CPUExecutionProvider"))
                self.assertEqual(namespace["_input_name"], "input")
                namespace["_model"]("fixed-model.onnx")
                self.assertEqual(len(attempts), 1)
                if platform == "win32":
                    self.assertEqual(attempts[0][2:], (False, "sequential"))

    def test_unavailable_or_broken_gpu_falls_back_to_cpu(self):
        for platform, gpu in (("darwin", "CoreMLExecutionProvider"),
                              ("win32", "DmlExecutionProvider"),
                              ("linux", "CUDAExecutionProvider")):
            with self.subTest(platform=platform):
                namespace, attempts = _namespace(platform, ["CPUExecutionProvider"])
                namespace["_model"]("fixed-model.onnx")
                self.assertEqual(attempts[0][1], ("CPUExecutionProvider",))
                namespace, attempts = _namespace(platform, [gpu, "CPUExecutionProvider"],
                                                 fail_gpu=True)
                namespace["_model"]("fixed-model.onnx")
                self.assertEqual([attempt[1] for attempt in attempts],
                                 [(gpu, "CPUExecutionProvider"), ("CPUExecutionProvider",)])

    def test_cpu_failure_is_not_silenced(self):
        namespace, _ = _namespace("linux", ["CPUExecutionProvider"])
        with mock.patch.dict(namespace, {"ort": SimpleNamespace(
                SessionOptions=lambda: SimpleNamespace(),
                get_available_providers=lambda: ["CPUExecutionProvider"],
                InferenceSession=mock.Mock(side_effect=RuntimeError("invalid model")))}):
            with self.assertRaisesRegex(RuntimeError, "invalid model"):
                namespace["_model"]("fixed-model.onnx")


if __name__ == "__main__":
    unittest.main()
