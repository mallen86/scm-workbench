"""Check bundled processor encoding choices without requiring optional AI wheels."""
import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scm_workbench/builtin_processors/advanced_upscaler.py"


class AdvancedUpscalerEncodingTests(unittest.TestCase):
    def test_png_uses_fast_lossless_save_while_jpeg_keeps_its_settings(self):
        source = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        process = next(node for node in source.body
                       if isinstance(node, ast.FunctionDef) and node.name == "process_image")
        encoding = next(node for node in process.body
                        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                        and isinstance(node.test.left, ast.Name)
                        and node.test.left.id == "image_format"
                        and len(node.test.comparators) == 1
                        and isinstance(node.test.comparators[0], ast.Constant)
                        and node.test.comparators[0].value == "JPEG")
        save = process.body[-1]
        self.assertIsInstance(save, ast.Expr)
        self.assertIsInstance(save.value, ast.Call)
        self.assertEqual(save.value.func.attr, "save")
        self.assertTrue(any(key.arg is None and isinstance(key.value, ast.Name)
                            and key.value.id == "options" for key in save.value.keywords))

        policy = compile(ast.Module(body=[encoding], type_ignores=[]), str(SOURCE), "exec")
        for image_format, expected in (("PNG", False), ("JPEG", True), ("WEBP", None)):
            with self.subTest(image_format=image_format):
                namespace = {"image_format": image_format, "options": {}}
                exec(policy, namespace)
                self.assertEqual(namespace["options"].get("optimize"), expected)
                if image_format == "JPEG":
                    self.assertEqual(namespace["options"]["quality"], 95)
                    self.assertEqual(namespace["options"]["subsampling"], 0)


if __name__ == "__main__":
    unittest.main()
