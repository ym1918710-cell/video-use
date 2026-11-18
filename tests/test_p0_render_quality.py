import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


HELPERS = Path(__file__).parents[1] / "helpers"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"video_use_{name}", HELPERS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load("render")
captions = load("build_captions")
validate = load("validate_edl")

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_source(path: Path, size: str, seconds: float, freq: int = 440) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True, capture_output=True)


def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    stream = json.loads(out.stdout)["streams"][0]
    return stream["width"], stream["height"]


class SegmentTimingTests(unittest.TestCase):
    def test_frames_sum_to_rounded_total_and_offsets_are_contiguous(self):
        ranges = [{"start": 0, "end": 1.013}, {"start": 5, "end": 6.517}, {"start": 9, "end": 9.71}]
        timing = render.segment_timing(ranges, "30/1")
        total = sum(r["end"] - r["start"] for r in ranges)
        self.assertEqual(sum(t[0] for t in timing), round(total * 30))
        for (f0, o0, d0), (_f1, o1, _d1) in zip(timing, timing[1:]):
            self.assertAlmostEqual(o0 + d0, o1, places=9)
            self.assertAlmostEqual(d0, f0 / 30, places=9)

    def test_every_range_keeps_at_least_one_frame(self):
        timing = render.segment_timing([{"start": 0, "end": 0.001}], "24")
        self.assertEqual(timing[0][0], 1)


class FramingTests(unittest.TestCase):
    def test_landscape_source_into_portrait_canvas_crops_at_the_anchor(self):
        chain = render.framing_filter((1080, 1920), (3840, 2160), framing={"anchor_x": 0.0})
        self.assertTrue(chain.startswith("crop=1214:2160:0:0,"))
        self.assertTrue(chain.endswith("scale=1080:1920:flags=lanczos,setsar=1"))

    def test_punch_in_is_cropped_from_source_pixels(self):
        chain = render.framing_filter((1920, 1080), (3840, 2160), punch_in={"scale": 1.2, "anchor_y": 0.4})
        self.assertTrue(chain.startswith("crop=3200:1800:320:144,"))

    def test_matching_source_needs_only_a_scale(self):
        self.assertEqual(
            render.framing_filter((1080, 1920), (1080, 1920)),
            "scale=1080:1920:flags=lanczos,setsar=1",
        )

    def test_blur_fit_builds_background_and_foreground(self):
        chain = render.framing_filter((1080, 1920), (1920, 1080), framing={"mode": "blur_fit"})
        self.assertIn("boxblur", chain)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", chain)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            render.framing_filter((1080, 1920), (1920, 1080), framing={"mode": "stretch"})


class PerSourceGradeTests(unittest.TestCase):
    def test_auto_grade_is_measured_once_per_source(self):
        edl = {
            "sources": {"A": "a.mp4", "B": "b.mp4"},
            "ranges": [
                {"source": "A", "start": 0, "end": 1},
                {"source": "B", "start": 0, "end": 1},
                {"source": "A", "start": 3, "end": 4},
            ],
            "grade": "auto",
        }
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(render, "probe_source_fps", return_value="30/1"), \
                patch.object(render, "auto_grade_for_ranges", side_effect=lambda path, spans: (f"eq=n={len(spans)}", {})) as grade, \
                patch.object(render, "extract_segment") as extract, \
                contextlib.redirect_stdout(io.StringIO()):
            render.extract_all_segments(edl, Path(temporary), preview=False)
        self.assertEqual(grade.call_count, 2)
        filters = [call.args[3] for call in extract.call_args_list]
        self.assertEqual(filters, ["eq=n=2", "eq=n=1", "eq=n=2"])
        self.assertTrue(all(call.kwargs["include_audio"] is False for call in extract.call_args_list))


class CueTimingTests(unittest.TestCase):
    def test_small_gaps_are_bridged_and_short_cues_extended(self):
        cues = render.smooth_cue_timing([(0.0, 0.2, "A"), (0.6, 1.0, "B"), (1.1, 2.0, "C"), (3.0, 3.5, "D")])
        self.assertEqual(cues[0][1], 0.6)   # 0.2 → min 0.45 → gap 0.15 bridged
        self.assertEqual(cues[1][1], 1.1)   # 0.1 gap bridged
        self.assertEqual(cues[2][1], 2.0)   # real pause kept
        for (a, b, _), (c, _d, _t) in zip(cues, cues[1:]):
            self.assertLessEqual(b, c)


class ArabicCaptionCanvasTests(unittest.TestCase):
    def test_landscape_canvas_and_arabic_comma_break(self):
        with tempfile.TemporaryDirectory() as temporary:
            edit = Path(temporary)
            (edit / "transcripts").mkdir()
            (edit / "transcripts" / "A.json").write_text(json.dumps({"words": [
                {"type": "word", "text": "أولاً،", "start": 0.0, "end": 0.3},
                {"type": "word", "text": "البيانات", "start": 0.35, "end": 0.7},
                {"type": "word", "text": "مهمة", "start": 0.75, "end": 1.0},
            ]}, ensure_ascii=False), encoding="utf-8")
            edl = {"canvas": {"width": 1920, "height": 1080}, "sources": {"A": "a.mp4"},
                   "ranges": [{"source": "A", "start": 0.0, "end": 1.1}]}
            ass = captions.build_ass(edl, edit, "Geeza Pro", None, None, 4)
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("PlayResY: 1080", ass)
        self.assertIn("Style: Arabic,Geeza Pro,32,", ass)
        dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogue), 2)
        self.assertTrue(dialogue[0].endswith("أولاً،"))


class ValidatorTests(unittest.TestCase):
    def _report(self, edl: dict, root: Path) -> dict:
        path = root / "edl.json"
        path.write_text(json.dumps(edl), encoding="utf-8")
        return validate.validate_edl(path)

    @unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
    def test_unanchored_cover_crop_of_mismatched_orientation_warns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_source(root / "wide.mp4", "320x180", 1)
            base = {"sources": {"W": "wide.mp4"}, "ranges": [{"source": "W", "start": 0, "end": 0.5}],
                    "canvas": {"width": 1080, "height": 1920}}
            codes = [w["code"] for w in self._report(base, root)["warnings"]]
            self.assertIn("orientation_crop", codes)
            anchored = dict(base, source_framing={"W": {"anchor_x": 0.3}})
            codes = [w["code"] for w in self._report(anchored, root)["warnings"]]
            self.assertNotIn("orientation_crop", codes)

    def test_invalid_canvas_framing_and_crossfade_are_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.mp4").touch()
            report = self._report({
                "sources": {"A": "a.mp4"}, "ranges": [{"source": "A", "start": 0, "end": 1}],
                "canvas": {"width": 1081, "height": 1920},
                "source_framing": {"A": {"mode": "stretch"}},
                "audio": {"crossfade_ms": 500},
            }, root)
        codes = {e["code"] for e in report["errors"]}
        self.assertTrue({"invalid_canvas", "invalid_source_framing", "invalid_audio"} <= codes)


@unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
class RenderIntegrationTests(unittest.TestCase):
    def test_mixed_orientation_sources_concat_to_one_canvas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_source(root / "tall.mp4", "180x320", 2)
            make_source(root / "wide.mp4", "320x180", 2)
            edl = {"sources": {"T": "tall.mp4", "W": "wide.mp4"},
                   "source_framing": {"W": {"mode": "blur_fit"}},
                   "ranges": [{"source": "T", "start": 0, "end": 0.5}, {"source": "W", "start": 0.2, "end": 0.9}]}
            with contextlib.redirect_stdout(io.StringIO()):
                segments = render.extract_all_segments(edl, root, preview=True)
                render.concat_segments(segments, root / "base.mp4", root)
            self.assertEqual({video_size(s) for s in segments}, {(1080, 1920)})
            frames = subprocess.run([
                "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(root / "base.mp4"),
            ], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(int(frames), 36)  # 0.5s + 0.7s at 30 fps

    def test_audio_crossfades_never_dip_to_silence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_source(root / "a.mp4", "320x180", 3, freq=300)
            make_source(root / "b.mp4", "320x180", 3, freq=500)
            edl = {"sources": {"A": "a.mp4", "B": "b.mp4"},
                   "ranges": [{"source": "A", "start": 0.5, "end": 1.5}, {"source": "B", "start": 1.0, "end": 2.0}]}
            wav = root / "mix.wav"
            with contextlib.redirect_stdout(io.StringIO()):
                render.build_audio_track(edl, root, "30/1", wav)
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-f", "f32le", "-"],
                                 check=True, capture_output=True).stdout
            samples = np.frombuffer(raw, np.float32)
            self.assertAlmostEqual(len(samples) / 48000, 2.0, delta=0.002)
            window = 240  # 5ms
            cut = 48000
            levels = [np.sqrt(np.mean(samples[i:i + window] ** 2)) for i in range(cut - 1440, cut + 1440, window)]
            steady = np.sqrt(np.mean(samples[12000:24000] ** 2))
            # Equal-power crossfade of two uncorrelated tones stays near unity;
            # the old fade-to-silence went to zero at the boundary.
            self.assertGreater(min(levels) / steady, 0.6)


if __name__ == "__main__":
    unittest.main()
