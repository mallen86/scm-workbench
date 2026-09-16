import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scm_workbench.postprocessing import stage_images, discover_images


def png():
    from io import BytesIO
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (1, 1), "white").save(output, format="PNG")
    return output.getvalue()


class RunnerTests(unittest.TestCase):
    def test_one_process_callback_order_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "game/front").mkdir(parents=True)
            (root / "game/double_sided").mkdir(parents=True)
            (root / "game/front/b.png").write_bytes(png())
            (root / "game/front/a.png").write_bytes(png())
            records = discover_images(root)
            entries = stage_images(records, root / "run")
            source = root / "processor.py"
            source.write_text("""
from pathlib import Path
STATE = []
def process_image(image_path, context):
    STATE.append(context['name'])
    Path(image_path).write_bytes(Path(image_path).read_bytes())
""", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"source_path": str(source), "run_root": str(root / "run"), "entries": list(entries)}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-u", "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.startswith("WB_POSTPROCESS_PROGRESS ")]
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line.split(" ", 1)[1])["index"] for line in lines], [1, 2])

    def test_non_none_result_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "run/work/front").mkdir(parents=True)
            image = root / "run/work/front/a.png"; image.write_bytes(png())
            source = root / "processor.py"; source.write_text("def process_image(image_path, context):\n    return 1\n")
            manifest = root / "manifest.json"; manifest.write_text(json.dumps({"source_path": str(source), "run_root": str(root / "run"), "entries": [{"staged": str(image), "name": "a.png", "role": "front"}]}))
            result = subprocess.run([sys.executable, "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must return None", result.stdout)


if __name__ == "__main__":
    unittest.main()
