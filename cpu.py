from __future__ import annotations

from dataclasses import dataclass

from bus import Bus
from cartridge import MapperKind
from opcodes import INVALID_OPCODES, disassemble


FLAG_Z = 0x80
FLAG_N = 0x40
FLAG_H = 0x20
FLAG_C = 0x10
PPU_DOTS_PER_LINE = 456
PPU_VBLANK_LINE = 144
PPU_LINES_PER_FRAME = 154


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


@dataclass(frozen=True)
class CPUProfileStats:
    interrupt_entries: int
    halt_idle_batches: int
    halt_idle_cycles: int


class CPU:
    def __init__(self, bus: Bus, start_pc: int = 0x0100, post_boot: bool = True) -> None:
        self.bus = bus
        self._fast_rom_cartridge = bus.cartridge
        self._fast_rom_data = bus.cartridge.data
        self._fast_rom_data_len = len(bus.cartridge.data)
        self._fast_rom_is_mbc3 = bus.cartridge.type_spec.mapper is MapperKind.MBC3
        if post_boot and bus.cgb_mode:
            self.a = 0x11
            self.f = 0x80
            self.b = 0x00
            self.c = 0x00
            self.d = 0xFF
            self.e = 0x56
            self.h = 0x00
            self.l = 0x0D
        elif post_boot:
            self.a = 0x01
            self.f = 0xB0
            self.b = 0x00
            self.c = 0x13
            self.d = 0x00
            self.e = 0xD8
            self.h = 0x01
            self.l = 0x4D
        else:
            self.a = 0x00
            self.f = 0x00
            self.b = 0x00
            self.c = 0x00
            self.d = 0x00
            self.e = 0x00
            self.h = 0x00
            self.l = 0x00
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
        self.profile_enabled = False
        self._profile_interrupt_entries = 0
        self._profile_halt_idle_batches = 0
        self._profile_halt_idle_cycles = 0

    def consume_profile(self) -> CPUProfileStats:
        stats = CPUProfileStats(
            interrupt_entries=self._profile_interrupt_entries,
            halt_idle_batches=self._profile_halt_idle_batches,
            halt_idle_cycles=self._profile_halt_idle_cycles,
        )
        self._profile_interrupt_entries = 0
        self._profile_halt_idle_batches = 0
        self._profile_halt_idle_cycles = 0
        return stats

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

    def step(self, trace: bool = False, prefetched_opcode: int | None = None) -> int:
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

        if trace:
            pc_before = self.pc
            raw, mnemonic = disassemble(self.bus, pc_before)
            registers_before = self.format_registers()
        self._begin_instruction_timing()
        if prefetched_opcode is None or trace:
            opcode = self._fetch8()
        else:
            opcode = prefetched_opcode & 0xFF
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
        cycles = self._execute_opcode(opcode)
        self.instructions += 1
        self._pad_instruction_cycles(cycles)
        self._end_instruction_timing()
        self._finish_instruction()
        if trace:
            self.last_trace = CPUTrace(pc_before, raw, mnemonic, registers_before, cycles)
        return cycles

    def _step_prefetched_fast(self, opcode: int) -> int:
        cycles = self._execute_prefetched_fast(opcode & 0xFF)
        if cycles:
            self.instructions += 1
            self._finish_instruction()
            return cycles
        self._add_cycles(4)
        self.pc = (self.pc + 1) & 0xFFFF
        cycles = self._execute_opcode(opcode & 0xFF)
        self.instructions += 1
        self._finish_instruction()
        return cycles

    def run(
        self,
        max_instructions: int | None = None,
        trace: bool = False,
        trace_sink=None,
        step_mode: bool = False,
        stop_condition=None,
        stop_frame_ppu=None,
        stop_frame_target: int | None = None,
        after_step=None,
    ) -> None:
        if max_instructions is not None and max_instructions < 0:
            raise ValueError("max_instructions must be non-negative")
        steps = 0
        fast_step = not trace and not step_mode and after_step is None
        bus = self.bus
        mapper = bus.mapper
        rom_data = self._fast_rom_data
        rom_data_len = self._fast_rom_data_len
        mbc3_fast_rom = self._fast_rom_is_mbc3
        fast_rom_cartridge = self._fast_rom_cartridge
        while max_instructions is None or steps < max_instructions:
            if stop_frame_target is not None:
                if stop_frame_ppu.frame_count >= stop_frame_target:
                    break
            elif stop_condition is not None and stop_condition():
                break
            if fast_step:
                if self.stopped:
                    if bus.stop_wake_requested() or self._pending_interrupts():
                        self.stopped = False
                        bus.exit_stop()
                    else:
                        steps += 1
                        continue
                pending_interrupts = bus.ie & bus.io[0x0F] & 0x1F
                if pending_interrupts:
                    if self.halted:
                        self.halted = False
                    if self.ime:
                        interrupt_cycles = self._service_interrupt_pending(pending_interrupts)
                        self._finish_instruction()
                        steps += 1
                        continue
            idle_cycles = (
                self._halt_idle_cycles(
                    max_instructions=max_instructions,
                    steps=steps,
                    trace=trace,
                    step_mode=step_mode,
                    after_step=after_step,
                )
                if self.halted
                else 0
            )
            if idle_cycles:
                if self.profile_enabled:
                    self._profile_halt_idle_batches += 1
                    self._profile_halt_idle_cycles += idle_cycles
                self._add_cycles(idle_cycles)
                steps += idle_cycles // 4
                if after_step is not None:
                    after_step()
                continue
            if fast_step and self.halted and self._pending_interrupts():
                self.halted = False
            pc = self.pc
            if pc <= 0x7FFF:
                if bus._oam_dma_active:
                    opcode = bus.read8(pc)
                elif bus.boot_rom_enabled and pc < len(bus.boot_rom):
                    opcode = bus.boot_rom[pc]
                elif mbc3_fast_rom:
                    if pc < 0x4000:
                        opcode = rom_data[pc] if pc < rom_data_len else 0xFF
                    else:
                        opcode = rom_data[
                            fast_rom_cartridge._mbc3_rom_bank_offset + (pc - 0x4000)
                        ]
                else:
                    opcode = mapper.read_rom(pc)
            else:
                opcode = self._read8_fast(pc)
            if opcode == 0xF0:
                ly_wait_steps = self._fast_forward_ly_wait_loop(
                    max_instructions=max_instructions,
                    steps=steps,
                    trace=trace,
                    step_mode=step_mode,
                    after_step=after_step,
                )
                if ly_wait_steps:
                    steps += ly_wait_steps
                    continue
            elif opcode == 0x3D:
                dec8_steps = self._fast_forward_dec8_delay_loop(
                    max_instructions=max_instructions,
                    steps=steps,
                    trace=trace,
                    step_mode=step_mode,
                    after_step=after_step,
                )
                if dec8_steps:
                    steps += dec8_steps
                    continue
            elif opcode in {0x00, 0x0B, 0x1B}:
                loop_steps = self._fast_forward_dec16_delay_loop(
                    max_instructions=max_instructions,
                    steps=steps,
                    trace=trace,
                    step_mode=step_mode,
                    after_step=after_step,
                )
                if loop_steps:
                    steps += loop_steps
                    continue
                if opcode == 0x00:
                    nop_steps = self._fast_forward_nops(
                        max_instructions=max_instructions,
                        steps=steps,
                        trace=trace,
                        step_mode=step_mode,
                        after_step=after_step,
                    )
                    if nop_steps:
                        steps += nop_steps
                        continue
            if (
                pc == 0x00B5
                or pc == 0x2670
                or pc == 0x2649
                or pc == 0x1837
                or pc == 0x25C4
                or pc == 0x25D8
                or pc == 0x276D
                or pc == 0x36E2
                or pc == 0x3E75
                or pc == 0x3E8D
                or pc == 0x01A7
                or pc == 0x5A5F
                or pc == 0x4000
                or pc == 0x3C04
                or pc == 0x019A
                or pc == 0x38F6
                or pc == 0x374F
                or pc == 0x3872
                or pc == 0x3AD9
                or pc == 0x4BD1
                or pc == 0x4B6C
                or pc == 0x71AE
                or pc == 0x71E6
            ):
                hot_steps = self._fast_forward_hot_rom_sequence(
                    opcode=opcode,
                    max_instructions=max_instructions,
                    steps=steps,
                    trace=trace,
                    step_mode=step_mode,
                    after_step=after_step,
                )
                if hot_steps:
                    steps += hot_steps
                    continue
            if fast_step:
                self._step_prefetched_fast(opcode)
            else:
                self.step(trace=trace, prefetched_opcode=None if trace else opcode)
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

    def _halt_idle_cycles(
        self,
        *,
        max_instructions: int | None,
        steps: int,
        trace: bool,
        step_mode: bool,
        after_step,
    ) -> int:
        if trace or step_mode:
            return 0
        if not self.halted or self._pending_interrupts():
            return 0
        if max_instructions is None:
            max_cycles = 1 << 20
        else:
            remaining_steps = max_instructions - steps
            if remaining_steps <= 0:
                return 0
            max_cycles = remaining_steps * 4
        cycles = self.bus.cycles_until_next_interrupt_event(max_cycles)
        if cycles <= 0:
            return 0
        return min(max_cycles, ((cycles + 3) // 4) * 4)

    def _fast_forward_nops(
        self,
        *,
        max_instructions: int | None,
        steps: int,
        trace: bool,
        step_mode: bool,
        after_step,
    ) -> int:
        if trace or step_mode or after_step is not None:
            return 0
        if self.halted or self.stopped or self._halt_bug or self._ime_delay:
            return 0
        if self.ime and self._pending_interrupts():
            return 0
        if not self._can_fast_fetch_nop_at(self.pc):
            return 0

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < 2:
            return 0
        max_cycles = min(remaining_instructions * 4, 4096)
        event_cycles = self._fast_forward_safe_cycles(max_cycles)
        max_nops = min(remaining_instructions, event_cycles // 4)
        if max_nops < 2:
            return 0

        count = 1
        pc = (self.pc + 1) & 0xFFFF
        while count < max_nops and self._can_fast_fetch_nop_at(pc):
            count += 1
            pc = (pc + 1) & 0xFFFF
        if count < 2:
            return 0

        self.pc = (self.pc + count) & 0xFFFF
        self.instructions += count
        self._add_cycles(count * 4)
        self.f &= 0xF0
        return count

    def _fast_forward_dec8_delay_loop(
        self,
        *,
        max_instructions: int | None,
        steps: int,
        trace: bool,
        step_mode: bool,
        after_step,
    ) -> int:
        if trace or step_mode or after_step is not None:
            return 0
        if self.halted or self.stopped or self._halt_bug or self._ime_delay:
            return 0
        if self.ime and self._pending_interrupts():
            return 0

        pc = self.pc
        if not self._matches_bytes(pc, (0x3D, 0x20, 0xFD)):
            return 0

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < 2:
            return 0

        remaining_iterations = self.a or 0x100
        max_iterations = remaining_instructions // 2
        max_cycles = min(max_iterations * 16, 4096)
        event_cycles = self._fast_forward_safe_cycles(max_cycles)
        max_iterations = min(max_iterations, max(0, event_cycles // 16))
        if max_iterations < 1:
            return 0

        iterations = min(remaining_iterations, max_iterations)
        completed = iterations == remaining_iterations
        result = (self.a - iterations) & 0xFF
        previous_value = (self.a - iterations + 1) & 0xFF
        self.a = result
        self.f = (
            (self.f & FLAG_C)
            | FLAG_N
            | (FLAG_Z if result == 0 else 0)
            | (FLAG_H if (previous_value & 0x0F) == 0x00 else 0)
        )
        self.pc = (pc + 3) & 0xFFFF if completed else pc
        cycles = (iterations - 1) * 16 + (12 if completed else 16)
        instruction_count = iterations * 2
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_ly_wait_loop(
        self,
        *,
        max_instructions: int | None,
        steps: int,
        trace: bool,
        step_mode: bool,
        after_step,
    ) -> int:
        if trace or step_mode or after_step is not None:
            return 0
        if self.halted or self.stopped or self._halt_bug or self._ime_delay:
            return 0
        if self.ime and self._pending_interrupts():
            return 0

        pc = self.pc
        if self.bus.read8(pc) != 0xF0 or self.bus.read8((pc + 1) & 0xFFFF) != 0x44:
            return 0
        compare_opcode = self.bus.read8((pc + 2) & 0xFFFF)
        branch_opcode = self.bus.read8((pc + 3) & 0xFFFF)
        if self.bus.read8((pc + 4) & 0xFFFF) != 0xFB:
            return 0
        if compare_opcode == 0xBC:
            target = self.h
        elif compare_opcode == 0xBD:
            target = self.l
        else:
            return 0
        if branch_opcode not in {0x20, 0x28}:
            return 0

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < 3:
            return 0
        max_iterations = remaining_instructions // 3
        max_cycles = min(max_iterations * 28, 4096)
        safe_cycles = self._fast_forward_safe_cycles(max_cycles)
        max_iterations = min(max_iterations, max(0, safe_cycles // 28))
        if max_iterations < 1:
            return 0

        last_sample = self.a
        for index in range(max_iterations):
            sample = self._ly_after_cycles(8 + index * 28)
            last_sample = sample
            equal = sample == target
            branch_taken = not equal if branch_opcode == 0x20 else equal
            if not branch_taken:
                self.a = sample
                self._set_cp_flags(sample, target)
                self.pc = (pc + 5) & 0xFFFF
                instruction_count = (index + 1) * 3
                cycles = index * 28 + 24
                self.instructions += instruction_count
                self._add_cycles(cycles)
                return instruction_count

        self.a = last_sample
        self._set_cp_flags(last_sample, target)
        instruction_count = max_iterations * 3
        self.instructions += instruction_count
        self._add_cycles(max_iterations * 28)
        return instruction_count

    def _ly_after_cycles(self, cycles: int) -> int:
        ppu = getattr(self.bus, "ppu", None)
        if ppu is None or not getattr(ppu, "lcd_enabled", False):
            return self.bus.io[0x44]
        scanline = getattr(ppu, "_scanline", 0)
        line_dots = getattr(ppu, "line_dots", 0) + max(0, cycles)
        scanline = (scanline + line_dots // PPU_DOTS_PER_LINE) % PPU_LINES_PER_FRAME
        line_dots %= PPU_DOTS_PER_LINE
        if scanline == PPU_LINES_PER_FRAME - 1 and line_dots >= 4:
            return 0
        if scanline < PPU_VBLANK_LINE and line_dots >= PPU_DOTS_PER_LINE - 4:
            return (scanline + 1) & 0xFF
        return scanline & 0xFF

    def _set_cp_flags(self, left: int, right: int) -> None:
        left &= 0xFF
        right &= 0xFF
        result = left - right
        self.f = (
            FLAG_N
            | (FLAG_Z if (result & 0xFF) == 0 else 0)
            | (FLAG_H if (left & 0x0F) < (right & 0x0F) else 0)
            | (FLAG_C if left < right else 0)
        )

    def _fast_forward_dec16_delay_loop(
        self,
        *,
        max_instructions: int | None,
        steps: int,
        trace: bool,
        step_mode: bool,
        after_step,
    ) -> int:
        if trace or step_mode or after_step is not None:
            return 0
        if self.halted or self.stopped or self._halt_bug or self._ime_delay:
            return 0
        if self.ime and self._pending_interrupts():
            return 0

        pc = self.pc
        if self._matches_bytes(pc, (0x0B, 0x78, 0xB1, 0x20, 0xFB)):
            register_name = "bc"
            register_value = self.bc
            instructions_per_iteration = 4
            taken_cycles = 28
            final_cycles = 24
            final_pc = (pc + 5) & 0xFFFF
        elif self._matches_bytes(pc, (0x1B, 0x7A, 0xB3, 0x20, 0xFB)):
            register_name = "de"
            register_value = self.de
            instructions_per_iteration = 4
            taken_cycles = 28
            final_cycles = 24
            final_pc = (pc + 5) & 0xFFFF
        elif self._matches_bytes(pc, (0x00, 0x00, 0x00, 0x1B, 0x7A, 0xB3, 0x20, 0xF8)):
            register_name = "de"
            register_value = self.de
            instructions_per_iteration = 7
            taken_cycles = 40
            final_cycles = 36
            final_pc = (pc + 8) & 0xFFFF
        else:
            return 0

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < instructions_per_iteration:
            return 0

        remaining_iterations = register_value or 0x10000
        max_iterations = remaining_instructions // instructions_per_iteration
        max_cycles = min(max_iterations * taken_cycles, 4096)
        event_cycles = self._fast_forward_safe_cycles(max_cycles)
        max_iterations = min(max_iterations, max(0, event_cycles // taken_cycles))
        if max_iterations < 1:
            return 0

        iterations = min(remaining_iterations, max_iterations)
        completed = iterations == remaining_iterations
        next_value = (register_value - iterations) & 0xFFFF
        if register_name == "bc":
            self.bc = next_value
        else:
            self.de = next_value
        self.a = ((next_value >> 8) | (next_value & 0xFF)) & 0xFF
        self.f = FLAG_Z if next_value == 0 else 0
        self.pc = final_pc if completed else pc
        cycles = (iterations - 1) * taken_cycles + (final_cycles if completed else taken_cycles)
        instruction_count = iterations * instructions_per_iteration
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_hot_rom_sequence(
        self,
        *,
        opcode: int,
        max_instructions: int | None,
        steps: int,
        trace: bool,
        step_mode: bool,
        after_step,
    ) -> int:
        if trace or step_mode or after_step is not None:
            return 0
        if self.halted or self.stopped or self._halt_bug or self._ime_delay:
            return 0
        if self.ime and self._pending_interrupts():
            return 0
        pc = self.pc
        if opcode == 0x2A and pc == 0x00B5 and self._matches_bytes(
            pc,
            (0x2A, 0x12, 0x13, 0x0B, 0x79, 0xB0, 0x20, 0xF8, 0xC9),
        ):
            return self._fast_forward_hot_copy_loop(max_instructions, steps, duplicate=False)
        if opcode == 0x2A and pc == 0x1837 and self._matches_bytes(
            pc,
            (0x2A, 0x12, 0x13, 0x12, 0x13, 0x0B, 0x79, 0xB0, 0x20, 0xF6),
        ):
            return self._fast_forward_hot_copy_loop(max_instructions, steps, duplicate=True)
        if opcode == 0xFA and pc == 0x2670 and self._matches_bytes(
            pc,
            (
                0xFA,
                0xA6,
                0xD0,
                0x3D,
                0x20,
                0x08,
                0xCD,
                0x8B,
                0x26,
                0xEA,
                0xA5,
                0xD0,
                0x3E,
                0x08,
                0xEA,
                0xA6,
                0xD0,
                0xFA,
                0xA5,
                0xD0,
                0x07,
                0xEA,
                0xA5,
                0xD0,
                0xE6,
                0x01,
                0xC9,
            ),
        ):
            return self._fast_forward_hot_bitstream_step(max_instructions, steps)
        if opcode == 0x5F and pc == 0x2649 and self._matches_bytes(
            pc,
            (
                0x5F,
                0xFA,
                0xA7,
                0xD0,
                0xA7,
                0x28,
                0x14,
                0xFE,
                0x02,
                0x38,
                0x08,
                0x28,
                0x0C,
                0xCB,
                0x0B,
                0xCB,
                0x0B,
                0x18,
                0x08,
                0xCB,
                0x23,
                0xCB,
                0x23,
                0x18,
                0x02,
                0xCB,
                0x33,
                0xFA,
                0xAD,
                0xD0,
                0x6F,
                0xFA,
                0xAE,
                0xD0,
                0x67,
                0x7E,
                0xB3,
                0x77,
                0xC9,
            ),
        ):
            return self._fast_forward_hot_or_mask_step(max_instructions, steps)
        if opcode == 0x43 and pc == 0x25C4 and self._matches_bytes(
            pc,
            (
                0x43,
                0xAF,
                0xCD,
                0x49,
                0x26,
                0x58,
                0xCD,
                0xD8,
                0x25,
                0x1B,
                0x7A,
                0xA7,
                0x20,
                0x02,
                0x7B,
                0xA7,
                0x20,
                0xEE,
                0x18,
                0xA8,
            ),
        ):
            return self._fast_forward_hot_zero_mask_loop(max_instructions, steps)
        if opcode == 0xFA and pc == 0x25D8 and self._matches_bytes(
            pc,
            (
                0xFA,
                0xA4,
                0xD0,
                0x47,
                0xFA,
                0xA2,
                0xD0,
                0x3C,
                0xB8,
                0x28,
                0x13,
                0xEA,
                0xA2,
                0xD0,
                0xFA,
                0xAD,
                0xD0,
                0x3C,
                0xEA,
                0xAD,
                0xD0,
                0xC0,
            ),
        ):
            return self._fast_forward_hot_counter_step(max_instructions, steps)
        if opcode == 0xCB and pc == 0x276D and self._matches_bytes(
            pc,
            (
                0xCB,
                0x3F,
                0x0E,
                0x00,
                0x30,
                0x02,
                0x0E,
                0x01,
                0x6F,
                0xFA,
                0xAA,
                0xD0,
                0xA7,
                0x28,
                0x04,
                0xCB,
                0x5B,
                0x18,
                0x02,
                0xCB,
                0x43,
                0x5D,
                0x20,
                0x09,
                0xFA,
                0xB1,
                0xD0,
                0x6F,
                0xFA,
                0xB2,
                0xD0,
                0x18,
                0x07,
                0xFA,
                0xB3,
                0xD0,
                0x6F,
                0xFA,
                0xB4,
                0xD0,
                0x67,
                0x7B,
                0x85,
                0x6F,
                0x30,
                0x01,
                0x24,
                0x7E,
                0xCB,
                0x41,
                0x20,
                0x02,
                0xCB,
                0x37,
                0xE6,
                0x0F,
                0x5F,
                0xC9,
            ),
        ):
            return self._fast_forward_hot_nibble_fetch_step(max_instructions, steps)
        if opcode == 0x7A and pc == 0x36E2 and self._matches_bytes(
            pc,
            (0x7A, 0x22, 0x0B, 0x78, 0xB1, 0x20, 0xF9),
        ):
            return self._fast_forward_hot_fill_loop(max_instructions, steps)
        if opcode == 0xF5 and pc == 0x3E75 and self._matches_bytes(
            pc,
            (0xF5, 0x3E, 0x13, 0xE0, 0xB8, 0xEA, 0x00, 0x20, 0xCD, 0x49, 0x7E),
        ):
            return self._fast_forward_pokemon_predef_call(max_instructions, steps)
        if opcode == 0x21 and pc == 0x374F and self._matches_bytes(
            pc,
            (0x21, 0x2A, 0xC0, 0xAF, 0xB6, 0x23, 0xB6, 0x23, 0x23, 0xB6, 0x20, 0xF4),
        ):
            return self._fast_forward_pokemon_wram_flag_wait_loop(max_instructions, steps)
        if (
            opcode == 0xE5
            and pc == 0x3872
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_text_wait_loop(max_instructions, steps)
        if (
            opcode == 0xE5
            and pc == 0x3AD9
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_text_wait_loop_alt(max_instructions, steps)
        if (
            opcode == 0xF0
            and pc == 0x4B6C
            and self._fast_rom_is_mbc3
            and self._fast_rom_cartridge.mbc3_rom_bank % self._fast_rom_cartridge.rom_bank_count == 1
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_sprite_oam_piece_loop(max_instructions, steps)
        if (
            opcode == 0x1C
            and pc == 0x4BD1
            and self._fast_rom_is_mbc3
            and self._fast_rom_cartridge.mbc3_rom_bank % self._fast_rom_cartridge.rom_bank_count == 1
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_object_position_helper(max_instructions, steps)
        if (
            opcode == 0x2A
            and pc == 0x71AE
            and self._fast_rom_is_mbc3
            and self._fast_rom_cartridge.mbc3_rom_bank % self._fast_rom_cartridge.rom_bank_count == 3
            and self._matches_bytes(
                pc,
                (0x2A, 0xFE, 0xFF, 0x28, 0x11, 0xB8, 0x2A, 0x20, 0xF7),
            )
        ):
            return self._fast_forward_pokemon_bank3_table_scan(max_instructions, steps)
        if (
            opcode == 0xE5
            and pc == 0x71E6
            and self._fast_rom_is_mbc3
            and self._fast_rom_cartridge.mbc3_rom_bank % self._fast_rom_cartridge.rom_bank_count == 3
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_bank3_bit_helper(max_instructions, steps)
        if opcode == 0xF1 and pc in {0x01A7, 0x3E8D} and self._matches_bytes(
            pc,
            (0xF1, 0xE0, 0xB8, 0xEA, 0x00, 0x20, 0xC9),
        ):
            return self._fast_forward_pokemon_bank_restore_return(max_instructions, steps)
        if (
            opcode == 0xFA
            and pc == 0x5A5F
            and self._fast_rom_is_mbc3
            and self._fast_rom_cartridge.mbc3_rom_bank % self._fast_rom_cartridge.rom_bank_count == 1
            and self._matches_bytes(
                pc,
                (0xFA, 0x2B, 0xD1, 0xFE, 0x02, 0x28, 0x0F, 0xFE, 0x03, 0x28, 0x0B, 0xFE, 0x05, 0xC0),
            )
        ):
            return self._fast_forward_pokemon_text_predef_return(max_instructions, steps)
        if (
            opcode == 0xCD
            and pc == 0x38F6
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
            and self._matches_bytes(
                0x019A,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_joypad_poll_loop(max_instructions, steps)
        if (
            opcode == 0xF0
            and pc == 0x019A
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_joypad_status_call(max_instructions, steps)
        if (
            opcode == 0xF0
            and pc == 0x4000
            and self._fast_rom_is_mbc3
            and self._fast_rom_cartridge.mbc3_rom_bank % self._fast_rom_cartridge.rom_bank_count == 3
            and self._matches_bytes(
                pc,
                (
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
                ),
            )
        ):
            return self._fast_forward_pokemon_joypad_status_return(max_instructions, steps)
        if (
            opcode == 0x7E
            and pc == 0x3C04
            and self._matches_bytes(pc, (0x7E, 0x47, 0x3E, 0xEE, 0xB8, 0x20, 0x18))
            and self._matches_bytes(0x3C23, (0xF0, 0x8B, 0xA7, 0xC8, 0x3D, 0xE0, 0x8B, 0xC0))
        ):
            return self._fast_forward_pokemon_text_delay_return(max_instructions, steps)
        return 0

    def _fast_forward_pokemon_predef_call(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        cycles = 412
        if (
            self._ime_delay
            or self.halted
            or self.stopped
            or self._halt_bug
            or not self._fast_rom_is_mbc3
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        sp = self.sp
        stack_addresses = (
            (sp - 1) & 0xFFFF,
            (sp - 2) & 0xFFFF,
            (sp - 3) & 0xFFFF,
            (sp - 4) & 0xFFFF,
        )
        if any(not self._is_direct_fast_address(address) for address in stack_addresses):
            return 0

        bus = self.bus
        wram = bus.wram
        predef_id = wram[0xCC4E - 0xC000]
        table_offset = 0x13 * 0x4000 + (0x7E79 - 0x4000) + predef_id * 3
        data = self._fast_rom_data
        if table_offset + 2 >= self._fast_rom_data_len:
            return 0

        target_bank = data[table_offset]
        target_low = data[table_offset + 1]
        target_high = data[table_offset + 2]
        target = target_low | (target_high << 8)
        table_index = predef_id * 3
        table_low = table_index & 0xFF
        table_high = table_index >> 8
        instruction_count = 44 if table_high else 43

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < instruction_count:
            return 0

        wram[0xCC4F - 0xC000] = self.h
        wram[0xCC50 - 0xC000] = self.l
        wram[0xCC51 - 0xC000] = self.d
        wram[0xCC52 - 0xC000] = self.e
        wram[0xCC53 - 0xC000] = self.b
        wram[0xCC54 - 0xC000] = self.c
        wram[0xD0B7 - 0xC000] = target_bank

        self._write8_direct_fast((sp - 1) & 0xFFFF, self.a)
        self._write8_direct_fast((sp - 2) & 0xFFFF, self.f & 0xF0)
        self._write8_direct_fast((sp - 3) & 0xFFFF, 0x3E)
        self._write8_direct_fast((sp - 4) & 0xFFFF, 0x8D)
        self.sp = (sp - 4) & 0xFFFF

        bus.hram[0xFFB8 - 0xFF80] = target_bank & 0xFF
        bus.mapper.write_rom_control(0x2000, target_bank)

        hl_before_add = 0x7E79
        de_for_table = (table_high << 8) | table_low
        add_result = hl_before_add + de_for_table
        self.f = (
            (FLAG_Z if table_low == 0 else 0)
            | (FLAG_H if ((hl_before_add & 0x0FFF) + (de_for_table & 0x0FFF)) > 0x0FFF else 0)
            | (FLAG_C if add_result > 0xFFFF else 0)
        )
        self.a = target_bank & 0xFF
        self.d = 0x3E
        self.e = 0x8D
        self.h = target_high
        self.l = target_low
        self.pc = target
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_wram_flag_wait_loop(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        cycles_per_iteration = 76
        instructions_per_iteration = 9
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instructions_per_iteration
            or not self._can_batch_direct_memory_cycles()
        ):
            return 0

        wram = self.bus.wram
        value = (
            wram[0xC02A - 0xC000]
            | wram[0xC02B - 0xC000]
            | wram[0xC02D - 0xC000]
        )
        if value == 0:
            return 0

        max_iterations = min(
            remaining_instructions // instructions_per_iteration,
            4096 // cycles_per_iteration,
        )
        safe_cycles = self._fast_forward_safe_cycles(max_iterations * cycles_per_iteration)
        iterations = safe_cycles // cycles_per_iteration
        if iterations <= 0:
            return 0

        self.a = value & 0xFF
        self.f = 0
        self.h = 0xC0
        self.l = 0x2D
        self.pc = 0x374F
        instruction_count = iterations * instructions_per_iteration
        self.instructions += instruction_count
        self._add_cycles(iterations * cycles_per_iteration)
        return instruction_count

    def _fast_forward_pokemon_text_wait_loop(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 132
        cycles = 1420
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._fast_rom_is_mbc3
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
            or not self._matches_bytes(0x3831, (0xCD, 0x9A, 0x01, 0xF0, 0xB7, 0xA7, 0xF0, 0xB3))
            or not self._matches_bytes(0x3849, (0xF0, 0xD5, 0xA7, 0x28, 0x04, 0xAF, 0xE0, 0xB5, 0xC9))
            or not self._matches_bytes(0x3C04, (0x7E, 0x47, 0x3E, 0xEE, 0xB8, 0x20, 0x18))
            or not self._matches_bytes(0x3C23, (0xF0, 0x8B, 0xA7, 0xC8, 0x3D, 0xE0, 0x8B, 0xC0))
            or not self._matches_bytes(0x3E6D, (0xEA, 0x4E, 0xCC, 0xF0, 0xB8, 0xEA, 0x12, 0xCF))
        ):
            return 0

        sp = self.sp
        stack_addresses = tuple((sp + offset) & 0xFFFF for offset in range(-8, 2))
        if any(not self._is_direct_fast_address(address) for address in stack_addresses):
            return 0

        bus = self.bus
        wram = bus.wram
        hram = bus.hram
        if wram[0xD09B - 0xC000] != 0:
            return 0
        tile_value = self._read8_direct_fast(0xC4F2)
        delay_counter = hram[0xFF8B - 0xFF80]
        if tile_value is None or tile_value == 0xEE or delay_counter <= 1:
            return 0

        next_buttons = hram[0xFFF8 - 0xFF80]
        old_buttons = hram[0xFFB1 - 0xFF80]
        changed = next_buttons ^ old_buttons
        pressed = changed & next_buttons
        if (
            next_buttons == 0x0F
            or pressed != 0
            or hram[0xFFB7 - 0xFF80] != 0
            or hram[0xFFD5 - 0xFF80] == 0
            or wram[0xD730 - 0xC000] & 0x20
            or wram[0xCD6B - 0xC000] != 0
            or wram[0xD12B - 0xC000] in {0x02, 0x03, 0x05}
        ):
            return 0

        predef_id = 0x2D
        table_offset = 0x13 * 0x4000 + (0x7E79 - 0x4000) + predef_id * 3
        data = self._fast_rom_data
        if table_offset + 2 >= self._fast_rom_data_len:
            return 0
        target_bank = data[table_offset]
        target_low = data[table_offset + 1]
        target_high = data[table_offset + 2]
        target = target_low | (target_high << 8)
        target_offset = target_bank * 0x4000 + (target - 0x4000)
        target_bytes = (
            0xFA,
            0x2B,
            0xD1,
            0xFE,
            0x02,
            0x28,
            0x0F,
            0xFE,
            0x03,
            0x28,
            0x0B,
            0xFE,
            0x05,
            0xC0,
        )
        if (
            target_bank != 0x01
            or target != 0x5A5F
            or target_offset + len(target_bytes) > self._fast_rom_data_len
            or any(data[target_offset + index] != value for index, value in enumerate(target_bytes))
        ):
            return 0

        old_bank = hram[0xFFB8 - 0xFF80]
        initial_h = self.h
        initial_l = self.l
        hram[0xFF8B - 0xFF80] = (delay_counter - 1) & 0xFF
        hram[0xFFB2 - 0xFF80] = next_buttons & old_buttons
        hram[0xFFB3 - 0xFF80] = pressed
        hram[0xFFB1 - 0xFF80] = next_buttons
        hram[0xFFB4 - 0xFF80] = next_buttons
        hram[0xFFB5 - 0xFF80] = 0
        hram[0xFFB8 - 0xFF80] = old_bank

        wram[0xCC4E - 0xC000] = predef_id
        wram[0xCC4F - 0xC000] = initial_h
        wram[0xCC50 - 0xC000] = initial_l
        wram[0xCC51 - 0xC000] = changed
        wram[0xCC52 - 0xC000] = old_buttons
        wram[0xCC53 - 0xC000] = next_buttons
        wram[0xCC54 - 0xC000] = self.c
        wram[0xCF12 - 0xC000] = old_bank
        wram[0xD0B7 - 0xC000] = target_bank
        bus.mapper.write_rom_control(0x2000, old_bank)

        self.a = 0
        self.f = FLAG_Z | FLAG_H
        self.b = next_buttons
        self.d = 0x3E
        self.e = 0x8D
        self.h = target_high
        self.l = target_low
        self.pc = 0x3872
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_text_wait_loop_alt(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        cycles_per_iteration = 796
        instructions_per_iteration = 73
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instructions_per_iteration
            or not self._fast_rom_is_mbc3
            or not self._can_batch_direct_memory_cycles()
            or not self._matches_bytes(0x3831, (0xCD, 0x9A, 0x01, 0xF0, 0xB7, 0xA7, 0xF0, 0xB3))
            or not self._matches_bytes(0x3849, (0xF0, 0xD5, 0xA7, 0x28, 0x04, 0xAF, 0xE0, 0xB5, 0xC9))
            or not self._matches_bytes(
                0x019A,
                (
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
                ),
            )
            or not self._matches_bytes(0x3C04, (0x7E, 0x47, 0x3E, 0xEE, 0xB8, 0x20, 0x18))
            or not self._matches_bytes(0x3C23, (0xF0, 0x8B, 0xA7, 0xC8, 0x3D, 0xE0, 0x8B, 0xC0))
        ):
            return 0

        bank3_offset = 3 * 0x4000
        bank3_bytes = (
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
        )
        data = self._fast_rom_data
        if bank3_offset + len(bank3_bytes) > self._fast_rom_data_len or any(
            data[bank3_offset + index] != value for index, value in enumerate(bank3_bytes)
        ):
            return 0

        sp = self.sp
        stack_addresses = tuple((sp + offset) & 0xFFFF for offset in range(-8, 2))
        if any(not self._is_direct_fast_address(address) for address in stack_addresses):
            return 0

        bus = self.bus
        wram = bus.wram
        hram = bus.hram
        if wram[0xD09B - 0xC000] != 0:
            return 0

        tile_value = self._read8_direct_fast(0xC48E)
        cc34 = wram[0xCC34 - 0xC000]
        if (
            tile_value is None
            or tile_value == 0xEE
            or hram[0xFF8B - 0xFF80] != 0
            or cc34 == 1
        ):
            return 0

        next_buttons = hram[0xFFF8 - 0xFF80]
        old_buttons = hram[0xFFB1 - 0xFF80]
        changed = next_buttons ^ old_buttons
        pressed = changed & next_buttons
        if (
            next_buttons == 0x0F
            or pressed != 0
            or hram[0xFFB7 - 0xFF80] != 0
            or hram[0xFFD5 - 0xFF80] == 0
            or wram[0xD730 - 0xC000] & 0x20
            or wram[0xCD6B - 0xC000] != 0
        ):
            return 0

        max_iterations = min(
            remaining_instructions // instructions_per_iteration,
            4096 // cycles_per_iteration,
        )
        safe_cycles = self._fast_forward_safe_cycles(max_iterations * cycles_per_iteration)
        iterations = safe_cycles // cycles_per_iteration
        if iterations <= 0:
            return 0

        final_changed = changed
        final_old_buttons = old_buttons
        if iterations > 1:
            final_changed = 0
            final_old_buttons = next_buttons

        self._write8_direct_fast((sp - 1) & 0xFFFF, self.h)
        self._write8_direct_fast((sp - 2) & 0xFFFF, self.l)

        hram[0xFFB2 - 0xFF80] = next_buttons if iterations > 1 else next_buttons & old_buttons
        hram[0xFFB3 - 0xFF80] = 0 if iterations > 1 else pressed
        hram[0xFFB1 - 0xFF80] = next_buttons
        hram[0xFFB4 - 0xFF80] = next_buttons
        hram[0xFFB5 - 0xFF80] = 0
        old_bank = hram[0xFFB8 - 0xFF80]
        if self._fast_rom_cartridge.mbc3_rom_bank != old_bank:
            bus.mapper.write_rom_control(0x2000, old_bank)

        dec_result = (cc34 - 1) & 0xFF
        self.a = dec_result
        self.b = tile_value
        self.d = final_changed
        self.e = final_old_buttons
        self.f = (
            FLAG_N
            | (FLAG_Z if dec_result == 0 else 0)
            | (FLAG_H if (cc34 & 0x0F) == 0 else 0)
        )
        self.pc = 0x3AD9
        instruction_count = iterations * instructions_per_iteration
        self.instructions += instruction_count
        self._add_cycles(iterations * cycles_per_iteration)
        return instruction_count

    def _fast_forward_pokemon_sprite_oam_piece_loop(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < 34
            or not self._can_batch_direct_memory_cycles()
        ):
            return 0

        sp = self.sp
        stack_low = (sp - 2) & 0xFFFF
        stack_high = (sp - 1) & 0xFFFF
        if (
            not self._is_direct_fast_address(stack_low)
            or not self._is_direct_fast_address(stack_high)
        ):
            return 0

        max_cycles = self._fast_forward_safe_cycles(4096)
        if max_cycles < 292:
            return 0

        bus = self.bus
        hram = bus.hram
        y_base = hram[0xFF92 - 0xFF80]
        x_base = hram[0xFF91 - 0xFF80]
        priority_mask = hram[0xFF94 - 0xFF80]
        sprite_id = self._read8_direct_fast(0xD5CD)
        if sprite_id is None:
            return 0
        sprite_nibble = ((sprite_id & 0x0F) << 4 | (sprite_id >> 4)) & 0x0F

        h = self.h
        l = self.l
        b = self.b
        c = self.c
        d = self.d
        e = self.e
        if d != 0xC3:
            return 0

        total_instructions = 0
        total_cycles = 0
        final_a = self.a
        final_f = self.f & 0xF0
        pc = 0x4B6C
        ran = 0

        while remaining_instructions - total_instructions >= 34:
            hl = ((h << 8) | l) & 0xFFFF
            bc = ((b << 8) | c) & 0xFFFF
            de = ((d << 8) | e) & 0xFFFF
            if any(
                not self._is_direct_fast_address((de + offset) & 0xFFFF)
                for offset in range(4)
            ):
                break

            y_offset = self._read8_direct_fast(hl)
            x_offset = self._read8_direct_fast((hl + 1) & 0xFFFF)
            tile_offset = self._read8_direct_fast(bc)
            attr = self._read8_direct_fast((hl + 2) & 0xFFFF)
            if None in {y_offset, x_offset, tile_offset, attr}:
                break

            attr = int(attr)
            tile_offset = int(tile_offset)
            if sprite_nibble == 0x0B:
                tile_sum = 0x7C + tile_offset
                tile = tile_sum & 0xFF
                branch_instruction_adjust = -3
                branch_cycle_adjust = -16
                carry = FLAG_C if tile_sum > 0xFF else 0
            else:
                tile_base = (sprite_nibble << 3) + (sprite_nibble << 2)
                tile_sum = tile_base + tile_offset
                tile = tile_sum & 0xFF
                branch_instruction_adjust = 0
                branch_cycle_adjust = 0
                carry = FLAG_C if tile_sum > 0xFF else 0

            rendered_attr = attr
            attr_bit1 = bool(attr & 0x02)
            if attr_bit1:
                rendered_attr |= priority_mask
                carry = 0
            attr_bit0 = bool(rendered_attr & 0x01)

            instruction_count = 38 + branch_instruction_adjust + (2 if attr_bit1 else 0)
            cycles = (
                (308 if attr_bit0 else 312)
                + branch_cycle_adjust
                + (16 if attr_bit1 else 0)
            )
            if total_cycles + cycles > max_cycles:
                break
            if total_instructions + instruction_count > remaining_instructions:
                break

            y_value = (y_base + 0x10 + int(y_offset)) & 0xFF
            x_value = (x_base + 0x08 + int(x_offset)) & 0xFF
            if not (
                self._write8_direct_fast(de, y_value)
                and self._write8_direct_fast((de + 1) & 0xFFFF, x_value)
                and self._write8_direct_fast((de + 2) & 0xFFFF, tile)
                and self._write8_direct_fast((de + 3) & 0xFFFF, rendered_attr)
            ):
                break

            next_bc = (bc + 1) & 0xFFFF
            if not (
                self._write8_direct_fast(stack_low, next_bc & 0xFF)
                and self._write8_direct_fast(stack_high, (next_bc >> 8) & 0xFF)
            ):
                break

            h = ((hl + 3) >> 8) & 0xFF
            l = (hl + 3) & 0xFF
            b = (next_bc >> 8) & 0xFF
            c = next_bc & 0xFF
            e = (e + 4) & 0xFF
            final_a = rendered_attr
            final_f = FLAG_H | (0 if attr_bit0 else FLAG_Z) | carry
            total_instructions += instruction_count
            total_cycles += cycles
            ran += 1
            if attr_bit0:
                pc = 0x4BAA
                break

        if ran == 0:
            return 0

        self.a = final_a
        self.f = final_f
        self.b = b
        self.c = c
        self.d = d
        self.e = e
        self.h = h
        self.l = l
        self.pc = pc
        self.instructions += total_instructions
        self._add_cycles(total_cycles)
        return total_instructions

    def _fast_forward_pokemon_bank3_table_scan(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < 3
            or not self._can_batch_direct_memory_cycles()
        ):
            return 0

        max_cycles = self._fast_forward_safe_cycles(4096)
        if max_cycles < 28:
            return 0

        h = self.h
        l = self.l
        b = self.b
        total_instructions = 0
        total_cycles = 0
        final_a = self.a
        final_f = self.f & 0xF0
        final_pc = 0x71AE
        ran = 0

        while remaining_instructions - total_instructions >= 3:
            hl = ((h << 8) | l) & 0xFFFF
            value = self._read8_direct_fast(hl)
            if value is None:
                break
            value = int(value)

            if value == 0xFF:
                instruction_count = 3
                cycles = 28
                if total_cycles + cycles > max_cycles:
                    break
                final_a = value
                result = (value - 0xFF) & 0xFF
                final_f = FLAG_N | (FLAG_Z if result == 0 else 0)
                hl = (hl + 1) & 0xFFFF
                h = (hl >> 8) & 0xFF
                l = hl & 0xFF
                total_instructions += instruction_count
                total_cycles += cycles
                final_pc = 0x71C4
                ran += 1
                break

            pair_value = self._read8_direct_fast((hl + 1) & 0xFFFF)
            if pair_value is None:
                break
            pair_value = int(pair_value)
            instruction_count = 6
            cycles = 44 if value == b else 48
            if total_cycles + cycles > max_cycles:
                break
            if total_instructions + instruction_count > remaining_instructions:
                break

            final_a = pair_value
            result = (value - b) & 0xFF
            final_f = (
                FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) < (b & 0x0F) else 0)
                | (FLAG_C if value < b else 0)
            )
            hl = (hl + 2) & 0xFFFF
            h = (hl >> 8) & 0xFF
            l = hl & 0xFF
            total_instructions += instruction_count
            total_cycles += cycles
            ran += 1
            if value == b:
                final_pc = 0x71B7
                break

        if ran == 0:
            return 0

        self.a = final_a
        self.f = final_f
        self.h = h
        self.l = l
        self.pc = final_pc
        self.instructions += total_instructions
        self._add_cycles(total_cycles)
        return total_instructions

    def _fast_forward_pokemon_object_position_helper(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 20
        cycles = 156
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        d = self.d
        e = self.e
        y_address = ((d << 8) | ((e + 2) & 0xFF)) & 0xFFFF
        x_address = ((d << 8) | ((e + 4) & 0xFF)) & 0xFFFF
        snapped_y_address = ((d << 8) | ((e + 8) & 0xFF)) & 0xFFFF
        snapped_x_address = ((d << 8) | ((e + 9) & 0xFF)) & 0xFFFF
        sp = self.sp
        if any(
            not self._is_direct_fast_address(address)
            for address in (
                y_address,
                x_address,
                snapped_y_address,
                snapped_x_address,
                sp,
                (sp + 1) & 0xFFFF,
            )
        ):
            return 0

        y_value = self._read8_direct_fast(y_address)
        x_value = self._read8_direct_fast(x_address)
        if None in {y_value, x_value}:
            return 0
        y_value = int(y_value)
        x_value = int(x_value)
        snapped_y = (y_value + 4) & 0xF0
        snapped_x = x_value & 0xF0

        hram = self.bus.hram
        hram[0xFF92 - 0xFF80] = y_value
        hram[0xFF91 - 0xFF80] = x_value
        if not (
            self._write8_direct_fast(snapped_y_address, snapped_y)
            and self._write8_direct_fast(snapped_x_address, snapped_x)
        ):
            return 0

        return_low = self._read8_direct_fast(sp)
        return_high = self._read8_direct_fast((sp + 1) & 0xFFFF)
        if None in {return_low, return_high}:
            return 0

        self.a = snapped_x
        self.f = FLAG_H | (FLAG_Z if snapped_x == 0 else 0)
        self.e = (e + 9) & 0xFF
        self.sp = (sp + 2) & 0xFFFF
        self.pc = int(return_low) | (int(return_high) << 8)
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_bank3_bit_helper(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        bit_index = self.c & 0x07
        if self.b == 0:
            instruction_count = 33 + bit_index * 4
            cycles = 268 + bit_index * 32
        elif self.b == 0x02:
            instruction_count = 32 + bit_index * 4
            cycles = 256 + bit_index * 32
        else:
            instruction_count = 34 + bit_index * 4
            cycles = 272 + bit_index * 32

        byte_offset = self.c >> 3
        target_address = (self.hl + byte_offset) & 0xFFFF
        if self.l + byte_offset > 0xFF:
            instruction_count += 1

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        sp = self.sp
        stack_addresses = tuple((sp + offset) & 0xFFFF for offset in range(-6, 2))
        if target_address in stack_addresses:
            return 0
        if any(not self._is_direct_fast_address(address) for address in stack_addresses):
            return 0

        return_low = self._read8_direct_fast(sp)
        return_high = self._read8_direct_fast((sp + 1) & 0xFFFF)
        memory_value = self._read8_direct_fast(target_address)
        if None in {return_low, return_high, memory_value}:
            return 0

        memory_value = int(memory_value)
        mask = 1 << bit_index
        write_value: int | None = None
        if self.b == 0:
            result = memory_value & (mask ^ 0xFF)
            final_f = FLAG_H | (FLAG_Z if result == 0 else 0)
            write_value = result
        elif self.b == 0x02:
            result = memory_value & mask
            final_f = FLAG_H | (FLAG_Z if result == 0 else 0)
        else:
            result = memory_value | mask
            final_f = FLAG_Z if result == 0 else 0
            write_value = result

        if not (
            self._write8_direct_fast((sp - 1) & 0xFFFF, self.h)
            and self._write8_direct_fast((sp - 2) & 0xFFFF, self.l)
            and self._write8_direct_fast((sp - 3) & 0xFFFF, self.d)
            and self._write8_direct_fast((sp - 4) & 0xFFFF, self.e)
            and self._write8_direct_fast((sp - 5) & 0xFFFF, self.b)
            and self._write8_direct_fast((sp - 6) & 0xFFFF, self.c)
        ):
            return 0
        if write_value is not None and not self._write8_direct_fast(target_address, write_value):
            return 0

        self.a = result & 0xFF
        self.f = final_f
        self.c = result & 0xFF
        self.sp = (sp + 2) & 0xFFFF
        self.pc = int(return_low) | (int(return_high) << 8)
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_bank_restore_return(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 4
        cycles = 56
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._fast_rom_is_mbc3
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        sp = self.sp
        stack_addresses = tuple((sp + offset) & 0xFFFF for offset in range(4))
        if any(not self._is_direct_fast_address(address) for address in stack_addresses):
            return 0
        popped_f = self._read8_direct_fast(stack_addresses[0])
        popped_a = self._read8_direct_fast(stack_addresses[1])
        return_low = self._read8_direct_fast(stack_addresses[2])
        return_high = self._read8_direct_fast(stack_addresses[3])
        if None in {popped_f, popped_a, return_low, return_high}:
            return 0

        bank = popped_a & 0xFF
        self.f = popped_f & 0xF0
        self.a = bank
        self.bus.hram[0xFFB8 - 0xFF80] = bank
        self.bus.mapper.write_rom_control(0x2000, bank)
        self.sp = (sp + 4) & 0xFFFF
        self.pc = (return_low | (return_high << 8)) & 0xFFFF
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_text_predef_return(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 7
        cycles = 76
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        value = self._read8_direct_fast(0xD12B)
        if value is None or value in {0x02, 0x03, 0x05}:
            return 0
        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        return_low = self._read8_direct_fast(sp)
        return_high = self._read8_direct_fast(sp_next)
        if return_low is None or return_high is None:
            return 0

        self.a = value
        self.f = (
            FLAG_N
            | (FLAG_Z if value == 0x05 else 0)
            | (FLAG_H if (value & 0x0F) < 0x05 else 0)
            | (FLAG_C if value < 0x05 else 0)
        )
        self.sp = (sp + 2) & 0xFFFF
        self.pc = (return_low | (return_high << 8)) & 0xFFFF
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_joypad_poll_loop(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        cycles_per_iteration = 456
        instructions_per_iteration = 42
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instructions_per_iteration
            or not self._fast_rom_is_mbc3
            or not self._can_batch_direct_memory_cycles()
        ):
            return 0

        bank3_offset = 3 * 0x4000
        bank3_bytes = (
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
        )
        data = self._fast_rom_data
        if bank3_offset + len(bank3_bytes) > self._fast_rom_data_len or any(
            data[bank3_offset + index] != value for index, value in enumerate(bank3_bytes)
        ):
            return 0

        bus = self.bus
        hram = bus.hram
        wram = bus.wram
        next_buttons = hram[0xFFF8 - 0xFF80]
        delay = hram[0xFFD5 - 0xFF80]
        if (
            delay == 0
            or next_buttons == 0x0F
            or next_buttons & 0x03
            or wram[0xD730 - 0xC000] & 0x20
            or wram[0xCD6B - 0xC000] != 0
        ):
            return 0

        max_iterations = min(remaining_instructions // instructions_per_iteration, 8)
        safe_cycles = self._fast_forward_safe_cycles(max_iterations * cycles_per_iteration)
        iterations = safe_cycles // cycles_per_iteration
        if iterations <= 0:
            return 0

        old_buttons = hram[0xFFB1 - 0xFF80]
        changed = next_buttons ^ old_buttons
        pressed = changed & next_buttons
        if pressed & 0x03:
            return 0

        final_changed = changed
        final_old_buttons = old_buttons
        if iterations > 1:
            final_changed = 0
            final_old_buttons = next_buttons
        hram[0xFFB2 - 0xFF80] = next_buttons if iterations > 1 else next_buttons & old_buttons
        hram[0xFFB3 - 0xFF80] = 0 if iterations > 1 else pressed
        hram[0xFFB1 - 0xFF80] = next_buttons
        hram[0xFFB4 - 0xFF80] = next_buttons
        old_bank = hram[0xFFB8 - 0xFF80]
        if self._fast_rom_cartridge.mbc3_rom_bank != old_bank:
            bus.mapper.write_rom_control(0x2000, old_bank)

        self.a = delay
        self.b = next_buttons
        self.d = final_changed
        self.e = final_old_buttons
        self.f = FLAG_H
        self.pc = 0x38F6
        instruction_count = iterations * instructions_per_iteration
        self.instructions += instruction_count
        self._add_cycles(iterations * cycles_per_iteration)
        return instruction_count

    def _fast_forward_pokemon_joypad_status_call(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 33
        cycles = 352
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._fast_rom_is_mbc3
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        bus = self.bus
        next_buttons = bus.hram[0xFFF8 - 0xFF80]
        if next_buttons == 0x0F:
            return 0
        if bus.wram[0xD730 - 0xC000] & 0x20:
            return 0
        if bus.wram[0xCD6B - 0xC000] != 0:
            return 0

        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        return_low = self._read8_direct_fast(sp)
        return_high = self._read8_direct_fast(sp_next)
        if return_low is None or return_high is None:
            return 0

        old_bank = bus.hram[0xFFB8 - 0xFF80]
        old_flags = self.f & 0xF0
        old_buttons = bus.hram[0xFFB1 - 0xFF80]
        changed = next_buttons ^ old_buttons
        bus.hram[0xFFB2 - 0xFF80] = next_buttons & old_buttons
        bus.hram[0xFFB3 - 0xFF80] = changed & next_buttons
        bus.hram[0xFFB1 - 0xFF80] = next_buttons
        bus.hram[0xFFB4 - 0xFF80] = next_buttons
        bus.hram[0xFFB8 - 0xFF80] = old_bank
        bus.mapper.write_rom_control(0x2000, old_bank)
        self.a = old_bank
        self.f = old_flags
        self.b = next_buttons
        self.d = changed
        self.e = old_buttons
        self.sp = (sp + 2) & 0xFFFF
        self.pc = (return_low | (return_high << 8)) & 0xFFFF
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_joypad_status_return(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 23
        cycles = 208
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        bus = self.bus
        next_buttons = bus.hram[0xFFF8 - 0xFF80]
        if next_buttons == 0x0F:
            return 0
        if bus.wram[0xD730 - 0xC000] & 0x20:
            return 0
        if bus.wram[0xCD6B - 0xC000] != 0:
            return 0

        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        return_low = self._read8_direct_fast(sp)
        return_high = self._read8_direct_fast(sp_next)
        if return_low is None or return_high is None:
            return 0

        old_buttons = bus.hram[0xFFB1 - 0xFF80]
        changed = next_buttons ^ old_buttons
        bus.hram[0xFFB2 - 0xFF80] = next_buttons & old_buttons
        bus.hram[0xFFB3 - 0xFF80] = changed & next_buttons
        bus.hram[0xFFB1 - 0xFF80] = next_buttons
        bus.hram[0xFFB4 - 0xFF80] = next_buttons
        self.a = 0
        self.b = next_buttons
        self.d = changed
        self.e = old_buttons
        self.f = FLAG_Z | FLAG_H
        self.sp = (sp + 2) & 0xFFFF
        self.pc = (return_low | (return_high << 8)) & 0xFFFF
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_pokemon_text_delay_return(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 11
        cycles = 96
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if (
            remaining_instructions < instruction_count
            or not self._can_batch_direct_memory_cycles()
            or self._fast_forward_safe_cycles(cycles) < cycles
        ):
            return 0

        value = self._read8_direct_fast(self.hl)
        old_counter = self.bus.hram[0xFF8B - 0xFF80]
        if value is None or value == 0xEE or old_counter <= 1:
            return 0
        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        return_low = self._read8_direct_fast(sp)
        return_high = self._read8_direct_fast(sp_next)
        if return_low is None or return_high is None:
            return 0

        next_counter = (old_counter - 1) & 0xFF
        self.bus.hram[0xFF8B - 0xFF80] = next_counter
        self.a = next_counter
        self.b = value
        self.f = FLAG_N | (FLAG_H if (old_counter & 0x0F) == 0 else 0)
        self.sp = (sp + 2) & 0xFFFF
        self.pc = (return_low | (return_high << 8)) & 0xFFFF
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_hot_bitstream_step(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 9
        cycles = 108
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < instruction_count:
            return 0
        if self._fast_forward_safe_cycles(cycles) < cycles:
            return 0
        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        lo = self._read8_direct_fast(sp)
        hi = self._read8_direct_fast(sp_next)
        if lo is None or hi is None:
            return 0

        wram = self.bus.wram
        counter_index = 0xD0A6 - 0xC000
        counter = (wram[counter_index] - 1) & 0xFF
        if counter == 0:
            return 0
        sample_index = 0xD0A5 - 0xC000
        sample = wram[sample_index]
        carry = (sample >> 7) & 1
        sample = ((sample << 1) | carry) & 0xFF
        wram[counter_index] = counter
        wram[sample_index] = sample
        self.a = sample & 0x01
        self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
        self.sp = (sp + 2) & 0xFFFF
        self.pc = lo | (hi << 8)
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_hot_counter_step(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        instruction_count = 11
        cycles = 124
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < instruction_count:
            return 0
        if self._fast_forward_safe_cycles(cycles) < cycles:
            return 0
        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        lo = self._read8_direct_fast(sp)
        hi = self._read8_direct_fast(sp_next)
        if lo is None or hi is None:
            return 0

        wram = self.bus.wram
        limit = wram[0xD0A4 - 0xC000]
        next_index = (wram[0xD0A2 - 0xC000] + 1) & 0xFF
        if next_index == limit:
            return 0
        old_low_address = wram[0xD0AD - 0xC000]
        next_low_address = (old_low_address + 1) & 0xFF
        if next_low_address == 0:
            return 0

        wram[0xD0A2 - 0xC000] = next_index
        wram[0xD0AD - 0xC000] = next_low_address
        self.a = next_low_address
        self.b = limit
        self.f = (
            (FLAG_H if (old_low_address & 0x0F) == 0x0F else 0)
            | (FLAG_C if next_index < limit else 0)
        )
        self.sp = (sp + 2) & 0xFFFF
        self.pc = lo | (hi << 8)
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_hot_copy_loop(
        self,
        max_instructions: int | None,
        steps: int,
        *,
        duplicate: bool,
    ) -> int:
        if self.bc <= 1:
            return 0
        if self.bus.ppu.lcd_enabled or not self._can_batch_direct_memory_cycles():
            return 0
        cycles_per_iteration = 68 if duplicate else 52
        instructions_per_iteration = 9 if duplicate else 7
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        iterations = min(
            self.bc - 1,
            4096 // cycles_per_iteration,
            remaining_instructions // instructions_per_iteration,
        )
        if iterations <= 0:
            return 0
        safe_cycles = self._fast_forward_safe_cycles(iterations * cycles_per_iteration)
        iterations = min(iterations, safe_cycles // cycles_per_iteration)
        if iterations <= 0:
            return 0

        source = self.hl
        destination = self.de
        for offset in range(iterations):
            value = self._read8_direct_fast((source + offset) & 0xFFFF, 1)
            if value is None:
                return 0
            write_address = (destination + (offset * 2 if duplicate else offset)) & 0xFFFF
            if not self._write8_direct_fast(write_address, value, 1):
                return 0
            if duplicate and not self._write8_direct_fast((write_address + 1) & 0xFFFF, value, 1):
                return 0

        self.hl = (source + iterations) & 0xFFFF
        self.de = (destination + iterations * (2 if duplicate else 1)) & 0xFFFF
        self.bc = (self.bc - iterations) & 0xFFFF
        self.a = (self.b | self.c) & 0xFF
        self.f = 0
        instruction_count = iterations * instructions_per_iteration
        self.instructions += instruction_count
        self._add_cycles(iterations * cycles_per_iteration)
        return instruction_count

    def _fast_forward_hot_fill_loop(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        if self.bc <= 1:
            return 0
        if self.bus.ppu.lcd_enabled or not self._can_batch_direct_memory_cycles():
            return 0
        cycles_per_iteration = 40
        instructions_per_iteration = 6
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        iterations = min(
            self.bc - 1,
            4096 // cycles_per_iteration,
            remaining_instructions // instructions_per_iteration,
        )
        if iterations <= 0:
            return 0
        safe_cycles = self._fast_forward_safe_cycles(iterations * cycles_per_iteration)
        iterations = min(iterations, safe_cycles // cycles_per_iteration)
        if iterations <= 0:
            return 0

        address = self.hl
        value = self.d
        for offset in range(iterations):
            if not self._write8_direct_fast((address + offset) & 0xFFFF, value, 1):
                return 0

        self.hl = (address + iterations) & 0xFFFF
        self.bc = (self.bc - iterations) & 0xFFFF
        self.a = (self.b | self.c) & 0xFF
        self.f = 0
        instruction_count = iterations * instructions_per_iteration
        self.instructions += instruction_count
        self._add_cycles(iterations * cycles_per_iteration)
        return instruction_count

    def _fast_forward_hot_zero_mask_loop(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        wram = self.bus.wram
        de_value = self.de
        if de_value <= 1:
            return 0
        mode = wram[0xD0A7 - 0xC000]
        if mode == 0:
            or_cycles = 112
            or_instructions = 12
        elif mode == 1:
            or_cycles = 156
            or_instructions = 17
        elif mode == 2:
            or_cycles = 144
            or_instructions = 16
        else:
            or_cycles = 160
            or_instructions = 18

        limit = wram[0xD0A4 - 0xC000]
        counter = wram[0xD0A2 - 0xC000]
        low_address = wram[0xD0AD - 0xC000]
        high_address = wram[0xD0AE - 0xC000]
        first_address = low_address | (high_address << 8)
        if not self._is_direct_fast_address(first_address):
            return 0

        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        max_iterations = min(de_value - 1, 16)
        total_cycles = 0
        instruction_count = 0
        iterations = 0
        next_de = de_value
        next_counter = counter
        next_low_address = low_address
        last_address = first_address
        while iterations < max_iterations:
            candidate_counter = (next_counter + 1) & 0xFF
            if candidate_counter == limit:
                break
            candidate_low_address = (next_low_address + 1) & 0xFF
            if candidate_low_address == 0:
                break
            candidate_de = (next_de - 1) & 0xFFFF
            if candidate_de == 0:
                break
            last_address = next_low_address | (high_address << 8)
            if not self._is_direct_fast_address(last_address):
                break
            loop_cycles = or_cycles + (224 if (candidate_de >> 8) else 228)
            loop_instructions = or_instructions + (21 if (candidate_de >> 8) else 23)
            if total_cycles + loop_cycles > 4096:
                break
            if instruction_count + loop_instructions > remaining_instructions:
                break
            total_cycles += loop_cycles
            instruction_count += loop_instructions
            iterations += 1
            next_de = candidate_de
            next_counter = candidate_counter
            next_low_address = candidate_low_address

        if iterations == 0:
            return 0
        safe_cycles = self._fast_forward_safe_cycles(total_cycles)
        while iterations and total_cycles > safe_cycles:
            loop_cycles = or_cycles + (224 if (next_de >> 8) else 228)
            loop_instructions = or_instructions + (21 if (next_de >> 8) else 23)
            total_cycles -= loop_cycles
            instruction_count -= loop_instructions
            next_de = (next_de + 1) & 0xFFFF
            next_counter = (next_counter - 1) & 0xFF
            next_low_address = (next_low_address - 1) & 0xFF
            iterations -= 1
        if iterations == 0:
            return 0
        last_address = ((next_low_address - 1) & 0xFF) | (high_address << 8)

        wram[0xD0A2 - 0xC000] = next_counter
        wram[0xD0AD - 0xC000] = next_low_address
        self.de = next_de
        self.b = limit
        self.h = (last_address >> 8) & 0xFF
        self.l = last_address & 0xFF
        self.a = self.d if self.d != 0 else self.e
        self.f = FLAG_H
        self.pc = 0x25C4
        self.instructions += instruction_count
        self._add_cycles(total_cycles)
        return instruction_count

    def _fast_forward_hot_nibble_fetch_step(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        wram = self.bus.wram
        if wram[0xD0AA - 0xC000] != 0:
            return 0

        old_a = self.a
        shifted = old_a >> 1
        use_high_table = bool(self.e & 0x01)
        base_address = (
            wram[0xD0B3 - 0xC000] | (wram[0xD0B4 - 0xC000] << 8)
            if use_high_table
            else wram[0xD0B1 - 0xC000] | (wram[0xD0B2 - 0xC000] << 8)
        )
        address = (base_address + shifted) & 0xFFFF
        address_carry = ((base_address & 0xFF) + shifted) > 0xFF
        cycles = 212 if use_high_table else 220
        instruction_count = (25 if use_high_table else 26) + int(address_carry)
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < instruction_count:
            return 0
        if self._fast_forward_safe_cycles(cycles) < cycles:
            return 0
        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        lo = self._read8_direct_fast(sp)
        hi = self._read8_direct_fast(sp_next)
        if lo is None or hi is None:
            return 0
        value = self._read8_direct_fast(address, 8)
        if value is None:
            return 0

        if old_a & 0x01:
            nibble = value & 0x0F
        else:
            nibble = (value >> 4) & 0x0F
        self.a = nibble
        self.c = old_a & 0x01
        self.e = nibble
        self.h = (address >> 8) & 0xFF
        self.l = address & 0xFF
        self.f = (FLAG_Z if nibble == 0 else 0) | FLAG_H
        self.sp = (sp + 2) & 0xFFFF
        self.pc = lo | (hi << 8)
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_hot_or_mask_step(
        self,
        max_instructions: int | None,
        steps: int,
    ) -> int:
        wram = self.bus.wram
        mode = wram[0xD0A7 - 0xC000]
        if mode == 0:
            cycles = 112
            instruction_count = 12
            mask = self.a
        elif mode == 1:
            cycles = 156
            instruction_count = 17
            mask = (self.a << 2) & 0xFF
        elif mode == 2:
            cycles = 144
            instruction_count = 16
            mask = ((self.a & 0x0F) << 4) | (self.a >> 4)
        else:
            cycles = 160
            instruction_count = 18
            mask = ((self.a >> 2) | ((self.a & 0x03) << 6)) & 0xFF
        remaining_instructions = (
            (1 << 20) if max_instructions is None else max_instructions - steps
        )
        if remaining_instructions < instruction_count:
            return 0
        if self._fast_forward_safe_cycles(cycles) < cycles:
            return 0
        sp = self.sp
        sp_next = (sp + 1) & 0xFFFF
        if not self._is_direct_fast_address(sp) or not self._is_direct_fast_address(sp_next):
            return 0
        lo = self._read8_direct_fast(sp)
        hi = self._read8_direct_fast(sp_next)
        if lo is None or hi is None:
            return 0

        address = wram[0xD0AD - 0xC000] | (wram[0xD0AE - 0xC000] << 8)
        value = self._read8_direct_fast(address, 8)
        if value is None:
            return 0
        result = value | mask
        if not self._write8_direct_fast(address, result, 8):
            return 0
        self.a = result
        self.e = mask
        self.h = (address >> 8) & 0xFF
        self.l = address & 0xFF
        self.f = FLAG_Z if result == 0 else 0
        self.sp = (sp + 2) & 0xFFFF
        self.pc = lo | (hi << 8)
        self.instructions += instruction_count
        self._add_cycles(cycles)
        return instruction_count

    def _fast_forward_safe_cycles(self, max_cycles: int) -> int:
        if max_cycles <= 0:
            return 0
        if self.ime:
            enabled_interrupts = self.bus.ie & 0x1F
            stat_may_interrupt = bool(
                enabled_interrupts & 0x02 and self.bus.io[0x41] & 0x78
            )
            timer_may_interrupt = bool(
                enabled_interrupts & 0x04 and self.bus.io[0x07] & 0x04
            )
            serial_may_interrupt = bool(
                enabled_interrupts & 0x08 and getattr(self.bus, "_serial_transfer_cycles", 0)
            )
            joypad_may_interrupt = bool(enabled_interrupts & 0x10)
            if stat_may_interrupt or timer_may_interrupt or serial_may_interrupt or joypad_may_interrupt:
                return self.bus.cycles_until_next_interrupt_event(max_cycles)
            if enabled_interrupts & 0x01:
                return self._cycles_until_vblank_or_frame_boundary(max_cycles)
        return self._cycles_until_frame_boundary(max_cycles)

    def _cycles_until_vblank_or_frame_boundary(self, max_cycles: int) -> int:
        ppu = getattr(self.bus, "ppu", None)
        if ppu is None or not getattr(ppu, "lcd_enabled", False):
            return max_cycles
        scanline = getattr(ppu, "_scanline", 0)
        line_dots = getattr(ppu, "line_dots", 0)
        if scanline < PPU_VBLANK_LINE:
            cycles = (PPU_VBLANK_LINE - scanline) * PPU_DOTS_PER_LINE - line_dots
            cpu_cycles = self.bus.cpu_cycles_for_device_cycles(cycles)
            return max(1, min(max_cycles, cpu_cycles))
        return self._cycles_until_frame_boundary(max_cycles)

    def _cycles_until_frame_boundary(self, max_cycles: int) -> int:
        ppu = getattr(self.bus, "ppu", None)
        if ppu is None or not getattr(ppu, "lcd_enabled", False):
            return max_cycles
        scanline = getattr(ppu, "_scanline", 0)
        line_dots = getattr(ppu, "line_dots", 0)
        cycles = (PPU_LINES_PER_FRAME - scanline) * PPU_DOTS_PER_LINE - line_dots
        cpu_cycles = self.bus.cpu_cycles_for_device_cycles(cycles)
        return max(1, min(max_cycles, cpu_cycles))

    def _matches_bytes(self, address: int, values: tuple[int, ...]) -> bool:
        address &= 0xFFFF
        end_address = address + len(values) - 1
        bus = self.bus
        if (
            self._fast_rom_is_mbc3
            and end_address <= 0x7FFF
            and not bus._oam_dma_active
            and not (bus.boot_rom_enabled and address < len(bus.boot_rom))
        ):
            data = self._fast_rom_data
            if address < 0x4000:
                if end_address >= 0x4000:
                    return False
                if end_address >= self._fast_rom_data_len:
                    return False
                offset = address
            elif address >= 0x4000:
                offset = self._fast_rom_cartridge._mbc3_rom_bank_offset + (address - 0x4000)
            else:
                return False
            for index, value in enumerate(values):
                if data[offset + index] != value:
                    return False
            return True
        for index, value in enumerate(values):
            if bus.read8((address + index) & 0xFFFF) != value:
                return False
        return True

    def _can_fast_fetch_nop_at(self, address: int) -> bool:
        address &= 0xFFFF
        if not (
            address <= 0x7FFF
            or 0xC000 <= address <= 0xFDFF
            or 0xFF80 <= address <= 0xFFFE
        ):
            return False
        return self.bus.read8(address) == 0x00

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

    def _execute_prefetched_fast(self, opcode: int) -> int:
        pc = self.pc

        if 0x40 <= opcode <= 0x7F and opcode != 0x76:
            src = opcode & 0x07
            dst = (opcode >> 3) & 0x07
            if src != 0x06 and dst != 0x06:
                if src == 0:
                    value = self.b
                elif src == 1:
                    value = self.c
                elif src == 2:
                    value = self.d
                elif src == 3:
                    value = self.e
                elif src == 4:
                    value = self.h
                elif src == 5:
                    value = self.l
                else:
                    value = self.a

                if dst == 0:
                    self.b = value
                elif dst == 1:
                    self.c = value
                elif dst == 2:
                    self.d = value
                elif dst == 3:
                    self.e = value
                elif dst == 4:
                    self.h = value
                elif dst == 5:
                    self.l = value
                else:
                    self.a = value
                self.pc = (pc + 1) & 0xFFFF
                self._add_cycles(4)
                return 4

        if 0x80 <= opcode <= 0xBF and (opcode & 0x07) != 0x06:
            src = opcode & 0x07
            if src == 0:
                value = self.b
            elif src == 1:
                value = self.c
            elif src == 2:
                value = self.d
            elif src == 3:
                value = self.e
            elif src == 4:
                value = self.h
            elif src == 5:
                value = self.l
            else:
                value = self.a

            operation = (opcode >> 3) & 0x07
            a = self.a
            if operation == 0:
                result = a + value
                self.a = result & 0xFF
                self.f = (
                    (FLAG_Z if self.a == 0 else 0)
                    | (FLAG_H if ((a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                    | (FLAG_C if result > 0xFF else 0)
                )
            elif operation == 1:
                carry = 1 if self.f & FLAG_C else 0
                result = a + value + carry
                self.a = result & 0xFF
                self.f = (
                    (FLAG_Z if self.a == 0 else 0)
                    | (FLAG_H if ((a & 0x0F) + (value & 0x0F) + carry) > 0x0F else 0)
                    | (FLAG_C if result > 0xFF else 0)
                )
            elif operation == 2:
                result = a - value
                self.a = result & 0xFF
                self.f = (
                    FLAG_N
                    | (FLAG_Z if self.a == 0 else 0)
                    | (FLAG_H if (a & 0x0F) < (value & 0x0F) else 0)
                    | (FLAG_C if a < value else 0)
                )
            elif operation == 3:
                carry = 1 if self.f & FLAG_C else 0
                result = a - value - carry
                self.a = result & 0xFF
                self.f = (
                    FLAG_N
                    | (FLAG_Z if self.a == 0 else 0)
                    | (FLAG_H if (a & 0x0F) < ((value & 0x0F) + carry) else 0)
                    | (FLAG_C if a < value + carry else 0)
                )
            elif operation == 4:
                self.a = a & value
                self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            elif operation == 5:
                self.a = a ^ value
                self.f = FLAG_Z if self.a == 0 else 0
            elif operation == 6:
                self.a = a | value
                self.f = FLAG_Z if self.a == 0 else 0
            else:
                result = a - value
                self.f = (
                    FLAG_N
                    | (FLAG_Z if (result & 0xFF) == 0 else 0)
                    | (FLAG_H if (a & 0x0F) < (value & 0x0F) else 0)
                    | (FLAG_C if a < value else 0)
                )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4

        if opcode == 0xFA and pc <= 0x7FFD:
            bus = self.bus
            lo = self._read8_fast(pc + 1)
            hi = self._read8_fast(pc + 2)
            address = lo | (hi << 8)
            can_batch = self._can_batch_direct_memory_cycles()
            value = self._read8_direct_fast(address, 16) if can_batch else None
            if value is not None:
                self.a = value
                self.pc = (pc + 3) & 0xFFFF
                self._add_cycles(16)
                return 16
            self._add_cycles(12)
            self.pc = (pc + 3) & 0xFFFF
            self.a = bus.read8(address)
            self._add_cycles(4)
            return 16
        if opcode == 0xEA and pc <= 0x7FFD:
            bus = self.bus
            lo = self._read8_fast(pc + 1)
            hi = self._read8_fast(pc + 2)
            address = lo | (hi << 8)
            if self._can_batch_direct_memory_cycles() and self._write8_direct_fast(address, self.a, 16):
                self.pc = (pc + 3) & 0xFFFF
                self._add_cycles(16)
                return 16
            self._add_cycles(12)
            self.pc = (pc + 3) & 0xFFFF
            bus.write8(address, self.a)
            self._add_cycles(4)
            return 16
        if opcode in {0x20, 0x28, 0x30, 0x38, 0x18} and pc <= 0x7FFE:
            offset = self._read8_fast(pc + 1)
            next_pc = (pc + 2) & 0xFFFF
            taken = (
                opcode == 0x18
                or (opcode == 0x20 and not self.f & FLAG_Z)
                or (opcode == 0x28 and self.f & FLAG_Z)
                or (opcode == 0x30 and not self.f & FLAG_C)
                or (opcode == 0x38 and self.f & FLAG_C)
            )
            self.pc = (
                (next_pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                if taken
                else next_pc
            )
            cycles = 12 if taken else 8
            self._add_cycles(cycles)
            return cycles
        if opcode == 0xCB and pc <= 0x7FFE:
            cb = self._read8_fast(pc + 1)
            if self._can_batch_direct_memory_cycles():
                cycles = self._execute_cb_fast_register(cb)
                if cycles:
                    self.pc = (pc + 2) & 0xFFFF
                    self._add_cycles(cycles)
                    return cycles
            if cb & 0x07 == 0x06:
                cycles = self._execute_cb_fast_hl(cb, pc)
                if cycles:
                    return cycles
        if opcode == 0xCD and pc <= 0x7FFD and self._can_batch_direct_memory_cycles():
            sp_high = (self.sp - 1) & 0xFFFF
            sp_low = (self.sp - 2) & 0xFFFF
            if self._is_direct_fast_address(sp_high) and self._is_direct_fast_address(sp_low):
                lo = self._read8_fast(pc + 1)
                hi = self._read8_fast(pc + 2)
                return_address = (pc + 3) & 0xFFFF
                self._write8_direct_fast(sp_high, (return_address >> 8) & 0xFF)
                self._write8_direct_fast(sp_low, return_address & 0xFF)
                self.sp = sp_low
                self.pc = lo | (hi << 8)
                self._add_cycles(24)
                return 24
        if opcode == 0xC9 and self._can_batch_direct_memory_cycles():
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                if lo is not None and hi is not None:
                    self.sp = (sp + 2) & 0xFFFF
                    self.pc = lo | (hi << 8)
                    self._add_cycles(16)
                    return 16

        if opcode == 0xA7:
            self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x6F:
            self.l = self.a
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode in {0xE6, 0xFE} and pc <= 0x7FFE and self._can_batch_direct_memory_cycles():
            value = self._read8_fast(pc + 1)
            if opcode == 0xE6:
                self.a &= value
                self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            else:
                result = self.a - value
                self.f = (
                    FLAG_N
                    | (FLAG_Z if (result & 0xFF) == 0 else 0)
                    | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                    | (FLAG_C if self.a < value else 0)
                )
            self.pc = (pc + 2) & 0xFFFF
            self._add_cycles(8)
            return 8
        if opcode == 0x47:
            self.b = self.a
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x67:
            self.h = self.a
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode in {0x7E, 0x77} and self._can_batch_direct_memory_cycles():
            address = (self.h << 8) | self.l
            if opcode == 0x7E:
                value = self._read8_direct_fast(address, 8)
                if value is None:
                    return self._execute_memory_exact_fast(opcode, pc)
                self.a = value
            elif not self._write8_direct_fast(address, self.a, 8):
                return self._execute_memory_exact_fast(opcode, pc)
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(8)
            return 8
        if opcode == 0x3C:
            value = self.a
            result = (value + 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x2C:
            value = self.l
            result = (value + 1) & 0xFF
            self.l = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x21 and pc <= 0x7FFD and self._can_batch_direct_memory_cycles():
            self.l = self._read8_fast(pc + 1)
            self.h = self._read8_fast(pc + 2)
            self.pc = (pc + 3) & 0xFFFF
            self._add_cycles(12)
            return 12
        if opcode == 0xD1 and self._can_batch_direct_memory_cycles():
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                if lo is not None and hi is not None:
                    self.sp = (sp + 2) & 0xFFFF
                    self.d = hi
                    self.e = lo
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(12)
                    return 12
        if opcode == 0xC0:
            if self.f & FLAG_Z:
                if self._can_batch_direct_memory_cycles():
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(8)
                    return 8
            elif self._can_batch_direct_memory_cycles():
                sp = self.sp
                sp_next = (sp + 1) & 0xFFFF
                if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                    lo = self._read8_direct_fast(sp)
                    hi = self._read8_direct_fast(sp_next)
                    if lo is not None and hi is not None:
                        self.sp = (sp + 2) & 0xFFFF
                        self.pc = lo | (hi << 8)
                        self._add_cycles(20)
                        return 20
        if opcode == 0x5F:
            self.e = self.a
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x78:
            self.a = self.b
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x7A:
            self.a = self.d
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x7B:
            self.a = self.e
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x43:
            self.b = self.e
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x58:
            self.e = self.b
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xAF:
            self.a = 0
            self.f = FLAG_Z
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x07:
            carry = (self.a >> 7) & 1
            self.a = ((self.a << 1) | carry) & 0xFF
            self.f = FLAG_C if carry else 0
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xB3:
            self.a |= self.e
            self.f = FLAG_Z if self.a == 0 else 0
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x3C:
            value = self.a
            result = (value + 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x2C:
            value = self.l
            result = (value + 1) & 0xFF
            self.l = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x24:
            value = self.h
            result = (value + 1) & 0xFF
            self.h = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x3D:
            value = self.a
            result = (value - 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xB8:
            value = self.b
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xBD:
            value = self.l
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x85:
            value = self.l
            result = self.a + value
            self.f = (
                (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                | (FLAG_C if result > 0xFF else 0)
            )
            self.a = result & 0xFF
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4

        if opcode == 0xB1:
            self.a |= self.c
            self.f = FLAG_Z if self.a == 0 else 0
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x5D:
            self.e = self.l
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x79:
            self.a = self.c
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x7C:
            self.a = self.h
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x4F:
            self.c = self.a
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x0D:
            value = self.c
            result = (value - 1) & 0xFF
            self.c = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x0C:
            value = self.c
            result = (value + 1) & 0xFF
            self.c = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x57:
            self.d = self.a
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xB2:
            self.a |= self.d
            self.f = FLAG_Z if self.a == 0 else 0
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x7D:
            self.a = self.l
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x87:
            value = self.a
            result = value + value
            self.a = result & 0xFF
            self.f = (
                (FLAG_Z if self.a == 0 else 0)
                | (FLAG_H if ((value & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                | (FLAG_C if result > 0xFF else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x05:
            value = self.b
            result = (value - 1) & 0xFF
            self.b = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x54:
            self.d = self.h
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xA8:
            self.a ^= self.b
            self.f = FLAG_Z if self.a == 0 else 0
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x53:
            self.d = self.e
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0x2F:
            self.f = (self.f & (FLAG_Z | FLAG_C)) | FLAG_N | FLAG_H
            self.a ^= 0xFF
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xB0:
            self.a |= self.b
            self.f = FLAG_Z if self.a == 0 else 0
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(4)
            return 4
        if opcode == 0xE9:
            self.pc = (self.h << 8) | self.l
            self._add_cycles(4)
            return 4

        can_batch = self._can_batch_direct_memory_cycles()
        bus = self.bus
        if opcode == 0xCB and pc <= 0x7FFE:
            cb = self._read8_fast(pc + 1)
            if can_batch:
                cycles = self._execute_cb_fast_register(cb)
                if cycles:
                    self.pc = (pc + 2) & 0xFFFF
                    self._add_cycles(cycles)
                    return cycles
            if cb & 0x07 == 0x06:
                cycles = self._execute_cb_fast_hl(cb, pc)
                if cycles:
                    return cycles

        if opcode == 0xFA and pc <= 0x7FFD:
            lo = self._read8_fast(pc + 1)
            hi = self._read8_fast(pc + 2)
            address = lo | (hi << 8)
            value = self._read8_direct_fast(address, 16) if can_batch else None
            if value is not None:
                self.a = value
                self.pc = (pc + 3) & 0xFFFF
                self._add_cycles(16)
                return 16
            self._add_cycles(12)
            self.pc = (pc + 3) & 0xFFFF
            self.a = bus.read8(address)
            self._add_cycles(4)
            return 16

        if opcode == 0xEA and pc <= 0x7FFD:
            lo = self._read8_fast(pc + 1)
            hi = self._read8_fast(pc + 2)
            address = lo | (hi << 8)
            if can_batch and self._write8_direct_fast(address, self.a, 16):
                self.pc = (pc + 3) & 0xFFFF
                self._add_cycles(16)
                return 16
            self._add_cycles(12)
            self.pc = (pc + 3) & 0xFFFF
            bus.write8(address, self.a)
            self._add_cycles(4)
            return 16
        if opcode == 0xCD and can_batch and pc <= 0x7FFD:
            sp_high = (self.sp - 1) & 0xFFFF
            sp_low = (self.sp - 2) & 0xFFFF
            if self._is_direct_fast_address(sp_high) and self._is_direct_fast_address(sp_low):
                lo = self._read8_fast(pc + 1)
                hi = self._read8_fast(pc + 2)
                return_address = (pc + 3) & 0xFFFF
                self._write8_direct_fast(sp_high, (return_address >> 8) & 0xFF)
                self._write8_direct_fast(sp_low, return_address & 0xFF)
                self.sp = sp_low
                self.pc = lo | (hi << 8)
                self._add_cycles(24)
                return 24
        if opcode == 0xC9 and can_batch:
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                if lo is not None and hi is not None:
                    self.sp = (sp + 2) & 0xFFFF
                    self.pc = lo | (hi << 8)
                    self._add_cycles(16)
                    return 16
        if opcode == 0xC0:
            if self.f & FLAG_Z:
                if can_batch:
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(8)
                    return 8
                return 0
            if can_batch:
                sp = self.sp
                sp_next = (sp + 1) & 0xFFFF
                if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                    lo = self._read8_direct_fast(sp)
                    hi = self._read8_direct_fast(sp_next)
                    if lo is not None and hi is not None:
                        self.sp = (sp + 2) & 0xFFFF
                        self.pc = lo | (hi << 8)
                        self._add_cycles(20)
                        return 20
        if opcode == 0xD1 and can_batch:
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                if lo is not None and hi is not None:
                    self.sp = (sp + 2) & 0xFFFF
                    self.d = hi
                    self.e = lo
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(12)
                    return 12

        if opcode in {0xC1, 0xE1, 0xF1} and can_batch:
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                if lo is not None and hi is not None:
                    self.sp = (sp + 2) & 0xFFFF
                    if opcode == 0xC1:
                        self.b = hi
                        self.c = lo
                    elif opcode == 0xE1:
                        self.h = hi
                        self.l = lo
                    else:
                        self.a = hi
                        self.f = lo & 0xF0
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(12)
                    return 12
        if opcode in {0xC5, 0xD5, 0xE5, 0xF5} and can_batch:
            sp_high = (self.sp - 1) & 0xFFFF
            sp_low = (self.sp - 2) & 0xFFFF
            if self._is_direct_fast_address(sp_high) and self._is_direct_fast_address(sp_low):
                if opcode == 0xC5:
                    hi = self.b
                    lo = self.c
                elif opcode == 0xD5:
                    hi = self.d
                    lo = self.e
                elif opcode == 0xE5:
                    hi = self.h
                    lo = self.l
                else:
                    hi = self.a
                    lo = self.f & 0xF0
                self._write8_direct_fast(sp_high, hi)
                self._write8_direct_fast(sp_low, lo)
                self.sp = sp_low
                self.pc = (pc + 1) & 0xFFFF
                self._add_cycles(16)
                return 16

        if opcode == 0xC8:
            if not self.f & FLAG_Z:
                if can_batch:
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(8)
                    return 8
                return 0
            if can_batch:
                sp = self.sp
                sp_next = (sp + 1) & 0xFFFF
                if self._is_direct_fast_address(sp) and self._is_direct_fast_address(sp_next):
                    lo = self._read8_direct_fast(sp)
                    hi = self._read8_direct_fast(sp_next)
                    if lo is not None and hi is not None:
                        self.sp = (sp + 2) & 0xFFFF
                        self.pc = lo | (hi << 8)
                        self._add_cycles(20)
                        return 20

        if opcode in {0xC2, 0xCA, 0xC3} and can_batch and pc <= 0x7FFD:
            lo = self._read8_fast(pc + 1)
            hi = self._read8_fast(pc + 2)
            address = lo | (hi << 8)
            taken = (
                opcode == 0xC3
                or (opcode == 0xC2 and not self.f & FLAG_Z)
                or (opcode == 0xCA and self.f & FLAG_Z)
            )
            self.pc = address if taken else (pc + 3) & 0xFFFF
            cycles = 16 if taken else 12
            self._add_cycles(cycles)
            return cycles

        if opcode == 0xF9 and can_batch:
            self.sp = (self.h << 8) | self.l
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(8)
            return 8

        if opcode in {0x20, 0x28, 0x30, 0x38, 0x18} and can_batch and pc <= 0x7FFE:
            offset = self._read8_fast(pc + 1)
            next_pc = (pc + 2) & 0xFFFF
            taken = (
                opcode == 0x18
                or (opcode == 0x20 and not self.f & FLAG_Z)
                or (opcode == 0x28 and self.f & FLAG_Z)
                or (opcode == 0x30 and not self.f & FLAG_C)
                or (opcode == 0x38 and self.f & FLAG_C)
            )
            self.pc = (
                (next_pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                if taken
                else next_pc
            )
            cycles = 12 if taken else 8
            self._add_cycles(cycles)
            return cycles

        if opcode in {0xF0, 0xE0} and pc <= 0x7FFE:
            offset = self._read8_fast(pc + 1)
            address = 0xFF00 | offset
            self._add_cycles(8)
            self.pc = (pc + 2) & 0xFFFF
            if opcode == 0xF0:
                self.a = bus.read8(address)
            else:
                bus.write8(address, self.a)
            self._add_cycles(4)
            return 12

        if opcode in {0xE6, 0xFE, 0xC6, 0x0E, 0x3E, 0x06, 0x16, 0x1E, 0x26, 0xF8} and can_batch and pc <= 0x7FFE:
            value = self._read8_fast(pc + 1)
            if opcode == 0xE6:
                self.a &= value
                self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            elif opcode == 0xFE:
                result = self.a - value
                self.f = (
                    FLAG_N
                    | (FLAG_Z if (result & 0xFF) == 0 else 0)
                    | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                    | (FLAG_C if self.a < value else 0)
                )
            elif opcode == 0xC6:
                result = self.a + value
                self.f = (
                    (FLAG_Z if (result & 0xFF) == 0 else 0)
                    | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                    | (FLAG_C if result > 0xFF else 0)
                )
                self.a = result & 0xFF
            elif opcode == 0x0E:
                self.c = value
            elif opcode == 0x3E:
                self.a = value
            elif opcode == 0x06:
                self.b = value
            elif opcode == 0x16:
                self.d = value
            elif opcode == 0x1E:
                self.e = value
            elif opcode == 0x26:
                self.h = value
            else:
                sp = self.sp
                offset = value - 0x100 if value & 0x80 else value
                result = (sp + offset) & 0xFFFF
                self.h = (result >> 8) & 0xFF
                self.l = result & 0xFF
                self.f = (
                    (FLAG_H if ((sp & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                    | (FLAG_C if ((sp & 0xFF) + value) > 0xFF else 0)
                )
            self.pc = (pc + 2) & 0xFFFF
            self._add_cycles(8)
            cycles = 12 if opcode == 0xF8 else 8
            if cycles == 12:
                self._add_cycles(4)
            return cycles

        if opcode == 0x21 and can_batch and pc <= 0x7FFD:
            self.l = self._read8_fast(pc + 1)
            self.h = self._read8_fast(pc + 2)
            self.pc = (pc + 3) & 0xFFFF
            self._add_cycles(12)
            return 12

        if opcode in {0x01, 0x11} and can_batch and pc <= 0x7FFD:
            lo = self._read8_fast(pc + 1)
            hi = self._read8_fast(pc + 2)
            if opcode == 0x01:
                self.b = hi
                self.c = lo
            else:
                self.d = hi
                self.e = lo
            self.pc = (pc + 3) & 0xFFFF
            self._add_cycles(12)
            return 12

        if opcode in {0x7E, 0x77, 0x72, 0x73, 0x22, 0x12, 0x1A, 0x2A, 0x0A, 0x32, 0x56, 0x35} and can_batch:
            address = (self.h << 8) | self.l
            if opcode in {0x12, 0x1A}:
                address = (self.d << 8) | self.e
            elif opcode == 0x0A:
                address = (self.b << 8) | self.c
            if opcode in {0x7E, 0x1A, 0x2A, 0x0A, 0x56}:
                value = self._read8_direct_fast(address, 8)
                if value is None:
                    return self._execute_memory_exact_fast(opcode, pc)
                if opcode == 0x56:
                    self.d = value
                else:
                    self.a = value
                if opcode == 0x2A:
                    address = (address + 1) & 0xFFFF
                    self.h = (address >> 8) & 0xFF
                    self.l = address & 0xFF
            elif opcode == 0x35:
                value = self._read8_direct_fast(address, 12)
                if value is None:
                    return self._execute_memory_exact_fast(opcode, pc)
                result = (value - 1) & 0xFF
                if not self._write8_direct_fast(address, result, 12):
                    return self._execute_memory_exact_fast(opcode, pc)
                self.f = (
                    (self.f & FLAG_C)
                    | FLAG_N
                    | (FLAG_Z if result == 0 else 0)
                    | (FLAG_H if (value & 0x0F) == 0x00 else 0)
                )
            else:
                value = self.a
                if opcode == 0x72:
                    value = self.d
                elif opcode == 0x73:
                    value = self.e
                if not self._write8_direct_fast(address, value, 8):
                    return self._execute_memory_exact_fast(opcode, pc)
                if opcode == 0x22:
                    address = (address + 1) & 0xFFFF
                    self.h = (address >> 8) & 0xFF
                    self.l = address & 0xFF
                elif opcode == 0x32:
                    address = (address - 1) & 0xFFFF
                    self.h = (address >> 8) & 0xFF
                    self.l = address & 0xFF
            self.pc = (pc + 1) & 0xFFFF
            cycles = 12 if opcode == 0x35 else 8
            self._add_cycles(cycles)
            return cycles

        if opcode in {0x7E, 0x77, 0x72, 0x73, 0x22, 0x12, 0x1A, 0x2A, 0x0A, 0x32, 0x56, 0x35, 0x3A, 0x5E, 0x6E, 0x46, 0x70, 0x86, 0xA6, 0xB6, 0x34}:
            return self._execute_memory_exact_fast(opcode, pc)

        if opcode in {0x0B, 0x1B, 0x09, 0x19, 0x13, 0x23} and can_batch:
            if opcode == 0x0B:
                value = ((self.b << 8) | self.c) - 1
                self.b = (value >> 8) & 0xFF
                self.c = value & 0xFF
            elif opcode == 0x1B:
                value = ((self.d << 8) | self.e) - 1
                self.d = (value >> 8) & 0xFF
                self.e = value & 0xFF
            elif opcode == 0x09:
                hl = (self.h << 8) | self.l
                value = (self.b << 8) | self.c
                result = hl + value
                self.f = (
                    (self.f & FLAG_Z)
                    | (FLAG_H if ((hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF else 0)
                    | (FLAG_C if result > 0xFFFF else 0)
                )
                self.h = (result >> 8) & 0xFF
                self.l = result & 0xFF
            elif opcode == 0x19:
                hl = (self.h << 8) | self.l
                value = (self.d << 8) | self.e
                result = hl + value
                self.f = (
                    (self.f & FLAG_Z)
                    | (FLAG_H if ((hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF else 0)
                    | (FLAG_C if result > 0xFFFF else 0)
                )
                self.h = (result >> 8) & 0xFF
                self.l = result & 0xFF
            elif opcode == 0x13:
                value = ((self.d << 8) | self.e) + 1
                self.d = (value >> 8) & 0xFF
                self.e = value & 0xFF
            else:
                value = ((self.h << 8) | self.l) + 1
                self.h = (value >> 8) & 0xFF
                self.l = value & 0xFF
            self.pc = (pc + 1) & 0xFFFF
            self._add_cycles(8)
            return 8

        return 0

    def _execute_memory_exact_fast(self, opcode: int, pc: int) -> int:
        address = (self.h << 8) | self.l
        if opcode in {0x12, 0x1A}:
            address = (self.d << 8) | self.e
        elif opcode == 0x0A:
            address = (self.b << 8) | self.c

        self.pc = (pc + 1) & 0xFFFF
        self._add_cycles(4)

        if opcode in {0x7E, 0x1A, 0x2A, 0x0A, 0x3A, 0x56, 0x5E, 0x6E, 0x46, 0x86, 0xA6, 0xB6}:
            value = self.bus.read8(address)
            self._add_cycles(4)
            if opcode in {0x7E, 0x1A, 0x2A, 0x0A, 0x3A}:
                self.a = value
                if opcode == 0x2A:
                    address = (address + 1) & 0xFFFF
                    self.h = (address >> 8) & 0xFF
                    self.l = address & 0xFF
                elif opcode == 0x3A:
                    address = (address - 1) & 0xFFFF
                    self.h = (address >> 8) & 0xFF
                    self.l = address & 0xFF
            elif opcode == 0x56:
                self.d = value
            elif opcode == 0x5E:
                self.e = value
            elif opcode == 0x6E:
                self.l = value
            elif opcode == 0x46:
                self.b = value
            elif opcode == 0x86:
                result = self.a + value
                self.f = (
                    (FLAG_Z if (result & 0xFF) == 0 else 0)
                    | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                    | (FLAG_C if result > 0xFF else 0)
                )
                self.a = result & 0xFF
            elif opcode == 0xA6:
                self.a &= value
                self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            else:
                self.a |= value
                self.f = FLAG_Z if self.a == 0 else 0
            return 8

        if opcode == 0x35:
            value = self.bus.read8(address)
            self._add_cycles(4)
            result = (value - 1) & 0xFF
            self.bus.write8(address, result)
            self._add_cycles(4)
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            return 12

        if opcode == 0x34:
            value = self.bus.read8(address)
            self._add_cycles(4)
            result = (value + 1) & 0xFF
            self.bus.write8(address, result)
            self._add_cycles(4)
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 12

        value = self.a
        if opcode == 0x72:
            value = self.d
        elif opcode == 0x73:
            value = self.e
        elif opcode == 0x70:
            value = self.b
        self.bus.write8(address, value)
        self._add_cycles(4)
        if opcode == 0x22:
            address = (address + 1) & 0xFFFF
            self.h = (address >> 8) & 0xFF
            self.l = address & 0xFF
        elif opcode == 0x32:
            address = (address - 1) & 0xFFFF
            self.h = (address >> 8) & 0xFF
            self.l = address & 0xFF
        return 8

    def _execute_cb_fast_hl(self, opcode: int, pc: int) -> int:
        address = (self.h << 8) | self.l
        self.pc = (pc + 2) & 0xFFFF
        self._add_cycles(8)
        value = self.bus.read8(address)
        self._add_cycles(4)
        if opcode < 0x40:
            result = self._cb_rotate_shift((opcode >> 3) & 0x07, value)
            self.bus.write8(address, result)
            self._add_cycles(4)
            return 16
        bit = (opcode >> 3) & 0x07
        if opcode < 0x80:
            self.f = (
                (self.f & FLAG_C)
                | FLAG_H
                | (FLAG_Z if (value & (1 << bit)) == 0 else 0)
            )
            return 12
        if opcode < 0xC0:
            result = value & ~(1 << bit)
        else:
            result = value | (1 << bit)
        self.bus.write8(address, result)
        self._add_cycles(4)
        return 16

    def _execute_cb_fast_register(self, opcode: int) -> int:
        if opcode == 0x3F:
            carry = self.a & 1
            self.a >>= 1
            self.f = (FLAG_Z if self.a == 0 else 0) | (FLAG_C if carry else 0)
            return 8
        if opcode == 0x41:
            self.f = (self.f & FLAG_C) | FLAG_H | (FLAG_Z if not self.c & 0x01 else 0)
            return 8
        if opcode == 0x43:
            self.f = (self.f & FLAG_C) | FLAG_H | (FLAG_Z if not self.e & 0x01 else 0)
            return 8
        if opcode == 0x37:
            self.a = ((self.a & 0x0F) << 4) | (self.a >> 4)
            self.f = FLAG_Z if self.a == 0 else 0
            return 8
        if opcode == 0x0B:
            carry = self.e & 1
            self.e = ((carry << 7) | (self.e >> 1)) & 0xFF
            self.f = (FLAG_Z if self.e == 0 else 0) | (FLAG_C if carry else 0)
            return 8
        if opcode == 0x23:
            carry = (self.e >> 7) & 1
            self.e = (self.e << 1) & 0xFF
            self.f = (FLAG_Z if self.e == 0 else 0) | (FLAG_C if carry else 0)
            return 8
        if opcode == 0x33:
            self.e = ((self.e & 0x0F) << 4) | (self.e >> 4)
            self.f = FLAG_Z if self.e == 0 else 0
            return 8
        index = opcode & 0x07
        if index == 6:
            return 0
        value = self._get_r8(index)
        if opcode < 0x40:
            operation = (opcode >> 3) & 0x07
            self._set_r8(index, self._cb_rotate_shift(operation, value))
            return 8
        bit = (opcode >> 3) & 0x07
        if opcode < 0x80:
            self.f = (
                (self.f & FLAG_C)
                | FLAG_H
                | (FLAG_Z if (value & (1 << bit)) == 0 else 0)
            )
            return 8
        if opcode < 0xC0:
            self._set_r8(index, value & ~(1 << bit))
            return 8
        self._set_r8(index, value | (1 << bit))
        return 8
        return 0

    def _execute_opcode(self, opcode: int) -> int:
        if opcode in INVALID_OPCODES:
            raise IllegalInstruction(f"Illegal opcode ${opcode:02X} at ${self.pc - 1:04X}")

        if opcode == 0xFA:
            pc = self.pc
            bus = self.bus
            if pc <= 0x7FFE and self._can_batch_direct_memory_cycles():
                lo = bus.read8(pc)
                hi = bus.read8(pc + 1)
                value = self._read8_direct_fast(lo | (hi << 8), 12)
                if value is not None:
                    self.pc = (pc + 2) & 0xFFFF
                    self.a = value
                    self._add_cycles(12)
                    return 16
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            address = lo | (hi << 8)
            bus = self.bus
            if not bus._oam_dma_active and 0xC000 <= address <= 0xDFFF:
                self.a = bus.wram[address - 0xC000]
            elif not bus._oam_dma_active and 0xE000 <= address <= 0xFDFF:
                self.a = bus.wram[address - 0xE000]
            elif 0xFF80 <= address <= 0xFFFE:
                self.a = bus.hram[address - 0xFF80]
            else:
                self.a = bus.read8(address)
            self._add_cycles(4)
            return 16
        if opcode == 0xEA:
            pc = self.pc
            bus = self.bus
            if pc <= 0x7FFE and self._can_batch_direct_memory_cycles():
                lo = bus.read8(pc)
                hi = bus.read8(pc + 1)
                address = lo | (hi << 8)
                if self._write8_direct_fast(address, self.a, 12):
                    self.pc = (pc + 2) & 0xFFFF
                    self._add_cycles(12)
                    return 16
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            address = lo | (hi << 8)
            bus = self.bus
            if not bus._oam_dma_active and 0xC000 <= address <= 0xDFFF:
                bus.wram[address - 0xC000] = self.a
            elif not bus._oam_dma_active and 0xE000 <= address <= 0xFDFF:
                bus.wram[address - 0xE000] = self.a
            elif 0xFF80 <= address <= 0xFFFE:
                bus.hram[address - 0xFF80] = self.a
            else:
                bus.write8(address, self.a)
            self._add_cycles(4)
            return 16
        if opcode == 0x20:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                self.pc = (pc + 1) & 0xFFFF
                if not self.f & FLAG_Z:
                    self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                    self._add_cycles(8)
                    return 12
                self._add_cycles(4)
                return 8
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if not self.f & FLAG_Z:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x28:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                self.pc = (pc + 1) & 0xFFFF
                if self.f & FLAG_Z:
                    self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                    self._add_cycles(8)
                    return 12
                self._add_cycles(4)
                return 8
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if self.f & FLAG_Z:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0xCD:
            pc = self.pc
            sp_high = (self.sp - 1) & 0xFFFF
            sp_low = (self.sp - 2) & 0xFFFF
            if (
                pc <= 0x7FFE
                and self._can_batch_direct_memory_cycles()
                and self._is_direct_fast_address(sp_high)
                and self._is_direct_fast_address(sp_low)
            ):
                lo = self.bus.read8(pc)
                hi = self.bus.read8(pc + 1)
                self._write8_direct_fast(sp_high, (pc + 2) >> 8)
                self._write8_direct_fast(sp_low, (pc + 2) & 0xFF)
                self.sp = sp_low
                self.pc = lo | (hi << 8)
                self._add_cycles(20)
                return 24
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            address = lo | (hi << 8)
            self._internal_cycle()
            self.sp = (self.sp - 1) & 0xFFFF
            self._write8_fast(self.sp, (self.pc >> 8) & 0xFF)
            self._add_cycles(4)
            self.sp = (self.sp - 1) & 0xFFFF
            self._write8_fast(self.sp, self.pc & 0xFF)
            self._add_cycles(4)
            self.pc = address
            return 24
        if opcode == 0xCB:
            return self._execute_cb(self._fetch8())
        if opcode == 0xA7:
            self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            return 4
        if opcode == 0x6F:
            self.l = self.a
            return 4
        if opcode == 0xC9:
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if (
                self._can_batch_direct_memory_cycles()
                and self._is_direct_fast_address(sp)
                and self._is_direct_fast_address(sp_next)
            ):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                self.sp = (sp + 2) & 0xFFFF
                self.pc = (lo or 0) | ((hi or 0) << 8)
                self._add_cycles(12)
                return 16
            lo = self._read8_fast(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self._read8_fast(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.pc = lo | (hi << 8)
            self._internal_cycle()
            return 16
        if opcode == 0xC0:
            if not self.f & FLAG_Z:
                sp = self.sp
                sp_next = (sp + 1) & 0xFFFF
                if (
                    self._can_batch_direct_memory_cycles()
                    and self._is_direct_fast_address(sp)
                    and self._is_direct_fast_address(sp_next)
                ):
                    lo = self._read8_direct_fast(sp)
                    hi = self._read8_direct_fast(sp_next)
                    self.sp = (sp + 2) & 0xFFFF
                    self.pc = (lo or 0) | ((hi or 0) << 8)
                    self._add_cycles(16)
                    return 20
            self._internal_cycle()
            if not self.f & FLAG_Z:
                lo = self._read8_fast(self.sp)
                self._add_cycles(4)
                self.sp = (self.sp + 1) & 0xFFFF
                hi = self._read8_fast(self.sp)
                self._add_cycles(4)
                self.sp = (self.sp + 1) & 0xFFFF
                self.pc = lo | (hi << 8)
                self._internal_cycle()
                return 20
            return 8
        if opcode == 0xD1:
            sp = self.sp
            sp_next = (sp + 1) & 0xFFFF
            if (
                self._can_batch_direct_memory_cycles()
                and self._is_direct_fast_address(sp)
                and self._is_direct_fast_address(sp_next)
            ):
                lo = self._read8_direct_fast(sp)
                hi = self._read8_direct_fast(sp_next)
                self.sp = (sp + 2) & 0xFFFF
                self.d = hi or 0
                self.e = lo or 0
                self._add_cycles(8)
                return 12
            lo = self._read8_fast(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self._read8_fast(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.d = hi
            self.e = lo
            return 12
        if opcode == 0xF0:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                value = self._read8_direct_fast(0xFF00 + offset)
                if value is not None:
                    self.pc = (pc + 1) & 0xFFFF
                    self.a = value
                    self._add_cycles(8)
                    return 12
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.a = self._read8_fast(0xFF00 + offset)
            self._add_cycles(4)
            return 12
        if opcode == 0xE0:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                if self._is_direct_fast_address(0xFF00 + offset):
                    self._write8_direct_fast(0xFF00 + offset, self.a)
                    self.pc = (pc + 1) & 0xFFFF
                    self._add_cycles(8)
                    return 12
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self._write8_fast(0xFF00 + offset, self.a)
            self._add_cycles(4)
            return 12
        if opcode == 0xE6:
            self.a &= self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            return 8
        if opcode == 0x47:
            self.b = self.a
            return 4
        if opcode == 0x67:
            self.h = self.a
            return 4
        if opcode == 0x7E:
            address = (self.h << 8) | self.l
            bus = self.bus
            if not bus._oam_dma_active and 0xC000 <= address <= 0xDFFF:
                self.a = bus.wram[address - 0xC000]
            elif not bus._oam_dma_active and 0xE000 <= address <= 0xFDFF:
                self.a = bus.wram[address - 0xE000]
            elif 0xFF80 <= address <= 0xFFFE:
                self.a = bus.hram[address - 0xFF80]
            else:
                self.a = bus.read8(address)
            self._add_cycles(4)
            return 8
        if opcode == 0x3C:
            value = self.a
            result = (value + 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 4
        if opcode == 0x2C:
            value = self.l
            result = (value + 1) & 0xFF
            self.l = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 4
        if opcode == 0x5F:
            self.e = self.a
            return 4
        if opcode == 0x30:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                self.pc = (pc + 1) & 0xFFFF
                if not self.f & FLAG_C:
                    self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                    self._add_cycles(8)
                    return 12
                self._add_cycles(4)
                return 8
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if not self.f & FLAG_C:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x18:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                self.pc = (pc + 1) & 0xFFFF
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                self._add_cycles(8)
                return 12
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self._internal_cycle()
            self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
            return 12
        if opcode == 0x3D:
            value = self.a
            result = (value - 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            return 4
        if opcode == 0xB8:
            value = self.b
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            return 4
        if opcode == 0xFE:
            value = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            return 8
        if opcode == 0x77:
            address = (self.h << 8) | self.l
            bus = self.bus
            if not bus._oam_dma_active and 0xC000 <= address <= 0xDFFF:
                bus.wram[address - 0xC000] = self.a
            elif not bus._oam_dma_active and 0xE000 <= address <= 0xFDFF:
                bus.wram[address - 0xE000] = self.a
            elif 0xFF80 <= address <= 0xFFFE:
                bus.hram[address - 0xFF80] = self.a
            else:
                bus.write8(address, self.a)
            self._add_cycles(4)
            return 8
        if opcode == 0x72:
            address = (self.h << 8) | self.l
            bus = self.bus
            if not bus._oam_dma_active and 0xC000 <= address <= 0xDFFF:
                bus.wram[address - 0xC000] = self.d
            elif not bus._oam_dma_active and 0xE000 <= address <= 0xFDFF:
                bus.wram[address - 0xE000] = self.d
            elif 0xFF80 <= address <= 0xFFFE:
                bus.hram[address - 0xFF80] = self.d
            else:
                bus.write8(address, self.d)
            self._add_cycles(4)
            return 8
        if opcode == 0x73:
            address = (self.h << 8) | self.l
            bus = self.bus
            if not bus._oam_dma_active and 0xC000 <= address <= 0xDFFF:
                bus.wram[address - 0xC000] = self.e
            elif not bus._oam_dma_active and 0xE000 <= address <= 0xFDFF:
                bus.wram[address - 0xE000] = self.e
            elif 0xFF80 <= address <= 0xFFFE:
                bus.hram[address - 0xFF80] = self.e
            else:
                bus.write8(address, self.e)
            self._add_cycles(4)
            return 8
        if opcode == 0x07:
            carry = (self.a >> 7) & 1
            self.a = ((self.a << 1) | carry) & 0xFF
            self.f = FLAG_C if carry else 0
            return 4
        if opcode == 0x21:
            pc = self.pc
            if pc <= 0x7FFE and self._can_batch_direct_memory_cycles():
                self.l = self.bus.read8(pc)
                self.h = self.bus.read8(pc + 1)
                self.pc = (pc + 2) & 0xFFFF
                self._add_cycles(8)
                return 12
            self.l = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.h = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 12
        if opcode == 0x38:
            pc = self.pc
            if pc <= 0x7FFF and self._can_batch_direct_memory_cycles():
                offset = self.bus.read8(pc)
                self.pc = (pc + 1) & 0xFFFF
                if self.f & FLAG_C:
                    self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                    self._add_cycles(8)
                    return 12
                self._add_cycles(4)
                return 8
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if self.f & FLAG_C:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x22:
            hl = (self.h << 8) | self.l
            self._write8_fast(hl, self.a)
            self._add_cycles(4)
            hl = (hl + 1) & 0xFFFF
            self.h = (hl >> 8) & 0xFF
            self.l = hl & 0xFF
            return 8
        if opcode == 0x12:
            self._write8_fast((self.d << 8) | self.e, self.a)
            self._add_cycles(4)
            return 8
        if opcode == 0x1A:
            self.a = self._read8_fast((self.d << 8) | self.e)
            self._add_cycles(4)
            return 8
        if opcode == 0x2A:
            hl = (self.h << 8) | self.l
            self.a = self._read8_fast(hl)
            self._add_cycles(4)
            hl = (hl + 1) & 0xFFFF
            self.h = (hl >> 8) & 0xFF
            self.l = hl & 0xFF
            return 8
        if opcode == 0x0B:
            self._internal_cycle()
            value = ((self.b << 8) | self.c) - 1
            self.b = (value >> 8) & 0xFF
            self.c = value & 0xFF
            return 8
        if opcode == 0x1B:
            self._internal_cycle()
            value = ((self.d << 8) | self.e) - 1
            self.d = (value >> 8) & 0xFF
            self.e = value & 0xFF
            return 8
        if opcode == 0x09:
            self._internal_cycle()
            hl = (self.h << 8) | self.l
            value = (self.b << 8) | self.c
            result = hl + value
            self.f = (
                (self.f & FLAG_Z)
                | (FLAG_H if ((hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF else 0)
                | (FLAG_C if result > 0xFFFF else 0)
            )
            self.h = (result >> 8) & 0xFF
            self.l = result & 0xFF
            return 8
        if opcode == 0x19:
            self._internal_cycle()
            hl = (self.h << 8) | self.l
            value = (self.d << 8) | self.e
            result = hl + value
            self.f = (
                (self.f & FLAG_Z)
                | (FLAG_H if ((hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF else 0)
                | (FLAG_C if result > 0xFFFF else 0)
            )
            self.h = (result >> 8) & 0xFF
            self.l = result & 0xFF
            return 8
        if opcode == 0x13:
            self._internal_cycle()
            value = ((self.d << 8) | self.e) + 1
            self.d = (value >> 8) & 0xFF
            self.e = value & 0xFF
            return 8
        if opcode == 0x23:
            self._internal_cycle()
            value = ((self.h << 8) | self.l) + 1
            self.h = (value >> 8) & 0xFF
            self.l = value & 0xFF
            return 8
        if opcode == 0x0E:
            self.c = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 8
        if opcode == 0x3E:
            self.a = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 8
        if opcode == 0xC6:
            value = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            result = self.a + value
            self.f = (
                (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                | (FLAG_C if result > 0xFF else 0)
            )
            self.a = result & 0xFF
            return 8
        if opcode == 0xB3:
            self.a |= self.e
            self.f = FLAG_Z if self.a == 0 else 0
            return 4
        if opcode == 0x78:
            self.a = self.b
            return 4
        if opcode == 0x7A:
            self.a = self.d
            return 4
        if opcode == 0x7B:
            self.a = self.e
            return 4
        if opcode == 0x43:
            self.b = self.e
            return 4
        if opcode == 0x58:
            self.e = self.b
            return 4
        if opcode == 0xAF:
            self.a = 0
            self.f = FLAG_Z
            return 4
        if opcode == 0xBD:
            value = self.l
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            return 4
        if opcode == 0x24:
            value = self.h
            result = (value + 1) & 0xFF
            self.h = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 4
        if opcode == 0x85:
            value = self.l
            result = self.a + value
            self.f = (
                (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                | (FLAG_C if result > 0xFF else 0)
            )
            self.a = result & 0xFF
            return 4

        if opcode == 0x00:
            return 4
        if opcode == 0x01:
            self.c = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.b = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 12
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
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self._internal_cycle()
            self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
            return 12
        if opcode == 0x05:
            value = self.b
            result = (value - 1) & 0xFF
            self.b = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            return 4
        if opcode == 0x06:
            self.b = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 8
        if opcode == 0x07:
            carry = (self.a >> 7) & 1
            self.a = ((self.a << 1) | carry) & 0xFF
            self.f = FLAG_C if carry else 0
            return 4
        if opcode == 0x09:
            self._internal_cycle()
            hl = (self.h << 8) | self.l
            value = (self.b << 8) | self.c
            result = hl + value
            self.f = (
                (self.f & FLAG_Z)
                | (FLAG_H if ((hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF else 0)
                | (FLAG_C if result > 0xFFFF else 0)
            )
            self.h = (result >> 8) & 0xFF
            self.l = result & 0xFF
            return 8
        if opcode == 0x0C:
            value = self.c
            result = (value + 1) & 0xFF
            self.c = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 4
        if opcode == 0x0D:
            value = self.c
            result = (value - 1) & 0xFF
            self.c = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            return 4
        if opcode == 0x0E:
            self.c = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 8
        if opcode == 0x12:
            self.bus.write8((self.d << 8) | self.e, self.a)
            self._add_cycles(4)
            return 8
        if opcode == 0x13:
            self._internal_cycle()
            value = ((self.d << 8) | self.e) + 1
            self.d = (value >> 8) & 0xFF
            self.e = value & 0xFF
            return 8
        if opcode == 0x19:
            self._internal_cycle()
            hl = (self.h << 8) | self.l
            value = (self.d << 8) | self.e
            result = hl + value
            self.f = (
                (self.f & FLAG_Z)
                | (FLAG_H if ((hl & 0x0FFF) + (value & 0x0FFF)) > 0x0FFF else 0)
                | (FLAG_C if result > 0xFFFF else 0)
            )
            self.h = (result >> 8) & 0xFF
            self.l = result & 0xFF
            return 8
        if opcode == 0x1A:
            self.a = self.bus.read8((self.d << 8) | self.e)
            self._add_cycles(4)
            return 8
        if opcode == 0x1B:
            self._internal_cycle()
            value = ((self.d << 8) | self.e) - 1
            self.d = (value >> 8) & 0xFF
            self.e = value & 0xFF
            return 8
        if opcode == 0x20:
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if not self.f & FLAG_Z:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x28:
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if self.f & FLAG_Z:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x30:
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if not self.f & FLAG_C:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x38:
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if self.f & FLAG_C:
                self._internal_cycle()
                self.pc = (self.pc + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
                return 12
            return 8
        if opcode == 0x2C:
            value = self.l
            result = (value + 1) & 0xFF
            self.l = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 4
        if opcode == 0x22:
            hl = (self.h << 8) | self.l
            self.bus.write8(hl, self.a)
            self._add_cycles(4)
            hl = (hl + 1) & 0xFFFF
            self.h = (hl >> 8) & 0xFF
            self.l = hl & 0xFF
            return 8
        if opcode == 0x23:
            self._internal_cycle()
            value = ((self.h << 8) | self.l) + 1
            self.h = (value >> 8) & 0xFF
            self.l = value & 0xFF
            return 8
        if opcode == 0x27:
            self._daa()
            return 4
        if opcode == 0x2F:
            self.a ^= 0xFF
            self.f = (self.f & (FLAG_Z | FLAG_C)) | FLAG_N | FLAG_H
            return 4
        if opcode == 0x37:
            self.f = (self.f & FLAG_Z) | FLAG_C
            return 4
        if opcode == 0x3F:
            self.f = (self.f & FLAG_Z) | (0 if self.f & FLAG_C else FLAG_C)
            return 4
        if opcode == 0x3D:
            value = self.a
            result = (value - 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | FLAG_N
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x00 else 0)
            )
            return 4
        if opcode == 0x3C:
            value = self.a
            result = (value + 1) & 0xFF
            self.a = result
            self.f = (
                (self.f & FLAG_C)
                | (FLAG_Z if result == 0 else 0)
                | (FLAG_H if (value & 0x0F) == 0x0F else 0)
            )
            return 4
        if opcode == 0x2A:
            hl = (self.h << 8) | self.l
            self.a = self.bus.read8(hl)
            self._add_cycles(4)
            hl = (hl + 1) & 0xFFFF
            self.h = (hl >> 8) & 0xFF
            self.l = hl & 0xFF
            return 8
        if opcode == 0x3E:
            self.a = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 8
        if opcode == 0x76:
            if not self.ime and self._pending_interrupts():
                self._halt_bug = True
            else:
                self.halted = True
            return 4
        if opcode == 0x47:
            self.b = self.a
            return 4
        if opcode == 0x43:
            self.b = self.e
            return 4
        if opcode == 0x4F:
            self.c = self.a
            return 4
        if opcode == 0x57:
            self.d = self.a
            return 4
        if opcode == 0x58:
            self.e = self.b
            return 4
        if opcode == 0x5D:
            self.e = self.l
            return 4
        if opcode == 0x5F:
            self.e = self.a
            return 4
        if opcode == 0x67:
            self.h = self.a
            return 4
        if opcode == 0x6F:
            self.l = self.a
            return 4
        if opcode == 0x78:
            self.a = self.b
            return 4
        if opcode == 0x79:
            self.a = self.c
            return 4
        if opcode == 0x7A:
            self.a = self.d
            return 4
        if opcode == 0x7B:
            self.a = self.e
            return 4
        if opcode == 0x7C:
            self.a = self.h
            return 4
        if opcode == 0x7D:
            self.a = self.l
            return 4
        if opcode == 0x77:
            self.bus.write8((self.h << 8) | self.l, self.a)
            self._add_cycles(4)
            return 8
        if opcode == 0x7E:
            self.a = self.bus.read8((self.h << 8) | self.l)
            self._add_cycles(4)
            return 8
        if opcode == 0x85:
            value = self.l
            result = self.a + value
            self.f = (
                (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                | (FLAG_C if result > 0xFF else 0)
            )
            self.a = result & 0xFF
            return 4
        if opcode == 0x86:
            value = self.bus.read8((self.h << 8) | self.l)
            self._add_cycles(4)
            result = self.a + value
            self.f = (
                (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if ((self.a & 0x0F) + (value & 0x0F)) > 0x0F else 0)
                | (FLAG_C if result > 0xFF else 0)
            )
            self.a = result & 0xFF
            return 8
        if opcode == 0xA7:
            self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            return 4
        if opcode == 0xAF:
            self.a = 0
            self.f = FLAG_Z
            return 4
        if opcode == 0xB0:
            self.a |= self.b
            self.f = FLAG_Z if self.a == 0 else 0
            return 4
        if opcode == 0xB1:
            self.a |= self.c
            self.f = FLAG_Z if self.a == 0 else 0
            return 4
        if opcode == 0xB3:
            self.a |= self.e
            self.f = FLAG_Z if self.a == 0 else 0
            return 4
        if opcode == 0xBD:
            value = self.l
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            return 4
        if opcode == 0xB8:
            value = self.b
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            return 4
        if opcode == 0xC3:
            address = self._fetch16()
            self._internal_cycle()
            self.pc = address
            return 16
        if opcode == 0xC9:
            lo = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.pc = lo | (hi << 8)
            self._internal_cycle()
            return 16
        if opcode == 0xCB:
            return self._execute_cb(self._fetch8())
        if opcode == 0xCD:
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            address = lo | (hi << 8)
            self._internal_cycle()
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, (self.pc >> 8) & 0xFF)
            self._add_cycles(4)
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.pc & 0xFF)
            self._add_cycles(4)
            self.pc = address
            return 24
        if opcode == 0xD9:
            self.pc = self._pop16()
            self.ime = True
            self._ime_delay = 0
            self._internal_cycle()
            return 16
        if opcode == 0xD1:
            lo = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.d = hi
            self.e = lo
            return 12
        if opcode == 0xE0:
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.bus.write8(0xFF00 + offset, self.a)
            self._add_cycles(4)
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
            self.pc = (self.h << 8) | self.l
            return 4
        if opcode == 0xEA:
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.bus.write8(lo | (hi << 8), self.a)
            self._add_cycles(4)
            return 16
        if opcode == 0xF0:
            offset = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.a = self.bus.read8(0xFF00 + offset)
            self._add_cycles(4)
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
            self.sp = (self.h << 8) | self.l
            return 8
        if opcode == 0xFA:
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.a = self.bus.read8(lo | (hi << 8))
            self._add_cycles(4)
            return 16
        if opcode == 0xFB:
            self._ime_delay = 2
            return 4
        if opcode == 0xE6:
            self.a &= self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.f = (FLAG_Z if self.a == 0 else 0) | FLAG_H
            return 8
        if opcode == 0xFE:
            value = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            result = self.a - value
            self.f = (
                FLAG_N
                | (FLAG_Z if (result & 0xFF) == 0 else 0)
                | (FLAG_H if (self.a & 0x0F) < (value & 0x0F) else 0)
                | (FLAG_C if self.a < value else 0)
            )
            return 8

        if opcode in {0x07, 0x0F, 0x17, 0x1F}:
            self._rotate_accumulator(opcode)
            return 4

        if opcode in {0x20, 0x28, 0x30, 0x38}:
            condition = self._condition((opcode - 0x20) // 8)
            self._jr(condition)
            return 12 if condition else 8

        if opcode == 0x21:
            self.l = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.h = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            return 12
        if opcode & 0xCF == 0x01:
            self._set_r16((opcode >> 4) & 0x03, self._fetch16())
            return 12
        if opcode in {0x02, 0x12, 0x22, 0x32}:
            self._ld_indirect_a(opcode)
            return 8
        if opcode in {0x0A, 0x1A, 0x2A, 0x3A}:
            self._ld_a_indirect(opcode)
            return 8
        if opcode == 0x0B:
            self._internal_cycle()
            value = ((self.b << 8) | self.c) - 1
            self.b = (value >> 8) & 0xFF
            self.c = value & 0xFF
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
        if opcode == 0x36:
            value = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            self.bus.write8((self.h << 8) | self.l, value)
            self._add_cycles(4)
            return 12
        if opcode & 0xC7 == 0x06:
            index = (opcode >> 3) & 0x07
            self._set_r8(index, self._fetch8())
            return 12 if index == 6 else 8
        if opcode == 0x72:
            self.bus.write8((self.h << 8) | self.l, self.d)
            self._add_cycles(4)
            return 8
        if opcode == 0x73:
            self.bus.write8((self.h << 8) | self.l, self.e)
            self._add_cycles(4)
            return 8
        if 0x40 <= opcode <= 0x7F:
            dst = (opcode >> 3) & 0x07
            src = opcode & 0x07
            self._set_r8(dst, self._get_r8(src))
            return 8 if dst == 6 or src == 6 else 4
        if 0x80 <= opcode <= 0xBF:
            src = opcode & 0x07
            self._alu((opcode >> 3) & 0x07, self._get_r8(src))
            return 8 if src == 6 else 4

        if opcode == 0xC0:
            self._internal_cycle()
            if not self.f & FLAG_Z:
                lo = self.bus.read8(self.sp)
                self._add_cycles(4)
                self.sp = (self.sp + 1) & 0xFFFF
                hi = self.bus.read8(self.sp)
                self._add_cycles(4)
                self.sp = (self.sp + 1) & 0xFFFF
                self.pc = lo | (hi << 8)
                self._internal_cycle()
                return 20
            return 8
        if opcode == 0xC1:
            lo = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.b = hi
            self.c = lo
            return 12
        if opcode == 0xC5:
            self._internal_cycle()
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.b)
            self._add_cycles(4)
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.c)
            self._add_cycles(4)
            return 16
        if opcode == 0xC8:
            self._internal_cycle()
            if self.f & FLAG_Z:
                lo = self.bus.read8(self.sp)
                self._add_cycles(4)
                self.sp = (self.sp + 1) & 0xFFFF
                hi = self.bus.read8(self.sp)
                self._add_cycles(4)
                self.sp = (self.sp + 1) & 0xFFFF
                self.pc = lo | (hi << 8)
                self._internal_cycle()
                return 20
            return 8
        if opcode == 0xCA:
            lo = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            hi = self.bus.read8(self.pc)
            self._add_cycles(4)
            self.pc = (self.pc + 1) & 0xFFFF
            if self.f & FLAG_Z:
                self._internal_cycle()
                self.pc = lo | (hi << 8)
                return 16
            return 12
        if opcode == 0xD5:
            self._internal_cycle()
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.d)
            self._add_cycles(4)
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.e)
            self._add_cycles(4)
            return 16
        if opcode == 0xE1:
            lo = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.h = hi
            self.l = lo
            return 12
        if opcode == 0xE5:
            self._internal_cycle()
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.h)
            self._add_cycles(4)
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.l)
            self._add_cycles(4)
            return 16
        if opcode == 0xF1:
            lo = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            hi = self.bus.read8(self.sp)
            self._add_cycles(4)
            self.sp = (self.sp + 1) & 0xFFFF
            self.a = hi
            self.f = lo & 0xF0
            return 12
        if opcode == 0xF5:
            self._internal_cycle()
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.a)
            self._add_cycles(4)
            self.sp = (self.sp - 1) & 0xFFFF
            self.bus.write8(self.sp, self.f & 0xF0)
            self._add_cycles(4)
            return 16

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
        if opcode == 0x3F:
            carry = self.a & 1
            self.a >>= 1
            self.f = (FLAG_Z if self.a == 0 else 0) | (FLAG_C if carry else 0)
            return 8
        if opcode == 0x41:
            self.f = (self.f & FLAG_C) | FLAG_H | (FLAG_Z if not self.c & 0x01 else 0)
            return 8
        if opcode == 0x43:
            self.f = (self.f & FLAG_C) | FLAG_H | (FLAG_Z if not self.e & 0x01 else 0)
            return 8
        if opcode == 0x37:
            self.a = ((self.a & 0x0F) << 4) | (self.a >> 4)
            self.f = FLAG_Z if self.a == 0 else 0
            return 8
        if opcode == 0x0B:
            carry = self.e & 1
            self.e = ((carry << 7) | (self.e >> 1)) & 0xFF
            self.f = (FLAG_Z if self.e == 0 else 0) | (FLAG_C if carry else 0)
            return 8
        if opcode == 0x23:
            carry = (self.e >> 7) & 1
            self.e = (self.e << 1) & 0xFF
            self.f = (FLAG_Z if self.e == 0 else 0) | (FLAG_C if carry else 0)
            return 8
        if opcode == 0x33:
            self.e = ((self.e & 0x0F) << 4) | (self.e >> 4)
            self.f = FLAG_Z if self.e == 0 else 0
            return 8

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
        return self._service_interrupt_pending(pending)

    def _service_interrupt_pending(self, pending: int) -> int:
        bit = (pending & -pending).bit_length() - 1
        if self.profile_enabled:
            self._profile_interrupt_entries += 1
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

    def _read8_fast(self, address: int) -> int:
        address &= 0xFFFF
        bus = self.bus
        if address <= 0x7FFF:
            if bus._oam_dma_active:
                return bus.read8(address)
            if bus.boot_rom_enabled and address < len(bus.boot_rom):
                return bus.boot_rom[address]
            if self._fast_rom_is_mbc3:
                data = self._fast_rom_data
                if address < 0x4000:
                    return data[address] if address < self._fast_rom_data_len else 0xFF
                return data[
                    self._fast_rom_cartridge._mbc3_rom_bank_offset
                    + (address - 0x4000)
                ]
            return bus.mapper.read_rom(address)
        if 0xFF80 <= address <= 0xFFFE:
            return bus.hram[address - 0xFF80]
        if bus._oam_dma_active:
            return bus.read8(address)
        if 0xA000 <= address <= 0xBFFF:
            return bus.mapper.read_ram(address)
        if 0xC000 <= address <= 0xDFFF:
            if not bus.cgb_mode or address < 0xD000:
                return bus.wram[address - 0xC000]
            return bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (address - 0xD000)]
        if 0xE000 <= address <= 0xFDFF:
            effective = address - 0x2000
            if not bus.cgb_mode or effective < 0xD000:
                return bus.wram[effective - 0xC000]
            return bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (effective - 0xD000)]
        return bus.read8(address)

    def _read8_direct_fast(self, address: int, stable_cycles: int = 0) -> int | None:
        address &= 0xFFFF
        bus = self.bus
        if 0xFF80 <= address <= 0xFFFE:
            return bus.hram[address - 0xFF80]
        if bus._oam_dma_active:
            return None
        if address <= 0x7FFF:
            if bus.boot_rom_enabled and address < 0x100:
                return None
            if self._fast_rom_is_mbc3:
                data = self._fast_rom_data
                if address < 0x4000:
                    return data[address] if address < self._fast_rom_data_len else 0xFF
                return data[
                    self._fast_rom_cartridge._mbc3_rom_bank_offset
                    + (address - 0x4000)
                ]
            return bus.mapper.read_rom(address)
        if 0x8000 <= address <= 0x9FFF and stable_cycles:
            if (
                bus.cpu_cycles_for_device_cycles(bus.ppu.cycles_until_next_event())
                > stable_cycles
            ):
                return bus.vram[bus._vram_offset(address)] if bus._vram_read_accessible() else 0xFF
            return None
        if 0xA000 <= address <= 0xBFFF:
            return bus.mapper.read_ram(address)
        if 0xC000 <= address <= 0xDFFF:
            if not bus.cgb_mode or address < 0xD000:
                return bus.wram[address - 0xC000]
            return bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (address - 0xD000)]
        if 0xE000 <= address <= 0xFDFF:
            effective = address - 0x2000
            if not bus.cgb_mode or effective < 0xD000:
                return bus.wram[effective - 0xC000]
            return bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (effective - 0xD000)]
        return None

    def _write8(self, address: int, value: int) -> None:
        self.bus.write8(address, value)
        self._add_cycles(4)

    def _write8_fast(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        bus = self.bus
        if 0xFF80 <= address <= 0xFFFE:
            bus.hram[address - 0xFF80] = value
            return
        if bus._oam_dma_active:
            bus.write8(address, value)
            return
        if 0xC000 <= address <= 0xDFFF:
            if not bus.cgb_mode or address < 0xD000:
                bus.wram[address - 0xC000] = value
            else:
                bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (address - 0xD000)] = value
            return
        if 0xE000 <= address <= 0xFDFF:
            effective = address - 0x2000
            if not bus.cgb_mode or effective < 0xD000:
                bus.wram[effective - 0xC000] = value
            else:
                bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (effective - 0xD000)] = value
            return
        bus.write8(address, value)

    def _write8_direct_fast(self, address: int, value: int, stable_cycles: int = 0) -> bool:
        address &= 0xFFFF
        value &= 0xFF
        bus = self.bus
        if 0xFF80 <= address <= 0xFFFE:
            bus.hram[address - 0xFF80] = value
            return True
        if bus._oam_dma_active:
            return False
        if address <= 0x7FFF:
            bus.mapper.write_rom_control(address, value)
            return True
        if 0x8000 <= address <= 0x9FFF and stable_cycles:
            if (
                bus.cpu_cycles_for_device_cycles(bus.ppu.cycles_until_next_event())
                > stable_cycles
            ):
                if bus._vram_write_accessible():
                    bus.vram[bus._vram_offset(address)] = value
                return True
            return False
        if 0xA000 <= address <= 0xBFFF:
            bus.mapper.write_ram(address, value)
            return True
        if 0xC000 <= address <= 0xDFFF:
            if not bus.cgb_mode or address < 0xD000:
                bus.wram[address - 0xC000] = value
            else:
                bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (address - 0xD000)] = value
            return True
        if 0xE000 <= address <= 0xFDFF:
            effective = address - 0x2000
            if not bus.cgb_mode or effective < 0xD000:
                bus.wram[effective - 0xC000] = value
            else:
                bus.wram[(bus._wram_bank_register or 1) * 0x1000 + (effective - 0xD000)] = value
            return True
        return False

    def _is_direct_fast_address(self, address: int) -> bool:
        address &= 0xFFFF
        if 0xFF80 <= address <= 0xFFFE:
            return True
        if self.bus._oam_dma_active:
            return False
        return 0xA000 <= address <= 0xBFFF or 0xC000 <= address <= 0xFDFF

    def _can_batch_direct_memory_cycles(self) -> bool:
        bus = self.bus
        return not bus._oam_dma_active and not bus._oam_dma_requested

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
