from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol


CPU_CLOCK_HZ = 4_194_304
FRAME_SEQUENCER_PERIOD = 8192
DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_AUDIO_BUFFER_LIMIT = DEFAULT_SAMPLE_RATE
DMG_HIGH_PASS_CHARGE_FACTOR = 0.999958
AUDIO_OVERSAMPLE_FACTOR = 2

NR52 = 0x26
NR50 = 0x24
NR51 = 0x25
WAVE_RAM_START = 0x30
WAVE_RAM_END = 0x3F
UNUSED_AUDIO_REGISTERS = {0x15, 0x1F, *range(0x27, 0x30)}

TRIGGER_REGISTERS = {
    0x14: 0,
    0x19: 1,
    0x1E: 2,
    0x23: 3,
}

LENGTH_REGISTERS = {
    0x11: 0,
    0x16: 1,
    0x1B: 2,
    0x20: 3,
}

LENGTH_MAX = {
    0: 64,
    1: 64,
    2: 256,
    3: 64,
}

DAC_REGISTERS = {
    0: 0x12,
    1: 0x17,
    2: 0x1A,
    3: 0x21,
}

ENVELOPE_REGISTERS = {
    0x12: 0,
    0x17: 1,
    0x21: 3,
}

FREQUENCY_LOW_REGISTERS = {
    0: 0x13,
    1: 0x18,
    2: 0x1D,
}

FREQUENCY_HIGH_REGISTERS = {
    0: 0x14,
    1: 0x19,
    2: 0x1E,
}

PULSE_DUTY_PATTERNS = (
    (0, 0, 0, 0, 0, 0, 0, 1),
    (1, 0, 0, 0, 0, 0, 0, 1),
    (1, 0, 0, 0, 0, 1, 1, 1),
    (0, 1, 1, 1, 1, 1, 1, 0),
)

NOISE_DIVISORS = (8, 16, 32, 48, 64, 80, 96, 112)

READ_MASKS = {
    0x10: 0x80,
    0x11: 0x3F,
    0x13: 0xFF,
    0x14: 0xBF,
    0x16: 0x3F,
    0x18: 0xFF,
    0x19: 0xBF,
    0x1A: 0x7F,
    0x1B: 0xFF,
    0x1C: 0x9F,
    0x1D: 0xFF,
    0x1E: 0xBF,
    0x20: 0xFF,
    0x23: 0xBF,
}


class APUBus(Protocol):
    io: bytearray


@dataclass(frozen=True)
class APUProfileStats:
    tick_seconds: float
    generated_samples: int
    dropped_samples: int
    register_writes: int
    trigger_writes: int
    channel_triggers: int
    channel_disables: int


class APU:
    def __init__(
        self,
        bus: APUBus,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        audio_buffer_limit: int = DEFAULT_AUDIO_BUFFER_LIMIT,
    ) -> None:
        self.bus = bus
        if sample_rate <= 0:
            raise ValueError("APU sample rate must be positive")
        self.frame_sequence_step = 0
        self._frame_sequence_counter = 0
        self.sample_rate = sample_rate
        self._output_subsample_rate = self.sample_rate * AUDIO_OVERSAMPLE_FACTOR
        self._sample_cycle_accumulator = 0
        self._pending_output_cycles = 0
        self._cycles_until_subsample = self._cycles_until_next_subsample()
        if audio_buffer_limit <= 0:
            raise ValueError("APU audio buffer limit must be positive")
        self._audio_buffer_limit = audio_buffer_limit
        self._audio_samples: deque[tuple[int, int]] = deque()
        self._dropped_audio_samples = 0
        self.output_enabled = False
        self._high_pass_charge_factor = self._high_pass_charge_factor_for_sample_rate()
        self._high_pass_capacitors = [0.0, 0.0]
        self._resample_left_accumulator = 0
        self._resample_right_accumulator = 0
        self._resample_sample_count = 0
        self.channel_active = self.bus.io[NR52] & 0x0F
        self._channel_output_enabled = [False, False, False, False]
        self.length_timers = [0, 0, 0, 0]
        self.length_enabled = [False, False, False, False]
        self.channel_volumes = [self._initial_volume(channel) for channel in range(4)]
        self._envelope_timers = [0, 0, 0, 0]
        self._envelope_enabled = [False, False, False, False]
        self._sweep_timer = 0
        self._sweep_enabled = False
        self._sweep_shadow_period = 0
        self._sweep_negate_calculated = False
        self.frequency_timers = [0, 0, 0, 0]
        self.duty_positions = [0, 0]
        self.wave_position = 0
        self.wave_sample_buffer = 0
        self.noise_lfsr = 0x7FFF
        self.profile_enabled = False
        self._profile_tick_seconds = 0.0
        self._profile_generated_samples = 0
        self._profile_dropped_samples = 0
        self._profile_register_writes = 0
        self._profile_trigger_writes = 0
        self._profile_channel_triggers = 0
        self._profile_channel_disables = 0

    @property
    def powered(self) -> bool:
        return bool(self.bus.io[NR52] & 0x80)

    def read(self, offset: int) -> int:
        self._flush_pending_output_cycles()
        offset &= 0x7F
        if offset == NR52:
            return 0x70 | (0x80 if self.powered else 0) | (self.channel_active & 0x0F)
        if offset in UNUSED_AUDIO_REGISTERS:
            return 0xFF
        if WAVE_RAM_START <= offset <= WAVE_RAM_END:
            if self._wave_channel_active():
                return 0xFF
            return self.bus.io[offset]
        return self.bus.io[offset] | READ_MASKS.get(offset, 0x00)

    def write(self, offset: int, value: int) -> None:
        self._flush_pending_output_cycles()
        offset &= 0x7F
        value &= 0xFF
        if offset == NR52:
            self._profile_register_write()
            self._write_nr52(value)
            return
        if WAVE_RAM_START <= offset <= WAVE_RAM_END:
            if self._wave_channel_active():
                return
            self._profile_register_write()
            self.bus.io[offset] = value
            return
        if offset in UNUSED_AUDIO_REGISTERS:
            return
        if not self.powered:
            return

        self._profile_register_write()
        old_value = self.bus.io[offset]
        self.bus.io[offset] = value
        if offset == 0x10:
            self._handle_sweep_register_write(old_value, value)
        if offset in LENGTH_REGISTERS:
            self._write_length_register(LENGTH_REGISTERS[offset], value)
        if offset in TRIGGER_REGISTERS:
            self._profile_trigger_write(value)
            channel = TRIGGER_REGISTERS[offset]
            self._write_trigger_register(channel, value)
            if value & 0x80:
                self._trigger_channel(channel)
        if offset in DAC_REGISTERS.values():
            self._disable_channel_if_dac_off(offset)

    def tick(self, cycles: int) -> None:
        self._tick(cycles)

    def consume_profile(self) -> APUProfileStats:
        stats = APUProfileStats(
            tick_seconds=self._profile_tick_seconds,
            generated_samples=self._profile_generated_samples,
            dropped_samples=self._profile_dropped_samples,
            register_writes=self._profile_register_writes,
            trigger_writes=self._profile_trigger_writes,
            channel_triggers=self._profile_channel_triggers,
            channel_disables=self._profile_channel_disables,
        )
        self._profile_tick_seconds = 0.0
        self._profile_generated_samples = 0
        self._profile_dropped_samples = 0
        self._profile_register_writes = 0
        self._profile_trigger_writes = 0
        self._profile_channel_triggers = 0
        self._profile_channel_disables = 0
        return stats

    def _tick(self, cycles: int) -> None:
        if cycles <= 0:
            return
        if self.output_enabled:
            self._tick_and_sample_output(cycles)
            return
        self._advance_core(cycles)

    def _advance_core(self, cycles: int) -> None:
        if cycles <= 0 or not self.powered:
            return
        self._tick_frequency_timers(cycles)
        self._frame_sequence_counter += cycles
        while self._frame_sequence_counter >= FRAME_SEQUENCER_PERIOD:
            self._frame_sequence_counter -= FRAME_SEQUENCER_PERIOD
            self._advance_frame_sequence()

    def sample_channels(self) -> tuple[int, int, int, int]:
        self._flush_pending_output_cycles()
        if not self.powered:
            return (0, 0, 0, 0)
        return (
            self._pulse_sample(0),
            self._pulse_sample(1),
            self._wave_sample(),
            self._noise_sample(),
        )

    def dac_sample_channels(self) -> tuple[int, int, int, int]:
        self._flush_pending_output_cycles()
        if not self.powered:
            return (0, 0, 0, 0)
        return tuple(
            self._dac_output(channel, sample)
            for channel, sample in enumerate(self.sample_channels())
        )

    def mix_sample(self) -> tuple[int, int]:
        self._flush_pending_output_cycles()
        if not self.powered:
            return (0, 0)
        return self._mix_sample_from_dacs()

    def _mix_sample_from_dacs(self) -> tuple[int, int]:
        io = self.bus.io
        nr50 = io[NR50]
        nr51 = io[NR51]
        left_volume = ((nr50 >> 4) & 0x07) + 1
        right_volume = (nr50 & 0x07) + 1
        left = 0
        right = 0

        if io[0x12] & 0xF8:
            sample = 0
            if self._channel_output_enabled[0]:
                duty = (io[0x11] >> 6) & 0x03
                if PULSE_DUTY_PATTERNS[duty][self.duty_positions[0]]:
                    sample = self.channel_volumes[0]
            dac_sample = 15 - sample * 2
            if nr51 & 0x01:
                right += dac_sample
            if nr51 & 0x10:
                left += dac_sample

        if io[0x17] & 0xF8:
            sample = 0
            if self._channel_output_enabled[1]:
                duty = (io[0x16] >> 6) & 0x03
                if PULSE_DUTY_PATTERNS[duty][self.duty_positions[1]]:
                    sample = self.channel_volumes[1]
            dac_sample = 15 - sample * 2
            if nr51 & 0x02:
                right += dac_sample
            if nr51 & 0x20:
                left += dac_sample

        if io[0x1A] & 0x80:
            sample = 0
            if self._channel_output_enabled[2]:
                volume_code = (io[0x1C] >> 5) & 0x03
                if volume_code != 0:
                    sample = self.wave_sample_buffer >> (volume_code - 1)
            dac_sample = 15 - sample * 2
            if nr51 & 0x04:
                right += dac_sample
            if nr51 & 0x40:
                left += dac_sample

        if io[0x21] & 0xF8:
            sample = 0
            if self._channel_output_enabled[3] and not self.noise_lfsr & 0x01:
                sample = self.channel_volumes[3]
            dac_sample = 15 - sample * 2
            if nr51 & 0x08:
                right += dac_sample
            if nr51 & 0x80:
                left += dac_sample

        return left * left_volume, right * right_volume

    def output_sample(self) -> tuple[int, int]:
        self._flush_pending_output_cycles()
        return self._high_pass_filter_sample(self.mix_sample())

    def drain_audio_samples(self) -> list[tuple[int, int]]:
        samples = list(self._audio_samples)
        self._audio_samples.clear()
        return samples

    def set_sample_rate(self, sample_rate: int) -> None:
        if sample_rate <= 0:
            raise ValueError("APU sample rate must be positive")
        self._flush_pending_output_cycles()
        self.sample_rate = sample_rate
        self._output_subsample_rate = self.sample_rate * AUDIO_OVERSAMPLE_FACTOR
        self._high_pass_charge_factor = self._high_pass_charge_factor_for_sample_rate()
        self._reset_output_timing()
        self._audio_samples.clear()
        self._dropped_audio_samples = 0
        self._reset_resampler()
        self._reset_high_pass_filter()

    def set_output_enabled(self, enabled: bool) -> None:
        self._flush_pending_output_cycles()
        self.output_enabled = enabled
        if enabled:
            return
        self._reset_output_timing()
        self._audio_samples.clear()
        self._dropped_audio_samples = 0
        self._reset_resampler()
        self._reset_high_pass_filter()

    def on_div_write(self, *, div_apu_falling_edge: bool) -> None:
        self._flush_pending_output_cycles()
        self._frame_sequence_counter = 0
        if self.powered and div_apu_falling_edge:
            self._advance_frame_sequence()

    def _write_nr52(self, value: int) -> None:
        was_powered = self.powered
        if not value & 0x80:
            self._power_off()
        elif not was_powered:
            self.bus.io[NR52] = 0x80
            self.channel_active = 0
            self.frame_sequence_step = 0
            self._frame_sequence_counter = 0
        else:
            self.bus.io[NR52] = 0x80 | (self.bus.io[NR52] & 0x0F)

    def _power_off(self) -> None:
        for offset in range(0x10, 0x26):
            self.bus.io[offset] = 0
        self.bus.io[NR52] = 0
        self.channel_active = 0
        self.frame_sequence_step = 0
        self._frame_sequence_counter = 0
        self._reset_output_timing()
        self.length_timers = [0, 0, 0, 0]
        self.length_enabled = [False, False, False, False]
        self.channel_volumes = [0, 0, 0, 0]
        self._channel_output_enabled = [False, False, False, False]
        self._reset_resampler()
        self._reset_high_pass_filter()
        self._envelope_timers = [0, 0, 0, 0]
        self._envelope_enabled = [False, False, False, False]
        self._sweep_timer = 0
        self._sweep_enabled = False
        self._sweep_shadow_period = 0
        self._sweep_negate_calculated = False
        self.frequency_timers = [0, 0, 0, 0]
        self.duty_positions = [0, 0]
        self.wave_position = 0
        self.wave_sample_buffer = 0
        self.noise_lfsr = 0x7FFF

    def _trigger_channel(self, channel: int) -> None:
        if self.length_timers[channel] == 0:
            self.length_timers[channel] = LENGTH_MAX[channel]
            if self.length_enabled[channel] and not self._next_frame_step_clocks_length():
                self.length_timers[channel] -= 1
        self._restart_envelope(channel)
        if self._channel_dac_enabled(channel):
            self.channel_active |= 1 << channel
            self._channel_output_enabled[channel] = True
        else:
            self._disable_channel(channel)
        if channel == 0:
            self._restart_sweep()
        reload_cycles = self._frequency_timer_reload(channel)
        self.frequency_timers[channel] = reload_cycles if reload_cycles is not None else 0
        if channel == 2:
            self.wave_position = 0
        elif channel == 3:
            self.noise_lfsr = 0x7FFF

    def _channel_dac_enabled(self, channel: int) -> bool:
        register = DAC_REGISTERS[channel]
        value = self.bus.io[register]
        if channel == 2:
            return bool(value & 0x80)
        return bool(value & 0xF8)

    def _disable_channel_if_dac_off(self, offset: int) -> None:
        for channel, register in DAC_REGISTERS.items():
            if register == offset and not self._channel_dac_enabled(channel):
                self._disable_channel(channel)

    def _disable_channel(self, channel: int) -> None:
        if self.profile_enabled and self.channel_active & (1 << channel):
            self._profile_channel_disables += 1
        self.channel_active &= ~(1 << channel)
        self._channel_output_enabled[channel] = False

    def _profile_register_write(self) -> None:
        if self.profile_enabled:
            self._profile_register_writes += 1

    def _profile_trigger_write(self, value: int) -> None:
        if not self.profile_enabled:
            return
        self._profile_trigger_writes += 1
        if value & 0x80:
            self._profile_channel_triggers += 1

    def _dac_output(self, channel: int, sample: int) -> int:
        if not self._channel_dac_enabled(channel):
            return 0
        return 15 - (sample & 0x0F) * 2

    def _high_pass_filter_sample(self, sample: tuple[int, int]) -> tuple[int, int]:
        if not self._any_channel_dac_enabled():
            return (0, 0)
        left, right = sample
        left_output = float(left) - self._high_pass_capacitors[0]
        right_output = float(right) - self._high_pass_capacitors[1]
        self._high_pass_capacitors[0] = float(left) - left_output * self._high_pass_charge_factor
        self._high_pass_capacitors[1] = float(right) - right_output * self._high_pass_charge_factor
        return int(round(left_output)), int(round(right_output))

    def _reset_high_pass_filter(self) -> None:
        self._high_pass_capacitors = [0.0, 0.0]

    def _reset_resampler(self) -> None:
        self._resample_left_accumulator = 0
        self._resample_right_accumulator = 0
        self._resample_sample_count = 0

    def _reset_output_timing(self) -> None:
        self._sample_cycle_accumulator = 0
        self._pending_output_cycles = 0
        self._cycles_until_subsample = self._cycles_until_next_subsample()

    def _cycles_until_next_subsample(self) -> int:
        remaining = CPU_CLOCK_HZ - self._sample_cycle_accumulator
        if remaining <= 0:
            return 1
        subsample_rate = self._output_subsample_rate
        return max(1, (remaining + subsample_rate - 1) // subsample_rate)

    def _high_pass_charge_factor_for_sample_rate(self) -> float:
        return DMG_HIGH_PASS_CHARGE_FACTOR ** (CPU_CLOCK_HZ / self.sample_rate)

    def _any_channel_dac_enabled(self) -> bool:
        io = self.bus.io
        return bool(((io[0x12] | io[0x17] | io[0x21]) & 0xF8) or (io[0x1A] & 0x80))

    def _wave_channel_active(self) -> bool:
        return self.powered and bool(self._channel_output_enabled[2])

    def _write_length_register(self, channel: int, value: int) -> None:
        max_length = LENGTH_MAX[channel]
        mask = 0xFF if channel == 2 else 0x3F
        self.length_timers[channel] = max_length - (value & mask)

    def _write_trigger_register(self, channel: int, value: int) -> None:
        was_enabled = self.length_enabled[channel]
        self.length_enabled[channel] = bool(value & 0x40)
        if (
            not was_enabled
            and self.length_enabled[channel]
            and self.length_timers[channel] > 0
            and not self._next_frame_step_clocks_length()
        ):
            self._clock_length_timer(channel)

    def _next_frame_step_clocks_length(self) -> bool:
        return ((self.frame_sequence_step + 1) & 0x07) in {0, 2, 4, 6}

    def _next_frame_step_clocks_envelope(self) -> bool:
        return ((self.frame_sequence_step + 1) & 0x07) == 7

    def _clock_frame_sequencer(self) -> None:
        if self.frame_sequence_step in {0, 2, 4, 6}:
            self._clock_length_timers()
        if self.frame_sequence_step in {2, 6}:
            self._clock_sweep()
        if self.frame_sequence_step == 7:
            self._clock_envelopes()

    def _advance_frame_sequence(self) -> None:
        self.frame_sequence_step = (self.frame_sequence_step + 1) & 0x07
        self._clock_frame_sequencer()

    def _clock_length_timers(self) -> None:
        for channel in range(4):
            self._clock_length_timer(channel)

    def _clock_length_timer(self, channel: int) -> None:
        if not self.length_enabled[channel] or self.length_timers[channel] == 0:
            return
        self.length_timers[channel] -= 1
        if self.length_timers[channel] == 0:
            self._disable_channel(channel)

    def _restart_envelope(self, channel: int) -> None:
        register = DAC_REGISTERS[channel]
        if register not in ENVELOPE_REGISTERS:
            return
        period = self.bus.io[register] & 0x07
        self.channel_volumes[channel] = self._initial_volume(channel)
        timer = period or 8
        if self._next_frame_step_clocks_envelope():
            timer += 1
        self._envelope_timers[channel] = timer
        self._envelope_enabled[channel] = period != 0

    def _initial_volume(self, channel: int) -> int:
        register = DAC_REGISTERS[channel]
        if register not in ENVELOPE_REGISTERS:
            return 0
        return (self.bus.io[register] >> 4) & 0x0F

    def _clock_envelopes(self) -> None:
        for channel in (0, 1, 3):
            if not self._envelope_enabled[channel]:
                continue
            self._envelope_timers[channel] -= 1
            if self._envelope_timers[channel] > 0:
                continue
            register = DAC_REGISTERS[channel]
            period = self.bus.io[register] & 0x07
            self._envelope_timers[channel] = period or 8
            delta = 1 if self.bus.io[register] & 0x08 else -1
            next_volume = self.channel_volumes[channel] + delta
            if 0 <= next_volume <= 15:
                self.channel_volumes[channel] = next_volume
            else:
                self._envelope_enabled[channel] = False

    def _restart_sweep(self) -> None:
        self._sweep_shadow_period = self._channel_period(0)
        self._sweep_negate_calculated = False
        pace = self._sweep_pace()
        shift = self._sweep_shift()
        self._sweep_timer = pace or 8
        self._sweep_enabled = pace != 0 or shift != 0
        if shift != 0:
            self._disable_ch1_if_sweep_overflows(self._calculate_sweep_period())

    def _clock_sweep(self) -> None:
        if not self._sweep_enabled:
            return
        self._sweep_timer -= 1
        if self._sweep_timer > 0:
            return

        pace = self._sweep_pace()
        self._sweep_timer = pace or 8
        if pace == 0:
            return
        if self._sweep_shift() == 0:
            return

        new_period = self._calculate_sweep_period()
        if self._disable_ch1_if_sweep_overflows(new_period):
            return

        self._sweep_shadow_period = new_period
        self._write_channel_period(0, new_period)
        self._disable_ch1_if_sweep_overflows(self._calculate_sweep_period())

    def _handle_sweep_register_write(self, old_value: int, value: int) -> None:
        if (
            self._sweep_negate_calculated
            and old_value & 0x08
            and not value & 0x08
        ):
            self._disable_channel(0)

    def _sweep_pace(self) -> int:
        return (self.bus.io[0x10] >> 4) & 0x07

    def _sweep_shift(self) -> int:
        return self.bus.io[0x10] & 0x07

    def _calculate_sweep_period(self) -> int:
        delta = self._sweep_shadow_period >> self._sweep_shift()
        if self.bus.io[0x10] & 0x08:
            self._sweep_negate_calculated = True
            return self._sweep_shadow_period - delta
        return self._sweep_shadow_period + delta

    def _disable_ch1_if_sweep_overflows(self, period: int) -> bool:
        if period > 0x7FF:
            self._disable_channel(0)
            return True
        return False

    def _channel_period(self, channel: int) -> int:
        low = self.bus.io[FREQUENCY_LOW_REGISTERS[channel]]
        high = self.bus.io[FREQUENCY_HIGH_REGISTERS[channel]] & 0x07
        return low | (high << 8)

    def _write_channel_period(self, channel: int, period: int) -> None:
        period &= 0x7FF
        low_register = FREQUENCY_LOW_REGISTERS[channel]
        high_register = FREQUENCY_HIGH_REGISTERS[channel]
        self.bus.io[low_register] = period & 0xFF
        self.bus.io[high_register] = (self.bus.io[high_register] & 0xF8) | (period >> 8)

    def _tick_frequency_timers(self, cycles: int) -> None:
        enabled = self._channel_output_enabled
        volumes = self.channel_volumes
        timers = self.frequency_timers
        io = self.bus.io
        if not (
            (enabled[0] and (volumes[0] > 0 or (self._envelope_enabled[0] and io[0x12] & 0x08)))
            or (enabled[1] and (volumes[1] > 0 or (self._envelope_enabled[1] and io[0x17] & 0x08)))
            or (enabled[2] and (io[0x1C] & 0x60))
            or (enabled[3] and (volumes[3] > 0 or (self._envelope_enabled[3] and io[0x21] & 0x08)))
        ):
            return

        if enabled[0]:
            period = io[0x13] | ((io[0x14] & 0x07) << 8)
            reload_cycles = max(1, (2048 - period) * 4)
            timer = timers[0] - cycles
            while timer <= 0:
                timer += reload_cycles
                self.duty_positions[0] = (self.duty_positions[0] + 1) & 0x07
            timers[0] = timer

        if enabled[1]:
            period = io[0x18] | ((io[0x19] & 0x07) << 8)
            reload_cycles = max(1, (2048 - period) * 4)
            timer = timers[1] - cycles
            while timer <= 0:
                timer += reload_cycles
                self.duty_positions[1] = (self.duty_positions[1] + 1) & 0x07
            timers[1] = timer

        if enabled[2]:
            period = io[0x1D] | ((io[0x1E] & 0x07) << 8)
            reload_cycles = max(1, (2048 - period) * 2)
            timer = timers[2] - cycles
            while timer <= 0:
                timer += reload_cycles
                self.wave_position = (self.wave_position + 1) & 0x1F
                sample_byte = io[WAVE_RAM_START + (self.wave_position // 2)]
                self.wave_sample_buffer = sample_byte & 0x0F if self.wave_position & 1 else sample_byte >> 4
            timers[2] = timer

        if enabled[3]:
            value = io[0x22]
            shift = (value >> 4) & 0x0F
            if shift < 14:
                reload_cycles = max(1, NOISE_DIVISORS[value & 0x07] << shift)
                timer = timers[3] - cycles
                noise_lfsr = self.noise_lfsr
                width_mode = value & 0x08
                while timer <= 0:
                    timer += reload_cycles
                    xor_bit = (noise_lfsr & 0x01) ^ ((noise_lfsr >> 1) & 0x01)
                    noise_lfsr = (noise_lfsr >> 1) | (xor_bit << 14)
                    if width_mode:
                        noise_lfsr = (noise_lfsr & ~0x40) | (xor_bit << 6)
                    noise_lfsr &= 0x7FFF
                timers[3] = timer
                self.noise_lfsr = noise_lfsr

    def _frequency_timer_reload(self, channel: int) -> int | None:
        if channel in {0, 1}:
            return max(1, (2048 - self._channel_period(channel)) * 4)
        if channel == 2:
            return max(1, (2048 - self._channel_period(channel)) * 2)
        return self._noise_timer_reload()

    def _noise_timer_reload(self) -> int | None:
        value = self.bus.io[0x22]
        if ((value >> 4) & 0x0F) >= 14:
            return None
        divisor = NOISE_DIVISORS[value & 0x07]
        return max(1, divisor << ((value >> 4) & 0x0F))

    def _advance_waveform(self, channel: int) -> None:
        if channel in {0, 1}:
            self.duty_positions[channel] = (self.duty_positions[channel] + 1) & 0x07
        elif channel == 2:
            self.wave_position = (self.wave_position + 1) & 0x1F
            self.wave_sample_buffer = self._read_wave_ram_sample(self.wave_position)
        else:
            self._clock_noise_lfsr()

    def _clock_noise_lfsr(self) -> None:
        xor_bit = (self.noise_lfsr & 0x01) ^ ((self.noise_lfsr >> 1) & 0x01)
        self.noise_lfsr = (self.noise_lfsr >> 1) | (xor_bit << 14)
        if self.bus.io[0x22] & 0x08:
            self.noise_lfsr = (self.noise_lfsr & ~0x40) | (xor_bit << 6)
        self.noise_lfsr &= 0x7FFF

    def _pulse_sample(self, channel: int) -> int:
        if not self._channel_output_enabled[channel]:
            return 0
        if not self._channel_dac_enabled(channel):
            return 0
        duty_register = 0x11 if channel == 0 else 0x16
        duty = (self.bus.io[duty_register] >> 6) & 0x03
        if not PULSE_DUTY_PATTERNS[duty][self.duty_positions[channel]]:
            return 0
        return self.channel_volumes[channel]

    def _wave_sample(self) -> int:
        if not self._channel_output_enabled[2]:
            return 0
        if not self._channel_dac_enabled(2):
            return 0
        volume_code = (self.bus.io[0x1C] >> 5) & 0x03
        if volume_code == 0:
            return 0
        return self.wave_sample_buffer >> (volume_code - 1)

    def _read_wave_ram_sample(self, sample_index: int) -> int:
        sample_byte = self.bus.io[WAVE_RAM_START + ((sample_index & 0x1F) // 2)]
        if sample_index & 1:
            return sample_byte & 0x0F
        return sample_byte >> 4

    def _noise_sample(self) -> int:
        if not self._channel_output_enabled[3]:
            return 0
        if not self._channel_dac_enabled(3):
            return 0
        return self.channel_volumes[3] if not self.noise_lfsr & 0x01 else 0

    def _tick_and_sample_output(self, cycles: int) -> None:
        if cycles <= 0:
            return
        self._pending_output_cycles += cycles
        if self._pending_output_cycles < self._cycles_until_subsample:
            return
        self._process_pending_output_cycles(flush=False)

    def _process_pending_output_cycles(self, *, flush: bool) -> None:
        pending_cycles = self._pending_output_cycles
        if pending_cycles <= 0:
            return
        subsample_rate = self._output_subsample_rate
        while pending_cycles > 0:
            cycles_to_subsample = self._cycles_until_subsample
            if pending_cycles < cycles_to_subsample:
                if flush:
                    self._advance_core(pending_cycles)
                    self._sample_cycle_accumulator += pending_cycles * subsample_rate
                    pending_cycles = 0
                    self._cycles_until_subsample = self._cycles_until_next_subsample()
                break

            self._advance_core(cycles_to_subsample)
            self._sample_cycle_accumulator += cycles_to_subsample * subsample_rate
            pending_cycles -= cycles_to_subsample
            while self._sample_cycle_accumulator >= CPU_CLOCK_HZ:
                self._sample_cycle_accumulator -= CPU_CLOCK_HZ
                sample = self._mix_sample_from_dacs() if self.powered else (0, 0)
                self._queue_resampler_sample(sample)
            self._cycles_until_subsample = self._cycles_until_next_subsample()
        self._pending_output_cycles = pending_cycles

    def _flush_pending_output_cycles(self) -> None:
        self._process_pending_output_cycles(flush=True)

    def _queue_resampler_sample(self, sample: tuple[int, int]) -> None:
        if AUDIO_OVERSAMPLE_FACTOR == 2:
            if self._resample_sample_count == 0:
                self._resample_left_accumulator = sample[0]
                self._resample_right_accumulator = sample[1]
                self._resample_sample_count = 1
                return

            left_total = self._resample_left_accumulator + sample[0]
            right_total = self._resample_right_accumulator + sample[1]
            left_average = left_total // 2
            right_average = right_total // 2
            if left_total & 1 and left_average & 1:
                left_average += 1
            if right_total & 1 and right_average & 1:
                right_average += 1
            self._reset_resampler()
            self._append_audio_sample(self._high_pass_filter_sample((left_average, right_average)))
            return

        left_total = self._resample_left_accumulator + sample[0]
        right_total = self._resample_right_accumulator + sample[1]
        sample_count = self._resample_sample_count + 1
        if sample_count < AUDIO_OVERSAMPLE_FACTOR:
            self._resample_left_accumulator = left_total
            self._resample_right_accumulator = right_total
            self._resample_sample_count = sample_count
            return
        averaged_sample = (
            int(round(left_total / sample_count)),
            int(round(right_total / sample_count)),
        )
        self._reset_resampler()
        self._append_audio_sample(self._high_pass_filter_sample(averaged_sample))

    def _append_audio_sample(self, sample: tuple[int, int]) -> None:
        if len(self._audio_samples) >= self._audio_buffer_limit:
            self._audio_samples.popleft()
            self._dropped_audio_samples += 1
            if self.profile_enabled:
                self._profile_dropped_samples += 1
        self._audio_samples.append(sample)
        if self.profile_enabled:
            self._profile_generated_samples += 1
