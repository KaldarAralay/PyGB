from __future__ import annotations

import time
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING

from apu import DEFAULT_SAMPLE_RATE
from audio import AudioPlaybackStats, BufferedAudioPlayer, WavAudioWriter
from button_script import ButtonScript
from joypad import BUTTON_BITS
from ppu import DMG_GRAYSCALE, SCREEN_HEIGHT, SCREEN_WIDTH

if TYPE_CHECKING:
    from emulator import Emulator


DMG_FPS = 4_194_304 / (154 * 456)
# Windows/Tk timers often oversleep short 4-10 ms delays; after_idle gives
# steadier gameplay pacing and live audio still applies its own queue backstop.
MIN_RELIABLE_TK_DELAY_SECONDS = 0.010
LIVE_AUDIO_TARGET_QUEUE_MS = 300.0
LIVE_AUDIO_HIGH_WATERMARK_MS = 340.0
TK_DMG_COLORS = tuple(f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue in DMG_GRAYSCALE)
PPM_DMG_PIXELS = tuple(bytes(rgb) for rgb in DMG_GRAYSCALE)
PPM_SCREEN_HEADER = f"P6\n{SCREEN_WIDTH} {SCREEN_HEIGHT}\n255\n".encode("ascii")
PPM_FOUR_PIXEL_CHUNKS = tuple(
    PPM_DMG_PIXELS[index & 0x03]
    + PPM_DMG_PIXELS[(index >> 2) & 0x03]
    + PPM_DMG_PIXELS[(index >> 4) & 0x03]
    + PPM_DMG_PIXELS[(index >> 6) & 0x03]
    for index in range(256)
)
PPM_EIGHT_PIXEL_CHUNKS = tuple(
    PPM_FOUR_PIXEL_CHUNKS[index & 0xFF]
    + PPM_FOUR_PIXEL_CHUNKS[(index >> 8) & 0xFF]
    for index in range(65536)
)

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
    profile_startup: bool = False
    profile_interval: int = 60
    audio_enabled: bool = False
    audio_sample_rate: int = DEFAULT_SAMPLE_RATE
    audio_buffer_ms: int = 300
    audio_chunk_ms: int = 20
    audio_capture_path: Path | None = None

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


def frame_delay_ms(target_seconds: float, elapsed_seconds: float) -> int:
    remaining_seconds = target_seconds - elapsed_seconds
    if remaining_seconds <= MIN_RELIABLE_TK_DELAY_SECONDS:
        return 0
    return max(1, ceil(remaining_seconds * 1000))


def audio_pacing_delay_ms(
    queued_ms: float,
    *,
    target_queue_ms: float = LIVE_AUDIO_TARGET_QUEUE_MS,
    high_watermark_ms: float = LIVE_AUDIO_HIGH_WATERMARK_MS,
) -> int:
    if queued_ms <= high_watermark_ms:
        return 0
    return max(1, ceil(queued_ms - target_queue_ms))


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


def framebuffer_to_tk_ppm_data(framebuffer: list[list[int]]) -> bytes:
    height = len(framebuffer)
    width = len(framebuffer[0]) if height else 0
    header = (
        PPM_SCREEN_HEADER
        if width == SCREEN_WIDTH and height == SCREEN_HEIGHT
        else f"P6\n{width} {height}\n255\n".encode("ascii")
    )
    data = bytearray(header)
    pixels = PPM_DMG_PIXELS
    wide_chunks = PPM_EIGHT_PIXEL_CHUNKS
    chunks = PPM_FOUR_PIXEL_CHUNKS
    for row in framebuffer:
        x = 0
        row_width = len(row)
        while x + 7 < row_width:
            chunk_index = (
                (row[x] & 0x03)
                | ((row[x + 1] & 0x03) << 2)
                | ((row[x + 2] & 0x03) << 4)
                | ((row[x + 3] & 0x03) << 6)
                | ((row[x + 4] & 0x03) << 8)
                | ((row[x + 5] & 0x03) << 10)
                | ((row[x + 6] & 0x03) << 12)
                | ((row[x + 7] & 0x03) << 14)
            )
            data.extend(wide_chunks[chunk_index])
            x += 8
        while x + 3 < row_width:
            chunk_index = (
                (row[x] & 0x03)
                | ((row[x + 1] & 0x03) << 2)
                | ((row[x + 2] & 0x03) << 4)
                | ((row[x + 3] & 0x03) << 6)
            )
            data.extend(chunks[chunk_index])
            x += 4
        while x < row_width:
            data.extend(pixels[row[x] & 0x03])
            x += 1
    return bytes(data)


class TkDisplay:
    def __init__(
        self,
        emulator: Emulator,
        *,
        config: DisplayConfig | None = None,
        initial_buttons: set[str] | None = None,
        button_script: ButtonScript | None = None,
        max_frames: int | None = None,
        trace: bool = False,
        trace_sink=None,
    ) -> None:
        self.emulator = emulator
        self.config = config or DisplayConfig()
        self.pressed = set(initial_buttons or set())
        self.button_script = button_script
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
        self._profile_cpu_instructions = 0
        self._profile_cpu_cycles = 0
        self._profile_cpu_min_instructions: int | None = None
        self._profile_cpu_max_instructions = 0
        self._profile_cpu_interrupts = 0
        self._profile_cpu_halt_cycles = 0
        self._profile_cpu_halt_batches = 0
        self._profile_bus_slow_cycles = 0
        self._profile_bus_oam_dma_cycles = 0
        self._profile_bus_oam_dma_starts = 0
        self._profile_bus_timer_overflows = 0
        self._profile_ppu_mode3_lines = 0
        self._profile_ppu_rendered_lines = 0
        self._profile_ppu_render_segments = 0
        self._profile_ppu_sprite_lines = 0
        self._profile_ppu_selected_sprites = 0
        self._profile_ppu_max_sprites_per_line = 0
        self._profile_ppu_obj_penalty_dots = 0
        self._profile_ppu_window_penalty_dots = 0
        self._profile_ppu_bg_fast_pixels = 0
        self._profile_ppu_bg_slow_pixels = 0
        self._profile_ppu_window_fast_pixels = 0
        self._profile_ppu_window_slow_pixels = 0
        self._profile_ppu_sprite_pixels = 0
        self._profile_apu_seconds = 0.0
        self._profile_apu_samples = 0
        self._profile_apu_min_samples: int | None = None
        self._profile_apu_max_samples = 0
        self._profile_apu_register_writes = 0
        self._profile_apu_triggers = 0
        self._profile_apu_channel_disables = 0
        self._profile_apu_dropped_samples = 0
        self._profile_min_audio_queue_ms: float | None = None
        self._profile_max_audio_queue_ms = 0.0
        self._profile_total_seconds = 0.0
        self._profile_audio_stats: AudioPlaybackStats | None = None
        self._profile_spike_total_seconds = 0.0
        self._profile_spike_run_seconds = 0.0
        self._profile_spike_draw_seconds = 0.0
        self._profile_spike_audio_seconds = 0.0
        self._profile_spike_cpu_instructions = 0
        self._profile_spike_cpu_cycles = 0
        self._profile_spike_audio_queue_ms: float | None = None
        self._profile_spike_cause = "none"
        self._profile_report_started = time.perf_counter()
        self._startup_first_frame_logged = False
        self._audio_capture: WavAudioWriter | None = None
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
        self._log_startup("tk-created")
        self._present_initial_window()
        self._running = True
        self._profile_report_started = time.perf_counter()
        self._schedule_next_frame(1)
        self._root.mainloop()

    def _on_key_press(self, event) -> None:
        command = display_command_for_key(event.keysym)
        if command == "pause":
            self._paused = not self._paused
            self._update_title()
            return
        if command == "reset":
            restart_audio = self._audio_enabled and self._audio_player is not None
            if restart_audio:
                self._stop_audio()
            self.emulator.reset()
            self.emulator.set_buttons(self.pressed)
            if restart_audio:
                self._start_audio(raise_on_error=False)
            else:
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
        if delay_ms <= 0 and hasattr(self._root, "after_idle"):
            self._root.after_idle(self._run_frame)
            return
        self._root.after(delay_ms, self._run_frame)

    def _run_frame(self) -> None:
        if not self._running or self._reached_frame_limit():
            self._stop()
            return

        started = time.perf_counter()
        run_elapsed = 0.0
        draw_elapsed = 0.0
        audio_elapsed = 0.0
        audio_stats = None
        if not self._paused:
            self._apply_scripted_buttons()
            run_started = time.perf_counter()
            cpu = getattr(self.emulator, "cpu", None)
            cpu_instructions_before = getattr(cpu, "instructions", 0)
            cpu_cycles_before = getattr(cpu, "cycles", 0)
            frame_before = self.emulator.bus.ppu.frame_count
            pc_before = getattr(cpu, "pc", 0)
            self.emulator.run(
                max_instructions=self.config.max_instructions_per_frame,
                max_frames=1,
                trace=self._trace_enabled,
                trace_sink=self._trace_sink,
            )
            run_elapsed = time.perf_counter() - run_started
            frame_after = self.emulator.bus.ppu.frame_count
            cpu_instructions = max(
                0,
                getattr(cpu, "instructions", 0) - cpu_instructions_before,
            )
            cpu_cycles = max(0, getattr(cpu, "cycles", 0) - cpu_cycles_before)
            cpu_profile = self._consume_cpu_profile()
            bus_profile = self._consume_bus_profile()
            ppu_profile = self._consume_ppu_profile()
            apu_profile = self._consume_apu_profile()
            audio_started = time.perf_counter()
            audio_stats = self._write_audio()
            audio_elapsed = time.perf_counter() - audio_started
            draw_started = time.perf_counter()
            self._draw_frame()
            draw_elapsed = time.perf_counter() - draw_started
            self._log_first_frame_startup(
                run_elapsed=run_elapsed,
                draw_elapsed=draw_elapsed,
                cpu_instructions=cpu_instructions,
                cpu_cycles=cpu_cycles,
                frame_before=frame_before,
                frame_after=frame_after,
                pc_before=pc_before,
            )
        else:
            apu_profile = None
        elapsed = time.perf_counter() - started
        if not self._paused:
            self._record_profile_frame(
                run_elapsed,
                draw_elapsed,
                audio_elapsed,
                cpu_instructions,
                cpu_cycles,
                cpu_profile,
                bus_profile,
                ppu_profile,
                apu_profile,
                audio_stats,
                elapsed,
            )
        target = 1.0 / self.config.fps
        delay_ms = frame_delay_ms(target, elapsed)
        if audio_stats is not None:
            delay_ms = max(delay_ms, audio_pacing_delay_ms(audio_stats.queued_ms))
        self._schedule_next_frame(delay_ms)

    def _draw_frame(self) -> None:
        if self._image is None or self._label is None:
            return
        source_image = self._source_image or self._image
        source_image.tk.call(
            source_image,
            "put",
            framebuffer_to_tk_ppm_data(self.emulator.bus.ppu.framebuffer),
            "-format",
            "PPM",
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

    def _present_initial_window(self) -> None:
        if self._root is None:
            return
        self._draw_frame()
        update_idletasks = getattr(self._root, "update_idletasks", None)
        if update_idletasks is not None:
            update_idletasks()
        update = getattr(self._root, "update", None)
        if update is not None:
            update()
        self._log_startup("tk-presented")

    def _log_startup(self, event: str, **fields: object) -> None:
        if not self.config.profile_startup:
            return
        cpu = getattr(self.emulator, "cpu", None)
        bus = getattr(self.emulator, "bus", None)
        ppu = getattr(bus, "ppu", None)
        parts = [
            "window-startup",
            f"event={event}",
            f"mode={getattr(getattr(bus, 'mode', None), 'value', 'unknown')}",
            f"cgb={int(bool(getattr(bus, 'cgb_mode', False)))}",
            f"frame={getattr(ppu, 'frame_count', 0)}",
            f"ly={getattr(ppu, '_scanline', 0)}",
            f"ppu_mode={getattr(ppu, 'mode', 0)}",
            f"lcdc=0x{getattr(bus, 'io', bytearray(0x80))[0x40]:02X}",
            f"pc=0x{getattr(cpu, 'pc', 0):04X}",
            f"cpu_instr={getattr(cpu, 'instructions', 0)}",
            f"cpu_cycles={getattr(cpu, 'cycles', 0)}",
            f"vram_bank={getattr(bus, 'vram_bank', 0)}",
            f"wram_bank={getattr(bus, 'wram_bank', 1)}",
            f"key1=0x{getattr(bus, 'read8', lambda _address: 0xFF)(0xFF4D):02X}",
            f"dma_active={int(bool(getattr(bus, 'oam_dma_active', False)))}",
        ]
        parts.extend(f"{name}={value}" for name, value in fields.items())
        print(" ".join(parts), flush=True)

    def _log_first_frame_startup(
        self,
        *,
        run_elapsed: float,
        draw_elapsed: float,
        cpu_instructions: int,
        cpu_cycles: int,
        frame_before: int,
        frame_after: int,
        pc_before: int,
    ) -> None:
        if not self.config.profile_startup or self._startup_first_frame_logged:
            return
        self._startup_first_frame_logged = True
        if frame_after > frame_before:
            reason = "frame-reached"
        elif cpu_instructions >= self.config.max_instructions_per_frame:
            reason = "instruction-limit-no-frame"
        elif cpu_instructions == 0 and cpu_cycles == 0:
            reason = "cpu-no-progress"
        else:
            reason = "emulator-returned-no-frame"
        self._log_startup(
            "first-frame",
            reason=reason,
            frame_before=frame_before,
            frame_after=frame_after,
            frame_advanced=int(frame_after > frame_before),
            run_ms=f"{run_elapsed * 1000:.2f}",
            draw_ms=f"{draw_elapsed * 1000:.2f}",
            frame_cpu_instr=cpu_instructions,
            frame_cpu_cycles=cpu_cycles,
            pc_before=f"0x{pc_before:04X}",
        )

    def _apply_scripted_buttons(self) -> None:
        if self.button_script is None:
            return
        frame = self.emulator.bus.ppu.frame_count - self._start_frame
        self.emulator.set_buttons(self.button_script.buttons_for_frame(frame, self.pressed))

    def _record_profile_frame(
        self,
        run_seconds: float,
        draw_seconds: float,
        audio_seconds: float,
        cpu_instructions: int,
        cpu_cycles: int,
        cpu_profile,
        bus_profile,
        ppu_profile,
        apu_profile,
        audio_stats: AudioPlaybackStats | None,
        total_seconds: float,
    ) -> None:
        if not self.config.profile_window:
            return
        self._profile_frames += 1
        self._profile_run_seconds += run_seconds
        self._profile_draw_seconds += draw_seconds
        self._profile_audio_seconds += audio_seconds
        self._profile_cpu_instructions += cpu_instructions
        self._profile_cpu_cycles += cpu_cycles
        self._profile_cpu_min_instructions = (
            cpu_instructions
            if self._profile_cpu_min_instructions is None
            else min(self._profile_cpu_min_instructions, cpu_instructions)
        )
        self._profile_cpu_max_instructions = max(
            self._profile_cpu_max_instructions,
            cpu_instructions,
        )
        if cpu_profile is not None:
            self._profile_cpu_interrupts += cpu_profile.interrupt_entries
            self._profile_cpu_halt_cycles += cpu_profile.halt_idle_cycles
            self._profile_cpu_halt_batches += cpu_profile.halt_idle_batches
        if bus_profile is not None:
            self._profile_bus_slow_cycles += bus_profile.slow_system_counter_cycles
            self._profile_bus_oam_dma_cycles += bus_profile.oam_dma_cycles
            self._profile_bus_oam_dma_starts += bus_profile.oam_dma_starts
            self._profile_bus_timer_overflows += bus_profile.timer_overflows
        if ppu_profile is not None:
            self._profile_ppu_mode3_lines += ppu_profile.mode3_lines
            self._profile_ppu_rendered_lines += ppu_profile.rendered_lines
            self._profile_ppu_render_segments += ppu_profile.render_segments
            self._profile_ppu_sprite_lines += ppu_profile.sprite_lines
            self._profile_ppu_selected_sprites += ppu_profile.selected_sprites
            self._profile_ppu_max_sprites_per_line = max(
                self._profile_ppu_max_sprites_per_line,
                ppu_profile.max_sprites_per_line,
            )
            self._profile_ppu_obj_penalty_dots += ppu_profile.obj_penalty_dots
            self._profile_ppu_window_penalty_dots += ppu_profile.window_penalty_dots
            self._profile_ppu_bg_fast_pixels += ppu_profile.bg_fast_pixels
            self._profile_ppu_bg_slow_pixels += ppu_profile.bg_slow_pixels
            self._profile_ppu_window_fast_pixels += ppu_profile.window_fast_pixels
            self._profile_ppu_window_slow_pixels += ppu_profile.window_slow_pixels
            self._profile_ppu_sprite_pixels += ppu_profile.sprite_pixels
        if apu_profile is not None:
            self._profile_apu_seconds += apu_profile.tick_seconds
            self._profile_apu_samples += apu_profile.generated_samples
            self._profile_apu_min_samples = (
                apu_profile.generated_samples
                if self._profile_apu_min_samples is None
                else min(self._profile_apu_min_samples, apu_profile.generated_samples)
            )
            self._profile_apu_max_samples = max(
                self._profile_apu_max_samples,
                apu_profile.generated_samples,
            )
            self._profile_apu_register_writes += apu_profile.register_writes
            self._profile_apu_triggers += apu_profile.channel_triggers
            self._profile_apu_channel_disables += apu_profile.channel_disables
            self._profile_apu_dropped_samples += apu_profile.dropped_samples
        self._profile_total_seconds += total_seconds
        if audio_stats is not None:
            self._profile_audio_stats = audio_stats
            self._profile_min_audio_queue_ms = (
                self._profile_audio_stats.queued_ms
                if self._profile_min_audio_queue_ms is None
                else min(self._profile_min_audio_queue_ms, self._profile_audio_stats.queued_ms)
            )
            self._profile_max_audio_queue_ms = max(
                self._profile_max_audio_queue_ms,
                self._profile_audio_stats.queued_ms,
            )
        if total_seconds > self._profile_spike_total_seconds:
            self._profile_spike_total_seconds = total_seconds
            self._profile_spike_run_seconds = run_seconds
            self._profile_spike_draw_seconds = draw_seconds
            self._profile_spike_audio_seconds = audio_seconds
            self._profile_spike_cpu_instructions = cpu_instructions
            self._profile_spike_cpu_cycles = cpu_cycles
            self._profile_spike_audio_queue_ms = (
                audio_stats.queued_ms if audio_stats is not None else None
            )
            self._profile_spike_cause = self._profile_spike_cause_for_frame(
                run_seconds,
                draw_seconds,
                audio_seconds,
                cpu_instructions,
                bus_profile,
                ppu_profile,
                apu_profile,
                audio_stats,
            )
        if self._profile_frames < self.config.profile_interval:
            return

        now = time.perf_counter()
        wall_seconds = now - self._profile_report_started
        frames = self._profile_frames
        apu_min_samples = self._profile_apu_min_samples or 0
        cpu_min_instructions = self._profile_cpu_min_instructions or 0
        parts = [
            "window-profile",
            f"frames={frames}",
            f"run_ms={self._profile_run_seconds / frames * 1000:.2f}",
            f"draw_ms={self._profile_draw_seconds / frames * 1000:.2f}",
            f"audio_ms={self._profile_audio_seconds / frames * 1000:.2f}",
            f"cpu_instr={self._profile_cpu_instructions}",
            f"cpu_frame_instr={cpu_min_instructions}-{self._profile_cpu_max_instructions}",
            f"cpu_cycles={self._profile_cpu_cycles}",
            f"cpu_interrupts={self._profile_cpu_interrupts}",
            f"cpu_halt_cycles={self._profile_cpu_halt_cycles}",
            f"cpu_halt_batches={self._profile_cpu_halt_batches}",
            f"bus_slow_cycles={self._profile_bus_slow_cycles}",
            f"bus_dma_cycles={self._profile_bus_oam_dma_cycles}",
            f"bus_dma_starts={self._profile_bus_oam_dma_starts}",
            f"bus_timer_overflows={self._profile_bus_timer_overflows}",
            f"ppu_lines={self._profile_ppu_rendered_lines}/{self._profile_ppu_mode3_lines}",
            f"ppu_segments={self._profile_ppu_render_segments}",
            f"ppu_sprite_lines={self._profile_ppu_sprite_lines}",
            f"ppu_sprites={self._profile_ppu_selected_sprites}",
            f"ppu_max_sprites={self._profile_ppu_max_sprites_per_line}",
            f"ppu_obj_dots={self._profile_ppu_obj_penalty_dots}",
            f"ppu_win_dots={self._profile_ppu_window_penalty_dots}",
            f"ppu_bg_px={self._profile_ppu_bg_fast_pixels}/{self._profile_ppu_bg_slow_pixels}",
            f"ppu_win_px={self._profile_ppu_window_fast_pixels}/{self._profile_ppu_window_slow_pixels}",
            f"ppu_sprite_px={self._profile_ppu_sprite_pixels}",
            f"apu_ms={self._profile_apu_seconds / frames * 1000:.2f}",
            f"apu_samples={self._profile_apu_samples}",
            f"apu_frame_samples={apu_min_samples}-{self._profile_apu_max_samples}",
            f"apu_audio_ms={self._profile_apu_samples / self.config.audio_sample_rate * 1000:.1f}",
            f"apu_reg_writes={self._profile_apu_register_writes}",
            f"apu_triggers={self._profile_apu_triggers}",
            f"apu_disables={self._profile_apu_channel_disables}",
            f"apu_dropped_samples={self._profile_apu_dropped_samples}",
            f"active_ms={self._profile_total_seconds / frames * 1000:.2f}",
            f"wall_fps={frames / wall_seconds if wall_seconds > 0 else 0.0:.2f}",
            f"spike_ms={self._profile_spike_total_seconds * 1000:.2f}",
            f"spike_run_ms={self._profile_spike_run_seconds * 1000:.2f}",
            f"spike_draw_ms={self._profile_spike_draw_seconds * 1000:.2f}",
            f"spike_audio_ms={self._profile_spike_audio_seconds * 1000:.2f}",
            f"spike_cpu_instr={self._profile_spike_cpu_instructions}",
            f"spike_cpu_cycles={self._profile_spike_cpu_cycles}",
            f"spike_cause={self._profile_spike_cause}",
        ]
        if self._profile_audio_stats is not None:
            parts.extend(
                [
                    f"audio_queue_ms={self._profile_audio_stats.queued_ms:.1f}",
                    f"audio_queue_range_ms={(self._profile_min_audio_queue_ms or 0.0):.1f}-{self._profile_max_audio_queue_ms:.1f}",
                    f"spike_audio_queue_ms={(self._profile_spike_audio_queue_ms or 0.0):.1f}",
                    f"audio_underruns={self._profile_audio_stats.underruns}",
                    f"audio_low_buffer_events={self._profile_audio_stats.low_buffer_events}",
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
        self._profile_cpu_instructions = 0
        self._profile_cpu_cycles = 0
        self._profile_cpu_min_instructions = None
        self._profile_cpu_max_instructions = 0
        self._profile_cpu_interrupts = 0
        self._profile_cpu_halt_cycles = 0
        self._profile_cpu_halt_batches = 0
        self._profile_bus_slow_cycles = 0
        self._profile_bus_oam_dma_cycles = 0
        self._profile_bus_oam_dma_starts = 0
        self._profile_bus_timer_overflows = 0
        self._profile_ppu_mode3_lines = 0
        self._profile_ppu_rendered_lines = 0
        self._profile_ppu_render_segments = 0
        self._profile_ppu_sprite_lines = 0
        self._profile_ppu_selected_sprites = 0
        self._profile_ppu_max_sprites_per_line = 0
        self._profile_ppu_obj_penalty_dots = 0
        self._profile_ppu_window_penalty_dots = 0
        self._profile_ppu_bg_fast_pixels = 0
        self._profile_ppu_bg_slow_pixels = 0
        self._profile_ppu_window_fast_pixels = 0
        self._profile_ppu_window_slow_pixels = 0
        self._profile_ppu_sprite_pixels = 0
        self._profile_apu_seconds = 0.0
        self._profile_apu_samples = 0
        self._profile_apu_min_samples = None
        self._profile_apu_max_samples = 0
        self._profile_apu_register_writes = 0
        self._profile_apu_triggers = 0
        self._profile_apu_channel_disables = 0
        self._profile_apu_dropped_samples = 0
        self._profile_min_audio_queue_ms = None
        self._profile_max_audio_queue_ms = 0.0
        self._profile_total_seconds = 0.0
        self._profile_audio_stats = None
        self._profile_spike_total_seconds = 0.0
        self._profile_spike_run_seconds = 0.0
        self._profile_spike_draw_seconds = 0.0
        self._profile_spike_audio_seconds = 0.0
        self._profile_spike_cpu_instructions = 0
        self._profile_spike_cpu_cycles = 0
        self._profile_spike_audio_queue_ms = None
        self._profile_spike_cause = "none"
        self._profile_report_started = now

    def _profile_spike_cause_for_frame(
        self,
        run_seconds: float,
        draw_seconds: float,
        audio_seconds: float,
        cpu_instructions: int,
        bus_profile,
        ppu_profile,
        apu_profile,
        audio_stats: AudioPlaybackStats | None,
    ) -> str:
        if audio_stats is not None and audio_stats.queued_ms < 80.0:
            return "audio-queue-low"
        if draw_seconds > max(run_seconds, audio_seconds) and draw_seconds >= 0.003:
            return "draw-upload"
        if audio_seconds > max(run_seconds, draw_seconds) and audio_seconds >= 0.003:
            return "audio-submit"
        if ppu_profile is not None:
            window_pixels = ppu_profile.window_fast_pixels + ppu_profile.window_slow_pixels
            if window_pixels and ppu_profile.sprite_pixels:
                return "ppu-window-sprites"
            if ppu_profile.sprite_pixels or ppu_profile.selected_sprites:
                return "ppu-sprites"
            if window_pixels:
                return "ppu-window"
            if ppu_profile.bg_slow_pixels:
                return "ppu-bg-slow"
        if bus_profile is not None and bus_profile.slow_system_counter_cycles:
            return "bus-slow"
        if apu_profile is not None and (
            apu_profile.register_writes
            or apu_profile.channel_triggers
            or apu_profile.channel_disables
        ):
            return "apu-events"
        if cpu_instructions:
            return "cpu-run"
        return "none"

    def _reached_frame_limit(self) -> bool:
        if self.max_frames is None:
            return False
        return self.emulator.bus.ppu.frame_count - self._start_frame >= self.max_frames

    def _stop(self) -> None:
        self._running = False
        self._stop_audio()
        self._close_audio_capture()
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
        player: BufferedAudioPlayer | None = None
        try:
            self.emulator.bus.apu.set_sample_rate(self.config.audio_sample_rate)
            self.emulator.bus.apu.set_output_enabled(True)
            player = BufferedAudioPlayer(
                sample_rate=self.config.audio_sample_rate,
                target_buffer_ms=self.config.audio_buffer_ms,
                chunk_ms=self.config.audio_chunk_ms,
            )
            player.start()
            self._audio_player = player
            self._open_audio_capture()
        except (RuntimeError, ValueError) as exc:
            if player is not None:
                player.close()
            self._audio_player = None
            self._close_audio_capture()
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

    def _write_audio(self) -> AudioPlaybackStats | None:
        if self._audio_player is None:
            return None
        samples = self.emulator.drain_audio_samples()
        if self._audio_capture is not None:
            self._audio_capture.write(samples)
        self._audio_player.write(samples)
        return self._audio_player.stats()

    def _configure_apu_profile(self) -> None:
        enabled = self.config.profile_window
        cpu = getattr(self.emulator, "cpu", None)
        if hasattr(cpu, "profile_enabled"):
            cpu.profile_enabled = enabled
        bus = getattr(self.emulator, "bus", None)
        if hasattr(bus, "profile_enabled"):
            bus.profile_enabled = enabled
        ppu = getattr(bus, "ppu", None)
        if hasattr(ppu, "profile_enabled"):
            ppu.profile_enabled = enabled
        apu = getattr(bus, "apu", None)
        if hasattr(apu, "profile_enabled"):
            apu.profile_enabled = enabled

    def _consume_apu_profile(self):
        if not self.config.profile_window:
            return None
        consume_profile = getattr(self.emulator.bus.apu, "consume_profile", None)
        if consume_profile is None:
            return None
        return consume_profile()

    def _consume_cpu_profile(self):
        if not self.config.profile_window:
            return None
        cpu = getattr(self.emulator, "cpu", None)
        consume_profile = getattr(cpu, "consume_profile", None)
        if consume_profile is None:
            return None
        return consume_profile()

    def _consume_bus_profile(self):
        if not self.config.profile_window:
            return None
        bus = getattr(self.emulator, "bus", None)
        consume_profile = getattr(bus, "consume_profile", None)
        if consume_profile is None:
            return None
        return consume_profile()

    def _consume_ppu_profile(self):
        if not self.config.profile_window:
            return None
        ppu = getattr(self.emulator.bus, "ppu", None)
        consume_profile = getattr(ppu, "consume_profile", None)
        if consume_profile is None:
            return None
        return consume_profile()

    def _open_audio_capture(self) -> None:
        if self.config.audio_capture_path is None or self._audio_capture is not None:
            return
        self._audio_capture = WavAudioWriter(
            self.config.audio_capture_path,
            sample_rate=self.config.audio_sample_rate,
        )

    def _close_audio_capture(self) -> None:
        if self._audio_capture is None:
            return
        self._audio_capture.close()
        self._audio_capture = None


def run_tk_display(
    emulator: Emulator,
    *,
    config: DisplayConfig | None = None,
    initial_buttons: set[str] | None = None,
    button_script: ButtonScript | None = None,
    max_frames: int | None = None,
    trace: bool = False,
    trace_sink=None,
) -> None:
    TkDisplay(
        emulator,
        config=config,
        initial_buttons=initial_buttons,
        button_script=button_script,
        max_frames=max_frames,
        trace=trace,
        trace_sink=trace_sink,
    ).run()


def _tk_color(shade: int) -> str:
    return TK_DMG_COLORS[shade & 0x03]
