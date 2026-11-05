import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "render.py"
SPEC = importlib.util.spec_from_file_location("video_use_render", MODULE_PATH)
assert SPEC and SPEC.loader
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg and ffprobe")
class PunchInIntegrationTests(unittest.TestCase):
    def test_punch_in_segment_keeps_the_standard_landscape_output_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "source.mp4", root / "punch.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
            ], check=True, capture_output=True)
            render.extract_segment(source, 0, 0.8, "", output, rate="24/1", punch_in={"scale": 1.08})
            result = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            stream = json.loads(result.stdout)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (1920, 1080))


if __name__ == "__main__":
    unittest.main()
