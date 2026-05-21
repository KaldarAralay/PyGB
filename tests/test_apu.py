from __future__ import annotations

import unittest

from apu import CPU_CLOCK_HZ, FRAME_SEQUENCER_PERIOD
from bus import Bus
from cartridge import Cartridge, compute_header_checksum


def make_rom() -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0134 : 0x0134 + len(b"APUTEST")] = b"APUTEST"
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


class APUTests(unittest.TestCase):
    def test_nr52_power_off_clears_sound_registers_and_ignores_writes(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0xFF26), 0xF1)
        bus.write8(0xFF12, 0xF3)
        bus.write8(0xFF30, 0x9A)
        bus.write8(0xFF26, 0x00)

        self.assertEqual(bus.read8(0xFF26), 0x70)
        self.assertEqual(bus.read8(0xFF12), 0x00)
        self.assertEqual(bus.read8(0xFF30), 0x9A)

        bus.write8(0xFF12, 0xF3)
        self.assertEqual(bus.read8(0xFF12), 0x00)

    def test_nr52_power_on_reenables_register_writes(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.write8(0xFF12, 0xF3)

        self.assertEqual(bus.read8(0xFF26), 0xF0)
        self.assertEqual(bus.read8(0xFF12), 0xF3)

    def test_post_boot_channel_status_does_not_output_until_triggered(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        self.assertEqual(bus.read8(0xFF26), 0xF1)
        self.assertEqual(bus.apu.sample_channels(), (0, 0, 0, 0))
        self.assertEqual(bus.apu.dac_sample_channels(), (0, 0, 0, 0))
        self.assertEqual(bus.apu.mix_sample(), (0, 0))

    def test_unused_audio_registers_read_as_ff_and_ignore_writes(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x80)

        for address in (0xFF15, 0xFF1F, 0xFF27, 0xFF2F):
            with self.subTest(address=address):
                bus.write8(address, 0x00)
                self.assertEqual(bus.read8(address), 0xFF)

    def test_trigger_sets_channel_active_only_when_dac_enabled(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF14, 0x80)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

        bus.write8(0xFF12, 0x00)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

        bus.write8(0xFF14, 0x80)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_wave_channel_dac_controls_channel_active(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF1A, 0x80)
        bus.write8(0xFF1E, 0x80)
        self.assertEqual(bus.read8(0xFF26) & 0x04, 0x04)

        bus.write8(0xFF1A, 0x00)
        self.assertEqual(bus.read8(0xFF26) & 0x04, 0x00)

    def test_wave_ram_access_is_blocked_while_channel_active(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.write8(0xFF30, 0x9A)

        bus.write8(0xFF1A, 0x80)
        bus.write8(0xFF1E, 0x80)

        self.assertEqual(bus.read8(0xFF30), 0xFF)
        bus.write8(0xFF30, 0x45)

        bus.write8(0xFF1A, 0x00)
        self.assertEqual(bus.read8(0xFF30), 0x9A)

    def test_wave_ram_access_is_normal_with_dac_on_before_trigger(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.write8(0xFF1A, 0x80)

        bus.write8(0xFF30, 0x34)

        self.assertEqual(bus.read8(0xFF30), 0x34)

    def test_frame_sequencer_ticks_at_512_hz(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        self.assertEqual(bus.apu.frame_sequence_step, 0)
        bus.tick(FRAME_SEQUENCER_PERIOD - 1)
        self.assertEqual(bus.apu.frame_sequence_step, 0)
        bus.tick(1)
        self.assertEqual(bus.apu.frame_sequence_step, 1)
        bus.tick(FRAME_SEQUENCER_PERIOD * 7)
        self.assertEqual(bus.apu.frame_sequence_step, 0)

    def test_div_write_with_div_apu_bit_high_clocks_frame_sequencer(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.apu.frame_sequence_step = 1
        bus.apu._frame_sequence_counter = 123
        bus._system_counter = 0x1000
        bus.io[0x04] = 0x10

        bus.write8(0xFF04, 0x00)

        self.assertEqual(bus.apu.frame_sequence_step, 2)
        self.assertEqual(bus.apu._frame_sequence_counter, 0)

    def test_div_write_without_div_apu_falling_edge_resets_frame_phase_only(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.apu.frame_sequence_step = 1
        bus.apu._frame_sequence_counter = 123
        bus._system_counter = 0x0FFF
        bus.io[0x04] = 0x0F

        bus.write8(0xFF04, 0x00)

        self.assertEqual(bus.apu.frame_sequence_step, 1)
        self.assertEqual(bus.apu._frame_sequence_counter, 0)

    def test_div_write_extra_div_apu_tick_clocks_length_timer(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.apu.frame_sequence_step = 1

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF11, 0x3F)
        bus.write8(0xFF14, 0xC0)
        self.assertEqual(bus.apu.length_timers[0], 1)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

        bus._system_counter = 0x1000
        bus.io[0x04] = 0x10
        bus.write8(0xFF04, 0x00)

        self.assertEqual(bus.apu.frame_sequence_step, 2)
        self.assertEqual(bus.apu.length_timers[0], 0)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_length_counter_disables_channel_on_length_step(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.tick(FRAME_SEQUENCER_PERIOD)

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF11, 0x3F)
        bus.write8(0xFF14, 0xC0)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

        bus.tick(FRAME_SEQUENCER_PERIOD)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_length_disabled_keeps_channel_active(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF11, 0x3F)
        bus.write8(0xFF14, 0x80)

        bus.tick(FRAME_SEQUENCER_PERIOD * 8)

        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

    def test_length_enable_on_next_non_length_step_clocks_immediately(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF11, 0x3F)
        bus.write8(0xFF14, 0x80)

        self.assertEqual(bus.apu.frame_sequence_step, 0)
        self.assertEqual(bus.apu.length_timers[0], 1)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

        bus.write8(0xFF14, 0x40)

        self.assertEqual(bus.apu.length_timers[0], 0)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_length_enable_before_length_step_waits_for_frame_clock(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF11, 0x3F)
        bus.write8(0xFF14, 0x80)
        bus.tick(FRAME_SEQUENCER_PERIOD)

        self.assertEqual(bus.apu.frame_sequence_step, 1)

        bus.write8(0xFF14, 0x40)

        self.assertEqual(bus.apu.length_timers[0], 1)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

        bus.tick(FRAME_SEQUENCER_PERIOD)

        self.assertEqual(bus.apu.length_timers[0], 0)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_trigger_with_zero_length_on_next_non_length_step_loads_max_minus_one(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF14, 0xC0)

        self.assertEqual(bus.apu.frame_sequence_step, 0)
        self.assertEqual(bus.apu.length_timers[0], 63)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

    def test_wave_channel_length_uses_256_cycle_counter(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.tick(FRAME_SEQUENCER_PERIOD)

        bus.write8(0xFF1A, 0x80)
        bus.write8(0xFF1B, 0xFF)
        bus.write8(0xFF1E, 0xC0)
        self.assertEqual(bus.read8(0xFF26) & 0x04, 0x04)

        bus.tick(FRAME_SEQUENCER_PERIOD)

        self.assertEqual(bus.read8(0xFF26) & 0x04, 0x00)

    def test_envelope_loads_initial_volume_on_trigger_and_increases(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0xE9)
        bus.write8(0xFF14, 0x80)
        self.assertEqual(bus.apu.channel_volumes[0], 14)

        bus.tick(FRAME_SEQUENCER_PERIOD * 7)

        self.assertEqual(bus.apu.channel_volumes[0], 15)

    def test_trigger_before_envelope_step_delays_first_envelope_clock(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.tick(FRAME_SEQUENCER_PERIOD * 6)

        self.assertEqual(bus.apu.frame_sequence_step, 6)

        bus.write8(0xFF12, 0x19)
        bus.write8(0xFF14, 0x80)

        self.assertEqual(bus.apu.channel_volumes[0], 1)
        self.assertEqual(bus.apu._envelope_timers[0], 2)

        bus.tick(FRAME_SEQUENCER_PERIOD)

        self.assertEqual(bus.apu.frame_sequence_step, 7)
        self.assertEqual(bus.apu.channel_volumes[0], 1)
        self.assertEqual(bus.apu._envelope_timers[0], 1)

        bus.tick(FRAME_SEQUENCER_PERIOD * 8)

        self.assertEqual(bus.apu.frame_sequence_step, 7)
        self.assertEqual(bus.apu.channel_volumes[0], 2)

    def test_envelope_decreases_at_programmed_period(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0x22)
        bus.write8(0xFF14, 0x80)

        bus.tick(FRAME_SEQUENCER_PERIOD * 7)
        self.assertEqual(bus.apu.channel_volumes[0], 2)

        bus.tick(FRAME_SEQUENCER_PERIOD * 8)
        self.assertEqual(bus.apu.channel_volumes[0], 1)

    def test_envelope_period_zero_does_not_change_volume(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF12, 0x78)
        bus.write8(0xFF14, 0x80)

        bus.tick(FRAME_SEQUENCER_PERIOD * 32)

        self.assertEqual(bus.apu.channel_volumes[0], 7)

    def test_ch1_sweep_adds_period_and_writes_frequency_registers(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF10, 0x11)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0x00)
        bus.write8(0xFF14, 0x82)

        bus.tick(FRAME_SEQUENCER_PERIOD * 2)

        self.assertEqual(bus.io[0x13], 0x00)
        self.assertEqual(bus.io[0x14] & 0x07, 0x03)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

    def test_ch1_sweep_subtracts_period(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF10, 0x19)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0x00)
        bus.write8(0xFF14, 0x84)

        bus.tick(FRAME_SEQUENCER_PERIOD * 2)

        self.assertEqual(bus.io[0x13], 0x00)
        self.assertEqual(bus.io[0x14] & 0x07, 0x02)
        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

    def test_ch1_sweep_overflow_disables_channel(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF10, 0x11)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0x00)
        bus.write8(0xFF14, 0x87)

        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_ch1_sweep_clearing_negate_after_calculation_disables_channel(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF10, 0x19)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0x00)
        bus.write8(0xFF14, 0x84)

        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)

        bus.write8(0xFF10, 0x11)

        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x00)

    def test_ch1_sweep_shift_zero_does_not_calculate_or_overflow(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF10, 0x10)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0x00)
        bus.write8(0xFF14, 0x87)

        bus.tick(FRAME_SEQUENCER_PERIOD * 2)

        self.assertEqual(bus.read8(0xFF26) & 0x01, 0x01)
        self.assertEqual(bus.io[0x13], 0x00)
        self.assertEqual(bus.io[0x14] & 0x07, 0x07)

    def test_pulse_frequency_timer_advances_duty_sample(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF11, 0x80)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0xFF)
        bus.write8(0xFF14, 0x87)
        self.assertEqual(bus.apu.sample_channels()[0], 15)

        bus.tick(3)
        self.assertEqual(bus.apu.sample_channels()[0], 15)
        bus.tick(1)

        self.assertEqual(bus.apu.sample_channels()[0], 0)

    def test_wave_frequency_timer_advances_wave_ram_sample(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF30, 0x3C)
        bus.write8(0xFF31, 0x90)
        bus.write8(0xFF1A, 0x80)
        bus.write8(0xFF1C, 0x20)
        bus.write8(0xFF1D, 0xFF)
        bus.write8(0xFF1E, 0x87)

        self.assertEqual(bus.apu.sample_channels()[2], 0)
        self.assertEqual(bus.apu.wave_position, 0)

        bus.tick(2)

        self.assertEqual(bus.apu.wave_position, 1)
        self.assertEqual(bus.apu.sample_channels()[2], 12)

        bus.tick(2)

        self.assertEqual(bus.apu.wave_position, 2)
        self.assertEqual(bus.apu.sample_channels()[2], 9)

    def test_wave_trigger_outputs_previous_sample_until_next_wave_read(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF30, 0x0B)
        bus.write8(0xFF1A, 0x80)
        bus.write8(0xFF1C, 0x20)
        bus.write8(0xFF1D, 0xFF)
        bus.write8(0xFF1E, 0x87)
        bus.tick(2)
        self.assertEqual(bus.apu.sample_channels()[2], 11)

        bus.write8(0xFF1E, 0x87)

        self.assertEqual(bus.apu.wave_position, 0)
        self.assertEqual(bus.apu.sample_channels()[2], 11)

    def test_noise_frequency_timer_clocks_lfsr(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF21, 0xF0)
        bus.write8(0xFF22, 0x00)
        bus.write8(0xFF23, 0x80)
        initial_lfsr = bus.apu.noise_lfsr

        bus.tick(7)
        self.assertEqual(bus.apu.noise_lfsr, initial_lfsr)
        bus.tick(1)

        self.assertNotEqual(bus.apu.noise_lfsr, initial_lfsr)

    def test_noise_clock_shift_fourteen_stops_lfsr_clock(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF21, 0xF0)
        bus.write8(0xFF22, 0xE0)
        bus.write8(0xFF23, 0x80)
        initial_lfsr = bus.apu.noise_lfsr

        bus.apu.tick(1_000_000)

        self.assertEqual(bus.read8(0xFF26) & 0x08, 0x08)
        self.assertEqual(bus.apu.noise_lfsr, initial_lfsr)

    def test_mix_sample_applies_nr50_volume_and_nr51_panning(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF11, 0x80)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0xFF)
        bus.write8(0xFF14, 0x87)
        bus.write8(0xFF24, 0x77)
        bus.write8(0xFF25, 0x11)

        self.assertEqual(bus.apu.mix_sample(), (120, 120))

    def test_mix_sample_uses_signed_dac_for_active_low_waveform(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF11, 0x00)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0xFF)
        bus.write8(0xFF14, 0x87)
        bus.write8(0xFF24, 0x77)
        bus.write8(0xFF25, 0x11)

        self.assertEqual(bus.apu.sample_channels()[0], 0)
        self.assertEqual(bus.apu.dac_sample_channels()[0], -15)
        self.assertEqual(bus.apu.mix_sample(), (-120, -120))

    def test_mix_sample_does_not_add_dac_offset_for_inactive_channels(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)
        bus.write8(0xFF24, 0x77)
        bus.write8(0xFF25, 0xFF)

        self.assertEqual(bus.apu.dac_sample_channels(), (0, 0, 0, 0))
        self.assertEqual(bus.apu.mix_sample(), (0, 0))

    def test_output_sample_high_pass_filter_reduces_constant_dc(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF11, 0x80)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0xFF)
        bus.write8(0xFF14, 0x87)
        bus.write8(0xFF24, 0x77)
        bus.write8(0xFF25, 0x11)

        first = bus.apu.output_sample()
        last = first
        for _ in range(5000):
            last = bus.apu.output_sample()

        self.assertEqual(bus.apu.mix_sample(), (120, 120))
        self.assertEqual(first, (120, 120))
        self.assertLess(abs(last[0]), abs(first[0]))
        self.assertEqual(last[0], last[1])

    def test_set_sample_rate_resets_high_pass_filter_state(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.write8(0xFF26, 0x00)
        bus.write8(0xFF26, 0x80)

        bus.write8(0xFF11, 0x80)
        bus.write8(0xFF12, 0xF0)
        bus.write8(0xFF13, 0xFF)
        bus.write8(0xFF14, 0x87)
        bus.write8(0xFF24, 0x77)
        bus.write8(0xFF25, 0x11)
        for _ in range(5000):
            bus.apu.output_sample()

        bus.apu.set_sample_rate(4)

        self.assertEqual(bus.apu.output_sample(), (120, 120))

    def test_audio_sample_buffer_uses_configured_sample_rate(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)
        bus.apu.set_sample_rate(4)

        bus.apu.tick(CPU_CLOCK_HZ // 4 - 1)
        self.assertEqual(bus.apu.drain_audio_samples(), [])

        bus.apu.tick(1)
        self.assertEqual(bus.apu.drain_audio_samples(), [(0, 0)])
        self.assertEqual(bus.apu.drain_audio_samples(), [])

    def test_audio_sample_rate_must_be_positive(self) -> None:
        bus = Bus(Cartridge(make_rom()), serial_sink=lambda _: None)

        with self.assertRaises(ValueError):
            bus.apu.set_sample_rate(0)


if __name__ == "__main__":
    unittest.main()
