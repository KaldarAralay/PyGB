from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartridge import Cartridge
from emulator import Emulator


DOTS_PER_FRAME = 70224
DEFAULT_ROM = Path("roms/crystal.gbc")
DEFAULT_WATCH_BG_ADDRESSES = (0x9860,)

PYBOY_FFD5_WRITERS = (
    (0x00, 0x16C6),
    (0x03, 0x57FA),
    (0x03, 0x5804),
    (0x03, 0x5819),
    (0x03, 0x5823),
    (0x03, 0x5833),
    (0x0F, 0x6BFB),
    (0x22, 0x4277),
    (0x32, 0x41DC),
    (0x32, 0x4356),
    (0x42, 0x5A5C),
    (0x43, 0x6ECB),
)


def parse_int(value: str) -> int:
    return int(value, 0)


def parse_watch_addresses(value: str) -> set[int]:
    if value.lower() in {"none", "off", "false"}:
        return set()
    return {parse_int(part.strip()) & 0xFFFF for part in value.replace(",", " ").split() if part.strip()}


def selected_rom_bank(emulator: Emulator) -> int:
    pc = emulator.cpu.pc
    if pc < 0x4000:
        return 0
    mapper = emulator.bus.mapper
    if hasattr(mapper, "selected_rom_bank"):
        return int(mapper.selected_rom_bank())
    return 0


def safe_read8(emulator: Emulator, address: int) -> int:
    return emulator.bus._read8_unblocked(address & 0xFFFF)


def stack_preview(emulator: Emulator, length: int = 16) -> list[int]:
    sp = emulator.cpu.sp
    return [safe_read8(emulator, sp + offset) for offset in range(length)]


def hram_transfer_state(emulator: Emulator) -> dict[str, int]:
    return {
        f"{address:04X}": safe_read8(emulator, address)
        for address in (0xFFD4, 0xFFD5, 0xFFD6, 0xFFD7, 0xFFD9, 0xFFDA)
    }


def wram_transfer_state(emulator: Emulator) -> dict[str, int]:
    addresses = list(range(0xCF67, 0xCF73)) + [0xD002, 0xC596, 0xC597, 0xC598]
    return {f"{address:04X}": safe_read8(emulator, address) for address in addresses}


def gbemu_context(emulator: Emulator, *, address: int, value: int, old_value: int) -> dict[str, Any]:
    cpu = emulator.cpu
    bus = emulator.bus
    return {
        "wall_frame": cpu.cycles // DOTS_PER_FRAME,
        "cpu_cycles": cpu.cycles,
        "ppu_frame": bus.ppu.frame_count,
        "ly": bus.ppu._scanline,
        "mode": bus.ppu.mode,
        "line_dots": bus.ppu.line_dots,
        "rom_bank": selected_rom_bank(emulator),
        "pc": cpu.pc,
        "sp": cpu.sp,
        "a": cpu.a,
        "b": cpu.b,
        "c": cpu.c,
        "d": cpu.d,
        "e": cpu.e,
        "h": cpu.h,
        "l": cpu.l,
        "address": address & 0xFFFF,
        "old_value": old_value & 0xFF,
        "value": value & 0xFF,
        "vram_bank": bus.vram_bank,
        "wram_bank": bus.wram_bank,
        "hram_transfer": hram_transfer_state(emulator),
        "wram_transfer": wram_transfer_state(emulator),
        "stack_preview": stack_preview(emulator),
    }


def should_trace_bg_write(
    address: int,
    *,
    watch_bg_addresses: set[int],
    trace_all_bg_map_writes: bool,
) -> bool:
    if not 0x9800 <= address <= 0x9BFF:
        return False
    return trace_all_bg_map_writes or address in watch_bg_addresses


def trace_gbemu(args: argparse.Namespace) -> dict[str, Any]:
    cartridge = Cartridge.from_file(args.rom)
    emulator = Emulator(cartridge, serial_sink=lambda _: None, mode="cgb")
    events: list[dict[str, Any]] = []
    watch_bg_addresses = parse_watch_addresses(args.watch_bg_addresses)
    start_cycles = args.start_frame * DOTS_PER_FRAME
    end_cycles = args.end_frame * DOTS_PER_FRAME

    original_bus_write8 = emulator.bus.write8
    original_direct_write8 = emulator.cpu._write8_direct_fast

    def in_trace_window() -> bool:
        return start_cycles <= emulator.cpu.cycles <= end_cycles

    def record(kind: str, address: int, value: int, old_value: int, accepted: bool) -> None:
        if not in_trace_window():
            return
        if len(events) >= args.max_events:
            return
        if address == 0xFFD5 or should_trace_bg_write(
            address,
            watch_bg_addresses=watch_bg_addresses,
            trace_all_bg_map_writes=args.all_bg_map_writes,
        ):
            item = gbemu_context(emulator, address=address, value=value, old_value=old_value)
            item["kind"] = kind
            item["accepted"] = accepted
            events.append(item)

    def bus_write8(address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        old_value = safe_read8(emulator, address)
        original_bus_write8(address, value)
        accepted = safe_read8(emulator, address) == value
        record("bus.write8", address, value, old_value, accepted)

    def direct_write8(address: int, value: int, stable_cycles: int = 0) -> bool:
        address &= 0xFFFF
        value &= 0xFF
        old_value = safe_read8(emulator, address)
        accepted = original_direct_write8(address, value, stable_cycles)
        record("cpu._write8_direct_fast", address, value, old_value, accepted)
        return accepted

    emulator.bus.write8 = bus_write8  # type: ignore[method-assign]
    emulator.cpu._write8_direct_fast = direct_write8  # type: ignore[method-assign]

    while emulator.cpu.cycles < end_cycles:
        target = (emulator.cpu.cycles // DOTS_PER_FRAME + 1) * DOTS_PER_FRAME
        emulator.cpu.run(stop_condition=lambda target=target: emulator.cpu.cycles >= target)

    return {
        "engine": "gbemu",
        "start_frame": args.start_frame,
        "end_frame": args.end_frame,
        "events": events,
        "final": {
            "cpu_cycles": emulator.cpu.cycles,
            "wall_frame": emulator.cpu.cycles // DOTS_PER_FRAME,
            "ppu_frame": emulator.bus.ppu.frame_count,
            "pc": emulator.cpu.pc,
            "sp": emulator.cpu.sp,
            "ffd5": safe_read8(emulator, 0xFFD5),
            "v9860": emulator.bus.vram[0x1860],
            "hram_transfer": hram_transfer_state(emulator),
            "wram_transfer": wram_transfer_state(emulator),
        },
    }


def pyboy_context(pyboy: Any, frame: int, *, bank: int, address: int) -> dict[str, Any]:
    return {
        "frame": frame,
        "cpu_cycles": pyboy._cycles(),
        "hook_bank": bank,
        "hook_address": address,
        "pc": pyboy.register_file.PC,
        "sp": pyboy.register_file.SP,
        "a": pyboy.register_file.A,
        "ffd5": pyboy.memory[0xFFD5],
        "v9860": pyboy.memory[0, 0x9860],
        "hram_transfer": {
            f"{addr:04X}": pyboy.memory[addr]
            for addr in (0xFFD4, 0xFFD5, 0xFFD6, 0xFFD7, 0xFFD9, 0xFFDA)
        },
        "wram_transfer": {
            f"{addr:04X}": pyboy.memory[addr]
            for addr in list(range(0xCF67, 0xCF73)) + [0xD002, 0xC596, 0xC597, 0xC598]
        },
        "stack_preview": [pyboy.memory[(pyboy.register_file.SP + offset) & 0xFFFF] for offset in range(16)],
    }


def trace_pyboy(args: argparse.Namespace) -> dict[str, Any]:
    from pyboy import PyBoy

    pyboy = PyBoy(str(args.rom), window="null", sound_emulated=False, cgb=True)
    pyboy.set_emulation_speed(0)
    context: dict[str, Any] = {"events": [], "frame": 0, "max_events": args.max_events}

    def make_callback(bank: int, address: int) -> Callable[[dict[str, Any]], None]:
        def callback(ctx: dict[str, Any]) -> None:
            if ctx["frame"] < args.start_frame or len(ctx["events"]) >= ctx["max_events"]:
                return
            ctx["events"].append(pyboy_context(pyboy, ctx["frame"], bank=bank, address=address))

        return callback

    for bank, address in PYBOY_FFD5_WRITERS:
        pyboy.hook_register(bank, address, make_callback(bank, address), context)

    sampled_frames: list[dict[str, Any]] = []
    try:
        for frame in range(args.end_frame):
            context["frame"] = frame + 1
            pyboy.tick(1, render=False)
            if frame + 1 >= args.start_frame:
                sampled_frames.append(
                    {
                        "frame": frame + 1,
                        "cpu_cycles": pyboy._cycles(),
                        "pc": pyboy.register_file.PC,
                        "sp": pyboy.register_file.SP,
                        "ffd5": pyboy.memory[0xFFD5],
                        "v9860": pyboy.memory[0, 0x9860],
                    }
                )
    finally:
        pyboy.stop(save=False)

    return {
        "engine": "pyboy",
        "start_frame": args.start_frame,
        "end_frame": args.end_frame,
        "ffd5_writer_hooks": [
            {"bank": bank, "address": address} for bank, address in PYBOY_FFD5_WRITERS
        ],
        "events": context["events"],
        "sampled_frames": sampled_frames,
        "final": sampled_frames[-1] if sampled_frames else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trace Pokemon Crystal CGB tilemap transfer state around the frame-3600 oracle mismatch."
    )
    parser.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    parser.add_argument("--engine", choices=("gbemu", "pyboy", "both"), default="both")
    parser.add_argument("--start-frame", type=int, default=2380)
    parser.add_argument("--end-frame", type=int, default=3610)
    parser.add_argument("--watch-bg-addresses", default=" ".join(f"0x{addr:04X}" for addr in DEFAULT_WATCH_BG_ADDRESSES))
    parser.add_argument("--all-bg-map-writes", action="store_true")
    parser.add_argument("--max-events", type=int, default=512)
    parser.add_argument("--json-output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.start_frame < 0 or args.end_frame <= args.start_frame:
        raise SystemExit("--end-frame must be greater than --start-frame")
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")

    result: dict[str, Any] = {
        "rom": str(args.rom),
        "start_frame": args.start_frame,
        "end_frame": args.end_frame,
    }
    if args.engine in {"gbemu", "both"}:
        result["gbemu"] = trace_gbemu(args)
    if args.engine in {"pyboy", "both"}:
        result["pyboy"] = trace_pyboy(args)

    text = json.dumps(result, indent=2)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
