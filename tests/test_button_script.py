from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from button_script import load_button_script, parse_button_script


class ButtonScriptTests(unittest.TestCase):
    def test_parse_frame_button_duration_entries(self) -> None:
        script = parse_button_script("10:start:3,20:a+b:2,30:none:4")

        self.assertEqual(script.buttons_for_frame(9, {"left"}), {"left"})
        self.assertEqual(script.buttons_for_frame(10), {"start"})
        self.assertEqual(script.buttons_for_frame(12), {"start"})
        self.assertEqual(script.buttons_for_frame(13, {"left"}), {"left"})
        self.assertEqual(script.buttons_for_frame(20), {"a", "b"})
        self.assertEqual(script.buttons_for_frame(30, {"right"}), set())
        self.assertEqual(script.final_frame, 34)

    def test_parse_script_ignores_blank_lines_and_comments(self) -> None:
        script = parse_button_script(
            """
            # title screen
            120:start:8

            240:a:6 # choose menu item
            """
        )

        self.assertEqual(script.buttons_for_frame(120), {"start"})
        self.assertEqual(script.buttons_for_frame(240), {"a"})

    def test_overlapping_events_union_buttons(self) -> None:
        script = parse_button_script("10:a:5,12:start:2")

        self.assertEqual(script.buttons_for_frame(12), {"a", "start"})

    def test_parse_rejects_invalid_buttons_and_timings(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown button"):
            parse_button_script("10:menu:3")
        with self.assertRaisesRegex(ValueError, "duration"):
            parse_button_script("10:a:0")
        with self.assertRaisesRegex(ValueError, "frame"):
            parse_button_script("-1:a:1")

    def test_load_button_script_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "buttons.txt"
            path.write_text("4:start:2\n", encoding="utf-8")

            script = load_button_script(path)

        self.assertEqual(script.buttons_for_frame(4), {"start"})


if __name__ == "__main__":
    unittest.main()
