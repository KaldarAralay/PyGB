from __future__ import annotations

import unittest

from scripts.verify_pokemon_crystal_performance import (
    SCENARIOS,
    evaluate_crystal_scenario_metrics,
    live_window_ignore_initial,
    parse_window_profile_text,
)
from scripts.verify_pokemon_red_performance import window_audio_queue_min


class PokemonCrystalPerformanceGateTests(unittest.TestCase):
    def test_crystal_metrics_rejects_fps_window_drift_and_unpinned_rtc(self) -> None:
        scenario = SCENARIOS["overworld"]
        metrics = {
            "run_fps": 54.9,
            "frames": scenario.expected_frames,
            "ppu_frames": 600,
            "cpu_instr": 1234,
            "cpu_cycles": scenario.expected_cpu_cycles,
            "audio_output": 0,
            "rtc_halted": 0,
            "windows": [
                {
                    "run_fps": 54.0,
                    "frames": 60,
                    "cpu_instr": 120,
                    "cpu_cycles": 4_213_440,
                }
            ],
        }

        failures = evaluate_crystal_scenario_metrics(
            scenario,
            metrics,
            min_fps=55.0,
            min_window_fps=55.0,
            instruction_tolerance=0,
            cycle_tolerance=0,
            expected_cpu_instr=1235,
            expected_ppu_frames=600,
        )

        self.assertEqual(
            failures,
            [
                "overworld: run_fps 54.90 < 55.00",
                "overworld: cpu_instr 1234 drifted from 1235 by more than 0",
                "overworld: rtc_halted=0",
                "overworld: window[0] run_fps 54.00 < 55.00",
            ],
        )

    def test_crystal_audio_metrics_rejects_dropped_samples(self) -> None:
        scenario = SCENARIOS["overworld-audio"]
        metrics = {
            "run_fps": 55.0,
            "frames": scenario.expected_frames,
            "ppu_frames": 600,
            "cpu_instr": 1234,
            "cpu_cycles": scenario.expected_cpu_cycles,
            "audio_output": 1,
            "rtc_halted": 1,
            "apu_samples": 44_100,
            "apu_dropped_samples": 1,
            "windows": [
                {
                    "run_fps": 55.0,
                    "frames": 60,
                    "cpu_instr": 120,
                    "cpu_cycles": 4_213_440,
                }
            ],
        }

        failures = evaluate_crystal_scenario_metrics(
            scenario,
            metrics,
            min_fps=50.0,
            min_window_fps=50.0,
            instruction_tolerance=0,
            cycle_tolerance=0,
            expected_cpu_instr=1234,
            expected_ppu_frames=600,
        )

        self.assertEqual(failures, ["overworld-audio: apu_dropped_samples=1"])

    def test_parse_live_window_output_extracts_profile_lines(self) -> None:
        profiles = parse_window_profile_text(
            "\n".join(
                [
                    "Title: PM_CRYSTAL",
                    (
                        "window-profile frames=60 wall_fps=49.90 "
                        "audio_queue_range_ms=95.0-180.0 audio_underruns=0 "
                        "audio_dropped=0 apu_dropped_samples=0"
                    ),
                ]
            )
        )

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["wall_fps"], 49.90)
        self.assertEqual(window_audio_queue_min(profiles[0]), 95.0)

    def test_default_live_window_ignore_skips_warmup_and_startup_windows(self) -> None:
        class Args:
            window_ignore_initial = None
            live_warmup_frames = 5_400
            live_profile_interval = 60

        self.assertEqual(live_window_ignore_initial(Args()), 92)

    def test_explicit_live_window_ignore_is_respected(self) -> None:
        class Args:
            window_ignore_initial = 3
            live_warmup_frames = 5_400
            live_profile_interval = 60

        self.assertEqual(live_window_ignore_initial(Args()), 3)


if __name__ == "__main__":
    unittest.main()
