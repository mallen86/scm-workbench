"""A started fetch job records its decklist's slot count on the job row.

The strip reads the second stage's denominator from the job row, so the wiring
between the decklist and that field is what this covers: the job is started
with the process launch stubbed out, which keeps the test offline and leaves
nothing behind.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server


class FakeProcess:
    """Enough of a Popen for start_job to hand back a live-looking job."""

    pid = 4321
    returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class StartedFetchDeckTotalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fetch-deck-job-")
        self.data = Path(self.temp.name) / "data"
        self.repo = Path(self.temp.name) / "scm"
        (self.repo / "game" / "decklist").mkdir(parents=True)
        (self.repo / "plugins" / "mtg").mkdir(parents=True)
        (self.repo / "plugins" / "mtg" / "fetch.py").write_text("# fixture\n", encoding="utf-8")
        self.env = mock.patch.dict(
            os.environ,
            {"SCM_WORKBENCH_DATA": str(self.data), "SCM_WORKBENCH_SCM": str(self.repo)},
            clear=False,
        )
        self.env.start()
        self.old = {name: getattr(server, name) for name in ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "LOGS_DIR")}
        server.DATA_DIR = self.data
        server.SETTINGS_FILE = self.data / "settings.json"
        server.JOBS_FILE = self.data / "jobs.json"
        server.LOGS_DIR = self.data / "logs"
        self.data.mkdir(parents=True, exist_ok=True)
        server.JOBS.clear()

    def tearDown(self):
        server.JOBS.clear()
        for name, value in self.old.items():
            setattr(server, name, value)
        self.env.stop()
        self.temp.cleanup()

    def start(self, args):
        settings = {"scm_dir": str(self.repo), "extras_dir": str(self.repo)}
        with mock.patch.object(server, "load_settings", return_value=settings), \
                mock.patch.object(server, "subprocess") as proc:
            proc.Popen.return_value = FakeProcess()
            proc.PIPE = -1
            proc.STDOUT = -2
            proc.DEVNULL = -3
            job, errors = server.start_job("fetch:mtg", args)
        self.assertEqual(errors, [])
        self.assertIsNotNone(job)
        return job

    def test_a_declared_slot_count_lands_on_the_job_row(self):
        (self.repo / "game" / "decklist" / "defender.xml").write_text(
            '<?xml version="1.0"?><order><details><quantity>100</quantity></details>'
            '<fronts><card><id>A</id><name>A.png</name><slots>0</slots></card></fronts></order>',
            encoding="utf-8",
        )
        job = self.start({"deck_source": "file", "deck_file": "defender.xml", "format": "mpcfill_xml"})
        self.assertEqual(job["deck_total"], 100)
        # listed for the strip
        listed = next(row for row in server.list_jobs()["jobs"] if row["id"] == job["id"])
        self.assertEqual(listed["deck_total"], 100)

    def test_a_decklist_without_a_count_leaves_the_field_unset(self):
        (self.repo / "game" / "decklist" / "plain.txt").write_text("4 Lightning Bolt\n", encoding="utf-8")
        job = self.start({"deck_source": "file", "deck_file": "plain.txt", "format": "simple"})
        self.assertNotIn("deck_total", job)
        listed = next(row for row in server.list_jobs()["jobs"] if row["id"] == job["id"])
        self.assertNotIn("deck_total", listed)


if __name__ == "__main__":
    unittest.main()
