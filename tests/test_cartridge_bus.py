from __future__ import annotations

import unittest

from bus import (
    Bus,
    DMG_POST_BOOT_REGISTERED_MARK_TILE,
    DMG_POST_BOOT_REGISTERED_MARK_TILE_ADDRESS,
    EmulationMode,
    SERIAL_INTERNAL_TRANSFER_CYCLES,
)
from cartridge import Cartridge, NINTENDO_LOGO, compute_header_checksum
from ppu import DOTS_PER_LINE, MODE2_DOTS, MODE3_DOTS, MODE_DRAWING, MODE_HBLANK


def make_rom(program: bytes = b"", title: bytes = b"TEST", cgb_flag: int = 0x00) -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0100 : 0x0100 + len(program)] = program
    rom[0x0134 : 0x0134 + len(title)] = title
    rom[0x0143] = cgb_flag
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_cgb_bus() -> Bus:
    return Bus(
        Cartridge(make_rom(cgb_flag=0x80)),
        serial_sink=lambda _: None,
        mode=EmulationMode.CGB,
    )


def tick_to_next_hblank(bus: Bus) -> None:
    if bus.ppu.mode == MODE_HBLANK:
        bus.ppu.tick(DOTS_PER_LINE - bus.ppu.line_dots + MODE2_DOTS + MODE3_DOTS)
    else:
        bus.ppu.tick(MODE2_DOTS + MODE3_DOTS)


def make_mbc1_rom(rom_size: int = 0x10000, ram_size_code: int = 0x00, cart_type: int = 0x01) -> bytes:
    rom = bytearray([0x00] * rom_size)
    rom[0x0134 : 0x0134 + len(b"MBC1T")] = b"MBC1T"
    rom[0x0147] = cart_type
    rom[0x0148] = {0x8000: 0x00, 0x10000: 0x01, 0x20000: 0x02}.get(rom_size, 0x01)
    rom[0x0149] = ram_size_code
    if rom_size > 0x4000:
        rom[0x4000] = 0x11
    if rom_size > 0x8000:
        rom[0x8000] = 0x22
    if rom_size > 0xC000:
        rom[0xC000] = 0x33
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_rom_ram_rom(cart_type: int = 0x08, ram_size_code: int = 0x02) -> bytes:
    rom = bytearray(make_rom(title=b"ROMRAM"))
    rom[0x0147] = cart_type
    rom[0x0149] = ram_size_code
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_banked_rom(bank_count: int, cart_type: int, ram_size_code: int = 0x00) -> bytes:
    rom = bytearray([0x00] * (bank_count * 0x4000))
    rom[0x0134 : 0x0134 + len(b"BANKED")] = b"BANKED"
    rom[0x0147] = cart_type
    rom[0x0148] = {
        2: 0x00,
        4: 0x01,
        8: 0x02,
        16: 0x03,
        32: 0x04,
        64: 0x05,
        128: 0x06,
        256: 0x07,
        512: 0x08,
    }.get(bank_count, 0x08)
    rom[0x0149] = ram_size_code
    for bank in range(bank_count):
        rom[bank * 0x4000] = bank & 0xFF
        rom[bank * 0x4000 + 1] = (bank >> 8) & 0xFF
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_mbc1m_rom(ram_size_code: int = 0x00) -> bytes:
    rom = bytearray(make_banked_rom(64, cart_type=0x01, ram_size_code=ram_size_code))
    logo_offset = 0x10 * 0x4000 + 0x0104
    rom[logo_offset : logo_offset + len(NINTENDO_LOGO)] = NINTENDO_LOGO
    return bytes(rom)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CartridgeBusTests(unittest.TestCase):
    def read_latched_rtc_register(self, cartridge: Cartridge, register: int) -> int:
        cartridge.write_rom_control(0x0000, 0x0A)
        cartridge.write_rom_control(0x6000, 0x00)
        cartridge.write_rom_control(0x6000, 0x01)
        cartridge.write_rom_control(0x4000, register)
        return cartridge.read_ram(0xA000)

    def test_header_parsing_and_checksum(self) -> None:
        cartridge = Cartridge(make_rom(title=b"CPU TEST"))

        self.assertEqual(cartridge.header.title, "CPU TEST")
        self.assertEqual(cartridge.header.cartridge_type, "ROM ONLY")
        self.assertTrue(cartridge.header.header_checksum_ok)
        self.assertFalse(cartridge.header.cgb_supported)
        self.assertFalse(cartridge.header.cgb_only)
        self.assertEqual(cartridge.header.cgb_status, "DMG")

    def test_cgb_header_flag_is_parsed_without_polluting_title(self) -> None:
        enhanced = Cartridge(make_rom(title=b"CGBTEST", cgb_flag=0x80))
        cgb_only = Cartridge(make_rom(title=b"CGBONLY", cgb_flag=0xC0))

        self.assertEqual(enhanced.header.title, "CGBTEST")
        self.assertEqual(enhanced.header.cgb_flag, 0x80)
        self.assertTrue(enhanced.header.cgb_supported)
        self.assertFalse(enhanced.header.cgb_only)
        self.assertEqual(enhanced.header.cgb_status, "CGB enhanced")

        self.assertEqual(cgb_only.header.title, "CGBONLY")
        self.assertEqual(cgb_only.header.cgb_flag, 0xC0)
        self.assertTrue(cgb_only.header.cgb_supported)
        self.assertTrue(cgb_only.header.cgb_only)
        self.assertEqual(cgb_only.header.cgb_status, "CGB only")

    def test_cartridge_type_profile_is_header_driven(self) -> None:
        cases = [
            (0x00, 0x00, "ROM", False, False, False, False, False),
            (0x08, 0x02, "ROM", True, False, False, False, False),
            (0x03, 0x03, "MBC1", True, True, False, False, False),
            (0x06, 0x00, "MBC2", True, True, False, False, False),
            (0x0F, 0x00, "MBC3", True, True, True, False, False),
            (0x10, 0x03, "MBC3", True, True, True, False, False),
            (0x1E, 0x04, "MBC5", True, True, False, True, False),
            (0x22, 0x00, "UNSUPPORTED", True, True, False, True, False),
            (0xFF, 0x03, "HuC1", True, True, False, False, True),
            (0xEE, 0x00, "UNSUPPORTED", False, False, False, False, False),
        ]

        for (
            cart_type,
            ram_size_code,
            mapper_name,
            handles_ram,
            battery,
            rtc,
            profile_rumble,
            ir,
        ) in cases:
            with self.subTest(cart_type=cart_type):
                cartridge = Cartridge(
                    make_banked_rom(4, cart_type=cart_type, ram_size_code=ram_size_code)
                )

                self.assertEqual(cartridge.mapper_name, mapper_name)
                self.assertEqual(
                    cartridge.is_supported_mapper,
                    mapper_name != "UNSUPPORTED",
                )
                self.assertEqual(cartridge.handles_external_ram, handles_ram)
                self.assertEqual(cartridge.has_battery, battery)
                self.assertEqual(cartridge.has_mbc3_rtc, rtc)
                self.assertEqual(cartridge.type_spec.rumble, profile_rumble)
                self.assertEqual(cartridge.has_mbc5_rumble, mapper_name == "MBC5" and profile_rumble)
                self.assertEqual(cartridge.type_spec.ir, ir)

        self.assertEqual(Cartridge(make_rom()).mapper_status, "Mapper: ROM")
        self.assertEqual(
            Cartridge(make_banked_rom(4, cart_type=0x22)).mapper_status,
            "Mapper: unsupported (MBC7+SENSOR+RUMBLE+RAM+BATTERY)",
        )

    def test_cartridge_mapper_dispatch_is_header_driven(self) -> None:
        cases = [
            (0x00, "ROMMapper"),
            (0x08, "ROMMapper"),
            (0x03, "MBC1Mapper"),
            (0x06, "MBC2Mapper"),
            (0x10, "MBC3Mapper"),
            (0x1E, "MBC5Mapper"),
            (0x20, "UnsupportedMapper"),
            (0xFF, "HuC1Mapper"),
            (0xEE, "UnsupportedMapper"),
        ]

        for cart_type, mapper_class in cases:
            with self.subTest(cart_type=cart_type):
                ram_size_code = 0x03 if cart_type not in (0x00, 0x06, 0x20, 0xEE) else 0x00
                cartridge = Cartridge(
                    make_banked_rom(4, cart_type=cart_type, ram_size_code=ram_size_code)
                )

                self.assertEqual(type(cartridge.mapper).__name__, mapper_class)
                self.assertEqual(cartridge.mapper.name, cartridge.mapper_name)

    def test_memory_ranges_and_echo_ram(self) -> None:
        cartridge = Cartridge(make_rom_ram_rom())
        bus = Bus(cartridge, serial_sink=lambda _: None)
        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)

        bus.write8(0x8000, 0x12)
        bus.write8(0xA000, 0x34)
        bus.write8(0xC000, 0x56)
        bus.write8(0xE000, 0x78)
        bus.write8(0xFE00, 0x9A)
        bus.write8(0xFF80, 0xBC)
        bus.write8(0xFFFF, 0x1F)

        self.assertEqual(bus.read8(0x8000), 0x12)
        self.assertEqual(bus.read8(0xA000), 0x34)
        self.assertEqual(bus.read8(0xC000), 0x78)
        self.assertEqual(bus.read8(0xE000), 0x78)
        self.assertEqual(bus.read8(0xFE00), 0x9A)
        self.assertEqual(bus.read8(0xFF80), 0xBC)
        self.assertEqual(bus.read8(0xFFFF), 0x1F)

    def test_rom_only_external_ram_range_reads_ff_and_ignores_writes(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xA000, 0x34)

        self.assertEqual(bus.read8(0xA000), 0xFF)

    def test_boot_rom_overlays_cartridge_until_ff50_disable(self) -> None:
        rom = bytearray(make_rom())
        rom[0x0000] = 0x11
        rom[0x014D] = compute_header_checksum(rom)
        boot_rom = bytes([0x42]) + bytes([0x00] * 0xFE) + bytes([0x99])
        bus = Bus(Cartridge(bytes(rom)), serial_sink=lambda _: None, boot_rom=boot_rom)

        self.assertEqual(bus.read8(0x0000), 0x42)
        self.assertEqual(bus.read8(0x00FF), 0x99)
        self.assertEqual(bus.read8(0x0100), rom[0x0100])
        self.assertEqual(bus.read8(0xFF50), 0x00)

        bus.write8(0xFF50, 0x01)
        self.assertEqual(bus.read8(0x0000), 0x11)
        self.assertEqual(bus.read8(0xFF50), 0x01)

        bus.write8(0xFF50, 0x00)
        self.assertEqual(bus.read8(0x0000), 0x11)
        self.assertEqual(bus.read8(0xFF50), 0x01)

    def test_boot_rom_starts_with_power_on_io_state(self) -> None:
        boot_rom = bytes([0x00] * 0x100)
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None, boot_rom=boot_rom)

        self.assertEqual(bus.read8(0xFF04), 0x00)
        self.assertEqual(bus.read8(0xFF07), 0xF8)
        self.assertEqual(bus.read8(0xFF0F), 0xE0)
        self.assertEqual(bus.read8(0xFF40), 0x00)
        self.assertEqual(bus.read8(0xFF4D), 0x7E)
        self.assertEqual(bus.read8(0xFF50), 0x00)
        self.assertEqual(bus.read8(0xFF26), 0x70)
        self.assertFalse(bus.ppu.lcd_enabled)

    def test_post_boot_vram_contains_registered_mark_tile(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        start = DMG_POST_BOOT_REGISTERED_MARK_TILE_ADDRESS
        expected = bytes(
            [
                0x3C,
                0x00,
                0x42,
                0x00,
                0xB9,
                0x00,
                0xA5,
                0x00,
                0xB9,
                0x00,
                0xA5,
                0x00,
                0x42,
                0x00,
                0x3C,
                0x00,
            ]
        )

        self.assertEqual(DMG_POST_BOOT_REGISTERED_MARK_TILE, expected)
        self.assertEqual(
            bus.vram[start : start + len(DMG_POST_BOOT_REGISTERED_MARK_TILE)],
            bytearray(expected),
        )

    def test_io_registers_read_unused_bits_high(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF02, 0x00)
        bus.write8(0xFF07, 0x05)
        bus.write8(0xFF0F, 0x15)
        bus.write8(0xFF4D, 0x01)

        self.assertEqual(bus.read8(0xFF02), 0x7E)
        self.assertEqual(bus.read8(0xFF07), 0xFD)
        self.assertEqual(bus.read8(0xFF0F), 0xF5)
        self.assertEqual(bus.read8(0xFF4D), 0x7F)

    def test_unusable_io_registers_read_as_ff(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        for address in (0xFF03, 0xFF08, 0xFF0E):
            with self.subTest(address=address):
                bus.write8(address, 0x00)
                self.assertEqual(bus.read8(address), 0xFF)
                self.assertEqual(bus.io[address - 0xFF00], 0x00)

    def test_cgb_only_io_registers_are_inert_on_dmg(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        for address in (
            0xFF4C,
            0xFF4F,
            0xFF51,
            0xFF52,
            0xFF53,
            0xFF54,
            0xFF55,
            0xFF56,
            0xFF68,
            0xFF69,
            0xFF6A,
            0xFF6B,
            0xFF6C,
            0xFF70,
            0xFF72,
            0xFF73,
            0xFF74,
            0xFF75,
            0xFF76,
            0xFF77,
        ):
            with self.subTest(address=address):
                bus.write8(address, 0x00)
                self.assertEqual(bus.read8(address), 0xFF)
                self.assertEqual(bus.io[address - 0xFF00], 0x00)

    def test_cgb_mode_exposes_vram_bank_register(self) -> None:
        bus = Bus(
            Cartridge(make_rom(cgb_flag=0x80)),
            serial_sink=lambda _: None,
            mode=EmulationMode.CGB,
        )
        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)

        bus.write8(0x8000, 0x12)
        bus.write8(0xFF4F, 0x01)
        bus.write8(0x8000, 0x34)

        self.assertTrue(bus.cgb_mode)
        self.assertEqual(bus.vram_bank, 1)
        self.assertEqual(bus.read8(0xFF4F), 0xFF)
        self.assertEqual(bus.read8(0x8000), 0x34)
        bus.write8(0xFF4F, 0x00)
        self.assertEqual(bus.vram_bank, 0)
        self.assertEqual(bus.read8(0xFF4F), 0xFE)
        self.assertEqual(bus.read8(0x8000), 0x12)

    def test_cgb_mode_exposes_wram_bank_register(self) -> None:
        bus = Bus(
            Cartridge(make_rom(cgb_flag=0x80)),
            serial_sink=lambda _: None,
            mode=EmulationMode.CGB,
        )

        bus.write8(0xC000, 0x11)
        bus.write8(0xD000, 0x22)
        bus.write8(0xFF70, 0x02)
        bus.write8(0xD000, 0x33)

        self.assertEqual(bus.read8(0xC000), 0x11)
        self.assertEqual(bus.read8(0xE000), 0x11)
        self.assertEqual(bus.wram_bank_register, 2)
        self.assertEqual(bus.wram_bank, 2)
        self.assertEqual(bus.read8(0xFF70), 0xFA)
        self.assertEqual(bus.read8(0xD000), 0x33)
        self.assertEqual(bus.read8(0xF000), 0x33)

        bus.write8(0xFF70, 0x00)
        self.assertEqual(bus.wram_bank_register, 0)
        self.assertEqual(bus.wram_bank, 1)
        self.assertEqual(bus.read8(0xFF70), 0xF8)
        self.assertEqual(bus.read8(0xD000), 0x22)
        self.assertEqual(bus.read8(0xF000), 0x22)

    def test_cgb_mode_exposes_object_priority_mode_register(self) -> None:
        bus = Bus(
            Cartridge(make_rom(cgb_flag=0x80)),
            serial_sink=lambda _: None,
            mode=EmulationMode.CGB,
        )

        self.assertEqual(bus.cgb_object_priority_mode, 0)
        self.assertEqual(bus.read8(0xFF6C), 0xFE)
        bus.write8(0xFF6C, 0xFF)
        self.assertEqual(bus.cgb_object_priority_mode, 1)
        self.assertEqual(bus.read8(0xFF6C), 0xFF)
        bus.write8(0xFF6C, 0x00)
        self.assertEqual(bus.cgb_object_priority_mode, 0)
        self.assertEqual(bus.read8(0xFF6C), 0xFE)

    def test_cgb_gdma_copies_immediately_with_masked_addresses(self) -> None:
        bus = make_cgb_bus()
        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)
        source = bytes(range(0x10))
        bus.wram[0x0000:0x0010] = source

        bus.write8(0xFF51, 0xC0)
        bus.write8(0xFF52, 0x0F)
        bus.write8(0xFF53, 0x91)
        bus.write8(0xFF54, 0x2F)
        bus.write8(0xFF55, 0x00)

        destination = 0x1120
        self.assertEqual(bytes(bus.vram[destination : destination + 0x10]), source)
        self.assertEqual(bus.read8(0xFF55), 0xFF)
        self.assertEqual(bus.vram_dma_gdma_blocks, 1)
        self.assertEqual(bus.vram_dma_bytes, 0x10)
        for address in (0xFF51, 0xFF52, 0xFF53, 0xFF54):
            with self.subTest(address=address):
                self.assertEqual(bus.read8(address), 0xFF)

    def test_cgb_gdma_respects_current_vram_bank(self) -> None:
        bus = make_cgb_bus()
        bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)
        source = bytes(0x80 + index for index in range(0x10))
        bus.wram[0x0000:0x0010] = source
        bus.vram[0x0000:0x0010] = bytes([0xEE] * 0x10)

        bus.write8(0xFF4F, 0x01)
        bus.write8(0xFF51, 0xC0)
        bus.write8(0xFF52, 0x00)
        bus.write8(0xFF53, 0x00)
        bus.write8(0xFF54, 0x00)
        bus.write8(0xFF55, 0x00)

        self.assertEqual(bytes(bus.vram[0x0000:0x0010]), bytes([0xEE] * 0x10))
        self.assertEqual(bytes(bus.vram[0x2000:0x2010]), source)

    def test_cgb_hdma_copies_one_block_per_visible_hblank(self) -> None:
        bus = make_cgb_bus()
        source = bytes(0x20 + index for index in range(0x20))
        bus.wram[0x0000:0x0020] = source
        bus.vram[0x0000:0x0020] = bytes([0x00] * 0x20)

        bus.write8(0xFF51, 0xC0)
        bus.write8(0xFF52, 0x00)
        bus.write8(0xFF53, 0x00)
        bus.write8(0xFF54, 0x00)
        bus.write8(0xFF55, 0x81)

        self.assertTrue(bus.vram_dma_active)
        self.assertEqual(bus.read8(0xFF55), 0x01)
        self.assertEqual(bytes(bus.vram[0x0000:0x0020]), bytes([0x00] * 0x20))

        tick_to_next_hblank(bus)

        self.assertTrue(bus.vram_dma_active)
        self.assertEqual(bus.read8(0xFF55), 0x00)
        self.assertEqual(bytes(bus.vram[0x0000:0x0010]), source[:0x10])
        self.assertEqual(bytes(bus.vram[0x0010:0x0020]), bytes([0x00] * 0x10))

        tick_to_next_hblank(bus)

        self.assertFalse(bus.vram_dma_active)
        self.assertEqual(bus.read8(0xFF55), 0xFF)
        self.assertEqual(bytes(bus.vram[0x0000:0x0020]), source)
        self.assertEqual(bus.vram_dma_hdma_blocks, 2)

    def test_cgb_hdma_abort_preserves_remaining_length_status(self) -> None:
        bus = make_cgb_bus()
        source = bytes(0x40 + index for index in range(0x30))
        bus.wram[0x0000:0x0030] = source
        bus.vram[0x0000:0x0030] = bytes([0x00] * 0x30)

        bus.write8(0xFF51, 0xC0)
        bus.write8(0xFF52, 0x00)
        bus.write8(0xFF53, 0x00)
        bus.write8(0xFF54, 0x00)
        bus.write8(0xFF55, 0x82)
        tick_to_next_hblank(bus)

        bus.write8(0xFF55, 0x00)

        self.assertFalse(bus.vram_dma_active)
        self.assertEqual(bus.read8(0xFF55), 0x81)
        self.assertEqual(bus.vram_dma_blocks_remaining, 2)
        tick_to_next_hblank(bus)
        self.assertEqual(bytes(bus.vram[0x0000:0x0010]), source[:0x10])
        self.assertEqual(bytes(bus.vram[0x0010:0x0030]), bytes([0x00] * 0x20))
        self.assertEqual(bus.vram_dma_hdma_blocks, 1)

    def test_cgb_mode_exposes_palette_registers(self) -> None:
        bus = Bus(
            Cartridge(make_rom(cgb_flag=0x80)),
            serial_sink=lambda _: None,
            mode=EmulationMode.CGB,
        )

        bus.write8(0xFF68, 0x80)
        bus.write8(0xFF69, 0x12)
        self.assertEqual(bus.read8(0xFF68), 0x81)
        bus.write8(0xFF68, 0x00)
        self.assertEqual(bus.read8(0xFF69), 0x12)

        bus.write8(0xFF6A, 0x82)
        bus.write8(0xFF6B, 0xAB)
        self.assertEqual(bus.read8(0xFF6A), 0x83)
        bus.write8(0xFF6A, 0x02)
        self.assertEqual(bus.read8(0xFF6B), 0xAB)

        bus.write8(0xFF68, 0xFF)
        self.assertEqual(bus.read8(0xFF68), 0xBF)

    def test_cgb_palette_data_mode3_write_is_blocked_but_auto_increments(self) -> None:
        bus = make_cgb_bus()
        bus.write8(0xFF40, 0x91)
        bus.write8(0xFF68, 0x80)
        bus.write8(0xFF69, 0x12)
        bus.write8(0xFF68, 0x80)

        bus.tick(MODE2_DOTS)

        self.assertEqual(bus.ppu.mode, MODE_DRAWING)
        bus.write8(0xFF69, 0x34)
        self.assertEqual(bus.read8(0xFF68), 0x81)
        self.assertEqual(bus.read8(0xFF69), 0xFF)

        bus.tick(MODE3_DOTS)
        bus.write8(0xFF68, 0x00)
        self.assertEqual(bus.read8(0xFF69), 0x12)

    def test_key1_arms_and_reports_cgb_speed_state(self) -> None:
        bus = make_cgb_bus()

        self.assertEqual(bus.read8(0xFF4D), 0x7E)
        self.assertFalse(bus.double_speed)
        self.assertFalse(bus.speed_switch_armed)

        bus.write8(0xFF4D, 0x00)
        self.assertEqual(bus.read8(0xFF4D), 0x7E)

        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.speed_switch_armed)
        self.assertEqual(bus.speed_switch_arm_writes, 1)
        self.assertEqual(bus.read8(0xFF4D), 0x7F)

        self.assertTrue(bus.perform_speed_switch())
        self.assertTrue(bus.double_speed)
        self.assertFalse(bus.speed_switch_armed)
        self.assertEqual(bus.speed_switches, 1)
        self.assertEqual(bus.read8(0xFF4D), 0xFE)

        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())
        self.assertFalse(bus.double_speed)
        self.assertEqual(bus.speed_switches, 2)
        self.assertEqual(bus.read8(0xFF4D), 0x7E)

    def test_dmg_key1_speed_switch_preserves_legacy_timing_domain(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())
        self.assertTrue(bus.double_speed)

        bus.tick(16)

        self.assertEqual(bus.ppu.line_dots, 16)

    def test_cgb_double_speed_keeps_timers_serial_ppu_and_apu_domains_sane(self) -> None:
        out: list[str] = []
        bus = Bus(
            Cartridge(make_rom(cgb_flag=0x80)),
            serial_sink=out.append,
            mode=EmulationMode.CGB,
        )
        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())

        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF07, 0x05)
        apu_counter = bus.apu._frame_sequence_counter

        bus.tick(16)

        self.assertEqual(bus.read8(0xFF05), 0x01)
        self.assertEqual(bus.ppu.line_dots, 8)
        self.assertEqual(bus.apu._frame_sequence_counter, apu_counter + 8)

        bus.write8(0xFF01, ord("Z"))
        bus.write8(0xFF02, 0x81)
        bus.tick(SERIAL_INTERNAL_TRANSFER_CYCLES - 1)
        self.assertEqual(out, [])
        bus.tick(1)
        self.assertEqual(out, ["Z"])

    def test_cgb_double_speed_ppu_frame_progress_takes_twice_the_cpu_cycles(self) -> None:
        bus = make_cgb_bus()
        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())

        bus.tick(DOTS_PER_LINE * 2 - 1)
        self.assertEqual(bus.ppu._scanline, 0)
        self.assertEqual(bus.ppu.line_dots, DOTS_PER_LINE - 1)

        bus.tick(1)

        self.assertEqual(bus.ppu._scanline, 1)
        self.assertEqual(bus.ppu.line_dots, 0)

    def test_cgb_double_speed_oam_dma_uses_cpu_cycle_domain(self) -> None:
        bus = make_cgb_bus()
        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())
        for index in range(0xA0):
            bus.wram[index] = index

        bus.write8(0xFF46, 0xC0)
        bus.tick(639)
        self.assertTrue(bus.oam_dma_active)
        bus.tick(1)

        self.assertFalse(bus.oam_dma_active)
        self.assertEqual(bus.oam[0], 0x00)
        self.assertEqual(bus.oam[0x9F], 0x9F)
        self.assertEqual(bus.ppu.line_dots, 320)

    def test_cgb_double_speed_hdma_still_runs_on_ppu_hblank_domain(self) -> None:
        bus = make_cgb_bus()
        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())
        for index in range(0x20):
            bus.wram[index] = 0x80 | index
        bus.write8(0xFF51, 0xC0)
        bus.write8(0xFF52, 0x00)
        bus.write8(0xFF53, 0x00)
        bus.write8(0xFF54, 0x00)
        bus.write8(0xFF55, 0x81)

        bus.tick((MODE2_DOTS + MODE3_DOTS) * 2 - 1)
        self.assertEqual(bus.vram_dma_hdma_blocks, 0)
        bus.tick(1)
        self.assertEqual(bus.vram_dma_hdma_blocks, 1)
        self.assertEqual(bus.vram[0], 0x80)
        self.assertTrue(bus.vram_dma_active)

        ppu_cycles_to_next_hblank = DOTS_PER_LINE - bus.ppu.line_dots + MODE2_DOTS + MODE3_DOTS
        bus.tick(ppu_cycles_to_next_hblank * 2)

        self.assertEqual(bus.vram_dma_hdma_blocks, 2)
        self.assertEqual(bus.vram[0x10], 0x90)
        self.assertFalse(bus.vram_dma_active)

    def test_serial_transfer_hook(self) -> None:
        out: list[str] = []
        bus = Bus(Cartridge(make_rom()), serial_sink=out.append)
        bus.write8(0xFF0F, 0x00)

        bus.write8(0xFF01, ord("A"))
        bus.write8(0xFF02, 0x81)

        self.assertEqual(out, [])
        self.assertEqual(bus.read8(0xFF02) & 0x80, 0x80)

        bus.tick(SERIAL_INTERNAL_TRANSFER_CYCLES - 1)
        self.assertEqual(out, [])
        self.assertEqual(bus.read8(0xFF02) & 0x80, 0x80)

        bus.tick(1)

        self.assertEqual(out, ["A"])
        self.assertEqual(bus.serial_text, "A")
        self.assertEqual(bus.read8(0xFF01), 0xFF)
        self.assertEqual(bus.read8(0xFF02) & 0x80, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x08, 0x08)

    def test_serial_internal_clock_checks_control_bits(self) -> None:
        out: list[str] = []
        bus = Bus(Cartridge(make_rom()), serial_sink=out.append)

        bus.write8(0xFF01, ord("E"))
        bus.write8(0xFF02, 0x80)
        bus.tick(SERIAL_INTERNAL_TRANSFER_CYCLES)
        self.assertEqual(out, [])
        self.assertEqual(bus.read8(0xFF02) & 0x80, 0x80)

        bus.write8(0xFF01, ord("I"))
        bus.write8(0xFF02, 0xC1)

        self.assertEqual(out, [])
        bus.tick(SERIAL_INTERNAL_TRANSFER_CYCLES)

        self.assertEqual(out, ["I"])
        self.assertEqual(bus.serial_text, "I")
        self.assertEqual(bus.read8(0xFF01), 0xFF)
        self.assertEqual(bus.read8(0xFF02) & 0x80, 0x00)

    def test_mbc1_rom_bank_switching_for_combined_cpu_instrs(self) -> None:
        bus = Bus(Cartridge(make_mbc1_rom()), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0x4000), 0x11)
        bus.write8(0x2000, 0x02)
        self.assertEqual(bus.read8(0x4000), 0x22)
        bus.write8(0x2000, 0x03)
        self.assertEqual(bus.read8(0x4000), 0x33)
        bus.write8(0x2000, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x11)

    def test_mbc1_large_rom_advanced_mode_switches_lower_area(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(64, cart_type=0x01)), serial_sink=lambda _: None)

        bus.write8(0x4000, 0x01)
        self.assertEqual(bus.read8(0x0000), 0x00)
        self.assertEqual(bus.read8(0x4000), 0x21)

        bus.write8(0x6000, 0x01)

        self.assertEqual(bus.read8(0x0000), 0x20)
        self.assertEqual(bus.read8(0x4000), 0x21)

    def test_mbc1_small_rom_can_select_bank_zero_with_nonzero_raw_register(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(16, cart_type=0x01)), serial_sink=lambda _: None)

        bus.write8(0x2000, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x01)

        bus.write8(0x2000, 0x10)

        self.assertEqual(bus.read8(0x4000), 0x00)

    def test_mbc1m_multicart_uses_secondary_register_as_bits_4_and_5(self) -> None:
        cartridge = Cartridge(make_mbc1m_rom())
        bus = Bus(cartridge, serial_sink=lambda _: None)

        self.assertTrue(cartridge.has_mbc1m)

        bus.write8(0x4000, 0x01)
        self.assertEqual(bus.read8(0x0000), 0x00)
        self.assertEqual(bus.read8(0x4000), 0x11)

        bus.write8(0x6000, 0x01)
        self.assertEqual(bus.read8(0x0000), 0x10)
        self.assertEqual(bus.read8(0x4000), 0x11)

        bus.write8(0x2000, 0x02)
        self.assertEqual(bus.read8(0x4000), 0x12)
        bus.write8(0x4000, 0x03)
        self.assertEqual(bus.read8(0x0000), 0x30)
        self.assertEqual(bus.read8(0x4000), 0x32)

    def test_mbc1m_ignores_bit_4_but_uses_raw_register_for_bank_zero_translation(self) -> None:
        bus = Bus(Cartridge(make_mbc1m_rom()), serial_sink=lambda _: None)
        bus.write8(0x6000, 0x01)
        bus.write8(0x4000, 0x02)

        bus.write8(0x2000, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x21)

        bus.write8(0x2000, 0x10)
        self.assertEqual(bus.read8(0x4000), 0x20)

    def test_mbc1_ram_enable_and_disable(self) -> None:
        bus = Bus(Cartridge(make_mbc1_rom(ram_size_code=0x02, cart_type=0x02)), serial_sink=lambda _: None)

        bus.write8(0xA000, 0x12)
        self.assertEqual(bus.read8(0xA000), 0xFF)

        bus.write8(0x0000, 0x0A)
        bus.write8(0xA000, 0x34)
        self.assertEqual(bus.read8(0xA000), 0x34)

        bus.write8(0x0000, 0x00)
        bus.write8(0xA000, 0x56)
        self.assertEqual(bus.read8(0xA000), 0xFF)
        bus.write8(0x0000, 0x0A)
        self.assertEqual(bus.read8(0xA000), 0x34)

    def test_mbc1_ram_banking_mode_selects_ram_bank(self) -> None:
        bus = Bus(Cartridge(make_mbc1_rom(ram_size_code=0x03, cart_type=0x03)), serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x01)
        bus.write8(0x6000, 0x01)
        bus.write8(0xA000, 0x22)
        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x11)
        bus.write8(0x4000, 0x01)
        self.assertEqual(bus.read8(0xA000), 0x22)

    def test_large_mbc1_rom_wiring_keeps_ram_bank_fixed(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(64, cart_type=0x03, ram_size_code=0x03)), serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)
        bus.write8(0x6000, 0x01)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x02)
        bus.write8(0xA000, 0x22)
        bus.write8(0x4000, 0x00)

        self.assertEqual(bus.read8(0xA000), 0x22)

    def test_cartridge_ram_dump_and_load(self) -> None:
        cartridge = Cartridge(make_mbc1_rom(ram_size_code=0x02, cart_type=0x03))
        cartridge.write_rom_control(0x0000, 0x0A)
        cartridge.write_ram(0xA000, 0x5A)
        snapshot = cartridge.dump_ram()

        restored = Cartridge(make_mbc1_rom(ram_size_code=0x02, cart_type=0x03))
        restored.load_ram(snapshot)
        restored.write_rom_control(0x0000, 0x0A)

        self.assertEqual(restored.read_ram(0xA000), 0x5A)

    def test_rom_ram_cartridge_external_ram_is_always_accessible(self) -> None:
        bus = Bus(Cartridge(make_rom_ram_rom()), serial_sink=lambda _: None)

        bus.write8(0xA000, 0x5A)
        bus.write8(0xBFFF, 0xC3)

        self.assertEqual(bus.read8(0xA000), 0x5A)
        self.assertEqual(bus.read8(0xBFFF), 0xC3)
        self.assertTrue(bus.cartridge.has_persistent_data)

    def test_rom_ram_dump_and_load_round_trips_full_ram(self) -> None:
        cartridge = Cartridge(make_rom_ram_rom(cart_type=0x09))
        cartridge.write_ram(0xA000, 0x12)
        cartridge.write_ram(0xBFFF, 0x34)
        snapshot = cartridge.dump_ram()

        restored = Cartridge(make_rom_ram_rom(cart_type=0x09))
        restored.load_ram(snapshot)

        self.assertEqual(len(snapshot), 0x2000)
        self.assertEqual(restored.read_ram(0xA000), 0x12)
        self.assertEqual(restored.read_ram(0xBFFF), 0x34)

    def test_mbc2_rom_bank_select_uses_address_bit_8(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(16, cart_type=0x05)), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0x4000), 0x01)
        bus.write8(0x2000, 0x02)
        self.assertEqual(bus.read8(0x4000), 0x01)
        bus.write8(0x2100, 0x02)
        self.assertEqual(bus.read8(0x4000), 0x02)
        bus.write8(0x2100, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x01)

    def test_mbc2_internal_ram_enable_4_bit_storage_and_mirroring(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(4, cart_type=0x05)), serial_sink=lambda _: None)

        bus.write8(0xA000, 0x0C)
        self.assertEqual(bus.read8(0xA000), 0xFF)

        bus.write8(0x0000, 0x0A)
        bus.write8(0xA000, 0xAC)

        self.assertEqual(bus.read8(0xA000), 0xFC)
        self.assertEqual(bus.read8(0xA200), 0xFC)

        bus.write8(0x0100, 0x00)
        bus.write8(0xA000, 0x05)
        self.assertEqual(bus.read8(0xA000), 0xF5)

        bus.write8(0x0000, 0x00)
        bus.write8(0xA000, 0x07)
        self.assertEqual(bus.read8(0xA000), 0xFF)
        bus.write8(0x0000, 0x0A)
        self.assertEqual(bus.read8(0xA000), 0xF5)

    def test_mbc2_ram_dump_and_load_round_trips_internal_ram(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0x06))
        cartridge.write_rom_control(0x0000, 0x0A)
        cartridge.write_ram(0xA123, 0x5E)
        snapshot = cartridge.dump_ram()

        restored = Cartridge(make_banked_rom(4, cart_type=0x06))
        restored.load_ram(snapshot)
        restored.write_rom_control(0x0000, 0x0A)

        self.assertEqual(len(snapshot), 0x200)
        self.assertEqual(restored.read_ram(0xA123), 0xFE)

    def test_mbc3_rom_bank_zero_maps_to_one_and_allows_bank_20(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(64, cart_type=0x13, ram_size_code=0x03)), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0x4000), 0x01)
        bus.write8(0x2000, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x01)
        bus.write8(0x2000, 0x20)
        self.assertEqual(bus.read8(0x4000), 0x20)

    def test_mbc3_ram_banking_and_rtc_register_mapping(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(4, cart_type=0x10, ram_size_code=0x03)), serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x02)
        bus.write8(0xA000, 0x22)
        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x11)
        bus.write8(0x4000, 0x02)
        self.assertEqual(bus.read8(0xA000), 0x22)

        bus.write8(0x4000, 0x08)
        bus.write8(0xA000, 0x3B)
        self.assertEqual(bus.read8(0xA000), 0x3B)

    def test_mbc3_without_timer_does_not_map_rtc_registers(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(4, cart_type=0x13, ram_size_code=0x03)), serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x08)
        bus.write8(0xA000, 0x3B)
        self.assertEqual(bus.read8(0xA000), 0xFF)

        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x11)

    def test_mbc3_ram_selects_four_to_seven_wrap_on_32k_ram(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0x10, ram_size_code=0x03))
        bus = Bus(cartridge, serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x03)
        bus.write8(0xA000, 0x33)

        bus.write8(0x4000, 0x04)
        self.assertEqual(bus.read8(0xA000), 0x11)
        bus.write8(0xA000, 0x44)
        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x44)

        bus.write8(0x4000, 0x07)
        self.assertEqual(bus.read8(0xA000), 0x33)
        bus.write8(0xA000, 0x77)
        bus.write8(0x4000, 0x03)
        self.assertEqual(bus.read8(0xA000), 0x77)

    def test_mbc3_64k_ram_can_select_banks_zero_to_seven(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0x10, ram_size_code=0x05))
        bus = Bus(cartridge, serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x10)
        bus.write8(0x4000, 0x04)
        bus.write8(0xA000, 0x40)
        bus.write8(0x4000, 0x07)
        bus.write8(0xA000, 0x70)

        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x10)
        bus.write8(0x4000, 0x04)
        self.assertEqual(bus.read8(0xA000), 0x40)
        bus.write8(0x4000, 0x07)
        self.assertEqual(bus.read8(0xA000), 0x70)
        self.assertEqual(len(cartridge.ram), 0x10000)

    def test_mbc3_invalid_rtc_select_does_not_alias_ram(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0x10, ram_size_code=0x03))
        bus = Bus(cartridge, serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x11)
        for invalid_select in (0x0D, 0x0F):
            with self.subTest(invalid_select=invalid_select):
                bus.write8(0x4000, invalid_select)
                bus.write8(0xA000, 0x20 + invalid_select)

                self.assertEqual(bus.read8(0xA000), 0xFF)
                bus.write8(0x4000, 0x00)
                self.assertEqual(bus.read8(0xA000), 0x11)

        bus.write8(0x4000, 0x08)
        bus.write8(0xA000, 0x33)
        bus.write8(0x4000, 0x0F)
        bus.write8(0xA000, 0x44)
        bus.write8(0x4000, 0x08)
        self.assertEqual(bus.read8(0xA000), 0x33)

    def test_mbc3_rtc_latch_freezes_until_relatch(self) -> None:
        clock = FakeClock()
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=clock.time,
        )
        cartridge.write_rom_control(0x0000, 0x0A)

        clock.advance(65)
        cartridge.write_rom_control(0x6000, 0x00)
        cartridge.write_rom_control(0x6000, 0x01)
        cartridge.write_rom_control(0x4000, 0x08)
        self.assertEqual(cartridge.read_ram(0xA000), 5)
        cartridge.write_rom_control(0x4000, 0x09)
        self.assertEqual(cartridge.read_ram(0xA000), 1)

        clock.advance(20)
        cartridge.write_rom_control(0x4000, 0x08)
        self.assertEqual(cartridge.read_ram(0xA000), 5)

        cartridge.write_rom_control(0x6000, 0x00)
        cartridge.write_rom_control(0x6000, 0x01)
        self.assertEqual(cartridge.read_ram(0xA000), 25)

    def test_mbc3_rtc_halt_stops_clock_until_resumed(self) -> None:
        clock = FakeClock()
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=clock.time,
        )
        cartridge.write_rom_control(0x0000, 0x0A)

        cartridge.write_rom_control(0x4000, 0x0C)
        cartridge.write_ram(0xA000, 0x40)
        clock.advance(100)
        cartridge.write_rom_control(0x6000, 0x00)
        cartridge.write_rom_control(0x6000, 0x01)
        cartridge.write_rom_control(0x4000, 0x08)
        self.assertEqual(cartridge.read_ram(0xA000), 0)

        cartridge.write_rom_control(0x4000, 0x0C)
        cartridge.write_ram(0xA000, 0x00)
        clock.advance(61)
        cartridge.write_rom_control(0x6000, 0x00)
        cartridge.write_rom_control(0x6000, 0x01)
        cartridge.write_rom_control(0x4000, 0x08)
        self.assertEqual(cartridge.read_ram(0xA000), 1)
        cartridge.write_rom_control(0x4000, 0x09)
        self.assertEqual(cartridge.read_ram(0xA000), 1)

    def test_mbc3_rtc_day_counter_wrap_sets_carry(self) -> None:
        clock = FakeClock()
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=clock.time,
        )
        cartridge.write_rom_control(0x0000, 0x0A)

        cartridge.write_rom_control(0x4000, 0x0B)
        cartridge.write_ram(0xA000, 0xFE)
        cartridge.write_rom_control(0x4000, 0x0C)
        cartridge.write_ram(0xA000, 0x01)

        clock.advance(2 * 86400)
        cartridge.write_rom_control(0x6000, 0x00)
        cartridge.write_rom_control(0x6000, 0x01)

        cartridge.write_rom_control(0x4000, 0x0B)
        self.assertEqual(cartridge.read_ram(0xA000), 0x00)
        cartridge.write_rom_control(0x4000, 0x0C)
        self.assertEqual(cartridge.read_ram(0xA000), 0x80)

    def test_mbc3_rtc_save_state_preserves_ram_and_clock(self) -> None:
        clock = FakeClock(10)
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=clock.time,
        )
        cartridge.write_rom_control(0x0000, 0x0A)
        cartridge.write_ram(0xA000, 0x5A)
        clock.advance(65)

        ram_snapshot = cartridge.dump_ram()
        rtc_snapshot = cartridge._dump_rtc_state()

        restored_clock = FakeClock(75)
        restored = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=restored_clock.time,
        )
        restored.load_ram(ram_snapshot)
        restored._load_rtc_state(rtc_snapshot)
        restored.write_rom_control(0x0000, 0x0A)

        self.assertEqual(restored.read_ram(0xA000), 0x5A)
        self.assertEqual(self.read_latched_rtc_register(restored, 0x08), 5)
        self.assertEqual(self.read_latched_rtc_register(restored, 0x09), 1)

    def test_mbc3_rtc_only_save_state_round_trips_without_ram(self) -> None:
        clock = FakeClock()
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x0F, ram_size_code=0x00),
            rtc_time_provider=clock.time,
        )
        clock.advance(3)

        rtc_snapshot = cartridge._dump_rtc_state()

        restored = Cartridge(
            make_banked_rom(4, cart_type=0x0F, ram_size_code=0x00),
            rtc_time_provider=clock.time,
        )
        restored._load_rtc_state(rtc_snapshot)

        self.assertEqual(self.read_latched_rtc_register(restored, 0x08), 3)

    def test_mbc3_rtc_load_advances_by_elapsed_wall_time(self) -> None:
        clock = FakeClock(10)
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=clock.time,
        )
        clock.advance(10)

        rtc_snapshot = cartridge._dump_rtc_state()

        restored_clock = FakeClock(25)
        restored = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=restored_clock.time,
        )
        restored._load_rtc_state(rtc_snapshot)

        self.assertEqual(self.read_latched_rtc_register(restored, 0x08), 15)

    def test_mbc3_halted_rtc_does_not_advance_after_load(self) -> None:
        clock = FakeClock()
        cartridge = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=clock.time,
        )
        cartridge.write_rom_control(0x0000, 0x0A)
        clock.advance(12)
        cartridge.write_rom_control(0x4000, 0x0C)
        cartridge.write_ram(0xA000, 0x40)

        rtc_snapshot = cartridge._dump_rtc_state()

        restored_clock = FakeClock(100)
        restored = Cartridge(
            make_banked_rom(4, cart_type=0x10, ram_size_code=0x03),
            rtc_time_provider=restored_clock.time,
        )
        restored._load_rtc_state(rtc_snapshot)

        self.assertEqual(self.read_latched_rtc_register(restored, 0x08), 12)
        self.assertEqual(self.read_latched_rtc_register(restored, 0x0C), 0x40)

    def test_mbc5_selects_rom_bank_zero_and_9th_rom_bank_bit(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(260, cart_type=0x1B, ram_size_code=0x03)), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0x4000), 0x01)
        bus.write8(0x2000, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x00)
        bus.write8(0x2000, 0x01)
        bus.write8(0x3000, 0x01)
        self.assertEqual(bus.read8(0x4000), 0x01)
        self.assertEqual(bus.read8(0x4001), 0x01)

    def test_mbc5_ram_bank_select(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(4, cart_type=0x1B, ram_size_code=0x04)), serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x44)
        bus.write8(0x4000, 0x0F)
        bus.write8(0xA000, 0x99)
        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x44)
        bus.write8(0x4000, 0x0F)
        self.assertEqual(bus.read8(0xA000), 0x99)

    def test_mbc5_rumble_bit_does_not_select_ram_bank(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0x1E, ram_size_code=0x04))
        bus = Bus(cartridge, serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x08)
        bus.write8(0xA000, 0x22)

        self.assertTrue(cartridge.rumble_active)
        self.assertEqual(cartridge.mbc5_ram_bank, 0)
        self.assertEqual(bus.read8(0xA000), 0x22)

        bus.write8(0x4000, 0x00)
        self.assertFalse(cartridge.rumble_active)
        self.assertEqual(bus.read8(0xA000), 0x22)

    def test_plain_mbc5_uses_bit_3_as_ram_bank_select(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0x1B, ram_size_code=0x04))
        bus = Bus(cartridge, serial_sink=lambda _: None)
        bus.write8(0x0000, 0x0A)

        bus.write8(0xA000, 0x33)
        bus.write8(0x4000, 0x08)
        bus.write8(0xA000, 0x44)

        self.assertFalse(cartridge.rumble_active)
        self.assertEqual(cartridge.mbc5_ram_bank, 8)
        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x33)
        bus.write8(0x4000, 0x08)
        self.assertEqual(bus.read8(0xA000), 0x44)

    def test_huc1_rom_bank_select_maps_zero_to_one(self) -> None:
        bus = Bus(Cartridge(make_banked_rom(8, cart_type=0xFF, ram_size_code=0x03)), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0x4000), 0x01)
        bus.write8(0x2000, 0x03)
        self.assertEqual(bus.read8(0x4000), 0x03)
        bus.write8(0x2000, 0x00)
        self.assertEqual(bus.read8(0x4000), 0x01)

    def test_huc1_ram_banking_and_ir_mode(self) -> None:
        cartridge = Cartridge(make_banked_rom(4, cart_type=0xFF, ram_size_code=0x03))
        bus = Bus(cartridge, serial_sink=lambda _: None)

        bus.write8(0xA000, 0x11)
        bus.write8(0x4000, 0x02)
        bus.write8(0xA000, 0x22)
        bus.write8(0x4000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x11)
        bus.write8(0x4000, 0x02)
        self.assertEqual(bus.read8(0xA000), 0x22)

        bus.write8(0x0000, 0x0E)
        self.assertEqual(bus.read8(0xA000), 0xC0)
        cartridge.huc1_ir_input = True
        self.assertEqual(bus.read8(0xA000), 0xC1)
        bus.write8(0xA000, 0x01)
        self.assertTrue(cartridge.huc1_ir_transmitter_enabled)
        bus.write8(0xA000, 0x00)
        self.assertFalse(cartridge.huc1_ir_transmitter_enabled)

        bus.write8(0x0000, 0x00)
        self.assertEqual(bus.read8(0xA000), 0x22)

    def test_div_and_tima_tick(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF04, 0xFF)
        self.assertEqual(bus.read8(0xFF04), 0x00)
        bus.tick(255)
        self.assertEqual(bus.read8(0xFF04), 0x00)
        bus.tick(1)
        self.assertEqual(bus.read8(0xFF04), 0x01)

        bus.write8(0xFF05, 0xFE)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF07, 0x05)
        bus.write8(0xFF0F, 0x00)
        bus.tick(16)
        self.assertEqual(bus.read8(0xFF05), 0xFF)
        bus.tick(16)
        self.assertEqual(bus.read8(0xFF05), 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x00)
        bus.tick(4)
        self.assertEqual(bus.read8(0xFF05), 0x42)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_enabled_timer_counts_bulk_falling_edges_without_overflow(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0x10)
        bus.write8(0xFF07, 0x05)

        bus.tick(80)

        self.assertEqual(bus.read8(0xFF05), 0x15)
        self.assertEqual(bus.read8(0xFF04), 0x00)

    def test_timer_uses_div_falling_edges_for_div_and_tac_writes(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF07, 0x05)
        bus.tick(8)
        self.assertEqual(bus.read8(0xFF05), 0x00)

        bus.write8(0xFF04, 0x00)
        self.assertEqual(bus.read8(0xFF04), 0x00)
        self.assertEqual(bus.read8(0xFF05), 0x01)

        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF07, 0x05)
        bus.tick(8)
        bus.write8(0xFF07, 0x04)

        self.assertEqual(bus.read8(0xFF07), 0xFC)
        self.assertEqual(bus.read8(0xFF05), 0x01)

    def test_tima_write_cancels_pending_overflow_reload(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0xFF)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF07, 0x05)
        bus.write8(0xFF0F, 0x00)

        bus.tick(16)
        self.assertEqual(bus.read8(0xFF05), 0x00)
        bus.write8(0xFF05, 0x99)
        bus.tick(4)

        self.assertEqual(bus.read8(0xFF05), 0x99)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x00)

    def test_pending_tima_reload_uses_latest_tma_value(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0xFF)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF07, 0x05)
        bus.write8(0xFF0F, 0x00)

        bus.tick(16)
        bus.tick(3)
        bus.write8(0xFF06, 0x77)
        bus.tick(1)

        self.assertEqual(bus.read8(0xFF05), 0x77)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_oam_dma_copy(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        for offset in range(0xA0):
            bus.write8(0xC000 + offset, offset)

        bus.write8(0xFF46, 0xC0)

        self.assertTrue(bus.oam_dma_active)
        bus.tick(639)
        self.assertTrue(bus.oam_dma_active)
        bus.tick(1)
        self.assertFalse(bus.oam_dma_active)
        self.assertEqual(bus.oam[0x00], 0x00)
        self.assertEqual(bus.oam[0x01], 0x01)
        self.assertEqual(bus.oam[0x9F], 0x9F)

    def test_oam_dma_blocks_non_hram_access_while_active(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xC000, 0x12)
        bus.write8(0xFF80, 0x34)

        bus.write8(0xFF46, 0xC0)
        bus.tick(4)

        self.assertEqual(bus.read8(0xC000), 0xFF)
        bus.write8(0xC000, 0x99)
        bus.write8(0xFF80, 0x77)
        self.assertEqual(bus.read8(0xFF80), 0x77)

        bus.tick(636)

        self.assertEqual(bus.read8(0xC000), 0x12)
        self.assertEqual(bus.read8(0xFF80), 0x77)

    def test_joypad_action_and_direction_matrix_reads(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.joypad.press("a")
        bus.joypad.press("left")

        bus.write8(0xFF00, 0x20)
        self.assertEqual(bus.read8(0xFF00) & 0x0F, 0b1101)

        bus.write8(0xFF00, 0x10)
        self.assertEqual(bus.read8(0xFF00) & 0x0F, 0b1110)

        bus.write8(0xFF00, 0x00)
        self.assertEqual(bus.read8(0xFF00) & 0x0F, 0b1100)

    def test_joypad_press_requests_interrupt_for_selected_line(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF00, 0x10)

        bus.joypad.press("a")

        self.assertEqual(bus.read8(0xFF0F) & 0x10, 0x10)

    def test_joypad_held_button_does_not_retrigger_interrupt(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF00, 0x10)

        bus.joypad.press("a")
        bus.write8(0xFF0F, 0x00)
        bus.joypad.set_pressed({"a"})

        self.assertEqual(bus.read8(0xFF0F) & 0x10, 0x00)

    def test_joypad_unselected_press_does_not_request_interrupt(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF0F, 0x00)
        bus.write8(0xFF00, 0x30)

        bus.joypad.press("a")

        self.assertEqual(bus.read8(0xFF0F) & 0x10, 0x00)

    def test_joypad_set_pressed_rejects_unknown_buttons(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        with self.assertRaisesRegex(ValueError, "Unknown button"):
            bus.joypad.set_pressed({"a", "menu"})


if __name__ == "__main__":
    unittest.main()
