from __future__ import annotations

from dataclasses import dataclass

from bus import Bus
from opcodes import INVALID_OPCODES, disassemble


FLAG_Z = 0x80
FLAG_N = 0x40
FLAG_H = 0x20
FLAG_C = 0x10


class IllegalInstruction(Exception):
    pass


@dataclass
class CPUTrace:
    pc: int
    raw: list[int]
    mnemonic: str
    registers: str
    cycles: int

    def format(self) -> str:
        raw_text = " ".join(f"{byte:02X}" for byte in self.raw).ljust(8)
        return f"{self.pc:04X}: {raw_text} {self.mnemonic:<20} {self.registers} CY:{self.cycles}"


class CPU:
    def __init__(self, bus: Bus, start_pc: int = 0x0100, post_boot: bool = True) -> None:
        self.bus = bus
        self.a = 0x01 if post_boot else 0
        self.f = 0xB0 if post_boot else 0
        self.b = 0x00
        self.c = 0x13 if post_boot else 0
        self.d = 0x00
        self.e = 0xD8 if post_boot else 0
        self.h = 0x01 if post_boot else 0
        self.l = 0x4D if post_boot else 0
        self.sp = 0xFFFE if post_boot else 0
        self.pc = start_pc & 0xFFFF
        self.ime = False
        self._ime_delay = 0
        self.halted = False
        self.stopped = False
        self._halt_bug = False
        self.cycles = 0
        self.instructions = 0
        self.last_trace: CPUTrace | None = None
        self._instruction_timing_active = False
        self._instruction_cycles_used = 0

    @property
    def af(self) -> int:
        return (self.a << 8) | (self.f & 0xF0)

    @af.setter
    def af(self, value: int) -> None:
        self.a = (value >> 8) & 0xFF
        self.f = value & 0xF0

    @property
    def bc(self) -> int:
        return (self.b << 8) | self.c

    @bc.setter
    def bc(self, value: int) -> None:
        self.b = (value >> 8) & 0xFF
        self.c = value & 0xFF

    @property
    def de(self) -> int:
        return (self.d << 8) | self.e

    @de.setter
    def de(self, value: int) -> None:
        self.d = (value >> 8) & 0xFF
        self.e = value & 0xFF

    @property
    def hl(self) -> int:
        return (self.h << 8) | self.l

    @hl.setter
    def hl(self, value: int) -> None:
        self.h = (value >> 8) & 0xFF
        self.l = value & 0xFF

    def step(self, trace: bool = False) -> int:
        self.last_trace = None
        if self.stopped:
            if self.bus.stop_wake_requested() or self._pending_interrupts():
                self.stopped = False
                self.bus.exit_stop()
            else:
                return 0

        self._begin_instruction_timing()
        interrupt_cycles = self._service_interrupt_if_needed()
        if interrupt_cycles:
            self._pad_instruction_cycles(interrupt_cycles)
            self._end_instruction_timing()
            self._finish_instruction()
            return interrupt_cycles
        self._end_instruction_timing()

        if self.halted:
            if self._pending_interrupts():
                self.halted = False
            else:
                self._add_cycles(4)
                return 4

        pc_before = self.pc
        raw, mnemonic = disassemble(self.bus, pc_before)
        registers_before = self.format_registers()
        self._begin_instruction_timing()
        opcode = self._fetch8()
        cycles = self._execute_opcode(opcode)
        self.instructions += 1
        self._pad_instruction_cycles(cycles)
        self._end_instruction_timing()
        self._finish_instruction()
        if trace:
            self.last_trace = CPUTrace(pc_before, raw, mnemonic, registers_before, cycles)
        return cycles

    def run(
        self,
        max_instructions: int | None = None,
        trace: bool = False,
        trace_sink=None,
        step_mode: bool = False,
        stop_condition=None,
        after_step=None,
    ) -> None:
        if max_instructions is not None and max_instructions < 0:
            raise ValueError("max_instructions must be non-negative")
        steps = 0
        while max_instructions is None or steps < max_instructions:
            if stop_condition is not None and stop_condition():
                break
            self.step(trace=trace)
            steps += 1
            if after_step is not None:
                after_step()
            if trace and self.last_trace is not None:
                line = self.last_trace.format()
                if trace_sink is None:
                    print(line)
                else:
                    trace_sink(line)
            if step_mode:
                response = input("step> ").strip().lower()
                if response in {"q", "quit", "exit"}:
                    break

    def format_registers(self) -> str:
        return (
            f"A:{self.a:02X} F:{self.f:02X} B:{self.b:02X} C:{self.c:02X} "
            f"D:{self.d:02X} E:{self.e:02X} H:{self.h:02X} L:{self.l:02X} "
            f"SP:{self.sp:04X} PC:{self.pc:04X} "
            f"Z:{int(self.flag_z)} N:{int(self.flag_n)} H:{int(self.flag_h)} C:{int(self.flag_c)}"
        )

    @property
    def flag_z(self) -> bool:
        return bool(self.f & FLAG_Z)

    @flag_z.setter
    def flag_z(self, value: bool) -> None:
        self._set_flag(FLAG_Z, value)

    @property
    def flag_n(self) -> bool:
        return bool(self.f & FLAG_N)

    @flag_n.setter
    def flag_n(self, value: bool) -> None:
        self._set_flag(FLAG_N, value)

    @property
    def flag_h(self) -> bool:
        return bool(self.f & FLAG_H)

    @flag_h.setter
    def flag_h(self, value: bool) -> None:
        self._set_flag(FLAG_H, value)

    @property
    def flag_c(self) -> bool:
        return bool(self.f & FLAG_C)

    @flag_c.setter
    def flag_c(self, value: bool) -> None:
        self._set_flag(FLAG_C, value)

    def _execute_opcode(self, opcode: int) -> int:
        if opcode in INVALID_OPCODES:
            raise IllegalInstruction(f"Illegal opcode ${opcode:02X} at ${self.pc - 1:04X}")

        if opcode == 0x00:
            return 4
        if opcode == 0x08:
            self._write16(self._fetch16(), self.sp)
            return 20
        if opcode == 0x10:
            self._consume_stop_padding()
            if self.bus.perform_speed_switch():
                self.stopped = False
            else:
                self.bus.enter_stop()
                self.stopped = True
            return 4
        if opcode == 0x18:
            self._jr(True)
            return 12
        if opcode == 0x27:
            self._daa()
            return 4
        if opcode == 0x2F:
            self.a ^= 0xFF
            self.flag_n = True
            self.flag_h = True
            return 4
        if opcode == 0x37:
            self.flag_n = False
            self.flag_h = False
            self.flag_c = True
            return 4
        if opcode == 0x3F:
            self.flag_n = False
            self.flag_h = False
            self.flag_c = not self.flag_c
            return 4
        if opcode == 0x76:
            if not self.ime and self._pending_interrupts():
                self._halt_bug = True
            else:
                self.halted = True
            return 4
        if opcode == 0xC3:
            address = self._fetch16()
            self._internal_cycle()
            self.pc = address
            return 16
        if opcode == 0xC9:
            self.pc = self._pop16()
            self._internal_cycle()
            return 16
        if opcode == 0xCB:
            return self._execute_cb(self._fetch8())
        if opcode == 0xCD:
            address = self._fetch16()
            self._internal_cycle()
            self._push16(self.pc)
            self.pc = address
            return 24
        if opcode == 0xD9:
            self.pc = self._pop16()
            self.ime = True
            self._ime_delay = 0
            self._internal_cycle()
            return 16
        if opcode == 0xE0:
            self._write8(0xFF00 + self._fetch8(), self.a)
            return 12
        if opcode == 0xE2:
            self._write8(0xFF00 + self.c, self.a)
            return 8
        if opcode == 0xE8:
            offset = self._fetch8()
            signed = _signed8(offset)
            self._set_sp_add_flags(self.sp, offset)
            self._internal_cycle(8)
            self.sp = (self.sp + signed) & 0xFFFF
            return 16
        if opcode == 0xE9:
            self.pc = self.hl
            return 4
        if opcode == 0xEA:
            self._write8(self._fetch16(), self.a)
            return 16
        if opcode == 0xF0:
            self.a = self._read8(0xFF00 + self._fetch8())
            return 12
        if opcode == 0xF2:
            self.a = self._read8(0xFF00 + self.c)
            return 8
        if opcode == 0xF3:
            self.ime = False
            self._ime_delay = 0
            return 4
        if opcode == 0xF8:
            offset = self._fetch8()
            signed = _signed8(offset)
            self._set_sp_add_flags(self.sp, offset)
            self._internal_cycle()
            self.hl = (self.sp + signed) & 0xFFFF
            return 12
        if opcode == 0xF9:
            self._internal_cycle()
            self.sp = self.hl
            return 8
        if opcode == 0xFA:
            self.a = self._read8(self._fetch16())
            return 16
        if opcode == 0xFB:
            self._ime_delay = 2
            return 4

        if opcode in {0x07, 0x0F, 0x17, 0x1F}:
            self._rotate_accumulator(opcode)
            return 4

        if opcode in {0x20, 0x28, 0x30, 0x38}:
            condition = self._condition((opcode - 0x20) // 8)
            self._jr(condition)
            return 12 if condition else 8

        if opcode & 0xCF == 0x01:
            self._set_r16((opcode >> 4) & 0x03, self._fetch16())
            return 12
        if opcode in {0x02, 0x12, 0x22, 0x32}:
            self._ld_indirect_a(opcode)
            return 8
        if opcode in {0x0A, 0x1A, 0x2A, 0x3A}:
            self._ld_a_indirect(opcode)
            return 8
        if opcode & 0xCF == 0x03:
            index = (opcode >> 4) & 0x03
            self._internal_cycle()
            self._set_r16(index, (self._get_r16(index) + 1) & 0xFFFF)
            return 8
        if opcode & 0xCF == 0x09:
            self._internal_cycle()
            self._add_hl(self._get_r16((opcode >> 4) & 0x03))
            return 8
        if opcode & 0xCF == 0x0B:
            index = (opcode >> 4) & 0x03
            self._internal_cycle()
            self._set_r16(index, (self._get_r16(index) - 1) & 0xFFFF)
            return 8
        if opcode & 0xC7 == 0x04:
            index = (opcode >> 3) & 0x07
            self._set_r8(index, self._inc8(self._get_r8(index)))
            return 12 if index == 6 else 4
        if opcode & 0xC7 == 0x05:
            index = (opcode >> 3) & 0x07
            self._set_r8(index, self._dec8(self._get_r8(index)))
            return 12 if index == 6 else 4
        if opcode & 0xC7 == 0x06:
            index = (opcode >> 3) & 0x07
            self._set_r8(index, self._fetch8())
            return 12 if index == 6 else 8
        if 0x40 <= opcode <= 0x7F:
            dst = (opcode >> 3) & 0x07
            src = opcode & 0x07
            self._set_r8(dst, self._get_r8(src))
            return 8 if dst == 6 or src == 6 else 4
        if 0x80 <= opcode <= 0xBF:
            src = opcode & 0x07
            self._alu((opcode >> 3) & 0x07, self._get_r8(src))
            return 8 if src == 6 else 4

        if opcode in {0xC0, 0xC8, 0xD0, 0xD8}:
            condition = self._condition((opcode - 0xC0) // 8)
            self._internal_cycle()
            if condition:
                self.pc = self._pop16()
                self._internal_cycle()
                return 20
            return 8
        if opcode in {0xC2, 0xCA, 0xD2, 0xDA}:
            address = self._fetch16()
            condition = self._condition((opcode - 0xC2) // 8)
            if condition:
                self._internal_cycle()
                self.pc = address
                return 16
            return 12
        if opcode in {0xC4, 0xCC, 0xD4, 0xDC}:
            address = self._fetch16()
            condition = self._condition((opcode - 0xC4) // 8)
            if condition:
                self._internal_cycle()
                self._push16(self.pc)
                self.pc = address
                return 24
            return 12
        if opcode & 0xC7 == 0xC1:
            self._set_stack_r16((opcode >> 4) & 0x03, self._pop16())
            return 12
        if opcode & 0xC7 == 0xC5:
            self._internal_cycle()
            self._push16(self._get_stack_r16((opcode >> 4) & 0x03))
            return 16
        if opcode & 0xC7 == 0xC7:
            self._internal_cycle()
            self._push16(self.pc)
            self.pc = opcode & 0x38
            return 16
        if opcode in {0xC6, 0xCE, 0xD6, 0xDE, 0xE6, 0xEE, 0xF6, 0xFE}:
            operation = {0xC6: 0, 0xCE: 1, 0xD6: 2, 0xDE: 3, 0xE6: 4, 0xEE: 5, 0xF6: 6, 0xFE: 7}[opcode]
            self._alu(operation, self._fetch8())
            return 8

        raise NotImplementedError(f"Opcode ${opcode:02X} at ${self.pc - 1:04X} was not decoded")

    def _execute_cb(self, opcode: int) -> int:
        index = opcode & 0x07
        value = self._get_r8(index)
        if opcode < 0x40:
            operation = (opcode >> 3) & 0x07
            result = self._cb_rotate_shift(operation, value)
            self._set_r8(index, result)
            return 16 if index == 6 else 8

        bit = (opcode >> 3) & 0x07
        if opcode < 0x80:
            self.flag_z = (value & (1 << bit)) == 0
            self.flag_n = False
            self.flag_h = True
            return 12 if index == 6 else 8
        if opcode < 0xC0:
            self._set_r8(index, value & ~(1 << bit))
            return 16 if index == 6 else 8
        self._set_r8(index, value | (1 << bit))
        return 16 if index == 6 else 8

    def _cb_rotate_shift(self, operation: int, value: int) -> int:
        if operation == 0:
            carry = (value >> 7) & 1
            result = ((value << 1) | carry) & 0xFF
        elif operation == 1:
            carry = value & 1
            result = ((carry << 7) | (value >> 1)) & 0xFF
        elif operation == 2:
            carry = (value >> 7) & 1
            result = ((value << 1) | int(self.flag_c)) & 0xFF
        elif operation == 3:
            carry = value & 1
            result = ((int(self.flag_c) << 7) | (value >> 1)) & 0xFF
        elif operation == 4:
            carry = (value >> 7) & 1
            result = (value << 1) & 0xFF
        elif operation == 5:
            carry = value & 1
            result = (value & 0x80) | (value >> 1)
        elif operation == 6:
            carry = 0
            result = ((value & 0x0F) << 4) | ((value & 0xF0) >> 4)
        elif operation == 7:
            carry = value & 1
            result = value >> 1
        else:
            raise AssertionError(operation)
        self.flag_z = result == 0
        self.flag_n = False
        self.flag_h = False
        self.flag_c = bool(carry)
        return result

    def _rotate_accumulator(self, opcode: int) -> None:
        old_carry = int(self.flag_c)
        if opcode == 0x07:
            carry = (self.a >> 7) & 1
            self.a = ((self.a << 1) | carry) & 0xFF
        elif opcode == 0x0F:
            carry = self.a & 1
            self.a = ((carry << 7) | (self.a >> 1)) & 0xFF
        elif opcode == 0x17:
            carry = (self.a >> 7) & 1
            self.a = ((self.a << 1) | old_carry) & 0xFF
        elif opcode == 0x1F:
            carry = self.a & 1
            self.a = ((old_carry << 7) | (self.a >> 1)) & 0xFF
        else:
            raise AssertionError(opcode)
        self.flag_z = False
        self.flag_n = False
        self.flag_h = False
        self.flag_c = bool(carry)

    def _alu(self, operation: int, value: int) -> None:
        value &= 0xFF
        if operation == 0:
            self._add_a(value, 0)
        elif operation == 1:
            self._add_a(value, int(self.flag_c))
        elif operation == 2:
            self._sub_a(value, 0)
        elif operation == 3:
            self._sub_a(value, int(self.flag_c))
        elif operation == 4:
            self.a &= value
            self.flag_z = self.a == 0
            self.flag_n = False
            self.flag_h = True
            self.flag_c = False
        elif operation == 5:
            self.a ^= value
            self.flag_z = self.a == 0
            self.flag_n = False
            self.flag_h = False
            self.flag_c = False
        elif operation == 6:
            self.a |= value
            self.flag_z = self.a == 0
            self.flag_n = False
            self.flag_h = False
            self.flag_c = False
        elif operation == 7:
            self._sub_a(value, 0, store=False)
        else:
            raise AssertionError(operation)

    def _add_a(self, value: int, carry: int) -> None:
        result = self.a + value + carry
        self.flag_z = (result & 0xFF) == 0
        self.flag_n = False
        self.flag_h = ((self.a & 0x0F) + (value & 0x0F) + carry) > 0x0F
        self.flag_c = result > 0xFF
        self.a = result & 0xFF

    def _sub_a(self, value: int, carry: int, store: bool = True) -> None:
        result = self.a - value - carry
        self.flag_z = (result & 0xFF) == 0
        self.flag_n = True
        self.flag_h = (self.a & 0x0F) < ((value & 0x0F) + carry)
        self.flag_c = self.a < value + carry
        if store:
            self.a = result & 0xFF

    def _inc8(self, value: int) -> int:
        result = (value + 1) & 0xFF
        self.flag_z = result == 0
        self.flag_n = False
        self.flag_h = (value & 0x0F) == 0x0F
        return result

    def _dec8(self, value: int) -> int:
        result = (value - 1) & 0xFF
        self.flag_z = result == 0
        self.flag_n = True
        self.flag_h = (value & 0x0F) == 0x00
        return result

    def _add_hl(self, value: int) -> None:
        result = self.hl + value
        self.flag_n = False
        self.flag_h = ((self.hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF
        self.flag_c = result > 0xFFFF
        self.hl = result & 0xFFFF

    def _set_sp_add_flags(self, base: int, offset: int) -> None:
        self.flag_z = False
        self.flag_n = False
        self.flag_h = ((base & 0x0F) + (offset & 0x0F)) > 0x0F
        self.flag_c = ((base & 0xFF) + (offset & 0xFF)) > 0xFF

    def _daa(self) -> None:
        adjust = 0
        carry = self.flag_c
        if not self.flag_n:
            if self.flag_h or (self.a & 0x0F) > 9:
                adjust |= 0x06
            if self.flag_c or self.a > 0x99:
                adjust |= 0x60
                carry = True
            self.a = (self.a + adjust) & 0xFF
        else:
            if self.flag_h:
                adjust |= 0x06
            if self.flag_c:
                adjust |= 0x60
            self.a = (self.a - adjust) & 0xFF
        self.flag_z = self.a == 0
        self.flag_h = False
        self.flag_c = carry

    def _ld_indirect_a(self, opcode: int) -> None:
        if opcode == 0x02:
            self._write8(self.bc, self.a)
        elif opcode == 0x12:
            self._write8(self.de, self.a)
        elif opcode == 0x22:
            self._write8(self.hl, self.a)
            self.hl = (self.hl + 1) & 0xFFFF
        elif opcode == 0x32:
            self._write8(self.hl, self.a)
            self.hl = (self.hl - 1) & 0xFFFF

    def _ld_a_indirect(self, opcode: int) -> None:
        if opcode == 0x0A:
            self.a = self._read8(self.bc)
        elif opcode == 0x1A:
            self.a = self._read8(self.de)
        elif opcode == 0x2A:
            self.a = self._read8(self.hl)
            self.hl = (self.hl + 1) & 0xFFFF
        elif opcode == 0x3A:
            self.a = self._read8(self.hl)
            self.hl = (self.hl - 1) & 0xFFFF

    def _jr(self, condition: bool) -> None:
        offset = self._fetch8()
        if condition:
            self._internal_cycle()
            self.pc = (self.pc + _signed8(offset)) & 0xFFFF

    def _condition(self, index: int) -> bool:
        if index == 0:
            return not self.flag_z
        if index == 1:
            return self.flag_z
        if index == 2:
            return not self.flag_c
        if index == 3:
            return self.flag_c
        raise AssertionError(index)

    def _get_r8(self, index: int) -> int:
        if index == 0:
            return self.b
        if index == 1:
            return self.c
        if index == 2:
            return self.d
        if index == 3:
            return self.e
        if index == 4:
            return self.h
        if index == 5:
            return self.l
        if index == 6:
            return self._read8(self.hl)
        if index == 7:
            return self.a
        raise AssertionError(index)

    def _set_r8(self, index: int, value: int) -> None:
        value &= 0xFF
        if index == 0:
            self.b = value
        elif index == 1:
            self.c = value
        elif index == 2:
            self.d = value
        elif index == 3:
            self.e = value
        elif index == 4:
            self.h = value
        elif index == 5:
            self.l = value
        elif index == 6:
            self._write8(self.hl, value)
        elif index == 7:
            self.a = value
        else:
            raise AssertionError(index)

    def _get_r16(self, index: int) -> int:
        if index == 0:
            return self.bc
        if index == 1:
            return self.de
        if index == 2:
            return self.hl
        if index == 3:
            return self.sp
        raise AssertionError(index)

    def _set_r16(self, index: int, value: int) -> None:
        value &= 0xFFFF
        if index == 0:
            self.bc = value
        elif index == 1:
            self.de = value
        elif index == 2:
            self.hl = value
        elif index == 3:
            self.sp = value
        else:
            raise AssertionError(index)

    def _get_stack_r16(self, index: int) -> int:
        if index == 0:
            return self.bc
        if index == 1:
            return self.de
        if index == 2:
            return self.hl
        if index == 3:
            return self.af
        raise AssertionError(index)

    def _set_stack_r16(self, index: int, value: int) -> None:
        if index == 0:
            self.bc = value
        elif index == 1:
            self.de = value
        elif index == 2:
            self.hl = value
        elif index == 3:
            self.af = value
        else:
            raise AssertionError(index)

    def _push16(self, value: int) -> None:
        self.sp = (self.sp - 1) & 0xFFFF
        self._write8(self.sp, (value >> 8) & 0xFF)
        self.sp = (self.sp - 1) & 0xFFFF
        self._write8(self.sp, value & 0xFF)

    def _pop16(self) -> int:
        lo = self._read8(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        hi = self._read8(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        return lo | (hi << 8)

    def _service_interrupt_if_needed(self) -> int:
        pending = self._pending_interrupts()
        if pending and self.halted:
            self.halted = False
        if not self.ime or not pending:
            return 0
        bit = (pending & -pending).bit_length() - 1
        self.ime = False
        self.bus.interrupt_flags = self.bus.interrupt_flags & ~(1 << bit)
        self._internal_cycle(8)
        self._push16(self.pc)
        self.pc = (0x40, 0x48, 0x50, 0x58, 0x60)[bit]
        self._internal_cycle()
        return 20

    def _pending_interrupts(self) -> int:
        return self.bus.ie & self.bus.interrupt_flags & 0x1F

    def _fetch8(self) -> int:
        value = self.bus.read8(self.pc)
        self._add_cycles(4)
        if self._halt_bug:
            self._halt_bug = False
        else:
            self.pc = (self.pc + 1) & 0xFFFF
        return value

    def _fetch16(self) -> int:
        lo = self._fetch8()
        hi = self._fetch8()
        return lo | (hi << 8)

    def _consume_stop_padding(self) -> None:
        self.pc = (self.pc + 1) & 0xFFFF

    def _read8(self, address: int) -> int:
        value = self.bus.read8(address)
        self._add_cycles(4)
        return value

    def _write8(self, address: int, value: int) -> None:
        self.bus.write8(address, value)
        self._add_cycles(4)

    def _write16(self, address: int, value: int) -> None:
        self._write8(address, value & 0xFF)
        self._write8((address + 1) & 0xFFFF, (value >> 8) & 0xFF)

    def _internal_cycle(self, cycles: int = 4) -> None:
        self._add_cycles(cycles)

    def _add_cycles(self, cycles: int) -> None:
        if cycles <= 0:
            return
        self.cycles += cycles
        if self._instruction_timing_active:
            self._instruction_cycles_used += cycles
        self.bus.tick(cycles, defer_new_dma=True)

    def _begin_instruction_timing(self) -> None:
        self._instruction_timing_active = True
        self._instruction_cycles_used = 0

    def _end_instruction_timing(self) -> None:
        self._instruction_timing_active = False

    def _pad_instruction_cycles(self, total_cycles: int) -> None:
        remaining = total_cycles - self._instruction_cycles_used
        if remaining != 0:
            raise AssertionError(
                "Instruction cycle model mismatch: "
                f"used {self._instruction_cycles_used}, declared {total_cycles}"
            )

    def _finish_instruction(self) -> None:
        if self._ime_delay:
            self._ime_delay -= 1
            if self._ime_delay == 0:
                self.ime = True
        self.f &= 0xF0

    def _set_flag(self, flag: int, value: bool) -> None:
        if value:
            self.f |= flag
        else:
            self.f &= ~flag
        self.f &= 0xF0


def _signed8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value
