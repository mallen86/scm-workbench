# Image post-processing

Image post-processing runs an approved Python callback against selected card images before Create PDF. Simple mode offers the ready, no-download **Simple Upscaler (1200 PPI)** and a one-time **Install model & libraries** action for the app-owned **Advanced Upscaler (AI 4×)**. It can also run other processors already trusted and installed in Advanced mode, but cannot install arbitrary libraries or edit/trust custom code. Advanced mode provides the Processor library for creating, editing, reviewing, and installing optional libraries. The **Guide** button opens this document bundled with the running Workbench version.

## Choosing individual cards

Select **Choose cards** beside the image-scope options in either mode. Search by card name, check the images you want, and use **Next** / **Previous** to browse 12 images at a time. **Select all matches** includes every search result, not just the current page; **Selected only** reviews your choices and **Clear selection** starts over. Front, double-sided, and back images have separate labels and checkboxes.

Selections survive changing scope or interface mode and can be restored from Job history. **Refresh** reloads the folder list without silently changing your selection. If a selected image has disappeared, clear the selection and choose again. Only checked images are staged and processed; preview and run both reject missing or unsafe selections. No selection is made by default.

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

## Resource limits and clear failures

Image-processing jobs do not have a cumulative CPU-time quota. Simple Upscaler and custom processors retain a one-hour elapsed-time limit and a 15-minute no-activity limit; the fixed Advanced Upscaler retains its existing exemption from both. Library installation keeps its separate limits, and operating systems or app supervisors can impose their own restrictions.

On Windows, the two fixed bundled upscalers use an adaptive committed-memory budget: at most **16 GiB**, at most half the installed RAM, and no more than available physical RAM or system commit capacity after reserving **4 GiB or 20% of installed RAM**, whichever is larger. They require at least 1 GiB of safe processing capacity. Workbench checks headroom after staging and again before loading the processor, enforces an aggregate Windows Job Object cap, and monitors physical and commit headroom while processing. Low memory or unavailable memory telemetry stops the job rather than publishing partially processed images. Custom processors, including duplicated upscalers, retain their 4-GiB ceiling. Only one image-processing job can run within a Workbench worker at a time. These protections reduce risk; other applications and GPU drivers can still consume memory, and GPU VRAM is a separate budget.

Both modes show a failed job's bounded, plain-language reason on the Image post-processing page and in its sidebar notice. The reason remains visible in Job history after restarting the app, without opening the Advanced-only console. Memory failures suggest closing other applications or using smaller original images; timeouts suggest smaller batches. Oversized images are reported as size-limit failures, not as corrupt images. Both bundled upscalers reject an unsupported output size before decoding/resizing or AI inference: Simple checks its metadata-derived or standard-MTG fallback dimensions and Advanced checks its fixed 4× dimensions. More RAM does not remove the image/file safety limits or make repeated upscaling appropriate. Failure or cancellation before publication leaves the originals unchanged.

## Optional Advanced Upscaler: AI 4×

The app includes a read-only, app-trusted RealESRGAN_x4plus processor, but **not** its 67 MB model or ONNX Runtime wheels. The model and inference libraries are required to use the Advanced Upscaler. Select it on Image post-processing in either mode and choose **Install model & libraries**. Workbench asks for confirmation, advises keeping at least **2 GB free during installation** for platform-dependent libraries and temporary download/staging space, and runs a cancellable job. The Image post-processing page shows the running job's elapsed time and latest installer step, and keeps failure details visible when you return; if a job cannot start, it shows the reason rather than implying installation is running. Installation uses the pinned model URL at an immutable revision, an exact expected length and SHA-256, compatible wheel-only PyPI downloads with hash-locked offline installation, and an atomic ready marker. The model is stored under Workbench data, never inside the app or the managed SCM checkout. Installation does not require an SCM checkout; processing images still requires its image folders. It can be removed from either mode. App and bundled processor code updates reuse the installation when library requirements are unchanged and the existing libraries, pinned model, and Python compatibility checks pass. This includes code-only beta updates and preserves the installed CUDA profile without downloading or rebuilding libraries. Changed requirements, model pins, processor contracts, or Python ABI, and missing or damaged files, can still require repair or reinstallation. Removing the optional installation is respected; an update never silently reinstalls or reactivates it.

Once installed, runs never fetch models or packages. The same processor selects an available hardware backend and keeps ONNX Runtime's CPU provider as a fallback:

- macOS: CoreML when available, otherwise CPU.
- Windows: DirectML on compatible GPUs, otherwise CPU. DirectML sessions disable memory patterns and use sequential execution as required by that provider.
- Linux x86_64: choose **Auto** (default), **CUDA 12**, or **CUDA 13** before installing. Auto recommends CUDA 13 when both the CUDA 13 runtime and BLAS libraries are visible, otherwise CUDA 12 when its pair is visible; if neither complete pair is visible, it defaults to CUDA 12 for CPU fallback. Detection checks bounded standard system loader paths, the loader cache, and validated absolute `LD_LIBRARY_PATH` entries passed to the fixed runner, **not** the driver-advertised CUDA version from `nvidia-smi` or `nvcc`. An otherwise invisible `/usr/local/cuda*` toolkit directory alone does not select a profile; add its library directory to the loader configuration or start Workbench with that absolute directory in `LD_LIBRARY_PATH`. It does not prove that cuDNN 9, the driver, or a usable GPU exists. Select the matching major manually for custom library setups. The CUDA 12 profile pins ONNX Runtime GPU 1.26.0; CUDA 13 pins 1.30.0. Both include CPU fallback. Workbench **does not** download CUDA, cuDNN, or the NVIDIA driver. If the libraries are not on the system loader path, start Workbench with their absolute directories in `LD_LIBRARY_PATH`; the fixed built-in runner passes those directories to the job. Other Linux GPU families currently use CPU. An existing installation stays on its installed profile until you explicitly reinstall or switch; failed or cancelled switches leave it available. The selected profile and detected recommendation are shown before confirmation; changing system libraries alone does not switch it.

If GPU initialization fails—even when ONNX Runtime only logs an error and silently selects CPU—Workbench continues automatically on CPU. If CUDA initializes but its first (or a later) tile fails during inference, the fixed upscaler creates a separate CPU-only session and retries that tile once, then uses that session for the remaining images. Missing `libcudnn.so` is identified specifically; other GPU failures show a generic fallback warning. Both modes show the warning and active provider on the Image post-processing page and in the job notice. Initialization and tile checkpoints show work within the current image; the image-count progress bar advances only when a whole image finishes, then reports validation before publication. CPU inference can be slow, and **Cancel processing** remains available in Simple mode (use the job console's stop control in Advanced mode). A failed CPU session or CPU retry still fails the job without publishing its staged changes. A source-only update does not require **Install model & libraries** again when the verified installation remains compatible; the installed CUDA profile does not switch on a status read.

The fixed Advanced Upscaler has no Workbench CPU-time, wall-clock, or idle timeout during image staging and processing; its CPU inference uses the logical CPUs available to the process. This does not guarantee full utilization or a faster run. Memory, disk, file, process, and image safety limits still apply, as do any limits imposed by the operating system or app supervisor. Simple/custom processors retain their elapsed and idle limits, but no image processor has a Workbench cumulative CPU-time quota. Library installation retains its separate policies.

The optional dependency installer pins platform-specific ONNX Runtime wheels, resolves only compatible binary wheels, verifies their hashes, and publishes them privately; it never installs into the app bundle or system Python. GPU availability depends on the device and drivers; a GPU provider that fails to initialize also falls back to CPU. The fixed Linux AI runner has a bounded 16 GiB virtual address-space allowance because CUDA reserves more than 8 GiB even for a single tile; custom processors retain the 4 GiB allowance. The existing model and library tree are verified before carrying an installation forward to new app-owned source. Inference is tiled to bound memory, retains the real input format even for JPEG data with a `.png` filename, and sets JPEG/PNG output metadata to 1200 DPI. It increases each input dimension by four; reruns multiply it again, so restore or re-fetch the original images before using Advanced on an already processed deck. For large decks and CPU-only machines AI inference can take considerably longer than the Simple Upscaler. PNG output uses standard lossless compression instead of slower size optimization; pixels and metadata are unchanged, though files may be modestly larger. Both upscalers retain JPEG quality 95 and full chroma detail but avoid memory-heavy JPEG encoding optimization. Failure or cancellation leaves the original image batch unchanged.

The model originates with Real-ESRGAN (BSD 3-Clause, Xintao Wang). Its conversion provenance and model hash are linked in [the bundled attribution](licenses/Real-ESRGAN.txt). The app does not use PyTorch, Torchvision, or OpenCV for this built-in. In Advanced mode, **Duplicate** creates an editable, untrusted source copy. The copy does not inherit Workbench's verified model path or installed libraries: to run it, supply your own model path in its source, install its required libraries, and trust the revision. Only the fixed, app-owned processor receives Workbench's verified model path; installing its assets does not authorize arbitrary custom installations in Simple mode.

## Built-in Simple Upscaler: 1200 PPI with standard-MTG fallback

The Simple Upscaler uses the image's **actual, EXIF-oriented pixel dimensions** and honors **valid embedded PPI/DPI metadata**. For valid resolution below 1200 PPI, it multiplies both dimensions by `1200 / input PPI`, rounds each result to the nearest whole pixel (at least one pixel), and resamples with Pillow's Lanczos filter. It does not invent detail like an AI super-resolution model.

When embedded resolution is missing or unusable, it assumes a **standard MTG card, 63 × 88 mm**, and targets approximately 1200 PPI. The target box is `round(63 / 25.4 × 1200)` by `round(88 / 25.4 × 1200)` = **2976 × 4157 pixels**, swapped for landscape-oriented images. It uses one aspect-preserving scale, `min(target width / oriented width, target height / oriented height)`, and rounds both resulting dimensions to whole pixels. It fits **inside** that box without stretching, cropping, or downscaling. If that scale is at or below 1, the original file stays byte-for-byte unchanged and is reported as a skip. Thus an image with either oriented dimension already meeting its corresponding fit target is not enlarged, even if the other dimension is smaller.

This is an approximation, not measured physical resolution. Images with different aspect ratios or up to 3 mm bleed per edge are accepted as-is; Workbench does **not** detect or remove bleed or guarantee exactly 1200 effective PPI for that art. The fallback assumption and image name are recorded in the job log, while this policy is explained in both UI modes.

| Embedded input PPI | Pixel-dimension multiplier |
| --- | --- |
| 300 | 4× |
| 600 | 2× |
| 800 | 1.5× |
| 1200 or higher | Unchanged, including original bytes and metadata |

The processor applies EXIF orientation to pixels before resizing and uses those oriented dimensions. It retains the actual image format, including JPEG data under a `.png` filename, ICC color profile, and unrelated EXIF metadata. Resized JPEG/PNG/BMP files carry 1200-DPI metadata; any existing EXIF resolution/dimension fields are updated and the applied orientation instruction is removed. JPEG retains quality 95, full chroma detail, and non-optimized encoding to avoid its extra memory peak. Source buffers are explicitly closed before encoding.

Resolution must be a finite, positive pair with matching X/Y axes. The processor accepts physical JFIF density and EXIF resolution in inches or centimetres, and physical density exposed by Pillow for formats such as PNG/BMP. It does not use Pillow's fabricated 72-DPI JPEG fallback. Missing, invalid, unequal-axis, or conflicting resolution metadata uses the **standard-MTG fallback** described above. Unreadable EXIF cannot supply orientation or be preserved; otherwise unrelated readable EXIF is retained. Dimension rounding and the tiny density quantization of PNG/BMP can affect fractional results by one pixel; a 0.02-PPI tolerance recognizes encoded 1200 DPI, so rerunning Simple does not enlarge or re-encode its own output. Matching axes use only a tiny floating-point tolerance, not independent X/Y scaling.

An unsupported metadata-derived or fallback output size fails the batch before allocating output pixels. Failure or cancellation before publication leaves the original batch unchanged. Skips are successful no-op callbacks, so validation/publication still completes for the other images.

### Skip counts, reasons, and progress

Both **Simple and Advanced modes** show a skipped-image count and named reasons on Image post-processing, in the sidebar notice, and in persisted Job history. The sidebar shows a compact first reason; the processing page and history retain up to eight named reasons within a 4-KiB summary. If more images were skipped, the summary says how many additional reasons remain in the job log (available through the Advanced console). Each skip diagnostic includes the SCM-relative image name and explains an image already at/above 1200 PPI or already meeting the fallback fit target. Missing/unusable metadata that results in a resize is logged as a standard-MTG assumption, not counted as a skip.

Completed-image progress still counts completed callbacks, including successful skips. A finished job does **not** mean that every image was resized: mixed batches save only changed results, and an all-skipped batch succeeds with **originals unchanged**. Skip details supplement, never replace, failure details and rollback warnings.

The callback return contract remains `None`. The runner recognizes the fixed bundled Simple source and privately supplies its one-use skip reporter. After a successful callback, it emits a bounded `WB_POSTPROCESS_SKIP` event immediately before normal callback progress. The server accepts those events only for the app-owned Simple job, validates the exact next staged image identity and count, rejects duplicates and malformed/oversized reasons, and derives the bounded public summary. Custom processors cannot authorize skip summaries by printing such frames or returning a different result type.

Run it as follows:

1. Fetch the deck's card images or import a card back in Create PDF. Images without usable embedded PPI/DPI use the standard-MTG size assumption; check that this approximation suits your images.
2. Open **Image post-processing** and choose **Simple Upscaler (1200 PPI)**.
3. Select **Front and double-sided** (the default), **Front only**, **Double-sided only**, or **Back only**. Back only processes recognized images directly in `game/back`; the default never includes backs. Confirm the reported image count, then run the processor.
4. Wait for validation and transactional publication to finish. In Simple mode, **Cancel processing** is available while the job runs.
5. Open **Create PDF**, set **Resolution (PPI)** to `1200`, preview, and create the PDF. The 1200-DPI image tag does not control physical print size: Create PDF's selected card size and bleed/crop/fit options determine how many source pixels cover each printed inch. Fallback art, especially art with bleed or a different ratio, may therefore print at a different effective PPI. Inspect skipped images separately if they need higher-resolution art.

For example, a 745 × 1040 image with valid embedded 300 PPI becomes 2980 × 4160 pixels; at 600 PPI it becomes 1490 × 2080. With no usable embedded resolution it instead becomes 2976 × 4154 pixels using the standard-MTG fit. A 69:94 image ratio (63 × 88 mm plus 3 mm per edge) fits to 2976 × 4054 pixels, retaining all bleed rather than stretching to the target box.

Simple reruns leave images at or above 1200 PPI untouched, including images produced by older Workbench upscalers that wrote 1200 DPI. **Advanced Upscaler remains fixed AI 4×** and still multiplies pixel dimensions on every run; restore or re-fetch originals before rerunning Advanced. Advanced mode shows the bundled source as read-only. Choose **Duplicate** there for an editable copy, then review and trust the resulting custom revision before running it.
