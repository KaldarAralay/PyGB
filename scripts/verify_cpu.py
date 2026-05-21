from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emulator import Emulator  # noqa: E402


DEFAULT_MAX_STEPS = 50_000_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run unit tests and Blargg cpu_instrs ROM verification."
    )
    parser.add_argument(
        "--rom-root",
        type=Path,
        default=ROOT / "roms" / "cpu_instrs" / "cpu_instrs",
        help="Directory containing cpu_instrs.gb and individual/*.gb",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Maximum CPU steps per ROM before treating it as timed out.",
    )
    parser.add_argument("--skip-unit", action="store_true", help="Skip unittest discovery.")
    parser.add_argument("--skip-roms", action="store_true", help="Skip Blargg ROM runs.")
    return parser.parse_args()


def run_unit_tests() -> bool:
    print("== Unit tests ==")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    return result.wasSuccessful()


def run_rom(path: Path, max_steps: int) -> tuple[bool, str, int, int]:
    serial_chars: list[str] = []
    emulator = Emulator.from_rom_file(path, serial_sink=serial_chars.append)

    for steps in range(1, max_steps + 1):
        emulator.step()
        serial = "".join(serial_chars)
        if "Passed" in serial:
            return True, serial, steps, emulator.cpu.cycles
        if "Failed" in serial:
            return False, serial, steps, emulator.cpu.cycles

    return False, "".join(serial_chars), max_steps, emulator.cpu.cycles


def run_blargg_cpu_roms(rom_root: Path, max_steps: int) -> bool:
    print("== Blargg cpu_instrs ROMs ==")
    combined = rom_root / "cpu_instrs.gb"
    individual_dir = rom_root / "individual"
    roms = sorted(individual_dir.glob("*.gb")) + [combined]
    if not roms or not combined.exists():
        print(f"Missing Blargg cpu_instrs ROMs under {rom_root}")
        return False

    ok = True
    for rom in roms:
        passed, serial, steps, cycles = run_rom(rom, max_steps)
        status = "PASS" if passed else "FAIL"
        summary = serial.replace("\n", "\\n")
        print(f"{status} {rom.name} steps={steps} cycles={cycles} serial={summary!r}")
        ok = ok and passed
    return ok


def main() -> int:
    args = parse_args()
    ok = True
    if not args.skip_unit:
        ok = run_unit_tests() and ok
    if not args.skip_roms:
        ok = run_blargg_cpu_roms(args.rom_root, args.max_steps) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
