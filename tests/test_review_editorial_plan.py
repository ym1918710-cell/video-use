import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "review_editorial_plan.py"
SPEC = importlib.util.spec_from_file_location("video_use_review_editorial_plan", MODULE_PATH)
assert SPEC and SPEC.loader
reviewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewer)


class EditorialReviewTests(unittest.TestCase):
    def test_flags_a_protected_moment_cut_in_half(self):
        report = reviewer.review(
            {"ranges": [{"source": "A", "start": 0, "end": 3}]},
            {"protected_moments": [{"source": "A", "start": 2, "end": 4}]},
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["warnings"][0]["code"], "protected_moment_partially_cut")

    def test_accepts_a_wholly_preserved_moment_and_proposed_asset(self):
        report = reviewer.review(
            {"ranges": [{"source": "A", "start": 0, "end": 5}]},
            {
                "protected_moments": [{"source": "A", "start": 2, "end": 4}],
                "b_roll": [{"status": "proposed", "purpose": "Clarify the claim"}],
            },
        )
        self.assertTrue(report["passed"])

    def test_flags_an_unapproved_asset_attached_as_media(self):
        report = reviewer.review(
            {"ranges": []},
            {"sound_effects": [{"status": "proposed", "file": "mystery.mp3"}]},
        )
        self.assertEqual(report["warnings"][0]["code"], "unapproved_asset_has_file")


if __name__ == "__main__":
    unittest.main()
