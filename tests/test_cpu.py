from __future__ import annotations

import unittest

from bus import Bus, SERIAL_INTERNAL_TRANSFER_CYCLES
from cartridge import Cartridge, compute_header_checksum
from cpu import CPU, FLAG_C, FLAG_H, FLAG_N, FLAG_Z


def make_rom(program: bytes) -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0100 : 0x0100 + len(program)] = program
    rom[0x0134 : 0x0134 + len(b"CPUUNIT")] = b"CPUUNIT"
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def make_cpu(program: bytes) -> tuple[CPU, Bus]:
    bus = Bus(Cartridge(make_rom(program)), serial_sink=lambda _: None)
    return CPU(bus), bus


class CPUTests(unittest.TestCase):
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
        cpu, bus = make_cpu(bytes([0x3E, 0x01, 0xE0, 0x4D, 0x10, 0x00, 0x3E, 0x42]))
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
        self.assertEqual(cpu.a, 0x42)

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


if __name__ == "__main__":
    unittest.main()
