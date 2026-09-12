"""Missing-image evidence is classified once and survives job refreshes."""

import json
import tempfile
import unittest
from pathlib import Path

from scm_workbench import server


ERROR_404 = "Error fetching abc: 404 Client Error: Not Found for url: https://example.test/image"
NO_DATA = "Warning: No image data for slot 1 (Treasure Vault)"


class FetchImageWarningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fetch-image-warnings-")
        self.old = {name: getattr(server, name) for name in ("DATA_DIR", "JOBS_FILE", "LOGS_DIR")}
        server.DATA_DIR = Path(self.temp.name)
        server.JOBS_FILE = server.DATA_DIR / "jobs.json"
        server.LOGS_DIR = server.DATA_DIR / "logs"
        server.JOBS.clear()

    def tearDown(self):
        server.JOBS.clear()
        for name, value in self.old.items():
            setattr(server, name, value)
        self.temp.cleanup()

    def job(self, kind="fetch:mtg"):
        return {"id": "job", "ts": 1.0, "kind": kind, "title": "Fetch card art",
                "status": "running", "exit_code": None, "cmd": "fetch", "args": {},
                "log_lines": [], "subs": []}

    def test_two_prefetch_404s_latch_the_caution(self):
        job = self.job()
        server._append_job_line(job, "Prefetching 3 images with 2 workers...")
        server._append_job_line(job, ERROR_404)
        self.assertNotIn("image_warnings", job)
        server._append_job_line(job, ERROR_404)
        self.assertEqual(job["image_warnings"], {"multiple_404s": True})
        server._append_job_line(job, ERROR_404)
        self.assertEqual(job["_fetch_image_warning_state"]["not_found"], 2)

    def test_404s_after_prefetch_do_not_create_the_stage_one_caution(self):
        job = self.job()
        server._append_job_line(job, "Prefetch complete.")
        server._append_job_line(job, ERROR_404)
        server._append_job_line(job, ERROR_404)
        self.assertNotIn("image_warnings", job)

    def test_match_is_narrow_and_fetch_only(self):
        for line in ("Error fetching abc: 500 Server Error", "Card 404 is missing",
                     "Error fetching https://example.test/404: timed out"):
            job = self.job()
            server._append_job_line(job, line)
            server._append_job_line(job, line)
            self.assertNotIn("image_warnings", job)
        job = self.job("create_pdf")
        server._append_job_line(job, ERROR_404)
        server._append_job_line(job, ERROR_404)
        server._append_job_line(job, NO_DATA)
        self.assertNotIn("image_warnings", job)

    def test_no_data_warning_latches_independently(self):
        job = self.job()
        server._append_job_line(job, NO_DATA)
        self.assertEqual(job["image_warnings"], {"missing_data": True})
        server._append_job_line(job, ERROR_404)
        server._append_job_line(job, ERROR_404)
        self.assertEqual(job["image_warnings"], {"missing_data": True, "multiple_404s": True})

    def test_flags_are_listed_and_persisted(self):
        job = self.job()
        job.update(status="ok", exit_code=0, duration=1.2)
        server._append_job_line(job, ERROR_404)
        server._append_job_line(job, ERROR_404)
        server._append_job_line(job, NO_DATA)
        server.JOBS[job["id"]] = job
        listed = server.list_jobs()["jobs"][0]
        self.assertEqual(listed["image_warnings"], {"multiple_404s": True, "missing_data": True})
        self.assertTrue(server._persist_jobs(strict=True))
        stored = json.loads(server.JOBS_FILE.read_text(encoding="utf-8"))[0]
        self.assertEqual(stored["image_warnings"], {"multiple_404s": True, "missing_data": True})
        self.assertNotIn("_fetch_image_warning_state", stored)


if __name__ == "__main__":
    unittest.main()
