import unittest
from app.settings import duplicate_shortcuts, normalize_sequence


class ShortcutTests(unittest.TestCase):
    def test_duplicate_shortcuts_are_detected(self):
        duplicates = duplicate_shortcuts({"undo": "Ctrl+Z", "redo": "Ctrl+Z"})
        self.assertEqual(duplicates[0], ("Ctrl+Z", "undo", "redo"))

    def test_empty_shortcuts_are_ignored_for_conflicts(self):
        self.assertEqual(duplicate_shortcuts({"a": "", "b": ""}), [])

    def test_the_same_key_in_two_toolboxes_is_not_a_conflict(self):
        """Every toolbox numbers its own tools from 1 and only one is live."""
        from ui import gui  # noqa: F401  (registers the per-layer tool definitions)

        self.assertEqual(
            duplicate_shortcuts({"tool_1": "1", "tool_barline_1": "1", "tool_sv_chart_1": "1"}),
            [],
        )

    def test_a_global_action_still_conflicts_with_a_toolbox_key(self):
        from ui import gui  # noqa: F401

        duplicates = duplicate_shortcuts({"play_pause": "1", "tool_barline_1": "1"})
        self.assertEqual(duplicates, [("1", "play_pause", "tool_barline_1")])

    def test_two_tools_in_one_toolbox_still_conflict(self):
        from ui import gui  # noqa: F401

        duplicates = duplicate_shortcuts({"tool_barline_1": "1", "tool_barline_2": "1"})
        self.assertEqual(duplicates, [("1", "tool_barline_1", "tool_barline_2")])

    def test_sequence_normalization(self):
        self.assertEqual(normalize_sequence("Ctrl+Z"), "Ctrl+Z")


if __name__ == "__main__":
    unittest.main()
