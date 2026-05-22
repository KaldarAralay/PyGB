from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emulator import Emulator  # noqa: E402


DEFAULT_ROM = ROOT / "roms" / "PRed.gb"
DEFAULT_SAVE_FILE = ROOT / "qa-output" / "pokemon-red-round1-save.sav"
DEFAULT_SMOKE_FRAMES = 600
DEFAULT_MAPPER_FRAMES = 1800
DEFAULT_SAVE_FRAMES = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Pokemon Red as a repeatable real-ROM regression gate."
    )
    parser.add_argument("--rom", type=Path, default=DEFAULT_ROM, help="Pokemon Red ROM path.")
    parser.add_argument(
        "--smoke-frames",
        type=int,
        default=DEFAULT_SMOKE_FRAMES,
        help="Frames for the headless CLI boot smoke.",
    )
    parser.add_argument(
        "--mapper-frames",
        type=int,
        default=DEFAULT_MAPPER_FRAMES,
        help="Frames for direct emulator MBC3 activity probing.",
    )
    parser.add_argument(
        "--save-frames",
        type=int,
        default=DEFAULT_SAVE_FRAMES,
        help="Frames for the CLI --save-file round-trip.",
    )
    parser.add_argument(
        "--save-file",
        type=Path,
        default=DEFAULT_SAVE_FILE,
        help="Scratch save path used by the --save-file round-trip.",
    )
    parser.add_argument("--skip-cli-smoke", action="store_true")
    parser.add_argument("--skip-mapper-probe", action="store_true")
    parser.add_argument("--skip-save-roundtrip", action="store_true")
    return parser.parse_args()


def main_command(*args: str | Path) -> list[str]:
    return [sys.executable, "-B", str(ROOT / "main.py"), *map(str, args)]


def command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def run_cli_smoke(rom: Path, frames: int) -> bool:
    print("== Pokemon Red headless CLI smoke ==")
    command = main_command(rom, "--max-instructions", "0", "--frames", str(frames))
    print(f"command: {command_text(command)}")
    result = run_command(command)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        print(f"FAIL headless smoke returned {result.returncode}")
        return False
    expected = f"{frames} frames"
    if expected not in result.stdout:
        print(result.stdout, end="")
        print(f"FAIL headless smoke did not reach {expected}")
        return False
    print(f"PASS headless smoke reached {frames} frames")
    return True


def seeded_save_data(size: int) -> bytes:
    return bytes(((index // 0x2000) * 0x31 + index * 0x11) & 0xFF for index in range(size))


def run_save_roundtrip(rom: Path, save_file: Path, frames: int) -> bool:
    print("== Pokemon Red CLI save-file round-trip ==")
    emulator = Emulator.from_rom_file(rom)
    if not emulator.cartridge.has_persistent_data:
        print("FAIL ROM does not report persistent cartridge RAM")
        return False

    save_file.parent.mkdir(parents=True, exist_ok=True)
    seed = seeded_save_data(len(emulator.cartridge.ram))
    save_file.write_bytes(seed)

    command = main_command(
        rom,
        "--max-instructions",
        "0",
        "--frames",
        str(frames),
        "--save-file",
        save_file,
    )
    print(f"command: {command_text(command)}")
    result = run_command(command)
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        print(f"FAIL save-file smoke returned {result.returncode}")
        return False
    saved = save_file.read_bytes()
    if saved != seed:
        print(f"FAIL save data changed during {frames}-frame round-trip")
        return False
    print(f"PASS save-file round-trip preserved {len(saved)} bytes")
    return True


def run_mapper_probe(rom: Path, frames: int) -> bool:
    print("== Pokemon Red MBC3 long-run mapper probe ==")
    emulator = Emulator.from_rom_file(rom)
    cartridge = emulator.cartridge
    mapper = cartridge.mapper
    original_write_rom_control = mapper.write_rom_control
    rom_banks: list[int] = []
    ram_selects: list[int] = []
    ram_enable_states: list[bool] = []

    def record_write(address: int, value: int) -> None:
        original_write_rom_control(address, value)
        masked = address & 0x7FFF
        if 0x2000 <= masked <= 0x3FFF:
            rom_banks.append(cartridge.mbc3_rom_bank)
        elif 0x4000 <= masked <= 0x5FFF:
            ram_selects.append(cartridge.mbc3_ram_select)
        elif masked <= 0x1FFF:
            ram_enable_states.append(cartridge.ram_enabled)

    mapper.write_rom_control = record_write  # type: ignore[method-assign]
    emulator.run(max_frames=frames)
    long_run_ram_selects = sorted(set(ram_selects))

    ok = True
    if emulator.bus.ppu.frame_count < frames:
        print(f"FAIL reached {emulator.bus.ppu.frame_count} frames, expected {frames}")
        ok = False
    if cartridge.header.title != "POKEMON RED":
        print(f"FAIL unexpected title {cartridge.header.title!r}")
        ok = False
    if cartridge.mapper_status != "Mapper: MBC3":
        print(f"FAIL unexpected mapper status {cartridge.mapper_status!r}")
        ok = False
    if cartridge.rom_bank_count != 64:
        print(f"FAIL expected 64 ROM banks, saw {cartridge.rom_bank_count}")
        ok = False
    if cartridge.ram_bank_count != 4:
        print(f"FAIL expected 4 RAM banks, saw {cartridge.ram_bank_count}")
        ok = False
    if len(set(rom_banks)) < 2:
        print("FAIL long run did not observe multiple MBC3 ROM banks")
        ok = False
    if not any(ram_enable_states):
        print("FAIL long run did not observe cartridge RAM being enabled")
        ok = False
    if not any(cartridge.ram):
        print("FAIL long run did not write any cartridge RAM")
        ok = False

    ram_snapshot = cartridge.dump_ram()
    try:
        emulator.bus.write8(0x0000, 0x0A)
        for bank in range(cartridge.ram_bank_count):
            emulator.bus.write8(0x4000, bank)
            emulator.bus.write8(0xA123, 0x40 + bank)
        for bank in range(cartridge.ram_bank_count):
            emulator.bus.write8(0x4000, bank)
            observed = emulator.bus.read8(0xA123)
            expected = 0x40 + bank
            if observed != expected:
                print(
                    f"FAIL RAM bank {bank} read ${observed:02X}, expected ${expected:02X}"
                )
                ok = False
    finally:
        cartridge.load_ram(ram_snapshot)

    unique_banks = sorted(set(rom_banks))
    print(
        "PASS" if ok else "FAIL",
        f"frames={emulator.bus.ppu.frame_count}",
        f"instructions={emulator.cpu.instructions}",
        f"rom_switches={len(rom_banks)}",
        f"unique_rom_banks={unique_banks}",
        f"long_run_ram_selects={long_run_ram_selects}",
        "ram_banks_verified=0-3",
        f"ram_nonzero={sum(1 for byte in cartridge.ram if byte)}",
    )
    return ok


def main() -> int:
    args = parse_args()
    if args.smoke_frames < 0 or args.mapper_frames < 0 or args.save_frames < 0:
        raise SystemExit("frame counts must be non-negative")
    if not args.rom.exists():
        raise SystemExit(f"Missing Pokemon Red ROM: {args.rom}")

    ok = True
    if not args.skip_cli_smoke:
        ok = run_cli_smoke(args.rom, args.smoke_frames) and ok
    if not args.skip_mapper_probe:
        ok = run_mapper_probe(args.rom, args.mapper_frames) and ok
    if not args.skip_save_roundtrip:
        ok = run_save_roundtrip(args.rom, args.save_file, args.save_frames) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
