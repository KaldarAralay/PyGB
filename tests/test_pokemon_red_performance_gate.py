from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.verify_pokemon_red_performance import (
    SCENARIOS,
    evaluate_scenario_metrics,
    evaluate_window_profiles,
    parse_profile_line,
    parse_window_profile_log,
    window_audio_queue_min,
)


class PokemonRedPerformanceGateTests(unittest.TestCase):
    def test_parse_profile_line_converts_numbers_ranges_and_ratios(self) -> None:
        metrics = parse_profile_line(
            "pokemon-red-sprites-profile "
            "frames=600 run_fps=76.80 frame_ms_range=7.84-28.35 "
            "cpu_cycles=42134400 ppu_lines=86400/86400 "
            "save_file=C:\\Users\\sean1\\save.sav",
            "pokemon-red-sprites-profile",
        )

        self.assertEqual(metrics["profile"], "pokemon-red-sprites-profile")
        self.assertEqual(metrics["frames"], 600)
        self.assertEqual(metrics["run_fps"], 76.80)
        self.assertEqual(metrics["frame_ms_range"], (7.84, 28.35))
        self.assertEqual(metrics["cpu_cycles"], 42_134_400)
        self.assertEqual(metrics["ppu_lines"], (86_400, 86_400))
        self.assertEqual(metrics["save_file"], "C:\\Users\\sean1\\save.sav")

    def test_evaluate_scenario_metrics_rejects_fps_and_deterministic_drift(self) -> None:
        scenario = SCENARIOS["sprites"]
        metrics = {
            "run_fps": 49.9,
            "frames": scenario.expected_frames,
            "cpu_instr": scenario.expected_cpu_instr + 2,
            "cpu_cycles": scenario.expected_cpu_cycles,
            "audio_output": 0,
        }

        failures = evaluate_scenario_metrics(
            scenario,
            metrics,
            min_fps=50.0,
            instruction_tolerance=1,
            cycle_tolerance=0,
        )

        self.assertIn("sprites: run_fps 49.90 < 50.00", failures)
        self.assertIn(
            "sprites: cpu_instr 2717565 drifted from 2717563 by more than 1",
            failures,
        )

    def test_evaluate_audio_scenario_rejects_dropped_samples(self) -> None:
        scenario = SCENARIOS["sprites-audio"]
        metrics = {
            "run_fps": 60.0,
            "frames": scenario.expected_frames,
            "cpu_instr": scenario.expected_cpu_instr,
            "cpu_cycles": scenario.expected_cpu_cycles,
            "audio_output": 1,
            "apu_samples": 443_012,
            "apu_dropped_samples": 1,
        }

        failures = evaluate_scenario_metrics(
            scenario,
            metrics,
            min_fps=45.0,
            instruction_tolerance=0,
            cycle_tolerance=0,
        )

        self.assertEqual(failures, ["sprites-audio: apu_dropped_samples=1"])

    def test_window_profile_log_checks_non_startup_fps_queue_and_audio_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "window.log"
            path.write_text(
                "\n".join(
                    [
                        (
                            "window-profile frames=60 wall_fps=20.00 "
                            "audio_queue_range_ms=10.0-20.0 audio_underruns=0 "
                            "audio_dropped=0 apu_dropped_samples=0"
                        ),
                        (
                            "window-profile frames=60 wall_fps=49.00 "
                            "audio_queue_range_ms=70.0-120.0 audio_underruns=0 "
                            "audio_dropped=1 apu_dropped_samples=0"
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            profiles = parse_window_profile_log(path)

        self.assertEqual(len(profiles), 2)
        self.assertEqual(window_audio_queue_min(profiles[1]), 70.0)
        failures = evaluate_window_profiles(
            profiles,
            min_fps=50.0,
            min_audio_queue_ms=80.0,
            ignore_initial=1,
        )

        self.assertEqual(
            failures,
            [
                "window-profile[1]: audio_dropped=1",
                "window-profile[1]: wall_fps 49.00 < 50.00",
                "window-profile[1]: audio_queue_min_ms 70.0 < 80.0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
