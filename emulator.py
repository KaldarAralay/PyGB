from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from audio import AudioSample
from bus import Bus
from cartridge import Cartridge
from cpu import CPU
from joypad import Joypad


class Emulator:
    def __init__(
        self,
        cartridge: Cartridge,
        *,
        serial_sink: Callable[[str], None] | None = None,
        start_pc: int = 0x0100,
        post_boot: bool = True,
        boot_rom: bytes | None = None,
    ) -> None:
        self.cartridge = cartridge
        self._serial_sink = serial_sink
        self._start_pc = start_pc
        self._post_boot = post_boot
        self._boot_rom = bytes(boot_rom) if boot_rom is not None else None
        self.bus = Bus(cartridge, serial_sink=serial_sink, boot_rom=boot_rom)
        self.cpu = CPU(self.bus, start_pc=start_pc, post_boot=post_boot)

    @classmethod
    def from_rom_file(
        cls,
        path: str | Path,
        *,
        serial_sink: Callable[[str], None] | None = None,
        start_pc: int = 0x0100,
        post_boot: bool = True,
        boot_rom: bytes | None = None,
    ) -> "Emulator":
        return cls(
            Cartridge.from_file(path),
            serial_sink=serial_sink,
            start_pc=start_pc,
            post_boot=post_boot,
            boot_rom=boot_rom,
        )

    def set_buttons(self, buttons: str | set[str]) -> None:
        if isinstance(buttons, str):
            normalized = Joypad.normalize_buttons(buttons)
        else:
            normalized = set(buttons)
        self.bus.joypad.set_pressed(normalized)

    def step(self, trace: bool = False) -> int:
        return self.cpu.step(trace=trace)

    def run(
        self,
        *,
        max_instructions: int | None = None,
        max_frames: int | None = None,
        stop_on_serial_result: bool = False,
        trace: bool = False,
        trace_sink=None,
        step_mode: bool = False,
        audio_sink: Callable[[list[AudioSample]], None] | None = None,
    ) -> None:
        if max_instructions is not None and max_instructions < 0:
            raise ValueError("max_instructions must be non-negative")
        if max_frames is not None and max_frames < 0:
            raise ValueError("max_frames must be non-negative")
        frame_target = None if max_frames is None else self.bus.ppu.frame_count + max_frames

        def stop_condition() -> bool:
            serial_done = stop_on_serial_result and (
                "Passed" in self.bus.serial_text or "Failed" in self.bus.serial_text
            )
            frame_done = frame_target is not None and self.bus.ppu.frame_count >= frame_target
            return serial_done or frame_done

        def drain_audio() -> None:
            if audio_sink is None:
                return
            samples = self.drain_audio_samples()
            if samples:
                audio_sink(samples)

        self.cpu.run(
            max_instructions=max_instructions,
            trace=trace,
            trace_sink=trace_sink,
            step_mode=step_mode,
            stop_condition=stop_condition if stop_on_serial_result or frame_target is not None else None,
            after_step=drain_audio if audio_sink is not None else None,
        )
        drain_audio()

    def run_frame(self, *, max_instructions: int | None = None) -> None:
        self.run(max_instructions=max_instructions, max_frames=1)

    def reset(self, *, preserve_ram: bool = True) -> None:
        self.cartridge = self.cartridge.clone_for_reset(preserve_ram=preserve_ram)
        self.bus = Bus(self.cartridge, serial_sink=self._serial_sink, boot_rom=self._boot_rom)
        self.cpu = CPU(self.bus, start_pc=self._start_pc, post_boot=self._post_boot)

    def drain_audio_samples(self) -> list[AudioSample]:
        return self.bus.apu.drain_audio_samples()

    def save_ram_data(self) -> bytes:
        return self.cartridge.dump_ram()

    def load_ram_data(self, data: bytes) -> None:
        self.cartridge.load_ram(data)

    def load_save_file(self, path: str | Path) -> None:
        self.cartridge.load_ram_file(path)

    def save_save_file(self, path: str | Path) -> None:
        self.cartridge.save_ram_file(path)
