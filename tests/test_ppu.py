from __future__ import annotations

import unittest

from bus import Bus
from cartridge import Cartridge, compute_header_checksum
from ppu import (
    DOTS_PER_LINE,
    MODE0_DOTS,
    MODE2_DOTS,
    MODE3_DOTS,
    MODE_DRAWING,
    MODE_HBLANK,
    MODE_OAM,
    MODE_VBLANK,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
)


def make_rom() -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0134 : 0x0134 + len(b"PPUTEST")] = b"PPUTEST"
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_bus() -> Bus:
    return Bus(Cartridge(make_rom()), serial_sink=lambda _: None)


def set_tile_row(bus: Bus, tile_id: int, row: int, lo: int, hi: int) -> None:
    address = tile_id * 16 + row * 2
    bus.vram[address] = lo
    bus.vram[address + 1] = hi


def set_solid_tile(bus: Bus, tile_id: int, color_id: int) -> None:
    lo = 0xFF if color_id & 0x01 else 0x00
    hi = 0xFF if color_id & 0x02 else 0x00
    for row in range(8):
        set_tile_row(bus, tile_id, row, lo, hi)


def set_sprite(bus: Bus, index: int, *, y: int = 16, x: int = 8, tile: int = 2, attrs: int = 0) -> None:
    offset = index * 4
    bus.oam[offset : offset + 4] = bytes([y, x, tile, attrs])


class PPUTests(unittest.TestCase):
    def test_lcd_mode_timing_and_vblank_interrupt(self) -> None:
        bus = make_bus()
        bus.write8(0xFF0F, 0x00)

        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

        bus.tick(MODE2_DOTS - 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)

        bus.tick(MODE3_DOTS)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.tick(MODE0_DOTS)
        self.assertEqual(bus.read8(0xFF44), 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

        bus.tick(DOTS_PER_LINE * (SCREEN_HEIGHT - 1))
        self.assertEqual(bus.read8(0xFF44), 144)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_VBLANK)
        self.assertEqual(bus.read8(0xFF0F) & 0x01, 0x01)

    def test_scx_low_bits_extend_mode3_and_shorten_hblank(self) -> None:
        bus = make_bus()
        bus.write8(0xFF43, 5)

        bus.tick(MODE2_DOTS + MODE3_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(5)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.tick(MODE0_DOTS - 5)
        self.assertEqual(bus.read8(0xFF44), 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

    def test_scx_low_bits_can_change_before_first_fetch_latch(self) -> None:
        bus = make_bus()
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF43, 5)
        set_tile_row(bus, 0, 0, 0b0000_0100, 0)
        bus.vram[0x1800] = 0

        bus.tick(MODE2_DOTS + 4)
        bus.write8(0xFF43, 0)
        bus.tick(MODE3_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        self.assertEqual(bus.ppu.framebuffer[0][0], 0)

    def test_scx_low_bits_are_latched_after_first_fetch_start(self) -> None:
        bus = make_bus()
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF43, 5)
        set_tile_row(bus, 0, 0, 0b0000_0100, 0)
        bus.vram[0x1800] = 0

        bus.tick(MODE2_DOTS + 8)
        bus.write8(0xFF43, 0)
        bus.tick(MODE3_DOTS + 5)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)

    def test_scx_high_bits_can_change_after_low_bits_are_latched(self) -> None:
        bus = make_bus()
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF43, 5)
        set_tile_row(bus, 0, 0, 0b0000_0100, 0)
        set_tile_row(bus, 1, 0, 0, 0b0000_0100)
        bus.vram[0x1800] = 0
        bus.vram[0x1801] = 1

        bus.tick(MODE2_DOTS + 8)
        bus.write8(0xFF43, 8)
        bus.tick(MODE3_DOTS + 5)

        self.assertEqual(bus.ppu.framebuffer[0][0], 2)

    def test_mode3_scx_write_waits_for_next_background_fetch_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        set_solid_tile(bus, 2, 2)
        set_solid_tile(bus, 3, 3)
        bus.vram[0x1800:0x1803] = bytes([1, 2, 3])

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF43, 8)
        bus.tick(MODE3_DOTS - 12 - 4)

        self.assertEqual(bus.ppu.framebuffer[0][4:8], [1, 1, 1, 1])
        self.assertEqual(bus.ppu.framebuffer[0][8], 3)

    def test_palette_write_after_scroll_wait_affects_fifo_pixels_at_output_time(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        set_solid_tile(bus, 2, 2)
        set_solid_tile(bus, 3, 3)
        bus.vram[0x1800:0x1803] = bytes([1, 2, 3])

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF43, 8)
        bus.tick(2)
        bus.write8(0xFF47, 0x80)
        bus.tick(MODE3_DOTS - 12 - 6)

        self.assertEqual(bus.ppu.framebuffer[0][2:4], [1, 1])
        self.assertEqual(bus.ppu.framebuffer[0][4:8], [0, 0, 0, 0])
        self.assertEqual(bus.ppu.framebuffer[0][8], 2)

    def test_mode3_palette_write_affects_remaining_pixels_only(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF47, 0x00)
        bus.tick(MODE3_DOTS - 12 - 4)

        self.assertEqual(bus.ppu.framebuffer[0][:1], [1])
        self.assertEqual(bus.ppu.framebuffer[0][1], 0)

        bus.tick(MODE0_DOTS)
        bus.tick(MODE2_DOTS + MODE3_DOTS)

        self.assertEqual(bus.ppu.framebuffer[1][0], 0)

    def test_mode3_palette_write_accounts_for_sprite_fetch_stall(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=8, tile=2)

        bus.tick(MODE2_DOTS + 12 + 11 + 4)
        bus.write8(0xFF47, 0x00)
        bus.tick(MODE3_DOTS + 11 - (12 + 11 + 4))

        self.assertEqual(bus.ppu.framebuffer[0][:5], [1] * 5)
        self.assertEqual(bus.ppu.framebuffer[0][5], 0)

    def test_mode3_palette_write_after_obj_disable_keeps_prior_sprite_stall_position(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        set_sprite(bus, 0, x=8, tile=2)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 11 + 4)
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0x00)
        bus.tick(MODE3_DOTS + 11 - (12 + 11 + 4))

        self.assertEqual(bus.ppu.framebuffer[0][:5], [1] * 5)
        self.assertEqual(bus.ppu.framebuffer[0][5], 0)

    def test_hblank_palette_write_after_obj_disable_at_fetch_keeps_alignment_stall_only(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0x00)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=16, tile=2)

        bus.tick(MODE2_DOTS + 28)
        bus.write8(0xFF40, 0x91)
        bus.tick(148)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.write8(0xFF47, 0xFF)

        self.assertEqual(bus.ppu.framebuffer[0][155], 0)
        self.assertEqual(bus.ppu.framebuffer[0][156:160], [3] * 4)

    def test_mode3_palette_write_after_off_left_obj_disable_uses_forced_phase(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0x00)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=0, tile=2)

        bus.tick(DOTS_PER_LINE)
        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x91)
        bus.tick(148)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.write8(0xFF47, 0xFF)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[1][145], 0)
        self.assertEqual(bus.ppu.framebuffer[1][146], 3)

    def test_mode3_lcdc_bg_disable_affects_remaining_pixels_only(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF40, 0x90)
        bus.tick(MODE3_DOTS - 12 - 4)

        self.assertEqual(bus.ppu.framebuffer[0][:4], [1, 1, 1, 1])
        self.assertEqual(bus.ppu.framebuffer[0][4], 0)

        bus.tick(MODE0_DOTS)
        bus.tick(MODE2_DOTS + MODE3_DOTS)

        self.assertEqual(bus.ppu.framebuffer[1][0], 0)

    def test_mode3_lcdc_bg_disable_at_sprite_fetch_uses_fetch_position(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=12, tile=2)

        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x92)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][:4], [1] * 4)
        self.assertEqual(bus.ppu.framebuffer[0][4], 0)

    def test_mode3_lcdc_bg_enable_pulses_with_off_left_sprite_can_recolor_fifo(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, y=17, x=0, tile=2)

        bus.tick(DOTS_PER_LINE)
        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x92)
        bus.tick(12)
        bus.write8(0xFF40, 0x93)
        bus.tick(8)
        bus.write8(0xFF40, 0x92)
        bus.tick(8)
        bus.write8(0xFF40, 0x93)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[1][:10], [0] * 10)
        self.assertEqual(bus.ppu.framebuffer[1][10:19], [1] * 9)
        self.assertEqual(bus.ppu.framebuffer[1][19:26], [0] * 7)
        self.assertEqual(bus.ppu.framebuffer[1][26:32], [1] * 6)

    def test_mode3_lcdc_bg_map_change_waits_for_next_fetch_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        set_solid_tile(bus, 2, 2)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([2] * 32)

        bus.tick(MODE2_DOTS + 12 + 2)
        bus.write8(0xFF40, 0x99)
        bus.tick(MODE3_DOTS - 12 - 2)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [1] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][8], 2)

    def test_mode3_lcdc_bg_map_change_accounts_for_sprite_fetch_stall(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)
        set_sprite(bus, 0, x=8, tile=2)

        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x9B)
        bus.tick(8)
        bus.write8(0xFF40, 0x93)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][8:16], [0] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][16:24], [0] * 8)

    def test_mode3_lcdc_tile_data_change_waits_for_next_fetch_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 1)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0x00
            bus.vram[0x1011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 2)
        bus.write8(0xFF40, 0x81)
        bus.tick(MODE3_DOTS - 12 - 2)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [1] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][8], 2)

    def test_mode3_lcdc_tile_data_change_can_mix_tile_data_bytes(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x83)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 0, 3)
        bus.vram[0x1000:0x1010] = bytes([0] * 16)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        set_sprite(bus, 0, x=5, tile=2)

        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x93)
        bus.tick(8)
        bus.write8(0xFF40, 0x83)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][8:16], [2] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][16:24], [1] * 8)

    def test_mode3_lcdc_tile_data_repeated_early_obj_pulses_keep_claimed_fetches(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x83)
        bus.write8(0xFF47, 0xE4)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0x00
            bus.vram[0x1011 + row * 2] = 0x00
            bus.vram[0x0010 + row * 2] = 0xFF
            bus.vram[0x0011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=0, tile=2)
        set_sprite(bus, 1, x=10, tile=2)

        bus.tick(MODE2_DOTS + 36)
        bus.write8(0xFF40, 0x93)
        bus.tick(8)
        bus.write8(0xFF40, 0x83)
        bus.tick(16)
        bus.write8(0xFF40, 0x93)
        bus.tick(8)
        bus.write8(0xFF40, 0x83)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][16:24], [3] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][32:40], [0] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][40:48], [3] * 8)

    def test_mode3_lcdc_tile_data_high_byte_boundary_write_uses_next_tile(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x81)
        bus.write8(0xFF47, 0xE4)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0xFF
            bus.vram[0x1011 + row * 2] = 0x00
            bus.vram[0x0010 + row * 2] = 0x00
            bus.vram[0x0011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 15)
        bus.write8(0xFF40, 0x91)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][16:24], [1] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][24:32], [2] * 8)

    def test_mode3_lcdc_tile_data_low_boundary_with_aligned_sprite_splits_bytes(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x83)
        bus.write8(0xFF47, 0xE4)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0xFF
            bus.vram[0x1011 + row * 2] = 0x00
            bus.vram[0x0010 + row * 2] = 0x00
            bus.vram[0x0011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=5, tile=2)
        set_sprite(bus, 1, x=10, tile=2)

        bus.tick(MODE2_DOTS + 40)
        bus.write8(0xFF40, 0x93)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][16:24], [3] * 8)

    def test_mode3_lcdc_window_enable_before_trigger_extends_mode3(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 15)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF40, 0xF1)
        bus.tick(MODE3_DOTS - 12 - 4)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(6)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_mode3_lcdc_window_disable_before_trigger_shortens_mode3(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 80)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF40, 0x91)
        bus.tick(MODE3_DOTS - 12 - 4 - 1)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_mode3_lcdc_window_enable_after_trigger_point_does_not_start_retroactively(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 15)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 10)
        bus.write8(0xFF40, 0xF1)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0], [1] * SCREEN_WIDTH)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_mode3_lcdc_window_enable_early_pulses_stop_on_fetch_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 22)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 16)
        bus.write8(0xFF40, 0xD1)
        bus.tick(8)
        bus.write8(0xFF40, 0xF1)
        bus.tick(20)
        bus.write8(0xFF40, 0xD1)
        bus.tick(8)
        bus.write8(0xFF40, 0xF1)
        bus.tick(DOTS_PER_LINE)

        row = bus.ppu.framebuffer[0]
        self.assertEqual(row[:15], [1] * 15)
        self.assertEqual(row[15:31], [3] * 16)
        self.assertEqual(row[31:40], [1] * 9)

    def test_mode3_lcdc_window_enable_early_cancel_can_leave_boundary_glitch(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 15)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 16)
        bus.write8(0xFF40, 0xD1)
        bus.tick(8)
        bus.write8(0xFF40, 0xF1)
        bus.tick(20)
        bus.write8(0xFF40, 0xD1)
        bus.tick(8)
        bus.write8(0xFF40, 0xF1)
        bus.tick(DOTS_PER_LINE)

        row = bus.ppu.framebuffer[0]
        self.assertEqual(row[:8], [1] * 8)
        self.assertEqual(row[8], 0)
        self.assertEqual(row[9:40], [1] * 31)

    def test_mode3_lcdc_window_reenable_uses_new_wx_and_restarted_bg_phase(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 24)
        for row in range(8):
            set_tile_row(bus, 0, row, 0xAA, 0x66)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(DOTS_PER_LINE)
        bus.tick(MODE2_DOTS + 80)
        bus.write8(0xFF4B, 120)
        bus.write8(0xFF40, 0xD1)
        bus.tick(16)
        bus.write8(0xFF40, 0xF1)
        bus.tick(DOTS_PER_LINE)

        row = bus.ppu.framebuffer[1]
        self.assertEqual(row[60:65], [3] * 5)
        self.assertEqual(row[65:69], [1, 2, 3, 0])
        self.assertEqual(row[81:85], [1, 2, 3, 0])
        self.assertEqual(row[112], 0)
        self.assertEqual(row[113:116], [3] * 3)

    def test_mode3_lcdc_window_map_change_uses_window_fetch_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 0)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF40, 0xF1)
        bus.tick(8)
        bus.write8(0xFF40, 0xB1)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [3] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][8], 0)

    def test_mode3_lcdc_window_map_change_after_window_start_uses_later_fetch(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 0)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 40)
        bus.write8(0xFF40, 0xF1)
        bus.tick(8)
        bus.write8(0xFF40, 0xB1)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][24:32], [0] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][32:40], [3] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][40:48], [0] * 8)

    def test_mode3_lcdc_window_tile_data_change_can_mix_fetch_bytes(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xA3)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 3)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        set_sprite(bus, 0, x=5, tile=2)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF40, 0xB3)
        bus.tick(8)
        bus.write8(0xFF40, 0xA3)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [2] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][8:16], [3] * 8)

    def test_mode3_lcdc_window_tile_data_change_after_start_splits_fetch_bytes(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xA1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0xFF
            bus.vram[0x1011 + row * 2] = 0x00
            bus.vram[0x0010 + row * 2] = 0x00
            bus.vram[0x0011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 18 + 13)
        bus.write8(0xFF40, 0xB1)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][16:24], [3] * 8)

    def test_mode3_initial_window_tile_data_pulse_with_aligned_sprite_targets_next_tile(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xA3)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0xFF
            bus.vram[0x1011 + row * 2] = 0x00
            bus.vram[0x0010 + row * 2] = 0x00
            bus.vram[0x0011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=1, tile=2)
        set_sprite(bus, 1, x=10, tile=2)

        bus.tick(MODE2_DOTS + 40)
        bus.write8(0xFF40, 0xB3)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][0:8], [1] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][16:24], [2] * 8)

    def test_mode3_initial_window_tile_data_repeated_pulses_keep_claimed_fetches(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xA3)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        for row in range(8):
            bus.vram[0x1010 + row * 2] = 0x00
            bus.vram[0x1011 + row * 2] = 0x00
            bus.vram[0x0010 + row * 2] = 0xFF
            bus.vram[0x0011 + row * 2] = 0xFF
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=1, tile=2)
        set_sprite(bus, 1, x=10, tile=2)

        bus.tick(MODE2_DOTS + 40)
        bus.write8(0xFF40, 0xB3)
        bus.tick(8)
        bus.write8(0xFF40, 0xA3)
        bus.tick(16)
        bus.write8(0xFF40, 0xB3)
        bus.tick(8)
        bus.write8(0xFF40, 0xA3)
        bus.tick(16)
        bus.write8(0xFF40, 0xB3)
        bus.tick(8)
        bus.write8(0xFF40, 0xA3)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][16:24], [3] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][32:40], [0] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][40:48], [3] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][56:64], [0] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][64:72], [3] * 8)

    def test_mode3_palette_write_after_window_disable_keeps_prior_window_stall_position(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 1, 1)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 6 + 4)
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0x00)
        bus.tick(MODE3_DOTS + 6 - (12 + 6 + 4))

        self.assertEqual(bus.ppu.framebuffer[0][:1], [1])
        self.assertEqual(bus.ppu.framebuffer[0][1], 0)

    def test_mode3_palette_write_during_window_restart_waits_for_window_fetch(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF47, 0x00)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 11)
        set_solid_tile(bus, 0, 1)

        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF47, 0xFF)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][3], 0)
        self.assertEqual(bus.ppu.framebuffer[0][4], 3)

    def test_mode3_bg_palette_write_glitches_first_non_window_pixel(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0x01)
        set_solid_tile(bus, 1, 0)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF47, 0x02)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.framebuffer[0][1], 3)
        self.assertEqual(bus.ppu.framebuffer[0][2], 2)

    def test_hblank_bg_palette_write_can_update_last_fifo_pixels(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0x03)
        set_solid_tile(bus, 1, 0)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + MODE3_DOTS)
        bus.write8(0xFF47, 0x00)

        self.assertEqual(bus.ppu.framebuffer[0][154], 3)
        self.assertEqual(bus.ppu.framebuffer[0][157], 3)
        self.assertEqual(bus.ppu.framebuffer[0][158], 0)
        self.assertEqual(bus.ppu.framebuffer[0][159], 0)

    def test_hblank_bg_palette_write_applies_after_already_emitted_sprite(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0x03)
        set_solid_tile(bus, 1, 0)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)
        set_sprite(bus, 0, x=8)

        bus.tick(MODE2_DOTS + MODE3_DOTS + 11)
        bus.write8(0xFF47, 0x00)

        self.assertEqual(bus.ppu.framebuffer[0][154], 3)
        self.assertEqual(bus.ppu.framebuffer[0][157], 3)
        self.assertEqual(bus.ppu.framebuffer[0][158], 0)
        self.assertEqual(bus.ppu.framebuffer[0][159], 0)

    def test_line_zero_mode2_palette_write_advances_palette_phase(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0x00)
        set_solid_tile(bus, 1, 0)
        bus.vram[0x1800:0x1820] = bytes([1] * 32)

        bus.tick(MODE2_DOTS - 4)
        bus.write8(0xFF47, 0x00)
        bus.tick(16)
        bus.write8(0xFF47, 0x01)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][0], 0)
        self.assertEqual(bus.ppu.framebuffer[0][1], 1)
        self.assertEqual(bus.ppu.framebuffer[0][2], 1)

    def test_window_render_penalty_extends_mode3_and_shortens_hblank(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)

        bus.tick(MODE2_DOTS + MODE3_DOTS + 5)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.tick(MODE0_DOTS - 6)
        self.assertEqual(bus.read8(0xFF44), 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

    def test_wx_zero_with_scx_scroll_extends_window_penalty(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB1)
        bus.write8(0xFF43, 5)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 0)

        bus.tick(MODE2_DOTS + MODE3_DOTS + 11)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_visible_sprite_extends_mode3_and_shortens_hblank(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        set_sprite(bus, 0, x=8)

        bus.tick(MODE2_DOTS + MODE3_DOTS + 7)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.tick(MODE0_DOTS - 8)
        self.assertEqual(bus.read8(0xFF44), 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

    def test_mode3_lcdc_obj_enable_before_sprite_extends_mode3(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        set_sprite(bus, 0, x=40)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF40, 0x93)
        bus.tick(MODE3_DOTS + 7 - 12 - 4)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_mode3_lcdc_obj_disable_before_sprite_shortens_mode3(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        set_sprite(bus, 0, x=40)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF40, 0x91)
        bus.tick(MODE3_DOTS - 12 - 4 - 1)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_mode3_lcdc_obj_disable_after_left_edge_fetch_keeps_sprite_stall(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        set_sprite(bus, 0, x=8)

        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x91)
        bus.tick(MODE3_DOTS + 7 - 24)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_mode3_lcdc_obj_disable_early_write_hides_off_left_sprite_pixel(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0x00)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0x01, 0x01)
        set_sprite(bus, 0, y=16, x=1, tile=2)

        bus.tick(MODE2_DOTS + 24)
        bus.write8(0xFF40, 0x91)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][0], 0)

    def test_mode3_lcdc_obj_size_change_keeps_mode2_selected_sprites(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x97)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)
        set_sprite(bus, 0, y=16, x=8, tile=2)

        bus.tick(DOTS_PER_LINE * 8 + MODE2_DOTS)
        bus.write8(0xFF40, 0x93)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[8][0], 3)

    def test_mode3_lcdc_obj_size_change_splits_sprite_tile_bytes(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x97)
        bus.write8(0xFF47, 0x00)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0)
        set_tile_row(bus, 3, 0, 0, 0b1000_0000)
        set_sprite(bus, 0, y=16, x=16, tile=2)

        bus.tick(DOTS_PER_LINE * 8 + MODE2_DOTS)
        bus.write8(0xFF40, 0x93)
        bus.tick(32)
        bus.write8(0xFF40, 0x97)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[8][8], 3)

    def test_mode3_lcdc_obj_size_write_preserves_left_clipped_sprite_fetch(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x97)
        bus.write8(0xFF47, 0x00)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b0000_0001, 0)
        set_tile_row(bus, 3, 0, 0, 0b0000_0001)
        set_sprite(bus, 0, y=16, x=1, tile=2)

        bus.tick(DOTS_PER_LINE * 8 + MODE2_DOTS)
        bus.write8(0xFF40, 0x93)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[8][0], 2)

    def test_sprites_in_same_bg_tile_share_fetch_wait_penalty(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        set_sprite(bus, 0, x=8)
        set_sprite(bus, 1, x=12)

        bus.tick(MODE2_DOTS + MODE3_DOTS + 15)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_off_left_sprite_x_zero_uses_fixed_obj_penalty(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF43, 7)
        set_sprite(bus, 0, x=0)

        bus.tick(MODE2_DOTS + MODE3_DOTS + 14)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_ly_wraps_to_zero_during_vblank_line_153(self) -> None:
        bus = make_bus()

        bus.tick(DOTS_PER_LINE * 153)
        self.assertEqual(bus.read8(0xFF44), 153)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_VBLANK)

        bus.tick(3)
        self.assertEqual(bus.read8(0xFF44), 153)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_VBLANK)
        self.assertEqual(bus.ppu.frame_count, 0)

        bus.tick(DOTS_PER_LINE - 4)

        self.assertEqual(bus.ppu.frame_count, 1)
        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

    def test_line_153_ly_zero_updates_lyc_stat_interrupt(self) -> None:
        bus = make_bus()
        bus.write8(0xFF45, 0)

        bus.tick(DOTS_PER_LINE * 153)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF41, 0x40)

        bus.tick(4)

        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF41) & 0x04, 0x04)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_lcd_disable_resets_ly_and_mode(self) -> None:
        bus = make_bus()

        bus.tick(DOTS_PER_LINE * 5)
        self.assertEqual(bus.read8(0xFF44), 5)
        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)

        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.tick(DOTS_PER_LINE * 2)
        self.assertEqual(bus.read8(0xFF44), 0)

    def test_lcd_disable_blanks_framebuffer(self) -> None:
        bus = make_bus()
        bus.ppu.framebuffer[5][10] = 3

        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)

        self.assertEqual(bus.ppu.framebuffer[5][10], 0)
        self.assertTrue(all(pixel == 0 for row in bus.ppu.framebuffer for pixel in row))

    def test_render_scanline_blanks_when_lcd_disabled(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x01)
        bus.write8(0xFF47, 0xE4)
        set_solid_tile(bus, 1, 3)
        bus.vram[0x1800] = 1
        bus.ppu.framebuffer[0][0] = 3

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0], [0] * SCREEN_WIDTH)

    def test_lcd_disabled_does_not_request_stat_interrupts(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x48)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        self.assertEqual(bus.read8(0xFF41) & 0x04, 0x04)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

        bus.write8(0xFF41, 0x48)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

    def test_lcd_disabled_retains_lyc_flag_until_reenabled(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x40)
        bus.ppu._set_ly(0x90)
        bus.write8(0xFF45, 0x90)
        bus.write8(0xFF40, 0x00)

        self.assertEqual(bus.read8(0xFF44), 0)
        self.assertEqual(bus.read8(0xFF41) & 0x07, 0x04)

        bus.write8(0xFF45, 0x01)
        self.assertEqual(bus.read8(0xFF41) & 0x07, 0x04)

        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF40, 0x80)

        self.assertEqual(bus.read8(0xFF41) & 0x07, MODE_HBLANK)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

    def test_lcd_reenable_requests_lyc_stat_only_when_comparison_becomes_true(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x40)
        bus.ppu._set_ly(0x90)
        bus.write8(0xFF45, 0x90)
        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF45, 0x00)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF40, 0x80)

        self.assertEqual(bus.read8(0xFF41) & 0x07, 0x04)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF45, 0x01)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF40, 0x80)

        self.assertEqual(bus.read8(0xFF41) & 0x07, MODE_HBLANK)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF45, 0x00)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF40, 0x80)

        self.assertEqual(bus.read8(0xFF41) & 0x07, 0x04)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_vram_cpu_access_is_blocked_during_mode3(self) -> None:
        bus = make_bus()
        bus.write8(0x8000, 0x12)

        bus.tick(MODE2_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        self.assertEqual(bus.read8(0x8000), 0xFF)
        bus.write8(0x8000, 0x34)
        self.assertEqual(bus.vram[0], 0x12)

        bus.tick(MODE3_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        self.assertEqual(bus.read8(0x8000), 0x12)
        bus.write8(0x8000, 0x56)
        self.assertEqual(bus.vram[0], 0x56)

    def test_oam_cpu_access_is_blocked_during_modes2_and3(self) -> None:
        bus = make_bus()
        bus.oam[0] = 0x12

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)
        self.assertEqual(bus.read8(0xFE00), 0xFF)
        bus.write8(0xFE00, 0x34)
        self.assertEqual(bus.oam[0], 0x12)

        bus.tick(MODE2_DOTS)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        self.assertEqual(bus.read8(0xFE00), 0xFF)

        bus.tick(MODE3_DOTS)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        self.assertEqual(bus.read8(0xFE00), 0x12)
        bus.write8(0xFE00, 0x56)
        self.assertEqual(bus.oam[0], 0x56)

    def test_vram_and_oam_cpu_accessible_when_lcd_disabled(self) -> None:
        bus = make_bus()

        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)
        bus.write8(0x8000, 0x78)
        bus.write8(0xFE00, 0x9A)

        self.assertEqual(bus.read8(0x8000), 0x78)
        self.assertEqual(bus.read8(0xFE00), 0x9A)

    def test_lyc_stat_interrupt(self) -> None:
        bus = make_bus()
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF45, 1)
        bus.write8(0xFF41, 0x40)

        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.read8(0xFF41) & 0x04, 0x04)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_hblank_mode_source_interrupts_on_mode0_entry(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x08)
        bus.write8(0xFF0F, 0x00)

        bus.tick(MODE2_DOTS + MODE3_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_vblank_mode_source_interrupts_on_mode1_entry(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x10)
        bus.write8(0xFF0F, 0x00)

        bus.tick(DOTS_PER_LINE * SCREEN_HEIGHT)

        self.assertEqual(bus.read8(0xFF44), SCREEN_HEIGHT)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_VBLANK)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_oam_source_interrupts_on_vblank_entry_on_dmg(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x20)
        bus.write8(0xFF0F, 0x00)

        bus.tick(DOTS_PER_LINE * SCREEN_HEIGHT)

        self.assertEqual(bus.read8(0xFF44), SCREEN_HEIGHT)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_VBLANK)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_oam_mode_source_interrupts_on_next_mode2_entry(self) -> None:
        bus = make_bus()

        bus.write8(0xFF41, 0x20)
        bus.write8(0xFF0F, 0x00)

        bus.tick(MODE2_DOTS)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

        bus.tick(DOTS_PER_LINE - MODE2_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_write_quirk_requests_interrupt_during_oam_hblank_and_vblank(self) -> None:
        bus = make_bus()

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF41, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

        bus.tick(MODE2_DOTS + MODE3_DOTS)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF41, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

        bus.tick(MODE0_DOTS + DOTS_PER_LINE * (SCREEN_HEIGHT - 1))
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_VBLANK)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF41, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_write_quirk_requests_interrupt_when_ly_equals_lyc_in_mode3(self) -> None:
        bus = make_bus()

        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF45, 0)
        bus.tick(MODE2_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        self.assertEqual(bus.read8(0xFF41) & 0x04, 0x04)
        bus.write8(0xFF41, 0x00)

        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x02)

    def test_stat_write_quirk_does_not_trigger_during_plain_mode3_or_lcd_off(self) -> None:
        bus = make_bus()

        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF45, 1)
        bus.tick(MODE2_DOTS)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        self.assertEqual(bus.read8(0xFF41) & 0x04, 0x00)
        bus.write8(0xFF41, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF41, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x02, 0x00)

    def test_background_tile_render_to_framebuffer(self) -> None:
        bus = make_bus()
        bus.write8(0xFF47, 0xE4)
        bus.vram[0x0000] = 0b1000_0000
        bus.vram[0x0001] = 0b0100_0000
        bus.vram[0x1800] = 0

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.framebuffer[0][1], 2)
        self.assertEqual(bus.ppu.framebuffer[0][2], 0)

    def test_background_uses_lcdc_tile_map_select(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x99)
        bus.write8(0xFF47, 0xE4)
        set_tile_row(bus, 0, 0, 0b1000_0000, 0)
        set_tile_row(bus, 1, 0, 0, 0b1000_0000)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 2)

    def test_background_signed_tile_data_addressing(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x81)
        bus.write8(0xFF47, 0xE4)
        bus.vram[0x0FF0] = 0b1000_0000
        bus.vram[0x0FF1] = 0b1000_0000
        bus.vram[0x1800] = 0xFF

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 3)

    def test_background_scroll_wraps_across_256_pixel_map(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF42, 255)
        bus.write8(0xFF43, 255)
        set_tile_row(bus, 1, 7, 0b0000_0001, 0)
        set_tile_row(bus, 2, 7, 0, 0b1000_0000)
        bus.vram[0x1800 + 31 * 32 + 31] = 1
        bus.vram[0x1800 + 31 * 32] = 2

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.framebuffer[0][1], 2)

    def test_lcdc_bg_window_disable_blanks_background_and_window(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xB0)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 1

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 0)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_framebuffer_ppm_export_text(self) -> None:
        bus = make_bus()
        bus.ppu.framebuffer[0][0] = 3

        ppm = bus.ppu.frame_as_ppm()

        self.assertTrue(ppm.startswith("P3\n160 144\n255\n"))
        self.assertIn("0 0 0", ppm.splitlines()[3])

    def test_framebuffer_bmp_export_bytes(self) -> None:
        bus = make_bus()
        bus.ppu.framebuffer[0][0] = 3

        bmp = bus.ppu.frame_as_bmp()

        self.assertEqual(bmp[:2], b"BM")
        self.assertEqual(int.from_bytes(bmp[2:6], "little"), len(bmp))
        self.assertEqual(int.from_bytes(bmp[18:22], "little", signed=True), SCREEN_WIDTH)
        self.assertEqual(int.from_bytes(bmp[22:26], "little", signed=True), SCREEN_HEIGHT)
        self.assertEqual(int.from_bytes(bmp[28:30], "little"), 24)
        top_row_start = 54 + (SCREEN_HEIGHT - 1) * SCREEN_WIDTH * 3
        self.assertEqual(bmp[top_row_start : top_row_start + 3], b"\x00\x00\x00")

    def test_window_overlays_background_and_advances_window_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 7)
        set_tile_row(bus, 0, 0, 0b1000_0000, 0)
        set_tile_row(bus, 1, 0, 0, 0b1000_0000)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 2)
        self.assertEqual(bus.ppu.window_line, 1)

    def test_mode3_wx_write_before_trigger_moves_window_start(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 20)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF4B, 15)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [1] * 8)
        self.assertEqual(bus.ppu.framebuffer[0][8], 2)
        self.assertEqual(bus.ppu.window_line, 1)

    def test_mode3_wx_write_from_hidden_edge_glitches_first_window_pixel(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF3)
        bus.write8(0xFF47, 0x1B)
        bus.write8(0xFF48, 0x04)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 4)
        set_solid_tile(bus, 1, 3)
        set_tile_row(bus, 2, 0, 0b0000_0100, 0)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)
        set_sprite(bus, 0, y=16, x=8, tile=2, attrs=0x80)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 12)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][5], 1)
        self.assertEqual(bus.ppu.framebuffer[0][6], 0)

    def test_mode3_wx_write_from_six_to_hidden_edge_misses_current_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 6)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 4)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0], [1] * SCREEN_WIDTH)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_mode3_wx_five_hidden_edge_keeps_started_window_phase(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 5)
        set_tile_row(bus, 1, 0, 0xFF, 0b1010_1010)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 6)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [3, 1, 3, 1, 3, 1, 3, 1])
        self.assertEqual(bus.ppu.window_line, 1)

    def test_mode3_wx_five_reactivation_inserts_zero_pixel_on_tile_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 5)
        set_tile_row(bus, 1, 0, 0xFF, 0b1010_1010)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 13)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(
            bus.ppu.framebuffer[0][:10],
            [3, 1, 3, 1, 3, 1, 0, 3, 1, 3],
        )

    def test_mode3_wx_five_reactivation_glitch_is_cancelled_by_later_wx_write(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 5)
        set_tile_row(bus, 1, 0, 0xFF, 0b1010_1010)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 101)
        bus.tick(96)
        bus.write8(0xFF4B, 80)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][94:100], [3, 1, 3, 1, 3, 1])

    def test_mode3_wx_four_hidden_edge_keeps_started_window_phase(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 4)
        set_tile_row(bus, 1, 0, 0xFF, 0b1010_1010)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 5)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][:8], [1, 3, 1, 3, 1, 3, 1, 3])
        self.assertEqual(bus.ppu.window_line, 1)

    def test_mode3_wx_four_reactivation_inserts_zero_pixel_on_tile_boundary(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 4)
        set_tile_row(bus, 1, 0, 0xFF, 0b1010_1010)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12)
        bus.write8(0xFF4B, 12)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(
            bus.ppu.framebuffer[0][:9],
            [1, 3, 1, 3, 1, 0, 3, 1, 3],
        )

    def test_mode3_wx_write_after_high_wx_fetch_start_does_not_cancel_window(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 97)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 108)
        bus.write8(0xFF4B, 80)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][89], 1)
        self.assertEqual(bus.ppu.framebuffer[0][90], 2)
        self.assertEqual(bus.ppu.window_line, 1)

    def test_mode3_wx_write_before_high_wx_fetch_start_can_cancel_window(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 102)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 108)
        bus.write8(0xFF4B, 80)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0], [1] * SCREEN_WIDTH)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_mode3_wx_write_before_trigger_can_hide_window(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 20)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF4B, 167)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0], [1] * SCREEN_WIDTH)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_mode3_wx_write_to_past_trigger_suppresses_old_window_start(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 20)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 10)
        bus.write8(0xFF4B, 7)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0], [1] * SCREEN_WIDTH)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_mode3_wx_write_before_trigger_extends_mode3_for_window_penalty(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 167)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF4B, 15)
        bus.tick(MODE3_DOTS - 12 - 4)

        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)
        bus.tick(6)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_mode3_wx_write_after_trigger_point_does_not_start_retroactively(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 167)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800:0x1820] = bytes([0] * 32)
        bus.vram[0x1C00:0x1C20] = bytes([1] * 32)

        bus.tick(MODE2_DOTS + 12 + 10)
        bus.write8(0xFF4B, 7)
        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0], [1] * SCREEN_WIDTH)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_window_wy_condition_triggers_on_matching_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 5)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1
        bus.write8(0xFF40, 0xF1)

        bus.tick(DOTS_PER_LINE * 5 + MODE2_DOTS + MODE3_DOTS + 6)

        self.assertEqual(bus.ppu.framebuffer[5][0], 2)
        self.assertEqual(bus.ppu.window_line, 1)

    def test_window_wy_condition_does_not_trigger_retroactively(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 10)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1
        bus.write8(0xFF40, 0xF1)

        bus.tick(DOTS_PER_LINE * 5)
        bus.write8(0xFF4A, 0)
        bus.tick(MODE2_DOTS + MODE3_DOTS)

        self.assertEqual(bus.ppu.framebuffer[5][0], 1)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_window_wy_write_during_mode2_does_not_trigger_current_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 20)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1
        bus.write8(0xFF40, 0xF1)

        bus.write8(0xFF4A, 0)
        bus.tick(MODE2_DOTS + MODE3_DOTS + 6)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_window_wy_write_before_next_mode2_triggers_next_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 20)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1
        bus.write8(0xFF40, 0xF1)

        bus.write8(0xFF4A, 1)
        bus.tick(DOTS_PER_LINE + MODE2_DOTS + MODE3_DOTS + 6)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.framebuffer[1][0], 2)
        self.assertEqual(bus.ppu.window_line, 1)

    def test_window_wy_write_after_trigger_does_not_hide_window(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x00)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 5)
        bus.write8(0xFF4B, 7)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1
        bus.write8(0xFF40, 0xF1)

        bus.tick(DOTS_PER_LINE * 5 + MODE2_DOTS + MODE3_DOTS + 6)
        bus.write8(0xFF4A, 200)
        bus.tick(MODE0_DOTS + MODE2_DOTS + MODE3_DOTS)

        self.assertEqual(bus.ppu.framebuffer[6][0], 2)
        self.assertEqual(bus.ppu.window_line, 2)

    def test_window_hidden_by_wx_167_does_not_advance_window_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0xF1)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF4A, 0)
        bus.write8(0xFF4B, 167)
        set_solid_tile(bus, 0, 1)
        set_solid_tile(bus, 1, 2)
        bus.vram[0x1800] = 0
        bus.vram[0x1C00] = 1

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.window_line, 0)

    def test_sprite_uses_object_palette_and_transparency(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)
        set_sprite(bus, 0)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 3)
        self.assertEqual(bus.ppu.framebuffer[0][1], 0)

    def test_sprite_priority_hides_behind_nonzero_background(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 0, 0, 0b1000_0000, 0)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)
        bus.vram[0x1800] = 0
        set_sprite(bus, 0, attrs=0x80)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)

    def test_bg_priority_sprite_masks_lower_priority_sprite(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 0, 0, 0b1000_0000, 0)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0)
        set_tile_row(bus, 3, 0, 0b1000_0000, 0b1000_0000)
        bus.vram[0x1800] = 0
        set_sprite(bus, 0, attrs=0x80)
        set_sprite(bus, 1, tile=3)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)

    def test_sprite_x_flip_mirrors_pixels(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b0000_0001, 0)
        set_sprite(bus, 0, attrs=0x20)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(bus.ppu.framebuffer[0][7], 0)

    def test_sprite_uses_obp1_when_attribute_bit_set(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        bus.write8(0xFF49, 0x08)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0)
        set_sprite(bus, 0, attrs=0x10)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 2)

    def test_lcdc_obj_disable_hides_sprites(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)
        set_sprite(bus, 0)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 0)

    def test_bg_disabled_priority_sprite_still_renders(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x92)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 0, 0, 0b1000_0000, 0)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)
        bus.vram[0x1800] = 0
        set_sprite(bus, 0, attrs=0x80)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 3)

    def test_sprite_x_priority_beats_oam_order_on_dmg(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 3, 0, 0b1000_0000, 0b1000_0000)
        set_tile_row(bus, 4, 0, 0, 0b0000_1000)
        set_sprite(bus, 0, x=16, tile=3)
        set_sprite(bus, 1, x=12, tile=4)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][8], 2)

    def test_offscreen_sprites_count_toward_scanline_limit(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)

        for index in range(10):
            offset = index * 4
            bus.oam[offset : offset + 4] = bytes([16, 0, 2, 0])
        set_sprite(bus, 10)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 0)

    def test_8x16_sprite_uses_even_tile_number_for_top_tile(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x97)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0)
        set_tile_row(bus, 3, 0, 0, 0b1000_0000)
        set_sprite(bus, 0, tile=3)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 1)

    def test_y_flipped_8x16_sprite_uses_bottom_tile_first(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x97)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0)
        set_tile_row(bus, 3, 7, 0, 0b1000_0000)
        set_sprite(bus, 0, attrs=0x40)

        bus.ppu.render_scanline(0)

        self.assertEqual(bus.ppu.framebuffer[0][0], 2)

    def test_active_oam_dma_hides_sprites_for_rendered_line(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_tile_row(bus, 2, 0, 0b1000_0000, 0b1000_0000)
        bus.write8(0xC000, 16)
        bus.write8(0xC001, 8)
        bus.write8(0xC002, 2)
        bus.write8(0xC003, 0)

        bus.write8(0xFF46, 0xC0)
        bus.tick(16)
        bus.ppu.render_scanline(0)

        self.assertTrue(bus.oam_dma_active)
        self.assertEqual(bus.ppu.framebuffer[0][0], 0)

        bus.tick(624)
        bus.ppu.render_scanline(0)

        self.assertFalse(bus.oam_dma_active)
        self.assertEqual(bus.ppu.framebuffer[0][0], 3)

    def test_oam_dma_seen_during_oam_scan_hides_sprites_even_if_finished_before_render(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_solid_tile(bus, 2, 3)
        set_sprite(bus, 0)

        bus.wram[0x9F] = 0x00
        bus._oam_dma_active = True
        bus._oam_dma_source = 0xC000
        bus._oam_dma_index = 0x9F
        bus._oam_dma_cycle_counter = 0

        bus.tick(4)

        self.assertFalse(bus.oam_dma_active)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

        bus.tick(DOTS_PER_LINE - 4)

        self.assertEqual(bus.ppu.framebuffer[0][0], 0)

        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[1][0], 3)

    def test_mid_mode3_oam_dma_preserves_rendered_sprite_pixels_only(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF47, 0xE4)
        bus.write8(0xFF48, 0xE4)
        set_solid_tile(bus, 2, 3)
        set_sprite(bus, 0, x=8)
        set_sprite(bus, 1, x=40)

        bus.tick(MODE2_DOTS + 40)
        bus.write8(0xFF47, 0xE4)

        bus.write8(0xFF46, 0xC0)
        bus.tick(4)

        self.assertTrue(bus.oam_dma_active)

        bus.tick(DOTS_PER_LINE)

        self.assertEqual(bus.ppu.framebuffer[0][0], 3)
        self.assertEqual(bus.ppu.framebuffer[0][32], 0)

    def test_mid_mode3_oam_dma_before_sprite_shortens_mode3(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        set_sprite(bus, 0, x=40)

        bus.tick(MODE2_DOTS + 12 + 4)
        bus.write8(0xFF46, 0xC0)
        bus.tick(4)
        bus.tick(MODE3_DOTS - 12 - 8 - 1)

        self.assertEqual(bus.ppu.mode, MODE_DRAWING)
        bus.tick(1)
        self.assertEqual(bus.ppu.mode, MODE_HBLANK)

    def test_dma_hidden_sprites_do_not_extend_mode3_after_dma_finishes(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        set_sprite(bus, 0)

        bus.wram[0x9F] = 0x00
        bus._oam_dma_active = True
        bus._oam_dma_source = 0xC000
        bus._oam_dma_index = 0x9F
        bus._oam_dma_cycle_counter = 0

        bus.tick(4)
        self.assertFalse(bus.oam_dma_active)

        bus.tick(MODE2_DOTS - 4)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)

        bus.tick(MODE3_DOTS - 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_DRAWING)

        bus.tick(1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_HBLANK)

    def test_oam_dma_crossing_hblank_into_oam_hides_next_line_sprites(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_solid_tile(bus, 2, 3)
        set_sprite(bus, 0)
        bus.tick(DOTS_PER_LINE - 2)

        bus.wram[0x9F] = 0x00
        bus._oam_dma_active = True
        bus._oam_dma_source = 0xC000
        bus._oam_dma_index = 0x9F
        bus._oam_dma_cycle_counter = 0

        bus.tick(4)

        self.assertFalse(bus.oam_dma_active)
        self.assertEqual(bus.read8(0xFF44), 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

        bus.tick(DOTS_PER_LINE - 2)

        self.assertEqual(bus.ppu.framebuffer[1][0], 0)

    def test_final_oam_dma_cycle_crossing_into_oam_hides_next_line_sprites(self) -> None:
        bus = make_bus()
        bus.write8(0xFF40, 0x93)
        bus.write8(0xFF48, 0xE4)
        set_solid_tile(bus, 2, 3)
        set_sprite(bus, 0)
        bus.tick(DOTS_PER_LINE - 1)

        bus.wram[0x9F] = 0x00
        bus._oam_dma_active = True
        bus._oam_dma_source = 0xC000
        bus._oam_dma_index = 0x9F
        bus._oam_dma_cycle_counter = 3

        bus.tick(1)

        self.assertFalse(bus.oam_dma_active)
        self.assertEqual(bus.read8(0xFF44), 1)
        self.assertEqual(bus.read8(0xFF41) & 0x03, MODE_OAM)

        bus.tick(DOTS_PER_LINE - 1)

        self.assertEqual(bus.ppu.framebuffer[1][0], 0)


if __name__ == "__main__":
    unittest.main()
