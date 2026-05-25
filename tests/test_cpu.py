from __future__ import annotations

import unittest
from unittest.mock import patch

from bus import Bus, EmulationMode, SERIAL_INTERNAL_TRANSFER_CYCLES
from cartridge import Cartridge, compute_header_checksum
from cpu import CPU, FLAG_C, FLAG_H, FLAG_N, FLAG_Z


def make_rom(program: bytes, *, cgb_flag: int = 0x00) -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0100 : 0x0100 + len(program)] = program
    rom[0x0134 : 0x0134 + len(b"CPUUNIT")] = b"CPUUNIT"
    rom[0x0143] = cgb_flag
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_cpu(program: bytes) -> tuple[CPU, Bus]:
    bus = Bus(Cartridge(make_rom(program)), serial_sink=lambda _: None)
    return CPU(bus), bus


def make_cgb_cpu(program: bytes) -> tuple[CPU, Bus]:
    bus = Bus(
        Cartridge(make_rom(program, cgb_flag=0x80)),
        serial_sink=lambda _: None,
        mode=EmulationMode.CGB,
    )
    return CPU(bus), bus


def make_mbc3_rom() -> bytes:
    rom = bytearray([0x00] * 0x100000)
    rom[0x0134 : 0x0134 + len(b"CPUMBC3")] = b"CPUMBC3"
    rom[0x0147] = 0x13
    rom[0x0148] = 0x05
    rom[0x0149] = 0x03
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_mbc3_cpu(start_pc: int) -> tuple[CPU, Bus]:
    bus = Bus(Cartridge(make_mbc3_rom()), serial_sink=lambda _: None)
    return CPU(bus, start_pc=start_pc), bus


POKEMON_SPRITE_OAM_PIECE_LOOP = bytes(
    [
        0xF0,
        0x92,
        0xC6,
        0x10,
        0x86,
        0x12,
        0x23,
        0xF0,
        0x91,
        0xC6,
        0x08,
        0x86,
        0x1C,
        0x12,
        0x1C,
        0x0A,
        0x03,
        0xC5,
        0x47,
        0xFA,
        0xCD,
        0xD5,
        0xCB,
        0x37,
        0xE6,
        0x0F,
        0xFE,
        0x0B,
        0x20,
        0x04,
        0x3E,
        0x7C,
        0x18,
        0x08,
        0xCB,
        0x27,
        0xCB,
        0x27,
        0x4F,
        0xCB,
        0x27,
        0x81,
        0x80,
        0xC1,
        0x12,
        0x23,
        0x1C,
        0x7E,
        0xCB,
        0x4F,
        0x28,
        0x03,
        0xF0,
        0x94,
        0xB6,
        0x23,
        0x12,
        0x1C,
        0xCB,
        0x47,
        0x28,
        0xC2,
    ]
)

POKEMON_OBJECT_POSITION_HELPER = bytes(
    [
        0x1C,
        0x1C,
        0x1A,
        0xE0,
        0x92,
        0x1C,
        0x1C,
        0x1A,
        0xE0,
        0x91,
        0x3E,
        0x04,
        0x83,
        0x5F,
        0xF0,
        0x92,
        0xC6,
        0x04,
        0xE6,
        0xF0,
        0x12,
        0x1C,
        0xF0,
        0x91,
        0xE6,
        0xF0,
        0x12,
        0xC9,
    ]
)

HOT_COPY_LOOP = bytes([0x2A, 0x12, 0x13, 0x0B, 0x79, 0xB0, 0x20, 0xF8, 0xC9])
HOT_DUPLICATE_COPY_LOOP = bytes(
    [0x2A, 0x12, 0x13, 0x12, 0x13, 0x0B, 0x79, 0xB0, 0x20, 0xF6]
)
HOT_FILL_LOOP = bytes([0x7A, 0x22, 0x0B, 0x78, 0xB1, 0x20, 0xF9])


class CPUTests(unittest.TestCase):
    def test_step_skips_trace_formatting_when_trace_disabled(self) -> None:
        cpu, _ = make_cpu(bytes([0x00]))

        with patch("cpu.disassemble") as disassemble_mock, patch.object(
            cpu, "format_registers", wraps=cpu.format_registers
        ) as format_registers_mock:
            cpu.step(trace=False)

        disassemble_mock.assert_not_called()
        format_registers_mock.assert_not_called()
        self.assertIsNone(cpu.last_trace)

    def test_step_collects_trace_when_trace_enabled(self) -> None:
        cpu, _ = make_cpu(bytes([0x00]))

        cpu.step(trace=True)

        self.assertIsNotNone(cpu.last_trace)
        self.assertEqual(cpu.last_trace.pc, 0x0100)
        self.assertEqual(cpu.last_trace.raw, [0x00])
        self.assertEqual(cpu.last_trace.mnemonic, "NOP")

    def test_load_add_and_flags(self) -> None:
        cpu, _ = make_cpu(bytes([0x3E, 0x0F, 0xC6, 0x01, 0xCE, 0xF0]))

        cpu.step()
        self.assertEqual(cpu.a, 0x0F)
        cpu.step()
        self.assertEqual(cpu.a, 0x10)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_H)
        cpu.step()
        self.assertEqual(cpu.a, 0x00)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_Z | FLAG_C)

    def test_cgb_fast_ld_a_hl_reads_selected_wram_bank(self) -> None:
        cpu, bus = make_cgb_cpu(bytes([0x21, 0x02, 0xD0, 0x7E, 0x76]))
        bus.write8(0xFF70, 0x05)
        bus.wram[0x1002] = 0x11
        bus.wram[0x5002] = 0x42

        cpu.run(max_instructions=2)

        self.assertEqual(cpu.a, 0x42)
        self.assertEqual(bus.wram[0x1002], 0x11)

    def test_cgb_fast_ld_hl_a_writes_selected_wram_bank(self) -> None:
        cpu, bus = make_cgb_cpu(bytes([0x21, 0x02, 0xD0, 0x3E, 0x55, 0x77, 0x76]))
        bus.write8(0xFF70, 0x05)
        bus.wram[0x1002] = 0x11
        bus.wram[0x5002] = 0x42

        cpu.run(max_instructions=3)

        self.assertEqual(bus.wram[0x5002], 0x55)
        self.assertEqual(bus.wram[0x1002], 0x11)

    def test_call_ret_and_stack_order(self) -> None:
        cpu, _ = make_cpu(bytes([0xCD, 0x06, 0x01, 0x3E, 0x42, 0x76, 0x3E, 0x99, 0xC9]))

        cpu.step()
        self.assertEqual(cpu.pc, 0x0106)
        self.assertEqual(cpu.sp, 0xFFFC)
        cpu.step()
        self.assertEqual(cpu.a, 0x99)
        cpu.step()
        self.assertEqual(cpu.pc, 0x0103)
        cpu.step()
        self.assertEqual(cpu.a, 0x42)

    def test_cb_prefix_bit_res_set(self) -> None:
        cpu, _ = make_cpu(bytes([0x06, 0x81, 0xCB, 0x40, 0xCB, 0x80, 0xCB, 0xC8]))

        cpu.step()
        self.assertEqual(cpu.b, 0x81)
        cpu.step()
        self.assertFalse(cpu.flag_z)
        self.assertTrue(cpu.flag_h)
        cpu.step()
        self.assertEqual(cpu.b, 0x80)
        cpu.step()
        self.assertEqual(cpu.b, 0x82)

    def test_jr_signed_offset(self) -> None:
        cpu, _ = make_cpu(bytes([0x18, 0x02, 0x3E, 0x01, 0x3E, 0x02, 0x18, 0xFE]))

        cpu.step()
        self.assertEqual(cpu.pc, 0x0104)
        cpu.step()
        self.assertEqual(cpu.a, 0x02)
        cpu.step()
        self.assertEqual(cpu.pc, 0x0106)

    def test_ldh_serial_program(self) -> None:
        program = bytes(
            [
                0x3E,
                ord("O"),
                0xE0,
                0x01,
                0x3E,
                0x81,
                0xE0,
                0x02,
                0x76,
            ]
        )
        out: list[str] = []
        bus = Bus(Cartridge(make_rom(program)), serial_sink=out.append)
        cpu = CPU(bus)

        for _ in range(5):
            cpu.step()

        self.assertEqual(out, [])
        self.assertEqual(bus.read8(0xFF02) & 0x80, 0x80)

        bus.tick(SERIAL_INTERNAL_TRANSFER_CYCLES)

        self.assertEqual(out, ["O"])
        self.assertEqual(bus.serial_text, "O")
        self.assertTrue(cpu.halted)

    def test_ei_enables_after_following_instruction(self) -> None:
        # EI; LD A,$77; NOP. A pending VBlank interrupt should not preempt the
        # instruction immediately following EI.
        cpu, bus = make_cpu(bytes([0xFB, 0x3E, 0x77, 0x00]))
        bus.write8(0xFFFF, 0x01)
        bus.write8(0xFF0F, 0x01)

        cpu.step()
        self.assertFalse(cpu.ime)
        cpu.step()
        self.assertEqual(cpu.a, 0x77)
        self.assertTrue(cpu.ime)
        cpu.step()
        self.assertEqual(cpu.pc, 0x0040)
        self.assertFalse(cpu.ime)
        self.assertEqual(cpu.sp, 0xFFFC)

    def test_add_sp_signed_flags(self) -> None:
        cpu, _ = make_cpu(bytes([0x31, 0xF8, 0xFF, 0xE8, 0x08, 0xF8, 0xF8]))

        cpu.step()
        self.assertEqual(cpu.sp, 0xFFF8)
        cpu.step()
        self.assertEqual(cpu.sp, 0x0000)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_H | FLAG_C)
        cpu.step()
        self.assertEqual(cpu.hl, 0xFFF8)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), 0)

    def test_stop_resumes_for_key1_speed_switch_request(self) -> None:
        cpu, bus = make_cgb_cpu(
            bytes(
                [
                    0x3E,
                    0x01,
                    0xE0,
                    0x4D,
                    0x10,
                    0x00,
                    0x3E,
                    0x01,
                    0xE0,
                    0x4D,
                    0x10,
                    0x00,
                    0x3E,
                    0x42,
                ]
            )
        )
        bus.tick(300)
        self.assertNotEqual(bus.read8(0xFF04), 0x00)

        cpu.step()
        cpu.step()
        self.assertEqual(bus.read8(0xFF4D) & 0x01, 0x01)
        cpu.step()
        self.assertFalse(cpu.stopped)
        self.assertEqual(bus.read8(0xFF4D) & 0x81, 0x80)
        self.assertEqual(bus.read8(0xFF04), 0x00)
        cpu.step()
        cpu.step()
        cpu.step()
        self.assertFalse(cpu.stopped)
        self.assertEqual(bus.read8(0xFF4D) & 0x81, 0x00)
        cpu.step()
        self.assertEqual(cpu.a, 0x42)

    def test_dmg_stop_with_key1_request_preserves_legacy_speed_switch_path(self) -> None:
        cpu, bus = make_cpu(bytes([0x3E, 0x01, 0xE0, 0x4D, 0x10, 0x00, 0x3E, 0x42]))

        cpu.step()
        cpu.step()
        cpu.step()

        self.assertFalse(cpu.stopped)
        self.assertTrue(bus.double_speed)
        self.assertEqual(cpu.pc, 0x0106)
        cpu.step()
        self.assertEqual(cpu.a, 0x42)

    def test_cgb_double_speed_cpu_instructions_advance_devices_at_half_rate(self) -> None:
        cpu, bus = make_cgb_cpu(bytes([0x00, 0x00, 0x00, 0x00]))
        bus.write8(0xFF4D, 0x01)
        self.assertTrue(bus.perform_speed_switch())

        cpu.run(max_instructions=4)

        self.assertEqual(cpu.cycles, 16)
        self.assertEqual(bus.ppu.line_dots, 8)

    def test_stop_waits_until_enabled_interrupt_is_pending(self) -> None:
        cpu, bus = make_cpu(bytes([0x10, 0x00, 0x3E, 0x42]))

        cpu.step()
        self.assertTrue(cpu.stopped)
        self.assertEqual(cpu.pc, 0x0102)

        bus.write8(0xFF0F, 0x10)
        cpu.step()
        self.assertTrue(cpu.stopped)
        self.assertEqual(cpu.a, 0x01)
        self.assertEqual(cpu.pc, 0x0102)

        bus.write8(0xFFFF, 0x10)
        cpu.step()
        self.assertFalse(cpu.stopped)
        self.assertEqual(cpu.a, 0x42)
        self.assertEqual(cpu.pc, 0x0104)

    def test_stop_resets_and_freezes_div_until_wake(self) -> None:
        cpu, bus = make_cpu(bytes([0x10, 0x00, 0x3E, 0x42]))
        bus.tick(300)
        self.assertNotEqual(bus.read8(0xFF04), 0x00)

        cpu.step()
        stopped_cycles = cpu.cycles

        self.assertTrue(cpu.stopped)
        self.assertEqual(bus.read8(0xFF04), 0x00)

        cpu.step()
        bus.tick(255)

        self.assertTrue(cpu.stopped)
        self.assertEqual(cpu.cycles, stopped_cycles)
        self.assertEqual(bus.read8(0xFF04), 0x00)

        bus.write8(0xFFFF, 0x10)
        bus.write8(0xFF0F, 0x10)
        cpu.step()

        self.assertFalse(cpu.stopped)
        self.assertEqual(cpu.a, 0x42)
        self.assertEqual(cpu.pc, 0x0104)

    def test_stop_freezes_tima_until_wake(self) -> None:
        cpu, bus = make_cpu(bytes([0x10, 0x00, 0x3E, 0x42]))
        bus.write8(0xFF04, 0x00)
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF07, 0x05)
        bus.tick(8)

        cpu.step()
        self.assertTrue(cpu.stopped)
        self.assertEqual(bus.read8(0xFF05), 0x00)

        for _ in range(8):
            cpu.step()

        self.assertEqual(bus.read8(0xFF05), 0x00)

        bus.write8(0xFFFF, 0x10)
        bus.write8(0xFF0F, 0x10)
        cpu.step()
        bus.tick(16)

        self.assertFalse(cpu.stopped)
        self.assertEqual(bus.read8(0xFF05), 0x01)

    def test_stop_wakes_for_selected_joypad_line_even_without_ie(self) -> None:
        cpu, bus = make_cpu(bytes([0x10, 0x00, 0x3E, 0x42]))
        bus.write8(0xFF00, 0x10)

        cpu.step()
        self.assertTrue(cpu.stopped)

        bus.joypad.press("a")
        cpu.step()

        self.assertFalse(cpu.stopped)
        self.assertEqual(cpu.a, 0x42)
        self.assertEqual(cpu.pc, 0x0104)
        self.assertEqual(bus.read8(0xFF0F) & 0x10, 0x10)

    def test_stop_wake_services_interrupt_when_ime_set(self) -> None:
        cpu, bus = make_cpu(bytes([0x10, 0x00, 0x3E, 0x42]))

        cpu.step()
        cpu.ime = True
        bus.write8(0xFFFF, 0x10)
        bus.write8(0xFF0F, 0x10)

        cpu.step()

        self.assertFalse(cpu.stopped)
        self.assertFalse(cpu.ime)
        self.assertEqual(cpu.pc, 0x0060)
        self.assertEqual(cpu.sp, 0xFFFC)

    def test_oam_dma_started_by_cpu_begins_after_instruction_cycles(self) -> None:
        cpu, bus = make_cpu(bytes([0x3E, 0xC0, 0xE0, 0x46]))
        bus.write8(0xC000, 0x5A)

        cpu.step()
        cpu.step()

        self.assertTrue(bus.oam_dma_active)
        self.assertEqual(bus.oam[0], 0x00)
        bus.tick(4)
        self.assertEqual(bus.oam[0], 0x5A)

    def test_timer_reload_can_mature_before_later_instruction_write(self) -> None:
        cpu, bus = make_cpu(bytes([0xE0, 0x05]))
        cpu.a = 0x99
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF0F, 0x00)
        bus._tima_reload_delay = 5

        cpu.step()

        self.assertEqual(cpu.cycles, 12)
        self.assertEqual(bus.read8(0xFF05), 0x99)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_call_internal_cycle_allows_timer_reload_before_stack_writes(self) -> None:
        cpu, bus = make_cpu(bytes([0xCD, 0x08, 0x01]))
        cpu.sp = 0xFF07
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF0F, 0x00)
        bus._tima_reload_delay = 17

        cpu.step()

        self.assertEqual(cpu.cycles, 24)
        self.assertEqual(cpu.pc, 0x0108)
        self.assertEqual(cpu.sp, 0xFF05)
        self.assertEqual(bus.read8(0xFF06), 0x01)
        self.assertEqual(bus.read8(0xFF05), 0x03)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_push_internal_cycle_allows_timer_reload_before_stack_writes(self) -> None:
        cpu, bus = make_cpu(bytes([0xC5]))
        cpu.bc = 0x1234
        cpu.sp = 0xFF07
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF0F, 0x00)
        bus._tima_reload_delay = 9

        cpu.step()

        self.assertEqual(cpu.cycles, 16)
        self.assertEqual(cpu.sp, 0xFF05)
        self.assertEqual(bus.read8(0xFF06), 0x12)
        self.assertEqual(bus.read8(0xFF05), 0x34)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_interrupt_entry_idle_cycles_allow_timer_reload_before_stack_writes(self) -> None:
        cpu, bus = make_cpu(bytes([0x00]))
        cpu.ime = True
        cpu.sp = 0xFF07
        bus.write8(0xFFFF, 0x01)
        bus.write8(0xFF0F, 0x01)
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF06, 0x42)
        bus._tima_reload_delay = 9

        cpu.step()

        self.assertEqual(cpu.cycles, 20)
        self.assertEqual(cpu.pc, 0x0040)
        self.assertEqual(cpu.sp, 0xFF05)
        self.assertEqual(bus.read8(0xFF06), 0x01)
        self.assertEqual(bus.read8(0xFF05), 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x01, 0x00)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_taken_conditional_ret_internal_cycle_allows_timer_reload_before_stack_reads(self) -> None:
        cpu, bus = make_cpu(bytes([0xC0]))
        cpu.f = 0x00
        cpu.sp = 0xFF05
        bus.write8(0xFF05, 0x00)
        bus.write8(0xFF06, 0x42)
        bus.write8(0xFF0F, 0x00)
        bus._tima_reload_delay = 5

        cpu.step()

        self.assertEqual(cpu.cycles, 20)
        self.assertEqual(cpu.pc, 0x4242)
        self.assertEqual(cpu.sp, 0xFF07)
        self.assertEqual(bus.read8(0xFF0F) & 0x04, 0x04)

    def test_halt_bug_repeats_next_opcode_when_interrupt_pending_and_ime_clear(self) -> None:
        cpu, bus = make_cpu(bytes([0x76, 0x04, 0x04]))
        bus.write8(0xFFFF, 0x01)
        bus.write8(0xFF0F, 0x01)

        cpu.step()
        self.assertFalse(cpu.halted)
        self.assertEqual(cpu.pc, 0x0101)
        cpu.step()
        self.assertEqual(cpu.b, 0x01)
        self.assertEqual(cpu.pc, 0x0101)
        cpu.step()
        self.assertEqual(cpu.b, 0x02)
        self.assertEqual(cpu.pc, 0x0102)

    def test_halt_without_pending_interrupt_waits_without_advancing_pc(self) -> None:
        cpu, _ = make_cpu(bytes([0x76, 0x04]))

        cpu.step()
        self.assertTrue(cpu.halted)
        self.assertEqual(cpu.pc, 0x0101)
        cpu.step()
        self.assertEqual(cpu.pc, 0x0101)
        self.assertEqual(cpu.b, 0x00)

    def test_run_rejects_negative_instruction_limit(self) -> None:
        cpu, _ = make_cpu(bytes([0x00]))

        with self.assertRaisesRegex(ValueError, "max_instructions"):
            cpu.run(max_instructions=-1)

    def test_run_fast_forwards_contiguous_nops_without_step_callbacks(self) -> None:
        cpu, _ = make_cpu(bytes([0x00] * 8 + [0x3E, 0x42]))

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=8)

        step_mock.assert_not_called()
        self.assertEqual(cpu.pc, 0x0108)
        self.assertEqual(cpu.instructions, 8)
        self.assertEqual(cpu.cycles, 32)

    def test_run_keeps_per_instruction_nops_when_after_step_is_used(self) -> None:
        cpu, _ = make_cpu(bytes([0x00] * 3))
        callbacks: list[int] = []

        cpu.run(max_instructions=3, after_step=lambda: callbacks.append(cpu.pc))

        self.assertEqual(callbacks, [0x0101, 0x0102, 0x0103])
        self.assertEqual(cpu.instructions, 3)
        self.assertEqual(cpu.cycles, 12)

    def test_run_fast_forwards_dec_de_delay_loop(self) -> None:
        cpu, bus = make_cpu(bytes([0x11, 0x03, 0x00, 0x1B, 0x7A, 0xB3, 0x20, 0xFB]))
        cpu.step()

        with (
            patch.object(bus, "cycles_until_next_interrupt_event", return_value=4096),
            patch.object(cpu, "step", wraps=cpu.step) as step_mock,
        ):
            cpu.run(max_instructions=12)

        step_mock.assert_not_called()
        self.assertEqual(cpu.de, 0)
        self.assertEqual(cpu.a, 0)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_Z)
        self.assertEqual(cpu.pc, 0x0108)
        self.assertEqual(cpu.instructions, 13)
        self.assertEqual(cpu.cycles, 92)

    def test_run_fast_forwards_partial_dec_bc_delay_loop_at_instruction_limit(self) -> None:
        cpu, _ = make_cpu(bytes([0x0B, 0x78, 0xB1, 0x20, 0xFB]))
        cpu.bc = 3

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=4)

        step_mock.assert_not_called()
        self.assertEqual(cpu.bc, 2)
        self.assertEqual(cpu.a, 2)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), 0)
        self.assertEqual(cpu.pc, 0x0100)
        self.assertEqual(cpu.instructions, 4)
        self.assertEqual(cpu.cycles, 28)

    def test_run_fast_forwards_nop_padded_dec_de_delay_loop(self) -> None:
        cpu, _ = make_cpu(bytes([0x00, 0x00, 0x00, 0x1B, 0x7A, 0xB3, 0x20, 0xF8]))
        cpu.de = 2

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=14)

        step_mock.assert_not_called()
        self.assertEqual(cpu.de, 0)
        self.assertEqual(cpu.a, 0)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_Z)
        self.assertEqual(cpu.pc, 0x0108)
        self.assertEqual(cpu.instructions, 14)
        self.assertEqual(cpu.cycles, 76)

    def test_run_fast_forwards_dec_a_delay_loop(self) -> None:
        cpu, _ = make_cpu(bytes([0x3D, 0x20, 0xFD, 0xC9]))
        cpu.a = 3
        cpu.f = FLAG_C

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=6)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 0)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_Z | FLAG_N | FLAG_C)
        self.assertEqual(cpu.pc, 0x0103)
        self.assertEqual(cpu.instructions, 6)
        self.assertEqual(cpu.cycles, 44)

    def test_run_fast_forwards_partial_dec_a_delay_loop_at_instruction_limit(self) -> None:
        cpu, _ = make_cpu(bytes([0x3D, 0x20, 0xFD]))
        cpu.a = 0x10
        cpu.f = 0

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=2)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 0x0F)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_N | FLAG_H)
        self.assertEqual(cpu.pc, 0x0100)
        self.assertEqual(cpu.instructions, 2)
        self.assertEqual(cpu.cycles, 16)

    def test_run_fast_forwards_ly_wait_until_equal_loop(self) -> None:
        cpu, bus = make_cpu(bytes([0xF0, 0x44, 0xBD, 0x20, 0xFB]))
        cpu.l = 11
        bus.ppu._scanline = 10
        bus.ppu.line_dots = 0
        bus.io[0x44] = 10

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=51)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 11)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_Z | FLAG_N)
        self.assertEqual(cpu.pc, 0x0105)
        self.assertEqual(cpu.instructions, 51)
        self.assertEqual(cpu.cycles, 472)

    def test_run_fast_forwards_ly_wait_while_equal_loop(self) -> None:
        cpu, bus = make_cpu(bytes([0xF0, 0x44, 0xBC, 0x28, 0xFB]))
        cpu.h = 10
        bus.ppu._scanline = 10
        bus.ppu.line_dots = 0
        bus.io[0x44] = 10

        with patch.object(cpu, "step", wraps=cpu.step) as step_mock:
            cpu.run(max_instructions=51)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 11)
        self.assertEqual(cpu.f & (FLAG_Z | FLAG_N | FLAG_H | FLAG_C), FLAG_N)
        self.assertEqual(cpu.pc, 0x0105)
        self.assertEqual(cpu.instructions, 51)
        self.assertEqual(cpu.cycles, 472)

    def test_direct_fast_cgb_vram_read_write_respects_selected_bank(self) -> None:
        cpu, bus = make_cgb_cpu(b"\x00")
        bus.write8(0xFF4F, 0x01)
        bus.vram[0x1800] = 0x11
        bus.vram[0x2000 + 0x1800] = 0x22

        self.assertEqual(cpu._read8_direct_fast(0x9800, stable_cycles=8), 0x22)
        self.assertTrue(cpu._write8_direct_fast(0x9800, 0x33, stable_cycles=8))

        self.assertEqual(bus.vram[0x1800], 0x11)
        self.assertEqual(bus.vram[0x2000 + 0x1800], 0x33)

    def test_run_fast_forwards_pokemon_bank_restore_return(self) -> None:
        cpu, bus = make_mbc3_cpu(0x3E8D)
        rom = bytearray(bus.cartridge.data)
        rom[0x3E8D : 0x3E94] = bytes([0xF1, 0xE0, 0xB8, 0xEA, 0x00, 0x20, 0xC9])
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.sp = 0xD000
        bus.write8(0xD000, FLAG_C)
        bus.write8(0xD001, 0x03)
        bus.write8(0xD002, 0x34)
        bus.write8(0xD003, 0x12)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=4)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 0x03)
        self.assertEqual(cpu.f, FLAG_C)
        self.assertEqual(cpu.pc, 0x1234)
        self.assertEqual(cpu.sp, 0xD004)
        self.assertEqual(bus.hram[0xFFB8 - 0xFF80], 0x03)
        self.assertEqual(bus.cartridge.mbc3_rom_bank, 0x03)
        self.assertEqual(cpu.instructions, 4)
        self.assertEqual(cpu.cycles, 56)

    def test_run_fast_forwards_pokemon_text_predef_return(self) -> None:
        cpu, bus = make_mbc3_cpu(0x5A5F)
        rom = bytearray(bus.cartridge.data)
        rom[0x5A5F : 0x5A6D] = bytes(
            [0xFA, 0x2B, 0xD1, 0xFE, 0x02, 0x28, 0x0F, 0xFE, 0x03, 0x28, 0x0B, 0xFE, 0x05, 0xC0]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        bus.mapper.write_rom_control(0x2000, 0x01)
        bus.write8(0xD12B, 0x00)
        cpu.sp = 0xD000
        bus.write8(0xD000, 0x78)
        bus.write8(0xD001, 0x56)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=7)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 0x00)
        self.assertEqual(cpu.f, FLAG_N | FLAG_H | FLAG_C)
        self.assertEqual(cpu.pc, 0x5678)
        self.assertEqual(cpu.sp, 0xD002)
        self.assertEqual(cpu.instructions, 7)
        self.assertEqual(cpu.cycles, 76)

    def test_run_fast_forwards_pokemon_joypad_status_return(self) -> None:
        cpu, bus = make_mbc3_cpu(0x4000)
        rom = bytearray(bus.cartridge.data)
        rom[0xC000 : 0xC027] = bytes(
            [
                0xF0,
                0xF8,
                0xFE,
                0x0F,
                0xCA,
                0x3C,
                0x40,
                0x47,
                0xF0,
                0xB1,
                0x5F,
                0xA8,
                0x57,
                0xA3,
                0xE0,
                0xB2,
                0x7A,
                0xA0,
                0xE0,
                0xB3,
                0x78,
                0xE0,
                0xB1,
                0xFA,
                0x30,
                0xD7,
                0xCB,
                0x6F,
                0x20,
                0x16,
                0xF0,
                0xB1,
                0xE0,
                0xB4,
                0xFA,
                0x6B,
                0xCD,
                0xA7,
                0xC8,
            ]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        bus.mapper.write_rom_control(0x2000, 0x03)
        bus.hram[0xFFF8 - 0xFF80] = 0x0A
        bus.hram[0xFFB1 - 0xFF80] = 0x0C
        bus.write8(0xD730, 0x00)
        bus.write8(0xCD6B, 0x00)
        cpu.sp = 0xD000
        bus.write8(0xD000, 0x78)
        bus.write8(0xD001, 0x56)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=23)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFFB1 - 0xFF80], 0x0A)
        self.assertEqual(bus.hram[0xFFB2 - 0xFF80], 0x08)
        self.assertEqual(bus.hram[0xFFB3 - 0xFF80], 0x02)
        self.assertEqual(bus.hram[0xFFB4 - 0xFF80], 0x0A)
        self.assertEqual(cpu.a, 0x00)
        self.assertEqual(cpu.b, 0x0A)
        self.assertEqual(cpu.d, 0x06)
        self.assertEqual(cpu.e, 0x0C)
        self.assertEqual(cpu.f, FLAG_Z | FLAG_H)
        self.assertEqual(cpu.pc, 0x5678)
        self.assertEqual(cpu.sp, 0xD002)
        self.assertEqual(cpu.instructions, 23)
        self.assertEqual(cpu.cycles, 208)

    def test_run_fast_forwards_pokemon_joypad_status_call(self) -> None:
        cpu, bus = make_mbc3_cpu(0x019A)
        rom = bytearray(bus.cartridge.data)
        rom[0x019A : 0x01AE] = bytes(
            [
                0xF0,
                0xB8,
                0xF5,
                0x3E,
                0x03,
                0xE0,
                0xB8,
                0xEA,
                0x00,
                0x20,
                0xCD,
                0x00,
                0x40,
                0xF1,
                0xE0,
                0xB8,
                0xEA,
                0x00,
                0x20,
                0xC9,
            ]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        bus.hram[0xFFB8 - 0xFF80] = 0x02
        bus.hram[0xFFF8 - 0xFF80] = 0x0A
        bus.hram[0xFFB1 - 0xFF80] = 0x0C
        bus.write8(0xD730, 0x00)
        bus.write8(0xCD6B, 0x00)
        cpu.f = FLAG_C
        cpu.sp = 0xD000
        bus.write8(0xD000, 0x78)
        bus.write8(0xD001, 0x56)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=33)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFFB1 - 0xFF80], 0x0A)
        self.assertEqual(bus.hram[0xFFB2 - 0xFF80], 0x08)
        self.assertEqual(bus.hram[0xFFB3 - 0xFF80], 0x02)
        self.assertEqual(bus.hram[0xFFB4 - 0xFF80], 0x0A)
        self.assertEqual(bus.hram[0xFFB8 - 0xFF80], 0x02)
        self.assertEqual(bus.cartridge.mbc3_rom_bank, 0x02)
        self.assertEqual(cpu.a, 0x02)
        self.assertEqual(cpu.f, FLAG_C)
        self.assertEqual(cpu.b, 0x0A)
        self.assertEqual(cpu.d, 0x06)
        self.assertEqual(cpu.e, 0x0C)
        self.assertEqual(cpu.pc, 0x5678)
        self.assertEqual(cpu.sp, 0xD002)
        self.assertEqual(cpu.instructions, 33)
        self.assertEqual(cpu.cycles, 352)

    def test_run_fast_forwards_pokemon_joypad_poll_loop(self) -> None:
        cpu, bus = make_mbc3_cpu(0x38F6)
        rom = bytearray(bus.cartridge.data)
        rom[0x38F6 : 0x390F] = bytes(
            [
                0xCD,
                0x9A,
                0x01,
                0xF0,
                0xB4,
                0xCB,
                0x47,
                0x28,
                0x02,
                0x18,
                0x04,
                0xCB,
                0x4F,
                0x28,
                0x05,
                0xCD,
                0xAF,
                0x20,
                0x18,
                0x05,
                0xF0,
                0xD5,
                0xA7,
                0x20,
                0xE7,
            ]
        )
        rom[0x019A : 0x01AE] = bytes(
            [
                0xF0,
                0xB8,
                0xF5,
                0x3E,
                0x03,
                0xE0,
                0xB8,
                0xEA,
                0x00,
                0x20,
                0xCD,
                0x00,
                0x40,
                0xF1,
                0xE0,
                0xB8,
                0xEA,
                0x00,
                0x20,
                0xC9,
            ]
        )
        rom[0xC000 : 0xC027] = bytes(
            [
                0xF0,
                0xF8,
                0xFE,
                0x0F,
                0xCA,
                0x3C,
                0x40,
                0x47,
                0xF0,
                0xB1,
                0x5F,
                0xA8,
                0x57,
                0xA3,
                0xE0,
                0xB2,
                0x7A,
                0xA0,
                0xE0,
                0xB3,
                0x78,
                0xE0,
                0xB1,
                0xFA,
                0x30,
                0xD7,
                0xCB,
                0x6F,
                0x20,
                0x16,
                0xF0,
                0xB1,
                0xE0,
                0xB4,
                0xFA,
                0x6B,
                0xCD,
                0xA7,
                0xC8,
            ]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.c = 0xEF
        cpu.h = 0x12
        cpu.l = 0x34
        cpu.sp = 0xD010
        bus.hram[0xFFB1 - 0xFF80] = 0x08
        bus.hram[0xFFB8 - 0xFF80] = 0x22
        bus.hram[0xFFD5 - 0xFF80] = 0x03
        bus.hram[0xFFF8 - 0xFF80] = 0x00
        bus.write8(0xD730, 0x00)
        bus.write8(0xCD6B, 0x00)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=84)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFFB1 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB2 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB3 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB4 - 0xFF80], 0x00)
        self.assertEqual(bus.cartridge.mbc3_rom_bank, 0x22)
        self.assertEqual(cpu.a, 0x03)
        self.assertEqual(cpu.f, FLAG_H)
        self.assertEqual(cpu.b, 0x00)
        self.assertEqual(cpu.c, 0xEF)
        self.assertEqual(cpu.d, 0x00)
        self.assertEqual(cpu.e, 0x00)
        self.assertEqual(cpu.h, 0x12)
        self.assertEqual(cpu.l, 0x34)
        self.assertEqual(cpu.sp, 0xD010)
        self.assertEqual(cpu.pc, 0x38F6)
        self.assertEqual(cpu.instructions, 84)
        self.assertEqual(cpu.cycles, 912)

    def test_run_fast_forwards_pokemon_wram_flag_wait_loop(self) -> None:
        cpu, bus = make_mbc3_cpu(0x374F)
        rom = bytearray(bus.cartridge.data)
        rom[0x374F : 0x375B] = bytes(
            [0x21, 0x2A, 0xC0, 0xAF, 0xB6, 0x23, 0xB6, 0x23, 0x23, 0xB6, 0x20, 0xF4]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        bus.write8(0xC02A, 0x01)
        bus.write8(0xC02B, 0x02)
        bus.write8(0xC02D, 0x04)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=18)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 0x07)
        self.assertEqual(cpu.f, 0x00)
        self.assertEqual(cpu.h, 0xC0)
        self.assertEqual(cpu.l, 0x2D)
        self.assertEqual(cpu.pc, 0x374F)
        self.assertEqual(cpu.instructions, 18)
        self.assertEqual(cpu.cycles, 152)

    def test_run_fast_forwards_pokemon_text_delay_return(self) -> None:
        cpu, bus = make_mbc3_cpu(0x3C04)
        rom = bytearray(bus.cartridge.data)
        rom[0x3C04 : 0x3C0B] = bytes([0x7E, 0x47, 0x3E, 0xEE, 0xB8, 0x20, 0x18])
        rom[0x3C23 : 0x3C2B] = bytes([0xF0, 0x8B, 0xA7, 0xC8, 0x3D, 0xE0, 0x8B, 0xC0])
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.hl = 0xC4F2
        bus.write8(0xC4F2, 0x7F)
        bus.hram[0xFF8B - 0xFF80] = 0x10
        cpu.sp = 0xD000
        bus.write8(0xD000, 0x78)
        bus.write8(0xD001, 0x56)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=11)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFF8B - 0xFF80], 0x0F)
        self.assertEqual(cpu.a, 0x0F)
        self.assertEqual(cpu.b, 0x7F)
        self.assertEqual(cpu.f, FLAG_N | FLAG_H)
        self.assertEqual(cpu.pc, 0x5678)
        self.assertEqual(cpu.sp, 0xD002)
        self.assertEqual(cpu.instructions, 11)
        self.assertEqual(cpu.cycles, 96)

    def test_run_fast_forwards_pokemon_text_wait_loop(self) -> None:
        cpu, bus = make_mbc3_cpu(0x3872)
        rom = bytearray(bus.cartridge.data)
        rom[0x3872 : 0x3891] = bytes(
            [
                0xE5,
                0xFA,
                0x9B,
                0xD0,
                0xA7,
                0x28,
                0x03,
                0xCD,
                0xC6,
                0x56,
                0x21,
                0xF2,
                0xC4,
                0xCD,
                0x04,
                0x3C,
                0xE1,
                0xCD,
                0x31,
                0x38,
                0x3E,
                0x2D,
                0xCD,
                0x6D,
                0x3E,
                0xF0,
                0xB5,
                0xE6,
                0x03,
                0x28,
                0xE1,
            ]
        )
        rom[0x3831 : 0x3839] = bytes([0xCD, 0x9A, 0x01, 0xF0, 0xB7, 0xA7, 0xF0, 0xB3])
        rom[0x3849 : 0x3852] = bytes([0xF0, 0xD5, 0xA7, 0x28, 0x04, 0xAF, 0xE0, 0xB5, 0xC9])
        rom[0x3C04 : 0x3C0B] = bytes([0x7E, 0x47, 0x3E, 0xEE, 0xB8, 0x20, 0x18])
        rom[0x3C23 : 0x3C2B] = bytes([0xF0, 0x8B, 0xA7, 0xC8, 0x3D, 0xE0, 0x8B, 0xC0])
        rom[0x3E6D : 0x3E75] = bytes([0xEA, 0x4E, 0xCC, 0xF0, 0xB8, 0xEA, 0x12, 0xCF])
        table_offset = 0x13 * 0x4000 + (0x7E79 - 0x4000) + 0x2D * 3
        rom[table_offset : table_offset + 3] = bytes([0x01, 0x5F, 0x5A])
        rom[0x5A5F : 0x5A6D] = bytes(
            [0xFA, 0x2B, 0xD1, 0xFE, 0x02, 0x28, 0x0F, 0xFE, 0x03, 0x28, 0x0B, 0xFE, 0x05, 0xC0]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.h = 0x12
        cpu.l = 0x34
        cpu.c = 0xEF
        cpu.sp = 0xD010
        bus.write8(0xC4F2, 0x7F)
        bus.write8(0xD09B, 0x00)
        bus.write8(0xD12B, 0x00)
        bus.write8(0xD730, 0x00)
        bus.write8(0xCD6B, 0x00)
        bus.hram[0xFF8B - 0xFF80] = 0x10
        bus.hram[0xFFF8 - 0xFF80] = 0x00
        bus.hram[0xFFB1 - 0xFF80] = 0x00
        bus.hram[0xFFB7 - 0xFF80] = 0x00
        bus.hram[0xFFB8 - 0xFF80] = 0x22
        bus.hram[0xFFD5 - 0xFF80] = 0x05

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=132)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFF8B - 0xFF80], 0x0F)
        self.assertEqual(bus.hram[0xFFB1 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB2 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB3 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB4 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB5 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB8 - 0xFF80], 0x22)
        self.assertEqual(bus.cartridge.mbc3_rom_bank, 0x22)
        self.assertEqual(bus.wram[0xCC4E - 0xC000], 0x2D)
        self.assertEqual(bus.wram[0xCC4F - 0xC000], 0x12)
        self.assertEqual(bus.wram[0xCC50 - 0xC000], 0x34)
        self.assertEqual(bus.wram[0xCC51 - 0xC000], 0x00)
        self.assertEqual(bus.wram[0xCC52 - 0xC000], 0x00)
        self.assertEqual(bus.wram[0xCC53 - 0xC000], 0x00)
        self.assertEqual(bus.wram[0xCC54 - 0xC000], 0xEF)
        self.assertEqual(bus.wram[0xCF12 - 0xC000], 0x22)
        self.assertEqual(bus.wram[0xD0B7 - 0xC000], 0x01)
        self.assertEqual(cpu.a, 0x00)
        self.assertEqual(cpu.f, FLAG_Z | FLAG_H)
        self.assertEqual(cpu.b, 0x00)
        self.assertEqual(cpu.c, 0xEF)
        self.assertEqual(cpu.d, 0x3E)
        self.assertEqual(cpu.e, 0x8D)
        self.assertEqual(cpu.h, 0x5A)
        self.assertEqual(cpu.l, 0x5F)
        self.assertEqual(cpu.pc, 0x3872)
        self.assertEqual(cpu.sp, 0xD010)
        self.assertEqual(cpu.instructions, 132)
        self.assertEqual(cpu.cycles, 1420)

    def test_run_fast_forwards_pokemon_alternate_text_wait_loop(self) -> None:
        cpu, bus = make_mbc3_cpu(0x3AD9)
        rom = bytearray(bus.cartridge.data)
        rom[0x3AD9 : 0x3B01] = bytes(
            [
                0xE5,
                0xFA,
                0x9B,
                0xD0,
                0xA7,
                0x28,
                0x08,
                0x06,
                0x1C,
                0x21,
                0xFF,
                0x56,
                0xCD,
                0xD6,
                0x35,
                0xE1,
                0xCD,
                0x31,
                0x38,
                0xF0,
                0xB5,
                0xA7,
                0x20,
                0x1B,
                0xE5,
                0x21,
                0x8E,
                0xC4,
                0xCD,
                0x04,
                0x3C,
                0xE1,
                0xFA,
                0x34,
                0xCC,
                0x3D,
                0x28,
                0x02,
                0x18,
                0xD8,
            ]
        )
        rom[0x3831 : 0x3839] = bytes([0xCD, 0x9A, 0x01, 0xF0, 0xB7, 0xA7, 0xF0, 0xB3])
        rom[0x3849 : 0x3852] = bytes([0xF0, 0xD5, 0xA7, 0x28, 0x04, 0xAF, 0xE0, 0xB5, 0xC9])
        rom[0x019A : 0x01AE] = bytes(
            [
                0xF0,
                0xB8,
                0xF5,
                0x3E,
                0x03,
                0xE0,
                0xB8,
                0xEA,
                0x00,
                0x20,
                0xCD,
                0x00,
                0x40,
                0xF1,
                0xE0,
                0xB8,
                0xEA,
                0x00,
                0x20,
                0xC9,
            ]
        )
        rom[3 * 0x4000 : 3 * 0x4000 + 39] = bytes(
            [
                0xF0,
                0xF8,
                0xFE,
                0x0F,
                0xCA,
                0x3C,
                0x40,
                0x47,
                0xF0,
                0xB1,
                0x5F,
                0xA8,
                0x57,
                0xA3,
                0xE0,
                0xB2,
                0x7A,
                0xA0,
                0xE0,
                0xB3,
                0x78,
                0xE0,
                0xB1,
                0xFA,
                0x30,
                0xD7,
                0xCB,
                0x6F,
                0x20,
                0x16,
                0xF0,
                0xB1,
                0xE0,
                0xB4,
                0xFA,
                0x6B,
                0xCD,
                0xA7,
                0xC8,
            ]
        )
        rom[0x3C04 : 0x3C0B] = bytes([0x7E, 0x47, 0x3E, 0xEE, 0xB8, 0x20, 0x18])
        rom[0x3C23 : 0x3C2B] = bytes([0xF0, 0x8B, 0xA7, 0xC8, 0x3D, 0xE0, 0x8B, 0xC0])
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.h = 0xC3
        cpu.l = 0xC9
        cpu.c = 0x56
        cpu.sp = 0xD010
        bus.write8(0xD00E, 0x12)
        bus.write8(0xD00F, 0x34)
        bus.write8(0xD09B, 0x00)
        bus.write8(0xC48E, 0x7F)
        bus.write8(0xCC34, 0x00)
        bus.write8(0xD730, 0x00)
        bus.write8(0xCD6B, 0x00)
        bus.hram[0xFF8B - 0xFF80] = 0x00
        bus.hram[0xFF8C - 0xFF80] = 0x06
        bus.hram[0xFFF8 - 0xFF80] = 0x00
        bus.hram[0xFFB1 - 0xFF80] = 0x04
        bus.hram[0xFFB5 - 0xFF80] = 0x99
        bus.hram[0xFFB7 - 0xFF80] = 0x00
        bus.hram[0xFFB8 - 0xFF80] = 0x22
        bus.hram[0xFFD5 - 0xFF80] = 0x05

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=146)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFF8B - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFF8C - 0xFF80], 0x06)
        self.assertEqual(bus.hram[0xFFB1 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB2 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB3 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB4 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB5 - 0xFF80], 0x00)
        self.assertEqual(bus.hram[0xFFB8 - 0xFF80], 0x22)
        self.assertEqual(bus.cartridge.mbc3_rom_bank, 0x22)
        self.assertEqual(bus.read8(0xD00E), 0xC9)
        self.assertEqual(bus.read8(0xD00F), 0xC3)
        self.assertEqual(cpu.a, 0xFF)
        self.assertEqual(cpu.f, FLAG_N | FLAG_H)
        self.assertEqual(cpu.b, 0x7F)
        self.assertEqual(cpu.c, 0x56)
        self.assertEqual(cpu.d, 0x00)
        self.assertEqual(cpu.e, 0x00)
        self.assertEqual(cpu.h, 0xC3)
        self.assertEqual(cpu.l, 0xC9)
        self.assertEqual(cpu.pc, 0x3AD9)
        self.assertEqual(cpu.sp, 0xD010)
        self.assertEqual(cpu.instructions, 146)
        self.assertEqual(cpu.cycles, 1592)

    def _run_hot_copy_loop_scenario(
        self,
        *,
        fast: bool,
        duplicate: bool,
    ) -> tuple[object, ...]:
        pc = 0x1837 if duplicate else 0x00B5
        loop = HOT_DUPLICATE_COPY_LOOP if duplicate else HOT_COPY_LOOP
        cpu, bus = make_mbc3_cpu(pc)
        rom = bytearray(bus.cartridge.data)
        rom[pc : pc + len(loop)] = loop
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        bus.io[0x40] = 0x00

        source = 0xC200
        destination = 0xC300
        count = 5
        for index, value in enumerate((0x10, 0x11, 0x12, 0x13)):
            bus.write8(source + index, value)
        for index in range(8):
            bus.write8(destination + index, 0xAA)
        cpu.hl = source
        cpu.de = destination
        cpu.bc = count

        instructions_per_iteration = 9 if duplicate else 7
        max_instructions = (count - 1) * instructions_per_iteration
        if fast:
            with patch.object(
                cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast
            ) as step_mock:
                cpu.run(max_instructions=max_instructions)
            step_mock.assert_not_called()
        else:
            cpu.run(max_instructions=max_instructions, after_step=lambda: None)

        destination_length = (count - 1) * (2 if duplicate else 1)
        return (
            cpu.a,
            cpu.f,
            cpu.b,
            cpu.c,
            cpu.d,
            cpu.e,
            cpu.h,
            cpu.l,
            cpu.pc,
            cpu.sp,
            cpu.instructions,
            cpu.cycles,
            tuple(bus.read8(destination + index) for index in range(destination_length)),
        )

    def test_run_fast_forwards_hot_copy_loop_shadow_oam_destination_matches_exact(
        self,
    ) -> None:
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                fast = self._run_hot_copy_loop_scenario(
                    fast=True,
                    duplicate=duplicate,
                )
                exact = self._run_hot_copy_loop_scenario(
                    fast=False,
                    duplicate=duplicate,
                )

                self.assertEqual(fast, exact)

    def _run_hot_fill_loop_scenario(self, *, fast: bool) -> tuple[object, ...]:
        cpu, bus = make_mbc3_cpu(0x36E2)
        rom = bytearray(bus.cartridge.data)
        rom[0x36E2 : 0x36E2 + len(HOT_FILL_LOOP)] = HOT_FILL_LOOP
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        bus.io[0x40] = 0x00

        destination = 0xC320
        count = 5
        for index in range(count - 1):
            bus.write8(destination + index, 0xAA)
        cpu.hl = destination
        cpu.bc = count
        cpu.d = 0x34

        max_instructions = (count - 1) * 6
        if fast:
            with patch.object(
                cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast
            ) as step_mock:
                cpu.run(max_instructions=max_instructions)
            step_mock.assert_not_called()
        else:
            cpu.run(max_instructions=max_instructions, after_step=lambda: None)

        return (
            cpu.a,
            cpu.f,
            cpu.b,
            cpu.c,
            cpu.d,
            cpu.e,
            cpu.h,
            cpu.l,
            cpu.pc,
            cpu.sp,
            cpu.instructions,
            cpu.cycles,
            tuple(bus.read8(destination + index) for index in range(count - 1)),
        )

    def test_run_fast_forwards_hot_fill_loop_shadow_oam_destination_matches_exact(
        self,
    ) -> None:
        fast = self._run_hot_fill_loop_scenario(fast=True)
        exact = self._run_hot_fill_loop_scenario(fast=False)

        self.assertEqual(fast, exact)

    def _run_sprite_oam_piece_loop_scenario(
        self,
        *,
        fast: bool,
        sprite_id: int,
        piece_bytes: bytes,
        tile_offsets: bytes,
        max_instructions: int,
        de_low: int,
        target_length: int,
    ) -> tuple[object, ...]:
        cpu, bus = make_mbc3_cpu(0x4B6C)
        rom = bytearray(bus.cartridge.data)
        rom[0x4B6C : 0x4BAA] = POKEMON_SPRITE_OAM_PIECE_LOOP
        rom[0x4098 : 0x4098 + len(piece_bytes)] = piece_bytes
        rom[0x4080 : 0x4080 + len(tile_offsets)] = tile_offsets
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.b = 0x40
        cpu.c = 0x80
        cpu.d = 0xC3
        cpu.e = de_low
        cpu.h = 0x40
        cpu.l = 0x98
        cpu.sp = 0xDFF1
        bus.write8(0xD5CD, sprite_id)
        bus.hram[0xFF91 - 0xFF80] = 0x10
        bus.hram[0xFF92 - 0xFF80] = 0x20
        bus.hram[0xFF94 - 0xFF80] = 0x80
        for index in range(target_length):
            bus.write8(0xC300 + de_low + index, 0xAA)

        if fast:
            with patch.object(
                cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast
            ) as step_mock:
                cpu.run(max_instructions=max_instructions)
            step_mock.assert_not_called()
        else:
            cpu.run(max_instructions=max_instructions, after_step=lambda: None)

        return (
            cpu.a,
            cpu.f,
            cpu.b,
            cpu.c,
            cpu.d,
            cpu.e,
            cpu.h,
            cpu.l,
            cpu.pc,
            cpu.sp,
            cpu.instructions,
            cpu.cycles,
            tuple(bus.read8(0xC300 + de_low + index) for index in range(target_length)),
            tuple(bus.read8(0xDFEF + index) for index in range(2)),
        )

    def test_run_fast_forwards_pokemon_sprite_oam_piece_loop_matches_exact_normal_branch(
        self,
    ) -> None:
        fast = self._run_sprite_oam_piece_loop_scenario(
            fast=True,
            sprite_id=0xA0,
            piece_bytes=bytes([0x00, 0x00, 0x00, 0x01, 0x02, 0x03]),
            tile_offsets=bytes([0x00, 0x05]),
            max_instructions=78,
            de_low=0x20,
            target_length=8,
        )
        exact = self._run_sprite_oam_piece_loop_scenario(
            fast=False,
            sprite_id=0xA0,
            piece_bytes=bytes([0x00, 0x00, 0x00, 0x01, 0x02, 0x03]),
            tile_offsets=bytes([0x00, 0x05]),
            max_instructions=78,
            de_low=0x20,
            target_length=8,
        )

        self.assertEqual(fast, exact)

    def test_run_fast_forwards_pokemon_sprite_oam_piece_loop_matches_exact_b_branch(
        self,
    ) -> None:
        piece_bytes = bytes(
            [0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x08, 0x00, 0x02, 0x08, 0x08, 0x03]
        )
        tile_offsets = bytes([0x00, 0x01, 0x02, 0x03])
        fast = self._run_sprite_oam_piece_loop_scenario(
            fast=True,
            sprite_id=0xB0,
            piece_bytes=piece_bytes,
            tile_offsets=tile_offsets,
            max_instructions=144,
            de_low=0x50,
            target_length=16,
        )
        exact = self._run_sprite_oam_piece_loop_scenario(
            fast=False,
            sprite_id=0xB0,
            piece_bytes=piece_bytes,
            tile_offsets=tile_offsets,
            max_instructions=144,
            de_low=0x50,
            target_length=16,
        )

        self.assertEqual(fast, exact)

    def test_run_fast_forwards_pokemon_sprite_oam_piece_loop(self) -> None:
        cpu, bus = make_mbc3_cpu(0x4B6C)
        rom = bytearray(bus.cartridge.data)
        rom[0x4B6C : 0x4BAA] = bytes(
            [
                0xF0,
                0x92,
                0xC6,
                0x10,
                0x86,
                0x12,
                0x23,
                0xF0,
                0x91,
                0xC6,
                0x08,
                0x86,
                0x1C,
                0x12,
                0x1C,
                0x0A,
                0x03,
                0xC5,
                0x47,
                0xFA,
                0xCD,
                0xD5,
                0xCB,
                0x37,
                0xE6,
                0x0F,
                0xFE,
                0x0B,
                0x20,
                0x04,
                0x3E,
                0x7C,
                0x18,
                0x08,
                0xCB,
                0x27,
                0xCB,
                0x27,
                0x4F,
                0xCB,
                0x27,
                0x81,
                0x80,
                0xC1,
                0x12,
                0x23,
                0x1C,
                0x7E,
                0xCB,
                0x4F,
                0x28,
                0x03,
                0xF0,
                0x94,
                0xB6,
                0x23,
                0x12,
                0x1C,
                0xCB,
                0x47,
                0x28,
                0xC2,
            ]
        )
        rom[0x4098 : 0x409E] = bytes([0x00, 0x00, 0x00, 0x01, 0x02, 0x03])
        rom[0x4080 : 0x4082] = bytes([0x00, 0x05])
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.b = 0x40
        cpu.c = 0x80
        cpu.d = 0xC3
        cpu.e = 0x20
        cpu.h = 0x40
        cpu.l = 0x98
        cpu.sp = 0xDFF1
        bus.write8(0xD5CD, 0xA0)
        bus.hram[0xFF91 - 0xFF80] = 0x50
        bus.hram[0xFF92 - 0xFF80] = 0x20
        bus.hram[0xFF94 - 0xFF80] = 0x80
        bus.write8(0xC320, 0xAA)
        bus.write8(0xC321, 0xAA)
        bus.write8(0xC322, 0xAA)
        bus.write8(0xC323, 0xAA)
        bus.write8(0xC324, 0xAA)
        bus.write8(0xC325, 0xAA)
        bus.write8(0xC326, 0xAA)
        bus.write8(0xC327, 0xAA)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=78)

        step_mock.assert_not_called()
        self.assertEqual([bus.read8(0xC320 + index) for index in range(8)], [
            0x30,
            0x58,
            0x78,
            0x00,
            0x31,
            0x5A,
            0x7D,
            0x83,
        ])
        self.assertEqual(bus.read8(0xDFEF), 0x82)
        self.assertEqual(bus.read8(0xDFF0), 0x40)
        self.assertEqual(cpu.a, 0x83)
        self.assertEqual(cpu.f, FLAG_H)
        self.assertEqual(cpu.b, 0x40)
        self.assertEqual(cpu.c, 0x82)
        self.assertEqual(cpu.d, 0xC3)
        self.assertEqual(cpu.e, 0x28)
        self.assertEqual(cpu.h, 0x40)
        self.assertEqual(cpu.l, 0x9E)
        self.assertEqual(cpu.pc, 0x4BAA)
        self.assertEqual(cpu.sp, 0xDFF1)
        self.assertEqual(cpu.instructions, 78)
        self.assertEqual(cpu.cycles, 636)

    def test_run_fast_forwards_pokemon_sprite_oam_piece_loop_b_branch_adds_offset(self) -> None:
        cpu, bus = make_mbc3_cpu(0x4B6C)
        rom = bytearray(bus.cartridge.data)
        rom[0x4B6C : 0x4BAA] = bytes(
            [
                0xF0,
                0x92,
                0xC6,
                0x10,
                0x86,
                0x12,
                0x23,
                0xF0,
                0x91,
                0xC6,
                0x08,
                0x86,
                0x1C,
                0x12,
                0x1C,
                0x0A,
                0x03,
                0xC5,
                0x47,
                0xFA,
                0xCD,
                0xD5,
                0xCB,
                0x37,
                0xE6,
                0x0F,
                0xFE,
                0x0B,
                0x20,
                0x04,
                0x3E,
                0x7C,
                0x18,
                0x08,
                0xCB,
                0x27,
                0xCB,
                0x27,
                0x4F,
                0xCB,
                0x27,
                0x81,
                0x80,
                0xC1,
                0x12,
                0x23,
                0x1C,
                0x7E,
                0xCB,
                0x4F,
                0x28,
                0x03,
                0xF0,
                0x94,
                0xB6,
                0x23,
                0x12,
                0x1C,
                0xCB,
                0x47,
                0x28,
                0xC2,
            ]
        )
        rom[0x4098 : 0x40A4] = bytes(
            [0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x08, 0x00, 0x02, 0x08, 0x08, 0x03]
        )
        rom[0x4080 : 0x4084] = bytes([0x00, 0x01, 0x02, 0x03])
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.b = 0x40
        cpu.c = 0x80
        cpu.d = 0xC3
        cpu.e = 0x50
        cpu.h = 0x40
        cpu.l = 0x98
        cpu.sp = 0xDFF1
        bus.write8(0xD5CD, 0xB0)
        bus.hram[0xFF91 - 0xFF80] = 0x10
        bus.hram[0xFF92 - 0xFF80] = 0x20
        bus.hram[0xFF94 - 0xFF80] = 0x80

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=144)

        step_mock.assert_not_called()
        self.assertEqual([bus.read8(0xC350 + index) for index in range(16)], [
            0x30,
            0x18,
            0x7C,
            0x00,
            0x30,
            0x20,
            0x7D,
            0x00,
            0x38,
            0x18,
            0x7E,
            0x82,
            0x38,
            0x20,
            0x7F,
            0x83,
        ])
        self.assertEqual(bus.read8(0xDFEF), 0x84)
        self.assertEqual(bus.read8(0xDFF0), 0x40)
        self.assertEqual(cpu.a, 0x83)
        self.assertEqual(cpu.f, FLAG_H)
        self.assertEqual(cpu.b, 0x40)
        self.assertEqual(cpu.c, 0x84)
        self.assertEqual(cpu.d, 0xC3)
        self.assertEqual(cpu.e, 0x60)
        self.assertEqual(cpu.h, 0x40)
        self.assertEqual(cpu.l, 0xA4)
        self.assertEqual(cpu.pc, 0x4BAA)
        self.assertEqual(cpu.sp, 0xDFF1)
        self.assertEqual(cpu.instructions, 144)
        self.assertEqual(cpu.cycles, 1212)

    def _run_object_position_helper_scenario(
        self,
        *,
        fast: bool,
        y_value: int,
        x_value: int,
    ) -> tuple[object, ...]:
        cpu, bus = make_mbc3_cpu(0x4BD1)
        bus.mapper.write_rom_control(0x2000, 0x01)
        rom = bytearray(bus.cartridge.data)
        rom[0x4BD1 : 0x4BED] = POKEMON_OBJECT_POSITION_HELPER
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.d = 0xC1
        cpu.e = 0x20
        cpu.sp = 0xDFF0
        bus.write8(0xDFF0, 0x67)
        bus.write8(0xDFF1, 0x45)
        bus.write8(0xC122, y_value)
        bus.write8(0xC124, x_value)

        if fast:
            with patch.object(
                cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast
            ) as step_mock:
                cpu.run(max_instructions=20)
            step_mock.assert_not_called()
        else:
            cpu.run(max_instructions=20, after_step=lambda: None)

        return (
            cpu.a,
            cpu.f,
            cpu.d,
            cpu.e,
            cpu.pc,
            cpu.sp,
            cpu.instructions,
            cpu.cycles,
            bus.hram[0xFF92 - 0xFF80],
            bus.hram[0xFF91 - 0xFF80],
            tuple(bus.read8(0xC120 + index) for index in range(10)),
        )

    def test_run_fast_forwards_pokemon_object_position_helper_matches_exact(
        self,
    ) -> None:
        fast = self._run_object_position_helper_scenario(
            fast=True,
            y_value=0x23,
            x_value=0x57,
        )
        exact = self._run_object_position_helper_scenario(
            fast=False,
            y_value=0x23,
            x_value=0x57,
        )

        self.assertEqual(fast, exact)

    def test_run_fast_forwards_pokemon_object_position_helper(self) -> None:
        cpu, bus = make_mbc3_cpu(0x4BD1)
        bus.mapper.write_rom_control(0x2000, 0x01)
        rom = bytearray(bus.cartridge.data)
        rom[0x4BD1 : 0x4BED] = bytes(
            [
                0x1C,
                0x1C,
                0x1A,
                0xE0,
                0x92,
                0x1C,
                0x1C,
                0x1A,
                0xE0,
                0x91,
                0x3E,
                0x04,
                0x83,
                0x5F,
                0xF0,
                0x92,
                0xC6,
                0x04,
                0xE6,
                0xF0,
                0x12,
                0x1C,
                0xF0,
                0x91,
                0xE6,
                0xF0,
                0x12,
                0xC9,
            ]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.d = 0xC1
        cpu.e = 0x20
        cpu.sp = 0xDFF0
        bus.write8(0xDFF0, 0x67)
        bus.write8(0xDFF1, 0x45)
        bus.write8(0xC122, 0x23)
        bus.write8(0xC124, 0x57)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=20)

        step_mock.assert_not_called()
        self.assertEqual(bus.hram[0xFF92 - 0xFF80], 0x23)
        self.assertEqual(bus.hram[0xFF91 - 0xFF80], 0x57)
        self.assertEqual(bus.read8(0xC128), 0x20)
        self.assertEqual(bus.read8(0xC129), 0x50)
        self.assertEqual(cpu.a, 0x50)
        self.assertEqual(cpu.f, FLAG_H)
        self.assertEqual(cpu.d, 0xC1)
        self.assertEqual(cpu.e, 0x29)
        self.assertEqual(cpu.pc, 0x4567)
        self.assertEqual(cpu.sp, 0xDFF2)
        self.assertEqual(cpu.instructions, 20)
        self.assertEqual(cpu.cycles, 156)

    def test_run_fast_forwards_pokemon_bank3_table_scan(self) -> None:
        cpu, bus = make_mbc3_cpu(0x71AE)
        bus.mapper.write_rom_control(0x2000, 0x03)
        rom = bytearray(bus.cartridge.data)
        offset = 3 * 0x4000 + (0x71AE - 0x4000)
        rom[offset : offset + 9] = bytes(
            [0x2A, 0xFE, 0xFF, 0x28, 0x11, 0xB8, 0x2A, 0x20, 0xF7]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.b = 0x30
        cpu.h = 0xD5
        cpu.l = 0xCE
        bus.write8(0xD5CE, 0x10)
        bus.write8(0xD5CF, 0xAA)
        bus.write8(0xD5D0, 0x20)
        bus.write8(0xD5D1, 0xBB)
        bus.write8(0xD5D2, 0x30)
        bus.write8(0xD5D3, 0xCC)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=18)

        step_mock.assert_not_called()
        self.assertEqual(cpu.a, 0xCC)
        self.assertEqual(cpu.f, FLAG_Z | FLAG_N)
        self.assertEqual(cpu.b, 0x30)
        self.assertEqual(cpu.h, 0xD5)
        self.assertEqual(cpu.l, 0xD4)
        self.assertEqual(cpu.pc, 0x71B7)
        self.assertEqual(cpu.instructions, 18)
        self.assertEqual(cpu.cycles, 140)

    def test_run_fast_forwards_pokemon_bank3_bit_test_helper(self) -> None:
        cpu, bus = make_mbc3_cpu(0x71E6)
        bus.mapper.write_rom_control(0x2000, 0x03)
        rom = bytearray(bus.cartridge.data)
        offset = 3 * 0x4000 + (0x71E6 - 0x4000)
        rom[offset : offset + 63] = bytes(
            [
                0xE5,
                0xD5,
                0xC5,
                0x79,
                0x57,
                0xE6,
                0x07,
                0x5F,
                0x7A,
                0xCB,
                0x3F,
                0xCB,
                0x3F,
                0xCB,
                0x3F,
                0x85,
                0x6F,
                0x30,
                0x01,
                0x24,
                0x1C,
                0x16,
                0x01,
                0x1D,
                0x28,
                0x04,
                0xCB,
                0x22,
                0x18,
                0xF9,
                0x78,
                0xA7,
                0x28,
                0x0B,
                0xFE,
                0x02,
                0x28,
                0x10,
                0x7E,
                0x47,
                0x7A,
                0xB0,
                0x77,
                0x18,
                0x0D,
                0x7E,
                0x47,
                0x7A,
                0xEE,
                0xFF,
                0xA0,
                0x77,
                0x18,
                0x04,
                0x7E,
                0x47,
                0x7A,
                0xA0,
                0xC1,
                0xD1,
                0xE1,
                0x4F,
                0xC9,
            ]
        )
        bus.cartridge.data = bytes(rom)
        cpu._fast_rom_data = bus.cartridge.data
        cpu._fast_rom_data_len = len(bus.cartridge.data)
        cpu.b = 0x02
        cpu.c = 0x03
        cpu.d = 0x44
        cpu.e = 0x55
        cpu.h = 0xD5
        cpu.l = 0xA6
        cpu.sp = 0xDFF0
        bus.write8(0xDFF0, 0x67)
        bus.write8(0xDFF1, 0x45)
        bus.write8(0xD5A6, 0x08)

        with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
            cpu.run(max_instructions=44)

        step_mock.assert_not_called()
        self.assertEqual([bus.read8(0xDFEA + index) for index in range(6)], [
            0x03,
            0x02,
            0x55,
            0x44,
            0xA6,
            0xD5,
        ])
        self.assertEqual(bus.read8(0xD5A6), 0x08)
        self.assertEqual(cpu.a, 0x08)
        self.assertEqual(cpu.f, FLAG_H)
        self.assertEqual(cpu.b, 0x02)
        self.assertEqual(cpu.c, 0x08)
        self.assertEqual(cpu.d, 0x44)
        self.assertEqual(cpu.e, 0x55)
        self.assertEqual(cpu.h, 0xD5)
        self.assertEqual(cpu.l, 0xA6)
        self.assertEqual(cpu.pc, 0x4567)
        self.assertEqual(cpu.sp, 0xDFF2)
        self.assertEqual(cpu.instructions, 44)
        self.assertEqual(cpu.cycles, 352)

    def test_run_fast_forwards_pokemon_bank3_bit_write_helper(self) -> None:
        cases = [
            (0x00, 0x05, 0xFF, 0xDF, FLAG_H, 53, 428),
            (0x01, 0x05, 0x00, 0x20, 0x00, 54, 432),
        ]
        for operation, bit, start_value, expected_value, expected_flags, instructions, cycles in cases:
            with self.subTest(operation=operation):
                cpu, bus = make_mbc3_cpu(0x71E6)
                bus.mapper.write_rom_control(0x2000, 0x03)
                rom = bytearray(bus.cartridge.data)
                offset = 3 * 0x4000 + (0x71E6 - 0x4000)
                rom[offset : offset + 63] = bytes(
                    [
                        0xE5,
                        0xD5,
                        0xC5,
                        0x79,
                        0x57,
                        0xE6,
                        0x07,
                        0x5F,
                        0x7A,
                        0xCB,
                        0x3F,
                        0xCB,
                        0x3F,
                        0xCB,
                        0x3F,
                        0x85,
                        0x6F,
                        0x30,
                        0x01,
                        0x24,
                        0x1C,
                        0x16,
                        0x01,
                        0x1D,
                        0x28,
                        0x04,
                        0xCB,
                        0x22,
                        0x18,
                        0xF9,
                        0x78,
                        0xA7,
                        0x28,
                        0x0B,
                        0xFE,
                        0x02,
                        0x28,
                        0x10,
                        0x7E,
                        0x47,
                        0x7A,
                        0xB0,
                        0x77,
                        0x18,
                        0x0D,
                        0x7E,
                        0x47,
                        0x7A,
                        0xEE,
                        0xFF,
                        0xA0,
                        0x77,
                        0x18,
                        0x04,
                        0x7E,
                        0x47,
                        0x7A,
                        0xA0,
                        0xC1,
                        0xD1,
                        0xE1,
                        0x4F,
                        0xC9,
                    ]
                )
                bus.cartridge.data = bytes(rom)
                cpu._fast_rom_data = bus.cartridge.data
                cpu._fast_rom_data_len = len(bus.cartridge.data)
                cpu.b = operation
                cpu.c = bit
                cpu.d = 0x44
                cpu.e = 0x55
                cpu.h = 0xD5
                cpu.l = 0xA6
                cpu.sp = 0xDFF0
                bus.write8(0xDFF0, 0x34)
                bus.write8(0xDFF1, 0x12)
                bus.write8(0xD5A6, start_value)

                with patch.object(cpu, "_step_prefetched_fast", wraps=cpu._step_prefetched_fast) as step_mock:
                    cpu.run(max_instructions=instructions)

                step_mock.assert_not_called()
                self.assertEqual(bus.read8(0xD5A6), expected_value)
                self.assertEqual(cpu.a, expected_value)
                self.assertEqual(cpu.f, expected_flags)
                self.assertEqual(cpu.b, operation)
                self.assertEqual(cpu.c, expected_value)
                self.assertEqual(cpu.pc, 0x1234)
                self.assertEqual(cpu.sp, 0xDFF2)
                self.assertEqual(cpu.instructions, instructions)
                self.assertEqual(cpu.cycles, cycles)

    def test_run_bulk_halt_preserves_instruction_limit_cycles(self) -> None:
        cpu, _ = make_cpu(bytes([0x76, 0x00]))

        cpu.run(max_instructions=10)

        self.assertTrue(cpu.halted)
        self.assertEqual(cpu.pc, 0x0101)
        self.assertEqual(cpu.cycles, 40)

    def test_run_bulk_halt_invokes_after_step_after_idle_batch(self) -> None:
        cpu, _ = make_cpu(bytes([0x76, 0x00]))
        callbacks: list[int] = []

        cpu.run(max_instructions=10, after_step=lambda: callbacks.append(cpu.cycles))

        self.assertTrue(cpu.halted)
        self.assertEqual(cpu.pc, 0x0101)
        self.assertEqual(cpu.cycles, 40)
        self.assertEqual(callbacks, [4, 40])


if __name__ == "__main__":
    unittest.main()
