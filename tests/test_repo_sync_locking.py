import json
import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scm_workbench import repo_sync


class RepoSyncLockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="repo-sync-lock-")
        self.env = patch.dict(os.environ, {"SCM_WORKBENCH_DATA": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_same_repo_serializes_and_different_repos_do_not(self):
        active = {"scm": 0, "extras": 0}
        peak = {"scm": 0, "extras": 0}
        mutex = threading.Lock()
        gate = threading.Barrier(4)

        def worker(key):
            gate.wait()
            with repo_sync._repo_lock(key):
                with mutex:
                    active[key] += 1
                    peak[key] = max(peak[key], active[key])
                time.sleep(0.04)
                with mutex:
                    active[key] -= 1

        threads = [threading.Thread(target=worker, args=(key,))
                   for key in ("scm", "scm", "extras", "extras")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(peak["scm"], 1)
        self.assertEqual(peak["extras"], 1)

    def test_lock_releases_after_exception(self):
        with self.assertRaises(RuntimeError):
            with repo_sync._repo_lock("scm"):
                raise RuntimeError("boom")
        with repo_sync._repo_lock("scm"):
            pass

    def test_malformed_metadata_is_not_replaced(self):
        state = repo_sync.state_file()
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(b"{not-json")
        before = state.read_bytes()
        with self.assertRaises(repo_sync.RepoError):
            repo_sync.set_source("scm", "main")
        self.assertEqual(state.read_bytes(), before)

        manifest = repo_sync.manifest_file("scm")
        manifest.write_bytes(b"[]")
        before = manifest.read_bytes()
        with self.assertRaises(repo_sync.RepoError):
            repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertEqual(manifest.read_bytes(), before)

    def test_check_discards_result_when_source_changes_during_resolution(self):
        repo_sync.save_state({"scm": {"source": "main"}})
        sha = "a" * 40
        calls = []

        def resolve(key, source):
            calls.append(source)
            if len(calls) == 1:
                repo_sync.save_state({"scm": {"source": "feature"}})
            return {"sha": sha, "ref": source, "date": None}

        with patch.object(repo_sync, "resolve_target", side_effect=resolve):
            result = repo_sync.check_repo("scm", force=True)
        self.assertEqual(calls, ["main", "feature"])
        self.assertEqual(result["target"]["ref"], "feature")
        self.assertEqual(repo_sync.load_state()["scm"]["source"], "feature")

    def test_state_source_precedes_invalid_settings_and_invalidates_cache(self):
        repo_sync.save_state({
            "scm": {
                "source": "main",
                "last_check": {"checked": {"target": {"sha": "a" * 40}},
                               "checked_at": time.time()},
            }
        })
        (Path(self.temp.name) / "settings.json").write_text(
            '{"repos": {"scm": {"source": "../invalid"}}}')
        source, signature = repo_sync._source_snapshot("scm", repo_sync.load_state())
        self.assertEqual(source, "main")
        self.assertEqual(signature, ("state", "main"))
        repo_sync.set_source("scm", "feature")
        self.assertNotIn("last_check", repo_sync.load_state()["scm"])

        with patch.object(repo_sync, "resolve_target",
                          return_value={"sha": "b" * 40, "ref": "feature", "date": None}):
            result = repo_sync.check_repo("scm")
        self.assertFalse(result["cached"])
        self.assertEqual(result["target"]["ref"], "feature")

    def test_settings_source_change_is_rechecked_without_state_authority(self):
        settings = Path(self.temp.name) / "settings.json"
        settings.write_text('{"repos": {"scm": {"source": "main"}}}')
        calls = []

        def resolve(key, source):
            calls.append(source)
            if len(calls) == 1:
                settings.write_text('{"repos": {"scm": {"source": "feature"}}}')
            return {"sha": "c" * 40, "ref": source, "date": None}

        with patch.object(repo_sync, "resolve_target", side_effect=resolve):
            result = repo_sync.check_repo("scm", force=True)
        self.assertEqual(calls, ["main", "feature"])
        self.assertEqual(result["target"]["ref"], "feature")

    def test_cache_source_binding_ignores_lower_settings_but_detects_settings_change(self):
        checked = {"repo": "scm", "ok": True, "cached": False,
                   "target": {"sha": "a" * 40}, "source": "main",
                   "up_to_date": False}
        repo_sync.save_state({"scm": {"source": "main",
                                       "last_check": {"checked": checked,
                                                      "checked_at": time.time()}}})
        # This lower-priority setting is both ignored and malformed.
        Path(self.temp.name, "settings.json").write_text(
            '{"repos": {"scm": {"source": "../ignored"}}}')
        with patch.object(repo_sync, "resolve_target", side_effect=AssertionError):
            result = repo_sync.check_repo("scm")
        self.assertTrue(result["cached"])
        self.assertEqual(result["source"], "main")

        # Remove state authority: the cached main result must not be used for
        # the new settings-backed source.
        Path(self.temp.name, "settings.json").write_text(
            '{"repos": {"scm": {"source": "feature"}}}')
        state = repo_sync.load_state()
        state["scm"].pop("source")
        repo_sync.save_state(state)
        with patch.object(repo_sync, "resolve_target",
                          return_value={"sha": "b" * 40, "ref": "feature", "date": None}) as resolve:
            result = repo_sync.check_repo("scm")
        self.assertFalse(result["cached"])
        resolve.assert_called_once_with("scm", "feature")

    def test_old_cache_without_source_is_a_miss(self):
        repo_sync.save_state({"scm": {"last_check": {
            "checked": {"target": {"sha": "a" * 40}},
            "checked_at": time.time()}}})
        with patch.object(repo_sync, "resolve_target",
                          return_value={"sha": "b" * 40, "ref": "main", "date": None}) as resolve:
            result = repo_sync.check_repo("scm")
        self.assertFalse(result["cached"])
        resolve.assert_called_once_with("scm", "latest-release")
        self.assertEqual(repo_sync.load_state()["scm"]["last_check"]["checked"]["source"],
                         "latest-release")

    def test_windows_lock_retries_only_contention(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = 0

            def locking(self, _fd, mode, _size):
                self.calls += 1
                if mode == self.LK_NBLCK and self.calls < 4:
                    raise OSError(errno.EACCES, "busy")

        fake = FakeMsvcrt()
        lock_path = Path(self.temp.name) / "windows.lock"
        with patch.object(repo_sync.sys, "platform", "win32"), \
                patch.dict(sys.modules, {"msvcrt": fake}), \
                patch.object(repo_sync.time, "sleep") as sleep:
            with repo_sync._file_lock(lock_path):
                pass
        self.assertEqual(fake.calls, 5)  # three retries, acquire, release
        self.assertEqual(sleep.call_count, 3)

        class Broken(FakeMsvcrt):
            def locking(self, _fd, _mode, _size):
                raise OSError(errno.EPERM, "not contention")

        with patch.object(repo_sync.sys, "platform", "win32"), \
                patch.dict(sys.modules, {"msvcrt": Broken()}):
            with self.assertRaises(repo_sync.RepoError):
                with repo_sync._file_lock(lock_path):
                    pass

    def test_cross_process_lock_serialization(self):
        script = """
import os, sys, time
from pathlib import Path
from scm_workbench import repo_sync
p = Path(os.environ['MARKER'])
with repo_sync._repo_lock('scm'):
    p.with_name('held').write_text('1')
    if os.environ['MODE'] == 'first':
        while not Path(os.environ['RELEASE']).exists():
            time.sleep(0.02)
    else:
        p.write_text('second')
"""
        marker = Path(self.temp.name) / "marker"
        release = Path(self.temp.name) / "release"
        env = os.environ.copy()
        env.update({"PYTHONPATH": os.getcwd(), "SCM_WORKBENCH_DATA": self.temp.name,
                    "MARKER": str(marker), "RELEASE": str(release)})
        env["MODE"] = "first"
        first = subprocess.Popen([sys.executable, "-c", script], env=env)
        try:
            deadline = time.time() + 3
            while not (Path(self.temp.name) / "held").exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue((Path(self.temp.name) / "held").exists())
            env["MODE"] = "second"
            second = subprocess.Popen([sys.executable, "-c", script], env=env)
            try:
                time.sleep(0.15)
                self.assertFalse(marker.exists())
                release.write_text("1")
                self.assertEqual(first.wait(3), 0)
                self.assertEqual(second.wait(3), 0)
                self.assertEqual(marker.read_text(), "second")
            finally:
                if second.poll() is None:
                    second.kill()
        finally:
            release.write_text("1")
            if first.poll() is None:
                first.kill()
                first.wait()

    def test_concurrent_state_writes_remain_json(self):
        def writer(value):
            repo_sync.save_state({"scm": {"marker": value}})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        parsed = json.loads(repo_sync.state_file().read_text())
        self.assertIsInstance(parsed, dict)
        self.assertIn("scm", parsed)

    def test_replace_failure_keeps_previous_bytes(self):
        path = Path(self.temp.name) / "metadata.json"
        path.write_bytes(b'{"old": true}')
        before = path.read_bytes()
        with patch.object(repo_sync.os, "replace", side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                repo_sync._atomic_write_json(path, {"new": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".metadata.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
