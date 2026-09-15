#!/usr/bin/env python3
"""Smoke the packaged Pillow helper with the interpreter that will run jobs."""

from __future__ import annotations

import base64
import io
import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from PIL import Image


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFElEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    helper = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "scm_workbench/pdf_preview_helper.py"
    if not helper.is_file():
        raise SystemExit("PDF preview helper is missing")
    helper_source = helper.read_text(encoding="utf-8")
    if 'opened.draft("RGB", (NORMALIZED_LONG_EDGE, NORMALIZED_LONG_EDGE))' not in helper_source:
        raise SystemExit("PDF preview helper does not request reduced-resolution image decoding")
    with tempfile.TemporaryDirectory(prefix="scm-pdf-preview-helper-") as temporary:
        work = Path(temporary)
        raw = work / "raw"
        fronts = work / "fronts"
        raw.mkdir()
        fronts.mkdir()
        (raw / "0001.img").write_bytes(PNG)
        prepared = work / "prepared.json"
        subprocess.run(
            [sys.executable, str(helper), "prepare", str(raw), str(fronts), str(prepared)],
            check=True, stdin=subprocess.DEVNULL, timeout=10,
        )
        prepared_value = json.loads(prepared.read_text(encoding="utf-8"))
        if prepared_value.get("count") != 1 or len(list(fronts.iterdir())) != 1:
            raise SystemExit("PDF preview helper did not prepare exactly one image")

        page = work / "page1.png"
        page.write_bytes(PNG)
        jpeg = work / "page1.jpg"
        encoded = work / "encoded.json"
        subprocess.run(
            [sys.executable, str(helper), "encode", str(page), str(jpeg), str(encoded)],
            check=True, stdin=subprocess.DEVNULL, timeout=10,
        )
        encoded_value = json.loads(encoded.read_text(encoding="utf-8"))
        payload = jpeg.read_bytes()
        if (not payload.startswith(b"\xff\xd8\xff") or len(payload) > 512 * 1024 or
                encoded_value.get("bytes") != len(payload) or
                not (1 <= encoded_value.get("width", 0) <= 900) or
                not (1 <= encoded_value.get("height", 0) <= 900)):
            raise SystemExit("PDF preview helper returned an invalid bounded JPEG")

        # Sixteen ordinary high-resolution card images exceed the old 128
        # million aggregate-pixel cap even though each image is safely below
        # the per-image cap and decoding is sequential. Keep that real-world
        # sample working while preserving both per-image and count bounds.
        typical_raw = work / "typical-raw"
        typical_fronts = work / "typical-fronts"
        typical_raw.mkdir()
        typical_fronts.mkdir()
        source = Image.new("RGB", (3000, 3000), "#855a43")
        source_bytes = io.BytesIO()
        source.save(source_bytes, "JPEG", quality=60)
        source.close()
        for index in range(1, 17):
            (typical_raw / f"{index:04d}.img").write_bytes(source_bytes.getvalue())
        typical_prepared = work / "typical-prepared.json"
        subprocess.run(
            [sys.executable, str(helper), "prepare", str(typical_raw),
             str(typical_fronts), str(typical_prepared)],
            check=True, stdin=subprocess.DEVNULL, timeout=15,
        )
        typical_value = json.loads(typical_prepared.read_text(encoding="utf-8"))
        dimensions = typical_value.get("dimensions") or []
        if (typical_value.get("count") != 16 or len(list(typical_fronts.iterdir())) != 16 or
                len(dimensions) != 16 or any(max(pair) > 640 for pair in dimensions)):
            raise SystemExit("PDF preview helper rejected a bounded high-resolution sample")

        # A tiny file can claim enormous decoded dimensions. Prove that the
        # Pillow boundary rejects that decompression bomb before publication.
        bomb_raw = work / "bomb-raw"
        bomb_fronts = work / "bomb-fronts"
        bomb_raw.mkdir()
        bomb_fronts.mkdir()
        ihdr_data = struct.pack(">IIBBBBB", 50000, 50000, 8, 2, 0, 0, 0)
        ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data
        ihdr += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xffffffff)
        (bomb_raw / "0001.img").write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr)
        rejected = subprocess.run(
            [sys.executable, str(helper), "prepare", str(bomb_raw),
             str(bomb_fronts), str(work / "bomb.json")],
            stdin=subprocess.DEVNULL, timeout=10,
        )
        if rejected.returncode == 0 or any(bomb_fronts.iterdir()):
            raise SystemExit("PDF preview helper accepted unsafe decoded dimensions")
    print("ok: PDF preview Pillow helper prepares private samples and emits one bounded JPEG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
