"""Transactional deployment tests using local tarballs and mocked network seams."""

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scm_workbench import repo_sync


SHA1 = "a" * 40
SHA2 = "b" * 40
SHA3 = "c" * 40


def digest(data):
    return hashlib.sha256(data).hexdigest()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="repo-sync-deployment-")
        self.data = Path(self.temp.name) / "data"
        self.env = patch.dict(os.environ, {"SCM_WORKBENCH_DATA": str(self.data)}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def target(sha=SHA1, ref="main"):
        return {"sha": sha, "ref": ref, "date": "2026-01-01"}

    def make_tar(self, files, wrapper="owner-repo-" + SHA1):
        """Make the same flat wrapper used by GitHub's tarball endpoint."""
        out = io.BytesIO()
        dirs = {wrapper + "/"}
        for rel in files:
            parts = rel.split("/")
            dirs.update(wrapper + "/" + "/".join(parts[:i]) + "/"
                        for i in range(1, len(parts)))
        with tarfile.open(fileobj=out, mode="w:gz") as tf:
            root = tarfile.TarInfo(wrapper + "/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tf.addfile(root)
            for name in sorted(dirs - {wrapper + "/"}):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            for rel, content in sorted(files.items()):
                info = tarfile.TarInfo(wrapper + "/" + rel)
                info.mode = 0o644
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return out.getvalue()

    def tar_path(self, files, name="snapshot.tar.gz"):
        path = Path(self.temp.name) / name
        path.write_bytes(self.make_tar(files))
        return path

    def init_repo(self, files, target=None):
        target = target or self.target()
        repo_sync.set_source("scm", "main")
        archive = self.tar_path(files)
        with patch.object(repo_sync, "resolve_target", return_value=target):
            result = repo_sync.cmd_init("scm", tarball=archive, log=lambda *_: None)
        self.assertTrue(result["ok"])
        return target

    @staticmethod
    def tree_bytes(root):
        root = Path(root)
        if not root.exists():
            return None
        result = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[rel] = ("symlink", os.readlink(path))
            elif path.is_file():
                result[rel] = ("file", path.read_bytes())
            elif path.is_dir():
                result[rel] = ("dir", None)
        return result

    def metadata_bytes(self):
        def raw(path):
            return path.read_bytes() if path.exists() else None
        return raw(repo_sync.state_file()), raw(repo_sync.manifest_file("scm"))

    def assert_no_transaction_artifacts(self):
        self.assertEqual(list(self.data.glob(".repos-journal-*.json")), [])
        self.assertEqual(list(self.data.glob(".repos-txn-*")), [])
        repos_parent = repo_sync.repo_dir("scm").parent
        self.assertEqual(list(repos_parent.glob(".repos-candidate-*")), [])
        self.assertEqual(list(repos_parent.glob(".repos-backup-*")), [])

    def test_init_publishes_candidate_and_metadata_atomically(self):
        files = {"README.md": b"readme", "game/front/card.txt": b"card"}
        target = self.init_repo(files)
        repo = repo_sync.repo_dir("scm")
        self.assertEqual(self.tree_bytes(repo)["game/front/card.txt"], ("file", b"card"))
        manifest = json.loads(repo_sync.manifest_file("scm").read_text())
        self.assertEqual(manifest["sha"], target["sha"])
        self.assertEqual(manifest["files"], {p: digest(v) for p, v in files.items()})
        state = repo_sync.load_state()["scm"]
        self.assertEqual(state["deployed"]["sha"], target["sha"])
        self.assertEqual(state["source"], "main")
        self.assertTrue(repo_sync.verify_deployed("scm"))
        self.assert_no_transaction_artifacts()

    def test_diff_update_handles_add_modify_delete_rename_and_preserves_users(self):
        old = {
            "README.md": b"readme", "added-later.txt": b"old add",
            "modified.txt": b"old", "gone.txt": b"remove me",
            "old-name.txt": b"rename me", "edited.txt": b"upstream old",
            "data/README.md": b"placeholder",
        }
        self.init_repo(old)
        repo = repo_sync.repo_dir("scm")
        (repo / "data/user.bin").write_bytes(b"user bytes")
        (repo / "untracked.bin").write_bytes(b"untracked bytes")
        (repo / "edited.txt").write_bytes(b"local edit")
        target = self.target(SHA2, "main")
        changed = {"added-later.txt": b"new file", "modified.txt": b"new",
                   "new-name.txt": b"rename me", "edited.txt": b"upstream new"}
        compare = {"status": "ahead", "too_many": False, "commits": 1,
                   "files": [
                       {"path": "added-later.txt", "status": "modified", "previous": None},
                       {"path": "modified.txt", "status": "modified", "previous": None},
                       {"path": "gone.txt", "status": "removed", "previous": None},
                       {"path": "new-name.txt", "status": "renamed", "previous": "old-name.txt"},
                       {"path": "edited.txt", "status": "modified", "previous": None},
                   ]}

        def fetch(_key, _sha, path, dest, log=print):
            repo_sync._secure_write_bytes(dest, changed[path])
            return len(changed[path])

        with patch.object(repo_sync, "resolve_target", return_value=target), \
                patch.object(repo_sync, "compare", return_value=compare), \
                patch.object(repo_sync, "download_to", side_effect=fetch):
            result = repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertEqual(result["applied"], 3)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["conflicts"], ["edited.txt"])
        self.assertEqual((repo / "added-later.txt").read_bytes(), b"new file")
        self.assertEqual((repo / "modified.txt").read_bytes(), b"new")
        self.assertFalse((repo / "gone.txt").exists())
        self.assertFalse((repo / "old-name.txt").exists())
        self.assertEqual((repo / "new-name.txt").read_bytes(), b"rename me")
        self.assertEqual((repo / "edited.txt").read_bytes(), b"local edit")
        self.assertEqual((repo / "data/user.bin").read_bytes(), b"user bytes")
        self.assertEqual((repo / "untracked.bin").read_bytes(), b"untracked bytes")
        self.assert_no_transaction_artifacts()

    def test_full_update_keeps_authorized_and_untracked_files(self):
        old = {"tracked.txt": b"old", "data/README.md": b"placeholder"}
        self.init_repo(old)
        repo = repo_sync.repo_dir("scm")
        (repo / "data/deck.json").write_bytes(b"deck")
        (repo / "game/front").mkdir(parents=True, exist_ok=True)
        (repo / "game/front/user.svg").write_bytes(b"art")
        (repo / "random.local").write_bytes(b"do not discard")
        replacement = {"tracked.txt": b"new", "new.txt": b"new file",
                       "data/README.md": b"placeholder"}
        target = self.target(SHA2)
        archive = self.tar_path(replacement, "replacement.tar.gz")
        with patch.object(repo_sync, "resolve_target", return_value=target), \
                patch.object(repo_sync, "gh_download_to",
                             side_effect=lambda url, dest, **kw: (Path(dest).write_bytes(archive.read_bytes()) or archive.stat().st_size)):
            result = repo_sync.cmd_update("scm", force_full=True, log=lambda *_: None)
        self.assertEqual(result["applied"], 2)
        self.assertEqual((repo / "tracked.txt").read_bytes(), b"new")
        self.assertEqual((repo / "new.txt").read_bytes(), b"new file")
        self.assertEqual((repo / "data/deck.json").read_bytes(), b"deck")
        self.assertEqual((repo / "game/front/user.svg").read_bytes(), b"art")
        self.assertEqual((repo / "random.local").read_bytes(), b"do not discard")
        self.assert_no_transaction_artifacts()

    def update_failure_fixture(self):
        self.init_repo({"tracked.txt": b"old", "data/user.bin": b"user"})
        repo = repo_sync.repo_dir("scm")
        (repo / "local.txt").write_bytes(b"local")
        return self.tree_bytes(repo), self.metadata_bytes(), self.target(SHA2)

    def run_failed_update(self, injector):
        before_tree, before_meta, target = self.update_failure_fixture()
        with patch.object(repo_sync, "resolve_target", return_value=target):
            injector()
        repo = repo_sync.repo_dir("scm")
        self.assertEqual(self.tree_bytes(repo), before_tree)
        self.assertEqual(self.metadata_bytes(), before_meta)
        self.assert_no_transaction_artifacts()

    def basic_compare(self):
        return {"status": "ahead", "too_many": False, "commits": 1,
                "files": [{"path": "tracked.txt", "status": "modified", "previous": None}]}

    def test_failures_before_and_during_publication_restore_exact_previous_state(self):
        def extraction():
            with patch.object(repo_sync, "gh_download_to", return_value=0), \
                    patch.object(repo_sync, "extract_tarball", side_effect=repo_sync.RepoError("extract")), \
                    self.assertRaises(repo_sync.RepoError):
                repo_sync.cmd_update("scm", force_full=True, log=lambda *_: None)

        def clone():
            with patch.object(repo_sync, "_clone_tree", side_effect=repo_sync.RepoError("clone")), \
                    self.assertRaises(repo_sync.RepoError):
                repo_sync.cmd_update("scm", log=lambda *_: None)

        def apply():
            with patch.object(repo_sync, "_clone_tree", wraps=repo_sync._clone_tree), \
                    patch.object(repo_sync, "compare", return_value=self.basic_compare()), \
                    patch.object(repo_sync, "download_to", return_value=0), \
                    patch.object(repo_sync, "apply_changes", side_effect=repo_sync.RepoError("apply")), \
                    self.assertRaises(repo_sync.RepoError):
                repo_sync.cmd_update("scm", log=lambda *_: None)

        for injector in (extraction, clone, apply):
            self.run_failed_update(injector)

    def test_fingerprint_and_each_publish_failure_roll_back(self):
        # Fingerprinting is before publication and must leave a pre-existing
        # forced-redeploy tree untouched.
        self.init_repo({"tracked.txt": b"old"})
        repo = repo_sync.repo_dir("scm")
        before_tree, before_meta = self.tree_bytes(repo), self.metadata_bytes()
        archive = self.tar_path({"tracked.txt": b"new"}, "fingerprint.tar.gz")
        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA2)), \
                patch.object(repo_sync, "_fingerprint_tree", side_effect=repo_sync.RepoError("fingerprint")), \
                self.assertRaises(repo_sync.RepoError):
            repo_sync.cmd_init("scm", tarball=archive, force_redeploy=True, log=lambda *_: None)
        self.assertEqual(self.tree_bytes(repo), before_tree)
        self.assertEqual(self.metadata_bytes(), before_meta)
        self.assert_no_transaction_artifacts()

        failures = ("backup", "publish", "manifest", "state")
        for failure in failures:
            before_tree, before_meta, target = self.update_failure_fixture()
            captured = {}

            def build(*args, **kwargs):
                tx = original_build(*args, **kwargs)
                captured["tx"] = tx
                return tx

            original_build = repo_sync._build_tx
            with patch.object(repo_sync, "_build_tx", side_effect=build), \
                    patch.object(repo_sync, "compare", return_value=self.basic_compare()), \
                    patch.object(repo_sync, "download_to", side_effect=lambda _k, _s, _p, d, log=print: (repo_sync._secure_write_bytes(d, b"new"), 3)[1]), \
                    patch.object(repo_sync, "resolve_target", return_value=target):
                if failure in ("backup", "publish"):
                    rename_calls = [0]
                    if os.name == "nt":
                        original_rename = repo_sync._windows_rename_sibling

                        def rename(parent, src, dst):
                            if failure == "backup" and dst == captured["tx"]["backup"].name:
                                raise repo_sync.RepoError("backup rename")
                            if (failure == "publish" and dst == captured["tx"]["repo"].name
                                    and not rename_calls[0]):
                                rename_calls[0] += 1
                                raise repo_sync.RepoError("publish rename")
                            return original_rename(parent, src, dst)

                        context = patch.object(
                            repo_sync, "_windows_rename_sibling", side_effect=rename)
                    else:
                        original_rename = repo_sync.os.rename

                        def rename(src, dst):
                            if failure == "backup" and Path(dst) == captured["tx"]["backup"]:
                                raise OSError("backup rename")
                            if (failure == "publish" and Path(dst) == captured["tx"]["repo"]
                                    and not rename_calls[0]):
                                rename_calls[0] += 1
                                raise OSError("publish rename")
                            return original_rename(src, dst)

                        context = patch.object(repo_sync.os, "rename", side_effect=rename)
                elif failure == "manifest":
                    context = patch.object(repo_sync, "save_manifest", side_effect=repo_sync.RepoError("manifest"))
                else:
                    context = patch.object(repo_sync, "save_state", side_effect=repo_sync.RepoError("state"))
                with context, self.assertRaises(repo_sync.RepoError):
                    repo_sync.cmd_update("scm", log=lambda *_: None)
            self.assertEqual(self.tree_bytes(repo_sync.repo_dir("scm")), before_tree, failure)
            self.assertEqual(self.metadata_bytes(), before_meta, failure)
            self.assert_no_transaction_artifacts()

    def test_source_changes_before_publication_are_stale_and_rolled_back(self):
        before_tree, before_meta, target = self.update_failure_fixture()
        original_publish = repo_sync._publish_tx

        def stale(tx, *args):
            state = repo_sync.load_state()
            state["scm"]["source"] = "feature"
            repo_sync.save_state(state)
            return original_publish(tx, *args)

        with patch.object(repo_sync, "resolve_target", return_value=target), \
                patch.object(repo_sync, "_publish_tx", side_effect=stale), \
                patch.object(repo_sync, "compare", return_value=self.basic_compare()), \
                patch.object(repo_sync, "download_to", side_effect=lambda _k, _s, _p, d, log=print: (repo_sync._secure_write_bytes(d, b"new"), 3)[1]), \
                self.assertRaises(repo_sync.RepoError):
            repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertEqual(self.tree_bytes(repo_sync.repo_dir("scm")), before_tree)
        # The stale source change is fenced, but the concurrent source edit is
        # not overwritten by transaction cleanup; only the manifest remains
        # unchanged and the live tree is byte-identical.
        self.assertEqual(repo_sync._raw_metadata(repo_sync.manifest_file("scm")), before_meta[1])
        self.assertEqual(repo_sync.load_state()["scm"]["source"], "feature")
        self.assert_no_transaction_artifacts()

    def test_other_repo_state_edit_is_preserved_by_stale_fence(self):
        before_tree, before_meta, target = self.update_failure_fixture()
        original_publish = repo_sync._publish_tx

        def stale(tx, *args):
            state = repo_sync.load_state()
            state["extras"] = {"marker": "concurrent"}
            repo_sync.save_state(state)
            return original_publish(tx, *args)

        with patch.object(repo_sync, "resolve_target", return_value=target), \
                patch.object(repo_sync, "_publish_tx", side_effect=stale), \
                patch.object(repo_sync, "compare", return_value=self.basic_compare()), \
                patch.object(repo_sync, "download_to", side_effect=lambda _k, _s, _p, d, log=print: (repo_sync._secure_write_bytes(d, b"new"), 3)[1]), \
                self.assertRaises(repo_sync.RepoError):
            repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertEqual(self.tree_bytes(repo_sync.repo_dir("scm")), before_tree)
        self.assertEqual(repo_sync.load_state()["extras"]["marker"], "concurrent")
        self.assertEqual(repo_sync._raw_metadata(repo_sync.manifest_file("scm")), before_meta[1])
        self.assert_no_transaction_artifacts()

    def test_f016_windows_rename_uses_handle_safe_seam(self):
        parent = self.data / "rename-parent"
        parent.mkdir(parents=True)
        with patch.object(repo_sync, "_windows_rename_sibling") as windows, \
                patch.object(repo_sync, "safe_destination", side_effect=lambda p: p), \
                patch.object(repo_sync.os, "name", "nt"):
            repo_sync._secure_rename_sibling(str(parent), "src", "dst")
        args = windows.call_args.args
        self.assertEqual(str(args[0]), str(parent))
        self.assertEqual(args[1:], ("src", "dst"))

    def test_f017_forced_init_preserves_dynamic_tracked_edit(self):
        self.init_repo({"a.txt": b"upstream", "README.md": b"readme"})
        repo = repo_sync.repo_dir("scm")
        (repo / "README.md").write_bytes(b"local edit")
        self.assertFalse(repo_sync.verify_deployed("scm"))
        archive = self.tar_path({"a.txt": b"upstream", "README.md": b"readme"}, "redeploy.tar.gz")
        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA2)):
            repo_sync.cmd_init("scm", tarball=archive, force_redeploy=True, log=lambda *_: None)
        self.assertEqual((repo / "README.md").read_bytes(), b"local edit")
        manifest = repo_sync.load_manifest("scm")
        self.assertEqual(manifest["local_edits"], ["README.md"])
        self.assertTrue(repo_sync.verify_deployed("scm"))

    def test_f018_deleted_target_entry_keeps_recovery_journal(self):
        tx, _, _, _ = self._make_journal_fixture("live_published", "before")
        state = repo_sync.load_state()
        del state["scm"]
        repo_sync.save_state(state)
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._recover_locked("scm")
        self.assertTrue(tx["journal"].exists())

    def test_f019_missing_root_committed_recovery_requires_live_coherence(self):
        tx, _, _, _ = self._make_journal_fixture("state_committed", "after")
        shutil.rmtree(tx["root"])
        repo_sync._recover_locked("scm")
        self.assertFalse(tx["journal"].exists())
        self.assertTrue(repo_sync.repo_dir("scm").is_dir())
        tx, _, _, _ = self._make_journal_fixture("state_committed", "after")
        shutil.rmtree(tx["root"])
        shutil.rmtree(repo_sync.repo_dir("scm"))
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._recover_locked("scm")
        self.assertTrue(tx["journal"].exists())

    def test_f020_prepublication_recovery_accepts_only_before_metadata(self):
        tx, _, _, _ = self._make_journal_fixture("live_published", "after")
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._recover_locked("scm")
        self.assertTrue(tx["journal"].exists())

    def test_f021_secure_rename_walks_parent_and_rejects_symlink_parent(self):
        if os.name == "nt":
            self.skipTest("POSIX descriptor-relative seam")
        parent = self.data / "parent" / "nested"
        parent.mkdir(parents=True)
        (parent / "source").mkdir()
        repo_sync._secure_rename_sibling(parent, "source", "destination")
        self.assertTrue((parent / "destination").is_dir())
        link = self.data / "parent-link"
        link.symlink_to(parent.parent, target_is_directory=True)
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._secure_rename_sibling(link / "nested", "destination", "other")

    def test_missing_live_or_malformed_manifest_refuses_mutation(self):
        repo_sync.set_source("scm", "main")
        repo_sync.save_state({"scm": {"source": "main", "deployed": {"sha": SHA1}}})
        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA2)), \
                self.assertRaises(repo_sync.RepoError):
            repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertFalse(repo_sync.repo_dir("scm").exists())

        repo = repo_sync.repo_dir("scm")
        repo.mkdir(parents=True)
        (repo / "old.txt").write_bytes(b"old")
        manifest = repo_sync.manifest_file("scm")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(b"not json")
        before = manifest.read_bytes()
        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA2)), \
                self.assertRaises(repo_sync.RepoError):
            repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual((repo / "old.txt").read_bytes(), b"old")

    def _make_journal_fixture(self, phase, metadata_state="before", symlink=None):
        # Each simulated crash starts from an independent old deployment.
        repo = repo_sync.repo_dir("scm")
        if repo.exists():
            shutil.rmtree(repo)
        for path in (repo_sync.state_file(), repo_sync.manifest_file("scm"),
                     repo_sync.data_dir() / ".repos-journal-scm.json"):
            if path.exists():
                path.unlink()
        for path in repo_sync.data_dir().glob(".repos-txn-scm-*"):
            if path.exists():
                shutil.rmtree(path)
        repo_sync.save_state({"scm": {"source": "main", "deployed": {"sha": SHA1}}})
        repo = repo_sync.repo_dir("scm")
        repo.mkdir(parents=True)
        (repo / "old.txt").write_bytes(b"old")
        old_manifest = {"sha": SHA1, "ref": "main", "date": None,
                        "files": {"old.txt": digest(b"old")}}
        repo_sync.save_manifest("scm", old_manifest)
        old_state_raw, old_manifest_raw = self.metadata_bytes()
        target = self.target(SHA2)
        tx = repo_sync._build_tx("scm", repo_sync.load_state(), old_manifest,
                                 target, "main", ("state", "main"))
        new_state = {"scm": {"source": "main", "deployed": {"sha": SHA2}}}
        new_manifest = {"sha": SHA2, "ref": "main", "date": None,
                        "files": {"new.txt": digest(b"new")}}
        tx["state_after"] = repo_sync._tx_snapshot(tx, "state-after.bin",
                                                     repo_sync._json_payload(new_state))
        tx["manifest_after"] = repo_sync._tx_snapshot(tx, "manifest-after.bin",
                                                        repo_sync._json_payload(new_manifest))
        candidate = tx["candidate"]
        candidate.mkdir(parents=True)
        (candidate / "new.txt").write_bytes(b"new")
        if phase in {"backup_renamed", "live_published", "manifest_committed", "state_committed"}:
            os.rename(repo, tx["backup"])
        if phase in {"live_published", "manifest_committed", "state_committed"}:
            os.rename(candidate, repo)
        if metadata_state == "after":
            repo_sync._write_exact_metadata(repo_sync.state_file(), repo_sync._tx_read_snapshot(tx, tx["state_after"]))
            repo_sync._write_exact_metadata(repo_sync.manifest_file("scm"), repo_sync._tx_read_snapshot(tx, tx["manifest_after"]))
        elif metadata_state == "manifest-after":
            repo_sync._write_exact_metadata(repo_sync.manifest_file("scm"), repo_sync._tx_read_snapshot(tx, tx["manifest_after"]))
        if symlink:
            target_path = Path(self.temp.name) / "outside.txt"
            target_path.write_bytes(b"outside")
            victim = tx[symlink]
            if victim.exists():
                repo_sync._remove_tree(victim)
            victim.symlink_to(target_path, target_is_directory=True)
        repo_sync._journal_write(tx, phase)
        return tx, old_state_raw, old_manifest_raw, target_path if symlink else None

    def test_journal_phases_recover_to_coherent_metadata_and_remove_bounded_artifacts(self):
        for phase, expected_new in (("prepared", False), ("backup_renamed", False),
                                    ("live_published", False), ("manifest_committed", True),
                                    ("state_committed", True)):
            with self.subTest(phase=phase):
                tx, old_state, old_manifest, _ = self._make_journal_fixture(
                    phase, "after" if expected_new else "before")
                after_state = repo_sync._tx_read_snapshot(tx, tx["state_after"])
                after_manifest = repo_sync._tx_read_snapshot(tx, tx["manifest_after"])
                repo_sync._recover_locked("scm")
                repo = repo_sync.repo_dir("scm")
                self.assertEqual((repo / ("new.txt" if expected_new else "old.txt")).read_bytes(),
                                 b"new" if expected_new else b"old")
                self.assertEqual(repo_sync._raw_metadata(repo_sync.state_file()),
                                 after_state if expected_new else old_state)
                self.assertEqual(repo_sync._raw_metadata(repo_sync.manifest_file("scm")),
                                 after_manifest if expected_new else old_manifest)
                self.assert_no_transaction_artifacts()

    def test_journal_metadata_partial_commit_is_completed_or_rolled_back(self):
        tx, old_state, old_manifest, _ = self._make_journal_fixture("manifest_committed", "manifest-after")
        after_state = repo_sync._tx_read_snapshot(tx, tx["state_after"])
        after_manifest = repo_sync._tx_read_snapshot(tx, tx["manifest_after"])
        repo_sync._recover_locked("scm")
        self.assertEqual(repo_sync._raw_metadata(repo_sync.state_file()), after_state)
        self.assertEqual(repo_sync._raw_metadata(repo_sync.manifest_file("scm")), after_manifest)
        self.assertEqual((repo_sync.repo_dir("scm") / "new.txt").read_bytes(), b"new")
        self.assert_no_transaction_artifacts()

        tx, old_state, old_manifest, _ = self._make_journal_fixture("state_committed", "before")
        repo_sync._recover_locked("scm")
        self.assertEqual(repo_sync._raw_metadata(repo_sync.state_file()), old_state)
        self.assertEqual(repo_sync._raw_metadata(repo_sync.manifest_file("scm")), old_manifest)
        self.assertEqual((repo_sync.repo_dir("scm") / "old.txt").read_bytes(), b"old")
        self.assert_no_transaction_artifacts()

    def test_tampered_journal_and_generated_symlinks_fail_closed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        outside_file = outside / "sentinel"
        outside_file.write_bytes(b"do not touch")
        journal = repo_sync.data_dir() / ".repos-journal-scm.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        for field, value in (("txn", "../../outside"), ("candidate", "../../outside"),
                             ("backup", "../../outside"), ("key", "extras")):
            payload = {"version": 1, "key": "scm", "phase": "prepared",
                       "txn": ".repos-txn-scm-" + "a" * 32,
                       "candidate": ".repos-candidate-scm-" + "a" * 32,
                       "backup": ".repos-backup-scm-" + "a" * 32,
                       "state_before": {}, "manifest_before": {},
                       "state_after": {}, "manifest_after": {}}
            payload[field] = value
            journal.write_text(json.dumps(payload))
            with self.assertRaises(repo_sync.RepoError):
                repo_sync._recover_locked("scm")
            self.assertEqual(outside_file.read_bytes(), b"do not touch")
            journal.unlink()

        tx, _, _, _ = self._make_journal_fixture("prepared", symlink="candidate")
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._recover_locked("scm")
        self.assertEqual(outside_file.read_bytes(), b"do not touch")
        # A backup symlink is checked before any rename as well.
        if tx["root"].exists():
            repo_sync._remove_transaction(tx["root"])
        if tx["journal"].exists():
            tx["journal"].unlink()
        tx, _, _, _ = self._make_journal_fixture("backup_renamed", symlink="backup")
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._recover_locked("scm")
        self.assertEqual(outside_file.read_bytes(), b"do not touch")

    def test_transaction_tokens_are_unique_under_same_key_lock(self):
        tokens = []
        lock = threading.Lock()

        def worker():
            with repo_sync._repo_lock("scm"):
                tx = repo_sync._tx_paths("scm")
                with lock:
                    tokens.append(tx["token"])
                repo_sync._remove_transaction(tx["root"])

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertEqual(len(tokens), 12)

    def test_clone_bounds_and_special_files_are_rejected(self):
        source = Path(self.temp.name) / "source"
        source.mkdir()
        for index in range(3):
            (source / f"{index}.txt").write_bytes(b"x")
        dest = Path(self.temp.name) / "dest"
        with patch.object(repo_sync, "TREE_FILE_CAP", 2), self.assertRaises(repo_sync.RepoError):
            repo_sync._clone_tree(source, dest)
        self.assertFalse(dest.exists())

        with patch.object(repo_sync, "TREE_BYTES_CAP", 2), self.assertRaises(repo_sync.RepoError):
            repo_sync._validate_tree(source)
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_bytes(b"outside")
        (source / "link.txt").symlink_to(outside)
        with self.assertRaises(repo_sync.RepoError):
            repo_sync._clone_tree(source, Path(self.temp.name) / "symlink-dest")
        if hasattr(os, "mkfifo"):
            special = source / "fifo"
            os.mkfifo(special)
            with self.assertRaises(repo_sync.RepoError):
                repo_sync._validate_tree(source)

    def test_streamed_tar_download_cap_closes_response_and_cleans_destination(self):
        class Response:
            headers = {}
            def __init__(self):
                self.closed = False
                self.sent = False
            def read(self, size):
                if self.sent:
                    return b""
                self.sent = True
                return b"x" * (size + 1)
            def close(self):
                self.closed = True
            def geturl(self):
                return repo_sync.RAW + "/x"

        response = Response()
        destination = Path(self.temp.name) / "streamed.bin"
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=response), \
                self.assertRaises(repo_sync.RepoError):
            repo_sync.gh_download_to(repo_sync.RAW + "/x", destination, max_bytes=10)
        self.assertTrue(response.closed)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".streamed.bin.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
