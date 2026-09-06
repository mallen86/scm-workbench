"""Bounds and hostile-input tests for the managed repository synchronizer."""

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scm_workbench import repo_sync


SHA = "a" * 40
SHA2 = "b" * 40


class FakeResponse:
    def __init__(self, body=b"", headers=None, chunk=None, final_url=None):
        self.body = body
        self.headers = headers or {}
        self.chunk = chunk
        self.final_url = final_url
        self.offset = 0
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.offset >= len(self.body):
            return b""
        if self.chunk is not None:
            size = min(size, self.chunk)
        end = min(len(self.body), self.offset + size) if size >= 0 else len(self.body)
        out = self.body[self.offset:end]
        self.offset = end
        return out

    def close(self):
        self.closed = True

    def geturl(self):
        return self.final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RepoSyncBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scm-workbench-bounds-")
        self.data = Path(self.temp.name) / "data"
        self.env = patch.dict(os.environ, {"SCM_WORKBENCH_DATA": str(self.data)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def assert_repo_error(self, fn, *args, **kwargs):
        with self.assertRaises(repo_sync.RepoError):
            fn(*args, **kwargs)

    def test_repository_keys_and_sources_are_bounded(self):
        self.assertEqual(repo_sync.validate_repo_key("scm"), "scm")
        self.assertEqual(repo_sync.validate_repo_key("extras"), "extras")
        for value in ("", None, "unknown", "x" * 33, "scm\n"):
            self.assert_repo_error(repo_sync.validate_repo_key, value)
        self.assert_repo_error(repo_sync.repo_dir, "unknown")
        self.assert_repo_error(repo_sync.manifest_file, "unknown")

        for value in ("main", "latest-release", "feature/topic", "v1.2.3", SHA,
                      "référence/雪"):
            self.assertEqual(repo_sync.validate_source(value), value)
        invalid = (
            "", None, " main", "main ", "/main", "\\main", "-main", "--help",
            "../main", "main/..", "main//x", "main/./x", "https://evil",
            "file://x", "a\\b", "C:/main", "main?x", "main#x", "main%20",
            "main[x]", "main^x", "main~x", "main*x", "main:x", "bad\x00ref",
            "main..x", "main@{x", "main/.hidden", "main/hidden.",
            "main/foo.lock", "main\tbranch", "x" * 257,
        )
        for value in invalid:
            self.assert_repo_error(repo_sync.validate_source, value)
        # A lone surrogate cannot be UTF-8 encoded and must not leak into an
        # exception or URL construction.
        self.assert_repo_error(repo_sync.validate_source, "bad\ud800")

    def test_portable_repository_paths_reject_escape_forms(self):
        for value in ("README.md", "dir/file.txt", "unicode/é雪.txt"):
            self.assertEqual(repo_sync.validate_repo_path(value), value)
        invalid = (
            "", ".", "..", "/etc/passwd", "\\etc\\passwd", "a\\b", "C:/x",
            "C:\\x", "//server/share", "a//b", "a/./b", "a/../b",
            "a\x7fb", "a\n/b", "x" * 256,
        )
        for value in invalid:
            self.assert_repo_error(repo_sync.validate_repo_path, value)
        self.assert_repo_error(repo_sync.validate_repo_path, "a/" + ("x" * 256))
        self.assert_repo_error(repo_sync.validate_repo_path, "/".join(["x"] * 65))
        self.assert_repo_error(repo_sync.validate_repo_path, "x" * 4097)

    def test_source_and_state_environment_are_restored(self):
        # This also covers the ordinary settings/state workflow used by older
        # callers, while setUp/tearDown guarantees no process environment leak.
        self.assertEqual(repo_sync.load_source("extras"), "main")
        repo_sync.set_source("extras", "feature/topic")
        self.assertEqual(repo_sync.load_source("extras"), "feature/topic")
        repo_sync.set_source("extras", SHA)
        self.assertEqual(repo_sync.load_source("extras"), SHA)
        repo_sync.save_state({"extras": {"source": "release/v1"}})
        self.assertEqual(repo_sync.load_source("extras"), "release/v1")
        repo_sync.save_state({"extras": {"source": "../escape"}})
        self.assert_repo_error(repo_sync.load_source, "extras")

    def test_gh_json_enforces_declared_and_streamed_caps(self):
        response = FakeResponse(b'{"ok": true}', {"Content-Length": "12"})
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=response):
            self.assertEqual(repo_sync.gh_json("/repos/example"), {"ok": True})
        self.assertTrue(response.closed)

        too_large_header = FakeResponse(b"{}", {"Content-Length": str(repo_sync.GH_JSON_CAP + 1)})
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=too_large_header):
            self.assert_repo_error(repo_sync.gh_json, "/repos/example")
        self.assertTrue(too_large_header.closed)

        streaming = FakeResponse(b"x" * (repo_sync.GH_JSON_CAP + 1), chunk=1 << 20)
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=streaming):
            self.assert_repo_error(repo_sync.gh_json, "/repos/example")
        self.assertTrue(streaming.closed)
        self.assertLessEqual(max(streaming.read_sizes), 1 << 16)

    def test_gh_json_rejects_malformed_shape_and_bounds_errors(self):
        for body in (b"not json", b'"scalar"', b"null", b"123"):
            response = FakeResponse(body)
            with patch.object(repo_sync.urllib.request, "urlopen", return_value=response):
                self.assert_repo_error(repo_sync.gh_json, "/repos/example")
            self.assertTrue(response.closed)

        with patch.object(repo_sync.urllib.request, "urlopen", side_effect=OSError("e" * 10000)):
            with self.assertRaises(repo_sync.RepoError) as caught:
                repo_sync.gh_json("/" + "p" * 5000)
        self.assertLess(len(str(caught.exception)), 500)
        self.assertNotIn("e" * 1000, str(caught.exception))
        self.assert_repo_error(repo_sync.gh_json, "https://evil")
        self.assert_repo_error(repo_sync.gh_json, "repos/no-leading-slash")

    def test_list_refs_filters_bad_fields_limits_counts_and_result(self):
        tag = {"name": "v1.0", "commit": {"sha": SHA}}
        release = {"tag_name": "v1.0", "name": "Release", "published_at": "2025-01-01",
                   "prerelease": False}
        tags = [tag] + [{"name": "../bad", "commit": {"sha": "short"}},
                         {"name": "x" * 257, "commit": {"sha": SHA}}]
        releases = [release, {**release, "tag_name": "bad/tag/../x"},
                    {**release, "name": "n" * 257}, {**release, "prerelease": "no"}]
        with patch.object(repo_sync, "repo_api", return_value={"default_branch": "main"}), \
             patch.object(repo_sync, "gh_json", side_effect=[tags, releases]):
            result = repo_sync.list_refs("scm")
        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(result["tags"], [{"name": "v1.0", "sha": SHA}])
        self.assertEqual(result["releases"], [{"tag": "v1.0", "name": "Release",
                                                "date": "2025-01-01", "prerelease": False}])

        many_tags = [tag] * 101
        many_releases = [release] * 31
        with patch.object(repo_sync, "repo_api", return_value={"default_branch": "main"}), \
             patch.object(repo_sync, "gh_json", side_effect=[many_tags, many_releases]):
            result = repo_sync.list_refs("scm")
        self.assertEqual(len(result["tags"]), 100)
        self.assertEqual(len(result["releases"]), 30)

        with patch.object(repo_sync, "repo_api", return_value={"default_branch": "main"}), \
             patch.object(repo_sync, "gh_json", side_effect=[{}, []]):
            self.assert_repo_error(repo_sync.list_refs, "scm")
        with patch.object(repo_sync, "repo_api", return_value={"default_branch": "main"}), \
             patch.object(repo_sync, "gh_json", side_effect=[[tag], {}]):
            self.assert_repo_error(repo_sync.list_refs, "scm")

        with patch.object(repo_sync, "repo_api", return_value={"default_branch": "main"}), \
             patch.object(repo_sync, "gh_json", side_effect=[[tag], [release]]), \
             patch.object(repo_sync, "REFS_RESULT_CAP", 10):
            self.assert_repo_error(repo_sync.list_refs, "scm")
        with patch.object(repo_sync, "repo_api", return_value={"default_branch": "main/../x"}), \
             patch.object(repo_sync, "gh_json", return_value=[]):
            self.assert_repo_error(repo_sync.list_refs, "scm")

    def test_compare_requires_strict_commit_and_file_shapes(self):
        valid_file = {"filename": "dir/file.txt", "status": "modified",
                      "previous_filename": "old/file.txt"}
        good = {"status": "ahead", "total_commits": 2, "files": [valid_file]}
        with patch.object(repo_sync, "gh_json", return_value=good):
            self.assertEqual(repo_sync.compare("scm", SHA, SHA2), {
                "files": [{"path": "dir/file.txt", "status": "modified",
                            "previous": "old/file.txt"}],
                "too_many": False, "commits": 2, "status": "ahead"})

        for base, head in (("short", SHA2), (SHA, "g" * 40), (SHA, True), (SHA, SHA + "x")):
            self.assert_repo_error(repo_sync.compare, "scm", base, head)
        for response in (
            None, [], {"status": "wat", "total_commits": 1, "files": []},
            {"status": "ahead", "files": []},
            {"status": "ahead", "total_commits": True, "files": []},
            {"status": "ahead", "total_commits": -1, "files": []},
            {"status": "ahead", "total_commits": 1, "files": {}},
        ):
            with patch.object(repo_sync, "gh_json", return_value=response):
                self.assert_repo_error(repo_sync.compare, "scm", SHA, SHA2)

        exactly_cap = {"status": "ahead", "total_commits": 1,
                       "files": [valid_file] * repo_sync.DIFF_FILE_CAP}
        with patch.object(repo_sync, "gh_json", return_value=exactly_cap):
            result = repo_sync.compare("scm", SHA, SHA2)
        self.assertEqual(len(result["files"]), repo_sync.DIFF_FILE_CAP)
        self.assertTrue(result["too_many"])

        too_many = {"status": "ahead", "total_commits": 1,
                    "files": [valid_file] * (repo_sync.DIFF_FILE_CAP + 1)}
        with patch.object(repo_sync, "gh_json", return_value=too_many):
            self.assert_repo_error(repo_sync.compare, "scm", SHA, SHA2)
        for bad_path in ("../x", "/x", "a\\b", "C:/x", "a//b", "a/./b"):
            response = {"status": "ahead", "total_commits": 1,
                        "files": [{"filename": bad_path, "status": "added"}]}
            with patch.object(repo_sync, "gh_json", return_value=response):
                self.assert_repo_error(repo_sync.compare, "scm", SHA, SHA2)
        response = {"status": "ahead", "total_commits": 1,
                    "files": [{"filename": "new.txt", "status": "renamed",
                               "previous_filename": "../old.txt"}]}
        with patch.object(repo_sync, "gh_json", return_value=response):
            self.assert_repo_error(repo_sync.compare, "scm", SHA, SHA2)

    def test_response_redirects_are_checked_against_context_hosts(self):
        evil = FakeResponse(b"{}", final_url="https://evil.example/x")
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=evil):
            self.assert_repo_error(repo_sync.gh_json, "/repos/example")
        self.assertTrue(evil.closed)

        codeload = FakeResponse(
            b"tar", {"Content-Length": "3"},
            final_url="https://codeload.github.com/Alan-Cha/scm-extras/tar.gz/" + SHA,
        )
        tar_url = repo_sync.API + "/repos/Alan-Cha/scm-extras/tarball/" + SHA
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=codeload):
            self.assertEqual(repo_sync.gh_get_bytes(tar_url), b"tar")
        self.assertTrue(codeload.closed)

        for initial, final in (
            (repo_sync.API + "/x", "https://codeload.github.com/x"),
            (repo_sync.RAW + "/x", "https://codeload.github.com/x"),
            (repo_sync.RAW + "/x", "https://raw.githubusercontent.com:443/x"),
            (repo_sync.RAW + "/x", "https://user@raw.githubusercontent.com/x"),
        ):
            response = FakeResponse(b"x", final_url=final)
            with patch.object(repo_sync.urllib.request, "urlopen", return_value=response):
                self.assert_repo_error(repo_sync.gh_get_bytes, initial)
            self.assertTrue(response.closed)

    def test_gh_get_bytes_allows_only_fixed_hosts_and_closes_streams(self):
        for url in (repo_sync.API + "/x", repo_sync.RAW + "/owner/repo/" + SHA + "/x"):
            response = FakeResponse(b"hello", {"Content-Length": "5"})
            progress = []
            with patch.object(repo_sync.urllib.request, "urlopen", return_value=response):
                self.assertEqual(repo_sync.gh_get_bytes(url, progress_cb=lambda d, t: progress.append((d, t))), b"hello")
            self.assertEqual(progress, [(0, 5), (5, 5)])
            self.assertTrue(response.closed)
        for url in ("http://api.github.com/x", "https://evil.example/x",
                    "https://api.github.com.evil/x", "https://user@api.github.com/x"):
            with patch.object(repo_sync.urllib.request, "urlopen") as opened:
                self.assert_repo_error(repo_sync.gh_get_bytes, url)
                opened.assert_not_called()

        bad_header = FakeResponse(b"x", {"Content-Length": "not-a-number"})
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=bad_header):
            self.assert_repo_error(repo_sync.gh_get_bytes, repo_sync.RAW + "/x")
        self.assertTrue(bad_header.closed)
        declared_large = FakeResponse(b"", {"Content-Length": "11"})
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=declared_large):
            self.assert_repo_error(repo_sync.gh_get_bytes, repo_sync.RAW + "/x", max_bytes=10)
        self.assertTrue(declared_large.closed)

    def test_gh_get_bytes_caps_chunks_streaming_and_progress_on_failure(self):
        response = FakeResponse(b"x" * (2 * 1024 * 1024 + 1), chunk=1 << 20)
        progress = []
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(repo_sync.RepoError):
                repo_sync.gh_get_bytes(repo_sync.RAW + "/x", max_bytes=2 * 1024 * 1024,
                                       progress_cb=lambda d, t: progress.append((d, t)))
        self.assertTrue(response.closed)
        self.assertEqual(progress[0], (0, 0))
        self.assertGreaterEqual(progress[-1][0], 2 * 1024 * 1024)
        self.assertLessEqual(max(response.read_sizes), 1 << 20)
        self.assert_repo_error(repo_sync.gh_get_bytes, repo_sync.RAW + "/x", max_bytes=0)
        self.assert_repo_error(repo_sync.gh_get_bytes, repo_sync.RAW + "/x", max_bytes=True)

        response = FakeResponse(b"unbounded")
        with patch.object(repo_sync.urllib.request, "urlopen", return_value=response):
            self.assertEqual(repo_sync.gh_get_bytes(repo_sync.RAW + "/x"), b"unbounded")
        self.assertTrue(response.closed)

    @staticmethod
    def make_tar(member_specs):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tf:
            for spec in member_specs:
                name, kind, payload = spec
                info = tarfile.TarInfo(name)
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                elif kind == "file":
                    info.size = len(payload)
                    tf.addfile(info, io.BytesIO(payload))
                    continue
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = payload
                elif kind == "hardlink":
                    info.type = tarfile.LNKTYPE
                    info.linkname = payload
                elif kind == "device":
                    info.type = tarfile.CHRTYPE
                    info.devmajor, info.devminor = 1, 3
                tf.addfile(info)
        return stream.getvalue()

    def assert_bad_tar(self, specs):
        path = Path(self.temp.name) / "bad.tar.gz"
        path.write_bytes(self.make_tar(specs))
        with self.assertRaises(repo_sync.RepoError):
            repo_sync.extract_tarball(path, Path(self.temp.name) / "extract")

    def test_tarball_validation_rejects_traversal_duplicates_and_special_types(self):
        for specs in (
            [("owner-repo-sha/", "dir", None), ("owner-repo-sha/../evil", "file", b"x")],
            [("owner-repo-sha/", "dir", None), ("owner-repo-sha/a", "file", b"x"),
             ("owner-repo-sha/a", "file", b"y")],
            [("owner-repo-sha/", "dir", None), ("owner-repo-sha/link", "symlink", "/outside")],
            [("owner-repo-sha/", "dir", None), ("owner-repo-sha/link", "hardlink", "x")],
            [("owner-repo-sha/", "dir", None), ("owner-repo-sha/device", "device", None)],
            [("owner-repo-sha/", "dir", None), ("other/file", "file", b"x")],
        ):
            self.assert_bad_tar(specs)

    def test_valid_legacy_tarball_extracts_flat_tree(self):
        # Real tarfile readers commonly expose the wrapper as either root or
        # root/.  Exercise both spellings through the actual extraction path.
        for index, wrapper in enumerate(("owner-repo-sha", "owner-repo-sha/")):
            tar_path = Path(self.temp.name) / f"valid-{index}.tar.gz"
            prefix = wrapper.rstrip("/") + "/"
            tar_path.write_bytes(self.make_tar([
                (wrapper, "dir", None),
                (prefix + "game/", "dir", None),
                (prefix + "game/front/", "dir", None),
                (prefix + "game/front/card.txt", "file", b"card"),
                (prefix + "README.md", "file", b"readme"),
            ]))
            dest = Path(self.temp.name) / f"tree-{index}"
            repo_sync.extract_tarball(tar_path, dest)
            self.assertEqual((dest / "game/front/card.txt").read_bytes(), b"card")
            self.assertEqual((dest / "README.md").read_bytes(), b"readme")
            self.assertEqual(repo_sync.tracked_paths(dest), ["README.md", "game/front/card.txt"])

    def test_windows_fallback_is_a_mockable_no_path_open_boundary(self):
        root = Path(self.temp.name) / "root"
        root.mkdir()
        target = root / "nested/file.txt"
        outside = Path(self.temp.name) / "outside.txt"
        calls = []

        def rejected(path, **kwargs):
            calls.append((Path(path), kwargs))
            raise repo_sync.RepoError("mocked reparse/outside final handle")

        with patch.object(repo_sync, "_WINDOWS_FALLBACK", True), \
             patch.object(repo_sync, "_DESCRIPTOR_IO", False), \
             patch.object(repo_sync, "_windows_open_checked", side_effect=rejected):
            with self.assertRaises(repo_sync.RepoError):
                repo_sync._secure_write_bytes(target, b"must-not-write")
        self.assertTrue(calls)
        self.assertFalse(target.exists())
        self.assertFalse(outside.exists())

        # The non-Windows fallback still verifies the opened handle is a
        # regular file; it must not silently accept a directory as a file.
        directory = root / "directory"
        directory.mkdir()
        with patch.object(repo_sync, "_WINDOWS_FALLBACK", False), \
             patch.object(repo_sync, "_DESCRIPTOR_IO", False):
            self.assert_repo_error(repo_sync._secure_hash_file, directory)

    def test_safe_paths_download_destinations_and_root_escape(self):
        root = Path(self.temp.name) / "root"
        root.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.assertEqual(repo_sync.safe_path(root, "unicode/é.txt"),
                         (root / "unicode/é.txt").resolve())
        self.assert_repo_error(repo_sync.safe_path, root, "../outside/file")
        self.assert_repo_error(repo_sync.safe_path, root, "/tmp/file")

        link_parent = root / "link-parent"
        link_parent.symlink_to(outside, target_is_directory=True)
        with patch.object(repo_sync, "gh_get_bytes", return_value=b"data") as get_bytes:
            self.assert_repo_error(repo_sync.download_to, "scm", SHA, "x.txt",
                                   link_parent / "x.txt")
            get_bytes.assert_not_called()
        leaf = root / "leaf"
        leaf.symlink_to(outside / "leaf")
        with patch.object(repo_sync, "gh_get_bytes", return_value=b"data") as get_bytes:
            self.assert_repo_error(repo_sync.download_to, "scm", SHA, "x.txt", leaf)
            get_bytes.assert_not_called()

        real = root / "real.txt"
        with patch.object(repo_sync, "gh_get_bytes", return_value=b"data"):
            self.assertEqual(repo_sync.download_to("scm", SHA, "dir/x.txt", real), 4)
        self.assertEqual(real.read_bytes(), b"data")

    def test_stash_restore_uses_secure_descriptor_copy_boundary(self):
        old_repo = Path(self.temp.name) / "old-repo"
        (old_repo / "data/nested").mkdir(parents=True)
        (old_repo / "data/nested/user.txt").write_bytes(b"user")
        stash = Path(self.temp.name) / "stash"
        new_repo = Path(self.temp.name) / "new-repo"
        new_repo.mkdir()
        with patch.object(repo_sync, "_secure_copy_file",
                          wraps=repo_sync._secure_copy_file) as copier:
            saved = repo_sync.stash_user_data(old_repo, stash)
            repo_sync.restore_user_data(saved, new_repo)
        self.assertEqual((new_repo / "data/nested/user.txt").read_bytes(), b"user")
        self.assertEqual(copier.call_count, 2)

    def test_tracked_hash_and_apply_reject_symlinks_and_bad_paths(self):
        tree = Path(self.temp.name) / "tree"
        (tree / "dir").mkdir(parents=True)
        good = tree / "dir/file.txt"
        good.write_bytes(b"content")
        self.assertEqual(repo_sync.tracked_paths(tree), ["dir/file.txt"])
        self.assertEqual(repo_sync.sha256_file(good), hashlib.sha256(b"content").hexdigest())

        outside = Path(self.temp.name) / "outside.txt"
        outside.write_bytes(b"outside")
        link = tree / "link.txt"
        link.symlink_to(outside)
        self.assert_repo_error(repo_sync.tracked_paths, tree)
        self.assert_repo_error(repo_sync.sha256_file, link)

        repo = repo_sync.repo_dir("scm")
        repo.mkdir(parents=True)
        pristine = Path(self.temp.name) / "pristine.txt"
        pristine.write_bytes(b"new")
        target = {"sha": SHA, "ref": "main", "date": None}
        for bad in ("../escape", "/escape", "a\\b", "a//b", "C:/escape"):
            with self.assertRaises(repo_sync.RepoError):
                repo_sync.apply_changes("scm", {"files": {}}, target, {bad: None}, [],
                                        lambda _: pristine)
            with self.assertRaises(repo_sync.RepoError):
                repo_sync.apply_changes("scm", {"files": {}}, target, {}, [bad],
                                        lambda _: pristine)
        symlink_local = repo / "link.txt"
        symlink_local.symlink_to(outside)
        self.assert_repo_error(repo_sync.apply_changes, "scm", {"files": {}}, target,
                               {"link.txt": None}, [], lambda _: pristine)
        symlink_pristine = Path(self.temp.name) / "pristine-link"
        symlink_pristine.symlink_to(pristine)
        self.assert_repo_error(repo_sync.apply_changes, "scm", {"files": {}}, target,
                               {"new.txt": None}, [], lambda _: symlink_pristine)

    def test_apply_plan_validates_everything_before_mutating(self):
        repo = repo_sync.repo_dir("scm")
        repo.mkdir(parents=True)
        live = repo / "keep.txt"
        live.write_bytes(b"keep")
        pristine = Path(self.temp.name) / "pristine.txt"
        pristine.write_bytes(b"new")
        target = {"sha": SHA, "ref": "main", "date": None}

        for bad_ops in (
            {"good/new.txt": None, "../escape": None},
            {"good/new.txt": "", "other.txt": None},
            {"good/new.txt": 0},
        ):
            with self.assertRaises(repo_sync.RepoError):
                repo_sync.apply_changes("scm", {"files": {}}, target, bad_ops, [],
                                        lambda _: pristine)
            self.assertEqual(live.read_bytes(), b"keep")
            self.assertFalse((repo / "good").exists())

        symlink_pristine = Path(self.temp.name) / "symlink-pristine"
        symlink_pristine.symlink_to(pristine)
        with self.assertRaises(repo_sync.RepoError):
            repo_sync.apply_changes(
                "scm", {"files": {}}, target,
                {"good/first.txt": None, "good/second.txt": None}, [],
                lambda path: pristine if path.endswith("first.txt") else symlink_pristine)
        self.assertFalse((repo / "good").exists())

        result = repo_sync.apply_changes(
            "scm", {"files": {}}, target, {"nested/new.txt": None}, [],
            lambda _: pristine)
        self.assertEqual(result["applied"], 1)
        self.assertEqual((repo / "nested/new.txt").read_bytes(), b"new")

    def test_valid_legacy_apply_workflow_remains_intact(self):
        repo = repo_sync.repo_dir("scm")
        repo.mkdir(parents=True)
        old = repo / "old.txt"
        old.write_bytes(b"old")
        staged = Path(self.temp.name) / "new.txt"
        staged.write_bytes(b"new")
        old_hash = hashlib.sha256(b"old").hexdigest()
        result = repo_sync.apply_changes(
            "scm", {"files": {"old.txt": old_hash}},
            {"sha": SHA2, "ref": "main", "date": "2025-01-01"},
            {"new.txt": "old.txt"}, [], lambda path: staged)
        self.assertEqual(result["applied"], 1)
        self.assertFalse(old.exists())
        self.assertEqual((repo / "new.txt").read_bytes(), b"new")
        self.assertEqual(result["manifest"]["files"]["new.txt"],
                         hashlib.sha256(b"new").hexdigest())


if __name__ == "__main__":
    unittest.main()
