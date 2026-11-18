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
class SubjectLayerIntegrationTests(unittest.TestCase):
    def test_small_mask_is_scaled_before_being_merged_with_subject(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, graph, mask, output = root / "base.mp4", root / "graph.mp4", root / "mask.mkv", root / "final.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(base),
            ], check=True, capture_output=True)
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=24", "-t", "0.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(graph),
            ], check=True, capture_output=True)
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=white:size=64x64:rate=24", "-t", "0.5",
                "-c:v", "ffv1", "-pix_fmt", "gray", str(mask),
            ], check=True, capture_output=True)
            render.build_final_composite(
                base, [], None, output, root,
                behind_subject_overlays=[{"file": str(graph), "start_in_output": 0, "duration": 0.5}],
                subject_mask_path=mask,
            )
            info = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            streams = json.loads(info.stdout)["streams"]
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (320, 180))
            self.assertTrue(any(stream["codec_type"] == "audio" for stream in streams))


if __name__ == "__main__":
    unittest.main()
