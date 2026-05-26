from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / ".tools" / "oracle" / "vendor"
sys.path.insert(0, str(VENDOR))
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from button_script import ButtonScript, load_button_script, parse_button_script  # noqa: E402
from cartridge import Cartridge  # noqa: E402
from emulator import Emulator  # noqa: E402
from ppu import (  # noqa: E402
    DOTS_PER_LINE,
    LINES_PER_FRAME,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    framebuffer_pixel_to_rgb,
)
from scripts.verify_crystal_cgb_render import (  # noqa: E402
    DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME,
    collect_crystal_stage_metrics,
    evaluate_crystal_stage_metrics,
    parse_checkpoint_frames,
)


DEFAULT_CHECKPOINT_FRAMES = (60, 600, 2400, 3600, 4800)
DEFAULT_BUTTON_SCRIPT = "3900:start:20,4300:a:20"
DEFAULT_OUTPUT_DIR = ROOT / "qa-output" / "crystal-cgb-pyboy-oracle"
DEFAULT_SOURCE_DEBUG_CHECKPOINTS = (3600,)
DEFAULT_CRYSTAL_SAVE_FILE = ROOT / "saves" / "pokemon-crystal-test.sav"
CRYSTAL_DYNAMIC_CHECKPOINT_FRAMES = (
    2400,
    3000,
    3600,
    4800,
    4920,
    5040,
    5400,
    6000,
    6600,
    7200,
    7800,
)
CRYSTAL_DYNAMIC_BUTTON_SCRIPT = (
    "3900:start:20,"
    "4300:a:20,"
    "4880:down:15,"
    "5000:up:15,"
    "5120:a:15,"
    "5500:a:10,"
    "5900:a:10,"
    "6300:a:10,"
    "6700:a:10,"
    "7100:a:10,"
    "7500:a:10"
)
CRYSTAL_DYNAMIC_STAGE_LABELS = {
    2400: "title-animation-palette",
    3000: "logo-animation-transition",
    3600: "static-title-lock",
    4800: "gender-menu-text",
    4920: "gender-menu-cursor-down",
    5040: "gender-menu-cursor-up",
    5400: "dialog-transition",
    6000: "intro-dialog-text",
    6600: "clock-day-menu",
    7200: "clock-minute-menu",
    7800: "clock-confirmation-menu",
}
CRYSTAL_OVERWORLD_CHECKPOINT_FRAMES = (
    4800,
    5400,
    7200,
    8400,
    8580,
    9060,
    9600,
    10200,
    10800,
    11400,
)
CRYSTAL_OVERWORLD_BUTTON_SCRIPT = (
    "3900:start:20,"
    "4300:a:20,"
    "5200:a:15,"
    "6000:left:24,"
    "6600:left:24,"
    "7200:down:24,"
    "7800:start:45,"
    "8400:b:30,"
    "9000:a:40,"
    "10000:a:20,"
    "10800:b:30"
)
CRYSTAL_OVERWORLD_STAGE_LABELS = {
    4800: "saved-game-summary",
    5400: "overworld-entry",
    7200: "overworld-movement-object-screen",
    8400: "overworld-menu-input-edge",
    8580: "overworld-menu-open",
    9060: "overworld-menu-close",
    9600: "overworld-text-box",
    10200: "overworld-text-advance",
    10800: "overworld-text-held",
    11400: "overworld-return",
}
CRYSTAL_OVERWORLD_ATTRIBUTE_CHECKPOINT_FRAME = 1_000_000
DEFAULT_MAJOR_DELTA_THRESHOLD = 224
DEFAULT_MAX_MAJOR_DIFF_RATIO = 0.95
DEFAULT_MAX_NONBLACK_DELTA_RATIO = 0.98
DOTS_PER_FRAME = DOTS_PER_LINE * LINES_PER_FRAME
SOURCE_DEBUG_SAMPLE_LIMIT = 32
PYBOY_CGB_POST_BOOT_IO_DEFAULTS = {
    0xFF00: 0xCF,
    0xFF01: 0x00,
    0xFF02: 0x7F,
    0xFF05: 0x00,
    0xFF06: 0x00,
    0xFF07: 0xF8,
    0xFF0F: 0xE1,
    0xFF40: 0x91,
    0xFF42: 0x00,
    0xFF43: 0x00,
    0xFF45: 0x00,
    0xFF47: 0xFC,
    0xFF48: 0xFF,
    0xFF49: 0xFF,
    0xFF4A: 0x00,
    0xFF4B: 0x00,
    0xFF4D: 0x7E,
    0xFF51: 0xFF,
    0xFF52: 0xF0,
    0xFF53: 0x1F,
    0xFF54: 0xF0,
    0xFF55: 0xFF,
}
CGB_ATTR_PRIORITY = 0x80
CGB_ATTR_Y_FLIP = 0x40
CGB_ATTR_X_FLIP = 0x20
CGB_ATTR_VRAM_BANK = 0x08
CGB_ATTR_PALETTE_MASK = 0x07
OBJ_PRIORITY = 0x80
OBJ_Y_FLIP = 0x40
OBJ_X_FLIP = 0x20
LCDC_ENABLE = 0x80
LCDC_WINDOW_TILEMAP = 0x40
LCDC_WINDOW_ENABLE = 0x20
LCDC_BG_WINDOW_TILE_DATA = 0x10
LCDC_BG_TILEMAP = 0x08
LCDC_OBJ_SIZE = 0x04
LCDC_OBJ_ENABLE = 0x02


@dataclass(frozen=True)
class OracleScenario:
    name: str
    description: str
    checkpoint_frames: tuple[int, ...]
    button_script: str | None
    source_debug_checkpoints: tuple[int, ...]
    stage_labels: dict[int, str]
    save_file: Path | None = None
    attribute_checkpoint_frame: int = DEFAULT_ATTRIBUTE_CHECKPOINT_FRAME
    gbemu_frame_clock: Literal["cpu", "ppu"] = "cpu"
    gbemu_input_clock: Literal["wall", "ppu"] = "wall"


ORACLE_SCENARIOS = {
    "static": OracleScenario(
        name="static",
        description="Static title/menu checkpoints that lock the current Crystal baseline.",
        checkpoint_frames=DEFAULT_CHECKPOINT_FRAMES,
        button_script=DEFAULT_BUTTON_SCRIPT,
        source_debug_checkpoints=DEFAULT_CHECKPOINT_FRAMES,
        stage_labels={
            2400: "title-animation-palette",
            3600: "static-title-lock",
            4800: "title-menu-start",
        },
    ),
    "dynamic": OracleScenario(
        name="dynamic",
        description=(
            "Title animation, gender-menu cursor movement, intro text, clock menu, "
            "and confirmation-menu checkpoints."
        ),
        checkpoint_frames=CRYSTAL_DYNAMIC_CHECKPOINT_FRAMES,
        button_script=CRYSTAL_DYNAMIC_BUTTON_SCRIPT,
        source_debug_checkpoints=CRYSTAL_DYNAMIC_CHECKPOINT_FRAMES,
        stage_labels=CRYSTAL_DYNAMIC_STAGE_LABELS,
    ),
    "overworld": OracleScenario(
        name="overworld",
        description=(
            "Saved-game Crystal path covering overworld movement, object-heavy "
            "screens, menu open/close, and a text box."
        ),
        checkpoint_frames=CRYSTAL_OVERWORLD_CHECKPOINT_FRAMES,
        button_script=CRYSTAL_OVERWORLD_BUTTON_SCRIPT,
        source_debug_checkpoints=CRYSTAL_OVERWORLD_CHECKPOINT_FRAMES,
        stage_labels=CRYSTAL_OVERWORLD_STAGE_LABELS,
        save_file=DEFAULT_CRYSTAL_SAVE_FILE,
        attribute_checkpoint_frame=CRYSTAL_OVERWORLD_ATTRIBUTE_CHECKPOINT_FRAME,
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare staged Pokemon Crystal CGB RGB frames against PyBoy."
    )
    parser.add_argument("--rom", type=Path, default=ROOT / "roms" / "crystal.gbc")
    parser.add_argument(
        "--save-file",
        type=Path,
        help=(
            "Battery RAM save file. Defaults to the selected scenario save fixture "
            "when that scenario starts from saved game state."
        ),
    )
    parser.add_argument(
        "--no-save-file",
        action="store_true",
        help="Disable the selected scenario save fixture.",
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(ORACLE_SCENARIOS),
        default="static",
        help="Named checkpoint/input scenario. Explicit checkpoint or button-script args override it.",
    )
    parser.add_argument(
        "--checkpoint-frames",
        help=(
            "Comma or space separated checkpoint frames. Defaults to the selected scenario, "
            "or the static baseline "
            f"{','.join(str(frame) for frame in DEFAULT_CHECKPOINT_FRAMES)}."
        ),
    )
    parser.add_argument(
        "--button-script",
        help=(
            "Inline frame:buttons[:duration] script. Defaults to the selected scenario script."
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
        help=(
            "First checkpoint where GBemu CGB tile attributes are required. "
            "Defaults to the selected scenario."
        ),
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
        "--max-nonblack-delta-ratio",
        type=float,
        default=DEFAULT_MAX_NONBLACK_DELTA_RATIO,
        help=(
            "Fail a stage when GBemu/PyBoy nonblack pixel coverage differs by "
            "more than this ratio."
        ),
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
    parser.add_argument(
        "--source-debug-checkpoints",
        help=(
            "Comma or space separated checkpoints where source-map debug summaries "
            "are added to JSON. Use 'none' to disable. Defaults to the selected scenario."
        ),
    )
    parser.add_argument(
        "--strict-source-state",
        action="store_true",
        help=(
            "Fail source-debug checkpoints on palette RAM, OAM, VRAM section, "
            "and stable register mismatches. Without this flag, source-debug "
            "remains diagnostic metadata for the tolerant visual oracle."
        ),
    )
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args()


def load_oracle_button_script(args: argparse.Namespace) -> ButtonScript | None:
    scenario = ORACLE_SCENARIOS[args.scenario]
    if args.no_button_script:
        if args.button_script_path is not None:
            raise ValueError("use either --no-button-script or --button-script-path")
        if args.button_script is not None:
            raise ValueError("use either --no-button-script or --button-script")
        return None
    if args.button_script is not None and args.button_script_path is not None:
        raise ValueError("use either --button-script or --button-script-path, not both")
    if args.button_script_path is not None:
        return load_button_script(args.button_script_path)
    if args.button_script is not None:
        return parse_button_script(args.button_script)
    if scenario.button_script:
        return parse_button_script(scenario.button_script)
    return None


def resolve_oracle_save_file(
    args: argparse.Namespace,
    scenario: OracleScenario,
) -> tuple[Path | None, str | None]:
    if args.no_save_file:
        if args.save_file is not None:
            raise ValueError("use either --no-save-file or --save-file")
        return None, None
    if args.save_file is not None:
        return args.save_file, "arg"
    if scenario.save_file is not None:
        return scenario.save_file, "scenario"
    return None, None


def _rtc_sidecar_path(save_file: Path) -> Path:
    return Path(f"{save_file}.rtc")


def _gbemu_rtc_state_from_sidecar(sidecar_bytes: bytes) -> dict[str, Any] | None:
    try:
        state = json.loads(sidecar_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or int(state.get("version", 0)) != 1:
        return None
    return state


def _gbemu_rtc_saved_at_from_sidecar(sidecar_bytes: bytes) -> float | None:
    state = _gbemu_rtc_state_from_sidecar(sidecar_bytes)
    if state is None:
        return None
    try:
        return float(state["saved_at"])
    except (KeyError, TypeError, ValueError):
        return None


def _deterministic_rtc_now_from_sidecar(sidecar_bytes: bytes) -> float | None:
    state = _gbemu_rtc_state_from_sidecar(sidecar_bytes)
    if state is None:
        return None
    saved_at = _gbemu_rtc_saved_at_from_sidecar(sidecar_bytes)
    if saved_at is not None:
        return saved_at
    return 0.0


def _gbemu_rtc_total_seconds(
    sidecar_bytes: bytes,
    *,
    now: float | None = None,
) -> int | None:
    state = _gbemu_rtc_state_from_sidecar(sidecar_bytes)
    if state is None:
        return None

    current_time = _deterministic_rtc_now_from_sidecar(sidecar_bytes) if now is None else now
    if current_time is None:
        return None
    total_seconds = (
        min(max(int(state["seconds"]), 0), 59)
        + min(max(int(state["minutes"]), 0), 59) * 60
        + min(max(int(state["hours"]), 0), 23) * 3600
        + min(max(int(state["days"]), 0), 0x1FF) * 86400
    )
    if not bool(state["halt"]):
        saved_at = float(state.get("saved_at", current_time))
        elapsed = int(current_time - saved_at)
        if elapsed > 0:
            total_seconds += elapsed
    return total_seconds


def _pyboy_rtc_bytes_from_gbemu_sidecar(
    sidecar_bytes: bytes,
    *,
    now: float | None = None,
    host_now: float | None = None,
) -> bytes | None:
    state = _gbemu_rtc_state_from_sidecar(sidecar_bytes)
    if state is None:
        return None

    total_seconds = _gbemu_rtc_total_seconds(sidecar_bytes, now=now)
    if total_seconds is None:
        return None

    # PyBoy stores RTC as a host-time epoch. When this helper is used for an
    # actual PyBoy run, callers pass host_now explicitly; otherwise the helper
    # remains deterministic and wall-clock-free for tests/conversion checks.
    current_time = (
        _deterministic_rtc_now_from_sidecar(sidecar_bytes)
        if host_now is None
        else host_now
    )
    if current_time is None:
        return None
    timezero = current_time - total_seconds
    halt = bool(state["halt"])
    return struct.pack("d", timezero) + bytes([int(halt), int(bool(state["carry"]))])


def load_pyboy_rtc_file(
    save_file: Path | None,
    *,
    now: float | None = None,
    host_now: float | None = None,
) -> io.BytesIO | None:
    if save_file is None:
        return None
    rtc_path = _rtc_sidecar_path(save_file)
    if not rtc_path.exists():
        return None
    sidecar_bytes = rtc_path.read_bytes()
    pyboy_bytes = _pyboy_rtc_bytes_from_gbemu_sidecar(
        sidecar_bytes,
        now=now,
        host_now=host_now,
    )
    return io.BytesIO(pyboy_bytes if pyboy_bytes is not None else sidecar_bytes)


def load_oracle_rtc_now(save_file: Path | None) -> float | None:
    if save_file is None:
        return None
    rtc_path = _rtc_sidecar_path(save_file)
    if rtc_path.exists():
        rtc_now = _deterministic_rtc_now_from_sidecar(rtc_path.read_bytes())
        if rtc_now is not None:
            return rtc_now
        raise ValueError(f"RTC sidecar is not a deterministic GBemu JSON fixture: {rtc_path}")
    raise ValueError(f"RTC sidecar is required for deterministic saved-game oracle: {rtc_path}")


def parse_source_debug_checkpoints(
    value: str | None,
    default_frames: tuple[int, ...] = DEFAULT_SOURCE_DEBUG_CHECKPOINTS,
) -> set[int]:
    if value is None:
        return set(default_frames)
    stripped = value.strip()
    if not stripped or stripped.lower() in {"none", "off", "false", "0"}:
        return set()
    frames = parse_checkpoint_frames(stripped)
    return set(frames)


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


def image_nonblack_metrics(image: Image.Image) -> dict[str, Any]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    data = rgb.tobytes()
    nonblack_pixels = 0
    left = width
    top = height
    right = -1
    bottom = -1
    color_counts: dict[tuple[int, int, int], int] = {}

    for pixel_index, index in enumerate(range(0, len(data), 3)):
        color = (data[index], data[index + 1], data[index + 2])
        color_counts[color] = color_counts.get(color, 0) + 1
        if color == (0, 0, 0):
            continue

        x = pixel_index % width
        y = pixel_index // width
        nonblack_pixels += 1
        left = min(left, x)
        top = min(top, y)
        right = max(right, x + 1)
        bottom = max(bottom, y + 1)

    total_pixels = width * height
    top_colors = sorted(
        color_counts.items(), key=lambda item: item[1], reverse=True
    )[:8]
    return {
        "nonblack_pixels": nonblack_pixels,
        "nonblack_ratio": nonblack_pixels / total_pixels,
        "nonblack_bbox": None
        if nonblack_pixels == 0
        else [left, top, right, bottom],
        "top_rgb_values": [
            [int(color[0]), int(color[1]), int(color[2]), int(count)]
            for color, count in top_colors
        ],
    }


def image_metrics(image: Image.Image) -> dict[str, Any]:
    rgb = image.convert("RGB")
    unique = sorted(image_unique_colors(rgb))
    return {
        "rgb_pixels": rgb.size[0] * rgb.size[1],
        "unique_rgb_colors": len(unique),
        "unique_rgb_values": unique,
        "size": list(rgb.size),
        **image_nonblack_metrics(rgb),
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
    left = gbemu_rgb.size[0]
    top = gbemu_rgb.size[1]
    right = -1
    bottom = -1
    gbemu_only_nonblack_pixels = 0
    pyboy_only_nonblack_pixels = 0
    both_nonblack_pixels = 0
    diff_values: list[tuple[int, int, int]] = []
    gbemu_data = gbemu_rgb.tobytes()
    pyboy_data = pyboy_rgb.tobytes()
    width, height = gbemu_rgb.size
    for pixel_index, index in enumerate(range(0, len(gbemu_data), 3)):
        gbemu_color = (
            gbemu_data[index],
            gbemu_data[index + 1],
            gbemu_data[index + 2],
        )
        pyboy_color = (
            pyboy_data[index],
            pyboy_data[index + 1],
            pyboy_data[index + 2],
        )
        gbemu_nonblack = gbemu_color != (0, 0, 0)
        pyboy_nonblack = pyboy_color != (0, 0, 0)
        if gbemu_nonblack and pyboy_nonblack:
            both_nonblack_pixels += 1
        elif gbemu_nonblack:
            gbemu_only_nonblack_pixels += 1
        elif pyboy_nonblack:
            pyboy_only_nonblack_pixels += 1

        red_delta = abs(gbemu_data[index] - pyboy_data[index])
        green_delta = abs(gbemu_data[index + 1] - pyboy_data[index + 1])
        blue_delta = abs(gbemu_data[index + 2] - pyboy_data[index + 2])
        pixel_delta = max(red_delta, green_delta, blue_delta)
        if pixel_delta:
            exact_diff_pixels += 1
            x = pixel_index % width
            y = pixel_index // width
            left = min(left, x)
            top = min(top, y)
            right = max(right, x + 1)
            bottom = max(bottom, y + 1)
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
    gbemu_nonblack_pixels = gbemu_only_nonblack_pixels + both_nonblack_pixels
    pyboy_nonblack_pixels = pyboy_only_nonblack_pixels + both_nonblack_pixels
    nonblack_denominator = max(gbemu_nonblack_pixels, pyboy_nonblack_pixels, 1)
    nonblack_delta_pixels = abs(gbemu_nonblack_pixels - pyboy_nonblack_pixels)
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
            "diff_bbox": None
            if exact_diff_pixels == 0
            else [left, top, right, bottom],
            "gbemu_only_nonblack_pixels": gbemu_only_nonblack_pixels,
            "pyboy_only_nonblack_pixels": pyboy_only_nonblack_pixels,
            "both_nonblack_pixels": both_nonblack_pixels,
            "gbemu_nonblack_pixels": gbemu_nonblack_pixels,
            "pyboy_nonblack_pixels": pyboy_nonblack_pixels,
            "nonblack_delta_pixels": nonblack_delta_pixels,
            "nonblack_delta_ratio": nonblack_delta_pixels / nonblack_denominator,
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
        or (checkpoint == checkpoint_frames[0] and checkpoint > 60)
        or checkpoint >= attribute_checkpoint_frame
    )


def _strict_section_failure(label: str, section_name: str, section: dict[str, Any]) -> str:
    first_diff = section.get("first_diff")
    detail = "" if first_diff is None else f"; first diff {first_diff}"
    return f"{label}: source state {section_name} differs{detail}"


def strict_source_state_failures(stage: dict[str, Any]) -> list[str]:
    label = f"crystal oracle frame {stage.get('checkpoint', '?')}"
    if not stage.get("strict_source_state_required"):
        return []

    source_debug = stage.get("source_debug")
    if source_debug is None:
        return [f"{label}: source-debug capture is required but missing"]

    state_compare = source_debug.get("state_compare", {})
    failures: list[str] = []
    for section_name, section in state_compare.get("palette_ram", {}).items():
        if not section.get("equal", True):
            failures.append(_strict_section_failure(label, section_name, section))

    oam = state_compare.get("oam", {})
    if not oam.get("equal", True):
        failures.append(_strict_section_failure(label, "oam", oam))

    for section_name, section in state_compare.get("vram_sections", {}).items():
        if not section.get("equal", True):
            failures.append(_strict_section_failure(label, section_name, section))

    for register, values in state_compare.get("register_values", {}).items():
        if register in {"FF41", "FF44"}:
            continue
        if not values.get("equal", True):
            failures.append(
                f"{label}: source state register {register} differs; "
                f"gbemu={values.get('gbemu')} pyboy={values.get('pyboy')}"
            )
    return failures


def evaluate_oracle_stage(
    stage: dict[str, Any],
    *,
    min_pyboy_unique_rgb_colors: int,
    require_pyboy_color_variety: bool,
    max_major_diff_ratio: float,
    max_nonblack_delta_ratio: float,
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
    if (
        int(stage.get("checkpoint", 0)) > 60
        and float(diff.get("nonblack_delta_ratio", 1.0)) > max_nonblack_delta_ratio
    ):
        failures.append(
            f"{label}: nonblack coverage delta ratio "
            f"{diff['nonblack_delta_ratio']:.4f} > {max_nonblack_delta_ratio:.4f}"
        )
    failures.extend(strict_source_state_failures(stage))
    return failures


def save_stage_images(
    *,
    output_dir: Path,
    checkpoint: int,
    gbemu_image: Image.Image,
    pyboy_image: Image.Image,
    diff_image: Image.Image,
    crop_bbox: list[int] | None,
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

    if crop_bbox is not None:
        left, top, right, bottom = crop_bbox
        crop_box = (
            max(0, left - 2),
            max(0, top - 2),
            min(gbemu_image.width, right + 2),
            min(gbemu_image.height, bottom + 2),
        )
        crop_paths = {
            "gbemu_crop_png": output_dir / f"{stem}-gbemu-crop.png",
            "pyboy_crop_png": output_dir / f"{stem}-pyboy-crop.png",
            "diff_crop_png": output_dir / f"{stem}-diff-crop.png",
        }
        gbemu_image.crop(crop_box).save(crop_paths["gbemu_crop_png"])
        pyboy_image.crop(crop_box).save(crop_paths["pyboy_crop_png"])
        diff_image.crop(crop_box).save(crop_paths["diff_crop_png"])
        paths.update(crop_paths)

    return {key: str(path) for key, path in paths.items()}


def cgb_rgb555_to_rgb(low: int, high: int) -> tuple[int, int, int]:
    value = (low & 0xFF) | ((high & 0x7F) << 8)
    return ((value & 0x1F) << 3, ((value >> 5) & 0x1F) << 3, ((value >> 10) & 0x1F) << 3)


def cgb_palette_rgb(palette_ram: list[int], palette_index: int, color_id: int) -> tuple[int, int, int]:
    offset = ((palette_index & 0x07) * 8) + ((color_id & 0x03) * 2)
    return cgb_rgb555_to_rgb(palette_ram[offset], palette_ram[offset + 1])


def tile_data_address(lcdc: int, tile_id: int, tile_y: int) -> int:
    if lcdc & LCDC_BG_WINDOW_TILE_DATA:
        return (tile_id & 0xFF) * 16 + tile_y * 2
    signed_id = tile_id - 0x100 if tile_id & 0x80 else tile_id
    return 0x1000 + signed_id * 16 + tile_y * 2


def _state_io(state: dict[str, Any], address: int) -> int:
    return int(state["io"][address])


def _classify_bg_window_pixel(state: dict[str, Any], x: int, y: int) -> dict[str, Any]:
    lcdc = _state_io(state, 0xFF40)
    if not lcdc & LCDC_ENABLE:
        return {
            "source": "off",
            "tile_id": None,
            "attr": 0,
            "palette": 0,
            "color_id": 0,
            "priority": False,
            "rgb": (0, 0, 0),
        }

    wx = _state_io(state, 0xFF4B) - 7
    wy = _state_io(state, 0xFF4A)
    window_active = bool(lcdc & LCDC_WINDOW_ENABLE) and y >= wy and _state_io(state, 0xFF4B) <= 166 and x >= max(0, wx)
    if window_active:
        source = "window"
        map_base = 0x1C00 if lcdc & LCDC_WINDOW_TILEMAP else 0x1800
        map_x = (x - wx) & 0xFF
        map_y = (y - wy) & 0xFF
    else:
        source = "background"
        map_base = 0x1C00 if lcdc & LCDC_BG_TILEMAP else 0x1800
        map_x = (x + _state_io(state, 0xFF43)) & 0xFF
        map_y = (y + _state_io(state, 0xFF42)) & 0xFF

    tile_map_index = map_base + (map_y // 8) * 32 + (map_x // 8)
    tile_id = state["vram0"][tile_map_index]
    attr = state["vram1"][tile_map_index]
    tile_x = map_x & 0x07
    tile_y = map_y & 0x07
    if attr & CGB_ATTR_X_FLIP:
        tile_x = 7 - tile_x
    if attr & CGB_ATTR_Y_FLIP:
        tile_y = 7 - tile_y

    address = tile_data_address(lcdc, tile_id, tile_y) & 0x1FFF
    bank = 1 if attr & CGB_ATTR_VRAM_BANK else 0
    vram = state[f"vram{bank}"]
    lo = vram[address]
    hi = vram[(address + 1) & 0x1FFF]
    bit = 7 - tile_x
    color_id = ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)
    palette = attr & CGB_ATTR_PALETTE_MASK
    return {
        "source": source,
        "tile_id": tile_id,
        "attr": attr,
        "palette": palette,
        "color_id": color_id,
        "priority": bool(attr & CGB_ATTR_PRIORITY),
        "tile_map_index": tile_map_index,
        "tile_bank": bank,
        "rgb": cgb_palette_rgb(state["bg_palette_ram"], palette, color_id),
    }


def _selected_sprites_for_pixel(state: dict[str, Any], y: int) -> list[tuple[int, int, int, int, int, int]]:
    lcdc = _state_io(state, 0xFF40)
    height = 16 if lcdc & LCDC_OBJ_SIZE else 8
    selected: list[tuple[int, int, int, int, int, int]] = []
    oam = state["oam"]
    for index in range(40):
        offset = index * 4
        sprite_y_raw = oam[offset]
        sprite_x_raw = oam[offset + 1]
        sprite_y = sprite_y_raw - 16
        sprite_x = sprite_x_raw - 8
        if sprite_y <= y < sprite_y + height:
            selected.append((sprite_x, index, sprite_y, oam[offset + 2], oam[offset + 3], sprite_x_raw))
            if len(selected) == 10:
                break
    opri = _state_io(state, 0xFF6C) & 0x01
    selected.sort(key=(lambda sprite: (sprite[0], sprite[1])) if opri else (lambda sprite: (sprite[1], 0)))
    return selected


def classify_cgb_pixel(state: dict[str, Any], x: int, y: int) -> dict[str, Any]:
    bg = _classify_bg_window_pixel(state, x, y)
    result = dict(bg)
    result["bg_source"] = bg["source"]
    result["hidden_obj"] = None
    lcdc = _state_io(state, 0xFF40)
    if not lcdc & LCDC_OBJ_ENABLE:
        return result

    height = 16 if lcdc & LCDC_OBJ_SIZE else 8
    for sprite_x, index, sprite_y, tile_id, attrs, _sprite_x_raw in _selected_sprites_for_pixel(state, y):
        if not (sprite_x <= x < sprite_x + 8):
            continue
        sprite_row = y - sprite_y
        if attrs & OBJ_Y_FLIP:
            sprite_row = height - 1 - sprite_row
        sprite_tile_id = tile_id
        if height == 16:
            sprite_tile_id = (tile_id & 0xFE) | ((sprite_row // 8) & 0x01)
        tile_y = sprite_row & 0x07
        tile_x = x - sprite_x
        if attrs & OBJ_X_FLIP:
            tile_x = 7 - tile_x
        address = ((sprite_tile_id & 0xFF) * 16 + tile_y * 2) & 0x1FFF
        bank = 1 if attrs & CGB_ATTR_VRAM_BANK else 0
        vram = state[f"vram{bank}"]
        lo = vram[address]
        hi = vram[(address + 1) & 0x1FFF]
        bit = 7 - tile_x
        color_id = ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)
        if color_id == 0:
            continue

        sprite = {
            "source": "obj",
            "sprite_index": index,
            "tile_id": sprite_tile_id,
            "attr": attrs,
            "palette": attrs & CGB_ATTR_PALETTE_MASK,
            "color_id": color_id,
            "priority": bool(attrs & OBJ_PRIORITY),
            "tile_bank": bank,
            "rgb": cgb_palette_rgb(state["obj_palette_ram"], attrs & CGB_ATTR_PALETTE_MASK, color_id),
        }
        bg_hides = (
            bg["color_id"] != 0
            and bool(lcdc & 0x01)
            and (bool(bg["priority"]) or bool(attrs & OBJ_PRIORITY))
        )
        if bg_hides:
            result["hidden_obj"] = sprite
            return result
        result.update(sprite)
        return result
    return result


def _bbox_to_json(bbox: list[int] | None) -> list[int] | None:
    return None if bbox is None else list(bbox)


def _extend_bbox(bbox: list[int] | None, x: int, y: int) -> list[int]:
    if bbox is None:
        return [x, y, x + 1, y + 1]
    bbox[0] = min(bbox[0], x)
    bbox[1] = min(bbox[1], y)
    bbox[2] = max(bbox[2], x + 1)
    bbox[3] = max(bbox[3], y + 1)
    return bbox


def source_model_summary(state: dict[str, Any], image: Image.Image) -> dict[str, Any]:
    rgb = image.convert("RGB")
    pixels = rgb.load()
    source_counts: Counter[str] = Counter()
    bg_source_counts: Counter[str] = Counter()
    modeled_nonzero_source_counts: Counter[str] = Counter()
    image_nonblack_source_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()
    tile_signatures: Counter[str] = Counter()
    modeled_bboxes: dict[str, list[int] | None] = {}
    image_bboxes: dict[str, list[int] | None] = {}

    for y in range(SCREEN_HEIGHT):
        for x in range(SCREEN_WIDTH):
            info = classify_cgb_pixel(state, x, y)
            source = str(info["source"])
            bg_source = str(info["bg_source"])
            source_counts[source] += 1
            bg_source_counts[bg_source] += 1
            priority_counts["priority" if info["priority"] else "normal"] += 1
            signature = (
                f"{source}:tile={info['tile_id']}:attr={info['attr']}:"
                f"pal={info['palette']}:color={info['color_id']}:prio={int(bool(info['priority']))}"
            )
            tile_signatures[signature] += 1
            if int(info["color_id"]) != 0:
                modeled_nonzero_source_counts[source] += 1
                modeled_bboxes[source] = _extend_bbox(modeled_bboxes.get(source), x, y)
            if pixels[x, y] != (0, 0, 0):
                image_nonblack_source_counts[source] += 1
                image_bboxes[source] = _extend_bbox(image_bboxes.get(source), x, y)

    return {
        "source_counts": dict(sorted(source_counts.items())),
        "bg_source_counts": dict(sorted(bg_source_counts.items())),
        "modeled_nonzero_source_counts": dict(sorted(modeled_nonzero_source_counts.items())),
        "image_nonblack_source_counts": dict(sorted(image_nonblack_source_counts.items())),
        "modeled_nonzero_bboxes": {key: _bbox_to_json(value) for key, value in sorted(modeled_bboxes.items())},
        "image_nonblack_bboxes": {key: _bbox_to_json(value) for key, value in sorted(image_bboxes.items())},
        "priority_counts": dict(sorted(priority_counts.items())),
        "top_tile_signatures": [
            {"signature": key, "pixels": count}
            for key, count in tile_signatures.most_common(16)
        ],
    }


def _first_diff(left: list[int], right: list[int], base: int) -> dict[str, int] | None:
    for index, (left_value, right_value) in enumerate(zip(left, right)):
        if left_value != right_value:
            return {"offset": base + index, "gbemu": left_value, "pyboy": right_value}
    return None


def _section_diff(gbemu: list[int], pyboy: list[int], start: int, end: int) -> dict[str, Any]:
    left = gbemu[start:end]
    right = pyboy[start:end]
    diff_count = sum(a != b for a, b in zip(left, right))
    return {
        "equal": diff_count == 0,
        "diff_count": diff_count,
        "gbemu_nonzero": sum(1 for value in left if value),
        "pyboy_nonzero": sum(1 for value in right if value),
        "first_diff": _first_diff(left, right, start),
    }


def compare_source_states(gbemu: dict[str, Any], pyboy: dict[str, Any]) -> dict[str, Any]:
    stable_registers = [0xFF40, 0xFF42, 0xFF43, 0xFF4A, 0xFF4B, 0xFF4F, 0xFF6C]
    timing_registers = [0xFF41, 0xFF44, 0xFF55]
    register_compare_masks = {
        0xFF4F: 0x01,
        0xFF6C: 0x01,
    }
    register_values = {
        f"{address:04X}": {
            "gbemu": gbemu["io"][address],
            "pyboy": pyboy["io"][address],
            "mask": register_compare_masks.get(address, 0xFF),
            "gbemu_compare": gbemu["io"][address] & register_compare_masks.get(address, 0xFF),
            "pyboy_compare": pyboy["io"][address] & register_compare_masks.get(address, 0xFF),
            "equal": (
                gbemu["io"][address] & register_compare_masks.get(address, 0xFF)
            )
            == (pyboy["io"][address] & register_compare_masks.get(address, 0xFF)),
        }
        for address in stable_registers + timing_registers
    }
    vram_sections = {
        "bank0_tiledata": _section_diff(gbemu["vram0"], pyboy["vram0"], 0x0000, 0x1800),
        "bank1_tiledata": _section_diff(gbemu["vram1"], pyboy["vram1"], 0x0000, 0x1800),
        "bank0_bg_map_9800": _section_diff(gbemu["vram0"], pyboy["vram0"], 0x1800, 0x1C00),
        "bank0_bg_map_9c00": _section_diff(gbemu["vram0"], pyboy["vram0"], 0x1C00, 0x2000),
        "bank1_attrs_9800": _section_diff(gbemu["vram1"], pyboy["vram1"], 0x1800, 0x1C00),
        "bank1_attrs_9c00": _section_diff(gbemu["vram1"], pyboy["vram1"], 0x1C00, 0x2000),
    }
    oam = _section_diff(gbemu["oam"], pyboy["oam"], 0, 0xA0)
    palette_ram = {
        "bg_palette_ram": _section_diff(gbemu["bg_palette_ram"], pyboy["bg_palette_ram"], 0, 64),
        "obj_palette_ram": _section_diff(gbemu["obj_palette_ram"], pyboy["obj_palette_ram"], 0, 64),
    }
    non_suspects = {
        "stable_lcdc_scroll_window_registers_equal": all(
            register_values[f"{address:04X}"]["equal"] for address in stable_registers
        ),
        "oam_equal": oam["equal"],
        "bg_palette_ram_equal": palette_ram["bg_palette_ram"]["equal"],
        "obj_palette_ram_equal": palette_ram["obj_palette_ram"]["equal"],
        "bank1_attribute_maps_equal": (
            vram_sections["bank1_attrs_9800"]["equal"]
            and vram_sections["bank1_attrs_9c00"]["equal"]
        ),
    }
    suspect = "unknown"
    if not palette_ram["bg_palette_ram"]["equal"] or not palette_ram["obj_palette_ram"]["equal"]:
        suspect = "palette"
    elif (
        not vram_sections["bank0_tiledata"]["equal"]
        or not vram_sections["bank0_bg_map_9800"]["equal"]
    ):
        suspect = "bank0_vram_tiledata_or_bg_map_timing"
    elif not oam["equal"]:
        suspect = "oam_timing"
    elif not register_values["FF44"]["equal"] or not register_values["FF41"]["equal"]:
        suspect = "lcd_timing_phase"
    return {
        "register_values": register_values,
        "non_suspects": non_suspects,
        "vram_sections": vram_sections,
        "palette_ram": palette_ram,
        "oam": oam,
        "suspect_class": suspect,
    }


def compact_pixel_info(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": info["source"],
        "bg_source": info["bg_source"],
        "tile_id": info["tile_id"],
        "attr": info["attr"],
        "palette": info["palette"],
        "color_id": info["color_id"],
        "priority": info["priority"],
        "tile_bank": info.get("tile_bank"),
        "hidden_obj": None
        if info.get("hidden_obj") is None
        else {
            "sprite_index": info["hidden_obj"]["sprite_index"],
            "tile_id": info["hidden_obj"]["tile_id"],
            "attr": info["hidden_obj"]["attr"],
            "palette": info["hidden_obj"]["palette"],
            "color_id": info["hidden_obj"]["color_id"],
            "priority": info["hidden_obj"]["priority"],
            "tile_bank": info["hidden_obj"]["tile_bank"],
        },
    }


def source_mismatch_samples(
    gbemu_state: dict[str, Any],
    pyboy_state: dict[str, Any],
    gbemu_image: Image.Image,
    pyboy_image: Image.Image,
) -> dict[str, Any]:
    gbemu_rgb = gbemu_image.convert("RGB")
    pyboy_rgb = pyboy_image.convert("RGB")
    gbemu_pixels = gbemu_rgb.load()
    pyboy_pixels = pyboy_rgb.load()
    samples: list[dict[str, Any]] = []
    pyboy_only_sources: Counter[str] = Counter()
    source_pair_counts: Counter[str] = Counter()
    for y in range(SCREEN_HEIGHT):
        for x in range(SCREEN_WIDTH):
            gbemu_color = gbemu_pixels[x, y]
            pyboy_color = pyboy_pixels[x, y]
            gbemu_info = classify_cgb_pixel(gbemu_state, x, y)
            pyboy_info = classify_cgb_pixel(pyboy_state, x, y)
            pair_key = f"{gbemu_info['source']}->{pyboy_info['source']}"
            source_pair_counts[pair_key] += 1
            if gbemu_color == (0, 0, 0) and pyboy_color != (0, 0, 0):
                pyboy_only_sources[str(pyboy_info["source"])] += 1
                if len(samples) < SOURCE_DEBUG_SAMPLE_LIMIT:
                    samples.append(
                        {
                            "x": x,
                            "y": y,
                            "gbemu_rgb": list(gbemu_color),
                            "pyboy_rgb": list(pyboy_color),
                            "gbemu": compact_pixel_info(gbemu_info),
                            "pyboy": compact_pixel_info(pyboy_info),
                        }
                    )
    return {
        "pyboy_only_nonblack_source_counts": dict(sorted(pyboy_only_sources.items())),
        "source_pair_counts": dict(source_pair_counts.most_common(12)),
        "pyboy_only_nonblack_samples": samples,
    }


def classify_visible_mismatch(
    gbemu_image: Image.Image,
    pyboy_image: Image.Image,
    mismatch: dict[str, Any],
) -> str:
    gbemu_rgb = gbemu_image.convert("RGB")
    pyboy_rgb = pyboy_image.convert("RGB")
    if gbemu_rgb.tobytes() == pyboy_rgb.tobytes():
        return "none"

    pyboy_only_sources = mismatch.get("pyboy_only_nonblack_source_counts", {})
    bg_window_only = int(pyboy_only_sources.get("background", 0)) + int(
        pyboy_only_sources.get("window", 0)
    )
    obj_only = int(pyboy_only_sources.get("obj", 0))
    if bg_window_only and bg_window_only >= obj_only:
        return "bg_window_coverage"
    if obj_only:
        return "obj_coverage"
    return "color_priority_or_timing"


def classify_stage_mismatch(stage: dict[str, Any]) -> str:
    diff = stage.get("diff", {})
    if int(diff.get("diff_pixels", 1)) == 0:
        return "none"

    source_debug = stage.get("source_debug")
    if not source_debug:
        return "unclassified_no_source_debug"

    state_compare = source_debug.get("state_compare", {})
    non_suspects = state_compare.get("non_suspects", {})
    vram_sections = state_compare.get("vram_sections", {})
    registers = state_compare.get("register_values", {})
    visible = source_debug.get("visible_mismatch_class")

    if not non_suspects.get("bg_palette_ram_equal", True) or not non_suspects.get(
        "obj_palette_ram_equal",
        True,
    ):
        return "palette"
    if not non_suspects.get("bank1_attribute_maps_equal", True) or not vram_sections.get(
        "bank1_tiledata",
        {},
    ).get("equal", True):
        return "cgb_attr"
    if visible == "obj_coverage" or not non_suspects.get("oam_equal", True):
        return "obj_priority"
    if not registers.get("FF55", {}).get("equal", True):
        return "hdma_timing"
    if not non_suspects.get("stable_lcdc_scroll_window_registers_equal", True):
        return "window_timing"
    if visible == "bg_window_coverage" or not vram_sections.get(
        "bank0_bg_map_9800",
        {},
    ).get("equal", True) or not vram_sections.get("bank0_bg_map_9c00", {}).get("equal", True):
        return "bg_window_tilemap"
    if not vram_sections.get("bank0_tiledata", {}).get("equal", True):
        return "vram_bank"
    return "fifo_timing"


def build_source_debug(
    *,
    gbemu_state: dict[str, Any],
    pyboy_state: dict[str, Any],
    gbemu_image: Image.Image,
    pyboy_image: Image.Image,
) -> dict[str, Any]:
    state_compare = compare_source_states(gbemu_state, pyboy_state)
    mismatch = source_mismatch_samples(
        gbemu_state,
        pyboy_state,
        gbemu_image,
        pyboy_image,
    )
    return {
        "visible_mismatch_class": classify_visible_mismatch(
            gbemu_image,
            pyboy_image,
            mismatch,
        ),
        "source_state_class": state_compare["suspect_class"],
        "state_compare": state_compare,
        "gbemu_source_model": source_model_summary(gbemu_state, gbemu_image),
        "pyboy_source_model": source_model_summary(pyboy_state, pyboy_image),
        "mismatch_samples": mismatch,
    }


def capture_gbemu_cgb_state(emulator: Emulator) -> dict[str, Any]:
    bus = emulator.bus
    io_addresses = [
        0xFF40,
        0xFF41,
        0xFF42,
        0xFF43,
        0xFF44,
        0xFF4A,
        0xFF4B,
        0xFF4F,
        0xFF55,
        0xFF6C,
    ]
    return {
        "io": {address: bus.read8(address) for address in io_addresses},
        "vram0": list(bus.vram[0x0000:0x2000]),
        "vram1": list(bus.vram[0x2000:0x4000]),
        "oam": list(bus.oam),
        "bg_palette_ram": list(bus.bg_palette_ram),
        "obj_palette_ram": list(bus.obj_palette_ram),
    }


def _read_pyboy_vram_bank(pyboy: Any, bank: int) -> list[int]:
    data = pyboy.memory[bank, 0x8000:0x9FFF]
    data.append(pyboy.memory[bank, 0x9FFF])
    return data


def _read_pyboy_palette_ram(pyboy: Any, index_address: int, data_address: int) -> list[int]:
    previous_index = pyboy.memory[index_address]
    values: list[int] = []
    for index in range(64):
        pyboy.memory[index_address] = index
        values.append(pyboy.memory[data_address])
    pyboy.memory[index_address] = previous_index
    return values


def capture_pyboy_cgb_state(pyboy: Any) -> dict[str, Any]:
    io_addresses = [
        0xFF40,
        0xFF41,
        0xFF42,
        0xFF43,
        0xFF44,
        0xFF4A,
        0xFF4B,
        0xFF4F,
        0xFF55,
        0xFF6C,
    ]
    return {
        "io": {address: pyboy.memory[address] for address in io_addresses},
        "vram0": _read_pyboy_vram_bank(pyboy, 0),
        "vram1": _read_pyboy_vram_bank(pyboy, 1),
        "oam": pyboy.memory[0xFE00:0xFEA0],
        "bg_palette_ram": _read_pyboy_palette_ram(pyboy, 0xFF68, 0xFF69),
        "obj_palette_ram": _read_pyboy_palette_ram(pyboy, 0xFF6A, 0xFF6B),
    }


def capture_pyboy_cgb_state_preserving(pyboy: Any) -> dict[str, Any]:
    state = io.BytesIO()
    pyboy.save_state(state)
    captured = capture_pyboy_cgb_state(pyboy)
    state.seek(0)
    pyboy.load_state(state)
    return captured


def pyboy_run_kwargs(save_file: Path | None, *, rtc_now: float | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "window": "null",
        "sound_emulated": False,
        "cgb": True,
    }
    if save_file is not None:
        kwargs["ram_file"] = io.BytesIO(save_file.read_bytes())
        # The emulated RTC value is pinned by rtc_now. PyBoy's file format is a
        # host-time epoch, so this anchor compensates for PyBoy's own clock read.
        rtc_file = load_pyboy_rtc_file(save_file, now=rtc_now, host_now=time.time())
        if rtc_file is not None:
            kwargs["rtc_file"] = rtc_file
    return kwargs


def force_pyboy_cgb_post_boot(pyboy: Any) -> None:
    """Match GBemu's CGB no-boot startup before PyBoy starts ticking frames."""

    pyboy.memory[0xFF50] = 0x01
    registers = pyboy.register_file
    registers.A = 0x11
    registers.F = 0x80
    registers.B = 0x00
    registers.C = 0x00
    registers.D = 0xFF
    registers.E = 0x56
    registers.HL = 0x000D
    registers.SP = 0xFFFE
    registers.PC = 0x0100

    pyboy.memory[0xFF04] = 0x00
    pyboy.memory[0xFFFF] = 0x00
    for address, value in PYBOY_CGB_POST_BOOT_IO_DEFAULTS.items():
        pyboy.memory[address] = value


def run_gbemu_stages(
    *,
    rom: Path,
    checkpoint_frames: list[int],
    button_script: ButtonScript | None,
    attribute_checkpoint_frame: int,
    min_unique_rgb_colors: int,
    source_debug_checkpoints: set[int],
    save_file: Path | None,
    rtc_now: float | None,
    frame_clock: Literal["cpu", "ppu"],
    input_clock: Literal["wall", "ppu"],
) -> list[dict[str, Any]]:
    if frame_clock not in {"cpu", "ppu"}:
        raise ValueError(f"unknown GBemu frame clock: {frame_clock!r}")
    if input_clock not in {"wall", "ppu"}:
        raise ValueError(f"unknown GBemu input clock: {input_clock!r}")
    cartridge = (
        Cartridge(rom.read_bytes(), rom, rtc_time_provider=lambda: rtc_now)
        if rtc_now is not None
        else Cartridge.from_file(rom)
    )
    emulator = Emulator(cartridge, serial_sink=lambda _: None, mode="cgb")
    if save_file is not None:
        emulator.load_save_file(save_file)
    initial_key1 = emulator.bus.read8(0xFF4D)
    stages: list[dict[str, Any]] = []
    wall_frame = 0
    for checkpoint in checkpoint_frames:
        while wall_frame < checkpoint:
            if button_script is not None:
                input_frame = (
                    emulator.bus.ppu.frame_count if input_clock == "ppu" else wall_frame
                )
                emulator.set_buttons(button_script.buttons_for_frame(input_frame, set()))
            if frame_clock == "cpu":
                target_cycles = (wall_frame + 1) * DOTS_PER_FRAME
                emulator.cpu.run(
                    stop_condition=lambda target_cycles=target_cycles: (
                        emulator.cpu.cycles >= target_cycles
                    )
                )
            else:
                emulator.run(max_frames=1)
            wall_frame += 1
        metrics = collect_crystal_stage_metrics(
            emulator,
            cartridge,
            checkpoint=checkpoint,
            initial_key1=initial_key1,
            frame_output_dir=None,
        )
        metrics["oracle_wall_frame"] = checkpoint
        metrics["oracle_gbemu_frame_clock"] = frame_clock
        metrics["oracle_gbemu_input_clock"] = input_clock
        metrics["oracle_cpu_cycle_target"] = (
            checkpoint * DOTS_PER_FRAME if frame_clock == "cpu" else None
        )
        metrics["oracle_ppu_frame_target"] = checkpoint if frame_clock == "ppu" else None
        metrics["oracle_actual_cpu_cycles"] = emulator.cpu.cycles
        metrics["oracle_actual_ppu_frame"] = emulator.bus.ppu.frame_count
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
        frame_count_failure_prefix = f"crystal frame {checkpoint}: reached "
        failures = [
            failure
            for failure in failures
            if not failure.startswith(frame_count_failure_prefix)
        ]
        stages.append(
            {
                "checkpoint": checkpoint,
                "metrics": metrics,
                "failures": failures,
                "image": framebuffer_to_rgb_image(emulator.bus.ppu.framebuffer),
                "source_state": capture_gbemu_cgb_state(emulator)
                if checkpoint in source_debug_checkpoints
                else None,
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
    source_debug_checkpoints: set[int],
    save_file: Path | None,
    rtc_now: float | None,
) -> dict[int, dict[str, Any]]:
    from pyboy import PyBoy

    pyboy = None
    checkpoints = set(checkpoint_frames)
    captures: dict[int, dict[str, Any]] = {}
    pressed: set[str] = set()
    try:
        pyboy = PyBoy(str(rom), **pyboy_run_kwargs(save_file, rtc_now=rtc_now))
        force_pyboy_cgb_post_boot(pyboy)
        pyboy.set_emulation_speed(0)
        for frame in range(max(checkpoint_frames)):
            target = (
                button_script.buttons_for_frame(frame, set())
                if button_script is not None
                else set()
            )
            pressed = apply_pyboy_buttons(pyboy, pressed, target)
            rendered_frame = frame + 1
            pyboy.tick(1, render=True)
            if rendered_frame in checkpoints:
                captures[rendered_frame] = {
                    "image": pyboy.screen.image.convert("RGB"),
                    "cycles": pyboy._cycles(),
                    "source_state": None,
                }
    finally:
        if pyboy is not None:
            pyboy.stop(save=False)
    if source_debug_checkpoints:
        for checkpoint in sorted(checkpoints & source_debug_checkpoints):
            if checkpoint in captures:
                captures[checkpoint]["source_state"] = run_pyboy_source_state_checkpoint(
                    rom=rom,
                    checkpoint=checkpoint,
                    button_script=button_script,
                    save_file=save_file,
                    rtc_now=rtc_now,
                )
    return captures


def run_pyboy_source_state_checkpoint(
    *,
    rom: Path,
    checkpoint: int,
    button_script: ButtonScript | None,
    save_file: Path | None,
    rtc_now: float | None,
) -> dict[str, Any]:
    from pyboy import PyBoy

    pyboy = None
    pressed: set[str] = set()
    try:
        pyboy = PyBoy(str(rom), **pyboy_run_kwargs(save_file, rtc_now=rtc_now))
        force_pyboy_cgb_post_boot(pyboy)
        pyboy.set_emulation_speed(0)
        for frame in range(checkpoint):
            target = (
                button_script.buttons_for_frame(frame, set())
                if button_script is not None
                else set()
            )
            pressed = apply_pyboy_buttons(pyboy, pressed, target)
            pyboy.tick(1, render=True)
        return capture_pyboy_cgb_state(pyboy)
    finally:
        if pyboy is not None:
            pyboy.stop(save=False)


def run_oracle(args: argparse.Namespace) -> dict[str, Any]:
    scenario = ORACLE_SCENARIOS[args.scenario]
    checkpoint_frames = (
        list(scenario.checkpoint_frames)
        if args.checkpoint_frames is None
        else parse_checkpoint_frames(args.checkpoint_frames)
    )
    attribute_checkpoint_frame = (
        scenario.attribute_checkpoint_frame
        if args.attribute_checkpoint_frame is None
        else args.attribute_checkpoint_frame
    )
    button_script = load_oracle_button_script(args)
    source_debug_checkpoints = parse_source_debug_checkpoints(
        args.source_debug_checkpoints,
        default_frames=scenario.source_debug_checkpoints,
    )
    save_file, save_file_source = resolve_oracle_save_file(args, scenario)
    if save_file is not None and not save_file.exists():
        raise ValueError(f"save file not found: {save_file}")
    rtc_file = _rtc_sidecar_path(save_file) if save_file is not None else None
    rtc_now = load_oracle_rtc_now(save_file)

    gbemu_stages = run_gbemu_stages(
        rom=args.rom,
        checkpoint_frames=checkpoint_frames,
        button_script=button_script,
        attribute_checkpoint_frame=attribute_checkpoint_frame,
        min_unique_rgb_colors=args.min_unique_rgb_colors,
        source_debug_checkpoints=source_debug_checkpoints,
        save_file=save_file,
        rtc_now=rtc_now,
        frame_clock=scenario.gbemu_frame_clock,
        input_clock=scenario.gbemu_input_clock,
    )
    pyboy_images = run_pyboy_stages(
        rom=args.rom,
        checkpoint_frames=checkpoint_frames,
        button_script=button_script,
        source_debug_checkpoints=source_debug_checkpoints,
        save_file=save_file,
        rtc_now=rtc_now,
    )

    failures: list[str] = []
    stages: list[dict[str, Any]] = []
    for gbemu_stage in gbemu_stages:
        checkpoint = gbemu_stage["checkpoint"]
        if checkpoint not in pyboy_images:
            failures.append(f"crystal oracle frame {checkpoint}: missing PyBoy capture")
            continue

        gbemu_image = gbemu_stage["image"]
        pyboy_capture = pyboy_images[checkpoint]
        pyboy_image = pyboy_capture["image"]
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
            crop_bbox=diff_metrics["diff_bbox"],
        )
        require_pyboy_color_variety = stage_requires_color_variety(
            checkpoint,
            checkpoint_frames,
            attribute_checkpoint_frame=attribute_checkpoint_frame,
        )
        stage = {
            "checkpoint": checkpoint,
            "scenario_stage": scenario.stage_labels.get(checkpoint),
            "stage": gbemu_stage["metrics"]["stage"],
            "gbemu": gbemu_stage["metrics"],
            "gbemu_image": image_metrics(gbemu_image),
            "gbemu_failures": gbemu_stage["failures"],
            "pyboy": image_metrics(pyboy_image),
            "pyboy_cycles": pyboy_capture["cycles"],
            "diff": diff_metrics,
            "images": image_paths,
            "source_debug_required": checkpoint in source_debug_checkpoints,
            "strict_source_state_required": (
                args.strict_source_state and checkpoint in source_debug_checkpoints
            ),
        }
        if checkpoint in source_debug_checkpoints:
            gbemu_source_state = gbemu_stage.get("source_state")
            pyboy_source_state = pyboy_capture.get("source_state")
            if gbemu_source_state is not None and pyboy_source_state is not None:
                stage["source_debug"] = build_source_debug(
                    gbemu_state=gbemu_source_state,
                    pyboy_state=pyboy_source_state,
                    gbemu_image=gbemu_image,
                    pyboy_image=pyboy_image,
                )
        stage["mismatch_class"] = classify_stage_mismatch(stage)
        stage_failures = evaluate_oracle_stage(
            stage,
            min_pyboy_unique_rgb_colors=args.min_pyboy_unique_rgb_colors,
            require_pyboy_color_variety=require_pyboy_color_variety,
            max_major_diff_ratio=args.max_major_diff_ratio,
            max_nonblack_delta_ratio=args.max_nonblack_delta_ratio,
        )
        stage["failures"] = stage_failures
        stage["status"] = "pass" if not stage_failures else "fail"
        failures.extend(stage_failures)
        stages.append(stage)

    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "scenario": scenario.name,
        "scenario_description": scenario.description,
        "rom": str(args.rom),
        "save_file": None if save_file is None else str(save_file),
        "save_file_source": save_file_source,
        "rtc_file": None if rtc_file is None or not rtc_file.exists() else str(rtc_file),
        "rtc_now": rtc_now,
        "checkpoint_frames": checkpoint_frames,
        "source_debug_checkpoints": sorted(source_debug_checkpoints),
        "strict_source_state": args.strict_source_state,
        "button_script": (
            None
            if button_script is None
            else (
                str(args.button_script_path)
                if args.button_script_path is not None
                else args.button_script
                if args.button_script is not None
                else scenario.button_script
            )
        ),
        "button_script_source": (
            None
            if button_script is None
            else "path"
            if args.button_script_path is not None
            else "inline"
            if args.button_script is not None
            else "scenario"
        ),
        "button_script_final_frame": None if button_script is None else button_script.final_frame,
        "gbemu_frame_clock": scenario.gbemu_frame_clock,
        "gbemu_input_clock": scenario.gbemu_input_clock,
        "pyboy_boot_mode": "forced_cgb_post_boot",
        "thresholds": {
            "major_delta_threshold": args.major_delta_threshold,
            "max_major_diff_ratio": args.max_major_diff_ratio,
            "max_nonblack_delta_ratio": args.max_nonblack_delta_ratio,
            "min_unique_rgb_colors": args.min_unique_rgb_colors,
            "min_pyboy_unique_rgb_colors": args.min_pyboy_unique_rgb_colors,
            "attribute_checkpoint_frame": attribute_checkpoint_frame,
        },
        "stages": stages,
    }


def main() -> int:
    args = parse_args()
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    if args.attribute_checkpoint_frame is not None and args.attribute_checkpoint_frame < 1:
        raise SystemExit("--attribute-checkpoint-frame must be positive")
    if args.min_unique_rgb_colors < 1:
        raise SystemExit("--min-unique-rgb-colors must be positive")
    if args.min_pyboy_unique_rgb_colors < 1:
        raise SystemExit("--min-pyboy-unique-rgb-colors must be positive")
    if not (0 <= args.major_delta_threshold <= 255):
        raise SystemExit("--major-delta-threshold must be between 0 and 255")
    if not (0 <= args.max_major_diff_ratio <= 1):
        raise SystemExit("--max-major-diff-ratio must be between 0 and 1")
    if not (0 <= args.max_nonblack_delta_ratio <= 1):
        raise SystemExit("--max-nonblack-delta-ratio must be between 0 and 1")

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
