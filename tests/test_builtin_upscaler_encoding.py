"""Bundled encoding and size-preflight contracts without optional AI wheels."""
import ast
from contextlib import redirect_stderr
import io
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None


DIRECTORY = Path(__file__).resolve().parents[1] / "scm_workbench/builtin_processors"


def callback_ast(name):
    source = DIRECTORY / name
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return source, next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "process_image")


class BundledUpscalerEncodingTests(unittest.TestCase):
    def test_jpeg_streams_without_optimization_and_retains_quality_and_chroma(self):
        for name in ("simple_upscaler.py", "advanced_upscaler.py"):
            source, process = callback_ast(name)
            encoding = next(node for node in process.body
                            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                            and isinstance(node.test.left, ast.Name)
                            and node.test.left.id == "image_format"
                            and len(node.test.comparators) == 1
                            and isinstance(node.test.comparators[0], ast.Constant)
                            and node.test.comparators[0].value == "JPEG")
            save = next(node for node in ast.walk(process)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "save")
            self.assertTrue(any(key.arg is None and isinstance(key.value, ast.Name)
                                and key.value.id in {"options", "save_options"}
                                for key in save.keywords))
            policy = compile(ast.Module(body=[encoding], type_ignores=[]), str(source), "exec")
            for image_format in ("PNG", "JPEG", "WEBP"):
                with self.subTest(processor=name, image_format=image_format):
                    options = {}
                    namespace = {"image_format": image_format, "options": options,
                                 "save_options": options, "resized": SimpleNamespace(mode="RGB")}
                    exec(policy, namespace)
                    expected = (False if image_format == "JPEG" else
                                name == "simple_upscaler.py" if image_format == "PNG" else None)
                    self.assertEqual(options.get("optimize"), expected)
                    if image_format == "JPEG":
                        self.assertEqual(options["quality"], 95)
                        self.assertEqual(options["subsampling"], 0)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_size_preflight_rejects_before_resize_or_ai_session_and_keeps_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card.png"
            Image.new("RGB", (10, 10), "red").save(path, dpi=(300, 300))
            original = path.read_bytes()
            for name in ("simple_upscaler.py", "advanced_upscaler.py"):
                with self.subTest(processor=name):
                    source, function = callback_ast(name)
                    model = mock.Mock(side_effect=AssertionError("unexpected AI initialization"))
                    namespace = {"Path": Path, "Image": Image, "ImageOps": ImageOps,
                                 "SCALE": 4, "_model": model}
                    tree = (ast.parse(source.read_text(encoding="utf-8")) if name == "simple_upscaler.py"
                            else ast.Module(body=[function], type_ignores=[]))
                    exec(compile(tree, str(source), "exec"), namespace)
                    with (mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1000),
                          mock.patch.object(ImageOps, "exif_transpose", side_effect=AssertionError("unexpected decode"))):
                        with self.assertRaisesRegex(ValueError, "(?:4×|1200-PPI) upscaling.*above the supported limit") as caught:
                            namespace["process_image"](path, {"model_path": "fixed-model.onnx"})
                    self.assertIn("smaller original images", str(caught.exception))
                    self.assertIn("restore or re-fetch", str(caught.exception))
                    model.assert_not_called()
                    self.assertEqual(path.read_bytes(), original)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_simple_releases_original_and_transposed_pixel_buffers_before_encoding(self):
        from scm_workbench.builtin_processors.simple_upscaler import process_image
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card.jpg"
            Image.new("RGB", (10, 15), "red").save(path, format="JPEG", dpi=(300, 300))
            opened, transformed = [], []
            real_open, real_transpose, real_save = Image.open, ImageOps.exif_transpose, Image.Image.save
            def capture_open(*args, **kwargs):
                image = real_open(*args, **kwargs)
                opened.append(image)
                return image
            def capture_transpose(image):
                copy = real_transpose(image)
                transformed.append(copy)
                return copy
            def save(image, *args, **kwargs):
                self.assertTrue(opened and transformed)
                for source in opened + transformed:
                    with self.assertRaises(ValueError):
                        source.getpixel((0, 0))
                return real_save(image, *args, **kwargs)
            with (mock.patch.object(Image, "open", side_effect=capture_open),
                  mock.patch.object(ImageOps, "exif_transpose", side_effect=capture_transpose),
                  mock.patch.object(Image.Image, "save", new=save)):
                process_image(path, {})
            with Image.open(path) as output:
                self.assertEqual(output.size, (40, 60))
                output.load()

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_simple_preserves_real_jpeg_format_dpi_and_exif_orientation(self):
        from scm_workbench.builtin_processors.simple_upscaler import process_image
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "jpeg-content.png"
            exif = Image.Exif()
            exif[274] = 6
            exif[282] = exif[283] = 300
            exif[296] = 2
            exif[256], exif[257] = 2, 3
            exif[270] = "Keep this description"
            Image.new("RGB", (2, 3), "red").save(
                path, format="JPEG", exif=exif, dpi=(300, 300), icc_profile=b"test ICC profile",
            )
            self.assertIsNone(process_image(path, {}))
            with Image.open(path) as output:
                self.assertEqual(output.format, "JPEG")
                self.assertEqual(output.size, (12, 8))
                self.assertEqual(output.info["dpi"], (1200, 1200))
                self.assertIsNone(output.getexif().get(274))
                self.assertEqual(output.getexif()[282], 1200)
                self.assertEqual(output.getexif()[283], 1200)
                self.assertEqual(output.getexif()[296], 2)
                self.assertEqual((output.getexif()[256], output.getexif()[257]), (12, 8))
                self.assertEqual(output.getexif()[270], "Keep this description")
                self.assertEqual(output.info["icc_profile"], b"test ICC profile")
                output.load()


@unittest.skipIf(Image is None, "Pillow is not installed")
class SimpleUpscalerResolutionTests(unittest.TestCase):
    def process(self, path):
        from scm_workbench.builtin_processors.simple_upscaler import process_image
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            self.assertIsNone(process_image(path, {"relative_path": f"game/front/{path.name}"}))
        return diagnostic.getvalue()

    def test_embedded_ppi_controls_scale_and_reruns_do_not_change_output(self):
        with tempfile.TemporaryDirectory() as temp:
            for fmt in ("JPEG", "PNG", "BMP"):
                for ppi, expected in ((300, (40, 56)), (600, (20, 28)),
                                      (800, (15, 21)), (1200, (10, 14)), (1600, (10, 14))):
                    with self.subTest(format=fmt, ppi=ppi):
                        path = Path(temp) / f"card-{ppi}.png"
                        Image.new("RGB", (10, 14), "red").save(path, format=fmt, dpi=(ppi, ppi))
                        original = path.read_bytes()
                        message = self.process(path)
                        with Image.open(path) as output:
                            self.assertEqual(output.format, fmt)
                            self.assertEqual(output.size, expected)
                            for axis in output.info["dpi"]:
                                self.assertAlmostEqual(axis, 1200 if ppi < 1200 else ppi, delta=0.02)
                            output.load()
                        if ppi >= 1200:
                            self.assertEqual(path.read_bytes(), original)
                            self.assertIn("already at or above 1200", message)
                        else:
                            self.assertEqual(message, "")
                        processed = path.read_bytes()
                        self.assertIn("already at or above 1200", self.process(path))
                        self.assertEqual(path.read_bytes(), processed)

    def test_missing_metadata_uses_standard_fit_not_pillow_72_ppi_and_reruns_skip(self):
        with (tempfile.TemporaryDirectory() as temp,
              mock.patch("scm_workbench.builtin_processors.simple_upscaler.STANDARD_MTG_PIXELS", (30, 42))):
            for fmt in ("PNG", "JPEG"):
                for with_exif in (False, True):
                    with self.subTest(format=fmt, exif=with_exif):
                        path = Path(temp) / "Cárd  A.png"
                        exif = Image.Exif()
                        exif[274] = 6
                        Image.new("RGB", (3, 5), "red").save(
                            path, format=fmt, **({"exif": exif} if with_exif else {}),
                        )
                        reporter = mock.Mock()
                        from scm_workbench.builtin_processors.simple_upscaler import process_image
                        with redirect_stderr(io.StringIO()):
                            process_image(path, {"_workbench_report_skip": reporter})
                        reporter.assert_not_called()  # A fallback resize is not a skipped image.
                        with Image.open(path) as output:
                            self.assertEqual(output.size, (42, 25) if with_exif else (25, 42))
                            self.assertIsNone(output.getexif().get(274))
                            self.assertEqual(output.format, fmt)
                            for axis in output.info["dpi"]:
                                self.assertAlmostEqual(axis, 1200, delta=0.02)
                        processed = path.read_bytes()
                        message = self.process(path)
                        self.assertIn("already at or above 1200", message)
                        self.assertEqual(path.read_bytes(), processed)
                        # Recreate the source to inspect its named fallback diagnostic.
                        Image.new("RGB", (3, 5)).save(path, format=fmt)
                        message = self.process(path)
                        self.assertIn("game/front/Cárd  A.png", message)
                        self.assertIn("missing embedded PPI/DPI", message)
                        self.assertIn("assuming standard MTG 63 × 88 mm", message)
                        self.assertNotIn("skipped", message)

    def test_invalid_and_inconsistent_density_pairs_use_standard_fit(self):
        with (tempfile.TemporaryDirectory() as temp,
              mock.patch("scm_workbench.builtin_processors.simple_upscaler.STANDARD_MTG_PIXELS", (30, 42))):
            path = Path(temp) / "card.png"
            Image.new("RGB", (3, 5), "red").save(path, dpi=(300, 300))
            original = path.read_bytes()
            real_open = Image.open
            for dpi in (None, (), (300,), (0, 0), (-1, -1), (float("nan"), 300),
                        (float("inf"), float("inf")), (True, True), ("invalid", 300),
                        (300, 600), (1200, 1300), (300, None)):
                with self.subTest(dpi=dpi):
                    def opened(*args, **kwargs):
                        image = real_open(*args, **kwargs)
                        image.info["dpi"] = dpi
                        return image
                    path.write_bytes(original)
                    with mock.patch.object(Image, "open", side_effect=opened):
                        message = self.process(path)
                    self.assertRegex(message, "invalid|inconsistent")
                    self.assertIn("assuming standard MTG", message)
                    self.assertNotIn("skipped", message)
                    with Image.open(path) as output:
                        self.assertEqual(output.size, (25, 42))

    def test_unreadable_exif_uses_fallback_and_diagnostics_stay_on_one_line(self):
        with (tempfile.TemporaryDirectory() as temp,
              mock.patch("scm_workbench.builtin_processors.simple_upscaler.STANDARD_MTG_PIXELS", (30, 42))):
            path = Path(temp) / "card.png"
            Image.new("RGB", (3, 5)).save(path, dpi=(300, 300))
            original = path.read_bytes()
            real_open = Image.open
            for error in (struct.error("invalid EXIF\nsecond line"), OverflowError("invalid resolution")):
                with self.subTest(error=type(error).__name__):
                    path.write_bytes(original)
                    def opened(*args, **kwargs):
                        image = real_open(*args, **kwargs)
                        image.getexif = mock.Mock(side_effect=error)
                        return image
                    with mock.patch.object(Image, "open", side_effect=opened):
                        message = self.process(path)
                    self.assertIn("invalid", message)
                    self.assertIn("assuming standard MTG", message)
                    self.assertEqual(len(message.splitlines()), 1)
                    with Image.open(path) as output:
                        self.assertEqual(output.size, (25, 42))

    def test_exif_pairs_units_and_conflicting_sources(self):
        with (tempfile.TemporaryDirectory() as temp,
              mock.patch("scm_workbench.builtin_processors.simple_upscaler.STANDARD_MTG_PIXELS", (30, 42))):
            for x, y, unit, jfif, expected, reason in (
                (600, 600, 2, None, (6, 10), None),
                (600 / 2.54, 600 / 2.54, 3, None, (6, 10), None),
                (600, 800, 2, None, (3, 5), "inconsistent"),
                (600, None, 2, None, (3, 5), "invalid"),
                (0, 0, 2, None, (3, 5), "invalid"),
                (600, 600, 1, None, (3, 5), "resolution unit"),
                (600, 600, None, None, (3, 5), "resolution unit"),
                (600, 600, 2, 300, (3, 5), "conflicting"),
            ):
                with self.subTest(x=x, y=y, unit=unit, jfif=jfif):
                    path = Path(temp) / "card.jpg"
                    exif = Image.Exif()
                    for tag, value in ((282, x), (283, y), (296, unit)):
                        if value is not None:
                            exif[tag] = value
                    Image.new("RGB", (3, 5), "red").save(
                        path, exif=exif, **({"dpi": (jfif, jfif)} if jfif else {}),
                    )
                    original = path.read_bytes()
                    message = self.process(path)
                    with Image.open(path) as output:
                        self.assertEqual(output.size, expected if reason is None else (25, 42))
                        if reason is None:
                            self.assertEqual(output.info["dpi"], (1200, 1200))
                            self.assertEqual(output.getexif()[296], 2)
                    if reason:
                        self.assertIn(reason, message)
                        self.assertIn("assuming standard MTG", message)
                        self.assertNotEqual(path.read_bytes(), original)
                        processed = path.read_bytes()
                        self.assertIn("already at or above 1200", self.process(path))
                        self.assertEqual(path.read_bytes(), processed)
                    else:
                        self.assertEqual(message, "")

    def test_fallback_target_math_aspect_landscape_and_bleed_without_large_allocations(self):
        from scm_workbench.builtin_processors.simple_upscaler import STANDARD_MTG_PIXELS
        self.assertEqual(STANDARD_MTG_PIXELS, (round(63 / 25.4 * 1200), round(88 / 25.4 * 1200)))
        self.assertEqual(STANDARD_MTG_PIXELS, (2976, 4157))
        with tempfile.TemporaryDirectory() as temp:
            for size, orientation, expected in (
                ((63, 88), None, (2976, 4157)),
                ((745, 1040), None, (2976, 4154)),
                ((69, 94), None, (2976, 4054)),  # Up to 3 mm bleed per edge, not removed.
                ((94, 69), None, (4054, 2976)),
                ((63, 88), 6, (4157, 2976)),
                ((100, 100), None, (2976, 2976)),
            ):
                with self.subTest(size=size, orientation=orientation):
                    path = Path(temp) / "card.png"
                    exif = Image.Exif()
                    if orientation:
                        exif[274] = orientation
                    Image.new("RGB", size).save(path, exif=exif)
                    # Assert production dimensions, but never allocate a 12-Mpixel regression output.
                    with mock.patch.object(Image.Image, "resize", return_value=Image.new("RGB", (1, 1))) as resize:
                        message = self.process(path)
                    resize.assert_called_once_with(expected, Image.Resampling.LANCZOS)
                    self.assertIn("assuming standard MTG", message)
                    self.assertNotIn("skipped", message)
                    self.assertLessEqual(abs(expected[0] / expected[1] -
                                             (size[1] / size[0] if orientation else size[0] / size[1])), 0.001)

    def test_fallback_never_downscales_or_decodes_images_at_or_above_fit_target(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card.png"
            Image.new("RGB", (3, 5)).save(path)
            original = path.read_bytes()
            real_open = Image.open
            for size in ((2976, 4157), (4000, 5000), (5000, 4000), (4157, 2000), (2000, 4157)):
                with self.subTest(size=size):
                    def opened(*args, **kwargs):
                        image = real_open(*args, **kwargs)
                        image._size = size  # Header dimensions only; any decode is a regression.
                        return image
                    with (mock.patch.object(Image, "open", side_effect=opened),
                          mock.patch.object(ImageOps, "exif_transpose", side_effect=AssertionError("decoded skip"))):
                        message = self.process(path)
                    self.assertIn("skipped", message)
                    self.assertIn("meet or exceed the standard MTG fallback fit target", message)
                    self.assertEqual(path.read_bytes(), original)

    def test_fallback_retains_output_pixel_preflight_before_decode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card.png"
            Image.new("RGB", (3, 5)).save(path)
            original = path.read_bytes()
            with (mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1000),
                  mock.patch.object(ImageOps, "exif_transpose", side_effect=AssertionError("decoded oversize"))):
                with self.assertRaisesRegex(ValueError, "above the supported limit"):
                    self.process(path)
            self.assertEqual(path.read_bytes(), original)

    def test_fractional_scale_rounds_actual_pixels_not_card_size(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "square.png"
            Image.new("RGBA", (3, 5), (12, 34, 56, 78)).save(path, dpi=(800, 800))
            self.process(path)
            with Image.open(path) as output:
                self.assertEqual(output.size, (5, 8))  # PNG quantization puts scale just above 1.5.
                self.assertEqual(output.mode, "RGBA")
                self.assertEqual(output.getpixel((0, 0))[3], 78)

    def test_exif_below_target_is_not_rounded_up_by_container_tolerance(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "near-target.jpg"
            exif = Image.Exif()
            exif[282] = exif[283] = 1199.99
            exif[296] = 2
            Image.new("RGB", (3, 5)).save(path, exif=exif)
            self.assertEqual(self.process(path), "")
            with Image.open(path) as output:
                self.assertEqual(output.size, (3, 5))
                self.assertEqual(output.info["dpi"], (1200, 1200))
                self.assertEqual(output.getexif()[282], 1200)

    def test_centimetre_resolution_overflow_is_invalid_not_already_upscaled(self):
        from scm_workbench.builtin_processors.simple_upscaler import _input_ppi
        opened = SimpleNamespace(format="JPEG", info={})
        with self.assertRaisesRegex(ValueError, "invalid PPI/DPI"):
            _input_ppi(opened, {282: 1e308, 283: 1e308, 296: 3})

    def test_tiny_positive_ppi_is_rejected_before_output_allocation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card.png"
            Image.new("RGB", (3, 5)).save(path, dpi=(300, 300))
            original = path.read_bytes()
            real_open = Image.open
            def opened(*args, **kwargs):
                image = real_open(*args, **kwargs)
                image.info["dpi"] = (5e-324, 5e-324)
                return image
            with (mock.patch.object(Image, "open", side_effect=opened),
                  mock.patch.object(ImageOps, "exif_transpose", side_effect=AssertionError("decoded oversize"))):
                with self.assertRaisesRegex(ValueError, "above the supported limit"):
                    self.process(path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
