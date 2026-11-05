import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "validate_edl.py"
SPEC = importlib.util.spec_from_file_location("video_use_validate_edl", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class ValidateEdlTests(unittest.TestCase):
    def write_edl(self, root: Path, body: dict) -> Path:
        path = root / "edl.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def test_valid_edl_passes_when_source_and_overlay_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.mp4").touch()
            (root / "overlay.mp4").touch()
            edl = self.write_edl(root, {
                "sources": {"A": "source.mp4"},
                "ranges": [{"source": "A", "start": 0, "end": 4}],
                "overlays": [{"file": "overlay.mp4", "start_in_output": 1, "duration": 2}],
                "total_duration_s": 4,
            })
            with patch.object(validator, "probe_duration", return_value=10):
                report = validator.validate_edl(edl)
            self.assertTrue(report["valid"])
            self.assertEqual(report["expected_duration_s"], 4.0)

    def test_rejects_unknown_source_and_invalid_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edl = self.write_edl(root, {
                "sources": {"A": "missing.mp4"},
                "ranges": [
                    {"source": "B", "start": 0, "end": 1},
                    {"source": "A", "start": 3, "end": 2},
                ],
            })
            report = validator.validate_edl(edl)
            self.assertFalse(report["valid"])
            self.assertEqual({item["code"] for item in report["errors"]}, {"source_missing", "unknown_source", "invalid_range_bounds"})

    def test_rejects_overlay_that_runs_past_the_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.mp4").touch()
            (root / "overlay.mp4").touch()
            edl = self.write_edl(root, {
                "sources": {"A": "source.mp4"},
                "ranges": [{"source": "A", "start": 0, "end": 3}],
                "overlays": [{"file": "overlay.mp4", "start_in_output": 2, "duration": 2}],
            })
            with patch.object(validator, "probe_duration", return_value=4):
                report = validator.validate_edl(edl)
            self.assertIn("overlay_outside_output", {item["code"] for item in report["errors"]})

    def test_punch_in_requires_a_restrained_scale_and_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.mp4").touch()
            valid = self.write_edl(root, {
                "sources": {"A": "source.mp4"},
                "ranges": [{"source": "A", "start": 0, "end": 3, "punch_in": {"scale": 1.08, "reason": "Key proof"}}],
            })
            with patch.object(validator, "probe_duration", return_value=4):
                self.assertTrue(validator.validate_edl(valid)["valid"])
            invalid = self.write_edl(root, {
                "sources": {"A": "source.mp4"},
                "ranges": [{"source": "A", "start": 0, "end": 3, "punch_in": {"scale": 1.5}}],
            })
            with patch.object(validator, "probe_duration", return_value=4):
                report = validator.validate_edl(invalid)
            self.assertIn("invalid_punch_in", {item["code"] for item in report["errors"]})

    def test_behind_subject_layer_requires_person_mask_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.mp4").touch()
            (root / "chart.mov").touch()
            edl = self.write_edl(root, {
                "sources": {"A": "source.mp4"},
                "ranges": [{"source": "A", "start": 0, "end": 3}],
                "behind_subject_overlays": [{"file": "chart.mov", "start_in_output": 0, "duration": 2}],
            })
            with patch.object(validator, "probe_duration", return_value=4):
                report = validator.validate_edl(edl)
            self.assertIn("subject_mask_required", {item["code"] for item in report["errors"]})


if __name__ == "__main__":
    unittest.main()
