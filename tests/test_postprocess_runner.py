import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scm_workbench.postprocessing import CONTRACT_VERSION, revision_digest, stage_images, discover_images


LIMITS = {"cpu_seconds": 60, "address_space": 1024 * 1024 * 1024,
          "file_size": 128 * 1024 * 1024, "open_files": 64, "processes": 8}


def runner_entries(entries):
    return [{key: entry[key] for key in ("role", "relative_path", "name", "staged", "index", "total")}
            for entry in entries]


def png():
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
    )


class RunnerTests(unittest.TestCase):
    def test_one_process_callback_order_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "game/front").mkdir(parents=True)
            (root / "game/double_sided").mkdir(parents=True)
            (root / "game/front/b.png").write_bytes(png())
            (root / "game/front/a.png").write_bytes(png())
            records = discover_images(root)
            entries = stage_images(records, root / "run")
            source = root / "run" / "processor.py"
            source_text = """
from pathlib import Path
STATE = []
def process_image(image_path, context):
    STATE.append(context['name'])
    Path(image_path).write_bytes(Path(image_path).read_bytes())
"""
            source.write_bytes(source_text.encode("utf-8"))
            manifest = root / "run" / "manifest.json"
            manifest.write_text(json.dumps({"source_path": str(source), "run_root": str(root / "run"),
                "entries": runner_entries(entries), "revision": revision_digest(source_text, []),
                "requirements": [], "contract": CONTRACT_VERSION, "environment": "",
                "environment_root": str(root / "environments"), "limits": LIMITS}), encoding="utf-8")
            result = subprocess.run([sys.executable, "-u", "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line for line in result.stdout.splitlines() if line.startswith("WB_POSTPROCESS_PROGRESS ")]
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line.split(" ", 1)[1])["index"] for line in lines], [1, 2])

    def test_private_environment_is_inserted_without_processing_pth_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); (root / "run/work/front").mkdir(parents=True)
            image = root / "run/work/front/a.png"; image.write_bytes(png())
            environment_root = root / "environments"
            site_packages = environment_root / ("a" * 64) / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / "demo_dependency.py").write_text("VALUE = b'\\0'\n", encoding="utf-8")
            # Direct sys.path insertion must not execute .pth contents.
            (site_packages / "hostile.pth").write_text("raise RuntimeError('pth executed')\n", encoding="utf-8")
            source_text = (
                "import demo_dependency\n"
                "def process_image(image_path, context):\n"
                "    with open(image_path, 'ab') as stream:\n"
                "        stream.write(demo_dependency.VALUE)\n"
            )
            source = root / "run" / "processor.py"; source.write_bytes(source_text.encode("utf-8"))
            requirements = ["demo==1.0"]
            manifest = root / "run" / "manifest.json"
            manifest.write_text(json.dumps({
                "source_path": str(source), "run_root": str(root / "run"),
                "entries": [{"staged": str(image), "name": "a.png", "role": "front",
                             "relative_path": "game/front/a.png", "index": 1, "total": 1}],
                "revision": revision_digest(source_text, requirements),
                "requirements": requirements, "contract": CONTRACT_VERSION,
                "environment": str(site_packages), "environment_root": str(environment_root),
                "limits": LIMITS,
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", "-B", "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(image.read_bytes().endswith(b"\0"))
            self.assertEqual(list(site_packages.rglob("*.pyc")), [])

    def test_non_none_result_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); (root / "run/work/front").mkdir(parents=True)
            image = root / "run/work/front/a.png"; image.write_bytes(png())
            source_text = "def process_image(image_path, context):\n    return 1\n"
            source = root / "run" / "processor.py"; source.write_bytes(source_text.encode("utf-8"))
            manifest = root / "run" / "manifest.json"; manifest.write_text(json.dumps({
                "source_path": str(source), "run_root": str(root / "run"),
                "entries": [{"staged": str(image), "name": "a.png", "role": "front",
                             "relative_path": "game/front/a.png", "index": 1, "total": 1}],
                "revision": revision_digest(source_text, []), "requirements": [],
                "contract": CONTRACT_VERSION, "environment": "",
                "environment_root": str(root / "environments"), "limits": LIMITS}))
            result = subprocess.run([sys.executable, "scm_workbench/postprocess_runner.py", "--manifest", str(manifest)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must return None", result.stdout)


if __name__ == "__main__":
    unittest.main()
