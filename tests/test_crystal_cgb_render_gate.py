from __future__ import annotations

import unittest

from scripts.verify_crystal_cgb_render import (
    DEFAULT_CHECKPOINT_FRAMES,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    evaluate_crystal_stage_metrics,
    parse_checkpoint_frames,
)


class CrystalCgbRenderGateTests(unittest.TestCase):
    def test_parse_checkpoint_frames_defaults_fallback_and_deduplicates(self) -> None:
        self.assertEqual(parse_checkpoint_frames(None), list(DEFAULT_CHECKPOINT_FRAMES))
        self.assertEqual(parse_checkpoint_frames(None, fallback_frame=2400), [2400])
        self.assertEqual(parse_checkpoint_frames("600,60 600 0x960"), [60, 600, 2400])

    def test_parse_checkpoint_frames_rejects_empty_or_non_positive_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one frame"):
            parse_checkpoint_frames("")
        with self.assertRaisesRegex(ValueError, "positive"):
            parse_checkpoint_frames("60,0")
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            parse_checkpoint_frames("60,title")

    def test_evaluate_crystal_stage_metrics_accepts_valid_stage(self) -> None:
        stage = self._valid_stage()

        failures = evaluate_crystal_stage_metrics(
            stage,
            min_unique_rgb_colors=2,
            require_attributes=True,
        )

        self.assertEqual(failures, [])

    def test_evaluate_crystal_stage_metrics_rejects_missing_cgb_evidence(self) -> None:
        stage = self._valid_stage()
        stage.update(
            {
                "header_cgb_only": False,
                "header_status": "DMG",
                "mode": "dmg",
                "bus_cgb_mode": False,
                "rgb_pixels": 10,
                "unique_rgb_colors": 1,
                "bg_palette_nonzero": 0,
                "vram_dma_blocks": 0,
                "attrs_nonzero": 0,
                "attrs_palette": 0,
                "attrs_bank": 0,
                "attrs_xflip": 0,
                "attrs_yflip": 0,
            }
        )

        failures = evaluate_crystal_stage_metrics(
            stage,
            min_unique_rgb_colors=2,
            require_attributes=True,
        )

        self.assertIn("crystal frame 2400: expected CGB-only header, got DMG", failures)
        self.assertIn("crystal frame 2400: emulator did not stay in CGB mode", failures)
        self.assertIn(
            "crystal frame 2400: framebuffer has 10 RGB pixels, expected 23040",
            failures,
        )
        self.assertIn(
            "crystal frame 2400: visible frame has 1 unique RGB colors, expected at least 2",
            failures,
        )
        self.assertIn("crystal frame 2400: BG palette RAM remained blank", failures)
        self.assertIn(
            "crystal frame 2400: CGB VRAM DMA path was not exercised",
            failures,
        )
        self.assertIn("crystal frame 2400: tile attribute maps remained blank", failures)
        self.assertIn("crystal frame 2400: no BG palette attributes were present", failures)
        self.assertIn("crystal frame 2400: no tile VRAM bank attributes were present", failures)
        self.assertIn("crystal frame 2400: no tile flip attributes were present", failures)

    def test_evaluate_crystal_stage_metrics_can_record_solid_transition_frames(self) -> None:
        stage = self._valid_stage()
        stage["unique_rgb_colors"] = 1

        failures = evaluate_crystal_stage_metrics(
            stage,
            min_unique_rgb_colors=2,
            require_color_variety=False,
        )

        self.assertEqual(failures, [])

    def _valid_stage(self) -> dict[str, object]:
        return {
            "checkpoint": 2400,
            "header_cgb_only": True,
            "header_status": "CGB only",
            "mode": "CGB",
            "bus_cgb_mode": True,
            "frames": 2400,
            "rgb_pixels": SCREEN_WIDTH * SCREEN_HEIGHT,
            "unique_rgb_colors": 4,
            "bg_palette_nonzero": 1,
            "vram_dma_blocks": 1,
            "attrs_nonzero": 1,
            "attrs_palette": 1,
            "attrs_bank": 1,
            "attrs_xflip": 1,
            "attrs_yflip": 1,
        }


if __name__ == "__main__":
    unittest.main()
