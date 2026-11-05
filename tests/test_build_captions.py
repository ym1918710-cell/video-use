import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "build_captions.py"
SPEC = importlib.util.spec_from_file_location("video_use_build_captions", MODULE_PATH)
assert SPEC and SPEC.loader
captions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(captions)


class ArabicCaptionTests(unittest.TestCase):
    def test_keeps_arabic_unicode_and_uses_output_timeline_offsets(self):
        with tempfile.TemporaryDirectory() as temporary:
            edit = Path(temporary)
            transcripts = edit / "transcripts"
            transcripts.mkdir()
            (transcripts / "A.json").write_text(json.dumps({"words": [
                {"type": "word", "text": "أهلاً", "start": 1.0, "end": 1.2},
                {"type": "word", "text": "بكم", "start": 1.3, "end": 1.5},
                {"type": "word", "text": "في", "start": 1.6, "end": 1.7},
                {"type": "word", "text": "Video Use", "start": 1.8, "end": 2.2},
            ]}, ensure_ascii=False), encoding="utf-8")
            ass = captions.build_ass({"sources": {"A": "source.mp4"}, "ranges": [{"source": "A", "start": 1.0, "end": 2.3}]}, edit, "Geeza Pro", 58, 250, 4)
            self.assertIn("Style: Arabic,Geeza Pro,58", ass)
            self.assertIn("أهلاً بكم في Video Use", ass)
            self.assertIn("0:00:00.00", ass)
            self.assertNotIn("أهلاً بكم في VIDEO USE", ass)


if __name__ == "__main__":
    unittest.main()
