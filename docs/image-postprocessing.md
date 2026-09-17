# Image post-processing

Image post-processing is an Advanced-mode workflow that runs a user-owned Python callback against fetched card images before Create PDF.

## Trust and safety

Processors and installed libraries run as your desktop user account. Workbench runs them in a separate bounded process, gives the callback private staged image copies, validates every result, and publishes the batch only after every image succeeds. Those protections prevent ordinary failures from leaving a partially changed image set, but they do **not** make arbitrary Python safe. Only trust code and packages you have reviewed.

Saving source creates an immutable, untrusted revision. Trust applies only to that exact source revision and dependency environment; an edit or dependency change requires approval again.

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

The callback must return `None`, retain the filename and image format, and save a valid bounded image back to `image_path`. If any callback fails, Workbench leaves the original batch unchanged.

## Optional libraries

Enter one package request per line in the processor editor, using a package name with an optional exact version, for example:

```text
opencv-python==4.12.0.88
```

Workbench accepts compatible wheel packages only and installs them into a private, interpreter-specific environment below Workbench data. It never installs them into the app bundle or system Python. URLs, local paths, VCS packages, custom indexes, pip options, and source builds are not accepted.

Save the revision before choosing **Install / update libraries**. Review and confirm the package list, wait for the dependency job to finish, then trust the resulting exact revision/environment. Choosing this action again resolves the currently compatible wheel set. Existing trust is preserved only when the verified dependency fingerprint and installed tree are unchanged; otherwise you must trust the new environment.

The packaged runtime already includes Pillow, so Pillow-only processors normally need no additional requirement.

## Example: upscale Scryfall images from 300 to 1200 PPI

This example increases both pixel dimensions by four using Pillow's Lanczos resampler and writes 1200-DPI metadata for JPEG and PNG files. It does not invent new detail like an AI super-resolution model would.

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

1. Switch Workbench to **Advanced** mode.
2. Fetch the deck's Scryfall images normally.
3. Open **Image post-processing** and choose **New processor**.
4. Name it `Scryfall 4x upscale`, paste the code, and leave requirements blank.
5. Save the revision and review the exact source.
6. Because requirements are blank, the private environment is already ready; trust the exact revision. If the UI does not show **Libraries ready**, choose **Install / update libraries**, wait for that job, and then trust it.
7. Select **Front and double-sided**, confirm the reported image count, then run the processor.
8. Wait for validation and transactional publication to finish.
9. Open **Create PDF**, set **Resolution (PPI)** to `1200`, preview, and create the PDF.

[Scryfall's image documentation](https://scryfall.com/docs/api/images) lists PNGs as 744 × 1040 pixels, while individual files currently returned by the API can be 745 × 1040. The processor scales the actual file dimensions: those examples become 2976 × 4160 or 2980 × 4160 pixels, respectively, close to the 3000 × 4200 pixels needed for a 2.5 × 3.5 inch card at 1200 PPI.
