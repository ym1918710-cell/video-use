"""Audio post, J/L cuts, transitions, face framing, auto punch, LUT, styled
captions, QC additions, NLE export, take comparison, deliverables."""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

HELPERS = Path(__file__).parents[1] / "helpers"
sys.path.insert(0, str(HELPERS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"video_use_r2_{name}", HELPERS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load("render")
audio_mix = load("audio_mix")
face_anchor = load("face_anchor")
captions = load("build_captions")
verify = load("verify_render")
export = load("export_timeline")
takes = load("compare_takes")
validate = load("validate_edl")
run_edit = load("run_edit")

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def tone_source(path: Path, freq: int, seconds: float, color: str = "red") -> None:
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x284:r=25:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def dominant_freq(samples: np.ndarray, rate: int = 48000) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.fft.rfftfreq(len(samples), 1 / rate)[np.argmax(spectrum)])


class AudioPostTests(unittest.TestCase):
    def test_speech_regions_and_duck_shape(self):
        rate = 16000
        t = np.arange(rate * 6) / rate
        voice = 0.3 * np.sin(2 * np.pi * 200 * t) * ((t < 2) | (t > 4)).astype(np.float32)
        regions = audio_mix.speech_regions(voice.astype(np.float32), rate)
        self.assertEqual(len(regions), 2)
        self.assertAlmostEqual(regions[0][1], 2.0, delta=0.05)
        shape = audio_mix.duck_shape(6.0, regions, attack=0.2, release=0.4, min_gap=0.7)
        self.assertEqual(shape[1000], 1.0)          # speech → ducked
        self.assertEqual(shape[3000], 0.0)          # 2 s pause → open
        self.assertAlmostEqual(float(shape[2200]), 0.5, delta=0.02)  # release ramp
        bridged = audio_mix.duck_shape(6.0, [(0, 2), (2.5, 4)], 0.2, 0.4, 0.7)
        self.assertEqual(bridged[2250], 1.0)        # short pause stays ducked

    def test_split_edit_shifts(self):
        ranges = [{"start": 0, "end": 2}, {"start": 5, "end": 7, "j_cut_ms": 300}, {"start": 9, "end": 10, "l_cut_ms": 200}]
        self.assertEqual(render.boundary_audio_shifts(ranges), [0.0, -0.3, 0.2, 0.0])

    def test_audio_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            issues = audio_mix.validate_audio({"audio": {"music": [{"file": "missing.mp3", "bed_lu": -5}],
                                                         "sfx": [{"file": "missing.wav"}],
                                                         "dialogue": {"preset": "loud"}}}, base, 10)
        codes = {i[1] for i in issues}
        self.assertTrue({"audio_file_missing", "music_bed_loud", "invalid_audio_time", "invalid_dialogue_chain"} <= codes)

    @unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
    def test_music_is_ducked_under_speech_and_rises_in_pauses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:r=48000:d=8",
                            "-af", "volume='if(between(t,2,4.5),0,0.3)':eval=frame", "-ac", "2", str(root / "dlg.wav")], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=c=pink:r=48000:d=3",
                            "-ac", "2", str(root / "bed.wav")], check=True)
            edl = {"audio": {"music": [{"file": "bed.wav", "fade_in_ms": 50, "fade_out_ms": 50}]}}
            with contextlib.redirect_stdout(io.StringIO()):
                mix = audio_mix.build_mix(edl, root, root / "dlg.wav", 8.0)
            music = audio_mix.read_mono(root / "stems" / "music.wav")
            self.assertTrue(mix.exists())
        level = lambda a, b: 20 * np.log10(np.sqrt(np.mean(music[int(a * 16000):int(b * 16000)] ** 2)) + 1e-9)
        ducked, open_ = level(0.6, 1.6), level(2.8, 3.8)
        self.assertAlmostEqual(open_ - ducked, 11.0, delta=1.5)   # gap_lu − bed_lu
        self.assertAlmostEqual(len(music) / 16000, 8.0, delta=0.01)  # looped to full length

    @unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
    def test_j_cut_brings_incoming_sound_under_outgoing_picture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tone_source(root / "a.mp4", 300, 4)
            tone_source(root / "b.mp4", 1200, 4, "blue")
            edl = {"sources": {"A": "a.mp4", "B": "b.mp4"},
                   "ranges": [{"source": "A", "start": 0, "end": 2}, {"source": "B", "start": 1.5, "end": 3, "j_cut_ms": 400}]}
            with contextlib.redirect_stdout(io.StringIO()):
                render.build_audio_track(edl, root, "25", root / "mix.wav")
            samples = audio_mix.read_mono(root / "mix.wav", 48000)
        timing = render.segment_timing(edl["ranges"], "25")
        self.assertAlmostEqual(len(samples) / 48000, timing[-1][1] + timing[-1][2], delta=0.002)
        before_cut = samples[int(1.7 * 48000):int(1.9 * 48000)]  # picture still A
        self.assertAlmostEqual(dominant_freq(before_cut), 1200, delta=20)


class FramingTests(unittest.TestCase):
    def test_face_centre_drives_the_crop(self):
        chain = render.framing_filter((1080, 1920), (3840, 2160), framing={"face_center": [0.25, 0.4]})
        crop = chain.split(",")[0]
        w, h, x, y = (int(v) for v in crop.removeprefix("crop=").split(":"))
        self.assertAlmostEqual(x + w / 2, 0.25 * 3840, delta=2)
        self.assertEqual(y, 0)  # clamped at the top edge

    def test_face_centre_with_rotation_and_median(self):
        faces = {"1.0": [{"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2}], "2.0": [], "3.0": [{"x": 0.12, "y": 0.2, "w": 0.2, "h": 0.2}]}
        self.assertEqual(face_anchor.face_center(faces, [1.0, 2.0, 3.0]), (0.21, 0.3))
        self.assertEqual(face_anchor.rotate_point(0.2, 0.3, "cw"), (0.7, 0.2))

    def test_auto_punch_alternates_on_jump_cuts_and_respects_manual(self):
        ranges = [{"source": "A", "start": 0, "end": 2}, {"source": "A", "start": 2.5, "end": 4},
                  {"source": "A", "start": 4.2, "end": 6}, {"source": "B", "start": 0, "end": 1},
                  {"source": "B", "start": 1.2, "end": 2, "punch_in": {"scale": 1.05}}, {"source": "B", "start": 2.2, "end": 3}]
        punches = face_anchor.plan_auto_punch(ranges, {"scale": 1.12})
        self.assertEqual([bool(p) for p in punches], [False, True, False, False, True, False])
        self.assertEqual(punches[1]["scale"], 1.12)
        self.assertEqual(punches[4], {"scale": 1.05})
        self.assertEqual(face_anchor.plan_auto_punch(ranges, None)[1], None)
        self.assertTrue(face_anchor.needs_faces({"auto_punch": True}))

    def test_lut_intensity_blend(self):
        self.assertEqual(render.lut_filter({}, Path(".")), "")
        full = render.lut_filter({"lut": "/x/look.cube"}, Path("."))
        self.assertTrue(full.startswith("lut3d=file="))
        half = render.lut_filter({"lut": "/x/look.cube", "intensity": 0.4}, Path("."))
        self.assertIn("blend=all_expr='A*0.600+B*0.400'", half)


@unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
class TransitionTests(unittest.TestCase):
    def test_dissolve_keeps_length_and_blends_the_cut(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tone_source(root / "a.mp4", 300, 3, "red")
            tone_source(root / "b.mp4", 600, 3, "blue")
            edl = {"sources": {"A": "a.mp4", "B": "b.mp4"},
                   "ranges": [{"source": "A", "start": 0, "end": 1.2},
                              {"source": "B", "start": 1.0, "end": 2.2, "transition": {"type": "dissolve", "duration_ms": 400}}]}
            plans: list = []
            with contextlib.redirect_stdout(io.StringIO()):
                segments = render.extract_all_segments(edl, root, preview=True, plans_out=plans)
                render.concat_segments(segments, root / "base.mp4", root)
                out = render.apply_transitions(edl, root, root / "base.mp4", plans, "25", True, False)
            frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                     "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                                    capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(int(frames), 60)
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.2", "-i", str(out), "-frames:v", "1",
                                  "-vf", "scale=8:8", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                 capture_output=True, check=True).stdout
            pixel = np.frombuffer(raw, np.uint8).reshape(-1, 3).mean(axis=0)
        self.assertGreater(pixel[0], 60)   # still some red …
        self.assertGreater(pixel[2], 60)   # … and already some blue


class CaptionStyleTests(unittest.TestCase):
    def test_colour_and_rtl_helpers(self):
        self.assertEqual(captions.ass_color("#FF8800", 0.55), "&H730088FF")
        self.assertTrue(captions.rtl_line("dashboard وفاهم").startswith("‏"))
        self.assertEqual(captions.rtl_line("plain"), "plain")

    def test_captions_move_clear_of_graphics_and_hide_under_end_card(self):
        skin = dict(captions.CAPTION_DEFAULTS)
        free = captions.caption_margin((1080, 1920), skin, (1, 2), [])
        box = {"start_in_output": 0, "duration": 5, "x": 0, "y": 1450, "w": 600, "h": 200}
        moved = captions.caption_margin((1080, 1920), skin, (1, 2), [box])
        self.assertNotEqual(free, moved)
        caption_bottom = 1920 - moved
        caption_top = caption_bottom - round(1920 * 0.03 * 2.8)
        self.assertFalse(caption_top < 1650 and 1450 < caption_bottom)
        end_card = {"start_in_output": 1.5, "duration": 2, "x": 0, "y": 0, "w": 1080, "h": 1920, "type": "end_card"}
        self.assertIsNone(captions.caption_margin((1080, 1920), skin, (1, 2), [end_card]))

    def test_brand_skin_and_word_highlight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "transcripts").mkdir()
            (root / "transcripts" / "A.json").write_text(json.dumps({"words": [
                {"type": "word", "text": "خمس", "start": 0.0, "end": 0.3},
                {"type": "word", "text": "أيام", "start": 0.35, "end": 0.7},
                {"type": "word", "text": "بس.", "start": 0.75, "end": 1.0}]}, ensure_ascii=False), encoding="utf-8")
            (root / "brand_kit.json").write_text(json.dumps({"caption_skin": {"box": True, "highlight": "#00FF00"},
                                                             "fonts": {"arabic": "Geeza Pro"}}))
            edl = {"sources": {"A": "a.mp4"}, "ranges": [{"source": "A", "start": 0, "end": 1.1}], "captions": True}
            ass = captions.build_styled_ass(edl, root, (1080, 1920))
            needs_track = captions.needs_coretext_track(edl, root)
        events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 3)                       # one per highlighted word
        self.assertIn("{\\c&H00FF00&}", events[1])
        self.assertIn(",4,", ass.split("Style: Caption,")[1])  # line box (BorderStyle 4)
        self.assertTrue(ass.split("Style: Caption,")[1].splitlines()[0].endswith(",-1"))
        self.assertEqual(needs_track, sys.platform == "darwin")


class QCTests(unittest.TestCase):
    def test_detect_parsers(self):
        stderr = ("lavfi.freezedetect.freeze_start: 3.2\n lavfi.freezedetect.freeze_duration: 2.8\n"
                  "[blackdetect @ 0x1] black_start:9.5 black_end:10 black_duration:0.5\n")
        events = verify.parse_detect_events(stderr)
        self.assertEqual(events["freezes"], [{"start_s": 3.2, "duration_s": 2.8}])
        self.assertEqual(events["black"], [{"start_s": 9.5, "duration_s": 0.5}])

    def test_caption_reading_merges_highlight_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "c.ass"
            path.write_text("\n".join([
                "Dialogue: 0,0:00:00.00,0:00:00.40,Caption,,0,0,0,,‏{\\c&H00FF00&}one{\\r} two",
                "Dialogue: 0,0:00:00.40,0:00:01.00,Caption,,0,0,0,,‏one {\\c&H00FF00&}two{\\r}",
                "Dialogue: 0,0:00:01.00,0:00:01.30,Caption,,0,0,0,,averyveryveryverylongcaptionline",
            ]), encoding="utf-8")
            reading = verify.caption_reading(path)
        self.assertEqual(reading["cues"], 2)
        self.assertEqual(len(reading["too_fast"]), 1)
        self.assertEqual(len(reading["too_short"]), 1)

    def test_pacing_report(self):
        edl = {"ranges": [{"source": "A", "start": 0, "end": 4}, {"source": "A", "start": 5, "end": 6}],
               "graphics": [{"type": "sticker", "start_in_output": 1, "duration": 1}]}
        report = verify.pacing_report(edl, Path("/nonexistent"))
        self.assertEqual(report["shots"], 2)
        self.assertEqual(report["longest_static_stretch_s"], 2.0)


class ExportAndWorkflowTests(unittest.TestCase):
    def test_timecode_and_cmx_and_xml(self):
        self.assertEqual(export.tc(30 * 3661 + 5, 30), "01:01:01:05")
        self.assertEqual(export.timebase("30000/1001"), (30, True))
        edl = {"sources": {"C0103": "/media/C0103.MP4"},
               "ranges": [{"source": "C0103", "start": 1.0, "end": 2.5}, {"source": "C0103", "start": 4.0, "end": 5.0}]}
        original = export.probe
        export.probe = lambda path: {"format": {"duration": "60", "tags": {"timecode": "01:00:00:00"}},
                                     "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}
        try:
            cmx = export.cmx3600(edl, Path("/"), "25", "Cut")
            xml = export.fcp7_xml(edl, Path("/"), "25", "Cut", (1080, 1920))
        finally:
            export.probe = original
        lines = [l for l in cmx.splitlines() if l[:3].isdigit()]
        self.assertEqual(len(lines), 2)
        self.assertIn("01:00:01:00 01:00:02:12 00:00:00:00 00:00:01:12", lines[0])  # 37.5 frames → 37
        self.assertIn("00:00:01:12 00:00:02:12", lines[1])
        root = ET.fromstring(xml.split("\n", 2)[2])
        items = root.findall(".//video/track/clipitem")
        self.assertEqual([i.find("in").text for i in items], ["25", "100"])
        self.assertEqual(len(root.findall(".//file[pathurl]")), 1)

    def test_take_comparison_ranks_cleaner_take_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "transcripts").mkdir()
            def words(text, start, gaps=()):
                out, t = [], start
                for i, w in enumerate(text.split()):
                    t += 0.4 if i in gaps else 0.0  # a hesitation, not a phrase break
                    out.append({"type": "word", "text": w, "start": round(t, 2), "end": round(t + 0.3, 2)})
                    t += 0.35
                return out
            (root / "transcripts" / "T1.json").write_text(json.dumps({"words":
                words("يعني الداتا اه مهمة جدا لكل شركة", 0, gaps=(2, 4))}, ensure_ascii=False), encoding="utf-8")
            (root / "transcripts" / "T2.json").write_text(json.dumps({"words":
                words("الداتا مهمة جدا لكل شركة", 0)}, ensure_ascii=False), encoding="utf-8")
            report = takes.compare(root)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["takes"][0]["source"], "T2")

    def test_deliverable_edls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edl = root / "edl.json"
            edl.write_text(json.dumps({"sources": {}, "ranges": [], "canvas": {"width": 1080, "height": 1920},
                                       "deliverables": [{"name": "square", "canvas": {"width": 1080, "height": 1080}}]}))
            pairs = run_edit.write_deliverable_edls(edl, root / "final.mp4")
            variant = json.loads(pairs[0][0].read_text())
        self.assertEqual(pairs[0][1].name, "final_square.mp4")
        self.assertEqual(variant["canvas"], {"width": 1080, "height": 1080})
        self.assertNotIn("deliverables", variant)

    def test_extra_validation(self):
        issues = validate.validate_extras({
            "ranges": [{"source": "A", "start": 0, "end": 1, "transition": "dissolve"},
                       {"source": "A", "start": 2, "end": 3, "transition": {"type": "spin"}, "j_cut_ms": 100, "l_cut_ms": 100}],
            "auto_punch": {"scale": 2}, "grade": {"lut": "missing.cube", "intensity": 3},
            "captions": {"position": 1.2}, "deliverables": [{"name": "x", "canvas": {"width": 15, "height": 20}}],
        }, Path("/nonexistent"))
        codes = {i[1] for i in issues}
        self.assertTrue({"invalid_transition", "invalid_split_edit", "invalid_auto_punch", "lut_missing",
                         "invalid_grade", "invalid_captions", "invalid_deliverable"} <= codes)


if __name__ == "__main__":
    unittest.main()
