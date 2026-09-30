"""Built-in dependency-free 4× card-image upscaler."""
from pathlib import Path

from PIL import Image, ImageOps

SCALE = 4
TARGET_DPI = (1200, 1200)


def process_image(image_path: Path, context: dict) -> None:
    with Image.open(image_path) as opened:
        image_format = opened.format
        icc_profile = opened.info.get("icc_profile")
        image = ImageOps.exif_transpose(opened)
        image.load()
        exif = image.getexif()

    # Orientation is now applied to pixels, so do not preserve that instruction.
    exif.pop(274, None)
    resized = image.resize(
        (image.width * SCALE, image.height * SCALE),
        Image.Resampling.LANCZOS,
    )

    save_options = {}
    if image_format in {"JPEG", "PNG"}:
        save_options["dpi"] = TARGET_DPI
    if icc_profile:
        save_options["icc_profile"] = icc_profile
    if exif:
        save_options["exif"] = exif.tobytes()
    if image_format == "JPEG":
        if resized.mode not in {"RGB", "L"}:
            resized = resized.convert("RGB")
        save_options.update(quality=95, subsampling=0, optimize=True)
    elif image_format == "PNG":
        save_options["optimize"] = True

    resized.save(image_path, format=image_format, **save_options)
