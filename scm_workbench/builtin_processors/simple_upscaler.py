"""Lanczos upscaler: honor embedded PPI, otherwise assume a standard MTG card."""
import math
from pathlib import Path
import struct
import sys

from PIL import Image, ImageOps

TARGET_DPI = (1200, 1200)
STANDARD_MTG_PIXELS = tuple(round(mm / 25.4 * TARGET_DPI[0]) for mm in (63, 88))


def _resolution_pair(value):
    try:
        x, y = value
        if isinstance(x, bool) or isinstance(y, bool):
            raise ValueError
        x, y = float(x), float(y)
        if not all(math.isfinite(axis) and axis > 0 for axis in (x, y)):
            raise ValueError
    except (TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("invalid PPI/DPI metadata; both axes must be finite and positive") from exc
    if not math.isclose(x, y, rel_tol=1e-6):
        raise ValueError("inconsistent X/Y PPI/DPI metadata; resizing could distort the image")
    return x


def _input_ppi(opened, exif):
    candidates = []
    if opened.format == "JPEG":
        # Pillow can synthesize 72 DPI or duplicate EXIF's X axis. Read actual
        # JFIF/EXIF fields instead, so neither fallback becomes an input PPI.
        unit = opened.info.get("jfif_unit")
        if unit in (1, 2):
            ppi = _resolution_pair(opened.info.get("jfif_density"))
            candidates.append(ppi * (2.54 if unit == 2 else 1))
        elif unit not in (None, 0):
            raise ValueError("invalid JFIF resolution unit")
    elif "dpi" in opened.info:
        candidates.append(_resolution_pair(opened.info["dpi"]))
    if any(tag in exif for tag in (282, 283, 296)):
        unit = exif.get(296)
        if unit not in (2, 3):
            raise ValueError("missing or invalid EXIF resolution unit")
        ppi = _resolution_pair((exif.get(282), exif.get(283)))
        candidates.append(ppi * (2.54 if unit == 3 else 1))
    if not candidates:
        raise ValueError("missing embedded PPI/DPI metadata")
    if not all(math.isfinite(ppi) and ppi > 0 for ppi in candidates):
        raise ValueError("invalid PPI/DPI metadata after resolution-unit conversion")
    # PNG/BMP densities are quantized in pixels/metre (one step = .0254 DPI).
    if any(not math.isclose(candidates[0], other, rel_tol=1e-6, abs_tol=0.02)
           for other in candidates[1:]):
        raise ValueError("conflicting embedded PPI/DPI metadata")
    return candidates[0]


def _diagnostic(image_path, context, action, reason):
    name = str(context.get("relative_path") or image_path.name)
    # One bounded diagnostic in the isolated runner's captured stderr, never
    # in the native worker's reserved protocol stdout.
    name = "".join(char if char.isprintable() else "?" for char in name)[:512]
    reason = "".join(char if char.isprintable() else "?" for char in reason)
    reason = reason.encode("utf-8", "replace")[:256].decode("utf-8", "ignore")
    suffix = " Original unchanged." if action == "skipped" else ""
    print(f"Simple Upscaler: {action} {name!r}: {reason}.{suffix}", file=sys.stderr, flush=True)
    return reason


def _skip(image_path, context, reason):
    reason = _diagnostic(image_path, context, "skipped", reason)
    reporter = context.get("_workbench_report_skip")
    if callable(reporter):
        reporter(reason)


def process_image(image_path: Path, context: dict) -> None:
    with Image.open(image_path) as opened:
        try:
            image_format = opened.format
            input_ppi = None
            metadata_error = None
            readable_exif = True
            try:
                exif = opened.getexif()
            except (ValueError, TypeError, SyntaxError, OSError, OverflowError,
                    ZeroDivisionError, struct.error) as exc:
                # Unreadable EXIF cannot supply orientation or be preserved.
                exif = Image.Exif()
                readable_exif = False
                opened.info.pop("exif", None)
                metadata_error = exc
            if readable_exif:
                try:
                    input_ppi = _input_ppi(opened, exif)
                except (ValueError, TypeError, SyntaxError, OSError, OverflowError,
                        ZeroDivisionError, struct.error) as exc:
                    metadata_error = exc
            width, height = opened.size
            if exif.get(274) in (5, 6, 7, 8):
                width, height = height, width
            if input_ppi is None:
                _diagnostic(image_path, context, "assuming standard MTG 63 × 88 mm",
                            f"{metadata_error}; approximate 1200 PPI, aspect-preserving fit, no bleed detection")
                target_width, target_height = STANDARD_MTG_PIXELS
                if width > height:
                    target_width, target_height = target_height, target_width
                # Fit inside the target box, never stretch, crop, or downscale.
                scale = min(target_width / width, target_height / height)
                if scale <= 1:
                    _skip(image_path, context, "pixels already meet or exceed the standard MTG fallback fit target")
                    return
            else:
                # Allow the half-step rounding of physical PNG/BMP density fields.
                encoded_target = (image_format in {"PNG", "BMP"} and "dpi" in opened.info
                                  and math.isclose(input_ppi, 1200, rel_tol=0, abs_tol=0.02))
                if input_ppi >= 1200 or encoded_target:
                    _skip(image_path, context, "embedded resolution is already at or above 1200 PPI")
                    return
                scale = 1200 / input_ppi
            pixel_limit = min(200_000_000, Image.MAX_IMAGE_PIXELS or 200_000_000)
            size_advice = (
                f"above the supported limit of {pixel_limit:,} total pixels. "
                "Use smaller original images; restore or re-fetch images if they were already upscaled."
            )
            if not math.isfinite(scale) or scale > pixel_limit:
                raise ValueError(f"1200-PPI upscaling would produce an oversized image, {size_advice}")
            output_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            if output_size[0] * output_size[1] > pixel_limit:
                raise ValueError(
                    f"1200-PPI upscaling would produce {output_size[0]:,} × {output_size[1]:,} pixels, "
                    f"{size_advice}"
                )
            icc_profile = opened.info.get("icc_profile")
            image = ImageOps.exif_transpose(opened) if readable_exif else opened.copy()
        finally:
            opened.close()  # Exiting Image.open() alone only closes the file handle.

    try:
        image.load()
        exif = image.getexif()
        # Orientation is now applied to pixels. Keep any EXIF resolution and
        # dimension tags consistent with the new pixels and container density.
        exif.pop(274, None)
        if any(tag in exif for tag in (282, 283, 296)):
            exif[282] = exif[283] = 1200
            exif[296] = 2
        for tag, value in ((256, output_size[0]), (257, output_size[1]),
                           (40962, output_size[0]), (40963, output_size[1])):
            if tag in exif:
                exif[tag] = value
        resized = image.resize(output_size, Image.Resampling.LANCZOS)
    finally:
        image.close()  # Release the full source buffer before encoding.
    save_options = {}
    if image_format in {"JPEG", "PNG", "BMP"}:
        save_options["dpi"] = TARGET_DPI
    if icc_profile:
        save_options["icc_profile"] = icc_profile
    if exif:
        save_options["exif"] = exif.tobytes()
    if image_format == "JPEG":
        if resized.mode not in {"RGB", "L"}:
            try:
                converted = resized.convert("RGB")
            except Exception:
                resized.close()
                raise
            resized.close()
            resized = converted
        # Optimized JPEG encoding buffers the full image and coefficient arrays.
        # Keep quality and chroma detail without that additional memory peak.
        save_options.update(quality=95, subsampling=0, optimize=False)
    elif image_format == "PNG":
        save_options["optimize"] = True

    try:
        resized.save(image_path, format=image_format, **save_options)
    finally:
        resized.close()
