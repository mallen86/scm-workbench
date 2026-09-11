"""Which user files survive a managed-repo operation, and which do not.

The app can end up owning files in the managed copy that are not upstream's:
card art and decklists it fetches, PDFs it prints, offsets it projects, and the
custom cutting templates it *generates*. Two operations handle them very
differently:

  * update    rebuilds the live tree from a clone of itself, so a file the
              manifest does not mention cannot be removed by it at all. A
              manifest entry is what lets it delete a path upstream dropped.
  * re-deploy builds a new tree from the downloaded archive and copies back
              only the documented user slots plus tracked files whose content
              differs from the manifest. This is the path bootstrap takes on a
              packaged launch when the deployed tree has drifted from its
              recorded state.

The rule these hold to is the module's own promise: an operation replaces what
upstream provides and never removes anything else.
"""

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scm_workbench import repo_sync


SHA1 = "a" * 40
SHA2 = "b" * 40
SHA3 = "c" * 40
# The app's own generated-template naming, from build_command.
CUSTOM_TEMPLATE = "cutting_templates/dxf/legal-standard-v1.dxf"
CALIBRATION = "calibration/letter-calibration.pdf"
BORDERLESS_TEMPLATE = "cutting_templates/borderless/dxf/legal-standard-borderless-v1.dxf"
EXTRAS_TEMPLATE = "cutting_templates/dxf/a3-poker-v1.dxf"


class UserFileSurvivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="user-file-survival-")
        self.data = Path(self.temp.name) / "data"
        self.env = patch.dict(os.environ, {"SCM_WORKBENCH_DATA": str(self.data)}, clear=False)
        self.env.start()
        self.counter = 0

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def target(sha):
        return {"sha": sha, "ref": "main", "date": "2026-01-01"}

    def make_tar(self, files):
        wrapper = "owner-repo-" + SHA1
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tf:
            root = tarfile.TarInfo(wrapper + "/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tf.addfile(root)
            for rel, content in sorted(files.items()):
                info = tarfile.TarInfo(wrapper + "/" + rel)
                info.mode = 0o644
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return out.getvalue()

    def archive(self, files):
        self.counter += 1
        path = Path(self.temp.name) / f"snap-{self.counter}.tar.gz"
        path.write_bytes(self.make_tar(files))
        return path

    def deploy(self, files, *, redeploy=False):
        """The first deploy, or the forced re-deploy bootstrap uses on drift."""
        archive = self.archive(files)
        repo_sync.set_source("scm", "main")
        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA1)):
            result = repo_sync.cmd_init("scm", tarball=archive, log=lambda *_: None,
                                        force_redeploy=redeploy)
        self.assertTrue(result["ok"], result)
        return result

    def update_full(self, files, sha=SHA3):
        """A full-tarball update: new tree from the archive, applied onto the clone.

        Each update gets its own target SHA: a second update aimed at the commit
        already deployed is a deliberate no-op, which would leave the assertions
        below testing nothing.
        """
        archive = self.archive(files)
        with patch.object(repo_sync, "resolve_target", return_value=self.target(sha)), \
                patch.object(repo_sync, "gh_download_to",
                             side_effect=lambda url, dest, **kw: (
                                 Path(dest).write_bytes(archive.read_bytes())
                                 or archive.stat().st_size)):
            result = repo_sync.cmd_update("scm", force_full=True, log=lambda *_: None)
        self.assertTrue(result["ok"], result)
        return result

    def update_diff(self, changed):
        """A per-file update: only `changed` is fetched and re-applied."""
        diff = {"status": "ahead", "too_many": False,
                "files": [{"path": rel, "status": "modified", "previous": None}
                          for rel in changed]}

        def download_to(key, sha, path, dest, log):
            Path(dest).write_bytes(changed[path])
            return len(changed[path])

        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA2)), \
                patch.object(repo_sync, "compare", return_value=diff), \
                patch.object(repo_sync, "download_to", side_effect=download_to):
            result = repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertTrue(result["ok"], result)
        return result

    def repo(self):
        return repo_sync.repo_dir("scm")

    def write(self, rel, content=b"mine"):
        path = self.repo() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def assert_survived(self, rel, label):
        path = self.repo() / rel
        self.assertTrue(path.exists(), f"{label} deleted {rel}")

    # ---- the documented user slots ---------------------------------------

    BASE = {"README.md": b"r", "game/front/README.md": b"p",
            "game/decklist/README.md": b"p", "game/output/README.md": b"p"}

    def seed_documented_slots(self):
        self.deploy(dict(self.BASE))
        self.write("game/front/card.png", b"art")
        self.write("game/decklist/deck.xml", b"<deck/>")
        self.write("game/output/printed.pdf", b"%PDF")

    def test_documented_user_slots_survive_a_diff_update(self):
        self.seed_documented_slots()
        self.update_diff({"README.md": b"r2"})
        for rel in ("game/front/card.png", "game/decklist/deck.xml", "game/output/printed.pdf"):
            self.assert_survived(rel, "a diff update")

    def test_documented_user_slots_survive_a_full_update(self):
        self.seed_documented_slots()
        self.update_full({"README.md": b"r2", "new.txt": b"n"})
        for rel in ("game/front/card.png", "game/decklist/deck.xml", "game/output/printed.pdf"):
            self.assert_survived(rel, "a full update")

    def test_documented_user_slots_survive_a_re_deploy(self):
        self.seed_documented_slots()
        self.deploy(dict(self.BASE), redeploy=True)
        for rel in ("game/front/card.png", "game/decklist/deck.xml", "game/output/printed.pdf"):
            self.assert_survived(rel, "a re-deploy")

    def test_upstream_tracked_files_still_update(self):
        # The protection must not freeze the tree: an upstream change has to
        # land, or every file under a user slot would stop updating.
        self.deploy({"README.md": b"r", "calibration/letter-calibration.pdf": b"v1"})
        self.update_full({"README.md": b"r2", "calibration/letter-calibration.pdf": b"v2"})
        self.assertEqual((self.repo() / "README.md").read_bytes(), b"r2")
        self.assertEqual((self.repo() / CALIBRATION).read_bytes(), b"v2")

    # ---- files the manifest has never seen -------------------------------

    def test_an_unrecorded_user_file_survives_both_update_modes(self):
        self.deploy({"README.md": b"r"})
        self.write("notes.txt", b"mine")
        self.write(CUSTOM_TEMPLATE, b"custom dxf")
        self.update_diff({"README.md": b"r2"})
        self.assert_survived("notes.txt", "a diff update")
        self.assert_survived(CUSTOM_TEMPLATE, "a diff update")
        self.update_full({"README.md": b"r3"})
        self.assert_survived("notes.txt", "a full update")
        self.assert_survived(CUSTOM_TEMPLATE, "a full update")

    # ---- the re-deploy path ----------------------------------------------

    def test_generated_cutting_templates_survive_a_re_deploy(self):
        """The app writes these into the repo, so they are user data.

        Both folders matter: a borderless template lands in
        cutting_templates/borderless/dxf, a default one in cutting_templates/dxf.
        """
        base = {"README.md": b"r", "cutting_templates/dxf/README.md": b"p",
                "cutting_templates/borderless/dxf/README.md": b"p"}
        self.deploy(dict(base))
        self.write(CUSTOM_TEMPLATE, b"custom dxf")
        self.write(BORDERLESS_TEMPLATE, b"custom borderless dxf")
        self.write(EXTRAS_TEMPLATE, b"another custom dxf")
        self.deploy(dict(base), redeploy=True)
        self.assert_survived(CUSTOM_TEMPLATE, "a re-deploy")
        self.assert_survived(BORDERLESS_TEMPLATE, "a re-deploy")
        self.assert_survived(EXTRAS_TEMPLATE, "a re-deploy")

    def test_generated_cutting_templates_survive_both_update_modes(self):
        """A recorded template must not be treated as an upstream deletion.

        A re-deploy records every file it publishes, the templates included.
        A later full update removes manifest paths the new archive lacks, which
        is how an upstream deletion takes effect, so a template has to be
        exempt from that or the update deletes the user's work.
        """
        base = {"README.md": b"r", "cutting_templates/dxf/README.md": b"p",
                "cutting_templates/borderless/dxf/README.md": b"p"}
        self.deploy(dict(base))
        self.write(CUSTOM_TEMPLATE, b"custom dxf")
        self.write(BORDERLESS_TEMPLATE, b"custom borderless dxf")
        self.deploy(dict(base), redeploy=True)      # records them in the manifest
        for rel in (CUSTOM_TEMPLATE, BORDERLESS_TEMPLATE):
            self.assert_survived(rel, "a re-deploy (before the updates)")
        self.update_diff({"README.md": b"r2"})
        for rel in (CUSTOM_TEMPLATE, BORDERLESS_TEMPLATE):
            self.assert_survived(rel, "a diff update")
        self.update_full({"README.md": b"r3"})
        for rel in (CUSTOM_TEMPLATE, BORDERLESS_TEMPLATE):
            self.assert_survived(rel, "a full update")

    def test_user_files_outside_the_documented_slots_survive_a_re_deploy(self):
        """Whatever nobody thought to list is still the user's."""
        self.deploy({"README.md": b"r"})
        self.write("notes.txt", b"mine")
        self.write("game/game-level.txt", b"also mine")
        self.write("calibration/mine-calibration.pdf", b"%PDF mine")
        self.deploy({"README.md": b"r"}, redeploy=True)
        self.assert_survived("notes.txt", "a re-deploy")
        self.assert_survived("game/game-level.txt", "a re-deploy")
        self.assert_survived("calibration/mine-calibration.pdf", "a re-deploy")

    def test_a_re_deploy_still_applies_upstream_changes(self):
        """Preserving local files must not freeze upstream's own content."""
        base = {"README.md": b"v1", "cutting_templates/dxf/README.md": b"p"}
        self.deploy(dict(base))
        self.write(CUSTOM_TEMPLATE, b"custom dxf")
        self.write("notes.txt", b"mine")
        self.deploy({"README.md": b"v2", "added-by-upstream.txt": b"new"}, redeploy=True)
        self.assertEqual((self.repo() / "README.md").read_bytes(), b"v2")
        self.assertEqual((self.repo() / "added-by-upstream.txt").read_bytes(), b"new")
        self.assert_survived(CUSTOM_TEMPLATE, "a re-deploy")
        self.assert_survived("notes.txt", "a re-deploy")

    # ---- the documented diff -> full fallback ----------------------------

    def test_a_failed_compare_falls_back_to_the_full_tarball(self):
        """A failed comparison must still complete the update.

        compare() raises RepoError for a rate limit, a 404, or a diff past the
        file cap. The handler has to switch modes, or the diff branch runs
        against a comparison that was never made.
        """
        self.deploy({"README.md": b"r"})
        archive = self.archive({"README.md": b"r2"})
        with patch.object(repo_sync, "resolve_target", return_value=self.target(SHA2)), \
                patch.object(repo_sync, "compare",
                             side_effect=repo_sync.RepoError("compare API returned too many files")), \
                patch.object(repo_sync, "gh_download_to",
                             side_effect=lambda url, dest, **kw: (
                                 Path(dest).write_bytes(archive.read_bytes())
                                 or archive.stat().st_size)):
            result = repo_sync.cmd_update("scm", log=lambda *_: None)
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.repo() / "README.md").read_bytes(), b"r2")


if __name__ == "__main__":
    unittest.main()
