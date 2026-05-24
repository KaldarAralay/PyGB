from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from button_script import ButtonScript, load_button_script, parse_button_script  # noqa: E402
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


DEFAULT_CHECKPOINT_FRAMES = (60, 600, 2400, 3600)
DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME = 2400
DEFAULT_FRAME_OUTPUT_DIR = ROOT / "qa-output" / "crystal-cgb-stages"
PPU_MODE_NAMES = {
    0: "hblank",
    1: "vblank",
    2: "oam",
    3: "drawing",
}


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


def parse_checkpoint_frames(raw_value: str | None, fallback_frame: int | None = None) -> list[int]:
    if raw_value is None:
        if fallback_frame is not None:
            return [fallback_frame]
        return list(DEFAULT_CHECKPOINT_FRAMES)

    tokens = [
        token.strip()
        for token in raw_value.replace(",", " ").split()
        if token.strip()
    ]
    if not tokens:
        raise ValueError("--checkpoint-frames must contain at least one frame")

    frames: list[int] = []
    for token in tokens:
        try:
            frame = int(token, 0)
        except ValueError as exc:
            raise ValueError(f"checkpoint frame {token!r} must be an integer") from exc
        if frame < 1:
            raise ValueError("checkpoint frames must be positive")
        frames.append(frame)
    return sorted(set(frames))


def stage_label(checkpoint: int) -> str:
    if checkpoint <= 60:
        return "first-visible"
    if checkpoint < DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME:
        return "startup"
    if checkpoint == DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME:
        return "attribute-sample"
    return "late-title-menu-sample"


def load_optional_button_script(
    *,
    button_script: str | None,
    button_script_path: Path | None,
) -> ButtonScript | None:
    if button_script and button_script_path is not None:
        raise ValueError("use either --button-script or --button-script-path, not both")
    if button_script_path is not None:
        return load_button_script(button_script_path)
    if button_script:
        return parse_button_script(button_script)
    return None


def collect_crystal_stage_metrics(
    emulator: Emulator,
    cartridge: Cartridge,
    *,
    checkpoint: int,
    initial_key1: int,
    frame_output_dir: Path | None,
) -> dict[str, Any]:
    bus = emulator.bus
    framebuffer = bus.ppu.framebuffer
    pixels = [pixel for row in framebuffer for pixel in row]
    rgb_pixels = sum(1 for pixel in pixels if pixel & RGB_PIXEL_FLAG)
    unique_rgb = {framebuffer_pixel_to_rgb(pixel) for pixel in pixels}
    unique_rgb_values = sorted(unique_rgb)
    attrs = (
        bus.vram[0x2000 + 0x1800 : 0x2000 + 0x1C00]
        + bus.vram[0x2000 + 0x1C00 : 0x2000 + 0x2000]
    )

    bmp_path: Path | None = None
    if frame_output_dir is not None:
        bmp_path = frame_output_dir / f"crystal-cgb-frame-{checkpoint:05d}.bmp"
        bus.ppu.write_frame_bmp(bmp_path)

    attrs_nonzero = sum(1 for value in attrs if value)
    attrs_palette = sum(1 for value in attrs if value & 0x07)
    attrs_bank = sum(1 for value in attrs if value & 0x08)
    attrs_xflip = sum(1 for value in attrs if value & 0x20)
    attrs_yflip = sum(1 for value in attrs if value & 0x40)
    vram_dma_blocks = bus.vram_dma_gdma_blocks + bus.vram_dma_hdma_blocks
    ppu_mode = bus.ppu.mode

    return {
        "checkpoint": checkpoint,
        "stage": stage_label(checkpoint),
        "title": cartridge.header.title,
        "header_status": cartridge.header.cgb_status,
        "header_cgb_only": cartridge.header.cgb_only,
        "mode": emulator.mode.value,
        "bus_cgb_mode": bus.cgb_mode,
        "frames": bus.ppu.frame_count,
        "cpu_instr": emulator.cpu.instructions,
        "cpu_cycles": emulator.cpu.cycles,
        "rgb_pixels": rgb_pixels,
        "unique_rgb_colors": len(unique_rgb),
        "unique_rgb_values": unique_rgb_values,
        "bg_palette_nonzero": sum(1 for value in bus.bg_palette_ram if value),
        "bg_palette_ram": list(bus.bg_palette_ram),
        "obj_palette_nonzero": sum(1 for value in bus.obj_palette_ram if value),
        "obj_palette_ram": list(bus.obj_palette_ram),
        "vram_dma_blocks": vram_dma_blocks,
        "vram_dma_gdma_blocks": bus.vram_dma_gdma_blocks,
        "vram_dma_hdma_blocks": bus.vram_dma_hdma_blocks,
        "vram_dma_bytes": bus.vram_dma_bytes,
        "vram_dma_active": bus.vram_dma_active,
        "vram_dma_blocks_remaining": bus.vram_dma_blocks_remaining,
        "vram_dma_source": bus.vram_dma_source,
        "vram_dma_destination": bus.vram_dma_destination,
        "key1_initial": initial_key1,
        "key1": bus.read8(0xFF4D),
        "speed_switch_armed": bus.speed_switch_armed,
        "double_speed": bus.double_speed,
        "speed_switch_arm_writes": bus.speed_switch_arm_writes,
        "speed_switches": bus.speed_switches,
        "attrs_nonzero": attrs_nonzero,
        "attrs_palette": attrs_palette,
        "attrs_bank": attrs_bank,
        "attrs_xflip": attrs_xflip,
        "attrs_yflip": attrs_yflip,
        "lcdc": bus.read8(0xFF40),
        "stat": bus.read8(0xFF41),
        "ly": bus.read8(0xFF44),
        "ppu_mode": ppu_mode,
        "ppu_mode_name": PPU_MODE_NAMES.get(ppu_mode, "unknown"),
        "vram_bank": bus.vram_bank,
        "wram_bank": bus.wram_bank,
        "wram_bank_register": bus.wram_bank_register,
        "bmp": str(bmp_path) if bmp_path is not None else None,
    }


def evaluate_crystal_stage_metrics(
    stage: dict[str, Any],
    *,
    min_unique_rgb_colors: int,
    require_color_variety: bool = True,
    require_palettes: bool = True,
    require_dma: bool = True,
    require_attributes: bool = False,
) -> list[str]:
    failures: list[str] = []
    label = f"crystal frame {stage.get('checkpoint', '?')}"

    if not stage.get("header_cgb_only", False):
        failures.append(
            f"{label}: expected CGB-only header, got {stage.get('header_status')}"
        )
    if stage.get("mode") != EmulationMode.CGB.value or not stage.get("bus_cgb_mode", False):
        failures.append(f"{label}: emulator did not stay in CGB mode")

    frames = int(stage.get("frames", -1))
    checkpoint = int(stage.get("checkpoint", frames))
    if frames < checkpoint:
        failures.append(f"{label}: reached {frames} frames, expected {checkpoint}")

    expected_pixels = SCREEN_WIDTH * SCREEN_HEIGHT
    rgb_pixels = int(stage.get("rgb_pixels", -1))
    if rgb_pixels != expected_pixels:
        failures.append(
            f"{label}: framebuffer has {rgb_pixels} RGB pixels, expected {expected_pixels}"
        )

    unique_rgb_colors = int(stage.get("unique_rgb_colors", 0))
    if require_color_variety and unique_rgb_colors < min_unique_rgb_colors:
        failures.append(
            f"{label}: visible frame has {unique_rgb_colors} unique RGB colors, "
            f"expected at least {min_unique_rgb_colors}"
        )

    if require_palettes and int(stage.get("bg_palette_nonzero", 0)) == 0:
        failures.append(f"{label}: BG palette RAM remained blank")
    if require_dma and int(stage.get("vram_dma_blocks", 0)) == 0:
        failures.append(f"{label}: CGB VRAM DMA path was not exercised")

    if require_attributes:
        if int(stage.get("attrs_nonzero", 0)) == 0:
            failures.append(f"{label}: tile attribute maps remained blank")
        if int(stage.get("attrs_palette", 0)) == 0:
            failures.append(f"{label}: no BG palette attributes were present")
        if int(stage.get("attrs_bank", 0)) == 0:
            failures.append(f"{label}: no tile VRAM bank attributes were present")
        if int(stage.get("attrs_xflip", 0)) == 0 or int(stage.get("attrs_yflip", 0)) == 0:
            failures.append(f"{label}: no tile flip attributes were present")

    return failures


def advance_to_checkpoint(
    emulator: Emulator,
    checkpoint: int,
    button_script: ButtonScript | None,
) -> None:
    if button_script is None:
        remaining = checkpoint - emulator.bus.ppu.frame_count
        if remaining > 0:
            emulator.run(max_instructions=None, max_frames=remaining)
        return

    while emulator.bus.ppu.frame_count < checkpoint:
        frame = emulator.bus.ppu.frame_count
        emulator.set_buttons(button_script.buttons_for_frame(frame, set()))
        emulator.run(max_instructions=None, max_frames=1)


def run_crystal_render_smoke(
    rom: Path,
    checkpoint_frames: list[int],
    min_unique_rgb_colors: int,
    require_crystal_attributes: bool,
    *,
    frame_output_dir: Path | None,
    button_script: ButtonScript | None = None,
    attribute_checkpoint_frame: int = DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    cartridge = Cartridge.from_file(rom)
    emulator = Emulator(cartridge, serial_sink=lambda _: None)

    if not cartridge.header.cgb_only:
        failures.append(f"crystal: expected CGB-only header, got {cartridge.header.cgb_status}")
    if not emulator.bus.cgb_mode:
        failures.append("crystal: emulator did not enter CGB mode")

    initial_key1 = emulator.bus.read8(0xFF4D)
    stages: list[dict[str, Any]] = []
    has_attribute_checkpoint = any(
        checkpoint >= attribute_checkpoint_frame for checkpoint in checkpoint_frames
    )
    for checkpoint in checkpoint_frames:
        advance_to_checkpoint(emulator, checkpoint, button_script)
        stage = collect_crystal_stage_metrics(
            emulator,
            cartridge,
            checkpoint=checkpoint,
            initial_key1=initial_key1,
            frame_output_dir=frame_output_dir,
        )
        require_stage_attributes = require_crystal_attributes and (
            checkpoint >= attribute_checkpoint_frame
            or (not has_attribute_checkpoint and checkpoint == checkpoint_frames[-1])
        )
        require_color_variety = (
            len(checkpoint_frames) == 1
            or checkpoint == checkpoint_frames[0]
            or checkpoint >= attribute_checkpoint_frame
        )
        stage_failures = evaluate_crystal_stage_metrics(
            stage,
            min_unique_rgb_colors=min_unique_rgb_colors,
            require_color_variety=require_color_variety,
            require_attributes=require_stage_attributes,
        )
        stage["status"] = "pass" if not stage_failures else "fail"
        stages.append(stage)
        failures.extend(stage_failures)

    final_stage = stages[-1] if stages else {}
    crystal = {
        **final_stage,
        "status": "pass" if not failures else "fail",
        "title": cartridge.header.title,
        "header_status": cartridge.header.cgb_status,
        "mode": emulator.mode.value,
        "checkpoint_frames": checkpoint_frames,
        "attribute_checkpoint_frame": attribute_checkpoint_frame,
        "button_script_final_frame": button_script.final_frame if button_script else None,
        "key1_final": emulator.bus.read8(0xFF4D),
        "stages": stages,
    }
    return crystal, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify staged Pokemon Crystal CGB rendering, synthetic CGB attributes, "
            "VRAM DMA activity, and KEY1 double-speed startup activity."
        )
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "crystal.gbc")
    parser.add_argument(
        "--frames",
        type=int,
        help=(
            "Legacy single-checkpoint mode. If omitted, the staged gate uses "
            f"{','.join(str(frame) for frame in DEFAULT_CHECKPOINT_FRAMES)}."
        ),
    )
    parser.add_argument(
        "--checkpoint-frames",
        help=(
            "Comma or space separated staged checkpoints. Defaults to "
            f"{','.join(str(frame) for frame in DEFAULT_CHECKPOINT_FRAMES)}."
        ),
    )
    parser.add_argument(
        "--attribute-checkpoint-frame",
        type=int,
        default=DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME,
        help="First checkpoint where --require-crystal-attributes is enforced.",
    )
    parser.add_argument("--min-unique-rgb-colors", type=int, default=2)
    parser.add_argument(
        "--require-crystal-attributes",
        action="store_true",
        help=(
            "Require qualifying Crystal checkpoints to contain palette, bank, "
            "and flip attributes."
        ),
    )
    parser.add_argument(
        "--frame-output-dir",
        type=Path,
        default=DEFAULT_FRAME_OUTPUT_DIR,
        help="Directory for staged BMP frame dumps.",
    )
    parser.add_argument(
        "--no-dump-frames",
        action="store_true",
        help="Skip BMP frame dumps.",
    )
    parser.add_argument(
        "--button-script",
        help="Optional inline frame:buttons[:duration] script for title/menu progression.",
    )
    parser.add_argument(
        "--button-script-path",
        type=Path,
        help="Optional file containing frame:buttons[:duration] input script entries.",
    )
    parser.add_argument("--json-output", type=Path, help="Write smoke results to a JSON file.")
    parser.add_argument("--print-json", action="store_true", help="Print smoke results as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    if args.frames is not None and args.frames < 1:
        raise SystemExit("--frames must be positive")
    if args.frames is not None and args.checkpoint_frames is not None:
        raise SystemExit("use either --frames or --checkpoint-frames, not both")
    if args.attribute_checkpoint_frame < 1:
        raise SystemExit("--attribute-checkpoint-frame must be positive")
    if args.min_unique_rgb_colors < 1:
        raise SystemExit("--min-unique-rgb-colors must be positive")
    try:
        checkpoint_frames = parse_checkpoint_frames(
            args.checkpoint_frames,
            fallback_frame=args.frames,
        )
        button_script = load_optional_button_script(
            button_script=args.button_script,
            button_script_path=args.button_script_path,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    frame_output_dir = None if args.no_dump_frames else args.frame_output_dir

    failures: list[str] = []
    synthetic, synthetic_failures = run_synthetic_attribute_checks()
    failures.extend(synthetic_failures)
    crystal, crystal_failures = run_crystal_render_smoke(
        args.rom,
        checkpoint_frames,
        args.min_unique_rgb_colors,
        args.require_crystal_attributes,
        frame_output_dir=frame_output_dir,
        button_script=button_script,
        attribute_checkpoint_frame=args.attribute_checkpoint_frame,
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
