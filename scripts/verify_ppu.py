from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emulator import Emulator  # noqa: E402
from ppu import DMG_GRAYSCALE, SCREEN_HEIGHT, SCREEN_WIDTH  # noqa: E402


DMG_ACID2_ROM = ROOT / "roms" / "dmg-acid2.gb"
DMG_ACID2_REFERENCE_RGB_SHA256 = (
    "2ba8286c29ae381838c71a88614302ce05f2b26102d1ed8dc51e25f83fcccc67"
)
MOONEYE_PASS_REGISTERS = (3, 5, 8, 13, 21, 34)
MOONEYE_FAIL_REGISTERS = (0x42,) * 6
DEFAULT_MAX_STEPS = 50_000_000

MOONEYE_PPU_TESTS = (
    "stat_irq_blocking.gb",
    "stat_lyc_onoff.gb",
    "vblank_stat_intr-GS.gb",
    "hblank_ly_scx_timing-GS.gb",
    "intr_1_2_timing-GS.gb",
    "intr_2_0_timing.gb",
    "intr_2_mode0_timing.gb",
    "intr_2_mode0_timing_sprites.gb",
    "intr_2_mode3_timing.gb",
    "intr_2_oam_ok_timing.gb",
    "lcdon_timing-GS.gb",
    "lcdon_write_timing-GS.gb",
)
MOONEYE_EXPECTED_PASS = set(MOONEYE_PPU_TESTS)

MEALYBUG_MODE3_TESTS = (
    "m3_lcdc_tile_sel_change",
    "m3_lcdc_bg_map_change",
    "m3_bgp_change",
    "m3_window_timing",
    "m3_window_timing_wx_0",
    "m3_bgp_change_sprites",
    "m3_scx_high_5_bits",
    "m3_scx_low_3_bits",
    "m3_scy_change",
    "m3_scx_high_5_bits_change2",
    "m3_scy_change2",
    "m3_lcdc_win_map_change",
    "m3_lcdc_win_map_change2",
    "m3_lcdc_tile_sel_win_change",
    "m3_lcdc_obj_en_change",
    "m3_lcdc_obj_en_change_variant",
    "m3_lcdc_obj_size_change",
    "m3_lcdc_obj_size_change_scx",
    "m3_obp0_change",
    "m3_lcdc_bg_en_change",
    "m3_wx_4_change",
    "m3_wx_4_change_sprites",
    "m3_wx_5_change",
    "m3_wx_6_change",
    "m3_lcdc_win_en_change_multiple",
    "m3_lcdc_win_en_change_multiple_wx",
)
MEALYBUG_MODE3_CANDIDATE_TESTS = (
    "m3_lcdc_tile_sel_change2",
    "m3_lcdc_tile_sel_win_change2",
)
MEALYBUG_EXPECTED_PASS = {
    "m3_lcdc_tile_sel_change",
    "m3_lcdc_bg_map_change",
    "m3_bgp_change",
    "m3_window_timing",
    "m3_window_timing_wx_0",
    "m3_bgp_change_sprites",
    "m3_scx_high_5_bits",
    "m3_scx_low_3_bits",
    "m3_scy_change",
    "m3_scx_high_5_bits_change2",
    "m3_scy_change2",
    "m3_lcdc_win_map_change",
    "m3_lcdc_win_map_change2",
    "m3_lcdc_tile_sel_win_change",
    "m3_lcdc_obj_en_change",
    "m3_lcdc_obj_en_change_variant",
    "m3_lcdc_obj_size_change",
    "m3_lcdc_obj_size_change_scx",
    "m3_obp0_change",
    "m3_lcdc_bg_en_change",
    "m3_wx_4_change",
    "m3_wx_4_change_sprites",
    "m3_wx_5_change",
    "m3_wx_6_change",
    "m3_lcdc_win_en_change_multiple",
    "m3_lcdc_win_en_change_multiple_wx",
}
MEALYBUG_CGB_REFERENCE_SHADE_MAP = {
    "m3_scx_high_5_bits_change2": {
        (255, 255, 255): 0,
        (123, 255, 49): 1,
        (255, 132, 132): 1,
        (0, 0, 0): 3,
    },
    "m3_lcdc_win_map_change2": {
        (255, 255, 255): 0,
        (123, 255, 49): 1,
        (255, 132, 132): 1,
        (0, 0, 0): 3,
    },
    "m3_lcdc_tile_sel_change2": {
        (255, 255, 255): 0,
        (123, 255, 49): 1,
        (255, 132, 132): 1,
        (0, 99, 198): 2,
        (0, 0, 0): 3,
    },
    "m3_lcdc_tile_sel_win_change2": {
        (255, 255, 255): 0,
        (123, 255, 49): 1,
        (255, 132, 132): 1,
        (0, 99, 198): 2,
        (0, 0, 0): 3,
    },
    "m3_scy_change2": {
        (255, 255, 255): 0,
        (123, 255, 49): 1,
        (255, 132, 132): 1,
        (0, 99, 198): 2,
        (0, 0, 0): 3,
    },
}


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the PPU regression gate: dmg-acid2, Mooneye "
            "acceptance/ppu ROMs, and selected Mealybug mode-3 image cases."
        )
    )
    parser.add_argument(
        "--rom-root",
        type=Path,
        default=ROOT / "roms",
        help="Directory containing downloaded external test ROM suites.",
    )
    parser.add_argument(
        "--mooneye-root",
        type=Path,
        help="Directory containing Mooneye test-suite files; defaults to roms/mooneye-test-suite.",
    )
    parser.add_argument(
        "--mealybug-root",
        type=Path,
        help="Directory containing Mealybug Tearoom files; defaults to roms/mealybug-tearoom-tests.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Maximum CPU steps per Mooneye/Mealybug ROM before timing out.",
    )
    parser.add_argument("--skip-dmg-acid2", action="store_true", help="Skip dmg-acid2 visual smoke.")
    parser.add_argument("--skip-mooneye", action="store_true", help="Skip Mooneye PPU tests.")
    parser.add_argument("--skip-mealybug", action="store_true", help="Skip selected Mealybug image tests.")
    parser.add_argument(
        "--include-mealybug-candidates",
        action="store_true",
        help="Also run adjacent Mealybug cases that are tracked but not part of the strict gate yet.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on every failing selected external test.",
    )
    return parser.parse_args()


def framebuffer_rgb_bytes(emulator: Emulator) -> bytes:
    rgb = bytearray()
    for row in emulator.bus.ppu.framebuffer:
        for shade in row:
            rgb.extend(DMG_GRAYSCALE[shade & 0x03])
    return bytes(rgb)


def run_dmg_acid2(path: Path) -> GateResult:
    if not path.exists():
        return GateResult("dmg-acid2", False, f"missing ROM: {path}")

    emulator = Emulator.from_rom_file(path, serial_sink=lambda _: None)
    emulator.run(max_frames=1)
    digest = hashlib.sha256(framebuffer_rgb_bytes(emulator)).hexdigest()
    passed = digest == DMG_ACID2_REFERENCE_RGB_SHA256
    return GateResult(
        "dmg-acid2",
        passed,
        f"frames={emulator.bus.ppu.frame_count} rgb_sha256={digest}",
    )


def cpu_register_tuple(emulator: Emulator) -> tuple[int, int, int, int, int, int]:
    cpu = emulator.cpu
    return (cpu.b, cpu.c, cpu.d, cpu.e, cpu.h, cpu.l)


def run_until_ld_b_b(path: Path, max_steps: int) -> tuple[Emulator, int, bool]:
    emulator = Emulator.from_rom_file(path, serial_sink=lambda _: None)
    for steps in range(max_steps + 1):
        if emulator.bus.read8(emulator.cpu.pc) == 0x40:
            return emulator, steps, True
        if steps == max_steps:
            break
        emulator.step()
    return emulator, max_steps, False


def find_file(root: Path, filename: str) -> Path | None:
    exact = root / filename
    if exact.exists():
        return exact
    matches = sorted(root.rglob(filename)) if root.exists() else []
    return matches[0] if matches else None


def run_mooneye_test(root: Path, filename: str, max_steps: int) -> GateResult:
    path = find_file(root, filename)
    if path is None:
        return GateResult(filename, False, f"missing Mooneye ROM under {root}")

    emulator, steps, found = run_until_ld_b_b(path, max_steps)
    registers = cpu_register_tuple(emulator)
    if not found:
        return GateResult(filename, False, f"timeout steps={steps} cycles={emulator.cpu.cycles}")
    if registers == MOONEYE_PASS_REGISTERS:
        return GateResult(filename, True, f"steps={steps} cycles={emulator.cpu.cycles}")
    if registers == MOONEYE_FAIL_REGISTERS:
        return GateResult(filename, False, f"reported failure steps={steps} cycles={emulator.cpu.cycles}")
    return GateResult(
        filename,
        False,
        f"unexpected breakpoint registers={registers} steps={steps} cycles={emulator.cpu.cycles}",
    )


def paeth_predictor(left: int, up: int, up_left: int) -> int:
    p = left + up - up_left
    pa = abs(p - left)
    pb = abs(p - up)
    pc = abs(p - up_left)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return up_left


def unpack_png_samples(
    row: bytes,
    width: int,
    bit_depth: int,
    color_type: int,
    palette: list[tuple[int, int, int]] | None = None,
) -> bytes:
    rgb = bytearray()
    if color_type == 0:
        if bit_depth == 8:
            for sample in row[:width]:
                rgb.extend((sample, sample, sample))
            return bytes(rgb)
        if bit_depth in {1, 2, 4}:
            mask = (1 << bit_depth) - 1
            max_sample = mask
            for x in range(width):
                bit_offset = x * bit_depth
                byte = row[bit_offset // 8]
                shift = 8 - bit_depth - (bit_offset % 8)
                sample = (byte >> shift) & mask
                value = (sample * 255) // max_sample
                rgb.extend((value, value, value))
            return bytes(rgb)
    if color_type == 2 and bit_depth == 8:
        return row[: width * 3]
    if color_type == 3:
        if palette is None:
            raise ValueError("Indexed PNG missing PLTE chunk")
        if bit_depth not in {1, 2, 4, 8}:
            raise ValueError(f"Unsupported indexed PNG bit depth {bit_depth}")
        mask = (1 << bit_depth) - 1
        for x in range(width):
            if bit_depth == 8:
                index = row[x]
            else:
                bit_offset = x * bit_depth
                byte = row[bit_offset // 8]
                shift = 8 - bit_depth - (bit_offset % 8)
                index = (byte >> shift) & mask
            if index >= len(palette):
                raise ValueError(f"Indexed PNG palette index out of range: {index}")
            rgb.extend(palette[index])
        return bytes(rgb)
    raise ValueError(f"Unsupported PNG format: bit_depth={bit_depth}, color_type={color_type}")


def read_png_rgb(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG file: {path}")

    pos = 8
    width = height = bit_depth = color_type = None
    compressed = bytearray()
    palette: list[tuple[int, int, int]] | None = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if (compression, filter_method, interlace) != (0, 0, 0):
                raise ValueError(f"Unsupported PNG options in {path}")
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"PLTE":
            if len(chunk) % 3:
                raise ValueError(f"Invalid PNG palette length in {path}")
            palette = [
                (chunk[index], chunk[index + 1], chunk[index + 2])
                for index in range(0, len(chunk), 3)
            ]
        elif chunk_type == b"IEND":
            break

    if width is None or height is None or bit_depth is None or color_type is None:
        raise ValueError(f"PNG missing IHDR: {path}")

    channels = 1 if color_type in {0, 3} else 3 if color_type == 2 else None
    if channels is None:
        raise ValueError(f"Unsupported PNG color type {color_type}: {path}")
    bits_per_pixel = channels * bit_depth
    scanline_bytes = (width * bits_per_pixel + 7) // 8
    filter_bpp = max(1, (bits_per_pixel + 7) // 8)
    raw = zlib.decompress(bytes(compressed))
    previous = bytearray(scanline_bytes)
    rgb = bytearray()
    offset = 0
    for _y in range(height):
        filter_type = raw[offset]
        offset += 1
        scanline = raw[offset : offset + scanline_bytes]
        offset += scanline_bytes
        reconstructed = bytearray(scanline_bytes)
        for x, value in enumerate(scanline):
            left = reconstructed[x - filter_bpp] if x >= filter_bpp else 0
            up = previous[x]
            up_left = previous[x - filter_bpp] if x >= filter_bpp else 0
            if filter_type == 0:
                reconstructed[x] = value
            elif filter_type == 1:
                reconstructed[x] = (value + left) & 0xFF
            elif filter_type == 2:
                reconstructed[x] = (value + up) & 0xFF
            elif filter_type == 3:
                reconstructed[x] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                reconstructed[x] = (value + paeth_predictor(left, up, up_left)) & 0xFF
            else:
                raise ValueError(f"Unsupported PNG filter {filter_type}: {path}")
        rgb.extend(unpack_png_samples(bytes(reconstructed), width, bit_depth, color_type, palette))
        previous = reconstructed
    return width, height, bytes(rgb)


def find_mealybug_rom(root: Path, stem: str) -> Path | None:
    candidates = sorted(root.rglob(f"{stem}.gb")) if root.exists() else []
    return candidates[0] if candidates else None


def find_mealybug_expected(root: Path, stem: str) -> Path | None:
    if not root.exists():
        return None
    candidates = sorted(root.rglob(f"{stem}.png"))
    if not candidates:
        return None
    dmg_cpu_b = [path for path in candidates if "dmg-cpu b" in {part.lower() for part in path.parts}]
    if dmg_cpu_b:
        return dmg_cpu_b[0]
    dmg_blob = [path for path in candidates if "dmg-blob" in {part.lower() for part in path.parts}]
    if dmg_blob:
        return dmg_blob[0]
    dmg_candidates = [path for path in candidates if any(part.lower().startswith("dmg") for part in path.parts)]
    if dmg_candidates:
        return dmg_candidates[0]
    if stem in MEALYBUG_CGB_REFERENCE_SHADE_MAP:
        cgb_cpu_c = [path for path in candidates if "cpu cgb c" in {part.lower() for part in path.parts}]
        if cgb_cpu_c:
            return cgb_cpu_c[0]
    return None


def normalize_mealybug_expected_rgb(stem: str, expected_rgb: bytes) -> bytes:
    shade_map = MEALYBUG_CGB_REFERENCE_SHADE_MAP.get(stem)
    if shade_map is None:
        return expected_rgb

    normalized = bytearray()
    for index in range(0, len(expected_rgb), 3):
        color = tuple(expected_rgb[index : index + 3])
        if color not in shade_map:
            raise ValueError(f"Unexpected CGB reference color for {stem}: {color}")
        normalized.extend(DMG_GRAYSCALE[shade_map[color]])
    return bytes(normalized)


def run_mealybug_test(root: Path, stem: str, max_steps: int) -> GateResult:
    rom_path = find_mealybug_rom(root, stem)
    expected_path = find_mealybug_expected(root, stem)
    if rom_path is None:
        return GateResult(stem, False, f"missing Mealybug ROM under {root}")
    if expected_path is None:
        return GateResult(stem, False, f"missing Mealybug expected PNG under {root}")

    emulator, steps, found = run_until_ld_b_b(rom_path, max_steps)
    if not found:
        return GateResult(stem, False, f"timeout steps={steps} cycles={emulator.cpu.cycles}")

    width, height, expected_rgb = read_png_rgb(expected_path)
    expected_rgb = normalize_mealybug_expected_rgb(stem, expected_rgb)
    actual_rgb = framebuffer_rgb_bytes(emulator)
    if (width, height) != (SCREEN_WIDTH, SCREEN_HEIGHT):
        return GateResult(stem, False, f"unexpected expected-image dimensions {(width, height)}")

    if actual_rgb == expected_rgb:
        return GateResult(stem, True, f"steps={steps} cycles={emulator.cpu.cycles}")

    mismatches = sum(
        1
        for index in range(0, len(actual_rgb), 3)
        if actual_rgb[index : index + 3] != expected_rgb[index : index + 3]
    )
    return GateResult(
        stem,
        False,
        f"pixels_different={mismatches} steps={steps} cycles={emulator.cpu.cycles}",
    )


def print_results(
    title: str,
    results: list[GateResult],
    *,
    expected_pass: set[str] | None = None,
    strict: bool = False,
) -> bool:
    print(f"== {title} ==")
    if not results:
        print("SKIP no tests selected")
        return True
    if expected_pass is None:
        expected_pass = {result.name for result in results}
    ok = True
    for result in results:
        expected = result.name in expected_pass or strict
        if result.passed and expected:
            status = "PASS"
            contributes_ok = True
        elif not result.passed and expected:
            status = "FAIL"
            contributes_ok = False
        elif result.passed and not expected:
            status = "XPASS"
            contributes_ok = False
        else:
            status = "XFAIL"
            contributes_ok = True
        print(f"{status} {result.name} {result.detail}")
        ok = ok and contributes_ok
    return ok


def main() -> int:
    args = parse_args()
    if args.max_steps < 1:
        raise SystemExit("--max-steps must be positive")

    rom_root = args.rom_root.resolve()
    mooneye_root = (args.mooneye_root or rom_root / "mooneye-test-suite").resolve()
    mealybug_root = (args.mealybug_root or rom_root / "mealybug-tearoom-tests").resolve()

    ok = True
    if not args.skip_dmg_acid2:
        ok = print_results(
            "dmg-acid2 visual smoke",
            [run_dmg_acid2(DMG_ACID2_ROM)],
            strict=True,
        ) and ok
    if not args.skip_mooneye:
        results = [run_mooneye_test(mooneye_root, filename, args.max_steps) for filename in MOONEYE_PPU_TESTS]
        ok = print_results(
            "Mooneye acceptance/ppu",
            results,
            expected_pass=MOONEYE_EXPECTED_PASS,
            strict=args.strict,
        ) and ok
    if not args.skip_mealybug:
        results = [run_mealybug_test(mealybug_root, stem, args.max_steps) for stem in MEALYBUG_MODE3_TESTS]
        ok = print_results(
            "Mealybug mode-3 image cases",
            results,
            expected_pass=MEALYBUG_EXPECTED_PASS,
            strict=args.strict,
        ) and ok
        if args.include_mealybug_candidates:
            candidate_results = [
                run_mealybug_test(mealybug_root, stem, args.max_steps)
                for stem in MEALYBUG_MODE3_CANDIDATE_TESTS
            ]
            candidate_ok = print_results(
                "Mealybug mode-3 candidate cases",
                candidate_results,
                expected_pass=set(),
                strict=args.strict,
            )
            if args.strict:
                ok = candidate_ok and ok

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
