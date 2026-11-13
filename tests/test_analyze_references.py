import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

SPEC = importlib.util.spec_from_file_location("video_use_analyze_references", Path(__file__).parents[1] / "helpers" / "analyze_references.py")
assert SPEC and SPEC.loader
ar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ar)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class ParserTests(unittest.TestCase):
    def test_pacing_from_cut_times(self):
        result = ar.pacing([2.0, 4.0, 9.0], 10.0)
        self.assertEqual(result["scene_cuts"], 3)
        self.assertEqual(result["cuts_per_minute"], 18.0)
        self.assertEqual(result["median_shot_s"], 2.0)  # shots 2, 2, 5, 1
        self.assertEqual(result["longest_shot_s"], 5.0)

    def test_loudness_json_is_parsed(self):
        stderr = 'noise\n{\n "input_i" : "-12.40",\n "input_tp" : "0.10",\n "input_lra" : "5.0"\n}\n'
        self.assertEqual(ar.parse_loudness(stderr), {"integrated_lufs": -12.4, "true_peak_dbtp": 0.1, "lra": 5.0})

    def test_dominant_colors_recover_two_flat_regions(self):
        pixels = np.array([[20, 40, 200]] * 75 + [[250, 250, 250]] * 25, np.uint8)
        colors = ar.dominant_colors(pixels, k=3)
        self.assertEqual(colors[0]["hex"], "#1428C8")
        self.assertEqual(colors[0]["share"], 0.75)
        self.assertEqual(colors[1]["hex"], "#FAFAFA")


@unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
class AnalyzeIntegrationTests(unittest.TestCase):
    def test_vertical_reference_with_one_hard_cut(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "ref.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1",
                "-f", "lavfi", "-i", "color=c=blue:s=180x320:r=30:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out),
            ], check=True, capture_output=True)
            report = ar.analyze(out)
        self.assertEqual(report["format"]["orientation"], "vertical")
        self.assertEqual(report["pacing"]["scene_cuts"], 1)
        self.assertTrue(report["silence"]["continuous_bed_likely"])
        self.assertIsNotNone(report["loudness"]["integrated_lufs"])
        self.assertGreaterEqual(len(report["dominant_colors"]), 2)
        self.assertTrue(any(c["share"] > 0.3 for c in report["dominant_colors"]))


if __name__ == "__main__":
    unittest.main()
