"""App-owned RealESRGAN_x4plus 4× processor; assets install only on request.

The model is supplied by Workbench's verified private environment. This
module never downloads anything or resolves model paths from the network.
"""
from pathlib import Path
import json
import os
import sys

from PIL import Image, ImageOps
import numpy as np
import onnxruntime as ort

SCALE = 4
TILE = 128
PAD = 24
DPI = (1200, 1200)
_session = None
_input_name = None
_provider = None
_fallback = False
ACTIVITY_PREFIX = "WB_ADVANCED_UPSCALER_ACTIVITY "


def _activity(context, phase, provider, tile=0, tiles=0):
    # Only the fixed built-in emits this bounded status; the runner retains
    # sole ownership of completed-image progress and the callback contract.
    frame = {"index": context["index"], "total": context["total"],
             "name": context["name"], "role": context["role"],
             "phase": phase, "provider": provider, "tile": tile, "tiles": tiles}
    print(ACTIVITY_PREFIX + json.dumps(frame, ensure_ascii=False, separators=(",", ":")), flush=True)


def _providers(available, platform: str) -> list[str]:
    if platform == "darwin":
        gpu = "CoreMLExecutionProvider"
    elif platform == "win32":
        gpu = "DmlExecutionProvider"
    elif platform.startswith("linux"):
        gpu = "CUDAExecutionProvider"
    else:
        gpu = None
    return [gpu, "CPUExecutionProvider"] if gpu in available else ["CPUExecutionProvider"]


def _available_cpus() -> int:
    # Python 3.13 accounts for the CPUs available to this process. Older
    # runtimes use the affinity mask when supported, then the machine count.
    process_count = getattr(os, "process_cpu_count", None)
    if callable(process_count):
        try:
            count = process_count()
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                return count
        except (OSError, ValueError):
            pass
    affinity = getattr(os, "sched_getaffinity", None)
    if callable(affinity):
        try:
            count = len(affinity(0))
            if count > 0:
                return count
        except (OSError, ValueError):
            pass
    count = os.cpu_count()
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 1


def _model(path: str, context: dict | None = None):
    global _session, _input_name, _provider, _fallback
    if _session is not None:
        if context is not None:
            _activity(context, "initializing", _provider)
        return _session
    if _session is None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = _available_cpus()
        # Tiles run sequentially; do not multiply intra-op workers by a
        # second inter-op pool. DirectML also requires sequential execution.
        options.inter_op_num_threads = 1
        providers = _providers(ort.get_available_providers(), sys.platform)
        if providers[0] == "DmlExecutionProvider":
            # DirectML requires sequential execution with memory patterns off.
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if context is not None:
            _activity(context, "initializing", providers[0])
        try:
            session = ort.InferenceSession(path, sess_options=options, providers=providers)
        except Exception:
            if len(providers) == 1:
                raise
            # An advertised GPU provider may lack system libraries or a driver.
            session = ort.InferenceSession(path, sess_options=options,
                                           providers=["CPUExecutionProvider"])
        # ORT can log a CUDA loader error yet return a CPU-only session. The
        # requested/advertised list is not evidence that GPU inference works.
        active = session.get_providers()
        if not isinstance(active, (tuple, list)) or not active:
            raise ValueError("advanced upscaler has no active execution provider")
        gpu = providers[0] if len(providers) > 1 else None
        if gpu in active:
            _provider = gpu
        elif "CPUExecutionProvider" in active:
            _provider = "CPUExecutionProvider"
            _fallback = bool(gpu or sys.platform.startswith("linux"))
            if _fallback and context is not None:
                _activity(context, "fallback", _provider)
        else:
            raise ValueError("advanced upscaler has no usable execution provider")
        inputs = session.get_inputs()
        if len(inputs) != 1 or len(session.get_outputs()) != 1:
            raise ValueError("advanced upscaler model has unexpected inputs or outputs")
        _input_name = inputs[0].name
        _session = session
    return _session


def process_image(image_path: Path, context: dict) -> None:
    global _provider, _fallback
    model_path = context.get("model_path")
    if not isinstance(model_path, str) or not model_path:
        raise ValueError("the Advanced Upscaler model is not installed")
    session = _model(model_path, context)
    with Image.open(image_path) as opened:
        image_format = opened.format  # SCM may name actual JPEG content .png.
        icc = opened.info.get("icc_profile")
        image = ImageOps.exif_transpose(opened)
        image.load()
        exif = image.getexif()
    exif.pop(274, None)
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    rgb = image.convert("RGB")
    width, height = rgb.size
    output = Image.new("RGB", (width * SCALE, height * SCALE))
    tiles = ((width + TILE - 1) // TILE) * ((height + TILE - 1) // TILE)
    completed = reported = 0
    _activity(context, "tile", _provider, 0, tiles)
    for top in range(0, height, TILE):
        for left in range(0, width, TILE):
            right, bottom = min(left + TILE, width), min(top + TILE, height)
            x0, y0 = max(0, left - PAD), max(0, top - PAD)
            x1, y1 = min(width, right + PAD), min(height, bottom + PAD)
            pixels = np.asarray(rgb.crop((x0, y0, x1, y1)), dtype=np.float32) / 255.0
            # Reflection supplies context at physical edges without blending
            # over the core tile or producing seam artifacts at tile borders.
            pad_left, pad_top = max(0, PAD - left), max(0, PAD - top)
            pad_right, pad_bottom = max(0, right + PAD - width), max(0, bottom + PAD - height)
            if any((pad_left, pad_top, pad_right, pad_bottom)):
                edge = "reflect" if pixels.shape[0] > 1 and pixels.shape[1] > 1 else "edge"
                pixels = np.pad(pixels, ((pad_top, pad_bottom), (pad_left, pad_right), (0, 0)), mode=edge)
            tensor = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
            predicted = session.run(None, {_input_name: tensor})[0]
            # Some ORT builds switch providers after a runtime inference fault.
            active = session.get_providers()
            if (_provider != "CPUExecutionProvider" and
                    _provider not in active and "CPUExecutionProvider" in active):
                _provider = "CPUExecutionProvider"
                _fallback = True
                # Match the last published checkpoint, including when tile
                # throttling omitted the most recent completed tiles.
                _activity(context, "fallback", _provider, reported, tiles)
            crop_x = (left - x0 + pad_left) * SCALE
            crop_y = (top - y0 + pad_top) * SCALE
            core = predicted[0, :, crop_y:crop_y + (bottom - top) * SCALE,
                             crop_x:crop_x + (right - left) * SCALE]
            if core.shape != (3, (bottom - top) * SCALE, (right - left) * SCALE):
                raise ValueError("advanced upscaler produced an invalid tile")
            tile = Image.fromarray(np.uint8(np.clip(core.transpose(1, 2, 0), 0, 1) * 255 + 0.5), "RGB")
            output.paste(tile, (left * SCALE, top * SCALE))
            completed += 1
            # At most 32 intermediate updates plus start/end per image.
            if completed == tiles or completed == 1 or completed * 32 // tiles != (completed - 1) * 32 // tiles:
                _activity(context, "tile", _provider, completed, tiles)
                reported = completed
    if alpha is not None and image_format == "PNG":
        output.putalpha(alpha.resize(output.size, Image.Resampling.LANCZOS))
    options = {"dpi": DPI}
    if icc:
        options["icc_profile"] = icc
    if exif:
        options["exif"] = exif.tobytes()
    if image_format == "JPEG":
        options.update(quality=95, subsampling=0, optimize=True)
    elif image_format == "PNG":
        # Extra PNG size optimization takes nearly as long as inference on
        # large images; standard lossless compression keeps identical pixels.
        options["optimize"] = False
    output.save(image_path, format=image_format, **options)
