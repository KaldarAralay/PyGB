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
from pyboy import PyBoy  # noqa: E402
from ppu import DMG_GRAYSCALE, SCREEN_HEIGHT, SCREEN_WIDTH  # noqa: E402


BUTTON_SCRIPT = (
    "4200:start:20,4550:a:20,4900:a:20,5300:a:20,"
    "5700:right:80,5900:right:80,6200:up:80"
)
DEFAULT_CROP = (0, 40, 64, 72)
EXPECTED_ENCYCLOPEDIA_TILES = (0x7C, 0x7D, 0x7E, 0x7F, 0x7C, 0x7D, 0x7E, 0x7F)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare the Pokemon Red Oak's Lab encyclopedia crop against PyBoy."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "PRed.gb")
    parser.add_argument(
        "--save-file",
        type=Path,
        default=ROOT / "saves" / "pokemon-red-test.sav",
    )
    parser.add_argument("--frames", type=int, default=6800)
    parser.add_argument("--button-script", default=BUTTON_SCRIPT)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "qa-output" / "oak-encyclopedia-oracle",
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


def encyclopedia_tiles(oam: list[int]) -> tuple[int, ...]:
    return tuple(oam[index * 4 + 2] for index in range(20, 28))


def main() -> int:
    args = parse_args()
    if args.frames <= 0:
        raise SystemExit("--frames must be positive")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    gbemu = run_gbemu(args)
    pyboy_image, pyboy_oam = run_pyboy(args)

    gbemu_image = framebuffer_image(gbemu.bus.ppu.framebuffer)
    gbemu_image.save(args.output_dir / "gbemu.png")
    pyboy_image.save(args.output_dir / "pyboy.png")

    gbemu_tiles = encyclopedia_tiles(list(gbemu.bus.oam))
    pyboy_tiles = encyclopedia_tiles(pyboy_oam)
    gbemu_crop = crop_shades_from_framebuffer(gbemu.bus.ppu.framebuffer, DEFAULT_CROP)
    pyboy_crop = crop_shades_from_pyboy(pyboy_image, DEFAULT_CROP)
    diff_pixels = count_differences(gbemu_crop, pyboy_crop)

    print(f"crop={DEFAULT_CROP} diff_pixels={diff_pixels}")
    print("gbemu_oam_tiles=" + " ".join(f"{tile:02X}" for tile in gbemu_tiles))
    print("pyboy_oam_tiles=" + " ".join(f"{tile:02X}" for tile in pyboy_tiles))

    if gbemu_tiles != EXPECTED_ENCYCLOPEDIA_TILES:
        raise SystemExit(
            "GBemu encyclopedia OAM tiles do not match the expected 7C-7F pattern"
        )
    if pyboy_tiles != EXPECTED_ENCYCLOPEDIA_TILES:
        raise SystemExit(
            "PyBoy encyclopedia OAM tiles do not match the expected 7C-7F pattern"
        )
    if diff_pixels:
        raise SystemExit("Oak's Lab encyclopedia crop differs from PyBoy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
