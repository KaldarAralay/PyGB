from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from ppu import SCREEN_HEIGHT, SCREEN_WIDTH
from scripts.verify_crystal_cgb_oracle import (
    ORACLE_SCENARIOS,
    DEFAULT_CRYSTAL_SAVE_FILE,
    classify_stage_mismatch,
    classify_visible_mismatch,
    compare_rgb_images,
    compare_source_states,
    evaluate_oracle_stage,
    force_pyboy_cgb_post_boot,
    image_metrics,
    _gbemu_rtc_saved_at_from_sidecar,
    _pyboy_rtc_bytes_from_gbemu_sidecar,
    load_oracle_rtc_now,
    parse_source_debug_checkpoints,
    resolve_oracle_save_file,
    stage_requires_color_variety,
    strict_source_state_failures,
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

    def test_compare_source_states_reports_oam_first_diff(self) -> None:
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
        gbemu = {
            "io": io,
            "vram0": [0] * 0x2000,
            "vram1": [0] * 0x2000,
            "oam": [0] * 160,
            "bg_palette_ram": [0] * 64,
            "obj_palette_ram": [0] * 64,
        }
        pyboy = {
            **gbemu,
            "oam": [0] * 160,
        }
        pyboy["oam"][0x22] = 0x60

        comparison = compare_source_states(gbemu, pyboy)

        self.assertFalse(comparison["non_suspects"]["oam_equal"])
        self.assertEqual(
            comparison["oam"]["first_diff"],
            {"offset": 0x22, "gbemu": 0, "pyboy": 0x60},
        )
        self.assertEqual(comparison["suspect_class"], "oam_timing")

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

        self.assertEqual(dynamic.gbemu_frame_clock, "cpu")
        self.assertEqual(dynamic.gbemu_input_clock, "wall")
        self.assertTrue({2400, 3600, 4800}.issubset(dynamic.checkpoint_frames))
        self.assertIn("down", dynamic.button_script or "")
        self.assertIn("up", dynamic.button_script or "")

    def test_overworld_scenario_uses_saved_game_fixture(self) -> None:
        overworld = ORACLE_SCENARIOS["overworld"]

        self.assertEqual(overworld.save_file, DEFAULT_CRYSTAL_SAVE_FILE)
        self.assertEqual(overworld.gbemu_frame_clock, "cpu")
        self.assertEqual(overworld.gbemu_input_clock, "wall")
        self.assertGreater(overworld.attribute_checkpoint_frame, max(overworld.checkpoint_frames))
        self.assertEqual(set(overworld.source_debug_checkpoints), set(overworld.checkpoint_frames))
        self.assertIn(5400, overworld.checkpoint_frames)
        self.assertIn(7200, overworld.checkpoint_frames)
        self.assertIn(8400, overworld.checkpoint_frames)
        self.assertIn(8580, overworld.checkpoint_frames)
        self.assertIn(9600, overworld.checkpoint_frames)
        self.assertIn("left", overworld.button_script or "")
        self.assertIn("down", overworld.button_script or "")
        self.assertIn("start", overworld.button_script or "")
        self.assertIn("overworld-text-box", overworld.stage_labels.values())

    def test_resolve_oracle_save_file_prefers_explicit_path(self) -> None:
        class Args:
            save_file = DEFAULT_CRYSTAL_SAVE_FILE.with_name("override.sav")
            no_save_file = False

        save_file, source = resolve_oracle_save_file(Args(), ORACLE_SCENARIOS["overworld"])

        self.assertEqual(save_file, DEFAULT_CRYSTAL_SAVE_FILE.with_name("override.sav"))
        self.assertEqual(source, "arg")

    def test_gbemu_rtc_sidecar_exposes_saved_at_for_deterministic_oracle(self) -> None:
        sidecar = json.dumps(
            {
                "version": 1,
                "saved_at": 1234.5,
                "seconds": 1,
                "minutes": 2,
                "hours": 3,
                "days": 4,
                "halt": False,
                "carry": False,
            }
        ).encode("utf-8")

        self.assertEqual(_gbemu_rtc_saved_at_from_sidecar(sidecar), 1234.5)

    def test_gbemu_rtc_sidecar_converts_to_pyboy_epoch(self) -> None:
        sidecar = json.dumps(
            {
                "version": 1,
                "saved_at": 1000.0,
                "seconds": 1,
                "minutes": 2,
                "hours": 3,
                "days": 4,
                "halt": False,
                "carry": True,
            }
        ).encode("utf-8")

        converted = _pyboy_rtc_bytes_from_gbemu_sidecar(
            sidecar,
            now=1010.0,
            host_now=1010.0,
        )

        self.assertIsNotNone(converted)
        assert converted is not None
        timezero = struct.unpack("d", converted[:8])[0]
        self.assertEqual(timezero, 1010.0 - (4 * 86400 + 3 * 3600 + 2 * 60 + 1 + 10))
        self.assertEqual(converted[8:], bytes([0, 1]))

    def test_force_pyboy_cgb_post_boot_disables_boot_and_sets_cgb_identity(self) -> None:
        class RegisterFile:
            A = F = B = C = D = E = 0
            HL = SP = PC = 0

        class FakePyBoy:
            def __init__(self) -> None:
                self.memory: dict[int, int] = {}
                self.register_file = RegisterFile()

        pyboy = FakePyBoy()

        force_pyboy_cgb_post_boot(pyboy)

        self.assertEqual(pyboy.memory[0xFF50], 0x01)
        self.assertEqual(pyboy.memory[0xFF04], 0x00)
        self.assertEqual(pyboy.memory[0xFFFF], 0x00)
        self.assertEqual(pyboy.memory[0xFF40], 0x91)
        self.assertEqual(pyboy.memory[0xFF4D], 0x7E)
        self.assertEqual(pyboy.register_file.A, 0x11)
        self.assertEqual(pyboy.register_file.F, 0x80)
        self.assertEqual(pyboy.register_file.D, 0xFF)
        self.assertEqual(pyboy.register_file.E, 0x56)
        self.assertEqual(pyboy.register_file.HL, 0x000D)
        self.assertEqual(pyboy.register_file.SP, 0xFFFE)
        self.assertEqual(pyboy.register_file.PC, 0x0100)

    def test_gbemu_rtc_sidecar_without_saved_at_uses_frozen_zero_epoch(self) -> None:
        sidecar = json.dumps(
            {
                "version": 1,
                "seconds": 1,
                "minutes": 2,
                "hours": 3,
                "days": 4,
                "halt": False,
                "carry": False,
            }
        ).encode("utf-8")

        with mock.patch(
            "scripts.verify_crystal_cgb_oracle.time.time",
            side_effect=AssertionError("live time must not be used"),
        ):
            converted = _pyboy_rtc_bytes_from_gbemu_sidecar(sidecar, host_now=0.0)

        self.assertIsNotNone(converted)
        assert converted is not None
        timezero = struct.unpack("d", converted[:8])[0]
        self.assertEqual(timezero, -(4 * 86400 + 3 * 3600 + 2 * 60 + 1))
        self.assertEqual(converted[8:], bytes([0, 0]))

    def test_load_oracle_rtc_now_requires_deterministic_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            save_file = Path(temp_dir) / "crystal.sav"

            with mock.patch(
                "scripts.verify_crystal_cgb_oracle.time.time",
                side_effect=AssertionError("live time must not be used"),
            ):
                with self.assertRaisesRegex(ValueError, "RTC sidecar is required"):
                    load_oracle_rtc_now(save_file)

    def test_load_oracle_rtc_now_uses_sidecar_saved_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            save_file = Path(temp_dir) / "crystal.sav"
            Path(f"{save_file}.rtc").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "saved_at": 1234.5,
                        "seconds": 1,
                        "minutes": 2,
                        "hours": 3,
                        "days": 4,
                        "halt": False,
                        "carry": False,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "scripts.verify_crystal_cgb_oracle.time.time",
                side_effect=AssertionError("live time must not be used"),
            ):
                self.assertEqual(load_oracle_rtc_now(save_file), 1234.5)

    def test_strict_source_state_failures_report_palette_oam_vram_and_registers(self) -> None:
        stage = {
            "checkpoint": 5400,
            "source_debug_required": True,
            "strict_source_state_required": True,
            "source_debug": {
                "state_compare": {
                    "palette_ram": {
                        "bg_palette_ram": {
                            "equal": False,
                            "first_diff": {"offset": 1, "gbemu": 2, "pyboy": 3},
                        },
                        "obj_palette_ram": {"equal": True, "first_diff": None},
                    },
                    "oam": {
                        "equal": False,
                        "first_diff": {"offset": 0x22, "gbemu": 0x64, "pyboy": 0x60},
                    },
                    "vram_sections": {
                        "bank0_tiledata": {
                            "equal": False,
                            "first_diff": {"offset": 0x20, "gbemu": 1, "pyboy": 2},
                        }
                    },
                    "register_values": {
                        "FF40": {"equal": False, "gbemu": 0xE3, "pyboy": 0xE7},
                        "FF41": {"equal": False, "gbemu": 0x88, "pyboy": 0x8E},
                    },
                }
            },
        }

        failures = strict_source_state_failures(stage)

        self.assertEqual(len(failures), 4)
        self.assertIn("source state bg_palette_ram differs", failures[0])
        self.assertIn("source state oam differs", failures[1])
        self.assertIn("source state bank0_tiledata differs", failures[2])
        self.assertIn("source state register FF40 differs", failures[3])
        self.assertFalse(any("FF41" in failure for failure in failures))

    def test_strict_source_state_failures_requires_source_debug_when_requested(self) -> None:
        self.assertEqual(
            strict_source_state_failures(
                {"checkpoint": 5400, "strict_source_state_required": True}
            ),
            ["crystal oracle frame 5400: source-debug capture is required but missing"],
        )

    def test_strict_source_state_failures_are_diagnostic_when_not_required(self) -> None:
        self.assertEqual(
            strict_source_state_failures(
                {
                    "checkpoint": 5400,
                    "source_debug_required": True,
                    "source_debug": {
                        "state_compare": {
                            "oam": {
                                "equal": False,
                                "first_diff": {"offset": 0, "gbemu": 1, "pyboy": 2},
                            }
                        }
                    },
                }
            ),
            [],
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
