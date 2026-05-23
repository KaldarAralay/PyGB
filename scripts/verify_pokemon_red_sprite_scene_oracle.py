from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / ".tools" / "oracle" / "vendor"
sys.path.insert(0, str(VENDOR))
sys.path.insert(0, str(ROOT))

from button_script import parse_button_script  # noqa: E402
from emulator import Emulator  # noqa: E402
from ppu import DMG_GRAYSCALE, SCREEN_HEIGHT, SCREEN_WIDTH  # noqa: E402
from pyboy import PyBoy  # noqa: E402


BUTTON_SCRIPT = "4200:start:20,4550:a:20,4900:a:20,5300:a:20"
DEFAULT_CROP = (0, 0, SCREEN_WIDTH, SCREEN_HEIGHT)
DEFAULT_FRAMES = 5250


def parse_crop(value: str) -> tuple[int, int, int, int]:
    try:
        left, top, right, bottom = (int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--crop must be four comma-separated integers: left,top,right,bottom"
        ) from exc
    if not (0 <= left < right <= SCREEN_WIDTH and 0 <= top < bottom <= SCREEN_HEIGHT):
        raise argparse.ArgumentTypeError(
            f"--crop must fit within 0,0,{SCREEN_WIDTH},{SCREEN_HEIGHT}"
        )
    return left, top, right, bottom


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the Pokemon Red saved-game sprite-heavy scene against PyBoy."
        )
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "PRed.gb")
    parser.add_argument(
        "--save-file",
        type=Path,
        default=ROOT / "saves" / "pokemon-red-test.sav",
    )
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--button-script", default=BUTTON_SCRIPT)
    parser.add_argument("--crop", type=parse_crop, default=DEFAULT_CROP)
    parser.add_argument("--max-diff-pixels", type=int, default=0)
    parser.add_argument(
        "--compare-hidden-oam",
        action="store_true",
        help="Compare all 40 OAM entries instead of only visible entries.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "qa-output" / "pokemon-red-sprite-scene-oracle",
    )
    return parser.parse_args()


def run_gbemu(args: argparse.Namespace) -> Emulator:
    emulator = Emulator.from_rom_file(args.rom)
    emulator.load_save_file(args.save_file)
    script = parse_button_script(args.button_script)
    for _ in range(args.frames):
        frame = emulator.bus.ppu.frame_count
        emulator.set_buttons(script.buttons_for_frame(frame, set()))
        emulator.run(max_frames=1)
    return emulator


def run_pyboy(args: argparse.Namespace):
    pyboy = PyBoy(
        str(args.rom),
        window="null",
        ram_file=io.BytesIO(args.save_file.read_bytes()),
        sound_emulated=False,
        cgb=False,
    )
    pyboy.set_emulation_speed(0)
    script = parse_button_script(args.button_script)
    pressed: set[str] = set()
    try:
        for frame in range(args.frames):
            target = script.buttons_for_frame(frame)
            for button in sorted(pressed - target):
                pyboy.button_release(button)
            for button in sorted(target - pressed):
                pyboy.button_press(button)
            pressed = set(target)
            pyboy.tick(1, render=frame == args.frames - 1)
        image = pyboy.screen.image.convert("RGB")
        oam = [pyboy.memory[0xFE00 + offset] for offset in range(160)]
        return image, oam
    finally:
        pyboy.stop(save=False)


def framebuffer_image(framebuffer: list[list[int]]):
    from PIL import Image

    image = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT))
    pixels = image.load()
    for y, row in enumerate(framebuffer):
        for x, shade in enumerate(row):
            pixels[x, y] = DMG_GRAYSCALE[shade & 0x03]
    return image


def pyboy_shade(pixel: tuple[int, int, int]) -> int:
    luminance = sum(pixel) / 3
    return min(
        range(4),
        key=lambda shade: abs(luminance - DMG_GRAYSCALE[shade][0]),
    )


def crop_shades_from_pyboy(image, crop: tuple[int, int, int, int]) -> list[list[int]]:
    left, top, right, bottom = crop
    return [
        [pyboy_shade(image.getpixel((x, y))) for x in range(left, right)]
        for y in range(top, bottom)
    ]


def crop_shades_from_framebuffer(
    framebuffer: list[list[int]],
    crop: tuple[int, int, int, int],
) -> list[list[int]]:
    left, top, right, bottom = crop
    return [row[left:right] for row in framebuffer[top:bottom]]


def count_differences(a: list[list[int]], b: list[list[int]]) -> int:
    return sum(
        1
        for row_a, row_b in zip(a, b)
        for px_a, px_b in zip(row_a, row_b)
        if px_a != px_b
    )


def selected_oam_entries(
    oam: list[int],
    *,
    visible_only: bool,
) -> tuple[tuple[int, int, int, int, int], ...]:
    entries: list[tuple[int, int, int, int, int]] = []
    for index in range(40):
        offset = index * 4
        y, x, tile, attr = oam[offset : offset + 4]
        if not visible_only or (0 < y < 160 and 0 < x < 168):
            entries.append((index, y, x, tile, attr))
    return tuple(entries)


def format_oam_entries(entries: tuple[tuple[int, int, int, int, int], ...]) -> str:
    if not entries:
        return "<none>"
    return " ".join(
        f"{index}:{y:02X},{x:02X},{tile:02X},{attr:02X}"
        for index, y, x, tile, attr in entries
    )


def first_oam_mismatch(
    gbemu_entries: tuple[tuple[int, int, int, int, int], ...],
    pyboy_entries: tuple[tuple[int, int, int, int, int], ...],
) -> str:
    for gbemu_entry, pyboy_entry in zip(gbemu_entries, pyboy_entries):
        if gbemu_entry != pyboy_entry:
            return f"gbemu={gbemu_entry} pyboy={pyboy_entry}"
    if len(gbemu_entries) != len(pyboy_entries):
        return f"entry_count gbemu={len(gbemu_entries)} pyboy={len(pyboy_entries)}"
    return ""


def main() -> int:
    args = parse_args()
    if args.frames <= 0:
        raise SystemExit("--frames must be positive")
    if args.max_diff_pixels < 0:
        raise SystemExit("--max-diff-pixels must be non-negative")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    gbemu = run_gbemu(args)
    pyboy_image, pyboy_oam = run_pyboy(args)

    gbemu_image = framebuffer_image(gbemu.bus.ppu.framebuffer)
    gbemu_image.save(args.output_dir / "gbemu.png")
    pyboy_image.save(args.output_dir / "pyboy.png")

    gbemu_crop = crop_shades_from_framebuffer(gbemu.bus.ppu.framebuffer, args.crop)
    pyboy_crop = crop_shades_from_pyboy(pyboy_image, args.crop)
    diff_pixels = count_differences(gbemu_crop, pyboy_crop)

    visible_only = not args.compare_hidden_oam
    gbemu_oam = selected_oam_entries(list(gbemu.bus.oam), visible_only=visible_only)
    pyboy_selected_oam = selected_oam_entries(pyboy_oam, visible_only=visible_only)

    print(f"crop={args.crop} diff_pixels={diff_pixels}")
    print(f"oam_visible_only={int(visible_only)}")
    print("gbemu_oam=" + format_oam_entries(gbemu_oam))
    print("pyboy_oam=" + format_oam_entries(pyboy_selected_oam))

    if diff_pixels > args.max_diff_pixels:
        raise SystemExit(
            f"Pokemon Red sprite scene crop differs from PyBoy: "
            f"{diff_pixels} > {args.max_diff_pixels}"
        )
    if gbemu_oam != pyboy_selected_oam:
        mismatch = first_oam_mismatch(gbemu_oam, pyboy_selected_oam)
        raise SystemExit(f"Pokemon Red sprite scene OAM differs from PyBoy: {mismatch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
