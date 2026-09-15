#!/usr/bin/env python3
"""Pillow work isolated from the stdlib-only Workbench worker.

The selected SCM interpreter already needs Pillow to run create_pdf.py. This
helper uses that same interpreter to normalize a bounded private card sample
and to turn the private first-page PNG into one bounded JPEG. It never imports
silhouette-card-maker modules and never reads a user path directly.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageOps


SAMPLE_MAX = 16
SOURCE_IMAGE_MAX_PIXELS = 25_000_000
# Decode one image at a time, so aggregate memory remains bounded by the
# per-image limit. Keep total work bounded by the maximum sample count and let
# the parent process enforce its overall render deadline.
SOURCE_IMAGES_TOTAL_PIXELS = SAMPLE_MAX * SOURCE_IMAGE_MAX_PIXELS
NORMALIZED_LONG_EDGE = 640
NORMALIZED_JPEG_MAX_BYTES = 2 * 1024 * 1024
PAGE_MAX_BYTES = 64 * 1024 * 1024
PAGE_MAX_PIXELS = 4_000_000
RESULT_LONG_EDGE = 900
RESULT_JPEG_MAX_BYTES = 512 * 1024

Image.MAX_IMAGE_PIXELS = SOURCE_IMAGE_MAX_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)


def _regular_files(directory: Path, suffix: str) -> list[Path]:
    info = os.lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("unsafe preview directory")
    result = []
    with os.scandir(directory) as entries:
        for entry in entries:
            item = Path(entry.path)
            observed = os.lstat(item)
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                raise ValueError("unsafe preview file")
            if item.suffix == suffix:
                result.append(item)
    result.sort(key=lambda item: item.name)
    if not result or len(result) > SAMPLE_MAX:
        raise ValueError("invalid preview sample")
    return result


def _rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, "white")
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _exclusive_write(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short preview write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, value: dict) -> None:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > 4096:
        raise ValueError("preview metadata is too large")
    _exclusive_write(path, payload)


def prepare(raw_dir: Path, front_dir: Path, metadata_path: Path) -> None:
    raw_files = _regular_files(raw_dir, ".img")
    front_info = os.lstat(front_dir)
    if stat.S_ISLNK(front_info.st_mode) or not stat.S_ISDIR(front_info.st_mode):
        raise ValueError("unsafe normalized preview directory")
    if any(front_dir.iterdir()):
        raise ValueError("normalized preview directory is not empty")

    total_pixels = 0
    dimensions = []
    for index, source in enumerate(raw_files, 1):
        with Image.open(source) as opened:
            width, height = opened.size
            pixels = width * height
            if width <= 0 or height <= 0 or pixels > SOURCE_IMAGE_MAX_PIXELS:
                raise ValueError("preview source dimensions are unsafe")
            total_pixels += pixels
            if total_pixels > SOURCE_IMAGES_TOTAL_PIXELS:
                raise ValueError("preview source pixels exceed the aggregate limit")
            opened.seek(0)
            # JPEG decoders can select a reduced native resolution before
            # loading pixels. The original dimensions above remain the safety
            # boundary; this only avoids decoding detail that the 640 px
            # representative sample would immediately discard.
            opened.draft("RGB", (NORMALIZED_LONG_EDGE, NORMALIZED_LONG_EDGE))
            transposed = ImageOps.exif_transpose(opened)
            transposed.load()
            normalized = _rgb(transposed)
        normalized.thumbnail((NORMALIZED_LONG_EDGE, NORMALIZED_LONG_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        normalized.save(output, "JPEG", quality=65, optimize=True)
        payload = output.getvalue()
        if not payload or len(payload) > NORMALIZED_JPEG_MAX_BYTES:
            raise ValueError("normalized preview image is too large")
        _exclusive_write(front_dir / f"{index:04d}.jpg", payload)
        dimensions.append([normalized.width, normalized.height])
        normalized.close()

    _write_json(metadata_path, {
        "version": 1,
        "count": len(raw_files),
        "dimensions": dimensions,
    })


def encode(page_path: Path, jpeg_path: Path, metadata_path: Path) -> None:
    observed = os.lstat(page_path)
    if (stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode) or
            observed.st_size < 1 or observed.st_size > PAGE_MAX_BYTES):
        raise ValueError("unsafe preview page")
    with Image.open(page_path) as opened:
        width, height = opened.size
        if width <= 0 or height <= 0 or width * height > PAGE_MAX_PIXELS:
            raise ValueError("preview page dimensions are unsafe")
        opened.load()
        image = _rgb(opened)

    image.thumbnail((RESULT_LONG_EDGE, RESULT_LONG_EDGE), Image.Resampling.LANCZOS)
    quality = 60
    payload = b""
    for _attempt in range(8):
        output = io.BytesIO()
        image.save(output, "JPEG", quality=quality, optimize=True)
        payload = output.getvalue()
        if payload and len(payload) <= RESULT_JPEG_MAX_BYTES:
            break
        if quality > 35:
            quality -= 10
        else:
            next_size = (max(240, int(image.width * 0.82)),
                         max(240, int(image.height * 0.82)))
            if next_size == image.size:
                break
            image.thumbnail(next_size, Image.Resampling.LANCZOS)
    if not payload or len(payload) > RESULT_JPEG_MAX_BYTES:
        raise ValueError("preview JPEG exceeds the result limit")

    _exclusive_write(jpeg_path, payload)
    _write_json(metadata_path, {
        "version": 1,
        "width": image.width,
        "height": image.height,
        "bytes": len(payload),
    })
    image.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("prepare", "encode"))
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("metadata")
    try:
        args = parser.parse_args(argv)
        source = Path(args.source)
        destination = Path(args.destination)
        metadata = Path(args.metadata)
        if args.operation == "prepare":
            prepare(source, destination, metadata)
        else:
            encode(source, destination, metadata)
        return 0
    except Exception:
        # The parent exposes only a generic bounded application error. Avoid
        # leaking paths, Pillow internals, or attacker-controlled filenames.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
