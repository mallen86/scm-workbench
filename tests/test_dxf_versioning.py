"""Generating a cutting template must never replace an existing one.

The generated name used to be fixed at '-v1', so running the same size twice
silently overwrote the first template. A template is the user's own work, so a
taken name takes the next version instead, which is also the naming convention
upstream uses.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import server


class DxfVersioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dxf-version-")
        self.repo = Path(self.temp.name)
        self.dxf = self.repo / "cutting_templates" / "dxf"
        self.borderless = self.repo / "cutting_templates" / "borderless" / "dxf"
        self.dxf.mkdir(parents=True)
        self.borderless.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def touch(self, rel, content=b"template"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def next_name(self, value):
        return server._dxf_output_without_overwriting(self.repo, value)

    # ---- the plain cases -------------------------------------------------

    def test_a_free_name_is_used_unchanged(self):
        self.assertEqual(self.next_name("cutting_templates/dxf/letter-standard-v1.dxf"),
                         "cutting_templates/dxf/letter-standard-v1.dxf")

    def test_taken_v1_becomes_v2(self):
        self.touch("cutting_templates/dxf/letter-standard-v1.dxf")
        self.assertEqual(self.next_name("cutting_templates/dxf/letter-standard-v1.dxf"),
                         "cutting_templates/dxf/letter-standard-v2.dxf")

    def test_the_next_free_version_is_chosen_not_just_the_last_plus_one(self):
        for version in (1, 2, 4):
            self.touch(f"cutting_templates/dxf/letter-standard-v{version}.dxf")
        # v3 is free, so it is used rather than skipping to v5.
        self.assertEqual(self.next_name("cutting_templates/dxf/letter-standard-v1.dxf"),
                         "cutting_templates/dxf/letter-standard-v3.dxf")

    def test_a_borderless_template_versions_in_its_own_folder(self):
        self.touch("cutting_templates/borderless/dxf/legal-standard-borderless-v1.dxf")
        self.assertEqual(
            self.next_name("cutting_templates/borderless/dxf/legal-standard-borderless-v1.dxf"),
            "cutting_templates/borderless/dxf/legal-standard-borderless-v2.dxf")
        # The default folder is untouched by a borderless collision.
        self.assertEqual(self.next_name("cutting_templates/dxf/legal-standard-v1.dxf"),
                         "cutting_templates/dxf/legal-standard-v1.dxf")

    def test_the_two_folders_do_not_share_a_counter(self):
        # Same paper and card, one default and one borderless: distinct names in
        # distinct folders, so neither bumps the other.
        self.touch("cutting_templates/dxf/a3-poker-v1.dxf")
        self.assertEqual(self.next_name("cutting_templates/borderless/dxf/a3-poker-borderless-v1.dxf"),
                         "cutting_templates/borderless/dxf/a3-poker-borderless-v1.dxf")

    # ---- an explicitly named output -------------------------------------

    def test_an_unversioned_explicit_name_counts_as_its_first_version(self):
        self.touch("my-template.dxf")
        self.assertEqual(self.next_name("my-template.dxf"), "my-template-v2.dxf")

    def test_a_free_unversioned_explicit_name_is_kept(self):
        self.assertEqual(self.next_name("my-template.dxf"), "my-template.dxf")

    def test_an_explicit_version_bumps_from_where_it_was_asked(self):
        self.touch("custom-v7.dxf")
        self.assertEqual(self.next_name("custom-v7.dxf"), "custom-v8.dxf")

    def test_an_absolute_path_stays_absolute(self):
        target = self.touch("cutting_templates/dxf/absolute-v1.dxf")
        self.assertEqual(self.next_name(str(target)), str(self.dxf / "absolute-v2.dxf"))

    def test_an_absolute_path_outside_the_repo_still_never_overwrites(self):
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        target = outside / "mine-v1.dxf"
        target.write_bytes(b"x")
        self.assertEqual(self.next_name(str(target)), str(outside / "mine-v2.dxf"))

    def test_a_dotted_name_keeps_its_extension(self):
        self.touch("cutting_templates/dxf/a3-70mm_square-v1.dxf")
        self.assertEqual(self.next_name("cutting_templates/dxf/a3-70mm_square-v1.dxf"),
                         "cutting_templates/dxf/a3-70mm_square-v2.dxf")

    # ---- robustness ------------------------------------------------------

    def test_a_missing_folder_is_not_an_error(self):
        # Nothing exists yet, so the requested name is simply free.
        self.assertEqual(self.next_name("cutting_templates/dxf/none-yet-v1.dxf"),
                         "cutting_templates/dxf/none-yet-v1.dxf")

    def test_a_symlink_at_the_target_is_treated_as_taken(self):
        # A link is not something to write through: treat the name as used.
        outside = Path(self.temp.name) / "outside.dxf"
        outside.write_bytes(b"target")
        link = self.dxf / "linked-v1.dxf"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable here")
        self.assertEqual(self.next_name("cutting_templates/dxf/linked-v1.dxf"),
                         "cutting_templates/dxf/linked-v2.dxf")

    def test_a_directly_named_file_whose_next_version_is_also_taken(self):
        self.touch("cutting_templates/dxf/chain-v1.dxf")
        self.touch("cutting_templates/dxf/chain-v2.dxf")
        self.touch("cutting_templates/dxf/chain-v3.dxf")
        self.assertEqual(self.next_name("cutting_templates/dxf/chain-v1.dxf"),
                         "cutting_templates/dxf/chain-v4.dxf")

    def test_an_exhausted_search_falls_back_to_the_requested_name(self):
        # The search is bounded; running out of it must not fail the job, and
        # must not claim a name was free.
        self.touch("cutting_templates/dxf/letter-standard-v1.dxf")
        with mock.patch.object(server, "_DXF_VERSION_MAX", 0):
            self.assertEqual(self.next_name("cutting_templates/dxf/letter-standard-v1.dxf"),
                             "cutting_templates/dxf/letter-standard-v1.dxf")

    def test_an_unusable_path_falls_back_to_the_requested_name(self):
        # Whatever the filesystem refuses to answer, the job still gets a path
        # instead of an exception.
        self.assertEqual(self.next_name("cutting_templates/dxf/letter\u0000standard-v1.dxf"),
                         "cutting_templates/dxf/letter\u0000standard-v1.dxf")


if __name__ == "__main__":
    unittest.main()
