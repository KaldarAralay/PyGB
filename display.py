from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from joypad import BUTTON_BITS
from ppu import DMG_GRAYSCALE, SCREEN_HEIGHT, SCREEN_WIDTH

if TYPE_CHECKING:
    from emulator import Emulator


DMG_FPS = 4_194_304 / (154 * 456)

DEFAULT_KEYMAP = {
    "z": "a",
    "x": "b",
    "Return": "start",
    "BackSpace": "select",
    "space": "select",
    "Right": "right",
    "Left": "left",
    "Up": "up",
    "Down": "down",
}

DISPLAY_COMMANDS = {
    "p": "pause",
    "Pause": "pause",
    "r": "reset",
    "t": "trace",
    "Escape": "quit",
}


@dataclass(frozen=True)
class DisplayConfig:
    scale: int = 3
    fps: float = DMG_FPS
    title: str = "GBemu"
    max_instructions_per_frame: int = 200_000

    def __post_init__(self) -> None:
        if self.scale < 1:
            raise ValueError("display scale must be at least 1")
        if self.fps <= 0:
            raise ValueError("display fps must be positive")
        if self.max_instructions_per_frame < 1:
            raise ValueError("per-frame instruction limit must be positive")


def button_for_key(keysym: str) -> str | None:
    key = keysym.lower() if len(keysym) == 1 else keysym
    button = DEFAULT_KEYMAP.get(key)
    if button is None or button not in BUTTON_BITS:
        return None
    return button


def display_command_for_key(keysym: str) -> str | None:
    key = keysym.lower() if len(keysym) == 1 else keysym
    return DISPLAY_COMMANDS.get(key)


def buttons_for_keys(keys: set[str]) -> set[str]:
    return {button for key in keys if (button := button_for_key(key)) is not None}


def framebuffer_to_tk_rows(framebuffer: list[list[int]], scale: int = 1) -> list[str]:
    if scale < 1:
        raise ValueError("display scale must be at least 1")

    rows: list[str] = []
    for row in framebuffer:
        colors = [_tk_color(shade) for shade in row for _ in range(scale)]
        row_text = "{" + " ".join(colors) + "}"
        rows.extend([row_text] * scale)
    return rows


class TkDisplay:
    def __init__(
        self,
        emulator: Emulator,
        *,
        config: DisplayConfig | None = None,
        initial_buttons: set[str] | None = None,
        max_frames: int | None = None,
        trace: bool = False,
        trace_sink=None,
    ) -> None:
        self.emulator = emulator
        self.config = config or DisplayConfig()
        self.pressed = set(initial_buttons or set())
        self.max_frames = max_frames
        self._trace_enabled = trace
        self._trace_sink = trace_sink
        self._start_frame = emulator.bus.ppu.frame_count
        self._root = None
        self._image = None
        self._label = None
        self._running = False
        self._paused = False
        self.emulator.set_buttons(self.pressed)

    def run(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            raise RuntimeError("Tkinter is not available in this Python runtime") from exc

        self._root = tk.Tk()
        self._root.title(self.config.title)
        self._root.resizable(False, False)
        self._image = tk.PhotoImage(
            width=SCREEN_WIDTH * self.config.scale,
            height=SCREEN_HEIGHT * self.config.scale,
        )
        self._label = tk.Label(self._root, image=self._image)
        self._label.image = self._image
        self._label.pack()
        self._root.bind("<KeyPress>", self._on_key_press)
        self._root.bind("<KeyRelease>", self._on_key_release)
        self._root.protocol("WM_DELETE_WINDOW", self._stop)
        self._update_title()
        self._running = True
        self._schedule_next_frame(0)
        self._root.mainloop()

    def _on_key_press(self, event) -> None:
        command = display_command_for_key(event.keysym)
        if command == "pause":
            self._paused = not self._paused
            self._update_title()
            return
        if command == "reset":
            self.emulator.reset()
            self.emulator.set_buttons(self.pressed)
            self._start_frame = self.emulator.bus.ppu.frame_count
            self._draw_frame()
            self._update_title()
            return
        if command == "trace":
            self._trace_enabled = not self._trace_enabled
            self._update_title()
            return
        if command == "quit":
            self._stop()
            return

        button = button_for_key(event.keysym)
        if button is None:
            return
        self.pressed.add(button)
        self.emulator.set_buttons(self.pressed)

    def _on_key_release(self, event) -> None:
        button = button_for_key(event.keysym)
        if button is None:
            return
        self.pressed.discard(button)
        self.emulator.set_buttons(self.pressed)

    def _schedule_next_frame(self, delay_ms: int) -> None:
        assert self._root is not None
        self._root.after(delay_ms, self._run_frame)

    def _run_frame(self) -> None:
        if not self._running or self._reached_frame_limit():
            self._stop()
            return

        started = time.perf_counter()
        if not self._paused:
            self.emulator.run(
                max_instructions=self.config.max_instructions_per_frame,
                max_frames=1,
                trace=self._trace_enabled,
                trace_sink=self._trace_sink,
            )
            self._draw_frame()
        elapsed = time.perf_counter() - started
        target = 1.0 / self.config.fps
        self._schedule_next_frame(max(1, int((target - elapsed) * 1000)))

    def _draw_frame(self) -> None:
        if self._image is None or self._label is None:
            return
        for y, row in enumerate(framebuffer_to_tk_rows(self.emulator.bus.ppu.framebuffer, self.config.scale)):
            self._image.put(row, to=(0, y))

    def _reached_frame_limit(self) -> bool:
        if self.max_frames is None:
            return False
        return self.emulator.bus.ppu.frame_count - self._start_frame >= self.max_frames

    def _stop(self) -> None:
        self._running = False
        if self._root is not None:
            self._root.destroy()

    def _update_title(self) -> None:
        if self._root is None:
            return
        suffix = ""
        if self._paused:
            suffix += " [paused]"
        if self._trace_enabled:
            suffix += " [trace]"
        self._root.title(f"{self.config.title}{suffix}")


def run_tk_display(
    emulator: Emulator,
    *,
    config: DisplayConfig | None = None,
    initial_buttons: set[str] | None = None,
    max_frames: int | None = None,
    trace: bool = False,
    trace_sink=None,
) -> None:
    TkDisplay(
        emulator,
        config=config,
        initial_buttons=initial_buttons,
        max_frames=max_frames,
        trace=trace,
        trace_sink=trace_sink,
    ).run()


def _tk_color(shade: int) -> str:
    red, green, blue = DMG_GRAYSCALE[shade & 0x03]
    return f"#{red:02x}{green:02x}{blue:02x}"
