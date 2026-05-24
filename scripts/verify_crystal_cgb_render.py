from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bus import Bus, EmulationMode  # noqa: E402
from cartridge import Cartridge, compute_header_checksum  # noqa: E402
from emulator import Emulator  # noqa: E402
from ppu import (  # noqa: E402
    RGB_PIXEL_FLAG,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    framebuffer_pixel_to_rgb,
    rgb_to_framebuffer_pixel,
)


def make_cgb_rom() -> bytes:
    rom = bytearray([0x00] * 0x8000)
    rom[0x0134 : 0x0134 + len(b"CGBRENDER")] = b"CGBRENDER"
    rom[0x0143] = 0x80
    rom[0x0147] = 0x00
    rom[0x0148] = 0x00
    rom[0x0149] = 0x00
    rom[0x014D] = compute_header_checksum(rom)
    return bytes(rom)


def set_cgb_solid_tile(bus: Bus, bank: int, tile_id: int, color_id: int) -> None:
    lo = 0xFF if color_id & 0x01 else 0x00
    hi = 0xFF if color_id & 0x02 else 0x00
    base = bank * 0x2000
    for row in range(8):
        address = base + tile_id * 16 + row * 2
        bus.vram[address] = lo
        bus.vram[address + 1] = hi


def set_cgb_bg_color(bus: Bus, palette: int, color_id: int, rgb555: int) -> None:
    offset = palette * 8 + color_id * 2
    bus.bg_palette_ram[offset] = rgb555 & 0xFF
    bus.bg_palette_ram[offset + 1] = (rgb555 >> 8) & 0x7F


def set_cgb_obj_color(bus: Bus, palette: int, color_id: int, rgb555: int) -> None:
    offset = palette * 8 + color_id * 2
    bus.obj_palette_ram[offset] = rgb555 & 0xFF
    bus.obj_palette_ram[offset + 1] = (rgb555 >> 8) & 0x7F


def set_sprite(
    bus: Bus,
    index: int,
    *,
    y: int = 16,
    x: int = 8,
    tile: int = 2,
    attrs: int = 0,
) -> None:
    offset = index * 4
    bus.oam[offset : offset + 4] = bytes([y, x, tile, attrs])


def make_cgb_bus() -> Bus:
    return Bus(Cartridge(make_cgb_rom()), serial_sink=lambda _: None, mode=EmulationMode.CGB)


def run_synthetic_attribute_checks() -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []

    palette_bus = make_cgb_bus()
    palette_bus.write8(0xFF40, 0x91)
    set_cgb_solid_tile(palette_bus, 0, 1, 2)
    set_cgb_bg_color(palette_bus, 3, 2, 0x03E0)
    palette_bus.vram[0x1800] = 1
    palette_bus.vram[0x2000 + 0x1800] = 0x03
    palette_bus.ppu.render_scanline(0)
    palette_pixel = palette_bus.ppu.framebuffer[0][0]
    palette_expected = rgb_to_framebuffer_pixel(0, 255, 0)
    if palette_pixel != palette_expected:
        failures.append("synthetic: BG palette attribute did not select BGP3 color 2")

    bank_bus = make_cgb_bus()
    bank_bus.write8(0xFF40, 0x91)
    set_cgb_solid_tile(bank_bus, 0, 2, 1)
    set_cgb_solid_tile(bank_bus, 1, 2, 3)
    set_cgb_bg_color(bank_bus, 0, 1, 0x001F)
    set_cgb_bg_color(bank_bus, 0, 3, 0x7C00)
    bank_bus.vram[0x1800] = 2
    bank_bus.vram[0x2000 + 0x1800] = 0x08
    bank_bus.ppu.render_scanline(0)
    bank_pixel = bank_bus.ppu.framebuffer[0][0]
    bank_expected = rgb_to_framebuffer_pixel(0, 0, 255)
    if bank_pixel != bank_expected:
        failures.append("synthetic: BG attribute bit 3 did not select tile data from VRAM bank 1")

    flip_bus = make_cgb_bus()
    flip_bus.write8(0xFF40, 0x91)
    tile_base = 4 * 16
    flip_bus.vram[tile_base + 7 * 2] = 0x00
    flip_bus.vram[tile_base + 7 * 2 + 1] = 0x01
    set_cgb_bg_color(flip_bus, 0, 2, 0x03E0)
    flip_bus.vram[0x1800] = 4
    flip_bus.vram[0x2000 + 0x1800] = 0x60
    flip_bus.ppu.render_scanline(0)
    flip_pixel = flip_bus.ppu.framebuffer[0][0]
    flip_expected = rgb_to_framebuffer_pixel(0, 255, 0)
    if flip_pixel != flip_expected:
        failures.append("synthetic: BG attribute X/Y flip bits did not mirror tile pixels")

    obj_palette_bus = make_cgb_bus()
    obj_palette_bus.write8(0xFF40, 0x93)
    set_cgb_solid_tile(obj_palette_bus, 0, 2, 2)
    set_cgb_obj_color(obj_palette_bus, 5, 2, 0x001F)
    set_sprite(obj_palette_bus, 0, attrs=0x05)
    obj_palette_bus.ppu.render_scanline(0)
    obj_palette_pixel = obj_palette_bus.ppu.framebuffer[0][0]
    obj_palette_expected = rgb_to_framebuffer_pixel(255, 0, 0)
    if obj_palette_pixel != obj_palette_expected:
        failures.append("synthetic: OBJ palette attribute did not select OBP5 color 2")

    obj_bank_bus = make_cgb_bus()
    obj_bank_bus.write8(0xFF40, 0x93)
    set_cgb_solid_tile(obj_bank_bus, 0, 2, 1)
    set_cgb_solid_tile(obj_bank_bus, 1, 2, 3)
    set_cgb_obj_color(obj_bank_bus, 0, 1, 0x001F)
    set_cgb_obj_color(obj_bank_bus, 0, 3, 0x7C00)
    set_sprite(obj_bank_bus, 0, attrs=0x08)
    obj_bank_bus.ppu.render_scanline(0)
    obj_bank_pixel = obj_bank_bus.ppu.framebuffer[0][0]
    obj_bank_expected = rgb_to_framebuffer_pixel(0, 0, 255)
    if obj_bank_pixel != obj_bank_expected:
        failures.append("synthetic: OBJ attribute bit 3 did not select tile data from VRAM bank 1")

    obj_order_bus = make_cgb_bus()
    obj_order_bus.write8(0xFF40, 0x93)
    set_cgb_solid_tile(obj_order_bus, 0, 3, 1)
    set_cgb_solid_tile(obj_order_bus, 0, 4, 2)
    set_cgb_obj_color(obj_order_bus, 0, 1, 0x001F)
    set_cgb_obj_color(obj_order_bus, 0, 2, 0x03E0)
    set_sprite(obj_order_bus, 0, x=16, tile=3)
    set_sprite(obj_order_bus, 1, x=12, tile=4)
    obj_order_bus.ppu.render_scanline(0)
    obj_order_pixel = obj_order_bus.ppu.framebuffer[0][8]
    obj_order_expected = rgb_to_framebuffer_pixel(255, 0, 0)
    if obj_order_pixel != obj_order_expected:
        failures.append("synthetic: CGB OBJ priority did not prefer the earlier OAM entry")

    obj_opri_bus = make_cgb_bus()
    obj_opri_bus.write8(0xFF40, 0x93)
    obj_opri_bus.write8(0xFF6C, 0x01)
    set_cgb_solid_tile(obj_opri_bus, 0, 3, 1)
    set_cgb_solid_tile(obj_opri_bus, 0, 4, 2)
    set_cgb_obj_color(obj_opri_bus, 0, 1, 0x001F)
    set_cgb_obj_color(obj_opri_bus, 0, 2, 0x03E0)
    set_sprite(obj_opri_bus, 0, x=16, tile=3)
    set_sprite(obj_opri_bus, 1, x=12, tile=4)
    obj_opri_bus.ppu.render_scanline(0)
    obj_opri_pixel = obj_opri_bus.ppu.framebuffer[0][8]
    obj_opri_expected = rgb_to_framebuffer_pixel(0, 255, 0)
    if obj_opri_pixel != obj_opri_expected:
        failures.append("synthetic: OPRI DMG-style mode did not prefer the lower X OBJ")

    obj_bg_priority_bus = make_cgb_bus()
    obj_bg_priority_bus.write8(0xFF40, 0x93)
    set_cgb_solid_tile(obj_bg_priority_bus, 0, 0, 1)
    set_cgb_solid_tile(obj_bg_priority_bus, 0, 2, 2)
    set_cgb_bg_color(obj_bg_priority_bus, 0, 1, 0x03E0)
    set_cgb_obj_color(obj_bg_priority_bus, 0, 2, 0x001F)
    obj_bg_priority_bus.vram[0x1800] = 0
    obj_bg_priority_bus.vram[0x2000 + 0x1800] = 0x80
    set_sprite(obj_bg_priority_bus, 0)
    obj_bg_priority_bus.ppu.render_scanline(0)
    obj_bg_priority_pixel = obj_bg_priority_bus.ppu.framebuffer[0][0]
    obj_bg_priority_expected = rgb_to_framebuffer_pixel(0, 255, 0)
    if obj_bg_priority_pixel != obj_bg_priority_expected:
        failures.append("synthetic: CGB BG attribute priority did not hide the OBJ pixel")

    obj_lcdc_priority_bus = make_cgb_bus()
    obj_lcdc_priority_bus.write8(0xFF40, 0x92)
    set_cgb_solid_tile(obj_lcdc_priority_bus, 0, 0, 1)
    set_cgb_solid_tile(obj_lcdc_priority_bus, 0, 2, 2)
    set_cgb_bg_color(obj_lcdc_priority_bus, 0, 1, 0x03E0)
    set_cgb_obj_color(obj_lcdc_priority_bus, 0, 2, 0x001F)
    obj_lcdc_priority_bus.vram[0x1800] = 0
    obj_lcdc_priority_bus.vram[0x2000 + 0x1800] = 0x80
    set_sprite(obj_lcdc_priority_bus, 0, attrs=0x80)
    obj_lcdc_priority_bus.ppu.render_scanline(0)
    obj_lcdc_priority_pixel = obj_lcdc_priority_bus.ppu.framebuffer[0][0]
    obj_lcdc_priority_expected = rgb_to_framebuffer_pixel(255, 0, 0)
    if obj_lcdc_priority_pixel != obj_lcdc_priority_expected:
        failures.append("synthetic: CGB LCDC bit 0 clear did not force OBJ over BG priority")

    return (
        {
            "status": "pass" if not failures else "fail",
            "bg_palette_pixel": framebuffer_pixel_to_rgb(palette_pixel),
            "bg_bank_pixel": framebuffer_pixel_to_rgb(bank_pixel),
            "bg_flip_pixel": framebuffer_pixel_to_rgb(flip_pixel),
            "obj_palette_pixel": framebuffer_pixel_to_rgb(obj_palette_pixel),
            "obj_bank_pixel": framebuffer_pixel_to_rgb(obj_bank_pixel),
            "obj_oam_priority_pixel": framebuffer_pixel_to_rgb(obj_order_pixel),
            "obj_opri_pixel": framebuffer_pixel_to_rgb(obj_opri_pixel),
            "obj_bg_priority_pixel": framebuffer_pixel_to_rgb(obj_bg_priority_pixel),
            "obj_lcdc_priority_pixel": framebuffer_pixel_to_rgb(obj_lcdc_priority_pixel),
        },
        failures,
    )


def run_crystal_render_smoke(
    rom: Path,
    frames: int,
    min_unique_rgb_colors: int,
    require_crystal_attributes: bool,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    cartridge = Cartridge.from_file(rom)
    emulator = Emulator(cartridge, serial_sink=lambda _: None)

    if not cartridge.header.cgb_only:
        failures.append(f"crystal: expected CGB-only header, got {cartridge.header.cgb_status}")
    if not emulator.bus.cgb_mode:
        failures.append("crystal: emulator did not enter CGB mode")

    initial_key1 = emulator.bus.read8(0xFF4D)
    emulator.run(max_instructions=None, max_frames=frames)
    final_key1 = emulator.bus.read8(0xFF4D)
    framebuffer = emulator.bus.ppu.framebuffer
    pixels = [pixel for row in framebuffer for pixel in row]
    rgb_pixels = sum(1 for pixel in pixels if pixel & RGB_PIXEL_FLAG)
    unique_rgb = {framebuffer_pixel_to_rgb(pixel) for pixel in pixels}
    attrs = (
        emulator.bus.vram[0x2000 + 0x1800 : 0x2000 + 0x1C00]
        + emulator.bus.vram[0x2000 + 0x1C00 : 0x2000 + 0x2000]
    )

    if emulator.bus.ppu.frame_count < frames:
        failures.append(f"crystal: reached {emulator.bus.ppu.frame_count} frames, expected {frames}")
    if rgb_pixels != SCREEN_WIDTH * SCREEN_HEIGHT:
        failures.append(
            f"crystal: framebuffer has {rgb_pixels} RGB pixels, expected {SCREEN_WIDTH * SCREEN_HEIGHT}"
        )
    if len(unique_rgb) < min_unique_rgb_colors:
        failures.append(
            f"crystal: visible frame has {len(unique_rgb)} unique RGB colors, expected at least {min_unique_rgb_colors}"
        )
    bg_palette_nonzero = sum(1 for value in emulator.bus.bg_palette_ram if value)
    if bg_palette_nonzero == 0:
        failures.append("crystal: BG palette RAM remained blank")
    vram_dma_blocks = emulator.bus.vram_dma_gdma_blocks + emulator.bus.vram_dma_hdma_blocks
    if vram_dma_blocks == 0:
        failures.append("crystal: CGB VRAM DMA path was not exercised")

    attrs_nonzero = sum(1 for value in attrs if value)
    attrs_palette = sum(1 for value in attrs if value & 0x07)
    attrs_bank = sum(1 for value in attrs if value & 0x08)
    attrs_xflip = sum(1 for value in attrs if value & 0x20)
    attrs_yflip = sum(1 for value in attrs if value & 0x40)
    if require_crystal_attributes:
        if attrs_nonzero == 0:
            failures.append("crystal: tile attribute maps remained blank")
        if attrs_palette == 0:
            failures.append("crystal: no BG palette attributes were present")
        if attrs_bank == 0:
            failures.append("crystal: no tile VRAM bank attributes were present")
        if attrs_xflip == 0 or attrs_yflip == 0:
            failures.append("crystal: no tile flip attributes were present")

    return (
        {
            "status": "pass" if not failures else "fail",
            "title": cartridge.header.title,
            "header_status": cartridge.header.cgb_status,
            "mode": emulator.mode.value,
            "frames": emulator.bus.ppu.frame_count,
            "cpu_instr": emulator.cpu.instructions,
            "cpu_cycles": emulator.cpu.cycles,
            "rgb_pixels": rgb_pixels,
            "unique_rgb_colors": len(unique_rgb),
            "bg_palette_nonzero": bg_palette_nonzero,
            "vram_dma_blocks": vram_dma_blocks,
            "vram_dma_gdma_blocks": emulator.bus.vram_dma_gdma_blocks,
            "vram_dma_hdma_blocks": emulator.bus.vram_dma_hdma_blocks,
            "vram_dma_bytes": emulator.bus.vram_dma_bytes,
            "key1_initial": initial_key1,
            "key1_final": final_key1,
            "speed_switch_armed": emulator.bus.speed_switch_armed,
            "double_speed": emulator.bus.double_speed,
            "speed_switch_arm_writes": emulator.bus.speed_switch_arm_writes,
            "speed_switches": emulator.bus.speed_switches,
            "attrs_nonzero": attrs_nonzero,
            "attrs_palette": attrs_palette,
            "attrs_bank": attrs_bank,
            "attrs_xflip": attrs_xflip,
            "attrs_yflip": attrs_yflip,
        },
        failures,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify first-pass CGB BG/window attributes, OBJ palette/priority, "
            "VRAM DMA activity, and KEY1 double-speed startup activity."
        )
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "crystal.gbc")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--min-unique-rgb-colors", type=int, default=2)
    parser.add_argument(
        "--require-crystal-attributes",
        action="store_true",
        help="Require the selected Crystal frame to contain palette, bank, and flip attributes.",
    )
    parser.add_argument("--json-output", type=Path, help="Write smoke results to a JSON file.")
    parser.add_argument("--print-json", action="store_true", help="Print smoke results as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    if args.frames < 1:
        raise SystemExit("--frames must be positive")
    if args.min_unique_rgb_colors < 1:
        raise SystemExit("--min-unique-rgb-colors must be positive")

    failures: list[str] = []
    synthetic, synthetic_failures = run_synthetic_attribute_checks()
    failures.extend(synthetic_failures)
    crystal, crystal_failures = run_crystal_render_smoke(
        args.rom,
        args.frames,
        args.min_unique_rgb_colors,
        args.require_crystal_attributes,
    )
    failures.extend(crystal_failures)

    result = {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "synthetic": synthetic,
        "crystal": crystal,
    }
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    if failures:
        print("Crystal CGB render smoke: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Crystal CGB render smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
