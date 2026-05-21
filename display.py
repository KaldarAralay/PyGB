from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apu import DEFAULT_SAMPLE_RATE
from audio import AudioPlaybackStats, BufferedAudioPlayer
from joypad import BUTTON_BITS
from ppu import DMG_GRAYSCALE, SCREEN_HEIGHT, SCREEN_WIDTH

if TYPE_CHECKING:
    from emulator import Emulator


DMG_FPS = 4_194_304 / (154 * 456)
TK_DMG_COLORS = tuple(f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue in DMG_GRAYSCALE)

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
    "m": "audio",
    "Escape": "quit",
}


@dataclass(frozen=True)
class DisplayConfig:
    scale: int = 3
    fps: float = DMG_FPS
    title: str = "GBemu"
    max_instructions_per_frame: int = 200_000
    profile_window: bool = False
    profile_interval: int = 60
    audio_enabled: bool = False
    audio_sample_rate: int = DEFAULT_SAMPLE_RATE
    audio_buffer_ms: int = 100
    audio_chunk_ms: int = 20

    def __post_init__(self) -> None:
        if self.scale < 1:
            raise ValueError("display scale must be at least 1")
        if self.fps <= 0:
            raise ValueError("display fps must be positive")
        if self.max_instructions_per_frame < 1:
            raise ValueError("per-frame instruction limit must be positive")
        if self.profile_interval < 1:
            raise ValueError("profile interval must be positive")
        if self.audio_sample_rate <= 0:
            raise ValueError("audio sample rate must be positive")
        if self.audio_buffer_ms <= 0:
            raise ValueError("audio buffer must be positive")
        if self.audio_chunk_ms <= 0:
            raise ValueError("audio chunk size must be positive")


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
    colors_by_shade = TK_DMG_COLORS
    for row in framebuffer:
        if scale == 1:
            colors = [colors_by_shade[shade & 0x03] for shade in row]
        else:
            colors = [colors_by_shade[shade & 0x03] for shade in row for _ in range(scale)]
        row_text = "{" + " ".join(colors) + "}"
        rows.extend([row_text] * scale)
    return rows


def framebuffer_to_tk_image_data(framebuffer: list[list[int]], scale: int = 1) -> str:
    return " ".join(framebuffer_to_tk_rows(framebuffer, scale))


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
        self._source_image = None
        self._image = None
        self._label = None
        self._running = False
        self._paused = False
        self._audio_enabled = self.config.audio_enabled
        self._audio_player: BufferedAudioPlayer | None = None
        self._profile_frames = 0
        self._profile_run_seconds = 0.0
        self._profile_draw_seconds = 0.0
        self._profile_audio_seconds = 0.0
        self._profile_apu_seconds = 0.0
        self._profile_apu_samples = 0
        self._profile_total_seconds = 0.0
        self._profile_audio_stats: AudioPlaybackStats | None = None
        self._profile_report_started = time.perf_counter()
        self.emulator.set_buttons(self.pressed)
        self.emulator.bus.apu.set_output_enabled(self._audio_enabled)
        self._configure_apu_profile()

    def run(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            raise RuntimeError("Tkinter is not available in this Python runtime") from exc

        self._root = tk.Tk()
        self._root.title(self.config.title)
        self._root.resizable(False, False)
        self._source_image = tk.PhotoImage(width=SCREEN_WIDTH, height=SCREEN_HEIGHT)
        if self.config.scale == 1:
            self._image = self._source_image
        else:
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
        if self._audio_enabled:
            self._start_audio(raise_on_error=True)
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
            self.emulator.bus.apu.set_sample_rate(self.config.audio_sample_rate)
            self.emulator.bus.apu.set_output_enabled(self._audio_enabled)
            self._configure_apu_profile()
            self._start_frame = self.emulator.bus.ppu.frame_count
            self._draw_frame()
            self._update_title()
            return
        if command == "trace":
            self._trace_enabled = not self._trace_enabled
            self._update_title()
            return
        if command == "audio":
            self._toggle_audio()
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
        run_elapsed = 0.0
        draw_elapsed = 0.0
        audio_elapsed = 0.0
        if not self._paused:
            run_started = time.perf_counter()
            self.emulator.run(
                max_instructions=self.config.max_instructions_per_frame,
                max_frames=1,
                trace=self._trace_enabled,
                trace_sink=self._trace_sink,
            )
            run_elapsed = time.perf_counter() - run_started
            apu_elapsed, apu_samples = self._consume_apu_profile()
            audio_started = time.perf_counter()
            self._write_audio()
            audio_elapsed = time.perf_counter() - audio_started
            draw_started = time.perf_counter()
            self._draw_frame()
            draw_elapsed = time.perf_counter() - draw_started
        else:
            apu_elapsed = 0.0
            apu_samples = 0
        elapsed = time.perf_counter() - started
        if not self._paused:
            self._record_profile_frame(
                run_elapsed,
                draw_elapsed,
                audio_elapsed,
                apu_elapsed,
                apu_samples,
                elapsed,
            )
        target = 1.0 / self.config.fps
        self._schedule_next_frame(max(1, int((target - elapsed) * 1000)))

    def _draw_frame(self) -> None:
        if self._image is None or self._label is None:
            return
        source_image = self._source_image or self._image
        source_image.put(
            framebuffer_to_tk_image_data(self.emulator.bus.ppu.framebuffer),
            to=(0, 0),
        )
        if self.config.scale > 1 and source_image is not self._image:
            self._image.tk.call(
                self._image,
                "copy",
                source_image,
                "-zoom",
                self.config.scale,
                self.config.scale,
            )

    def _record_profile_frame(
        self,
        run_seconds: float,
        draw_seconds: float,
        audio_seconds: float,
        apu_seconds: float,
        apu_samples: int,
        total_seconds: float,
    ) -> None:
        if not self.config.profile_window:
            return
        self._profile_frames += 1
        self._profile_run_seconds += run_seconds
        self._profile_draw_seconds += draw_seconds
        self._profile_audio_seconds += audio_seconds
        self._profile_apu_seconds += apu_seconds
        self._profile_apu_samples += apu_samples
        self._profile_total_seconds += total_seconds
        if self._audio_player is not None:
            self._profile_audio_stats = self._audio_player.stats()
        if self._profile_frames < self.config.profile_interval:
            return

        now = time.perf_counter()
        wall_seconds = now - self._profile_report_started
        frames = self._profile_frames
        parts = [
            "window-profile",
            f"frames={frames}",
            f"run_ms={self._profile_run_seconds / frames * 1000:.2f}",
            f"draw_ms={self._profile_draw_seconds / frames * 1000:.2f}",
            f"audio_ms={self._profile_audio_seconds / frames * 1000:.2f}",
            f"apu_ms={self._profile_apu_seconds / frames * 1000:.2f}",
            f"apu_samples={self._profile_apu_samples}",
            f"active_ms={self._profile_total_seconds / frames * 1000:.2f}",
            f"wall_fps={frames / wall_seconds if wall_seconds > 0 else 0.0:.2f}",
        ]
        if self._profile_audio_stats is not None:
            parts.extend(
                [
                    f"audio_queue_ms={self._profile_audio_stats.queued_ms:.1f}",
                    f"audio_underruns={self._profile_audio_stats.underruns}",
                    f"audio_dropped={self._profile_audio_stats.dropped_frames}",
                ]
            )
        print(
            " ".join(parts),
            flush=True,
        )
        self._profile_frames = 0
        self._profile_run_seconds = 0.0
        self._profile_draw_seconds = 0.0
        self._profile_audio_seconds = 0.0
        self._profile_apu_seconds = 0.0
        self._profile_apu_samples = 0
        self._profile_total_seconds = 0.0
        self._profile_audio_stats = None
        self._profile_report_started = now

    def _reached_frame_limit(self) -> bool:
        if self.max_frames is None:
            return False
        return self.emulator.bus.ppu.frame_count - self._start_frame >= self.max_frames

    def _stop(self) -> None:
        self._running = False
        self._stop_audio()
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
        if self._audio_enabled:
            suffix += " [audio]"
        self._root.title(f"{self.config.title}{suffix}")

    def _toggle_audio(self) -> None:
        if self._audio_enabled:
            self._audio_enabled = False
            self._stop_audio()
            return
        self._audio_enabled = True
        self._start_audio(raise_on_error=False)

    def _start_audio(self, *, raise_on_error: bool) -> None:
        if self._audio_player is not None:
            return
        try:
            self.emulator.bus.apu.set_sample_rate(self.config.audio_sample_rate)
            self.emulator.bus.apu.set_output_enabled(True)
            self._audio_player = BufferedAudioPlayer(
                sample_rate=self.config.audio_sample_rate,
                target_buffer_ms=self.config.audio_buffer_ms,
                chunk_ms=self.config.audio_chunk_ms,
            )
            self._audio_player.start()
        except (RuntimeError, ValueError) as exc:
            self._audio_enabled = False
            self.emulator.bus.apu.set_output_enabled(False)
            if raise_on_error:
                raise RuntimeError(str(exc)) from exc
            print(f"Audio disabled: {exc}", flush=True)

    def _stop_audio(self) -> None:
        if self._audio_player is None:
            return
        self._audio_player.close()
        self._audio_player = None
        self.emulator.bus.apu.set_output_enabled(False)
        self._profile_audio_stats = None

    def _write_audio(self) -> None:
        if self._audio_player is None:
            return
        self._audio_player.write(self.emulator.drain_audio_samples())

    def _configure_apu_profile(self) -> None:
        if hasattr(self.emulator.bus.apu, "profile_enabled"):
            self.emulator.bus.apu.profile_enabled = self.config.profile_window

    def _consume_apu_profile(self) -> tuple[float, int]:
        if not self.config.profile_window:
            return 0.0, 0
        consume_profile = getattr(self.emulator.bus.apu, "consume_profile", None)
        if consume_profile is None:
            return 0.0, 0
        return consume_profile()


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
    return TK_DMG_COLORS[shade & 0x03]
