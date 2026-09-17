import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench.postprocessing import (
    ConflictError,
    IntegrityError,
    ProcessorStore,
    PublicationTransaction,
    TransactionError,
    ValidationError,
    discover_images,
    environment_fingerprint,
    interpreter_fingerprint,
    normalize_requirements,
    recover_transactions,
    revision_digest,
    stage_images,
    validate_source,
    validate_wheel_report,
    wheel_only_pip_argv,
)


def png():
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
    )


SOURCE = "def process_image(image_path, context):\n    return None\n"


class PostprocessorTests(unittest.TestCase):
    def test_source_and_requirements_validation(self):
        self.assertEqual(normalize_requirements("Pillow==1.0\nnumpy[foo,bar]"), ("numpy[bar,foo]", "pillow==1.0"))
        with self.assertRaises(ValidationError): normalize_requirements("--index-url https://evil")
        with self.assertRaises(ValidationError): normalize_requirements("name @ https://evil")
        with self.assertRaises(ValidationError): normalize_requirements("Pillow\npillow==1.0")
        with self.assertRaises(ValidationError): validate_source("def process_image(x): pass")
        with self.assertRaises(ValidationError): validate_source("def process_image(a, b):\n    return 1\n\ndef process_image(c, d): pass")
        with self.assertRaises(ValidationError): validate_source("def process_image(a, b):\n    pass\nreturn\n")
        self.assertEqual(len(revision_digest(SOURCE, ())), 64)

    def test_store_immutable_revisions_compare_and_set_and_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProcessorStore(temp)
            first = store.save("Example", SOURCE, [])
            self.assertFalse(first["trusted"])
            fingerprint = store.environment_metadata(first["requirements"])["fingerprint"]
            with self.assertRaises(ConflictError):
                store.trust(first["id"], first["revision"], "env-a")
            trusted = store.trust(first["id"], first["revision"], fingerprint)
            self.assertTrue(trusted["trusted"])
            with self.assertRaises(ConflictError):
                store.save("Example", SOURCE + "\n# edit\n", [], processor_id=first["id"], expected_revision="wrong")
            second = store.save("Example", SOURCE + "\n# edit\n", [], processor_id=first["id"], expected_revision=first["revision"])
            self.assertNotEqual(first["revision"], second["revision"])
            self.assertFalse(second["trusted"])
            with self.assertRaisesRegex(ConflictError, "revision is stale"):
                store.trust(first["id"], first["revision"])
            self.assertEqual(len(store.list()), 1)
            self.assertEqual(store.duplicate(first["id"])["name"], "Example copy")
            store.delete(first["id"], expected_revision=second["revision"])

    def test_recording_an_unchanged_environment_preserves_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProcessorStore(temp)
            item = store.save("Libraries", SOURCE, "demo==1.0")

            def publish_environment(artifact_hash):
                wheels = [{
                    "canonical_name": "demo", "name": "demo", "version": "1.0",
                    "sha256": artifact_hash,
                }]
                lock_hash = hashlib.sha256(
                    f"demo==1.0 --hash=sha256:{artifact_hash}\n".encode("utf-8")
                ).hexdigest()
                environment = store.environment_metadata(item["requirements"], lock_hash)
                path = Path(environment["path"])
                (path / "site-packages").mkdir(parents=True)
                (path / "ready.json").write_text(json.dumps({
                    "fingerprint": environment["fingerprint"],
                    "requirements": item["requirements"],
                    "lock_hash": lock_hash,
                    "wheels": wheels,
                    "files": 1,
                    "bytes": 1,
                    "tree_digest": artifact_hash,
                }), encoding="utf-8")
                return lock_hash, environment["fingerprint"]

            first_lock, first_fingerprint = publish_environment("a" * 64)
            installed = store.record_environment(item["id"], item["revision"], first_lock)
            self.assertFalse(installed["processor"]["trusted"])
            store.trust(item["id"], item["revision"], first_fingerprint)
            unchanged = store.record_environment(item["id"], item["revision"], first_lock)
            self.assertTrue(unchanged["processor"]["trusted"])

            edited = store.save(
                "Libraries", SOURCE + "\n# source-only edit\n", "demo==1.0",
                processor_id=item["id"], expected_revision=item["revision"],
            )
            edited_status = store.status(edited["id"])
            self.assertTrue(edited_status["environment"]["ready"])
            self.assertEqual(edited_status["environment"]["fingerprint"], first_fingerprint)
            self.assertFalse(edited_status["processor"]["trusted"])
            store.trust(edited["id"], edited["revision"], first_fingerprint)

            second_lock, _ = publish_environment("b" * 64)
            changed = store.record_environment(edited["id"], edited["revision"], second_lock)
            self.assertFalse(changed["processor"]["trusted"])
            requirements_changed = store.save(
                "Libraries", SOURCE + "\n# dependency edit\n", "demo==2.0",
                processor_id=edited["id"], expected_revision=edited["revision"],
            )
            self.assertFalse(store.status(requirements_changed["id"])["environment"]["ready"])

            plain = store.save("No libraries", SOURCE, "")
            plain_fingerprint = store.environment_metadata([])["fingerprint"]
            store.trust(plain["id"], plain["revision"], plain_fingerprint)
            unchanged_plain = store.record_environment(plain["id"], plain["revision"], "")
            self.assertTrue(unchanged_plain["processor"]["trusted"])

    def test_saved_revision_tampering_is_detected_before_trust_or_run(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProcessorStore(temp)
            item = store.save("Example", SOURCE, [])
            source_path = store._processor(item["id"]) / "revisions" / f"{item['revision']}.py"
            source_path.write_text(SOURCE + "# changed outside Workbench\n", encoding="utf-8")
            with self.assertRaises(IntegrityError):
                store.get(item["id"])
            with self.assertRaises(IntegrityError):
                store.trust(item["id"], item["revision"])

    def test_saved_revision_hard_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProcessorStore(temp)
            item = store.save("Example", SOURCE, [])
            source_path = store._processor(item["id"]) / "revisions" / f"{item['revision']}.py"
            os.link(source_path, source_path.with_suffix(".linked"))
            with self.assertRaisesRegex(IntegrityError, "invalid processor source"):
                store.get(item["id"])

    def test_discovery_and_private_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "game/front").mkdir(parents=True)
            (root / "game/double_sided").mkdir(parents=True)
            (root / "game/front/Card 10.png").write_bytes(png())
            (root / "game/front/Card 2.png").write_bytes(png())
            (root / "game/double_sided/Other.png").write_bytes(png())
            (root / "game/front/README.md").write_text("placeholder")
            (root / "game/front/mislabeled.jpg").write_bytes(png())
            records = discover_images(root)
            self.assertEqual([r.name for r in records], ["Card 2.png", "Card 10.png", "Other.png"])
            staged = stage_images(records, root / "run")
            self.assertEqual(len(staged), 3)
            self.assertEqual(Path(staged[0]["staged"]).read_bytes(), (root / "game/front/Card 2.png").read_bytes())
            self.assertNotEqual(Path(staged[0]["staged"]).resolve(), (root / "game/front/Card.jpg").resolve())

    def test_transaction_rollback_and_startup_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "game/front/a.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            stage = root / "runs" / "fixture" / "stage.png"
            stage.parent.mkdir(parents=True); stage.write_bytes(b"new")
            phases = {"publish"}
            def fault(phase):
                if phase in phases: raise OSError("injected")
            tx = PublicationTransaction(root / "transactions", fault=fault)
            with self.assertRaises(Exception): tx.publish([(destination, stage)])
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(stage.exists())
            for phase in ("journal-prepared", "quarantine-renamed", "quarantine", "destination-published"):
                destination.write_bytes(b"old")
                stage.write_bytes(b"new")
                guarded = PublicationTransaction(
                    root / "transactions", fault=lambda reached, phase=phase: (_ for _ in ()).throw(OSError("injected")) if reached == phase else None,
                )
                with self.assertRaises(Exception):
                    guarded.publish([(destination, stage)])
                self.assertEqual(destination.read_bytes(), b"old")
                if phase == "journal-prepared":
                    with self.assertRaisesRegex(IntegrityError, "configured SCM checkout"):
                        recover_transactions(root / "transactions", root / "other-checkout")
                recovered = recover_transactions(root / "transactions", root)
                self.assertEqual(len(recovered), 1 if phase == "journal-prepared" else 0)
                self.assertFalse(stage.exists())
            destination.write_bytes(b"old")
            stage.write_bytes(b"new")
            committed = PublicationTransaction(
                root / "transactions",
                fault=lambda reached: (_ for _ in ()).throw(OSError("injected")) if reached == "committed" else None,
            )
            with self.assertRaisesRegex(Exception, "committed") as raised:
                committed.publish([(destination, stage)])
            self.assertTrue(raised.exception.committed)
            self.assertEqual(destination.read_bytes(), b"new")
            # A durable incomplete journal is recovered even without the
            # original transaction object.
            stage.write_bytes(b"new")
            tx2 = PublicationTransaction(root / "transactions")
            tx2.publish([(destination, stage)])
            self.assertEqual(destination.read_bytes(), b"new")
            self.assertEqual(recover_transactions(root / "transactions", root), ())

    def test_publication_detects_same_identity_content_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "game/front/a.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            observed = destination.stat()
            identity = [observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns]
            stage = root / "runs/fixture/a.png"
            stage.parent.mkdir(parents=True); stage.write_bytes(b"new")
            destination.write_bytes(b"BAD")
            os.utime(destination, ns=(observed.st_atime_ns, observed.st_mtime_ns))
            tx = PublicationTransaction(root / "transactions", "digest-conflict")
            with self.assertRaises(TransactionError):
                tx.publish([(destination, stage)], expected={str(destination): identity},
                           expected_digests={str(destination): hashlib.sha256(b"old").hexdigest()})
            self.assertEqual(destination.read_bytes(), b"BAD")

    def test_rollback_refuses_same_identity_quarantine_content_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "game/front/a.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            observed = destination.stat()
            identity = [observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns]
            stage = root / "runs/fixture/stage.png"
            stage.parent.mkdir(parents=True); stage.write_bytes(b"new")
            tx = PublicationTransaction(root / "transactions", "quarantine-digest")
            def fault(phase):
                if phase == "publish":
                    quarantine = destination.parent / f".wb-old-{tx.id}-0"
                    prior = quarantine.stat()
                    quarantine.write_bytes(b"BAD")
                    os.utime(quarantine, ns=(prior.st_atime_ns, prior.st_mtime_ns))
                    raise OSError("injected")
            tx.fault = fault
            with self.assertRaises(TransactionError) as raised:
                tx.publish([(destination, stage)], expected={str(destination): identity},
                           expected_digests={str(destination): hashlib.sha256(b"old").hexdigest()})
            self.assertFalse(raised.exception.rollback_safe)
            self.assertEqual(destination.read_bytes(), b"new")

    def test_rollback_refuses_to_overwrite_a_newer_external_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "game/front/a.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            stage = root / "runs" / "fixture" / "stage.png"
            stage.parent.mkdir(parents=True); stage.write_bytes(b"new")
            def fault(phase):
                if phase == "publish":
                    destination.write_bytes(b"external")
                    raise OSError("injected")
            tx = PublicationTransaction(root / "transactions", fault=fault)
            with self.assertRaisesRegex(Exception, "rollback failed"):
                tx.publish([(destination, stage)])
            self.assertEqual(destination.read_bytes(), b"external")
            self.assertTrue(tx.journal.exists())

    def test_interpreter_probe_disables_bytecode_writes(self):
        calls = []

        class FakeProcess:
            returncode = 0

            def wait(self, timeout):
                return 0

        def fake_popen(argv, **kwargs):
            calls.append(argv)
            kwargs["stdout"].write(json.dumps({
                "implementation": "CPython", "version": [3, 13, 0], "abi": "",
                "soabi": "cpython-313-test", "cache_tag": "cpython-313",
                "platform": "test", "machine": "arm64",
            }).encode("utf-8"))
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temp:
            interpreter = Path(temp) / "python"
            interpreter.write_bytes(b"fixture")
            with mock.patch("scm_workbench.postprocessing.subprocess.Popen", side_effect=fake_popen):
                self.assertEqual(len(interpreter_fingerprint(interpreter)), 64)
        self.assertEqual(calls[0][1:4], ["-I", "-B", "-c"])

    def test_environment_and_pip_helpers_are_bounded(self):
        fingerprint = environment_fingerprint(["Pillow"])
        self.assertEqual(len(fingerprint), 64)
        argv = wheel_only_pip_argv("python", ["Pillow"], target="target", offline=True)
        self.assertEqual(argv[:4], ["python", "-B", "-m", "pip"])
        self.assertIn("--only-binary=:all:", argv)
        self.assertIn("--no-index", argv)
        report = {"install": [{"metadata": {"name": "Pillow", "version": "1"}, "download_info": {"url": "https://files.pythonhosted.org/packages/Pillow.whl", "archive_info": {"hashes": {"sha256": "a" * 64}}}}]}
        self.assertEqual(validate_wheel_report(report)[0]["name"], "Pillow")
        with self.assertRaises(ValidationError): validate_wheel_report({"install": [{"metadata": {}, "download_info": {"url": "http://x/a.whl", "archive_info": {}}}]})


if __name__ == "__main__":
    unittest.main()
