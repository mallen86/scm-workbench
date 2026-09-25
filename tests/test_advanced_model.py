"""Optional model downloader: immutable origin, bounded bytes, integrity and no symlinks."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scm_workbench import advanced_model


class Response(io.BytesIO):
    def __init__(self, body, url=None, declared=None):
        super().__init__(body)
        self.url = url or advanced_model.MODEL_URL
        self.headers = {} if declared is None else {"Content-Length": declared}

    def geturl(self):
        return self.url


class AdvancedModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="advanced-model-")
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / advanced_model.MODEL_NAME
        self.body = b"pinned model fixture"
        self.patches = [
            mock.patch.object(advanced_model, "MODEL_BYTES", len(self.body)),
            mock.patch.object(advanced_model, "MODEL_SHA256", hashlib.sha256(self.body).hexdigest()),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def opener(self, response):
        op = mock.Mock()
        op.open.return_value = response
        return mock.patch.object(advanced_model.urllib.request, "build_opener", return_value=op)

    def test_verified_download_and_modified_asset_rejected(self):
        with self.opener(Response(self.body, declared=str(len(self.body)))):
            advanced_model.download_model(self.destination)
        self.assertTrue(advanced_model.verify_model(self.destination))
        self.destination.write_bytes(b"forged model contents")
        self.assertFalse(advanced_model.verify_model(self.destination))

    def test_unapproved_origin_and_length_and_digest_rejected(self):
        for response in (
            Response(self.body, url="https://example.com/model.onnx"),
            Response(self.body, declared="1000"),
            Response(self.body + b"extra"),
            Response(self.body[:-1]),
            Response(b"x" * len(self.body)),
        ):
            with self.subTest(response=response.geturl(), length=len(response.getvalue())):
                self.destination.unlink(missing_ok=True)
                with self.opener(response):
                    with self.assertRaises(ValueError):
                        advanced_model.download_model(self.destination)
                self.assertFalse(advanced_model.verify_model(self.destination))

    def test_symlink_and_hardlink_model_are_never_ready(self):
        original = Path(self.temp.name) / "original"
        original.write_bytes(self.body)
        self.destination.symlink_to(original)
        self.assertFalse(advanced_model.verify_model(self.destination))
        with self.opener(Response(self.body)):
            with self.assertRaises(FileExistsError):
                advanced_model.download_model(self.destination)
        self.destination.unlink()
        self.destination.hardlink_to(original)
        self.assertFalse(advanced_model.verify_model(self.destination))

    def test_redirects_are_restricted_and_bounded(self):
        handler = advanced_model._PinnedRedirects()
        request = mock.Mock()
        for url in ("http://us.aws.cdn.hf.co/asset", "https://evil.example/asset",
                    "https://user@huggingface.co/asset", "https://huggingface.co:444/asset"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    handler.redirect_request(request, None, 302, "redirect", {}, url)
        self.assertTrue(advanced_model._approved_url(advanced_model.MODEL_URL))
        self.assertTrue(advanced_model._approved_url("https://us.aws.cdn.hf.co/xet-bridge-us/asset"))


if __name__ == "__main__":
    unittest.main()
