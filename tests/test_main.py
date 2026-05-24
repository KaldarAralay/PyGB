from __future__ import annotations

import unittest

from apu import CPU_CLOCK_HZ
from bus import EmulationMode
from cartridge import Cartridge, compute_header_checksum
from emulator import Emulator


def make_rom(program: bytes = b"\x00", cgb_flag: int = 0x00) -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0100 : 0x0100 + len(program)] = program
    rom[0x0134 : 0x0134 + len(b"MAINTEST")] = b"MAINTEST"
    rom[0x0143] = cgb_flag
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_mbc1_ram_rom() -> bytes:
    rom = bytearray([0x00] * 0x10000)
    rom[0x0134 : 0x0134 + len(b"RESETT")] = b"RESETT"
    rom[0x0147] = 0x03
    rom[0x0148] = 0x01
    rom[0x0149] = 0x02
    for bank in range(4):
        rom[bank * 0x4000] = bank
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


class MainLoopTests(unittest.TestCase):
    def test_emulator_defaults_to_dmg_mode_even_for_cgb_capable_rom(self) -> None:
        emulator = Emulator(
            Cartridge(make_rom(cgb_flag=0x80)),
            serial_sink=lambda _: None,
        )

        self.assertEqual(emulator.mode, EmulationMode.DMG)
        self.assertFalse(emulator.bus.cgb_mode)

    def test_emulator_forces_cgb_mode_for_cgb_only_roms(self) -> None:
        cartridge = Cartridge(make_rom(cgb_flag=0xC0))

        default = Emulator(cartridge, serial_sink=lambda _: None)
        explicit_dmg = Emulator(cartridge, serial_sink=lambda _: None, mode="dmg")
        auto = Emulator(cartridge, serial_sink=lambda _: None, mode="auto")

        self.assertEqual(default.mode, EmulationMode.CGB)
        self.assertTrue(default.bus.cgb_mode)
        self.assertEqual(explicit_dmg.mode, EmulationMode.CGB)
        self.assertTrue(explicit_dmg.bus.cgb_mode)
        self.assertEqual(auto.mode, EmulationMode.CGB)
        self.assertTrue(auto.bus.cgb_mode)

    def test_cgb_post_boot_identity_registers(self) -> None:
        emulator = Emulator(
            Cartridge(make_rom(cgb_flag=0xC0)),
            serial_sink=lambda _: None,
        )

        self.assertEqual(emulator.cpu.a, 0x11)
        self.assertEqual(emulator.cpu.f, 0x80)
        self.assertEqual(emulator.cpu.bc, 0x0000)
        self.assertEqual(emulator.cpu.de, 0xFF56)
        self.assertEqual(emulator.cpu.hl, 0x000D)
        self.assertEqual(emulator.cpu.sp, 0xFFFE)
        self.assertEqual(emulator.cpu.pc, 0x0100)
        self.assertEqual(emulator.bus.read8(0xFF4D), 0x7E)
        self.assertFalse(emulator.bus.double_speed)
        self.assertFalse(emulator.bus.speed_switch_armed)
        self.assertEqual(emulator.bus.read8(0xFF4F), 0xFE)
        self.assertEqual(emulator.bus.read8(0xFF70), 0xF8)

    def test_emulator_can_select_cgb_mode_explicitly_or_from_auto(self) -> None:
        cgb_cartridge = Cartridge(make_rom(cgb_flag=0x80))
        dmg_cartridge = Cartridge(make_rom())

        explicit = Emulator(cgb_cartridge, serial_sink=lambda _: None, mode="cgb")
        auto_cgb = Emulator(cgb_cartridge, serial_sink=lambda _: None, mode="auto")
        auto_dmg = Emulator(dmg_cartridge, serial_sink=lambda _: None, mode="auto")

        self.assertEqual(explicit.mode, EmulationMode.CGB)
        self.assertTrue(explicit.bus.cgb_mode)
        self.assertEqual(auto_cgb.mode, EmulationMode.CGB)
        self.assertTrue(auto_cgb.bus.cgb_mode)
        self.assertEqual(auto_dmg.mode, EmulationMode.DMG)
        self.assertFalse(auto_dmg.bus.cgb_mode)

        explicit.bus.write8(0xFF70, 0x02)
        explicit.reset()
        self.assertEqual(explicit.mode, EmulationMode.CGB)
        self.assertTrue(explicit.bus.cgb_mode)
        self.assertEqual(explicit.bus.read8(0xFF70), 0xF8)

    def test_cpu_run_can_stop_on_ppu_frame_count(self) -> None:
        emulator = Emulator(Cartridge(make_rom()), serial_sink=lambda _: None)
        target = emulator.bus.ppu.frame_count + 1

        emulator.run(max_instructions=200_000, max_frames=1)

        self.assertEqual(emulator.bus.ppu.frame_count, target)

    def test_run_with_zero_frame_target_does_not_step_cpu(self) -> None:
        emulator = Emulator(Cartridge(make_rom(b"\x3C")), serial_sink=lambda _: None)

        emulator.run(max_frames=0)

        self.assertEqual(emulator.cpu.instructions, 0)
        self.assertEqual(emulator.cpu.a, 0x01)
        self.assertEqual(emulator.bus.ppu.frame_count, 0)

    def test_emulator_run_rejects_negative_limits(self) -> None:
        emulator = Emulator(Cartridge(make_rom()), serial_sink=lambda _: None)

        with self.assertRaisesRegex(ValueError, "max_instructions"):
            emulator.run(max_instructions=-1)
        with self.assertRaisesRegex(ValueError, "max_frames"):
            emulator.run(max_frames=-1)

    def test_rom_program_can_render_background_tile_through_ppu(self) -> None:
        program = bytes(
            [
                0x3E,
                0x00,  # LD A,$00
                0xE0,
                0x40,  # LDH ($FF40),A ; disable LCD so VRAM is CPU-accessible
                0x3E,
                0xE4,  # LD A,$E4
                0xE0,
                0x47,  # LDH ($FF47),A ; identity-ish DMG palette mapping
                0x21,
                0x00,
                0x80,  # LD HL,$8000
                0x36,
                0x80,  # LD (HL),$80 ; tile 0, row 0 low bit for pixel 0
                0x23,  # INC HL
                0x36,
                0x00,  # LD (HL),$00
                0x21,
                0x00,
                0x98,  # LD HL,$9800
                0x36,
                0x00,  # LD (HL),$00 ; BG map points to tile 0
                0x3E,
                0x91,  # LD A,$91
                0xE0,
                0x40,  # LDH ($FF40),A ; LCD on, BG enabled, $8000 tile data
                0x18,
                0xFE,  # JR -2
            ]
        )
        emulator = Emulator(Cartridge(make_rom(program)), serial_sink=lambda _: None)

        emulator.run(max_instructions=200_000, max_frames=1)

        self.assertEqual(emulator.bus.ppu.frame_count, 1)
        self.assertEqual(emulator.bus.ppu.framebuffer[0][0], 1)
        self.assertEqual(emulator.bus.ppu.framebuffer[0][1], 0)

    def test_emulator_buttons_and_save_ram_data_helpers(self) -> None:
        rom = bytearray(make_rom())
        rom[0x0147] = 0x03
        rom[0x0149] = 0x02
        rom[0x014D] = compute_header_checksum(rom)
        emulator = Emulator(Cartridge(bytes(rom)), serial_sink=lambda _: None)

        emulator.set_buttons("a,start")
        emulator.bus.write8(0xFF00, 0x10)
        self.assertEqual(emulator.bus.read8(0xFF00) & 0x0F, 0b0110)

        emulator.cartridge.write_rom_control(0x0000, 0x0A)
        emulator.cartridge.write_ram(0xA000, 0xA5)
        data = emulator.save_ram_data()
        restored = Emulator(Cartridge(bytes(rom)), serial_sink=lambda _: None)
        restored.load_ram_data(data)
        restored.cartridge.write_rom_control(0x0000, 0x0A)
        self.assertEqual(restored.cartridge.read_ram(0xA000), 0xA5)

    def test_emulator_set_buttons_validates_set_input(self) -> None:
        emulator = Emulator(Cartridge(make_rom()), serial_sink=lambda _: None)

        with self.assertRaisesRegex(ValueError, "Unknown button"):
            emulator.set_buttons({"a", "menu"})

    def test_emulator_reset_reinitializes_runtime_and_preserves_ram(self) -> None:
        emulator = Emulator(Cartridge(make_mbc1_ram_rom()), serial_sink=lambda _: None)
        emulator.cartridge.write_rom_control(0x0000, 0x0A)
        emulator.cartridge.write_ram(0xA000, 0x5A)
        emulator.cartridge.write_rom_control(0x2000, 0x02)
        emulator.cpu.a = 0x99
        emulator.bus.write8(0xC000, 0x42)

        emulator.reset()

        self.assertEqual(emulator.cpu.a, 0x01)
        self.assertEqual(emulator.cpu.pc, 0x0100)
        self.assertEqual(emulator.bus.read8(0xC000), 0x00)
        self.assertEqual(emulator.bus.read8(0x4000), 0x01)
        self.assertEqual(emulator.cartridge.read_ram(0xA000), 0xFF)
        emulator.cartridge.write_rom_control(0x0000, 0x0A)
        self.assertEqual(emulator.cartridge.read_ram(0xA000), 0x5A)

    def test_emulator_run_streams_audio_samples(self) -> None:
        emulator = Emulator(Cartridge(make_rom()), serial_sink=lambda _: None)
        emulator.bus.apu.set_sample_rate(CPU_CLOCK_HZ // 4)
        samples: list[tuple[int, int]] = []

        emulator.run(max_instructions=3, audio_sink=samples.extend)

        self.assertEqual(samples, [(120, 120), (120, 120), (120, 120)])
        self.assertEqual(emulator.drain_audio_samples(), [])
        self.assertFalse(emulator.bus.apu.output_enabled)

    def test_emulator_can_start_with_boot_rom_mapping(self) -> None:
        emulator = Emulator(
            Cartridge(make_rom()),
            serial_sink=lambda _: None,
            start_pc=0x0000,
            post_boot=False,
            boot_rom=bytes([0x42]) + bytes([0x00] * 0xFF),
        )

        self.assertEqual(emulator.cpu.pc, 0x0000)
        self.assertEqual(emulator.cpu.a, 0x00)
        self.assertEqual(emulator.bus.read8(0x0000), 0x42)
        self.assertEqual(emulator.bus.read8(0xFF40), 0x00)
        self.assertFalse(emulator.bus.ppu.lcd_enabled)


if __name__ == "__main__":
    unittest.main()
