from __future__ import annotations

import unittest

from PIL import Image

from ppu import SCREEN_HEIGHT, SCREEN_WIDTH
from scripts.verify_crystal_cgb_oracle import (
    compare_rgb_images,
    evaluate_oracle_stage,
    stage_requires_color_variety,
)


class CrystalCgbOracleTests(unittest.TestCase):
    def test_compare_rgb_images_reports_exact_and_major_differences(self) -> None:
        gbemu = Image.new("RGB", (2, 1))
        gbemu.putdata([(0, 0, 0), (10, 20, 30)])
        pyboy = Image.new("RGB", (2, 1))
        pyboy.putdata([(0, 0, 0), (250, 20, 30)])

        metrics, diff_image = compare_rgb_images(
            gbemu,
            pyboy,
            major_delta_threshold=224,
        )

        self.assertEqual(metrics["diff_pixels"], 1)
        self.assertEqual(metrics["major_diff_pixels"], 1)
        self.assertEqual(metrics["max_color_delta"], 240)
        self.assertEqual(diff_image.size, (2, 1))

    def test_stage_requires_color_variety_allows_middle_transition(self) -> None:
        checkpoints = [60, 600, 2400, 3600]

        self.assertTrue(
            stage_requires_color_variety(
                60,
                checkpoints,
                attribute_checkpoint_frame=2400,
            )
        )
        self.assertFalse(
            stage_requires_color_variety(
                600,
                checkpoints,
                attribute_checkpoint_frame=2400,
            )
        )
        self.assertTrue(
            stage_requires_color_variety(
                2400,
                checkpoints,
                attribute_checkpoint_frame=2400,
            )
        )

    def test_evaluate_oracle_stage_rejects_pyboy_blank_and_major_diff(self) -> None:
        stage = {
            "checkpoint": 2400,
            "gbemu_failures": [],
            "pyboy": {
                "size": [SCREEN_WIDTH, SCREEN_HEIGHT],
                "rgb_pixels": SCREEN_WIDTH * SCREEN_HEIGHT,
                "unique_rgb_colors": 1,
            },
            "diff": {
                "major_diff_ratio": 0.96,
            },
        }

        failures = evaluate_oracle_stage(
            stage,
            min_pyboy_unique_rgb_colors=2,
            require_pyboy_color_variety=True,
            max_major_diff_ratio=0.95,
        )

        self.assertEqual(
            failures,
            [
                "crystal oracle frame 2400: PyBoy frame has 1 unique RGB colors, expected at least 2",
                "crystal oracle frame 2400: major diff ratio 0.9600 > 0.9500",
            ],
        )


if __name__ == "__main__":
    unittest.main()
