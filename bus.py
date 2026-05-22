from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apu import APU
from cartridge import Cartridge
from joypad import Joypad
from ppu import (
    DOTS_PER_LINE,
    MODE2_DOTS,
    MODE_DRAWING,
    MODE_HBLANK,
    MODE_OAM,
    MODE_VBLANK,
    PPU,
    VISIBLE_LINES,
)


UNUSABLE_IO_OFFSETS = {0x03, *range(0x08, 0x0F)}
CGB_ONLY_IO_OFFSETS = {
    0x4C,
    0x4F,
    *range(0x51, 0x56),
    0x56,
    *range(0x68, 0x6C),
    0x6C,
    0x70,
    *range(0x72, 0x78),
}
SERIAL_INTERNAL_TRANSFER_CYCLES = 4096
PPU_SCROLL_REGISTER_OFFSETS = {0x42, 0x43}
PPU_RASTER_REGISTER_OFFSETS = {0x47, 0x48, 0x49}
PPU_WINDOW_X_REGISTER_OFFSET = 0x4B
DMG_POST_BOOT_REGISTERED_MARK_TILE_ADDRESS = 0x19 * 16
DMG_POST_BOOT_REGISTERED_MARK_TILE = bytes(
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


@dataclass(frozen=True)
class BusProfileStats:
    slow_system_counter_cycles: int
    oam_dma_cycles: int
    oam_dma_starts: int
    timer_overflows: int


class Bus:
    def __init__(
        self,
        cartridge: Cartridge,
        serial_sink: Callable[[str], None] | None = None,
        boot_rom: bytes | None = None,
    ) -> None:
        self.cartridge = cartridge
        self.mapper = cartridge.mapper
        self.boot_rom = bytes(boot_rom[:0x100]) if boot_rom is not None else b""
        self.boot_rom_enabled = bool(self.boot_rom)
        self.vram = bytearray(0x2000)
        self.wram = bytearray(0x2000)
        self.oam = bytearray(0xA0)
        self.io = bytearray(0x80)
        self.hram = bytearray(0x7F)
        self.ie = 0
        self.serial_text = ""
        self.serial_sink = serial_sink or self._stdout_serial_sink
        self._serial_transfer_cycles = 0
        self._system_counter = 0
        self._tima_reload_delay = 0
        self._oam_dma_requested = False
        self._oam_dma_active = False
        self._oam_dma_source = 0
        self._oam_dma_index = 0
        self._oam_dma_cycle_counter = 0
        self._stopped = False
        self.profile_enabled = False
        self._profile_slow_system_counter_cycles = 0
        self._profile_oam_dma_cycles = 0
        self._profile_oam_dma_starts = 0
        self._profile_timer_overflows = 0
        if not self.boot_rom_enabled:
            self._initialize_io_defaults()
            self._initialize_vram_defaults()
        self.io[0x50] = 0x00 if self.boot_rom_enabled else 0x01
        self._system_counter = self.io[0x04] << 8
        self.apu = APU(self)
        self.joypad = Joypad(self)
        self.ppu = PPU(self)

    def consume_profile(self) -> BusProfileStats:
        stats = BusProfileStats(
            slow_system_counter_cycles=self._profile_slow_system_counter_cycles,
            oam_dma_cycles=self._profile_oam_dma_cycles,
            oam_dma_starts=self._profile_oam_dma_starts,
            timer_overflows=self._profile_timer_overflows,
        )
        self._profile_slow_system_counter_cycles = 0
        self._profile_oam_dma_cycles = 0
        self._profile_oam_dma_starts = 0
        self._profile_timer_overflows = 0
        return stats

    @property
    def oam_dma_active(self) -> bool:
        return self._oam_dma_requested or self._oam_dma_active

    def read8(self, address: int) -> int:
        address &= 0xFFFF
        if self._oam_dma_active and not self._is_hram(address):
            return 0xFF
        return self._read8_unblocked(address)

    def _read8_unblocked(self, address: int) -> int:
        address &= 0xFFFF
        if address <= 0x7FFF:
            if self.boot_rom_enabled and address < len(self.boot_rom):
                return self.boot_rom[address]
            return self.mapper.read_rom(address)
        if address <= 0x9FFF:
            if not self._vram_read_accessible():
                return 0xFF
            return self.vram[address - 0x8000]
        if address <= 0xBFFF:
            return self.mapper.read_ram(address)
        if address <= 0xDFFF:
            return self.wram[address - 0xC000]
        if address <= 0xFDFF:
            return self.wram[address - 0xE000]
        if address <= 0xFE9F:
            if not self._oam_read_accessible():
                return 0xFF
            return self.oam[address - 0xFE00]
        if address <= 0xFEFF:
            return 0xFF
        if address <= 0xFF7F:
            offset = address - 0xFF00
            if offset in UNUSABLE_IO_OFFSETS or offset in CGB_ONLY_IO_OFFSETS:
                return 0xFF
            if address == 0xFF00:
                return self.joypad.read()
            if address == 0xFF02:
                return self.io[offset] | 0x7E
            if address == 0xFF04:
                return (self._system_counter >> 8) & 0xFF
            if address == 0xFF07:
                return self.io[offset] | 0xF8
            if address == 0xFF0F:
                return self.interrupt_flags
            if 0xFF10 <= address <= 0xFF3F:
                return self.apu.read(offset)
            if address == 0xFF4D:
                return self.io[offset] | 0x7E
            return self.io[offset]
        if address <= 0xFFFE:
            return self.hram[address - 0xFF80]
        return self.ie

    def write8(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        if self._oam_dma_active and not self._is_hram(address):
            return
        if address <= 0x7FFF:
            self.mapper.write_rom_control(address, value)
            return
        if address <= 0x9FFF:
            if not self._vram_write_accessible():
                return
            self.vram[address - 0x8000] = value
            return
        if address <= 0xBFFF:
            self.mapper.write_ram(address, value)
            return
        if address <= 0xDFFF:
            self.wram[address - 0xC000] = value
            return
        if address <= 0xFDFF:
            self.wram[address - 0xE000] = value
            return
        if address <= 0xFE9F:
            if not self._oam_write_accessible():
                return
            self.oam[address - 0xFE00] = value
            return
        if address <= 0xFEFF:
            return
        if address <= 0xFF7F:
            offset = address - 0xFF00
            if offset in UNUSABLE_IO_OFFSETS or offset in CGB_ONLY_IO_OFFSETS:
                return
            if address == 0xFF00:
                self.joypad.write_select(value)
                return
            if address == 0xFF04:
                self._write_div()
                return
            elif address == 0xFF05:
                self._write_tima(value)
                return
            elif address == 0xFF06:
                self._write_tma(value)
                return
            elif address == 0xFF07:
                self._write_tac(value)
                return
            elif 0xFF10 <= address <= 0xFF3F:
                self.apu.write(offset, value)
                return
            elif address == 0xFF40:
                old_value = self.io[offset]
                if not (old_value ^ value) & 0x80:
                    self.ppu.before_lcdc_write()
                    self.io[offset] = value
                    self.ppu.after_lcdc_write()
                    return
                self.io[offset] = value
                self.ppu.on_lcdc_write(old_value, value)
                return
            elif address == 0xFF41:
                value = self.ppu.on_stat_write(value)
            elif address == 0xFF44:
                return
            elif address == 0xFF45:
                self.io[offset] = value
                self.ppu.on_lyc_write()
                return
            elif offset in PPU_SCROLL_REGISTER_OFFSETS:
                self.ppu.before_scroll_register_write()
                self.io[offset] = value
                self.ppu.after_scroll_register_write()
                return
            elif offset == PPU_WINDOW_X_REGISTER_OFFSET:
                self.ppu.before_window_x_register_write()
                self.io[offset] = value
                self.ppu.after_window_x_register_write()
                return
            elif offset in PPU_RASTER_REGISTER_OFFSETS:
                self.ppu.before_render_register_write(offset)
                self.io[offset] = value
                self.ppu.after_render_register_write(offset)
                return
            elif address == 0xFF4D:
                value = (self.io[offset] & 0x80) | (value & 0x01) | 0x7E
            elif address == 0xFF46:
                self._request_oam_dma(value)
            elif address == 0xFF0F:
                value = (value & 0x1F) | 0xE0
            elif address == 0xFF50:
                if not self.boot_rom_enabled:
                    self.io[offset] |= 0x01
                    return
                self.io[offset] = value
                if value & 0x01:
                    self.boot_rom_enabled = False
                return
            self.io[offset] = value
            if address == 0xFF41:
                self.ppu.on_stat_written()
            if address == 0xFF02:
                self._write_serial_control(value)
            return
        if address <= 0xFFFE:
            self.hram[address - 0xFF80] = value
            return
        self.ie = value

    def read16(self, address: int) -> int:
        lo = self.read8(address)
        hi = self.read8((address + 1) & 0xFFFF)
        return lo | (hi << 8)

    def write16(self, address: int, value: int) -> None:
        self.write8(address, value & 0xFF)
        self.write8((address + 1) & 0xFFFF, (value >> 8) & 0xFF)

    def tick(self, cycles: int, defer_new_dma: bool = False) -> None:
        if self._stopped:
            return
        if self._oam_dma_requested and not defer_new_dma:
            self._begin_oam_dma()
        self._tick_system_counter(cycles)
        if self._serial_transfer_cycles:
            self._tick_serial(cycles)
        apu = self.apu
        if apu.output_enabled:
            apu._pending_output_cycles += cycles
            if apu._pending_output_cycles >= apu._cycles_until_subsample:
                apu._process_pending_output_cycles(flush=False)
        else:
            apu._advance_core(cycles)
        if self._oam_dma_requested:
            self._begin_oam_dma()

    def cycles_until_next_interrupt_event(self, max_cycles: int) -> int:
        if max_cycles <= 0:
            return 0
        if self._stopped:
            return max_cycles
        if self._tima_reload_delay or self._oam_dma_active or self._oam_dma_requested:
            return 1

        cycles = max_cycles
        if self._serial_transfer_cycles:
            cycles = min(cycles, self._serial_transfer_cycles)

        tac = self.io[0x07]
        if tac & 0x04:
            bit = (9, 3, 5, 7)[tac & 0x03]
            period = 1 << (bit + 1)
            cycles_until_timer_edge = period - (self._system_counter % period)
            cycles = min(cycles, cycles_until_timer_edge)

        cycles = min(cycles, self.ppu.cycles_until_next_event())
        return max(1, cycles)

    def perform_speed_switch(self) -> bool:
        key1 = self.io[0x4D]
        if not key1 & 0x01:
            return False
        self.reset_system_counter()
        self.io[0x4D] = ((key1 ^ 0x80) & 0x80) | 0x7E
        return True

    def enter_stop(self) -> None:
        self.reset_system_counter()
        self._stopped = True

    def exit_stop(self) -> None:
        self._stopped = False

    def reset_system_counter(self) -> None:
        self._system_counter = 0
        self.io[0x04] = 0

    def stop_wake_requested(self) -> bool:
        return self.joypad.stop_wake_requested()

    @property
    def interrupt_flags(self) -> int:
        return self.io[0x0F] | 0xE0

    @interrupt_flags.setter
    def interrupt_flags(self, value: int) -> None:
        self.io[0x0F] = (value & 0x1F) | 0xE0

    def _write_serial_control(self, value: int) -> None:
        if (value & 0x81) == 0x81:
            self._serial_transfer_cycles = SERIAL_INTERNAL_TRANSFER_CYCLES
        else:
            self._serial_transfer_cycles = 0

    def _tick_serial(self, cycles: int) -> None:
        if not self._serial_transfer_cycles:
            return
        self._serial_transfer_cycles -= cycles
        if self._serial_transfer_cycles <= 0:
            self._serial_transfer_cycles = 0
            self._emit_serial()

    def _emit_serial(self) -> None:
        char = chr(self.io[0x01])
        self.serial_text += char
        self.serial_sink(char)
        self.io[0x01] = 0xFF
        self.io[0x02] = self.io[0x02] & ~0x80
        self.interrupt_flags = self.interrupt_flags | 0x08

    def _tick_system_counter(self, cycles: int) -> None:
        io = self.io
        if (
            not self._tima_reload_delay
            and not self._oam_dma_active
            and not self._oam_dma_requested
        ):
            tac = io[0x07]
            system_counter = self._system_counter
            if not tac & 0x04:
                system_counter = (system_counter + cycles) & 0xFFFF
                self._system_counter = system_counter
                io[0x04] = (system_counter >> 8) & 0xFF
                self.ppu.tick(cycles)
                return

            bit = (9, 3, 5, 7)[tac & 0x03]
            period = 1 << (bit + 1)
            edge_count = (system_counter + cycles) // period - system_counter // period
            tima = io[0x05]
            if tima + edge_count <= 0xFF:
                system_counter = (system_counter + cycles) & 0xFFFF
                self._system_counter = system_counter
                io[0x04] = (system_counter >> 8) & 0xFF
                if edge_count:
                    io[0x05] = (tima + edge_count) & 0xFF
                self.ppu.tick(cycles)
                return

        if self.profile_enabled:
            self._profile_slow_system_counter_cycles += cycles
        for _ in range(cycles):
            self._tick_tima_reload_delay()
            old_signal = self._timer_signal()
            self._system_counter = (self._system_counter + 1) & 0xFFFF
            self.io[0x04] = (self._system_counter >> 8) & 0xFF
            if old_signal and not self._timer_signal():
                self._increment_tima()
            oam_dma_active_at_cycle_start = self._oam_dma_active
            if self.profile_enabled and oam_dma_active_at_cycle_start:
                self._profile_oam_dma_cycles += 1
            if oam_dma_active_at_cycle_start:
                self.ppu.on_oam_dma_active_cycle()
            self._tick_oam_dma()
            self.ppu.tick(1)
            if oam_dma_active_at_cycle_start:
                self.ppu.on_oam_dma_active_cycle()

    def _write_div(self) -> None:
        old_signal = self._timer_signal()
        old_div_apu_signal = self._div_apu_signal()
        self._system_counter = 0
        self.io[0x04] = 0
        if old_signal and not self._timer_signal():
            self._increment_tima()
        self.apu.on_div_write(div_apu_falling_edge=old_div_apu_signal)

    def _write_tac(self, value: int) -> None:
        old_signal = self._timer_signal()
        self.io[0x07] = (value & 0x07) | 0xF8
        if old_signal and not self._timer_signal():
            self._increment_tima()

    def _write_tima(self, value: int) -> None:
        if self._tima_reload_delay:
            self._tima_reload_delay = 0
        self.io[0x05] = value

    def _write_tma(self, value: int) -> None:
        self.io[0x06] = value

    def _timer_signal(self) -> bool:
        tac = self.io[0x07]
        if not tac & 0x04:
            return False
        bit = (9, 3, 5, 7)[tac & 0x03]
        return bool(self._system_counter & (1 << bit))

    def _div_apu_signal(self) -> bool:
        return bool(self._system_counter & (1 << 12))

    def _increment_tima(self) -> None:
        if self.io[0x05] == 0xFF:
            if self.profile_enabled:
                self._profile_timer_overflows += 1
            self.io[0x05] = 0x00
            self._tima_reload_delay = 4
        else:
            self.io[0x05] = (self.io[0x05] + 1) & 0xFF

    def _tick_tima_reload_delay(self) -> None:
        if not self._tima_reload_delay:
            return
        self._tima_reload_delay -= 1
        if self._tima_reload_delay == 0:
            self.io[0x05] = self.io[0x06]
            self.interrupt_flags = self.interrupt_flags | 0x04

    def _request_oam_dma(self, value: int) -> None:
        self._oam_dma_source = value << 8
        self._oam_dma_requested = True
        if self.profile_enabled:
            self._profile_oam_dma_starts += 1

    def _begin_oam_dma(self) -> None:
        self._oam_dma_requested = False
        self._oam_dma_active = True
        self._oam_dma_index = 0
        self._oam_dma_cycle_counter = 0

    def _tick_oam_dma(self) -> None:
        if not self._oam_dma_active:
            return
        self._oam_dma_cycle_counter += 1
        if self._oam_dma_cycle_counter < 4:
            return
        self._oam_dma_cycle_counter = 0
        self.oam[self._oam_dma_index] = self._read8_unblocked(
            (self._oam_dma_source + self._oam_dma_index) & 0xFFFF
        )
        self._oam_dma_index += 1
        if self._oam_dma_index >= 0xA0:
            self._oam_dma_active = False

    @staticmethod
    def _is_hram(address: int) -> bool:
        return 0xFF80 <= address <= 0xFFFE

    def _vram_read_accessible(self) -> bool:
        if not self.ppu.lcd_enabled:
            return True
        if self.ppu._scanline < VISIBLE_LINES:
            if self.ppu.mode == MODE_OAM and self.ppu.line_dots >= MODE2_DOTS - 4:
                return False
        return self.ppu.mode != MODE_DRAWING

    def _vram_write_accessible(self) -> bool:
        if not self.ppu.lcd_enabled:
            return True
        return self.ppu.mode != MODE_DRAWING

    def _oam_read_accessible(self) -> bool:
        if not self.ppu.lcd_enabled:
            return True
        if self.ppu._scanline < VISIBLE_LINES and self.ppu.line_dots >= DOTS_PER_LINE - 4:
            return False
        return self.ppu.mode in {MODE_HBLANK, MODE_VBLANK}

    def _oam_write_accessible(self) -> bool:
        if not self.ppu.lcd_enabled:
            return True
        if (
            self.ppu._scanline < VISIBLE_LINES
            and self.ppu.mode == MODE_OAM
            and self.ppu.line_dots >= MODE2_DOTS - 4
        ):
            return True
        return self.ppu.mode in {MODE_HBLANK, MODE_VBLANK}

    @staticmethod
    def _stdout_serial_sink(char: str) -> None:
        print(char, end="", flush=True)

    def _initialize_io_defaults(self) -> None:
        # Common post-boot DMG values. These help test ROMs that skip the boot ROM.
        defaults = {
            0x00: 0xCF,
            0x01: 0x00,
            0x02: 0x7E,
            0x04: 0xAB,
            0x05: 0x00,
            0x06: 0x00,
            0x07: 0xF8,
            0x0F: 0xE1,
            0x10: 0x80,
            0x11: 0xBF,
            0x12: 0xF3,
            0x14: 0xBF,
            0x16: 0x3F,
            0x17: 0x00,
            0x19: 0xBF,
            0x1A: 0x7F,
            0x1B: 0xFF,
            0x1C: 0x9F,
            0x1E: 0xBF,
            0x20: 0xFF,
            0x21: 0x00,
            0x22: 0x00,
            0x23: 0xBF,
            0x24: 0x77,
            0x25: 0xF3,
            0x26: 0xF1,
            0x40: 0x91,
            0x41: 0x80,
            0x42: 0x00,
            0x43: 0x00,
            0x44: 0x00,
            0x45: 0x00,
            0x47: 0xFC,
            0x48: 0xFF,
            0x49: 0xFF,
            0x4A: 0x00,
            0x4B: 0x00,
        }
        for offset, value in defaults.items():
            self.io[offset] = value

    def _initialize_vram_defaults(self) -> None:
        # The DMG boot ROM leaves the registered-trademark logo tile in VRAM.
        # Some hardware timing ROMs reuse it without copying their own tile data.
        start = DMG_POST_BOOT_REGISTERED_MARK_TILE_ADDRESS
        self.vram[start : start + len(DMG_POST_BOOT_REGISTERED_MARK_TILE)] = (
            DMG_POST_BOOT_REGISTERED_MARK_TILE
        )
