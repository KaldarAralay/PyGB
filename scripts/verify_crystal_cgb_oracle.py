from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / ".tools" / "oracle" / "vendor"
sys.path.insert(0, str(VENDOR))
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from button_script import ButtonScript, load_button_script, parse_button_script  # noqa: E402
from cartridge import Cartridge  # noqa: E402
from emulator import Emulator  # noqa: E402
from ppu import SCREEN_HEIGHT, SCREEN_WIDTH, framebuffer_pixel_to_rgb  # noqa: E402
from scripts.verify_crystal_cgb_render import (  # noqa: E402
    DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME,
    advance_to_checkpoint,
    collect_crystal_stage_metrics,
    evaluate_crystal_stage_metrics,
    parse_checkpoint_frames,
)


DEFAULT_CHECKPOINT_FRAMES = (60, 600, 2400, 3600, 4800)
DEFAULT_BUTTON_SCRIPT = "3900:start:20,4300:a:20"
DEFAULT_OUTPUT_DIR = ROOT / "qa-output" / "crystal-cgb-pyboy-oracle"
DEFAULT_MAJOR_DELTA_THRESHOLD = 224
DEFAULT_MAX_MAJOR_DIFF_RATIO = 0.95


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare staged Pokemon Crystal CGB RGB frames against PyBoy."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "crystal.gbc")
    parser.add_argument(
        "--checkpoint-frames",
        help=(
            "Comma or space separated checkpoint frames. Defaults to "
            f"{','.join(str(frame) for frame in DEFAULT_CHECKPOINT_FRAMES)}."
        ),
    )
    parser.add_argument(
        "--button-script",
        default=DEFAULT_BUTTON_SCRIPT,
        help=(
            "Inline frame:buttons[:duration] script. Defaults to a small "
            "late title/menu progression script."
        ),
    )
    parser.add_argument(
        "--button-script-path",
        type=Path,
        help="File containing frame:buttons[:duration] input script entries.",
    )
    parser.add_argument(
        "--no-button-script",
        action="store_true",
        help="Disable the default button script and run passive checkpoints only.",
    )
    parser.add_argument(
        "--attribute-checkpoint-frame",
        type=int,
        default=DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME,
        help="First checkpoint where GBemu CGB tile attributes are required.",
    )
    parser.add_argument("--min-unique-rgb-colors", type=int, default=2)
    parser.add_argument("--min-pyboy-unique-rgb-colors", type=int, default=2)
    parser.add_argument(
        "--major-delta-threshold",
        type=int,
        default=DEFAULT_MAJOR_DELTA_THRESHOLD,
        help="A pixel is a major mismatch when any RGB channel exceeds this delta.",
    )
    parser.add_argument(
        "--max-major-diff-ratio",
        type=float,
        default=DEFAULT_MAX_MAJOR_DIFF_RATIO,
        help="Fail a stage when the major mismatch ratio exceeds this value.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for GBemu, PyBoy, and diff PNGs.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "crystal-cgb-pyboy-oracle.json",
        help="Write oracle metrics to this JSON file.",
    )
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args()


def load_oracle_button_script(args: argparse.Namespace) -> ButtonScript | None:
    if args.no_button_script:
        if args.button_script_path is not None:
            raise ValueError("use either --no-button-script or --button-script-path")
        return None
    if args.button_script and args.button_script_path is not None:
        raise ValueError("use either --button-script or --button-script-path, not both")
    if args.button_script_path is not None:
        return load_button_script(args.button_script_path)
    if args.button_script:
        return parse_button_script(args.button_script)
    return None


def framebuffer_to_rgb_image(framebuffer: list[list[int]]) -> Image.Image:
    image = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT))
    pixels = image.load()
    for y, row in enumerate(framebuffer):
        for x, pixel in enumerate(row):
            pixels[x, y] = framebuffer_pixel_to_rgb(pixel)
    return image


def image_unique_colors(image: Image.Image) -> set[tuple[int, int, int]]:
    data = image.convert("RGB").tobytes()
    return {
        (data[index], data[index + 1], data[index + 2])
        for index in range(0, len(data), 3)
    }


def image_metrics(image: Image.Image) -> dict[str, Any]:
    rgb = image.convert("RGB")
    unique = sorted(image_unique_colors(rgb))
    return {
        "rgb_pixels": rgb.size[0] * rgb.size[1],
        "unique_rgb_colors": len(unique),
        "unique_rgb_values": unique,
        "size": list(rgb.size),
    }


def compare_rgb_images(
    gbemu_image: Image.Image,
    pyboy_image: Image.Image,
    *,
    major_delta_threshold: int,
) -> tuple[dict[str, Any], Image.Image]:
    gbemu_rgb = gbemu_image.convert("RGB")
    pyboy_rgb = pyboy_image.convert("RGB")
    if gbemu_rgb.size != pyboy_rgb.size:
        raise ValueError(f"image size mismatch: gbemu={gbemu_rgb.size} pyboy={pyboy_rgb.size}")

    exact_diff_pixels = 0
    major_diff_pixels = 0
    max_color_delta = 0
    total_abs_delta = 0
    diff_values: list[tuple[int, int, int]] = []
    gbemu_data = gbemu_rgb.tobytes()
    pyboy_data = pyboy_rgb.tobytes()
    for index in range(0, len(gbemu_data), 3):
        red_delta = abs(gbemu_data[index] - pyboy_data[index])
        green_delta = abs(gbemu_data[index + 1] - pyboy_data[index + 1])
        blue_delta = abs(gbemu_data[index + 2] - pyboy_data[index + 2])
        pixel_delta = max(red_delta, green_delta, blue_delta)
        if pixel_delta:
            exact_diff_pixels += 1
        if pixel_delta > major_delta_threshold:
            major_diff_pixels += 1
        max_color_delta = max(max_color_delta, pixel_delta)
        total_abs_delta += red_delta + green_delta + blue_delta
        diff_values.append(
            (
                min(red_delta * 4, 255),
                min(green_delta * 4, 255),
                min(blue_delta * 4, 255),
            )
        )

    total_pixels = gbemu_rgb.size[0] * gbemu_rgb.size[1]
    diff_image = Image.new("RGB", gbemu_rgb.size)
    diff_image.putdata(diff_values)
    return (
        {
            "diff_pixels": exact_diff_pixels,
            "diff_ratio": exact_diff_pixels / total_pixels,
            "major_diff_pixels": major_diff_pixels,
            "major_diff_ratio": major_diff_pixels / total_pixels,
            "max_color_delta": max_color_delta,
            "mean_abs_delta": total_abs_delta / (total_pixels * 3),
            "major_delta_threshold": major_delta_threshold,
        },
        diff_image,
    )


def stage_requires_color_variety(
    checkpoint: int,
    checkpoint_frames: list[int],
    *,
    attribute_checkpoint_frame: int,
) -> bool:
    return (
        len(checkpoint_frames) == 1
        or checkpoint == checkpoint_frames[0]
        or checkpoint >= attribute_checkpoint_frame
    )


def evaluate_oracle_stage(
    stage: dict[str, Any],
    *,
    min_pyboy_unique_rgb_colors: int,
    require_pyboy_color_variety: bool,
    max_major_diff_ratio: float,
) -> list[str]:
    failures = list(stage.get("gbemu_failures", []))
    label = f"crystal oracle frame {stage.get('checkpoint', '?')}"

    pyboy = stage["pyboy"]
    if pyboy.get("size") != [SCREEN_WIDTH, SCREEN_HEIGHT]:
        failures.append(f"{label}: PyBoy image size is {pyboy.get('size')}")
    if int(pyboy.get("rgb_pixels", 0)) != SCREEN_WIDTH * SCREEN_HEIGHT:
        failures.append(f"{label}: PyBoy RGB image is incomplete")
    if require_pyboy_color_variety and int(pyboy.get("unique_rgb_colors", 0)) < min_pyboy_unique_rgb_colors:
        failures.append(
            f"{label}: PyBoy frame has {pyboy.get('unique_rgb_colors')} unique RGB colors, "
            f"expected at least {min_pyboy_unique_rgb_colors}"
        )

    diff = stage["diff"]
    if float(diff.get("major_diff_ratio", 1.0)) > max_major_diff_ratio:
        failures.append(
            f"{label}: major diff ratio {diff['major_diff_ratio']:.4f} > "
            f"{max_major_diff_ratio:.4f}"
        )
    return failures


def save_stage_images(
    *,
    output_dir: Path,
    checkpoint: int,
    gbemu_image: Image.Image,
    pyboy_image: Image.Image,
    diff_image: Image.Image,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"crystal-cgb-oracle-{checkpoint:05d}"
    paths = {
        "gbemu_png": output_dir / f"{stem}-gbemu.png",
        "pyboy_png": output_dir / f"{stem}-pyboy.png",
        "diff_png": output_dir / f"{stem}-diff.png",
    }
    gbemu_image.save(paths["gbemu_png"])
    pyboy_image.save(paths["pyboy_png"])
    diff_image.save(paths["diff_png"])
    return {key: str(path) for key, path in paths.items()}


def run_gbemu_stages(
    *,
    rom: Path,
    checkpoint_frames: list[int],
    button_script: ButtonScript | None,
    attribute_checkpoint_frame: int,
    min_unique_rgb_colors: int,
) -> list[dict[str, Any]]:
    cartridge = Cartridge.from_file(rom)
    emulator = Emulator(cartridge, serial_sink=lambda _: None, mode="cgb")
    initial_key1 = emulator.bus.read8(0xFF4D)
    stages: list[dict[str, Any]] = []
    for checkpoint in checkpoint_frames:
        advance_to_checkpoint(emulator, checkpoint, button_script)
        metrics = collect_crystal_stage_metrics(
            emulator,
            cartridge,
            checkpoint=checkpoint,
            initial_key1=initial_key1,
            frame_output_dir=None,
        )
        require_attributes = checkpoint >= attribute_checkpoint_frame
        require_color_variety = stage_requires_color_variety(
            checkpoint,
            checkpoint_frames,
            attribute_checkpoint_frame=attribute_checkpoint_frame,
        )
        failures = evaluate_crystal_stage_metrics(
            metrics,
            min_unique_rgb_colors=min_unique_rgb_colors,
            require_color_variety=require_color_variety,
            require_attributes=require_attributes,
        )
        stages.append(
            {
                "checkpoint": checkpoint,
                "metrics": metrics,
                "failures": failures,
                "image": framebuffer_to_rgb_image(emulator.bus.ppu.framebuffer),
            }
        )
    return stages


def apply_pyboy_buttons(pyboy: Any, pressed: set[str], target: set[str]) -> set[str]:
    for button in sorted(pressed - target):
        pyboy.button_release(button)
    for button in sorted(target - pressed):
        pyboy.button_press(button)
    return set(target)


def run_pyboy_stages(
    *,
    rom: Path,
    checkpoint_frames: list[int],
    button_script: ButtonScript | None,
) -> dict[int, Image.Image]:
    from pyboy import PyBoy

    pyboy = PyBoy(str(rom), window="null", sound_emulated=False, cgb=True)
    pyboy.set_emulation_speed(0)
    checkpoints = set(checkpoint_frames)
    captures: dict[int, Image.Image] = {}
    pressed: set[str] = set()
    try:
        for frame in range(max(checkpoint_frames)):
            target = (
                button_script.buttons_for_frame(frame, set())
                if button_script is not None
                else set()
            )
            pressed = apply_pyboy_buttons(pyboy, pressed, target)
            rendered_frame = frame + 1
            render = rendered_frame in checkpoints
            pyboy.tick(1, render=render)
            if render:
                captures[rendered_frame] = pyboy.screen.image.convert("RGB")
    finally:
        pyboy.stop(save=False)
    return captures


def run_oracle(args: argparse.Namespace) -> dict[str, Any]:
    checkpoint_frames = (
        list(DEFAULT_CHECKPOINT_FRAMES)
        if args.checkpoint_frames is None
        else parse_checkpoint_frames(args.checkpoint_frames)
    )
    button_script = load_oracle_button_script(args)

    gbemu_stages = run_gbemu_stages(
        rom=args.rom,
        checkpoint_frames=checkpoint_frames,
        button_script=button_script,
        attribute_checkpoint_frame=args.attribute_checkpoint_frame,
        min_unique_rgb_colors=args.min_unique_rgb_colors,
    )
    pyboy_images = run_pyboy_stages(
        rom=args.rom,
        checkpoint_frames=checkpoint_frames,
        button_script=button_script,
    )

    failures: list[str] = []
    stages: list[dict[str, Any]] = []
    for gbemu_stage in gbemu_stages:
        checkpoint = gbemu_stage["checkpoint"]
        if checkpoint not in pyboy_images:
            failures.append(f"crystal oracle frame {checkpoint}: missing PyBoy capture")
            continue

        gbemu_image = gbemu_stage["image"]
        pyboy_image = pyboy_images[checkpoint]
        diff_metrics, diff_image = compare_rgb_images(
            gbemu_image,
            pyboy_image,
            major_delta_threshold=args.major_delta_threshold,
        )
        image_paths = save_stage_images(
            output_dir=args.output_dir,
            checkpoint=checkpoint,
            gbemu_image=gbemu_image,
            pyboy_image=pyboy_image,
            diff_image=diff_image,
        )
        require_pyboy_color_variety = stage_requires_color_variety(
            checkpoint,
            checkpoint_frames,
            attribute_checkpoint_frame=args.attribute_checkpoint_frame,
        )
        stage = {
            "checkpoint": checkpoint,
            "stage": gbemu_stage["metrics"]["stage"],
            "gbemu": gbemu_stage["metrics"],
            "gbemu_failures": gbemu_stage["failures"],
            "pyboy": image_metrics(pyboy_image),
            "diff": diff_metrics,
            "images": image_paths,
        }
        stage_failures = evaluate_oracle_stage(
            stage,
            min_pyboy_unique_rgb_colors=args.min_pyboy_unique_rgb_colors,
            require_pyboy_color_variety=require_pyboy_color_variety,
            max_major_diff_ratio=args.max_major_diff_ratio,
        )
        stage["failures"] = stage_failures
        stage["status"] = "pass" if not stage_failures else "fail"
        failures.extend(stage_failures)
        stages.append(stage)

    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "rom": str(args.rom),
        "checkpoint_frames": checkpoint_frames,
        "button_script": (
            None
            if button_script is None
            else (
                str(args.button_script_path)
                if args.button_script_path is not None
                else args.button_script
            )
        ),
        "button_script_source": (
            None
            if button_script is None
            else "path" if args.button_script_path is not None else "inline"
        ),
        "button_script_final_frame": None if button_script is None else button_script.final_frame,
        "thresholds": {
            "major_delta_threshold": args.major_delta_threshold,
            "max_major_diff_ratio": args.max_major_diff_ratio,
            "min_unique_rgb_colors": args.min_unique_rgb_colors,
            "min_pyboy_unique_rgb_colors": args.min_pyboy_unique_rgb_colors,
            "attribute_checkpoint_frame": args.attribute_checkpoint_frame,
        },
        "stages": stages,
    }


def main() -> int:
    args = parse_args()
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    if args.attribute_checkpoint_frame < 1:
        raise SystemExit("--attribute-checkpoint-frame must be positive")
    if args.min_unique_rgb_colors < 1:
        raise SystemExit("--min-unique-rgb-colors must be positive")
    if args.min_pyboy_unique_rgb_colors < 1:
        raise SystemExit("--min-pyboy-unique-rgb-colors must be positive")
    if not (0 <= args.major_delta_threshold <= 255):
        raise SystemExit("--major-delta-threshold must be between 0 and 255")
    if not (0 <= args.max_major_diff_ratio <= 1):
        raise SystemExit("--max-major-diff-ratio must be between 0 and 1")

    try:
        result = run_oracle(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    if result["failures"]:
        print("Crystal CGB PyBoy oracle: FAIL")
        for failure in result["failures"]:
            print(f"- {failure}")
        return 1
    print("Crystal CGB PyBoy oracle: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
