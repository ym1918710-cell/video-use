"""Camera moves, face tracking path, colour match, multicam, library, Linux text engine."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

HELPERS = Path(__file__).parents[1] / "helpers"
sys.path.insert(0, str(HELPERS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"video_use_r3_{name}", HELPERS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


camera = load("camera")
render = load("render")
grade = load("grade")
multicam = load("multicam")
library = load("library")
text_render = load("text_render")
validate = load("validate_edl")

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def dot_positions(path: Path, width: int, height: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, height, width)
    return np.array([np.nonzero(f > 128)[1].mean() for f in frames])


class EasingTests(unittest.TestCase):
    def test_eases_start_at_zero_end_at_one_and_rise(self):
        for ease in camera.EASES:
            values = [camera.ease_value(p / 20, ease) for p in range(21)]
            self.assertAlmostEqual(values[0], 0.0)
            self.assertAlmostEqual(values[-1], 1.0)
            self.assertTrue(all(b >= a - 1e-9 for a, b in zip(values, values[1:])), ease)

    def test_smooth_has_zero_velocity_at_both_ends(self):
        eps = 1e-3
        self.assertLess(camera.ease_value(eps, "smooth") / eps, 0.01)
        self.assertLess((1 - camera.ease_value(1 - eps, "smooth")) / eps, 0.01)

    def test_piecewise_channel(self):
        keys = [(0.0, 1.0, "linear"), (1.0, 1.2, "smooth"), (2.0, 1.0, "in_out")]
        self.assertEqual(camera.channel_value(keys, -1), 1.0)
        self.assertAlmostEqual(camera.channel_value(keys, 0.5), 1.1)
        self.assertAlmostEqual(camera.channel_value(keys, 1.0), 1.2)
        self.assertAlmostEqual(camera.channel_value(keys, 3.0), 1.0)
        self.assertIn("if(lt(", camera.channel_expr(keys, "T"))

    def test_zoom_shorthand_and_static(self):
        keys = camera.normalize_keys({"zoom": {"from": 1.0, "to": 1.2, "start": 0.5, "duration": 1.0}}, (0.5, 0.4), 3.0)
        self.assertAlmostEqual(camera.channel_value(keys["scale"], 0.4), 1.0)
        self.assertAlmostEqual(camera.channel_value(keys["scale"], 1.5), 1.2)
        self.assertEqual(keys["x"], [(0.0, 0.5, "linear")])
        self.assertIsNone(camera.normalize_keys({}, (0.5, 0.5), 3.0))

    def test_track_path_locks_when_still_and_follows_when_moving(self):
        still = [(t / 5, 0.5 + 0.005 * (t % 2), 0.4) for t in range(20)]
        self.assertIsNone(camera.smooth_track(still))
        moving = [(t / 5, 0.3 + 0.02 * t, 0.4) for t in range(20)] + [(4.0, None, None)]
        path = camera.smooth_track(moving)
        self.assertGreater(path[-1][1], path[0][1] + 0.2)
        self.assertTrue(all(b[0] > a[0] for a, b in zip(path, path[1:])))


@unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
class CameraRenderTests(unittest.TestCase):
    def test_keyframed_zoom_in_and_out_is_smooth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=4",
                            "-vf", "drawbox=x=440:y=230:w=10:h=10:color=white:t=fill", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-crf", "1", str(root / "dot.mp4")], check=True)
            motion = {"spec": {"camera": [{"t": 0, "scale": 1.0}, {"t": 1.2, "scale": 1.3, "ease": "smooth"},
                                          {"t": 2.6, "scale": 1.0, "ease": "in_out"}]}, "duration": 3.0}
            render.extract_segment(root / "dot.mp4", 0, 3.0, "", root / "z.mp4", rate="30", canvas=(640, 360),
                                   frames=90, include_audio=False, motion=motion)
            xs = dot_positions(root / "z.mp4", 640, 360)
        velocity = np.diff(xs)
        self.assertAlmostEqual(xs[0], xs[-1], delta=1.0)               # zoomed back out
        self.assertGreater(xs.max(), xs[0] + 20)                       # really zoomed in
        out_phase, in_phase = velocity[:36], velocity[36:78]
        self.assertTrue((out_phase > -0.3).all() and (in_phase < 0.3).all())  # no reversals / shake
        self.assertLess(np.abs(np.diff(velocity)).max(), 1.5)          # no jumps in speed


class ColourMatchTests(unittest.TestCase):
    def test_levels_transfer(self):
        src = {"r": [20, 100, 200], "g": [10, 90, 180], "b": [30, 120, 230]}
        ref = {"r": [10, 100, 220], "g": [10, 90, 180], "b": [10, 100, 200]}
        chain = grade.match_filter(src, ref)
        self.assertTrue(chain.startswith("lutrgb="))
        self.assertIn("g='clip(10.00+170.00*pow(clip((val-(10.00))/170.00,0,1),1.0000),0,255)'", chain)  # already equal
        self.assertNotIn("1.0000),0,255)'", chain.split(":g=")[0])  # red needs a midtone correction

    @unittest.skipUnless(HAS_FFMPEG, "requires ffmpeg")
    def test_matched_camera_lands_near_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, vf in (("ref", "eq=saturation=1"), ("warm", "colorbalance=rs=0.25:bs=-0.25,eq=contrast=0.8")):
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
                                "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / f"{name}.mp4")], check=True)
            ref = grade.rgb_percentiles(root / "ref.mp4", [(0, 2)])
            warm = grade.rgb_percentiles(root / "warm.mp4", [(0, 2)])
            chain = grade.match_filter(warm, ref)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(root / "warm.mp4"), "-vf", chain,
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "fixed.mp4")], check=True)
            fixed = grade.rgb_percentiles(root / "fixed.mp4", [(0, 2)])
        error_before = sum(abs(warm[c][1] - ref[c][1]) for c in "rgb")
        error_after = sum(abs(fixed[c][1] - ref[c][1]) for c in "rgb")
        self.assertLess(error_after, error_before * 0.5)


class MulticamTests(unittest.TestCase):
    def test_offset_estimation(self):
        rng = np.random.default_rng(0)
        master = np.clip(rng.normal(size=3000), 0, None)
        angle = np.concatenate([np.zeros(184), master])[:3000]  # angle starts 1.84 s later in its file
        offset, confidence = multicam.estimate_offset(master, angle)
        self.assertAlmostEqual(offset, 1.84, delta=0.011)
        self.assertGreater(confidence, 0.5)

    def test_shot_plan_follows_speakers_and_merges_flashes(self):
        config = {"angles": {"A": {"speaker": "s0"}, "B": {"speaker": "s1"}, "W": {"wide": True}},
                  "min_shot_s": 1.5, "max_shot_s": 7.0, "switch_delay_s": 0.0}
        segments = [(0, 4, "s0"), (4, 4.6, "s1"), (4.6, 8, "s0"), (8, 12, "s1")]
        shots = multicam.plan_shots(segments, 0, 12, config)
        self.assertEqual([s[2] for s in shots], ["A", "B"])  # the 0.6 s interjection never gets a cut
        self.assertEqual(shots[0][0], 0)
        self.assertEqual(shots[-1][1], 12)
        long = multicam.plan_shots([(0, 15, "s0")], 0, 15, config)
        self.assertIn("W", [s[2] for s in long])  # long monologue gets a wide break

    def test_switch_keeps_master_sound_and_audio_runs_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "transcripts").mkdir()
            words = [{"type": "word", "text": "a", "start": t, "end": t + 0.3, "speaker_id": "s0" if t < 3 else "s1"}
                     for t in np.arange(0, 6, 0.4).round(2)]
            (root / "transcripts" / "M.json").write_text(json.dumps({"words": words}))
            edl = {"sources": {"M": "m.mp4", "A": "a.mp4", "B": "b.mp4"},
                   "ranges": [{"source": "M", "start": 0.0, "end": 6.0, "beat": "intro"}],
                   "multicam": {"master": "M", "angles": {"A": {"speaker": "s0"}, "B": {"speaker": "s1"}},
                                "offsets": {"A": 0.5, "B": -0.2}, "switch_delay_s": 0.0}}
            result = multicam.switch(edl, root)
        ranges = result["ranges"]
        self.assertEqual([r["source"] for r in ranges], ["A", "B"])
        self.assertEqual(ranges[0]["start"], 0.5)                   # offset applied to the picture
        self.assertEqual(ranges[0]["audio_source"], {"source": "M", "start": 0.0})
        self.assertEqual(ranges[0]["beat"], "intro")
        self.assertEqual(render.audio_runs(ranges), [(0, 1)])      # one continuous sound part
        self.assertEqual(render.audio_runs([{"source": "X", "start": 0, "end": 1}, {"source": "X", "start": 2, "end": 3}]),
                         [(0, 0), (1, 1)])


class LibraryTests(unittest.TestCase):
    def test_bpm_from_clicks(self):
        env = np.zeros(3000)
        env[::50] = 1.0  # a click every 0.5 s → 120 bpm
        self.assertAlmostEqual(library.estimate_bpm(env), 120, delta=2)

    def test_search_suggest_and_resolve(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "library.json"
            lib = {"version": 1, "items": {
                "a1": {"id": "a1", "file": "/x/hype.wav", "name": "hype", "type": "music", "duration_s": 60, "bpm": 124,
                       "energy": "high", "moods": ["uplifting", "tech"], "license": "Artlist"},
                "b2": {"id": "b2", "file": "/x/calm.wav", "name": "calm", "type": "music", "duration_s": 90, "bpm": 80,
                       "energy": "calm", "moods": ["ambient"], "license": "Artlist"},
                "c3": {"id": "c3", "file": "/x/pop.wav", "name": "pop", "type": "sfx", "duration_s": 0.3, "bpm": None,
                       "energy": "high", "moods": []}}}
            library.save(lib, index)
            with patch.dict(os.environ, {"VIDEO_USE_LIBRARY": str(index)}):
                self.assertEqual(library.suggest(library.load(), "kinetic_promo", 45)[0]["id"], "a1")
                self.assertEqual(library.suggest(library.load(), "static_qa")[0]["id"], "b2")
                self.assertEqual(library.resolve("library:a1"), Path("/x/hype.wav"))
                with self.assertRaises(FileNotFoundError):
                    library.resolve("library:zz")
        self.assertEqual([i["id"] for i in library.search(lib, kind="sfx")], ["c3"])


class TextEngineTests(unittest.TestCase):
    def test_word_level_bidi(self):
        words = [("dashboard", "#F"), ("وفاهم", "#W"), ("كل", "#W"), ("الـ", "#W")]
        ordered = [w for w, _ in text_render.visual_order(words, rtl=True)]
        self.assertEqual(ordered, ["الـ", "كل", "وفاهم", "dashboard"])  # left → right on screen
        mixed = [("والحق", "#W"), ("free", "#W"), ("data", "#W"), ("analysis", "#W")]
        self.assertEqual([w for w, _ in text_render.visual_order(mixed, rtl=True)], ["free", "data", "analysis", "والحق"])

    def test_span_colours_by_utf16_offset(self):
        line = {"text": "خمس أيام هتتعلم", "color": "#FFF", "spans": [{"start": 4, "length": 4, "color": "#00F"}]}
        self.assertEqual(text_render.tokens_with_colors(line), [("خمس", "#FFF"), ("أيام", "#00F"), ("هتتعلم", "#FFF")])

    @unittest.skipUnless(shutil.which("fc-match"), "requires fontconfig")
    def test_pillow_engine_renders_a_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "card.png"
            text_render.render_cards([{"file": str(out), "lines": [{"text": "Free Bootcamp", "size": 40, "color": "#1F3FD9",
                                                                    "weight": "bold"}],
                                       "fill": {"colors": ["#FFFFFF"]}, "stroke": {"colors": ["#2B6BFF", "#8A3CFF"]},
                                       "strokeWidth": 4, "radius": 999, "padX": 20, "padY": 10, "maxWidth": 600,
                                       "shadow": {"color": "#00000066", "blur": 8, "dx": 0, "dy": 4}}])
            from PIL import Image
            image = Image.open(out)
            self.assertEqual(image.mode, "RGBA")
            self.assertGreater(image.width, 200)
            self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_arabic_without_raqm_fails_clearly(self):
        if text_render.raqm_available():
            self.skipTest("libraqm is available here")
        with self.assertRaises(RuntimeError):
            text_render.render_cards([{"file": "/tmp/x.png", "lines": [{"text": "أيام", "size": 20, "color": "#FFF"}],
                                       "padX": 1, "padY": 1, "maxWidth": 100}])


class CaptionSourceTests(unittest.TestCase):
    def test_broll_captions_use_audio_source_and_ranges_can_opt_out(self):
        captions = load("build_captions")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "transcripts").mkdir()
            words = [{"type": "word", "text": w, "start": s, "end": s + 0.3}
                     for w, s in (("سؤال", 0.1), ("أول", 0.5), ("إجابة", 2.1), ("طويلة", 2.5))]
            (root / "transcripts" / "T.json").write_text(json.dumps({"words": words}, ensure_ascii=False))
            edl = {"ranges": [{"source": "T", "start": 0, "end": 1, "captions": False},
                              {"source": "LAB", "start": 40, "end": 41, "audio_source": {"source": "T", "start": 2.0}}]}
            cues = captions.caption_cues(edl, root, 4)
        self.assertEqual([[w[2] for w in c[2]] for c in cues], [["إجابة", "طويلة"]])
        self.assertAlmostEqual(cues[0][0], 1.1)  # output time: after the 1 s question range


class FirstCutAndFaceTests(unittest.TestCase):
    def test_first_cut_strips_finishing_layers(self):
        run_edit = load("run_edit")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "edl.json"
            path.write_text(json.dumps({"sources": {"A": "a.mp4"}, "ranges": [{"source": "A", "start": 0, "end": 1}],
                                        "graphics": [{}], "captions": True, "overlays": [],
                                        "audio": {"crossfade_ms": 30, "dialogue": {"preset": "clean"},
                                                  "music": [{"file": "m.wav"}], "sfx": [{"file": "s.wav"}]}}))
            cut = json.loads(run_edit.write_first_cut_edl(path).read_text())
        self.assertNotIn("graphics", cut)
        self.assertNotIn("captions", cut)
        self.assertEqual(cut["audio"], {"crossfade_ms": 30, "dialogue": {"preset": "clean"}, "auto_sfx": "first_cut"})

    def test_graphics_and_captions_never_cover_a_face(self):
        graphics = load("graphics")
        captions = load("build_captions")
        face = {"type": "face", "start_in_output": 0, "duration": 5, "x": 400, "y": 300, "w": 300, "h": 380}
        entry = {"type": "sticker", "start_in_output": 1, "duration": 1, "x": 350, "y": 400, "w": 380, "h": 120}
        with contextlib.redirect_stdout(io.StringIO()):
            moved = graphics.clear_of_faces(entry, [face], (1080, 1920))
        self.assertFalse(moved["y"] < 680 and moved["y"] + 120 > 300)
        self.assertEqual(graphics.clear_of_faces({**entry, "start_in_output": 6}, [face], (1080, 1920)), {**entry, "start_in_output": 6})
        skin = dict(captions.CAPTION_DEFAULTS, position=0.3)
        margin = captions.caption_margin((1080, 1920), skin, (1, 2), [face])
        bottom = 1920 - margin
        top = bottom - round(1920 * 0.03 * 2.8)
        self.assertFalse(top < 680 and bottom > 300)


class ValidationTests(unittest.TestCase):
    def test_round3_rules(self):
        issues = validate.validate_extras({
            "sources": {"M": "m.mp4", "A": "a.mp4"},
            "ranges": [{"source": "A", "start": 0, "end": 1, "zoom": {"from": 1, "to": 3}, "track": "body",
                        "camera": [{"t": 0, "scale": 1.1, "ease": "bounce"}], "audio_source": {"source": "Q"}}],
            "grade": {"match": "Z"},
            "multicam": {"master": "M", "angles": {"A": {}, "X": {}}},
        }, Path("/nonexistent"))
        codes = {i[1] for i in issues}
        self.assertTrue({"invalid_zoom", "invalid_track", "invalid_camera_key", "invalid_audio_source",
                         "invalid_grade", "invalid_multicam"} <= codes)


if __name__ == "__main__":
    unittest.main()


class LightTransitionTests(unittest.TestCase):
    def test_broll_rule_applies_only_at_broll_boundaries(self):
        from render import resolve_transitions
        edl = {"ranges": [
            {"source": "A", "start": 0, "end": 1},
            {"source": "B", "start": 0, "end": 1, "audio_source": {"source": "A", "start": 1}},
            {"source": "C", "start": 0, "end": 1, "audio_source": {"source": "A", "start": 2}},
            {"source": "A", "start": 3, "end": 4},
            {"source": "A", "start": 5, "end": 6},
            {"source": "B", "start": 2, "end": 3, "broll": True, "transition": "cut"},
        ]}
        brand = {"transitions": {"broll": "light"}}
        got = [t and t["type"] for t in resolve_transitions(edl, brand)]
        self.assertEqual(got, [None, "light", None, "light", None, None])
        edl["transitions"] = {"broll": {"type": "dissolve"}}
        self.assertEqual(resolve_transitions(edl, brand)[1]["type"], "dissolve")

    def test_light_curve_peaks_on_the_cut_frame(self):
        from render import light_curve
        curve = light_curve(6, 7)
        self.assertEqual(len(curve), 14)
        self.assertEqual(curve.index(max(curve)), 6)
        self.assertTrue(all(a < b for a, b in zip(curve[:6], curve[1:7])))
        self.assertLess(curve[-1], 0.1)

    def test_validator_accepts_light_and_rule(self):
        from validate_edl import validate_extras
        edl = {"sources": {"A": "a.mp4"}, "transitions": {"broll": "light"},
               "ranges": [{"source": "A", "start": 0, "end": 1}, {"source": "A", "start": 1, "end": 2, "transition": "light"}]}
        self.assertFalse([x for x in validate_extras(edl, Path(".")) if x[0] == "error"])
        edl["transitions"] = {"broll": "sparkle"}
        self.assertTrue([x for x in validate_extras(edl, Path(".")) if x[0] == "error"])


class LightFlashDetectionTests(unittest.TestCase):
    def test_detects_flash_and_recovers_colour(self):
        sys.path.insert(0, str(HELPERS))
        from analyze_references import find_light_flashes
        from render import light_curve
        frames = np.full((60, 16, 9, 3), 90, np.uint8)
        frames[30:] = 120
        color = np.array([0x4F, 0x00, 0xFF]) / 255
        curve = light_curve(6, 7)
        for k, s in enumerate(curve):
            i = 24 + k
            tint = color + (1 - color) * 0.35 if k == 6 else color
            frames[i] = np.clip(255 - (255 - frames[i].astype(float)) * (1 - s * tint), 0, 255)
        events = find_light_flashes(frames, 30.0)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["time_s"], 1.0, places=2)
        self.assertGreaterEqual(events[0]["pre_frames"], 4)
        r, g, b = (int(events[0]["color"][k:k + 2], 16) for k in (1, 3, 5))
        self.assertGreater(b, 240); self.assertLess(g, 20); self.assertTrue(60 < r < 100)


class GraphicCollisionTests(unittest.TestCase):
    def test_moved_graphic_avoids_graphics_already_on_screen(self):
        sys.path.insert(0, str(HELPERS))
        from graphics import clear_of_faces
        face = {"x": 400, "y": 380, "w": 200, "h": 200, "start_in_output": 0, "duration": 5}
        first = {"type": "sticker", "x": 400, "y": 600, "w": 300, "h": 100, "start_in_output": 0, "duration": 5}
        entry = {"type": "sticker", "x": 400, "y": 400, "w": 300, "h": 100, "start_in_output": 1, "duration": 2}
        moved = clear_of_faces(entry, [face], (1080, 1920), [first])
        box = (moved["x"], moved["y"], moved["x"] + 300, moved["y"] + 100)
        for o in (face, first):
            self.assertFalse(box[0] < o["x"] + o["w"] and o["x"] < box[2] and box[1] < o["y"] + o["h"] and o["y"] < box[3])


class TextStyleAndSfxTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HELPERS))

    def test_drop_shadow_grows_canvas_and_darkens_below(self):
        from graphics import add_drop_shadow
        from PIL import Image
        card = Image.new("RGBA", (100, 40), (255, 255, 255, 255))
        out = add_drop_shadow(card, {"color": "#000000", "opacity": 0.6, "blur_ratio": 0.01, "offset_ratio": 0.02}, 1000)
        self.assertGreater(out.width, 100)
        pad = (out.width - 100) // 2
        below = out.getpixel((pad + 50, pad + 40 + 8))
        self.assertGreater(below[3], 40)

    def test_reveal_and_new_animations(self):
        from graphics import reveal_at, transform_at
        self.assertEqual(reveal_at("typewriter", 0.05, 0.4, 4)[0], 0.25)
        self.assertEqual(reveal_at("wipe", 0.4, 0.4)[0], 1.0)
        self.assertGreater(transform_at("stamp", 0.0, 2, 0.3, 0.1, 1000)[0], 1.4)
        self.assertLess(transform_at("drop", 0.02, 2, 0.3, 0.1, 1000)[2], 0)

    def test_highlight_spans_utf16(self):
        from graphics import highlight_spans
        spans = highlight_spans("أفضل أكاديمية", ["أكاديمية"], "#FFD23F")
        self.assertEqual(spans, [{"start": 5, "length": 8, "color": "#FFD23F"}])

    def test_every_effect_gets_a_sound(self):
        from sfx_library import auto_sfx, effect_events, silent_effects
        edl = {"ranges": [{"source": "A", "start": 0, "end": 2},
                          {"source": "B", "start": 0, "end": 2, "audio_source": {"source": "A", "start": 2}},
                          {"source": "A", "start": 4, "end": 6, "camera": [{"t": 1, "scale": 1.0}, {"t": 1.6, "scale": 1.4}]}],
               "graphics": [{"type": "sticker", "text": "a b", "start_in_output": 0.5, "duration": 1},
                            {"type": "headline", "text": "x", "start_in_output": 4.1, "duration": 1, "sfx": False}],
               "audio": {"sfx": [{"file": "x.wav", "start_in_output": 2.02}], "sfx_library": False}}
        brand = {"transitions": {"broll": "light"}}
        kinds = [e["kind"] for e in effect_events(edl, "30/1", brand)]
        self.assertIn("transition:light", kinds)
        self.assertIn("camera", kinds)
        with tempfile.TemporaryDirectory() as tmp:
            items = auto_sfx(edl, Path(tmp), "30/1", brand)
            self.assertTrue((Path(tmp) / "sfx" / "auto_pop.wav").exists())
        got = sorted(i["auto"] for i in items)
        # light @2.0 already has a hand-placed hit; headline opted out
        self.assertEqual(got, ["camera", "graphic:sticker", "transition:light"])
        edl["audio"]["auto_sfx"] = False
        self.assertTrue(silent_effects(edl, "30/1", brand))


class ClientTextStyleTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HELPERS))

    def test_kashida_only_after_joining_letters(self):
        from text_styles import stretch_arabic
        out = stretch_arabic("وقتك فلوسك", 3)
        self.assertEqual(out.replace("ـ", ""), "وقتك فلوسك")
        self.assertIn("ـــ", out)
        for k, ch in enumerate(out):
            if ch == "ـ" and k and out[k - 1] != "ـ":
                self.assertNotIn(out[k - 1], "اأإآدذرزوؤةى")
        self.assertEqual(stretch_arabic("Bootcamp", 3), "Bootcamp")

    def test_style_line_layers_outline_and_fill(self):
        from text_styles import style_line
        from PIL import Image, ImageDraw
        mask = Image.new("L", (120, 60), 0)
        ImageDraw.Draw(mask).rectangle((20, 15, 100, 45), fill=255)
        out = style_line(mask, {"fill": ["#0000FF", "#0000FF"], "outline": "#FFFFFF", "outline_ratio": 0.1,
                                "extrude": {"color": "#000080", "depth": 0.1}}, 60)
        px = out.getpixel((out.width // 2, out.height // 3))
        self.assertGreater(px[2], 200)
        self.assertGreater(out.width, 80)

    def test_classify_library_names(self):
        from sfx_library import classify
        self.assertEqual(classify("Swoosh Transition 23"), "swoosh")
        self.assertEqual(classify("NGTVST - Whoosh 2"), "whoosh")
        self.assertEqual(classify("Small Pop 01"), "pop")
        self.assertEqual(classify("Mountain Audio - Stamp - Sound (1)"), "impact")
        self.assertEqual(classify("shine-1-268902"), "shimmer")
        self.assertIsNone(classify("Samurai Sword 03"))


class FixedCaptionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HELPERS))

    def test_repeats_graphic_matches_words_and_logo(self):
        from build_captions import repeats_graphic
        words = [(1.0, 1.3, "أنا"), (1.3, 1.6, "محمد")]
        self.assertTrue(repeats_graphic(words, (1.0, 1.6), [{"type": "sticker", "text": "محمد", "start_in_output": 0.9, "duration": 1}]))
        self.assertFalse(repeats_graphic(words, (1.0, 1.6), [{"type": "sticker", "text": "داتا", "start_in_output": 0.9, "duration": 1}]))
        self.assertTrue(repeats_graphic(words, (1.0, 1.6), [{"type": "logo", "start_in_output": 0.9, "duration": 1}]))
        self.assertFalse(repeats_graphic(words, (1.0, 1.6), [{"type": "face", "start_in_output": 0.9, "duration": 1}]))


class MaterialAndOverlayTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HELPERS))

    def test_kind_by_folder_and_extension(self):
        from material import kind_for, tags_from_name
        root = Path("/m")
        self.assertEqual(kind_for(Path("/m/sound effect/Swoosh 23.wav"), root), "sfx")
        self.assertEqual(kind_for(Path("/m/music/Happy.wav"), root), "music")
        self.assertEqual(kind_for(Path("/m/Font/Cairo-Black.ttf"), root), "font")
        self.assertEqual(kind_for(Path("/m/graphic/arrow.mov"), root), "graphic")
        self.assertEqual(kind_for(Path("/m/BG/texture.jpg"), root), "background")
        self.assertEqual(kind_for(Path("/m/transition/Light Leaks.MOV"), root), "transition")
        self.assertIsNone(kind_for(Path("/m/plugn/Deep Glow/x.png"), root))
        self.assertEqual(kind_for(Path("/m/loose/clip.wav"), root), "audio")
        self.assertIn("question", tags_from_name("question mark ping spam (League of legends green screen effect)"))

    def test_green_screen_keys_out(self):
        from overlay_fx import keyed_rgba, keep_out_matte
        frame = np.zeros((4, 4, 3), np.uint8)
        frame[...] = (0, 255, 0)
        frame[0, 0] = (255, 220, 30)  # a yellow stroke survives
        rgba = keyed_rgba(frame, "green")
        self.assertLess(rgba[2, 2, 3], 0.05)
        self.assertGreater(rgba[0, 0, 3], 0.9)
        matte = keep_out_matte((100, 200), "subject")
        self.assertLess(matte[20, 50], 0.01)   # head band cleared
        self.assertGreater(matte[20, 2], 0.99)  # edges kept
        self.assertGreater(matte[190, 50], 0.99)  # below the shoulders kept


class PremiereExportTests(unittest.TestCase):
    def test_clip_xml_carries_timing_and_level(self):
        sys.path.insert(0, str(HELPERS))
        from export_timeline import _Xml
        x = _Xml("30/1", (1080, 1920))
        x.file = lambda path, kind="video": '<file id="f1"/>'
        clip = x.clip(Path("/tmp/a.wav"), 2.0, 0.5, 1.0, audio=True, gain_db=-6)
        self.assertIn("<start>60</start><end>75</end><in>30</in><out>45</out>", clip)
        self.assertIn("<value>0.50119</value>", clip)
        self.assertIn("<mediatype>audio</mediatype>", clip)


class FinishingLessonTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HELPERS))

    def test_denial_beats_match_the_editor(self):
        from beats import find_denials
        w = lambda t, a, b: {"type": "word", "text": t, "start": a, "end": b}
        words = [w("أنا", 5.56, 5.64), w("اسمي", 5.68, 5.84), w("مش", 5.86, 5.98), w("ممدوح،", 6.02, 6.40),
                 w("محمد،", 6.80, 7.10), w("وأنا", 7.26, 7.42), w("مش", 7.44, 7.56), w("data", 7.58, 7.76),
                 w("مش", 9.0, 9.2), w("عارفة", 9.2, 9.5)]
        got = [(s, round(e - s, 3)) for s, e, _ in find_denials(words)]
        self.assertEqual(got, [(5.8, 0.433), (7.26, 0.433)])  # the bare "مش عارفة" is no denial of identity

    def test_outro_is_detected_and_never_dips_to_black(self):
        from render import outro_index, resolve_transitions
        edl = {"ranges": [{"source": "A", "start": 0, "end": 2}, {"source": "OUT", "start": 0, "end": 3, "outro": True,
                                                                   "transition": {"type": "fade", "in_ms": 600}}]}
        self.assertEqual(outro_index(edl), 1)
        self.assertEqual(resolve_transitions(edl)[1]["type"], "dissolve")

    def test_word_anchored_graphic_is_readable_on_its_word(self):
        from graphics import Tokens, graphic_start, readable_lead
        tok = Tokens({}, Path("."))
        edl = {"ranges": [{"source": "A", "start": 0, "end": 10}]}
        g = {"type": "headline", "text": "x", "at": {"source": "A", "time": 4.0}, "duration": 2, "animation": "rise"}
        lead = readable_lead("rise", 2, tok)
        self.assertGreater(lead, 0)
        self.assertAlmostEqual(graphic_start(g, edl, tok, "30/1"), 4.0 - lead, places=3)
        self.assertEqual(graphic_start(dict(g, anticipate=False), edl, tok, "30/1"), 4.0)
