"""App-owned source upgrades reuse verified libraries, never unapproved caches."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from scm_workbench import advanced_model, postprocessing, server


SOURCE = "def process_image(image_path, context):\n    return None\n"
MODEL = b"bounded test model"
IDENT = server.BUILTIN_ADVANCED_UPSCALER_ID
NAME = "Advanced Upscaler (AI 4×)"


class BundledEnvironmentReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for attribute, value in (("MODEL_BYTES", len(MODEL)),
                                 ("MODEL_SHA256", hashlib.sha256(MODEL).hexdigest())):
            patch = mock.patch.object(advanced_model, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

    def seed(self, name="default", profile="cuda12", source=SOURCE, interpreter=sys.executable):
        store = postprocessing.ProcessorStore(self.root / name)
        req = advanced_model.requirements_for_platform("linux", profile)
        item = store.provision_bundled(IDENT, NAME, source, requirements=req, optional_model=True,
                                       interpreter=interpreter)
        wheels = [{"canonical_name": requirement.split("==")[0],
                   "version": requirement.split("==")[1], "sha256": "a" * 64}
                  for requirement in req]
        lock = hashlib.sha256(postprocessing.wheel_lock_text(wheels).encode()).hexdigest()
        environment = store.environment_metadata(req, lock, interpreter=interpreter)
        location = Path(environment["path"])
        site = location / "site-packages"
        site.mkdir(parents=True)
        (site / "library.py").write_bytes(b"# installed wheel fixture\n")
        (site / advanced_model.MODEL_NAME).write_bytes(MODEL)
        count, size, digest = server._validate_dependency_tree(site)
        postprocessing._atomic_json(location / "ready.json", {
            "fingerprint": environment["fingerprint"], "requirements": list(req),
            "lock_hash": lock, "wheels": wheels,
            "files": count, "bytes": size, "tree_digest": digest,
        })
        store.record_environment(IDENT, item["revision"], lock, interpreter=interpreter)
        self.assertTrue(store.status(IDENT, interpreter=interpreter)["processor"]["ready_to_run"])
        return store, req, location

    def upgrade(self, store, req, source=SOURCE + "\n# new shipped code\n", **kwargs):
        return store.provision_bundled(
            IDENT, NAME, source, requirements=req, optional_model=True,
            environment_verifier=server._verify_advanced_environment_for_reuse, **kwargs)

    def snapshot(self, location):
        return {p.relative_to(location).as_posix(): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                for p in location.rglob("*") if p.is_file()}

    def test_repeated_source_and_name_updates_keep_installation_without_rewriting_environment(self):
        store, req, location = self.seed()
        installed = store._metadata(IDENT)
        original_files = self.snapshot(location)
        verify = mock.Mock(wraps=server._verify_advanced_environment_for_reuse)
        for version in range(3):
            source = SOURCE + f"\n# release {version}\n"
            name = NAME + f" {version}"
            item = store.provision_bundled(
                IDENT, name, source, requirements=req, optional_model=True,
                environment_verifier=verify)
            status = store.status(IDENT)
            self.assertTrue(status["processor"]["ready_to_run"])
            self.assertEqual(store._metadata(IDENT)["installed_revision"], item["revision"])
            for key in ("installed_environment", "installed_tree_digest", "lock_hash", "environment", "trusted_tree_digest"):
                self.assertEqual(store._metadata(IDENT)[key], installed[key])
            self.assertEqual(self.snapshot(location), original_files)
            store.provision_bundled(IDENT, name, source, requirements=req, optional_model=True,
                                    environment_verifier=verify)
            self.assertEqual(verify.call_count, version + 1)  # no rehash on ordinary refresh
        renamed = store.provision_bundled(IDENT, "Renamed bundled upscaler", source,
                                         requirements=req, optional_model=True, environment_verifier=verify)
        self.assertEqual(renamed["revision"], item["revision"])
        self.assertTrue(store.status(IDENT)["processor"]["ready_to_run"])

    def test_real_server_upgrade_preserves_both_installed_cuda_profiles(self):
        shipped = server.BUILTIN_ADVANCED_UPSCALER_FILE.read_bytes().decode("utf-8")
        for profile in ("cuda12", "cuda13"):
            with self.subTest(profile=profile):
                store, req, location = self.seed(profile, profile, shipped + "\n# previous shipped revision\n")
                original = self.snapshot(location)
                before = store._metadata(IDENT)
                scm = self.root / "scm"
                scm.mkdir(exist_ok=True)
                with mock.patch.object(server, "DATA_DIR", store.data_root), mock.patch.object(
                        server.sys, "platform", "linux"), mock.patch.object(
                        advanced_model, "REQUIREMENTS", advanced_model.requirements_for_platform("linux")), mock.patch.object(
                        server.cuda_detection, "detect_cuda_profile", return_value={"recommended": "cuda13", "reason": "fixture"}), mock.patch.object(
                        server, "_prepare_dependency_job", side_effect=AssertionError("must not reinstall")), mock.patch.object(
                        advanced_model, "download_model", side_effect=AssertionError("must not download")):
                    updated = server._postprocessor_store(scm_root=scm, interpreter=Path(sys.executable))
                    status = server._postprocessor_status(updated, IDENT, Path(sys.executable))
                    self.assertTrue(status["processor"]["ready_to_run"])
                    self.assertEqual(status["processor"]["cuda_profile"], profile)
                    self.assertEqual(updated.get(IDENT)["source"], shipped)
                    self.assertNotEqual(updated.get(IDENT)["revision"], before["active_revision"])
                    self.assertEqual(updated._metadata(IDENT)["lock_hash"], before["lock_hash"])
                    self.assertEqual(tuple(updated.get(IDENT)["requirements"]), req)
                self.assertEqual(self.snapshot(location), original)

    def test_changed_requirements_or_contract_require_reinstallation(self):
        for changed in ("requirements", "contract"):
            with self.subTest(changed=changed):
                store, req, location = self.seed(changed)
                original = self.snapshot(location)
                if changed == "requirements":
                    req = advanced_model.requirements_for_platform("linux", "cuda13")
                else:
                    store = postprocessing.ProcessorStore(store.data_root, contract="next-contract")
                self.upgrade(store, req)
                self.assertFalse(store.status(IDENT)["processor"]["ready_to_run"])
                self.assertNotIn("installed_revision", store._metadata(IDENT))
                self.assertEqual(self.snapshot(location), original)

    def test_uninstalled_or_untrusted_environment_is_not_reactivated(self):
        for change in ("uninstall", "trusted", "environment", "trusted_tree_digest", "installed_revision", "lock_hash"):
            with self.subTest(change=change):
                store, req, location = self.seed(change)
                metadata = store._metadata(IDENT)
                if change == "uninstall":
                    store.remove_optional_environment(IDENT, metadata["active_revision"])
                else:
                    metadata[change] = "f" * 64
                    postprocessing._atomic_json(store._processor(IDENT) / "metadata.json", metadata)
                original = self.snapshot(location)
                self.upgrade(store, req)
                self.assertFalse(store.status(IDENT)["processor"]["ready_to_run"])
                self.assertNotIn("installed_revision", store._metadata(IDENT))
                self.assertEqual(self.snapshot(location), original)

    def test_library_and_model_bytes_are_verified_not_just_ready_marker(self):
        for change in ("library", "model", "missing_model", "model_pin"):
            with self.subTest(change=change):
                store, req, location = self.seed(change)
                site = location / "site-packages"
                if change == "library":
                    (site / "library.py").write_bytes(b"changed library")
                elif change == "model":
                    (site / advanced_model.MODEL_NAME).write_bytes(b"changed model")
                elif change == "missing_model":
                    (site / advanced_model.MODEL_NAME).unlink()
                # A new model pin rejects even an intact, previously trusted tree.
                with mock.patch.object(advanced_model, "MODEL_SHA256",
                                       "f" * 64 if change == "model_pin" else hashlib.sha256(MODEL).hexdigest()):
                    self.upgrade(store, req)
                self.assertFalse(store.status(IDENT)["processor"]["ready_to_run"])
                self.assertTrue(location.is_dir())  # retained for safe repair, not deleted

    def test_incompatible_runtime_and_missing_verifier_fail_closed(self):
        for change in ("runtime", "verifier"):
            with self.subTest(change=change):
                store, req, location = self.seed(change)
                original = self.snapshot(location)
                if change == "runtime":
                    with mock.patch.object(postprocessing, "environment_fingerprint", return_value="f" * 64), mock.patch.object(
                            postprocessing, "_legacy_environment_compatible", return_value=False):
                        self.upgrade(store, req)
                        self.assertFalse(store.status(IDENT)["processor"]["ready_to_run"])
                else:
                    store.provision_bundled(IDENT, NAME, SOURCE + "\n# update", requirements=req, optional_model=True)
                    self.assertFalse(store.status(IDENT)["processor"]["ready_to_run"])
                self.assertEqual(self.snapshot(location), original)

    def test_compatible_python_patch_and_bundle_path_change_keep_installation(self):
        old_python, new_python = self.root / "old/python", self.root / "updated/python"
        for path in (old_python, new_python):
            path.parent.mkdir()
            path.write_bytes(str(path).encode())
        class Process:
            returncode = 0
            def wait(self, timeout): return 0
        def probe(argv, **kwargs):
            kwargs["stdout"].write(json.dumps({
                "implementation": "CPython", "version": [3, 13, 1 if Path(argv[0]) == old_python else 12],
                "abi": "", "soabi": "cpython-313-test", "cache_tag": "cpython-313",
                "platform": "linux-x86_64", "machine": "x86_64", "multiarch": "x86_64-linux-gnu",
                "byteorder": "little", "pointer_bits": 64,
            }).encode())
            return Process()
        with mock.patch.object(postprocessing.subprocess, "Popen", side_effect=probe):
            store, req, location = self.seed(interpreter=old_python)
            original = self.snapshot(location)
            self.upgrade(store, req, interpreter=new_python)
            self.assertTrue(store.status(IDENT, interpreter=new_python)["processor"]["ready_to_run"])
            self.assertEqual(self.snapshot(location), original)

    def test_approved_legacy_migration_can_coincide_with_source_upgrade(self):
        store, req, location = self.seed()
        old_libraries = (location / "site-packages/library.py").read_bytes()
        with mock.patch.object(postprocessing, "environment_fingerprint", return_value="f" * 64), mock.patch.object(
                postprocessing, "_legacy_environment_compatible", return_value=True):
            self.upgrade(store, req)
            status = store.status(IDENT)
            self.assertTrue(status["processor"]["ready_to_run"])
            self.assertEqual(status["environment"]["fingerprint"], "f" * 64)
            moved = Path(status["environment"]["path"])
            self.assertEqual((moved / "site-packages/library.py").read_bytes(), old_libraries)
            self.assertEqual((moved / "site-packages" / advanced_model.MODEL_NAME).read_bytes(), MODEL)
            self.assertFalse(location.exists())

    def test_custom_processor_cannot_receive_bundled_trust(self):
        store = postprocessing.ProcessorStore(self.root)
        item = store.save("Custom", SOURCE, [])
        before = store._metadata(item["id"])
        with self.assertRaises(postprocessing.ConflictError):
            store.provision_bundled(item["id"], NAME, SOURCE, requirements=["demo==1.0"],
                                    optional_model=True, environment_verifier=server._verify_advanced_environment_for_reuse)
        self.assertEqual(store._metadata(item["id"]), before)

    def test_failed_metadata_publication_keeps_previous_installation_usable(self):
        store, req, location = self.seed()
        before = store._metadata(IDENT)
        original = self.snapshot(location)
        atomic_json = postprocessing._atomic_json
        def fail_metadata(path, value):
            if Path(path) == store._processor(IDENT) / "metadata.json":
                raise OSError("injected publication failure")
            atomic_json(path, value)
        with mock.patch.object(postprocessing, "_atomic_json", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.upgrade(store, req)
        self.assertEqual(store._metadata(IDENT), before)
        self.assertEqual(self.snapshot(location), original)
        self.assertTrue(store.status(IDENT)["processor"]["ready_to_run"])
        self.assertEqual(store.get(IDENT)["source"], SOURCE)

    def test_metadata_change_during_verification_is_not_overwritten(self):
        store, req, _ = self.seed()
        def changed(environment):
            server._verify_advanced_environment_for_reuse(environment)
            metadata = store._metadata(IDENT)
            metadata["updated"] = -1
            postprocessing._atomic_json(store._processor(IDENT) / "metadata.json", metadata)
        with self.assertRaises(postprocessing.ConflictError):
            store.provision_bundled(IDENT, NAME, SOURCE + "\n# update", requirements=req,
                                    optional_model=True, environment_verifier=changed)
        self.assertEqual(store._metadata(IDENT)["updated"], -1)
        self.assertEqual(store.get(IDENT)["source"], SOURCE)
