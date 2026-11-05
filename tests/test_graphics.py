import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
sys.path.insert(0, str(HELPERS))
SPEC = importlib.util.spec_from_file_location("video_use_graphics", HELPERS / "graphics.py")
assert SPEC and SPEC.loader
graphics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graphics)

CAN_RENDER = sys.platform == "darwin" and bool(shutil.which("swiftc") and shutil.which("ffmpeg"))
BRAND = {"colors": {"primary": "#2448F0", "secondary": "#7A3CFF", "accent": "#4D8BFF"},
         "gradients": [["#2B6BFF", "#8A3CFF"]]}


class TokenTests(unittest.TestCase):
    def test_empty_kit_is_brand_neutral(self):
        tokens = graphics.Tokens({}, Path("."))
        spec = graphics.card_spec({"type": "sticker", "text": "Hello"}, tokens, (1080, 1920))
        self.assertEqual(spec["fill"], {"colors": ["#FFFFFF"]})
        self.assertIsNone(spec["stroke"])
        self.assertEqual(spec["lines"][0]["color"], "#111111")
        number = graphics.card_spec({"type": "number", "text": "5"}, tokens, (1080, 1920))
        self.assertNotIn("extrude", number)

    def test_brand_tokens_dress_the_same_element(self):
        tokens = graphics.Tokens(BRAND, Path("."))
        spec = graphics.card_spec({"type": "sticker", "text": "Hello"}, tokens, (1080, 1920))
        self.assertEqual(spec["stroke"]["colors"], ["#2B6BFF", "#8A3CFF"])
        self.assertEqual(spec["lines"][0]["color"], "#2448F0")
        number = graphics.card_spec({"type": "number", "text": "5", "label": "أيام"}, tokens, (1080, 1920))
        self.assertEqual(number["extrude"]["color"], graphics.shade("#2448F0", 0.45))
        self.assertEqual(len(number["lines"]), 2)

    def test_sizes_follow_the_canvas(self):
        tokens = graphics.Tokens({}, Path("."))
        tall = graphics.card_spec({"type": "headline", "text": "A"}, tokens, (1080, 1920))
        draft = graphics.card_spec({"type": "headline", "text": "A"}, tokens, (720, 1280))
        self.assertAlmostEqual(tall["lines"][0]["size"] / draft["lines"][0]["size"], 1.5)

    def test_arabic_font_is_chosen_for_arabic_text(self):
        tokens = graphics.Tokens({"fonts": {"headline": "Avenir Next", "arabic": "Geeza Pro"}}, Path("."))
        self.assertEqual(tokens.font_for("أيام"), "Geeza Pro")
        self.assertEqual(tokens.font_for("Days"), "Avenir Next")

    def test_brand_kit_path_from_edl_or_edit_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "brand_kit.json").write_text(json.dumps(BRAND))
            self.assertEqual(graphics.load_brand_kit({}, root)["colors"]["primary"], "#2448F0")
            self.assertEqual(graphics.load_brand_kit({"brand_kit": {"client": "x"}}, root), {"client": "x"})
            self.assertEqual(graphics.load_brand_kit({"brand_kit": "missing.json"}, root), {})


class MotionAndLayoutTests(unittest.TestCase):
    def test_pop_settles_at_full_size_and_fades_out(self):
        start = graphics.transform_at("pop", 0.0, 2.0, 0.3, 0.16, 1080)
        settled = graphics.transform_at("pop", 1.0, 2.0, 0.3, 0.16, 1080)
        end = graphics.transform_at("pop", 2.0, 2.0, 0.3, 0.16, 1080)
        self.assertLess(start[0], 0.6)
        self.assertEqual(start[3], 0.0)
        self.assertAlmostEqual(settled[0], 1.0)
        self.assertEqual(settled[3], 1.0)
        self.assertAlmostEqual(end[3], 0.0)

    def test_place_centres_and_clamps_to_safe_area(self):
        self.assertEqual(graphics.place("center", (200, 100), (1080, 1920)), (440, 910))
        x, y = graphics.place({"x": 1.0, "y": 1.0}, (400, 300), (1080, 1920))
        self.assertLessEqual(x + 400, 1080 - round(1080 * 0.04) + 4)
        self.assertLessEqual(y + 300, 1920 - round(1920 * 0.14) + 4)  # clear of vertical platform UI

    def test_source_anchor_maps_through_kept_ranges(self):
        edl = {"ranges": [{"source": "A", "start": 10, "end": 12}, {"source": "B", "start": 5, "end": 8}]}
        self.assertEqual(graphics.output_time(edl, {"source": "B", "time": 6.0}, "30"), 3.0)
        with self.assertRaises(ValueError):
            graphics.output_time(edl, {"source": "A", "time": 20}, "30")


class ValidationTests(unittest.TestCase):
    def codes(self, element, total=10.0):
        return {issue[1] for issue in graphics.validate_graphics({"graphics": [element]}, total)}

    def test_validation(self):
        self.assertIn("invalid_graphic_type", self.codes({"type": "confetti"}))
        self.assertIn("graphic_missing_text", self.codes({"type": "sticker", "start_in_output": 1, "duration": 2}))
        self.assertIn("graphic_outside_output", self.codes({"type": "sticker", "text": "x", "start_in_output": 9, "duration": 2}))
        self.assertIn("invalid_graphic_time", self.codes({"type": "sticker", "text": "x", "duration": 2}))
        self.assertIn("invalid_graphic_animation", self.codes({"type": "sticker", "text": "x", "start_in_output": 1, "duration": 2, "animation": "spin"}))
        self.assertEqual(self.codes({"type": "number", "text": "5", "at": {"source": "A", "time": 3}, "duration": 2}), set())


@unittest.skipUnless(CAN_RENDER, "requires macOS CoreText, swiftc, and ffmpeg")
class RenderTests(unittest.TestCase):
    def test_build_graphics_writes_alpha_movs_with_exact_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edl = {"ranges": [{"source": "A", "start": 0, "end": 3}], "brand_kit": BRAND, "graphics": [
                {"type": "sticker", "text": "ولفهم الـTools", "start_in_output": 0.5, "duration": 1.0},
                {"type": "number", "text": "5", "label": "أيام", "start_in_output": 1.0, "duration": 1.0, "behind_subject": True},
                {"type": "logo", "start_in_output": 0, "duration": 3},
            ]}
            with contextlib.redirect_stdout(io.StringIO()):
                top, behind = graphics.build_graphics(edl, root, (540, 960), "30")
            self.assertEqual((len(top), len(behind)), (1, 1))  # logo skipped: no logo in kit
            probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                    "-show_entries", "stream=pix_fmt,nb_read_frames,width,height", "-of", "json",
                                    top[0]["file"]], check=True, capture_output=True, text=True)
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual(stream["pix_fmt"], "rgba")
            self.assertEqual(int(stream["nb_read_frames"]), 30)
            self.assertLessEqual(top[0]["x"] + stream["width"], 540)
            # Cached: a second build reuses the same files.
            with contextlib.redirect_stdout(io.StringIO()):
                again, _ = graphics.build_graphics(edl, root, (540, 960), "30")
            self.assertEqual(again[0]["file"], top[0]["file"])


if __name__ == "__main__":
    unittest.main()
