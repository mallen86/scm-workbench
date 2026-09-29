# Image post-processing

Image post-processing runs an approved Python callback against selected card images before Create PDF. Simple mode offers the ready, no-download **Simple Upscaler (4×)** and a one-time **Install model & libraries** action for the app-owned **Advanced Upscaler (AI 4×)**. It can also run other processors already trusted and installed in Advanced mode, but cannot install arbitrary libraries or edit/trust custom code. Advanced mode provides the Processor library for creating, editing, reviewing, and installing optional libraries. The **Guide** button opens this document bundled with the running Workbench version.

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

- `role`: `front`, `double_sided`, or `back`;
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

## Optional Advanced Upscaler: AI 4×

The app includes a read-only, app-trusted RealESRGAN_x4plus processor, but **not** its 67 MB model or ONNX Runtime wheels. The model and inference libraries are required to use the Advanced Upscaler. Select it on Image post-processing in either mode and choose **Install model & libraries**. Workbench asks for confirmation, advises keeping at least **2 GB free during installation** for platform-dependent libraries and temporary download/staging space, and runs a cancellable job. The Image post-processing page shows the running job's elapsed time and latest installer step, and keeps failure details visible when you return; if a job cannot start, it shows the reason rather than implying installation is running. Installation uses the pinned model URL at an immutable revision, an exact expected length and SHA-256, compatible wheel-only PyPI downloads with hash-locked offline installation, and an atomic ready marker. The model is stored under Workbench data, never inside the app or the managed SCM checkout. Installation does not require an SCM checkout; processing images still requires its image folders. It can be removed from either mode. App and bundled processor code updates reuse the installation when library requirements are unchanged and the existing libraries, pinned model, and Python compatibility checks pass. This includes code-only beta updates and preserves the installed CUDA profile without downloading or rebuilding libraries. Changed requirements, model pins, processor contracts, or Python ABI, and missing or damaged files, can still require repair or reinstallation. Removing the optional installation is respected; an update never silently reinstalls or reactivates it.

Once installed, runs never fetch models or packages. The same processor selects an available hardware backend and keeps ONNX Runtime's CPU provider as a fallback:

- macOS: CoreML when available, otherwise CPU.
- Windows: DirectML on compatible GPUs, otherwise CPU. DirectML sessions disable memory patterns and use sequential execution as required by that provider.
- Linux x86_64: choose **Auto** (default), **CUDA 12**, or **CUDA 13** before installing. Auto recommends CUDA 13 when both the CUDA 13 runtime and BLAS libraries are visible, otherwise CUDA 12 when its pair is visible; if neither complete pair is visible, it defaults to CUDA 12 for CPU fallback. Detection checks bounded standard system loader paths, the loader cache, and validated absolute `LD_LIBRARY_PATH` entries passed to the fixed runner, **not** the driver-advertised CUDA version from `nvidia-smi` or `nvcc`. An otherwise invisible `/usr/local/cuda*` toolkit directory alone does not select a profile; add its library directory to the loader configuration or start Workbench with that absolute directory in `LD_LIBRARY_PATH`. It does not prove that cuDNN 9, the driver, or a usable GPU exists. Select the matching major manually for custom library setups. The CUDA 12 profile pins ONNX Runtime GPU 1.26.0; CUDA 13 pins 1.30.0. Both include CPU fallback. Workbench **does not** download CUDA, cuDNN, or the NVIDIA driver. If the libraries are not on the system loader path, start Workbench with their absolute directories in `LD_LIBRARY_PATH`; the fixed built-in runner passes those directories to the job. Other Linux GPU families currently use CPU. An existing installation stays on its installed profile until you explicitly reinstall or switch; failed or cancelled switches leave it available. The selected profile and detected recommendation are shown before confirmation; changing system libraries alone does not switch it.

If GPU initialization fails—even when ONNX Runtime only logs an error and silently selects CPU—Workbench continues automatically on CPU. If CUDA initializes but its first (or a later) tile fails during inference, the fixed upscaler creates a separate CPU-only session and retries that tile once, then uses that session for the remaining images. Missing `libcudnn.so` is identified specifically; other GPU failures show a generic fallback warning. Both modes show the warning and active provider on the Image post-processing page and in the job notice. Initialization and tile checkpoints show work within the current image; the image-count progress bar advances only when a whole image finishes, then reports validation before publication. CPU inference can be slow, and **Cancel processing** remains available in Simple mode (use the job console's stop control in Advanced mode). A failed CPU session or CPU retry still fails the job without publishing its staged changes. A source-only update does not require **Install model & libraries** again when the verified installation remains compatible; the installed CUDA profile does not switch on a status read.

The fixed Advanced Upscaler has no Workbench CPU-time, wall-clock, or idle timeout during image staging and processing; its CPU inference uses the logical CPUs available to the process. This does not guarantee full utilization or a faster run. Memory, disk, file, process, and image safety limits still apply, as do any limits imposed by the operating system or app supervisor. Custom processors and library installation retain their time limits.

The optional dependency installer pins platform-specific ONNX Runtime wheels, resolves only compatible binary wheels, verifies their hashes, and publishes them privately; it never installs into the app bundle or system Python. GPU availability depends on the device and drivers; a GPU provider that fails to initialize also falls back to CPU. The fixed Linux AI runner has a bounded 16 GiB virtual address-space allowance because CUDA reserves more than 8 GiB even for a single tile; custom processors retain the 4 GiB allowance. The existing model and library tree are verified before carrying an installation forward to new app-owned source. Inference is tiled to bound memory, retains the real input format even for JPEG data with a `.png` filename, and sets JPEG/PNG output metadata to 1200 DPI. It increases each input dimension by four; reruns multiply it again, so restore or re-fetch the original images before switching upscalers on an already processed deck. For large decks and CPU-only machines AI inference can take considerably longer than the Simple Upscaler. PNG output uses standard lossless compression instead of slower size optimization; pixels and metadata are unchanged, though files may be modestly larger. Failure or cancellation leaves the original image batch unchanged.

The model originates with Real-ESRGAN (BSD 3-Clause, Xintao Wang). Its conversion provenance and model hash are linked in [the bundled attribution](licenses/Real-ESRGAN.txt). The app does not use PyTorch, Torchvision, or OpenCV for this built-in. In Advanced mode, **Duplicate** creates an editable, untrusted source copy. The copy does not inherit Workbench's verified model path or installed libraries: to run it, supply your own model path in its source, install its required libraries, and trust the revision. Only the fixed, app-owned processor receives Workbench's verified model path; installing its assets does not authorize arbitrary custom installations in Simple mode.

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

1. Fetch the deck's card images or import a card back in Create PDF.
2. Open **Image post-processing** and choose **Simple Upscaler (4×)**.
3. Select **Front and double-sided** (the default), **Front only**, **Double-sided only**, or **Back only**. Back only processes recognized images directly in `game/back`; the default never includes backs. Confirm the reported image count, then run the processor.
4. Wait for validation and transactional publication to finish. In Simple mode, **Cancel processing** is available while the job runs; if cancellation succeeds before publication, the original images remain unchanged.
5. Open **Create PDF**, set **Resolution (PPI)** to `1200`, preview, and create the PDF.

The upscaler scales every selected image each time it runs; it does not detect previous runs. If an older beta processed only some files, restore or re-fetch the original images before running it again, or the previously processed images will be enlarged a second time (16× their original dimensions). Advanced mode shows the bundled source as read-only. Choose **Duplicate** there if you want an editable copy, then review and trust the resulting custom revision before running it.

[Scryfall's image documentation](https://scryfall.com/docs/api/images) lists PNGs as 744 × 1040 pixels, while individual files currently returned by the API can be 745 × 1040. The processor scales the actual file dimensions: those examples become 2976 × 4160 or 2980 × 4160 pixels, respectively, close to the 3000 × 4200 pixels needed for a 2.5 × 3.5 inch card at 1200 PPI.
