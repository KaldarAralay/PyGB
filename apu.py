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
AUDIO_OUTPUT_BATCH_SUBSAMPLES = 64

NR52 = 0x26
NR50 = 0x24
NR51 = 0x25
WAVE_RAM_START = 0x30
WAVE_RAM_END = 0x3F
DMG_WAVE_RAM_ACCESS_WINDOW_CYCLES = 1
DMG_WAVE_TRIGGER_DELAY_CYCLES = 6
DMG_WAVE_RETRIGGER_CORRUPTION_DELAY_CYCLES = 2
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
NOISE_LFSR_MASK = 0x7FFF
NOISE_LFSR_FAST_FORWARD_THRESHOLD = 16
_NOISE_LFSR_JUMP_POWERS: dict[bool, list[tuple[int, ...]]] = {}

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


def _clock_noise_lfsr_state(state: int, *, width_mode: bool) -> int:
    xor_bit = (state & 0x01) ^ ((state >> 1) & 0x01)
    state = (state >> 1) | (xor_bit << 14)
    if width_mode:
        state = (state & ~0x40) | (xor_bit << 6)
    return state & NOISE_LFSR_MASK


def _apply_noise_lfsr_matrix(state: int, matrix: tuple[int, ...]) -> int:
    result = 0
    bit = 0
    while state:
        if state & 0x01:
            result ^= matrix[bit]
        state >>= 1
        bit += 1
    return result & NOISE_LFSR_MASK


def _noise_lfsr_jump_powers(width_mode: bool, bit_count: int) -> list[tuple[int, ...]]:
    powers = _NOISE_LFSR_JUMP_POWERS.setdefault(width_mode, [])
    if not powers:
        powers.append(
            tuple(
                _clock_noise_lfsr_state(1 << bit, width_mode=width_mode)
                for bit in range(15)
            )
        )
    while len(powers) < bit_count:
        previous = powers[-1]
        powers.append(
            tuple(_apply_noise_lfsr_matrix(value, previous) for value in previous)
        )
    return powers


def _advance_noise_lfsr_state(state: int, steps: int, *, width_mode: bool) -> int:
    state &= NOISE_LFSR_MASK
    if steps <= 0:
        return state
    if steps <= NOISE_LFSR_FAST_FORWARD_THRESHOLD:
        for _ in range(steps):
            state = _clock_noise_lfsr_state(state, width_mode=width_mode)
        return state
    powers = _noise_lfsr_jump_powers(width_mode, steps.bit_length())
    bit = 0
    while steps:
        if steps & 0x01:
            state = _apply_noise_lfsr_matrix(state, powers[bit])
        steps >>= 1
        bit += 1
    return state


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
        self._pending_core_cycles = 0
        self._cycles_until_subsample = self._cycles_until_next_subsample()
        self._output_batch_cycles = max(
            self._cycles_until_subsample,
            self._output_process_batch_cycles_for_rate(),
        )
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
        self._sweep_pace_latch = 0
        self._sweep_negate_calculated = False
        self.frequency_timers = [0, 0, 0, 0]
        self.duty_positions = [0, 0]
        self.wave_position = 0
        self.wave_sample_buffer = 0
        self._wave_ram_access_offset: int | None = None
        self._wave_ram_access_cycles_remaining = 0
        self.noise_lfsr = 0x7FFF
        self._pending_noise_cycles = 0
        self._pending_noise_lfsr_steps = 0
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
                access_offset = self._active_wave_ram_access_offset()
                if access_offset is None:
                    return 0xFF
                return self.bus.io[access_offset]
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
                access_offset = self._active_wave_ram_access_offset()
                if access_offset is not None:
                    self._profile_register_write()
                    self.bus.io[access_offset] = value
                return
            self._profile_register_write()
            self.bus.io[offset] = value
            return
        if offset in UNUSED_AUDIO_REGISTERS:
            return
        if not self.powered:
            if offset in LENGTH_REGISTERS:
                self._profile_register_write()
                self._write_length_register(LENGTH_REGISTERS[offset], value)
            return

        self._profile_register_write()
        old_value = self.bus.io[offset]
        if offset in {0x21, 0x22}:
            self._flush_pending_noise_lfsr_steps()
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

    def _defer_core_cycles(self, cycles: int) -> None:
        if cycles <= 0:
            return
        self._pending_core_cycles += cycles
        if self._pending_core_cycles >= self._cycles_until_next_core_event():
            self._flush_pending_core_cycles()

    def _cycles_until_next_core_event(self) -> int:
        if not self.powered:
            return FRAME_SEQUENCER_PERIOD
        remaining = FRAME_SEQUENCER_PERIOD - self._frame_sequence_counter
        return remaining if remaining > 0 else 1

    def _flush_pending_core_cycles(self) -> None:
        cycles = self._pending_core_cycles
        if cycles <= 0:
            return
        self._pending_core_cycles = 0
        self._advance_core(cycles)

    def _advance_core(self, cycles: int) -> None:
        if cycles <= 0:
            return
        io = self.bus.io
        powered = bool(io[NR52] & 0x80)
        if not powered:
            return
        enabled = self._channel_output_enabled
        volumes = self.channel_volumes
        envelopes = self._envelope_enabled
        clock_ch0 = enabled[0] and (
            volumes[0] > 0 or (envelopes[0] and io[0x12] & 0x08)
        )
        clock_ch1 = enabled[1] and (
            volumes[1] > 0 or (envelopes[1] and io[0x17] & 0x08)
        )
        clock_ch2 = enabled[2]
        nr42 = io[0x21]
        defer_ch3 = enabled[3] and volumes[3] == 0 and not (
            nr42 & 0x08 and nr42 & 0x07
        )
        clock_ch3 = enabled[3] and not defer_ch3
        if clock_ch0 or clock_ch1 or clock_ch2 or clock_ch3 or defer_ch3:
            self._tick_frequency_timers_selected(
                cycles,
                clock_ch0=bool(clock_ch0),
                clock_ch1=bool(clock_ch1),
                clock_ch2=bool(clock_ch2),
                clock_ch3=bool(clock_ch3),
                defer_ch3=bool(defer_ch3),
                io=io,
                timers=self.frequency_timers,
            )
        self._frame_sequence_counter += cycles
        while self._frame_sequence_counter >= FRAME_SEQUENCER_PERIOD:
            self._frame_sequence_counter -= FRAME_SEQUENCER_PERIOD
            self._advance_frame_sequence(clock_units=powered)

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
        enabled = self._channel_output_enabled
        volumes = self.channel_volumes
        duty_positions = self.duty_positions
        duty_patterns = PULSE_DUTY_PATTERNS

        nr12 = io[0x12]
        if nr12 & 0xF8:
            sample = 0
            if enabled[0]:
                duty = (io[0x11] >> 6) & 0x03
                if duty_patterns[duty][duty_positions[0]]:
                    sample = volumes[0]
            dac_sample = 15 - sample * 2
            if nr51 & 0x01:
                right += dac_sample
            if nr51 & 0x10:
                left += dac_sample

        nr17 = io[0x17]
        if nr17 & 0xF8:
            sample = 0
            if enabled[1]:
                duty = (io[0x16] >> 6) & 0x03
                if duty_patterns[duty][duty_positions[1]]:
                    sample = volumes[1]
            dac_sample = 15 - sample * 2
            if nr51 & 0x02:
                right += dac_sample
            if nr51 & 0x20:
                left += dac_sample

        nr1a = io[0x1A]
        if nr1a & 0x80:
            sample = 0
            if enabled[2]:
                volume_code = (io[0x1C] >> 5) & 0x03
                if volume_code != 0:
                    sample = self.wave_sample_buffer >> (volume_code - 1)
            dac_sample = 15 - sample * 2
            if nr51 & 0x04:
                right += dac_sample
            if nr51 & 0x40:
                left += dac_sample

        nr21 = io[0x21]
        if nr21 & 0xF8:
            if not (volumes[3] == 0 and not (nr21 & 0x08 and nr21 & 0x07)):
                self._flush_pending_noise_lfsr_steps()
            sample = 0
            if enabled[3] and not self.noise_lfsr & 0x01:
                sample = volumes[3]
            dac_sample = 15 - sample * 2
            if nr51 & 0x08:
                right += dac_sample
            if nr51 & 0x80:
                left += dac_sample

        return left * left_volume, right * right_volume

    def output_sample(self) -> tuple[int, int]:
        self._flush_pending_output_cycles()
        return self._high_pass_filter_sample(self.mix_sample())

    def drain_audio_samples(self, *, flush: bool = True) -> list[tuple[int, int]]:
        if flush:
            self._flush_pending_output_cycles()
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
            self.frame_sequence_step = 3
        else:
            self.bus.io[NR52] = 0x80 | (self.bus.io[NR52] & 0x0F)

    def _power_off(self) -> None:
        for offset in range(0x10, 0x26):
            self.bus.io[offset] = 0
        self.bus.io[NR52] = 0
        self.channel_active = 0
        self._reset_output_timing()
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
        self._sweep_pace_latch = 0
        self._sweep_negate_calculated = False
        self.frequency_timers = [0, 0, 0, 0]
        self.duty_positions = [0, 0]
        self.wave_position = 0
        self.wave_sample_buffer = 0
        self._clear_wave_ram_access_window()
        self.noise_lfsr = 0x7FFF
        self._pending_noise_cycles = 0
        self._pending_noise_lfsr_steps = 0

    def _trigger_channel(self, channel: int) -> None:
        was_channel_active = self._wave_channel_active() if channel == 2 else False
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
        if channel == 2 and was_channel_active:
            self._corrupt_wave_ram_on_active_retrigger()
        if channel == 0:
            self._restart_sweep()
        reload_cycles = self._frequency_timer_reload(channel)
        if reload_cycles is None:
            self.frequency_timers[channel] = 0
        elif channel in {0, 1}:
            self.frequency_timers[channel] = reload_cycles + (self.frequency_timers[channel] & 0x03)
        elif channel == 2:
            self.frequency_timers[channel] = reload_cycles + DMG_WAVE_TRIGGER_DELAY_CYCLES
        else:
            self.frequency_timers[channel] = reload_cycles
        if channel == 2:
            self.wave_position = 0
            self._clear_wave_ram_access_window()
        elif channel == 3:
            self.noise_lfsr = 0x7FFF
            self._pending_noise_cycles = 0
            self._pending_noise_lfsr_steps = 0

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
        if channel == 2:
            self._clear_wave_ram_access_window()
        if channel == 3:
            self._pending_noise_cycles = 0
            self._pending_noise_lfsr_steps = 0

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

    def _high_pass_filter_values(self, left: int, right: int) -> tuple[int, int]:
        io = self.bus.io
        if not (((io[0x12] | io[0x17] | io[0x21]) & 0xF8) or (io[0x1A] & 0x80)):
            return (0, 0)
        capacitors = self._high_pass_capacitors
        charge_factor = self._high_pass_charge_factor
        left_input = float(left)
        right_input = float(right)
        left_output = left_input - capacitors[0]
        right_output = right_input - capacitors[1]
        capacitors[0] = left_input - left_output * charge_factor
        capacitors[1] = right_input - right_output * charge_factor
        return int(round(left_output)), int(round(right_output))

    def _high_pass_filter_sample(self, sample: tuple[int, int]) -> tuple[int, int]:
        return self._high_pass_filter_values(sample[0], sample[1])

    def _reset_high_pass_filter(self) -> None:
        self._high_pass_capacitors = [0.0, 0.0]

    def _reset_resampler(self) -> None:
        self._resample_left_accumulator = 0
        self._resample_right_accumulator = 0
        self._resample_sample_count = 0

    def _reset_output_timing(self) -> None:
        self._sample_cycle_accumulator = 0
        self._pending_output_cycles = 0
        self._pending_core_cycles = 0
        self._cycles_until_subsample = self._cycles_until_next_subsample()
        self._output_batch_cycles = max(
            self._cycles_until_subsample,
            self._output_process_batch_cycles_for_rate(),
        )

    def _cycles_until_next_subsample(self) -> int:
        remaining = CPU_CLOCK_HZ - self._sample_cycle_accumulator
        if remaining <= 0:
            return 1
        subsample_rate = self._output_subsample_rate
        return max(1, (remaining + subsample_rate - 1) // subsample_rate)

    def _output_process_batch_cycles_for_rate(self) -> int:
        return max(
            1,
            CPU_CLOCK_HZ * AUDIO_OUTPUT_BATCH_SUBSAMPLES + self._output_subsample_rate - 1
        ) // self._output_subsample_rate

    def _high_pass_charge_factor_for_sample_rate(self) -> float:
        return DMG_HIGH_PASS_CHARGE_FACTOR ** (CPU_CLOCK_HZ / self.sample_rate)

    def _any_channel_dac_enabled(self) -> bool:
        io = self.bus.io
        return bool(((io[0x12] | io[0x17] | io[0x21]) & 0xF8) or (io[0x1A] & 0x80))

    def _wave_channel_active(self) -> bool:
        return self.powered and bool(self._channel_output_enabled[2])

    def _active_wave_ram_access_offset(self) -> int | None:
        if self._wave_ram_access_cycles_remaining <= 0:
            return None
        return self._wave_ram_access_offset

    def _clear_wave_ram_access_window(self) -> None:
        self._wave_ram_access_offset = None
        self._wave_ram_access_cycles_remaining = 0

    def _age_wave_ram_access_window(self, cycles: int) -> None:
        if self._wave_ram_access_cycles_remaining <= 0:
            self._clear_wave_ram_access_window()
            return
        self._wave_ram_access_cycles_remaining -= cycles
        if self._wave_ram_access_cycles_remaining <= 0:
            self._clear_wave_ram_access_window()

    def _open_wave_ram_access_window(
        self,
        access_offset: int,
        *,
        cycles_after_access: int,
    ) -> None:
        remaining = DMG_WAVE_RAM_ACCESS_WINDOW_CYCLES - cycles_after_access
        if remaining <= 0:
            self._clear_wave_ram_access_window()
            return
        self._wave_ram_access_offset = access_offset
        self._wave_ram_access_cycles_remaining = remaining

    def _corrupt_wave_ram_on_active_retrigger(self) -> None:
        access_offset = self._wave_ram_access_offset_after_cycles(
            DMG_WAVE_RETRIGGER_CORRUPTION_DELAY_CYCLES
        )
        if access_offset is None:
            return
        index = access_offset - WAVE_RAM_START
        if index < 4:
            self.bus.io[WAVE_RAM_START] = self.bus.io[access_offset]
            return
        source = WAVE_RAM_START + (index & 0x0C)
        self.bus.io[WAVE_RAM_START : WAVE_RAM_START + 4] = self.bus.io[
            source : source + 4
        ]

    def _wave_ram_access_offset_after_cycles(self, cycles: int) -> int | None:
        if not self._wave_channel_active():
            return None
        timer = self.frequency_timers[2]
        if timer <= 0 or cycles < timer:
            return None
        reload_cycles = self._frequency_timer_reload(2)
        if reload_cycles is None:
            return None
        elapsed_after_first_tick = cycles - timer
        if elapsed_after_first_tick % reload_cycles != 0:
            return None
        steps = 1 + elapsed_after_first_tick // reload_cycles
        wave_position = (self.wave_position + steps) & 0x1F
        return WAVE_RAM_START + (wave_position // 2)

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

    def _advance_frame_sequence(self, *, clock_units: bool = True) -> None:
        self.frame_sequence_step = (self.frame_sequence_step + 1) & 0x07
        if clock_units:
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
        self._sweep_pace_latch = pace
        self._sweep_timer = self._sweep_pace_latch or 8
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
        self._sweep_pace_latch = pace
        self._sweep_timer = pace or 8
        if pace == 0:
            return

        new_period = self._calculate_sweep_period()
        if self._disable_ch1_if_sweep_overflows(new_period):
            return
        if self._sweep_shift() == 0:
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
        clock_ch0 = enabled[0] and (
            volumes[0] > 0 or (self._envelope_enabled[0] and io[0x12] & 0x08)
        )
        clock_ch1 = enabled[1] and (
            volumes[1] > 0 or (self._envelope_enabled[1] and io[0x17] & 0x08)
        )
        clock_ch2 = enabled[2]
        nr42 = io[0x21]
        defer_ch3 = enabled[3] and volumes[3] == 0 and not (
            nr42 & 0x08 and nr42 & 0x07
        )
        clock_ch3 = enabled[3] and not defer_ch3
        if not (clock_ch0 or clock_ch1 or clock_ch2 or clock_ch3 or defer_ch3):
            return
        self._tick_frequency_timers_selected(
            cycles,
            clock_ch0=bool(clock_ch0),
            clock_ch1=bool(clock_ch1),
            clock_ch2=bool(clock_ch2),
            clock_ch3=bool(clock_ch3),
            defer_ch3=bool(defer_ch3),
            io=io,
            timers=timers,
        )

    def _tick_frequency_timers_selected(
        self,
        cycles: int,
        *,
        clock_ch0: bool,
        clock_ch1: bool,
        clock_ch2: bool,
        clock_ch3: bool,
        defer_ch3: bool,
        io: bytearray,
        timers: list[int],
    ) -> None:

        if clock_ch0:
            period = io[0x13] | ((io[0x14] & 0x07) << 8)
            reload_cycles = max(1, (2048 - period) * 4)
            timer = timers[0] - cycles
            while timer <= 0:
                timer += reload_cycles
                self.duty_positions[0] = (self.duty_positions[0] + 1) & 0x07
            timers[0] = timer

        if clock_ch1:
            period = io[0x18] | ((io[0x19] & 0x07) << 8)
            reload_cycles = max(1, (2048 - period) * 4)
            timer = timers[1] - cycles
            while timer <= 0:
                timer += reload_cycles
                self.duty_positions[1] = (self.duty_positions[1] + 1) & 0x07
            timers[1] = timer

        if clock_ch2:
            period = io[0x1D] | ((io[0x1E] & 0x07) << 8)
            reload_cycles = max(1, (2048 - period) * 2)
            timer = timers[2]
            if cycles >= timer:
                elapsed_after_first_tick = cycles - timer
                steps = 1 + elapsed_after_first_tick // reload_cycles
                cycles_after_last_access = elapsed_after_first_tick % reload_cycles
                timer = reload_cycles - cycles_after_last_access
                self.wave_position = (self.wave_position + steps) & 0x1F
                access_offset = WAVE_RAM_START + (self.wave_position // 2)
                sample_byte = io[access_offset]
                self.wave_sample_buffer = (
                    sample_byte & 0x0F if self.wave_position & 1 else sample_byte >> 4
                )
                self._open_wave_ram_access_window(
                    access_offset,
                    cycles_after_access=cycles_after_last_access,
                )
            else:
                timer -= cycles
                self._age_wave_ram_access_window(cycles)
            timers[2] = timer
        else:
            self._age_wave_ram_access_window(cycles)

        if defer_ch3:
            self._pending_noise_cycles += cycles
        elif clock_ch3:
            self._flush_pending_noise_lfsr_steps()
            value = io[0x22]
            shift = (value >> 4) & 0x0F
            if shift < 14:
                reload_cycles = max(1, NOISE_DIVISORS[value & 0x07] << shift)
                timer = timers[3]
                width_mode = value & 0x08
                if cycles >= timer:
                    elapsed_after_first_tick = cycles - timer
                    steps = 1 + elapsed_after_first_tick // reload_cycles
                    timer = reload_cycles - (elapsed_after_first_tick % reload_cycles)
                    self.noise_lfsr = _advance_noise_lfsr_state(
                        self.noise_lfsr,
                        steps,
                        width_mode=bool(width_mode),
                    )
                else:
                    timer -= cycles
                timers[3] = timer

    def _frequency_timers_can_fast_defer(self) -> bool:
        enabled = self._channel_output_enabled
        io = self.bus.io
        if enabled[0] and (
            self.channel_volumes[0] > 0 or (self._envelope_enabled[0] and io[0x12] & 0x08)
        ):
            return False
        if enabled[1] and (
            self.channel_volumes[1] > 0 or (self._envelope_enabled[1] and io[0x17] & 0x08)
        ):
            return False
        if enabled[2]:
            return False
        nr42 = io[0x21]
        if enabled[3] and not (
            self.channel_volumes[3] == 0 and not (nr42 & 0x08 and nr42 & 0x07)
        ):
            return False
        return True

    def _quiet_frequency_timers_can_skip(self) -> bool:
        return (
            not self._channel_output_enabled[2]
            and self.channel_volumes[0] == 0
            and self.channel_volumes[1] == 0
            and self.channel_volumes[3] == 0
            and not self._envelope_enabled[0]
            and not self._envelope_enabled[1]
            and not self._envelope_enabled[3]
        )

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
        self._flush_pending_noise_lfsr_steps()
        self.noise_lfsr = _clock_noise_lfsr_state(
            self.noise_lfsr,
            width_mode=bool(self.bus.io[0x22] & 0x08),
        )

    def _noise_lfsr_can_defer(self) -> bool:
        value = self.bus.io[0x21]
        return self.channel_volumes[3] == 0 and not (value & 0x08 and value & 0x07)

    def _flush_pending_noise_cycles(self) -> None:
        cycles = self._pending_noise_cycles
        if cycles <= 0:
            return
        self._pending_noise_cycles = 0
        if not self._channel_output_enabled[3]:
            return
        value = self.bus.io[0x22]
        shift = (value >> 4) & 0x0F
        if shift >= 14:
            return
        reload_cycles = max(1, NOISE_DIVISORS[value & 0x07] << shift)
        timer = self.frequency_timers[3]
        if cycles >= timer:
            elapsed_after_first_tick = cycles - timer
            steps = 1 + elapsed_after_first_tick // reload_cycles
            timer = reload_cycles - (elapsed_after_first_tick % reload_cycles)
            self._pending_noise_lfsr_steps += steps
        else:
            timer -= cycles
        self.frequency_timers[3] = timer

    def _flush_pending_noise_lfsr_steps(self) -> None:
        self._flush_pending_noise_cycles()
        if self._pending_noise_lfsr_steps <= 0:
            return
        self.noise_lfsr = _advance_noise_lfsr_state(
            self.noise_lfsr,
            self._pending_noise_lfsr_steps,
            width_mode=bool(self.bus.io[0x22] & 0x08),
        )
        self._pending_noise_lfsr_steps = 0

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
        if not self._noise_lfsr_can_defer():
            self._flush_pending_noise_lfsr_steps()
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
        constant_zero_sample = self._constant_zero_output_sample()
        if constant_zero_sample is not None and self._resampler_matches_constant(constant_zero_sample):
            self._advance_core(pending_cycles)
            total_sample_cycles = self._sample_cycle_accumulator + pending_cycles * subsample_rate
            subsamples = total_sample_cycles // CPU_CLOCK_HZ
            self._sample_cycle_accumulator = total_sample_cycles % CPU_CLOCK_HZ
            resample_count = self._resample_sample_count + subsamples
            output_samples = resample_count // AUDIO_OVERSAMPLE_FACTOR
            self._resample_sample_count = resample_count % AUDIO_OVERSAMPLE_FACTOR
            self._resample_left_accumulator = constant_zero_sample[0] * self._resample_sample_count
            self._resample_right_accumulator = constant_zero_sample[1] * self._resample_sample_count
            self._append_silent_audio_samples(output_samples)
            self._cycles_until_subsample = self._cycles_until_next_subsample()
            self._pending_output_cycles = 0
            return

        cpu_clock = CPU_CLOCK_HZ
        advance_core = self._advance_core
        queue_sample = self._queue_resampler_sample
        mix_sample = self._mix_sample_from_dacs
        sample_cycle_accumulator = self._sample_cycle_accumulator
        cycles_until_subsample = self._cycles_until_subsample
        powered = bool(self.bus.io[NR52] & 0x80)
        fast_resample = AUDIO_OVERSAMPLE_FACTOR == 2
        resample_sample_count = self._resample_sample_count
        resample_left_accumulator = self._resample_left_accumulator
        resample_right_accumulator = self._resample_right_accumulator
        append_sample = self._append_audio_sample
        high_pass_values = self._high_pass_filter_values
        while pending_cycles > 0:
            cycles_to_subsample = cycles_until_subsample
            if pending_cycles < cycles_to_subsample:
                if flush:
                    advance_core(pending_cycles)
                    sample_cycle_accumulator += pending_cycles * subsample_rate
                    pending_cycles = 0
                    remaining_cycles = cpu_clock - sample_cycle_accumulator
                    cycles_until_subsample = (
                        1
                        if remaining_cycles <= 0
                        else (remaining_cycles + subsample_rate - 1) // subsample_rate
                    )
                break

            advance_core(cycles_to_subsample)
            sample_cycle_accumulator += cycles_to_subsample * subsample_rate
            pending_cycles -= cycles_to_subsample
            while sample_cycle_accumulator >= cpu_clock:
                sample_cycle_accumulator -= cpu_clock
                sample = mix_sample() if powered else (0, 0)
                if fast_resample:
                    left, right = sample
                    if resample_sample_count == 0:
                        resample_left_accumulator = left
                        resample_right_accumulator = right
                        resample_sample_count = 1
                    else:
                        left_total = resample_left_accumulator + left
                        right_total = resample_right_accumulator + right
                        left_average = left_total // 2
                        right_average = right_total // 2
                        if left_total & 1 and left_average & 1:
                            left_average += 1
                        if right_total & 1 and right_average & 1:
                            right_average += 1
                        resample_left_accumulator = 0
                        resample_right_accumulator = 0
                        resample_sample_count = 0
                        append_sample(high_pass_values(left_average, right_average))
                else:
                    queue_sample(sample)
            remaining_cycles = cpu_clock - sample_cycle_accumulator
            cycles_until_subsample = (
                1
                if remaining_cycles <= 0
                else (remaining_cycles + subsample_rate - 1) // subsample_rate
            )
        self._sample_cycle_accumulator = sample_cycle_accumulator
        self._cycles_until_subsample = cycles_until_subsample
        self._pending_output_cycles = pending_cycles
        if fast_resample:
            self._resample_sample_count = resample_sample_count
            self._resample_left_accumulator = resample_left_accumulator
            self._resample_right_accumulator = resample_right_accumulator

    def _flush_pending_output_cycles(self) -> None:
        if self.output_enabled:
            self._process_pending_output_cycles(flush=True)
        else:
            self._flush_pending_core_cycles()

    def _queue_resampler_sample(self, sample: tuple[int, int]) -> None:
        if AUDIO_OVERSAMPLE_FACTOR == 2:
            left, right = sample
            if self._resample_sample_count == 0:
                self._resample_left_accumulator = left
                self._resample_right_accumulator = right
                self._resample_sample_count = 1
                return

            left_total = self._resample_left_accumulator + left
            right_total = self._resample_right_accumulator + right
            left_average = left_total // 2
            right_average = right_total // 2
            if left_total & 1 and left_average & 1:
                left_average += 1
            if right_total & 1 and right_average & 1:
                right_average += 1
            self._resample_left_accumulator = 0
            self._resample_right_accumulator = 0
            self._resample_sample_count = 0
            self._append_audio_sample(self._high_pass_filter_values(left_average, right_average))
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
        self._resample_left_accumulator = 0
        self._resample_right_accumulator = 0
        self._resample_sample_count = 0
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

    def _append_silent_audio_samples(self, count: int) -> None:
        if count <= 0:
            return
        audio_samples = self._audio_samples
        limit = self._audio_buffer_limit
        overflow = max(0, len(audio_samples) + count - limit)
        if overflow:
            drop_existing = min(overflow, len(audio_samples))
            for _ in range(drop_existing):
                audio_samples.popleft()
            self._dropped_audio_samples += overflow
            if self.profile_enabled:
                self._profile_dropped_samples += overflow
        audio_samples.extend([(0, 0)] * min(count, limit))
        if self.profile_enabled:
            self._profile_generated_samples += count

    def _constant_zero_output_sample(self) -> tuple[int, int] | None:
        io = self.bus.io
        if not io[NR52] & 0x80:
            return (0, 0)
        if not (((io[0x12] | io[0x17] | io[0x21]) & 0xF8) or (io[0x1A] & 0x80)):
            return (0, 0)
        enabled = self._channel_output_enabled
        volumes = self.channel_volumes
        envelopes = self._envelope_enabled
        if enabled[0] and (
            volumes[0] != 0 or (envelopes[0] and io[0x12] & 0x08 and io[0x12] & 0x07)
        ):
            return None
        if enabled[1] and (
            volumes[1] != 0 or (envelopes[1] and io[0x17] & 0x08 and io[0x17] & 0x07)
        ):
            return None
        if enabled[2] and ((io[0x1C] >> 5) & 0x03) != 0:
            return None
        if enabled[3] and (
            volumes[3] != 0 or (envelopes[3] and io[0x21] & 0x08 and io[0x21] & 0x07)
        ):
            return None
        sample = self._mix_sample_from_dacs()
        capacitors = self._high_pass_capacitors
        if abs(float(sample[0]) - capacitors[0]) > 1e-9:
            return None
        if abs(float(sample[1]) - capacitors[1]) > 1e-9:
            return None
        return sample

    def _resampler_matches_constant(self, sample: tuple[int, int]) -> bool:
        return (
            self._resample_left_accumulator == sample[0] * self._resample_sample_count
            and self._resample_right_accumulator == sample[1] * self._resample_sample_count
        )

    def _channels_are_dc_bias_only(self) -> bool:
        io = self.bus.io
        if self._channel_output_enabled[0] and not self._pulse_channel_is_zero_volume(0, io[0x12]):
            return False
        if self._channel_output_enabled[1] and not self._pulse_channel_is_zero_volume(1, io[0x17]):
            return False
        if self._channel_output_enabled[2] and ((io[0x1C] >> 5) & 0x03) != 0:
            return False
        if self._channel_output_enabled[3] and not self._pulse_channel_is_zero_volume(3, io[0x21]):
            return False
        return True

    def _pulse_channel_is_zero_volume(self, channel: int, dac_register_value: int) -> bool:
        if self.channel_volumes[channel] != 0:
            return False
        envelope_period = dac_register_value & 0x07
        envelope_increases = bool(dac_register_value & 0x08)
        return not (envelope_increases and envelope_period != 0)
