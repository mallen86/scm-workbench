import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import bootstrap, repo_sync


class BootstrapTests(unittest.TestCase):
    def test_first_boot_records_partial_failure_without_success_message(self):
        repos = {
            "scm": {"name": "silhouette-card-maker", "owner": "owner", "repo": "scm"},
            "extras": {"name": "scm-extras", "owner": "owner", "repo": "extras"},
        }
        logs = []

        def init(key, log=print):
            if key == "extras":
                raise repo_sync.RepoError("download failed")
            return {"ok": True}

        with tempfile.TemporaryDirectory(prefix="workbench-bootstrap-") as temp, \
                mock.patch.object(repo_sync, "REPOS", repos), \
                mock.patch.object(repo_sync, "load_state", return_value={}), \
                mock.patch.object(repo_sync, "cmd_init", side_effect=init):
            data = Path(temp)
            bootstrap.run_first_boot(data, log=logs.append)
            state = json.loads((data / "bootstrap.json").read_text(encoding="utf-8"))

        self.assertEqual(state["pending"], [])
        self.assertEqual(state["done"], ["scm"])
        self.assertEqual(state["failed"], ["extras"])
        self.assertEqual(state["phase"], "setup incomplete")
        self.assertTrue(any("preparation incomplete" in line for line in logs))
        self.assertFalse(any("preparation finished" in line for line in logs))

    def test_success_is_published_before_completion_flag(self):
        repos = {
            "scm": {"name": "silhouette-card-maker", "owner": "owner", "repo": "scm"},
            "extras": {"name": "scm-extras", "owner": "owner", "repo": "extras"},
        }
        ready = []
        violations = []
        writes = []

        def write_flag(_data, pending, done, phase, failed=None):
            writes.append((list(pending), list(done), phase, list(failed or [])))
            violations.extend(key for key in done if key not in ready)

        with tempfile.TemporaryDirectory(prefix="workbench-bootstrap-") as temp, \
                mock.patch.object(repo_sync, "REPOS", repos), \
                mock.patch.object(bootstrap, "_bootstrap_one", return_value=True), \
                mock.patch.object(bootstrap, "_write_flag", side_effect=write_flag):
            bootstrap.run_first_boot(
                Path(temp), log=lambda *_: None, on_repo_ready=ready.append,
            )

        self.assertEqual(set(ready), set(repos))
        self.assertEqual(violations, [])
        self.assertEqual(set(writes[-1][1]), set(repos))
        self.assertEqual(writes[-1][0], [])

    def test_sequential_bootstrap_returns_results_by_repository(self):
        fake = mock.Mock()
        fake.REPOS = {
            "scm": {"name": "silhouette-card-maker", "owner": "owner", "repo": "scm"},
            "extras": {"name": "scm-extras", "owner": "owner", "repo": "extras"},
        }
        fake.load_state.return_value = {}
        fake.cmd_init.side_effect = [None, repo_sync.RepoError("offline")]

        results = bootstrap.bootstrap_managed_repos(fake, log=lambda *_: None)

        self.assertEqual(results, {"scm": True, "extras": False})


if __name__ == "__main__":
    unittest.main()
