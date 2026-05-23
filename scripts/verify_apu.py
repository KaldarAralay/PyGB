from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emulator import Emulator  # noqa: E402


BLARGG_DMG_SOUND_TESTS = (
    "01-registers.gb",
    "02-len ctr.gb",
    "03-trigger.gb",
    "04-sweep.gb",
    "05-sweep details.gb",
    "06-overflow on trigger.gb",
    "07-len sweep period sync.gb",
    "08-len ctr during power.gb",
    "09-wave read while on.gb",
    "10-wave trigger while on.gb",
    "11-regs after power.gb",
    "12-wave write while on.gb",
)

EXPECTED_PASS = {
    "01-registers.gb",
    "02-len ctr.gb",
    "03-trigger.gb",
    "04-sweep.gb",
    "05-sweep details.gb",
    "06-overflow on trigger.gb",
    "07-len sweep period sync.gb",
    "08-len ctr during power.gb",
    "09-wave read while on.gb",
    "10-wave trigger while on.gb",
    "11-regs after power.gb",
    "12-wave write while on.gb",
}

KNOWN_FAILURE_REASONS: dict[str, str] = {}

DMG_SOUND_SIGNATURE = (0xDE, 0xB0, 0x61)
RESULT_STATUS_ADDR = 0xA000
RESULT_SIGNATURE_ADDR = 0xA001
RESULT_TEXT_ADDR = 0xA004
RESULT_TEXT_LIMIT = 4096
RESULT_RUNNING = 0x80
DEFAULT_MAX_INSTRUCTIONS = 8_000_000
DEFAULT_BATCH_INSTRUCTIONS = 50_000
DEFAULT_SETTLE_INSTRUCTIONS = 100_000


@dataclass(frozen=True)
class ApuRomResult:
    name: str
    path: Path
    passed: bool
    failed: bool
    timed_out: bool
    status_code: int
    signature_present: bool
    instructions: int
    cycles: int
    output: str

    @property
    def outcome(self) -> str:
        if self.passed:
            return "pass"
        if self.failed:
            return "fail"
        if self.timed_out:
            return "timeout"
        return "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "outcome": self.outcome,
            "passed": self.passed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "status_code": self.status_code,
            "signature_present": self.signature_present,
            "instructions": self.instructions,
            "cycles": self.cycles,
            "output": self.output,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Blargg dmg_sound ROMs as an APU regression lane."
    )
    parser.add_argument(
        "--rom-root",
        type=Path,
        default=ROOT / "roms" / "dmg_sound",
        help="Directory containing dmg_sound.gb and rom_singles/*.gb.",
    )
    parser.add_argument(
        "--max-instructions",
        type=int,
        default=DEFAULT_MAX_INSTRUCTIONS,
        help="Maximum CPU instructions per ROM before treating it as timed out.",
    )
    parser.add_argument(
        "--batch-instructions",
        type=int,
        default=DEFAULT_BATCH_INSTRUCTIONS,
        help="Instruction batch size between memory-output polls.",
    )
    parser.add_argument(
        "--settle-instructions",
        type=int,
        default=DEFAULT_SETTLE_INSTRUCTIONS,
        help="Extra instructions to run after terminal text appears.",
    )
    parser.add_argument(
        "--expected-pass-only",
        action="store_true",
        help="Run only the currently passing strict subset.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat every selected ROM as expected to pass, including any future unlisted ROMs.",
    )
    parser.add_argument(
        "--allow-xpass",
        action="store_true",
        help="Do not fail if a future known-failure ROM unexpectedly passes.",
    )
    parser.add_argument("--json-output", type=Path, help="Write result details to JSON.")
    parser.add_argument("--print-json", action="store_true", help="Print result JSON.")
    return parser.parse_args()


def read_blargg_memory_output(emulator: Emulator) -> tuple[int, bool, str]:
    bus = emulator.bus
    status_code = bus.read8(RESULT_STATUS_ADDR)
    signature = tuple(bus.read8(RESULT_SIGNATURE_ADDR + offset) for offset in range(3))
    chars: list[str] = []
    for offset in range(RESULT_TEXT_LIMIT):
        value = bus.read8(RESULT_TEXT_ADDR + offset)
        if value == 0:
            break
        if value in (0x09, 0x0A, 0x0D) or 0x20 <= value <= 0x7E:
            chars.append(chr(value))
        else:
            chars.append(".")
    return status_code, signature == DMG_SOUND_SIGNATURE, "".join(chars)


def classify_blargg_output(
    *,
    status_code: int,
    signature_present: bool,
    output: str,
    timed_out: bool,
) -> tuple[bool, bool]:
    passed = "Passed" in output and "Failed" not in output
    failed = "Failed" in output
    if signature_present and status_code not in (0x00, RESULT_RUNNING):
        failed = True
    if timed_out:
        passed = False
    return passed, failed


def has_terminal_output(status_code: int, signature_present: bool, output: str) -> bool:
    if "Passed" in output or "Failed" in output:
        return True
    return signature_present and status_code not in (0x00, RESULT_RUNNING)


def run_blargg_apu_rom(
    path: Path,
    *,
    max_instructions: int,
    batch_instructions: int,
    settle_instructions: int,
) -> ApuRomResult:
    emulator = Emulator.from_rom_file(path, serial_sink=lambda _: None)
    status_code = 0
    signature_present = False
    output = ""

    while emulator.cpu.instructions < max_instructions:
        remaining = max_instructions - emulator.cpu.instructions
        emulator.cpu.run(max_instructions=min(batch_instructions, remaining))
        status_code, signature_present, output = read_blargg_memory_output(emulator)
        if has_terminal_output(status_code, signature_present, output):
            if settle_instructions > 0:
                emulator.cpu.run(max_instructions=settle_instructions)
                status_code, signature_present, output = read_blargg_memory_output(emulator)
            break

    timed_out = not has_terminal_output(status_code, signature_present, output)
    passed, failed = classify_blargg_output(
        status_code=status_code,
        signature_present=signature_present,
        output=output,
        timed_out=timed_out,
    )
    return ApuRomResult(
        name=path.name,
        path=path,
        passed=passed,
        failed=failed,
        timed_out=timed_out,
        status_code=status_code,
        signature_present=signature_present,
        instructions=emulator.cpu.instructions,
        cycles=emulator.cpu.cycles,
        output=output,
    )


def selected_roms(rom_root: Path, *, expected_pass_only: bool) -> list[Path]:
    singles = rom_root / "rom_singles"
    names = EXPECTED_PASS if expected_pass_only else BLARGG_DMG_SOUND_TESTS
    return [singles / name for name in BLARGG_DMG_SOUND_TESTS if name in names]


def evaluate_results(
    results: list[ApuRomResult],
    *,
    strict: bool,
    allow_xpass: bool,
) -> list[str]:
    failures: list[str] = []
    for result in results:
        expected_to_pass = result.name in EXPECTED_PASS or strict
        known_failure = result.name in KNOWN_FAILURE_REASONS
        if expected_to_pass and not result.passed:
            failures.append(f"{result.name}: expected PASS, got {result.outcome}")
        elif known_failure and result.passed and not allow_xpass:
            failures.append(f"{result.name}: XPASS, update EXPECTED_PASS/known-failure list")
        elif not expected_to_pass and not known_failure and not result.passed:
            failures.append(f"{result.name}: unexpected {result.outcome}")
    return failures


def status_label(result: ApuRomResult, *, strict: bool) -> str:
    if result.name in EXPECTED_PASS or strict:
        return "PASS" if result.passed else "FAIL"
    if result.name in KNOWN_FAILURE_REASONS:
        return "XPASS" if result.passed else "XFAIL"
    return "PASS" if result.passed else "FAIL"


def print_results(results: list[ApuRomResult], *, strict: bool) -> None:
    for result in results:
        label = status_label(result, strict=strict)
        preview = result.output.replace("\n", "\\n")
        if len(preview) > 220:
            preview = preview[:217] + "..."
        reason = ""
        if label == "XFAIL":
            reason = f" reason={KNOWN_FAILURE_REASONS[result.name]}"
        print(
            f"{label} {result.name} outcome={result.outcome} "
            f"status=0x{result.status_code:02X} "
            f"instr={result.instructions} cycles={result.cycles} "
            f"signature={int(result.signature_present)} output={preview!r}{reason}"
        )


def results_to_json(results: list[ApuRomResult], failures: list[str]) -> dict[str, Any]:
    return {
        "suite": "Blargg dmg_sound",
        "expected_pass": sorted(EXPECTED_PASS),
        "known_failures": KNOWN_FAILURE_REASONS,
        "results": [result.to_json() for result in results],
        "failures": failures,
    }


def main() -> int:
    args = parse_args()
    roms = selected_roms(args.rom_root, expected_pass_only=args.expected_pass_only)
    missing = [path for path in roms if not path.exists()]
    if missing:
        print("Missing Blargg dmg_sound ROMs:")
        for path in missing:
            print(f"  {path}")
        return 1

    results = [
        run_blargg_apu_rom(
            rom,
            max_instructions=args.max_instructions,
            batch_instructions=args.batch_instructions,
            settle_instructions=args.settle_instructions,
        )
        for rom in roms
    ]
    failures = evaluate_results(
        results,
        strict=args.strict,
        allow_xpass=args.allow_xpass,
    )
    print_results(results, strict=args.strict)

    payload = results_to_json(results, failures)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.print_json:
        print(json.dumps(payload, indent=2))

    if failures:
        print("Failures:")
        for failure in failures:
            print(f"  {failure}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
