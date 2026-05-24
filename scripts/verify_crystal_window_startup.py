from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartridge import Cartridge  # noqa: E402
from emulator import Emulator  # noqa: E402


def parse_key_values(line: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in line.split()[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key] = value
    return values


def run_headless_smoke(rom: Path, instruction_limit: int) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    cartridge = Cartridge.from_file(rom)
    emulator = Emulator(cartridge, serial_sink=lambda _: None)

    if not cartridge.header.cgb_only:
        failures.append(f"headless: expected CGB-only header, got {cartridge.header.cgb_status}")
    if not emulator.bus.cgb_mode:
        failures.append("headless: emulator did not enter CGB mode")

    frame_before = emulator.bus.ppu.frame_count
    instr_before = emulator.cpu.instructions
    cycles_before = emulator.cpu.cycles
    try:
        emulator.run(max_instructions=instruction_limit, max_frames=1)
    except Exception as exc:  # noqa: BLE001 - report startup blockers clearly.
        return (
            {
                "status": "exception",
                "exception": type(exc).__name__,
                "message": str(exc),
                "header_status": cartridge.header.cgb_status,
                "mode": emulator.mode.value,
            },
            failures + [f"headless: {type(exc).__name__}: {exc}"],
        )

    frame_after = emulator.bus.ppu.frame_count
    instr_delta = emulator.cpu.instructions - instr_before
    cycle_delta = emulator.cpu.cycles - cycles_before
    frame_advanced = frame_after > frame_before
    if not frame_advanced:
        if instr_delta >= instruction_limit:
            reason = "instruction-limit-no-frame"
        elif instr_delta == 0 and cycle_delta == 0:
            reason = "cpu-no-progress"
        else:
            reason = "emulator-returned-no-frame"
        failures.append(f"headless: first frame did not advance; reason={reason}")
    else:
        reason = "frame-reached"

    return (
        {
            "status": "pass" if frame_advanced and not failures else "fail",
            "title": cartridge.header.title,
            "header_status": cartridge.header.cgb_status,
            "cgb_flag": cartridge.header.cgb_flag,
            "mode": emulator.mode.value,
            "frame_before": frame_before,
            "frame_after": frame_after,
            "frame_advanced": frame_advanced,
            "reason": reason,
            "cpu_instr": instr_delta,
            "cpu_cycles": cycle_delta,
            "pc": emulator.cpu.pc,
            "ly": emulator.bus.ppu._scanline,
            "ppu_mode": emulator.bus.ppu.mode,
            "lcdc": emulator.bus.read8(0xFF40),
            "key1": emulator.bus.read8(0xFF4D),
            "double_speed": emulator.bus.double_speed,
            "speed_switch_armed": emulator.bus.speed_switch_armed,
            "speed_switch_arm_writes": emulator.bus.speed_switch_arm_writes,
            "speed_switches": emulator.bus.speed_switches,
        },
        failures,
    )


def run_window_smoke(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    command = [
        args.window_python,
        "-B",
        str(ROOT / "main.py"),
        str(args.rom),
        "--window",
        "--max-instructions",
        "0",
        "--frames",
        "1",
        "--profile-startup",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=args.window_timeout,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    startup_lines = [
        line for line in output.splitlines() if line.startswith("window-startup ")
    ]
    events = [parse_key_values(line) for line in startup_lines]
    event_names = [event.get("event", "") for event in events]
    failures: list[str] = []

    if result.returncode:
        failures.append(f"window: command failed with return code {result.returncode}")
    if "Mode: CGB" not in output:
        failures.append("window: CLI output did not show Mode: CGB")
    for required in ("tk-created", "tk-presented", "first-frame"):
        if required not in event_names:
            failures.append(f"window: missing startup event {required}")
    if all(name in event_names for name in ("tk-presented", "first-frame")):
        if event_names.index("tk-presented") > event_names.index("first-frame"):
            failures.append("window: first frame started before Tk was presented")

    first_frame = next((event for event in events if event.get("event") == "first-frame"), {})
    if first_frame:
        if first_frame.get("frame_advanced") != "1":
            failures.append(
                f"window: first frame did not advance; reason={first_frame.get('reason', 'unknown')}"
            )
        if first_frame.get("reason") != "frame-reached":
            failures.append(f"window: first-frame reason={first_frame.get('reason', 'unknown')}")

    return (
        {
            "status": "pass" if not failures else "fail",
            "command": command,
            "returncode": result.returncode,
            "events": events,
            "output": output,
        },
        failures,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Pokemon Crystal reaches a first CGB frame and Tk presents before stepping."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "crystal.gbc")
    parser.add_argument("--headless-instruction-limit", type=int, default=250_000)
    parser.add_argument("--run-window", action="store_true", help="Run the Tk window startup smoke.")
    parser.add_argument("--window-python", default="python")
    parser.add_argument("--window-timeout", type=float, default=20.0)
    parser.add_argument("--json-output", type=Path, help="Write smoke results to a JSON file.")
    parser.add_argument("--print-json", action="store_true", help="Print smoke results as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures: list[str] = []
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    if args.headless_instruction_limit < 1:
        raise SystemExit("--headless-instruction-limit must be positive")
    if args.window_timeout <= 0:
        raise SystemExit("--window-timeout must be positive")

    headless, headless_failures = run_headless_smoke(args.rom, args.headless_instruction_limit)
    failures.extend(headless_failures)
    window = None
    if args.run_window:
        try:
            window, window_failures = run_window_smoke(args)
            failures.extend(window_failures)
        except subprocess.TimeoutExpired as exc:
            window = {
                "status": "timeout",
                "command": exc.cmd,
                "timeout": args.window_timeout,
                "output": "\n".join(
                    part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
                    for part in (exc.stdout, exc.stderr)
                    if part
                ),
            }
            failures.append(f"window: timed out after {args.window_timeout:.1f}s")

    result = {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "headless": headless,
        "window": window,
    }
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    if failures:
        print("Crystal CGB window startup smoke: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Crystal CGB window startup smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
