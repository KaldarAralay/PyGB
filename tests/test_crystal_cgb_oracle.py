from __future__ import annotations

import unittest

from PIL import Image

from ppu import SCREEN_HEIGHT, SCREEN_WIDTH
from scripts.verify_crystal_cgb_oracle import (
    ORACLE_SCENARIOS,
    classify_stage_mismatch,
    classify_visible_mismatch,
    compare_rgb_images,
    compare_source_states,
    evaluate_oracle_stage,
    image_metrics,
    parse_source_debug_checkpoints,
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
        self.assertEqual(metrics["diff_bbox"], [1, 0, 2, 1])
        self.assertEqual(metrics["both_nonblack_pixels"], 1)
        self.assertEqual(metrics["gbemu_only_nonblack_pixels"], 0)
        self.assertEqual(metrics["pyboy_only_nonblack_pixels"], 0)
        self.assertEqual(metrics["gbemu_nonblack_pixels"], 1)
        self.assertEqual(metrics["pyboy_nonblack_pixels"], 1)
        self.assertEqual(metrics["nonblack_delta_pixels"], 0)
        self.assertEqual(metrics["nonblack_delta_ratio"], 0)
        self.assertEqual(diff_image.size, (2, 1))

    def test_image_metrics_reports_nonblack_bounds_and_top_colors(self) -> None:
        image = Image.new("RGB", (3, 2))
        image.putdata(
            [
                (0, 0, 0),
                (1, 2, 3),
                (0, 0, 0),
                (0, 0, 0),
                (4, 5, 6),
                (0, 0, 0),
            ]
        )

        metrics = image_metrics(image)

        self.assertEqual(metrics["rgb_pixels"], 6)
        self.assertEqual(metrics["nonblack_pixels"], 2)
        self.assertEqual(metrics["nonblack_bbox"], [1, 0, 2, 2])
        self.assertEqual(metrics["top_rgb_values"][0], [0, 0, 0, 4])

    def test_stage_requires_color_variety_allows_middle_transition(self) -> None:
        checkpoints = [60, 600, 2400, 3600]

        self.assertFalse(
            stage_requires_color_variety(
                60,
                checkpoints,
                attribute_checkpoint_frame=2400,
            )
        )
        self.assertTrue(
            stage_requires_color_variety(
                60,
                [60],
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

    def test_parse_source_debug_checkpoints_accepts_none_and_lists(self) -> None:
        self.assertEqual(parse_source_debug_checkpoints("none"), set())
        self.assertEqual(parse_source_debug_checkpoints("3600, 4800"), {3600, 4800})
        self.assertEqual(
            parse_source_debug_checkpoints(
                None,
                ORACLE_SCENARIOS["dynamic"].source_debug_checkpoints,
            ),
            set(ORACLE_SCENARIOS["dynamic"].checkpoint_frames),
        )

    def test_compare_source_states_confirms_non_suspects_and_vram_drift(self) -> None:
        def state() -> dict:
            io = {
                0xFF40: 0xE7,
                0xFF41: 0x80,
                0xFF42: 8,
                0xFF43: 0,
                0xFF44: 25,
                0xFF4A: 136,
                0xFF4B: 7,
                0xFF4F: 0xFE,
                0xFF55: 0x80,
                0xFF6C: 0xFE,
            }
            return {
                "io": io,
                "vram0": [0] * 0x2000,
                "vram1": [0] * 0x2000,
                "oam": [0] * 160,
                "bg_palette_ram": [0] * 64,
                "obj_palette_ram": [0] * 64,
            }

        gbemu = state()
        pyboy = state()
        pyboy["vram0"][0x1800] = 1

        comparison = compare_source_states(gbemu, pyboy)

        self.assertTrue(comparison["non_suspects"]["oam_equal"])
        self.assertTrue(comparison["non_suspects"]["bank1_attribute_maps_equal"])
        self.assertEqual(
            comparison["vram_sections"]["bank0_bg_map_9800"]["first_diff"],
            {"offset": 0x1800, "gbemu": 0, "pyboy": 1},
        )
        self.assertEqual(comparison["suspect_class"], "bank0_vram_tiledata_or_bg_map_timing")

    def test_classify_visible_mismatch_separates_exact_image_from_state_drift(self) -> None:
        gbemu = Image.new("RGB", (2, 1), (0, 0, 0))
        pyboy = Image.new("RGB", (2, 1), (0, 0, 0))

        self.assertEqual(
            classify_visible_mismatch(
                gbemu,
                pyboy,
                {"pyboy_only_nonblack_source_counts": {"background": 2}},
            ),
            "none",
        )

    def test_classify_visible_mismatch_reports_bg_window_coverage(self) -> None:
        gbemu = Image.new("RGB", (2, 1), (0, 0, 0))
        pyboy = Image.new("RGB", (2, 1), (0, 0, 0))
        pyboy.putpixel((1, 0), (248, 248, 248))

        self.assertEqual(
            classify_visible_mismatch(
                gbemu,
                pyboy,
                {"pyboy_only_nonblack_source_counts": {"background": 1}},
            ),
            "bg_window_coverage",
        )

    def test_classify_stage_mismatch_prefers_palette_when_palette_ram_differs(self) -> None:
        stage = {
            "diff": {"diff_pixels": 1},
            "source_debug": {
                "visible_mismatch_class": "color_priority_or_timing",
                "state_compare": {
                    "non_suspects": {
                        "bg_palette_ram_equal": False,
                        "obj_palette_ram_equal": True,
                        "bank1_attribute_maps_equal": True,
                        "oam_equal": True,
                        "stable_lcdc_scroll_window_registers_equal": True,
                    },
                    "vram_sections": {},
                    "register_values": {"FF55": {"equal": True}},
                },
            },
        }

        self.assertEqual(classify_stage_mismatch(stage), "palette")

    def test_classify_stage_mismatch_reports_bg_window_tilemap(self) -> None:
        stage = {
            "diff": {"diff_pixels": 1},
            "source_debug": {
                "visible_mismatch_class": "bg_window_coverage",
                "state_compare": {
                    "non_suspects": {
                        "bg_palette_ram_equal": True,
                        "obj_palette_ram_equal": True,
                        "bank1_attribute_maps_equal": True,
                        "oam_equal": True,
                        "stable_lcdc_scroll_window_registers_equal": True,
                    },
                    "vram_sections": {
                        "bank0_bg_map_9800": {"equal": False},
                        "bank0_bg_map_9c00": {"equal": True},
                        "bank0_tiledata": {"equal": True},
                    },
                    "register_values": {"FF55": {"equal": True}},
                },
            },
        }

        self.assertEqual(classify_stage_mismatch(stage), "bg_window_tilemap")

    def test_dynamic_scenario_keeps_static_lock_frames(self) -> None:
        dynamic = ORACLE_SCENARIOS["dynamic"]

        self.assertTrue({2400, 3600, 4800}.issubset(dynamic.checkpoint_frames))
        self.assertIn("down", dynamic.button_script or "")
        self.assertIn("up", dynamic.button_script or "")

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
                "nonblack_delta_ratio": 0.99,
            },
        }

        failures = evaluate_oracle_stage(
            stage,
            min_pyboy_unique_rgb_colors=2,
            require_pyboy_color_variety=True,
            max_major_diff_ratio=0.95,
            max_nonblack_delta_ratio=0.98,
        )

        self.assertEqual(
            failures,
            [
                "crystal oracle frame 2400: PyBoy frame has 1 unique RGB colors, expected at least 2",
                "crystal oracle frame 2400: major diff ratio 0.9600 > 0.9500",
                "crystal oracle frame 2400: nonblack coverage delta ratio 0.9900 > 0.9800",
            ],
        )


if __name__ == "__main__":
    unittest.main()
