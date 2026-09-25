"""App-owned RealESRGAN_x4plus 4× processor; assets install only on request.

The model is supplied by Workbench's verified private environment. This
module never downloads anything or resolves model paths from the network.
"""
from pathlib import Path
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


def _model(path: str):
    global _session, _input_name
    if _session is None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        options.inter_op_num_threads = 1
        providers = (["CoreMLExecutionProvider", "CPUExecutionProvider"]
                     if sys.platform == "darwin" and "CoreMLExecutionProvider" in ort.get_available_providers()
                     else ["CPUExecutionProvider"])
        _session = ort.InferenceSession(path, sess_options=options, providers=providers)
        inputs = _session.get_inputs()
        if len(inputs) != 1 or len(_session.get_outputs()) != 1:
            raise ValueError("advanced upscaler model has unexpected inputs or outputs")
        _input_name = inputs[0].name
    return _session


def process_image(image_path: Path, context: dict) -> None:
    model_path = context.get("model_path")
    if not isinstance(model_path, str) or not model_path:
        raise ValueError("the Advanced Upscaler model is not installed")
    session = _model(model_path)
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
            crop_x = (left - x0 + pad_left) * SCALE
            crop_y = (top - y0 + pad_top) * SCALE
            core = predicted[0, :, crop_y:crop_y + (bottom - top) * SCALE,
                             crop_x:crop_x + (right - left) * SCALE]
            if core.shape != (3, (bottom - top) * SCALE, (right - left) * SCALE):
                raise ValueError("advanced upscaler produced an invalid tile")
            tile = Image.fromarray(np.uint8(np.clip(core.transpose(1, 2, 0), 0, 1) * 255 + 0.5), "RGB")
            output.paste(tile, (left * SCALE, top * SCALE))
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
