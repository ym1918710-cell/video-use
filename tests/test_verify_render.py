import importlib.util
import sys
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "verify_render.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("video_use_verify_render", MODULE_PATH)
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class VisualSignalTests(unittest.TestCase):
    def test_classifies_only_an_overwhelmingly_black_frame_as_suspect(self):
        self.assertTrue(verifier.is_nearly_black({"mean_luma": 6.0, "near_black_fraction": 0.99}))
        self.assertFalse(verifier.is_nearly_black({"mean_luma": 7.0, "near_black_fraction": 0.75}))
        self.assertFalse(verifier.is_nearly_black({"mean_luma": 22.0, "near_black_fraction": 0.99}))

    def test_cut_times_are_output_timeline_offsets(self):
        self.assertEqual(
            verifier.output_cut_times({"ranges": [
                {"start": 10, "end": 12.5}, {"start": 5, "end": 9}, {"start": 0, "end": 1},
            ]}),
            [2.5, 6.5],
        )


@unittest.skipUnless(shutil.which("ffmpeg"), "requires ffmpeg")
class VisualSignalIntegrationTests(unittest.TestCase):
    def test_frame_luma_distinguishes_black_from_bright_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "signal.mp4"
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=0.5",
                "-f", "lavfi", "-i", "color=c=white:s=64x64:r=24:d=0.5",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
            ], check=True, capture_output=True)
            black = verifier.frame_luma(output, 0.2)
            bright = verifier.frame_luma(output, 0.7)
            self.assertIsNotNone(black)
            self.assertIsNotNone(bright)
            self.assertTrue(verifier.is_nearly_black(black))
            self.assertFalse(verifier.is_nearly_black(bright))


if __name__ == "__main__":
    unittest.main()
