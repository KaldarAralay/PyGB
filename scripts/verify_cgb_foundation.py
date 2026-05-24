from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bus import EmulationMode  # noqa: E402
from cartridge import Cartridge, compute_header_checksum  # noqa: E402
from emulator import Emulator  # noqa: E402
from ppu import DOTS_PER_LINE, MODE2_DOTS, MODE3_DOTS, MODE_HBLANK  # noqa: E402


def make_rom(*, title: bytes = b"CGBFOUND", cgb_flag: int = 0x80) -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0100] = 0x00
    rom[0x0134 : 0x0134 + len(title)] = title
    rom[0x0143] = cgb_flag
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def check(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def tick_to_next_hblank(bus) -> None:
    if bus.ppu.mode == MODE_HBLANK:
        bus.ppu.tick(DOTS_PER_LINE - bus.ppu.line_dots + MODE2_DOTS + MODE3_DOTS)
    else:
        bus.ppu.tick(MODE2_DOTS + MODE3_DOTS)


def run_smoke() -> dict[str, Any]:
    failures: list[str] = []
    enhanced = Cartridge(make_rom(cgb_flag=0x80))
    cgb_only = Cartridge(make_rom(title=b"CGBONLY", cgb_flag=0xC0))
    dmg = Cartridge(make_rom(title=b"DMG", cgb_flag=0x00))

    check(enhanced.header.cgb_supported, failures, "CGB-enhanced header was not detected")
    check(not enhanced.header.cgb_only, failures, "CGB-enhanced header was treated as CGB-only")
    check(cgb_only.header.cgb_only, failures, "CGB-only header was not detected")
    check(not dmg.header.cgb_supported, failures, "DMG header was treated as CGB-capable")

    default_emulator = Emulator(enhanced, serial_sink=lambda _: None)
    auto_emulator = Emulator(enhanced, serial_sink=lambda _: None, mode="auto")
    cgb_emulator = Emulator(enhanced, serial_sink=lambda _: None, mode=EmulationMode.CGB)

    check(default_emulator.mode == EmulationMode.DMG, failures, "default emulator mode changed from DMG")
    check(auto_emulator.mode == EmulationMode.CGB, failures, "auto mode did not select CGB")
    check(cgb_emulator.bus.cgb_mode, failures, "explicit CGB mode did not create a CGB bus")

    default_cgb_only = Emulator(cgb_only, serial_sink=lambda _: None)
    explicit_dmg_cgb_only = Emulator(cgb_only, serial_sink=lambda _: None, mode="dmg")
    check(
        default_cgb_only.mode == EmulationMode.CGB,
        failures,
        "CGB-only cartridge did not force CGB mode by default",
    )
    check(
        explicit_dmg_cgb_only.mode == EmulationMode.CGB,
        failures,
        "CGB-only cartridge did not force CGB mode from requested DMG mode",
    )
    check(default_cgb_only.cpu.a == 0x11, failures, "CGB post-boot A register was not $11")
    check(default_cgb_only.cpu.f == 0x80, failures, "CGB post-boot F register was not $80")
    check(default_cgb_only.bus.read8(0xFF4D) == 0x7E, failures, "CGB KEY1 initial read was not $7E")

    dmg_bus = default_emulator.bus
    dmg_bus.write8(0xFF4F, 0x01)
    dmg_bus.write8(0xFF70, 0x02)
    check(dmg_bus.read8(0xFF4F) == 0xFF, failures, "DMG mode exposed FF4F")
    check(dmg_bus.read8(0xFF70) == 0xFF, failures, "DMG mode exposed FF70")

    bus = cgb_emulator.bus
    bus.write8(0xFF40, bus.read8(0xFF40) & ~0x80)

    bus.write8(0x8000, 0x12)
    bus.write8(0xFF4F, 0x01)
    bus.write8(0x8000, 0x34)
    check(bus.read8(0x8000) == 0x34, failures, "VRAM bank 1 readback failed")
    bus.write8(0xFF4F, 0x00)
    check(bus.read8(0x8000) == 0x12, failures, "VRAM bank 0 readback failed")

    bus.write8(0xD000, 0x56)
    bus.write8(0xFF70, 0x02)
    bus.write8(0xD000, 0x78)
    check(bus.read8(0xD000) == 0x78, failures, "WRAM bank 2 readback failed")
    bus.write8(0xFF70, 0x00)
    check(bus.wram_bank == 1, failures, "WRAM raw bank 0 did not map to bank 1")
    check(bus.read8(0xD000) == 0x56, failures, "WRAM bank 1 readback failed")

    bus.write8(0xFF68, 0x80)
    bus.write8(0xFF69, 0x9A)
    check(bus.read8(0xFF68) == 0x81, failures, "BG palette auto-increment failed")
    bus.write8(0xFF68, 0x00)
    check(bus.read8(0xFF69) == 0x9A, failures, "BG palette data readback failed")
    bus.write8(0xFF6A, 0x82)
    bus.write8(0xFF6B, 0xBC)
    bus.write8(0xFF6A, 0x02)
    check(bus.read8(0xFF6B) == 0xBC, failures, "OBJ palette data readback failed")

    check(bus.read8(0xFF6C) == 0xFE, failures, "OPRI initial CGB priority mode was not 0")
    bus.write8(0xFF6C, 0xFF)
    check(bus.read8(0xFF6C) == 0xFF, failures, "OPRI DMG priority mode write failed")
    bus.write8(0xFF6C, 0x00)
    check(bus.cgb_object_priority_mode == 0, failures, "OPRI CGB priority mode write failed")

    gdma_source = bytes(range(0x10))
    bus.wram[0x0000:0x0010] = gdma_source
    bus.write8(0xFF51, 0xC0)
    bus.write8(0xFF52, 0x0F)
    bus.write8(0xFF53, 0x91)
    bus.write8(0xFF54, 0x2F)
    bus.write8(0xFF55, 0x00)
    check(
        bytes(bus.vram[0x1120:0x1130]) == gdma_source,
        failures,
        "GDMA did not copy masked WRAM source bytes into VRAM",
    )
    check(bus.read8(0xFF55) == 0xFF, failures, "GDMA completion did not read back $FF")

    hdma_emulator = Emulator(enhanced, serial_sink=lambda _: None, mode=EmulationMode.CGB)
    hdma_bus = hdma_emulator.bus
    hdma_source = bytes(0x40 + index for index in range(0x20))
    hdma_bus.wram[0x0000:0x0020] = hdma_source
    hdma_bus.vram[0x0000:0x0020] = bytes([0x00] * 0x20)
    hdma_bus.write8(0xFF51, 0xC0)
    hdma_bus.write8(0xFF52, 0x00)
    hdma_bus.write8(0xFF53, 0x00)
    hdma_bus.write8(0xFF54, 0x00)
    hdma_bus.write8(0xFF55, 0x81)
    tick_to_next_hblank(hdma_bus)
    check(
        bytes(hdma_bus.vram[0x0000:0x0010]) == hdma_source[:0x10],
        failures,
        "HDMA did not copy the first block during visible HBlank",
    )
    check(hdma_bus.read8(0xFF55) == 0x00, failures, "HDMA active length readback was wrong")
    tick_to_next_hblank(hdma_bus)
    check(bytes(hdma_bus.vram[0x0000:0x0020]) == hdma_source, failures, "HDMA did not finish on the second HBlank")
    check(hdma_bus.read8(0xFF55) == 0xFF, failures, "HDMA completion did not read back $FF")

    speed_bus = Emulator(enhanced, serial_sink=lambda _: None, mode=EmulationMode.CGB).bus
    speed_bus.write8(0xFF4D, 0x01)
    check(speed_bus.speed_switch_armed, failures, "KEY1 prepare bit did not arm speed switch")
    check(speed_bus.read8(0xFF4D) == 0x7F, failures, "KEY1 armed readback was not $7F")
    check(speed_bus.perform_speed_switch(), failures, "KEY1 STOP speed switch did not toggle")
    check(speed_bus.double_speed, failures, "KEY1 double-speed bit did not latch")
    check(speed_bus.read8(0xFF4D) == 0xFE, failures, "KEY1 double-speed readback was not $FE")
    speed_bus.write8(0xFF04, 0x00)
    speed_bus.write8(0xFF05, 0x00)
    speed_bus.write8(0xFF07, 0x05)
    apu_counter = speed_bus.apu._frame_sequence_counter
    speed_bus.tick(16)
    check(speed_bus.read8(0xFF05) == 0x01, failures, "TIMA did not tick on the CGB CPU-speed domain")
    check(speed_bus.ppu.line_dots == 8, failures, "PPU did not run at half CPU cycles in double speed")
    check(
        speed_bus.apu._frame_sequence_counter == apu_counter + 8,
        failures,
        "APU did not run at normal-speed cycles in double speed",
    )

    crystal = run_local_crystal_smoke(failures)

    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "checks": {
            "header_cgb_supported": enhanced.header.cgb_supported,
            "header_cgb_only": cgb_only.header.cgb_only,
            "default_mode": default_emulator.mode.value,
            "cgb_only_default_mode": default_cgb_only.mode.value,
            "auto_mode": auto_emulator.mode.value,
            "cgb_post_boot_a": default_cgb_only.cpu.a,
            "key1_initial": default_cgb_only.bus.read8(0xFF4D),
            "vram_banks": len(bus.vram) // 0x2000,
            "wram_banks": len(bus.wram) // 0x1000,
            "opri_initial": cgb_emulator.bus.read8(0xFF6C),
            "gdma_blocks": bus.vram_dma_gdma_blocks,
            "hdma_blocks": hdma_bus.vram_dma_hdma_blocks,
            "key1_armed_readback": 0x7F,
            "double_speed": speed_bus.double_speed,
            "speed_switch_arm_writes": speed_bus.speed_switch_arm_writes,
            "speed_switches": speed_bus.speed_switches,
        },
        "crystal": crystal,
    }


def cli_mode_output(rom: Path, mode: str | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        "-B",
        str(ROOT / "main.py"),
        str(rom),
        "--max-instructions",
        "0",
        "--frames",
        "0",
    ]
    if mode is not None:
        command.extend(["--mode", mode])
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return {
        "command": command,
        "returncode": result.returncode,
        "output": output,
        "mode_cgb": "Mode: CGB" in output,
        "mode_dmg": "Mode: DMG" in output,
    }


def run_local_crystal_smoke(failures: list[str]) -> dict[str, Any]:
    rom = ROOT / "roms" / "crystal.gbc"
    if not rom.exists():
        return {"status": "skipped", "reason": f"{rom} not found"}

    cartridge = Cartridge.from_file(rom)
    emulator = Emulator(cartridge, serial_sink=lambda _: None)
    auto_emulator = Emulator(cartridge, serial_sink=lambda _: None, mode="auto")
    default_cli = cli_mode_output(rom)
    auto_cli = cli_mode_output(rom, "auto")

    check(cartridge.header.cgb_only, failures, "Pokemon Crystal header was not detected as CGB-only")
    check(emulator.mode == EmulationMode.CGB, failures, "Pokemon Crystal default startup did not enter CGB mode")
    check(auto_emulator.mode == EmulationMode.CGB, failures, "Pokemon Crystal auto startup did not enter CGB mode")
    check(emulator.cpu.a == 0x11, failures, "Pokemon Crystal CGB startup did not expose A=$11")
    check(default_cli["returncode"] == 0, failures, "Pokemon Crystal default CLI startup failed")
    check(default_cli["mode_cgb"], failures, "Pokemon Crystal default CLI output did not show Mode: CGB")
    check(auto_cli["returncode"] == 0, failures, "Pokemon Crystal --mode auto CLI startup failed")
    check(auto_cli["mode_cgb"], failures, "Pokemon Crystal --mode auto CLI output did not show Mode: CGB")

    return {
        "status": "checked",
        "path": str(rom),
        "title": cartridge.header.title,
        "cgb_flag": cartridge.header.cgb_flag,
        "cgb_status": cartridge.header.cgb_status,
        "default_mode": emulator.mode.value,
        "auto_mode": auto_emulator.mode.value,
        "cpu_a": emulator.cpu.a,
        "key1_initial": emulator.bus.read8(0xFF4D),
        "default_cli_mode_cgb": default_cli["mode_cgb"],
        "auto_cli_mode_cgb": auto_cli["mode_cgb"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the synthetic CGB-mode foundation without requiring a CGB ROM."
    )
    parser.add_argument("--json-output", type=Path, help="Write smoke results to a JSON file.")
    parser.add_argument("--print-json", action="store_true", help="Print smoke results as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_smoke()
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    if result["failures"]:
        print("CGB foundation smoke: FAIL")
        for failure in result["failures"]:
            print(f"- {failure}")
        return 1
    print("CGB foundation smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
