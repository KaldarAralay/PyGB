from __future__ import annotations

import unittest

from display import (
    DisplayConfig,
    TkDisplay,
    button_for_key,
    buttons_for_keys,
    display_command_for_key,
    framebuffer_to_tk_rows,
)


class FakeEvent:
    def __init__(self, keysym: str) -> None:
        self.keysym = keysym


class FakeRoot:
    def __init__(self) -> None:
        self.title_text = ""
        self.scheduled_delays: list[int] = []

    def after(self, delay_ms: int, callback) -> None:
        self.scheduled_delays.append(delay_ms)

    def title(self, text: str) -> None:
        self.title_text = text

    def destroy(self) -> None:
        pass


class FakePPU:
    frame_count = 0
    framebuffer = [[0]]


class FakeBus:
    def __init__(self) -> None:
        self.ppu = FakePPU()


class FakeEmulator:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.buttons: set[str] = set()
        self.run_calls: list[dict[str, object]] = []
        self.reset_count = 0

    def set_buttons(self, buttons: set[str]) -> None:
        self.buttons = set(buttons)

    def run(self, **kwargs) -> None:
        self.run_calls.append(kwargs)

    def reset(self) -> None:
        self.reset_count += 1


class DisplayTests(unittest.TestCase):
    def test_button_key_mapping(self) -> None:
        self.assertEqual(button_for_key("z"), "a")
        self.assertEqual(button_for_key("Z"), "a")
        self.assertEqual(button_for_key("x"), "b")
        self.assertEqual(button_for_key("Return"), "start")
        self.assertEqual(button_for_key("BackSpace"), "select")
        self.assertEqual(button_for_key("Left"), "left")
        self.assertEqual(button_for_key("unknown"), None)

        self.assertEqual(buttons_for_keys({"z", "Right", "unknown"}), {"a", "right"})

    def test_display_command_mapping(self) -> None:
        self.assertEqual(display_command_for_key("p"), "pause")
        self.assertEqual(display_command_for_key("P"), "pause")
        self.assertEqual(display_command_for_key("Pause"), "pause")
        self.assertEqual(display_command_for_key("r"), "reset")
        self.assertEqual(display_command_for_key("t"), "trace")
        self.assertEqual(display_command_for_key("T"), "trace")
        self.assertEqual(display_command_for_key("Escape"), "quit")
        self.assertIsNone(display_command_for_key("z"))

    def test_framebuffer_rows_map_dmg_shades_to_tk_colors(self) -> None:
        rows = framebuffer_to_tk_rows([[0, 1, 2, 3]])

        self.assertEqual(rows, ["{#ffffff #aaaaaa #555555 #000000}"])

    def test_framebuffer_rows_can_be_scaled_for_tk_photoimage(self) -> None:
        rows = framebuffer_to_tk_rows([[0, 3], [1, 2]], scale=2)

        self.assertEqual(
            rows,
            [
                "{#ffffff #ffffff #000000 #000000}",
                "{#ffffff #ffffff #000000 #000000}",
                "{#aaaaaa #aaaaaa #555555 #555555}",
                "{#aaaaaa #aaaaaa #555555 #555555}",
            ],
        )
        with self.assertRaises(ValueError):
            framebuffer_to_tk_rows([[0]], scale=0)

    def test_display_config_validation(self) -> None:
        self.assertEqual(DisplayConfig(scale=2).scale, 2)

        with self.assertRaises(ValueError):
            DisplayConfig(scale=0)
        with self.assertRaises(ValueError):
            DisplayConfig(fps=0)
        with self.assertRaises(ValueError):
            DisplayConfig(max_instructions_per_frame=0)

    def test_tk_display_trace_command_toggles_run_tracing(self) -> None:
        emulator = FakeEmulator()
        sink_lines: list[str] = []
        sink = sink_lines.append
        display = TkDisplay(
            emulator,
            config=DisplayConfig(fps=60),
            trace_sink=sink,
        )
        display._root = FakeRoot()
        display._running = True

        display._on_key_press(FakeEvent("t"))
        display._run_frame()

        self.assertEqual(display._root.title_text, "GBemu [trace]")
        self.assertEqual(emulator.run_calls[-1]["trace"], True)
        self.assertIs(emulator.run_calls[-1]["trace_sink"], sink)
        self.assertEqual(emulator.run_calls[-1]["max_frames"], 1)

        display._on_key_press(FakeEvent("T"))
        self.assertEqual(display._root.title_text, "GBemu")


if __name__ == "__main__":
    unittest.main()
