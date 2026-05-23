from __future__ import annotations

import unittest

from scripts.verify_pokemon_red_performance import (
    evaluate_scenario_metrics,
    window_audio_queue_min,
)
from scripts.verify_super_mario_land_performance import (
    SCENARIOS,
    parse_window_profile_text,
)


class SuperMarioLandPerformanceGateTests(unittest.TestCase):
    def test_parse_live_window_output_extracts_profile_lines(self) -> None:
        profiles = parse_window_profile_text(
            "\n".join(
                [
                    "Title: SUPER MARIOLAND",
                    (
                        "window-profile frames=60 wall_fps=59.90 "
                        "audio_queue_range_ms=120.0-260.0 audio_underruns=0 "
                        "audio_dropped=0 apu_dropped_samples=0"
                    ),
                ]
            )
        )

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["wall_fps"], 59.90)
        self.assertEqual(window_audio_queue_min(profiles[0]), 120.0)

    def test_action_audio_scenario_rejects_dropped_samples(self) -> None:
        scenario = SCENARIOS["action-audio"]
        metrics = {
            "run_fps": 60.0,
            "frames": scenario.expected_frames,
            "cpu_instr": scenario.expected_cpu_instr,
            "cpu_cycles": scenario.expected_cpu_cycles,
            "audio_output": 1,
            "apu_samples": 445_592,
            "apu_dropped_samples": 1,
        }

        failures = evaluate_scenario_metrics(
            scenario,
            metrics,
            min_fps=45.0,
            instruction_tolerance=0,
            cycle_tolerance=0,
        )

        self.assertEqual(failures, ["action-audio: apu_dropped_samples=1"])


if __name__ == "__main__":
    unittest.main()
