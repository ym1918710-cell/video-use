import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "session_state.py"
SPEC = importlib.util.spec_from_file_location("video_use_session_state", MODULE_PATH)
assert SPEC and SPEC.loader
session_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(session_state)


class SessionStateTests(unittest.TestCase):
    def test_initialize_creates_three_structured_files_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            edit_dir = Path(temporary) / "edit"
            created = session_state.initialize(edit_dir)
            self.assertEqual({path.name for path in created}, {"edit_brief.json", "style_profile.json", "brand_kit.json", "editorial_plan.json", "session_state.json"})
            self.assertEqual(session_state.initialize(edit_dir), [])
            brief = json.loads((edit_dir / "edit_brief.json").read_text())
            self.assertFalse(brief["approval"]["strategy_approved"])
            profile = json.loads((edit_dir / "style_profile.json").read_text())
            self.assertEqual(profile["base_editorial_memory"], "video-use/references/editorial-memory.md")
            self.assertIsNone(profile["style"]["recipe"])
            brand = json.loads((edit_dir / "brand_kit.json").read_text())
            self.assertEqual(brand["client"], "")  # brand-neutral until the user supplies one

    def test_initialize_never_overwrites_creator_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            edit_dir = Path(temporary)
            edit_dir.mkdir(exist_ok=True)
            style = edit_dir / "style_profile.json"
            style.write_text('{"caption": {"font": "My Font"}}', encoding="utf-8")
            session_state.initialize(edit_dir)
            self.assertEqual(json.loads(style.read_text())["caption"]["font"], "My Font")

    def test_update_changes_only_lifecycle_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            edit_dir = Path(temporary)
            state = session_state.update(edit_dir, status="qc_passed", latest_edl="/tmp/edl.json")
            self.assertEqual(state["status"], "qc_passed")
            self.assertEqual(state["latest_edl"], "/tmp/edl.json")


if __name__ == "__main__":
    unittest.main()
