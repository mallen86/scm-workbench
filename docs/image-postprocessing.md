# Image post-processing

Image post-processing runs an approved Python callback against fetched card images before Create PDF. Simple mode can select and run processors that are already trusted and ready, including the built-in **Simple Upscaler (4×)**. Advanced mode also provides the Processor library for creating, editing, reviewing, and installing optional libraries. The **Guide** button in that library opens a rendered copy of this document bundled with the running Workbench version.

## Trust and safety

Processors and installed libraries run as your desktop user account. Workbench runs them in a separate bounded process, gives the callback private staged image copies, validates every result, and publishes the batch only after every image succeeds. Those protections prevent ordinary failures from leaving a partially changed image set, but they do **not** make arbitrary Python safe. Only trust code and packages you have reviewed.

The built-in Simple Upscaler is read-only, app-trusted, and uses Pillow from the bundled runtime, so it needs no separate installation. It can be duplicated in Advanced mode when a customized version is needed; that duplicate starts untrusted like any user processor.

Saving user source creates an immutable, untrusted revision. Trust applies only to that exact source revision and dependency environment; an edit or dependency change requires approval again. **Revert** lists the processor's bounded immutable history. Loading an older revision replaces the editor contents but does not reactivate or trust it; review it and choose **Save revision** to make it current.

Opening a post-processing job from **Job history** selects the exact processor recorded by that job. If the processor has since been deleted, Workbench leaves the library unselected instead of silently opening a different processor.

## Callback contract

A processor defines one synchronous function:

```python
from pathlib import Path


def process_image(image_path: Path, context: dict) -> None:
    """Modify the private working copy at image_path in place."""
```

Workbench loads the module once, then calls `process_image` once for each staged image. The script should not scan SCM folders or loop over card files itself.

`context` contains:

- `role`: `front` or `double_sided`;
- `relative_path`: the image's non-absolute SCM-relative name;
- `name`: the basename;
- `index`: the one-based callback number;
- `total`: the total images in the job.

The callback must return `None`, retain the filename and image format, and save a valid bounded image back to `image_path`. SCM can fetch JPEG data under a `.png` filename; Workbench recognizes these files by their actual JPEG content and requires the processor to preserve that JPEG format. Other mismatched extensions are excluded. If any callback fails, Workbench leaves the original batch unchanged.

## Optional libraries

Enter one package request per line in the processor editor, using a package name with an optional exact version, for example:

```text
opencv-python==4.12.0.88
```

Workbench accepts compatible wheel packages only and installs them into a private, interpreter-compatible environment below Workbench data. It never installs them into the app bundle or system Python. URLs, local paths, VCS packages, custom indexes, pip options, and source builds are not accepted.

Save the revision before choosing **Install / update libraries**. Review and confirm the package list, wait for the dependency job to finish, then trust the resulting exact revision/environment. Choosing this action again resolves the currently compatible wheel set. Existing trust is preserved only when the verified dependency fingerprint and installed tree are unchanged; otherwise you must trust the new environment. A source-only edit with unchanged requirements reuses the installed environment, so you only need to review and trust the new source revision.

The interpreter fingerprint follows wheel compatibility: Python implementation and major/minor version, ABI tags, platform, and architecture. Rebuilding, relocating, or updating the app with a compatible runtime—including a Python patch update—preserves the verified environment and its trust. A genuinely incompatible Python minor version, ABI, platform, or architecture marks the old libraries as needing reinstallation while keeping the processor source available to review and edit.

The packaged runtime already includes Pillow, so Pillow-only processors normally need no additional requirement.

## Built-in Simple Upscaler: 300 to 1200 PPI

The processor shipped with Workbench increases both pixel dimensions by four using Pillow's Lanczos resampler and always writes 1200-DPI metadata for JPEG and PNG files. It does not multiply the source image's DPI, so a 400-DPI input is still marked as 1200 DPI after scaling—not 1600 DPI. It does not invent new detail like an AI super-resolution model would. Its bundled source is shown below for review.

```python
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
```

Run it as follows:

1. Fetch the deck's card images normally.
2. Open **Image post-processing** and choose **Simple Upscaler (4×)**.
3. Select **Front and double-sided** (or a narrower scope), confirm the reported image count, then run the processor.
4. Wait for validation and transactional publication to finish.
5. Open **Create PDF**, set **Resolution (PPI)** to `1200`, preview, and create the PDF.

The upscaler scales every selected image each time it runs; it does not detect previous runs. If an older beta processed only some files, restore or re-fetch the original images before running it again, or the previously processed images will be enlarged a second time (16× their original dimensions). Advanced mode shows the bundled source as read-only. Choose **Duplicate** there if you want an editable copy, then review and trust the resulting custom revision before running it.

[Scryfall's image documentation](https://scryfall.com/docs/api/images) lists PNGs as 744 × 1040 pixels, while individual files currently returned by the API can be 745 × 1040. The processor scales the actual file dimensions: those examples become 2976 × 4160 or 2980 × 4160 pixels, respectively, close to the 3000 × 4200 pixels needed for a 2.5 × 3.5 inch card at 1200 PPI.
