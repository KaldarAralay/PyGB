from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from audio import AudioPlaybackStats
from display import (
    DisplayConfig,
    TkDisplay,
    audio_pacing_delay_ms,
    button_for_key,
    buttons_for_keys,
    display_command_for_key,
    frame_delay_ms,
    framebuffer_to_tk_image_data,
    framebuffer_to_tk_ppm_data,
    framebuffer_to_tk_rows,
)


class FakeEvent:
    def __init__(self, keysym: str) -> None:
        self.keysym = keysym


class FakeRoot:
    def __init__(self) -> None:
        self.title_text = ""
        self.scheduled_delays: list[int] = []
        self.idle_callbacks = 0

    def after(self, delay_ms: int, callback) -> None:
        self.scheduled_delays.append(delay_ms)

    def after_idle(self, callback) -> None:
        self.idle_callbacks += 1

    def title(self, text: str) -> None:
        self.title_text = text

    def destroy(self) -> None:
        pass


class FakeImage:
    def __init__(self) -> None:
        self.put_calls: list[tuple[str, tuple[int, int]]] = []
        self.copy_calls: list[tuple[object, ...]] = []
        self.tk = self

    def put(self, data: str, *, to: tuple[int, int]) -> None:
        self.put_calls.append((data, to))

    def call(self, *args: object) -> None:
        self.copy_calls.append(args)


class FakePPU:
    frame_count = 0
    framebuffer = [[0]]


class FakeAPU:
    def __init__(self) -> None:
        self.output_enabled_values: list[bool] = []
        self.sample_rates: list[int] = []

    def set_output_enabled(self, enabled: bool) -> None:
        self.output_enabled_values.append(enabled)

    def set_sample_rate(self, sample_rate: int) -> None:
        self.sample_rates.append(sample_rate)


class FakeBus:
    def __init__(self) -> None:
        self.ppu = FakePPU()
        self.apu = FakeAPU()


class FakeEmulator:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.buttons: set[str] = set()
        self.run_calls: list[dict[str, object]] = []
        self.audio_samples = [(1, -1)]
        self.reset_count = 0

    def set_buttons(self, buttons: set[str]) -> None:
        self.buttons = set(buttons)

    def run(self, **kwargs) -> None:
        self.run_calls.append(kwargs)

    def reset(self) -> None:
        self.reset_count += 1

    def drain_audio_samples(self) -> list[tuple[int, int]]:
        samples = list(self.audio_samples)
        self.audio_samples.clear()
        return samples


class FakeAudioPlayer:
    instances: list["FakeAudioPlayer"] = []

    def __init__(self, *, sample_rate: int, target_buffer_ms: int, chunk_ms: int) -> None:
        self.sample_rate = sample_rate
        self.target_buffer_ms = target_buffer_ms
        self.chunk_ms = chunk_ms
        self.started = False
        self.closed = False
        self.writes: list[list[tuple[int, int]]] = []
        FakeAudioPlayer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def write(self, samples) -> None:
        self.started = True
        self.writes.append(list(samples))

    def stats(self) -> AudioPlaybackStats:
        return AudioPlaybackStats(
            queued_frames=2205,
            queued_ms=50.0,
            underruns=0,
            low_buffer_events=0,
            dropped_frames=0,
            submitted_frames=2205,
            completed_frames=0,
        )

    def close(self) -> None:
        self.closed = True


class FakeCaptureWriter:
    instances: list["FakeCaptureWriter"] = []

    def __init__(self, output: Path, *, sample_rate: int) -> None:
        self.output = output
        self.sample_rate = sample_rate
        self.writes: list[list[tuple[int, int]]] = []
        self.closed = False
        FakeCaptureWriter.instances.append(self)

    def write(self, samples) -> None:
        self.writes.append(list(samples))

    def close(self) -> None:
        self.closed = True


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
        self.assertEqual(display_command_for_key("m"), "audio")
        self.assertEqual(display_command_for_key("M"), "audio")
        self.assertEqual(display_command_for_key("Escape"), "quit")
        self.assertIsNone(display_command_for_key("z"))

    def test_frame_delay_skips_unreliable_short_tk_delays(self) -> None:
        self.assertEqual(frame_delay_ms(1 / 60, 1 / 60), 0)
        self.assertEqual(frame_delay_ms(1 / 60, 0.0161), 0)
        self.assertEqual(frame_delay_ms(1 / 60, 0.0130), 0)
        self.assertEqual(frame_delay_ms(1 / 60, 0.0100), 0)
        self.assertEqual(frame_delay_ms(1 / 60, 0.0040), 13)

    def test_audio_pacing_waits_only_above_high_watermark(self) -> None:
        self.assertEqual(audio_pacing_delay_ms(238.0), 0)
        self.assertEqual(audio_pacing_delay_ms(238.1), 14)
        self.assertEqual(audio_pacing_delay_ms(255.0), 30)

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

    def test_framebuffer_image_data_contains_all_scaled_rows(self) -> None:
        data = framebuffer_to_tk_image_data([[0, 3], [1, 2]], scale=2)

        self.assertEqual(
            data,
            "{#ffffff #ffffff #000000 #000000} "
            "{#ffffff #ffffff #000000 #000000} "
            "{#aaaaaa #aaaaaa #555555 #555555} "
            "{#aaaaaa #aaaaaa #555555 #555555}",
        )

    def test_framebuffer_ppm_data_uses_binary_dmg_pixels(self) -> None:
        data = framebuffer_to_tk_ppm_data([[0, 3], [1, 2]])

        self.assertEqual(
            data,
            b"P6\n2 2\n255\n"
            b"\xff\xff\xff\x00\x00\x00"
            b"\xaa\xaa\xaa\x55\x55\x55",
        )

    def test_display_config_validation(self) -> None:
        self.assertEqual(DisplayConfig(scale=2).scale, 2)

        with self.assertRaises(ValueError):
            DisplayConfig(scale=0)
        with self.assertRaises(ValueError):
            DisplayConfig(fps=0)
        with self.assertRaises(ValueError):
            DisplayConfig(max_instructions_per_frame=0)
        with self.assertRaises(ValueError):
            DisplayConfig(profile_interval=0)
        with self.assertRaises(ValueError):
            DisplayConfig(audio_sample_rate=0)
        with self.assertRaises(ValueError):
            DisplayConfig(audio_buffer_ms=0)
        with self.assertRaises(ValueError):
            DisplayConfig(audio_chunk_ms=0)

    def test_tk_display_draw_frame_uploads_one_full_image(self) -> None:
        emulator = FakeEmulator()
        emulator.bus.ppu.framebuffer = [[0, 3], [1, 2]]
        display = TkDisplay(emulator, config=DisplayConfig(scale=1))
        image = FakeImage()
        display._image = image
        display._label = object()

        display._draw_frame()

        self.assertEqual(image.put_calls, [])
        self.assertEqual(len(image.copy_calls), 1)
        self.assertEqual(
            image.copy_calls[0],
            (
                image,
                "put",
                framebuffer_to_tk_ppm_data(emulator.bus.ppu.framebuffer),
                "-format",
                "PPM",
            ),
        )

    def test_tk_display_scaled_draw_uploads_source_and_native_zooms(self) -> None:
        emulator = FakeEmulator()
        emulator.bus.ppu.framebuffer = [[0, 3], [1, 2]]
        display = TkDisplay(emulator, config=DisplayConfig(scale=2))
        source_image = FakeImage()
        scaled_image = FakeImage()
        display._source_image = source_image
        display._image = scaled_image
        display._label = object()

        display._draw_frame()

        self.assertEqual(source_image.put_calls, [])
        self.assertEqual(len(source_image.copy_calls), 1)
        self.assertEqual(
            source_image.copy_calls[0],
            (
                source_image,
                "put",
                framebuffer_to_tk_ppm_data(emulator.bus.ppu.framebuffer),
                "-format",
                "PPM",
            ),
        )
        self.assertEqual(scaled_image.put_calls, [])
        self.assertEqual(
            scaled_image.copy_calls,
            [(scaled_image, "copy", source_image, "-zoom", 2, 2)],
        )

    def test_tk_display_uses_idle_schedule_when_behind(self) -> None:
        display = TkDisplay(FakeEmulator(), config=DisplayConfig())
        root = FakeRoot()
        display._root = root

        display._schedule_next_frame(0)

        self.assertEqual(root.idle_callbacks, 1)
        self.assertEqual(root.scheduled_delays, [])

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

    def test_tk_display_audio_toggle_streams_and_closes_live_audio(self) -> None:
        FakeAudioPlayer.instances.clear()
        emulator = FakeEmulator()
        display = TkDisplay(
            emulator,
            config=DisplayConfig(audio_sample_rate=22_050, audio_buffer_ms=80, audio_chunk_ms=10),
        )
        display._root = FakeRoot()
        display._running = True

        with patch("display.BufferedAudioPlayer", FakeAudioPlayer):
            display._on_key_press(FakeEvent("m"))
            display._run_frame()
            display._on_key_press(FakeEvent("m"))

        player = FakeAudioPlayer.instances[0]
        self.assertTrue(player.started)
        self.assertEqual(player.sample_rate, 22_050)
        self.assertEqual(player.target_buffer_ms, 80)
        self.assertEqual(player.chunk_ms, 10)
        self.assertEqual(player.writes, [[(1, -1)]])
        self.assertTrue(player.closed)
        self.assertEqual(emulator.bus.apu.sample_rates, [22_050])
        self.assertEqual(emulator.bus.apu.output_enabled_values, [False, True, False])

    def test_tk_display_reset_restarts_live_audio_player(self) -> None:
        FakeAudioPlayer.instances.clear()
        emulator = FakeEmulator()
        display = TkDisplay(
            emulator,
            config=DisplayConfig(audio_sample_rate=22_050, audio_buffer_ms=80, audio_chunk_ms=10),
        )
        display._root = FakeRoot()

        with patch("display.BufferedAudioPlayer", FakeAudioPlayer):
            display._on_key_press(FakeEvent("m"))
            display._on_key_press(FakeEvent("r"))

        first_player, second_player = FakeAudioPlayer.instances
        self.assertEqual(emulator.reset_count, 1)
        self.assertTrue(first_player.closed)
        self.assertFalse(second_player.closed)
        self.assertEqual(emulator.bus.apu.sample_rates, [22_050, 22_050])
        self.assertEqual(emulator.bus.apu.output_enabled_values, [False, True, False, True])

    def test_tk_display_audio_capture_writes_live_samples_and_closes(self) -> None:
        FakeAudioPlayer.instances.clear()
        FakeCaptureWriter.instances.clear()
        emulator = FakeEmulator()
        capture_path = Path("live.wav")
        display = TkDisplay(
            emulator,
            config=DisplayConfig(
                audio_sample_rate=22_050,
                audio_capture_path=capture_path,
            ),
        )
        display._root = FakeRoot()
        display._running = True

        with (
            patch("display.BufferedAudioPlayer", FakeAudioPlayer),
            patch("display.WavAudioWriter", FakeCaptureWriter),
        ):
            display._on_key_press(FakeEvent("m"))
            display._run_frame()
            display._stop()

        capture = FakeCaptureWriter.instances[0]
        self.assertEqual(capture.output, capture_path)
        self.assertEqual(capture.sample_rate, 22_050)
        self.assertEqual(capture.writes, [[(1, -1)]])
        self.assertTrue(capture.closed)


if __name__ == "__main__":
    unittest.main()
