"""Check the shipped GPU provider policy without optional ONNX Runtime wheels."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "scm_workbench/builtin_processors/advanced_upscaler.py"


def _namespace(platform, available, *, fail_gpu=False, silent_cpu=False, fail_cpu=False):
    source = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    functions = [node for node in source.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_providers", "_model", "_activity", "_available_cpus"}]
    attempts = []

    class Options:
        def __init__(self):
            self.enable_mem_pattern = True
            self.execution_mode = "default"

    class Session:
        def __init__(self, providers):
            self.providers = providers

        def get_providers(self):
            return self.providers

        def get_inputs(self):
            return [SimpleNamespace(name="input")]

        def get_outputs(self):
            return [SimpleNamespace(name="output")]

    def inference_session(path, *, sess_options, providers):
        attempts.append((path, tuple(providers), sess_options.enable_mem_pattern,
                         sess_options.execution_mode, sess_options.intra_op_num_threads,
                         sess_options.inter_op_num_threads))
        if fail_gpu and len(providers) > 1:
            raise RuntimeError("GPU driver is unavailable")
        if fail_cpu and len(providers) == 1:
            raise RuntimeError("CPU session failed")
        return Session(["CPUExecutionProvider"] if silent_cpu else providers)

    ort = SimpleNamespace(SessionOptions=Options, InferenceSession=inference_session,
                          get_available_providers=lambda: available,
                          ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"))
    namespace = {"_session": None, "_input_name": None, "_provider": None,
                 "_fallback": False, "ACTIVITY_PREFIX": "WB_ADVANCED_UPSCALER_ACTIVITY ",
                 "json": json, "ort": ort, "os": os, "sys": SimpleNamespace(platform=platform)}
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
                self.assertEqual(namespace["_provider"], gpu)
                self.assertFalse(namespace["_fallback"])
                namespace["_model"]("fixed-model.onnx")
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0][-1], 1)
                if platform == "win32":
                    self.assertEqual(attempts[0][2:4], (False, "sequential"))

    def test_intra_op_threads_use_process_available_cpus_with_safe_fallback(self):
        for process_count, affinity, machine_count, expected in (
                (12, {0, 1}, 24, 12),
                (None, {0, 1, 2}, 24, 3),
                (0, set(), 6, 6),
                (None, set(), None, 1)):
            with self.subTest(process_count=process_count, affinity=affinity):
                cpu = SimpleNamespace(process_cpu_count=lambda: process_count,
                                      sched_getaffinity=lambda _pid: affinity,
                                      cpu_count=lambda: machine_count)
                namespace, attempts = _namespace("linux", ["CPUExecutionProvider"])
                namespace["os"] = cpu
                namespace["_model"]("fixed-model.onnx")
                self.assertEqual(attempts[0][-2:], (expected, 1))
        namespace, attempts = _namespace("win32", ["DmlExecutionProvider", "CPUExecutionProvider"])
        namespace["os"] = SimpleNamespace(process_cpu_count=lambda: 8)
        namespace["_model"]("fixed-model.onnx")
        self.assertEqual(attempts[0][2:], (False, "sequential", 8, 1))
        namespace, attempts = _namespace("linux", ["CPUExecutionProvider"])
        namespace["os"] = SimpleNamespace(
            process_cpu_count=lambda: (_ for _ in ()).throw(OSError()),
            sched_getaffinity=lambda _pid: (_ for _ in ()).throw(OSError()),
            cpu_count=lambda: 1)
        namespace["_model"]("fixed-model.onnx")
        self.assertEqual(attempts[0][-2:], (1, 1))

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
                self.assertEqual(namespace["_provider"], "CPUExecutionProvider")
                self.assertTrue(namespace["_fallback"])

    def test_silent_cpu_fallback_and_explicit_failure_emit_immediate_activity(self):
        context = {"index": 1, "total": 2, "name": "First.png", "role": "front"}
        for fail_gpu, silent_cpu in ((False, True), (True, False)):
            with self.subTest(fail_gpu=fail_gpu):
                namespace, _ = _namespace("linux", ["CUDAExecutionProvider", "CPUExecutionProvider"],
                                          fail_gpu=fail_gpu, silent_cpu=silent_cpu)
                with mock.patch.dict(namespace, {"print": mock.Mock()}) as bindings:
                    namespace["_model"]("fixed-model.onnx", context)
                    frames = [json.loads(call.args[0].split(" ", 1)[1]) for call in bindings["print"].call_args_list]
                    self.assertTrue(namespace["_fallback"])
                self.assertEqual([frame["phase"] for frame in frames], ["initializing", "fallback"])
                self.assertEqual(frames[-1]["provider"], "CPUExecutionProvider")

    def test_cpu_only_and_both_initializations_fail(self):
        namespace, attempts = _namespace("linux", ["CPUExecutionProvider"])
        namespace["_model"]("fixed-model.onnx")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(namespace["_provider"], "CPUExecutionProvider")
        context = {"index": 1, "total": 1, "name": "CPU.png", "role": "front"}
        for platform, expected in (("linux", ["initializing", "fallback"]),
                                   ("darwin", ["initializing"])):
            namespace, _ = _namespace(platform, ["CPUExecutionProvider"])
            with mock.patch.dict(namespace, {"print": mock.Mock()}) as bindings:
                namespace["_model"]("fixed-model.onnx", context)
                phases = [json.loads(call.args[0].split(" ", 1)[1])["phase"]
                          for call in bindings["print"].call_args_list]
            self.assertEqual(phases, expected)
        namespace, attempts = _namespace("linux", ["CUDAExecutionProvider", "CPUExecutionProvider"],
                                        fail_gpu=True, fail_cpu=True)
        with self.assertRaisesRegex(RuntimeError, "CPU session failed"):
            namespace["_model"]("fixed-model.onnx")
        self.assertEqual(len(attempts), 2)

    def test_tile_activity_precedes_first_completed_image(self):
        source = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                        and node.name == "process_image")
        events = []

        class Array:
            shape = (3, 512, 512)
            def __getitem__(self, key): return self
            def __truediv__(self, other): return self
            def __mul__(self, other): return self
            def __add__(self, other): return self
            def transpose(self, *args): return self

        class Image:
            width, height = 256, 128
            size = (256, 128)
            format = "PNG"
            info = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def load(self): pass
            def getexif(self): return {}
            def getbands(self): return ("R", "G", "B")
            def convert(self, mode): return self
            def crop(self, box): return self
            def paste(self, *args): pass
            def save(self, *args, **kwargs): events.append("saved")

        class Session:
            def get_providers(self): return ["CPUExecutionProvider"]
            def run(self, *args):
                events.append("inference")
                return [Array()]

        image = Image()
        namespace = {"Path": Path, "_model": lambda path, context: Session(), "_input_name": "input",
                     "_provider": "CPUExecutionProvider", "_fallback": True,
                     "SCALE": 4, "TILE": 128, "PAD": 24, "DPI": (1200, 1200),
                     "Image": SimpleNamespace(open=lambda path: image, new=lambda *args: image,
                                              fromarray=lambda *args: image),
                     "ImageOps": SimpleNamespace(exif_transpose=lambda value: value),
                     "np": SimpleNamespace(asarray=lambda *args, **kwargs: Array(), float32=float,
                                           ascontiguousarray=lambda value: value,
                                           pad=lambda value, *args, **kwargs: value,
                                           clip=lambda value, *args: value, uint8=lambda value: value),
                     "_activity": lambda context, phase, provider, tile=0, tiles=0:
                         events.append((phase, tile, tiles))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
        namespace["process_image"](Path("staged.png"), {"model_path": "model.onnx"})
        self.assertEqual(events[:4], [("tile", 0, 2), "inference", ("tile", 1, 2), "inference"])
        self.assertEqual(events[-2:], [("tile", 2, 2), "saved"])
        events.clear()
        namespace["_provider"] = "CUDAExecutionProvider"
        namespace["process_image"](Path("staged.png"), {"model_path": "model.onnx"})
        self.assertEqual(events[:4], [("tile", 0, 2), "inference", ("fallback", 0, 2),
                                      ("tile", 1, 2)])
        self.assertEqual(namespace["_provider"], "CPUExecutionProvider")
        events.clear()

        class RuntimeFallbackSession(Session):
            calls = 0
            def run(self, *args):
                self.calls += 1
                return super().run(*args)
            def get_providers(self):
                return ["CPUExecutionProvider"] if self.calls >= 5 else ["CUDAExecutionProvider"]

        # At 96 tiles only checkpoints 1, 3, 6, ... are published. A provider
        # switch on tile 5 must refer to checkpoint 3, not unpublished tile 4,
        # or the strict worker parser rejects it and all later CPU activity.
        runtime = RuntimeFallbackSession()
        image.width = 128 * 96
        image.size = (image.width, image.height)
        namespace["_model"] = lambda path, context: runtime
        namespace["_provider"] = "CUDAExecutionProvider"
        namespace["process_image"](Path("staged.png"), {"model_path": "model.onnx"})
        activity = [event for event in events if isinstance(event, tuple)]
        self.assertIn(("fallback", 3, 96), activity)
        self.assertLessEqual(len(activity), 36)
        self.assertEqual(activity[-1], ("tile", 96, 96))
        self.assertEqual(namespace["_provider"], "CPUExecutionProvider")
        events.clear()
        class BrokenSession(Session):
            def run(self, *args):
                raise RuntimeError("CPU inference failed")
        namespace["_model"] = lambda path, context: BrokenSession()
        with self.assertRaisesRegex(RuntimeError, "CPU inference failed"):
            namespace["process_image"](Path("staged.png"), {"model_path": "model.onnx"})
        self.assertNotIn("saved", events)

    def test_runtime_inference_failure_retries_once_on_cpu_and_reuses_session(self):
        # Exercise the shipped callback, not a reimplementation: GPU session
        # construction succeeds, then ORT fails lazily at session.run.
        source = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef)]
        events, sessions, failures = [], [], {1}
        missing = ("ONNXRuntimeError: Conv /conv_first/Conv RequireCudnnHandle: "
                   "cuDNN is unavailable or disabled: dlopen failed for libcudnn.so: "
                   "libcudnn.so: cannot open shared object file: No such file or directory")

        class Array:
            shape = (3, 512, 512)
            def __getitem__(self, key): return self
            def __truediv__(self, other): return self
            def __mul__(self, other): return self
            def __add__(self, other): return self
            def transpose(self, *args): return self

        class Image:
            width, height = 128, 128
            size = (128, 128)
            format = "PNG"
            info = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def load(self): pass
            def getexif(self): return {}
            def getbands(self): return ("R", "G", "B")
            def convert(self, mode): return self
            def crop(self, box): return self
            def paste(self, *args): pass
            def save(self, *args, **kwargs): events.append("saved")

        class Session:
            def __init__(self, providers):
                self.providers, self.calls = providers, 0
                sessions.append(self)
            def get_providers(self): return self.providers
            def get_inputs(self): return [SimpleNamespace(name="input")]
            def get_outputs(self): return [SimpleNamespace(name="output")]
            def run(self, *_args):
                self.calls += 1
                events.append(("run", self.providers[0], self.calls))
                if self.providers[0] == "CUDAExecutionProvider" and self.calls in failures:
                    raise RuntimeError(missing if self.calls == 1 else "unrelated GPU failure")
                if self.providers[0] == "CPUExecutionProvider" and cpu_failure[0]:
                    raise RuntimeError("CPU inference failed")
                return [Array()]

        cpu_failure, fail_cpu_setup = [False], [None]
        class Options:
            pass
        def inference_session(path, *, sess_options, providers):
            if providers == ["CPUExecutionProvider"] and fail_cpu_setup[0] == "constructor":
                raise RuntimeError("CPU session construction failed")
            session = Session(providers)
            if providers == ["CPUExecutionProvider"] and fail_cpu_setup[0] == "inputs":
                session.get_inputs = lambda: (_ for _ in ()).throw(RuntimeError("CPU inputs failed"))
            if providers == ["CPUExecutionProvider"] and fail_cpu_setup[0] == "providers":
                session.get_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
            return session
        ort = SimpleNamespace(SessionOptions=Options, InferenceSession=inference_session,
                              get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
        image = Image()
        namespace = {"Path": Path, "_session": None, "_input_name": None,
                     "_provider": None, "_fallback": False, "ACTIVITY_PREFIX": "WB_ADVANCED_UPSCALER_ACTIVITY ",
                     "SCALE": 4, "TILE": 128, "PAD": 24, "DPI": (1200, 1200),
                     "json": json, "os": SimpleNamespace(process_cpu_count=lambda: 2),
                     "sys": SimpleNamespace(platform="linux"), "ort": ort,
                     "print": lambda line, **kwargs: events.append(json.loads(line.split(" ", 1)[1])),
                     "Image": SimpleNamespace(open=lambda path: image, new=lambda *args: image,
                                              fromarray=lambda *args: image),
                     "ImageOps": SimpleNamespace(exif_transpose=lambda value: value),
                     "np": SimpleNamespace(asarray=lambda *args, **kwargs: Array(), float32=float,
                                           ascontiguousarray=lambda value: value,
                                           pad=lambda value, *args, **kwargs: value,
                                           clip=lambda value, *args: value, uint8=lambda value: value)}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
        def process(index=1, total=1):
            namespace["process_image"](Path("staged.png"), {"model_path": "model.onnx",
                "index": index, "total": total, "name": f"{index}.png", "role": "front"})
        def frames(): return [event for event in events if isinstance(event, dict)]

        process(1, 2)
        self.assertEqual([s.providers for s in sessions],
                         [["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]])
        self.assertEqual([(s.calls) for s in sessions], [1, 1])
        self.assertEqual([f["phase"] for f in frames()], ["initializing", "tile", "fallback", "tile"])
        self.assertEqual(frames()[2]["reason"], "missing_cudnn")
        self.assertEqual(frames()[2]["tile"], 0)
        self.assertEqual(events[-1], "saved")
        self.assertEqual(namespace["_provider"], "CPUExecutionProvider")
        process(2, 2)
        self.assertEqual(len(sessions), 2)  # remaining images reuse the CPU-only session
        self.assertEqual(sessions[1].calls, 2)
        self.assertEqual([f["phase"] for f in frames() if f["index"] == 2],
                         ["initializing", "tile", "tile"])
        self.assertEqual(events.count("saved"), 2)

        # A later tile failure must refer to the last *reported* checkpoint.
        events.clear(); sessions.clear(); failures.clear(); failures.add(5)
        namespace["_session"] = None; namespace["_provider"] = None
        image.width = 128 * 96; image.size = (image.width, image.height)
        process()
        fallback = [f for f in frames() if f["phase"] == "fallback"]
        self.assertEqual([(f["tile"], f.get("reason")) for f in fallback], [(3, None)])
        self.assertEqual([s.calls for s in sessions], [5, 92])
        self.assertLessEqual(len(frames()), 36)
        self.assertEqual(events[-1], "saved")

        # An unsuccessful CPU retry is terminal; no new session or save.
        events.clear(); sessions.clear(); failures.clear(); failures.add(1)
        namespace["_session"] = None; namespace["_provider"] = None
        image.width = 128; image.size = (128, 128)
        cpu_failure[0] = True
        with self.assertRaisesRegex(RuntimeError, "CPU inference failed"):
            process()
        self.assertEqual(len(sessions), 2)
        self.assertEqual([s.calls for s in sessions], [1, 1])
        self.assertNotIn("saved", events)
        cpu_failure[0] = False

        # CPU construction/input validation failure must not claim CPU work.
        for failure in ("constructor", "inputs", "providers"):
            events.clear(); sessions.clear(); fail_cpu_setup[0] = failure
            namespace["_session"] = None; namespace["_provider"] = None
            with self.subTest(failure=failure), self.assertRaisesRegex((RuntimeError, ValueError), "CPU"):
                process()
            self.assertNotIn("fallback", [f["phase"] for f in frames()])
            self.assertNotIn("saved", events)

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
