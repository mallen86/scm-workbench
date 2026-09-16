import hashlib
import tempfile
import unittest
from pathlib import Path

from scm_workbench.postprocessing import (
    ConflictError,
    IntegrityError,
    ProcessorStore,
    PublicationTransaction,
    ValidationError,
    discover_images,
    environment_fingerprint,
    normalize_requirements,
    recover_transactions,
    revision_digest,
    stage_images,
    validate_source,
    validate_wheel_report,
    wheel_only_pip_argv,
)


def png(width=2, height=3):
    from io import BytesIO
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


SOURCE = "def process_image(image_path, context):\n    return None\n"


class PostprocessorTests(unittest.TestCase):
    def test_source_and_requirements_validation(self):
        self.assertEqual(normalize_requirements("Pillow==1.0\nnumpy[foo,bar]"), ("numpy[bar,foo]", "pillow==1.0"))
        with self.assertRaises(ValidationError): normalize_requirements("--index-url https://evil")
        with self.assertRaises(ValidationError): normalize_requirements("name @ https://evil")
        with self.assertRaises(ValidationError): validate_source("def process_image(x): pass")
        with self.assertRaises(ValidationError): validate_source("def process_image(a, b):\n    return 1\n\ndef process_image(c, d): pass")
        self.assertEqual(len(revision_digest(SOURCE, ())), 64)

    def test_store_immutable_revisions_compare_and_set_and_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProcessorStore(temp)
            first = store.save("Example", SOURCE, ["Pillow"])
            self.assertFalse(first["trusted"])
            trusted = store.trust(first["id"], first["revision"], "env-a")
            self.assertTrue(trusted["trusted"])
            with self.assertRaises(ConflictError):
                store.save("Example", SOURCE + "\n# edit\n", ["Pillow"], processor_id=first["id"], expected_revision="wrong")
            second = store.save("Example", SOURCE + "\n# edit\n", ["Pillow"], processor_id=first["id"], expected_revision=first["revision"])
            self.assertNotEqual(first["revision"], second["revision"])
            self.assertFalse(second["trusted"])
            self.assertEqual(len(store.list()), 1)
            self.assertEqual(store.duplicate(first["id"])["name"], "Example copy")
            store.delete(first["id"], expected_revision=second["revision"])

    def test_discovery_and_private_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "game/front").mkdir(parents=True)
            (root / "game/double_sided").mkdir(parents=True)
            (root / "game/front/Card.png").write_bytes(png())
            (root / "game/double_sided/Other.png").write_bytes(png())
            (root / "game/front/README.md").write_text("placeholder")
            records = discover_images(root)
            self.assertEqual([r.name for r in records], ["Card.png", "Other.png"])
            staged = stage_images(records, root / "run")
            self.assertEqual(len(staged), 2)
            self.assertEqual(Path(staged[0]["staged"]).read_bytes(), (root / "game/front/Card.png").read_bytes())
            self.assertNotEqual(Path(staged[0]["staged"]).resolve(), (root / "game/front/Card.jpg").resolve())

    def test_transaction_rollback_and_startup_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "game/front/a.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            stage = root / "stage.png"; stage.write_bytes(b"new")
            phases = {"publish"}
            def fault(phase):
                if phase in phases: raise OSError("injected")
            tx = PublicationTransaction(root / "transactions", fault=fault)
            with self.assertRaises(Exception): tx.publish([(destination, stage)])
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(stage.exists())
            # A durable incomplete journal is recovered even without the
            # original transaction object.
            stage.write_bytes(b"new")
            tx2 = PublicationTransaction(root / "transactions")
            tx2.publish([(destination, stage)])
            self.assertEqual(destination.read_bytes(), b"new")
            self.assertEqual(recover_transactions(root / "transactions"), ())

    def test_environment_and_pip_helpers_are_bounded(self):
        fingerprint = environment_fingerprint(["Pillow"])
        self.assertEqual(len(fingerprint), 64)
        argv = wheel_only_pip_argv("python", ["Pillow"], target="target", offline=True)
        self.assertIn("--only-binary=:all:", argv)
        self.assertIn("--no-index", argv)
        report = {"install": [{"metadata": {"name": "Pillow", "version": "1"}, "download_info": {"url": "https://example.test/Pillow.whl", "archive_info": {"hashes": {"sha256": "a" * 64}}}}]}
        self.assertEqual(validate_wheel_report(report)[0]["name"], "Pillow")
        with self.assertRaises(ValidationError): validate_wheel_report({"install": [{"metadata": {}, "download_info": {"url": "http://x/a.whl", "archive_info": {}}}]})


if __name__ == "__main__":
    unittest.main()
