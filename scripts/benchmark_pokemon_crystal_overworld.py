from __future__ import annotations

import argparse
from collections import Counter
import cProfile
import json
import pstats
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from button_script import ButtonScript, parse_button_script  # noqa: E402
from cartridge import Cartridge  # noqa: E402
from emulator import Emulator  # noqa: E402
from ppu import DOTS_PER_LINE, LINES_PER_FRAME  # noqa: E402
from scripts.verify_crystal_cgb_oracle import (  # noqa: E402
    CRYSTAL_OVERWORLD_BUTTON_SCRIPT,
    DEFAULT_CRYSTAL_SAVE_FILE,
    load_oracle_rtc_now,
)


DEFAULT_ROM = ROOT / "roms" / "crystal.gbc"
DEFAULT_SAVE_FILE = DEFAULT_CRYSTAL_SAVE_FILE
DEFAULT_BUTTON_SCRIPT = CRYSTAL_OVERWORLD_BUTTON_SCRIPT
DEFAULT_WARMUP_FRAMES = 5400
DEFAULT_PROFILE_FRAMES = 600
DEFAULT_WINDOW_SIZE = 60
DOTS_PER_FRAME = DOTS_PER_LINE * LINES_PER_FRAME


@dataclass
class ProfileTotals:
    frame_times: list[float]
    audio_times: list[float]
    frame_instructions: list[int]
    frame_cycles: list[int]
    cpu_halt_cycles: int = 0
    cpu_halt_batches: int = 0
    cpu_interrupts: int = 0
    bus_slow_cycles: int = 0
    bus_dma_cycles: int = 0
    bus_dma_starts: int = 0
    bus_timer_overflows: int = 0
    vram_dma_gdma_blocks: int = 0
    vram_dma_hdma_blocks: int = 0
    vram_dma_bytes: int = 0
    apu_seconds: float = 0.0
    apu_samples: int = 0
    apu_dropped_samples: int = 0
    apu_reg_writes: int = 0
    apu_triggers: int = 0
    apu_disables: int = 0
    ppu_mode3_lines: int = 0
    ppu_rendered_lines: int = 0
    ppu_segments: int = 0
    ppu_sprite_lines: int = 0
    ppu_selected_sprites: int = 0
    ppu_max_sprites: int = 0
    ppu_obj_dots: int = 0
    ppu_window_dots: int = 0
    ppu_bg_fast_pixels: int = 0
    ppu_bg_slow_pixels: int = 0
    ppu_window_fast_pixels: int = 0
    ppu_window_slow_pixels: int = 0
    ppu_sprite_pixels: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile the Pokemon Crystal saved-game CGB overworld scene reproducibly."
    )
    parser.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    parser.add_argument("--save-file", type=Path, default=DEFAULT_SAVE_FILE)
    parser.add_argument(
        "--button-script",
        default=DEFAULT_BUTTON_SCRIPT,
        help="Wall-frame based script that continues the saved game and moves through overworld activity.",
    )
    parser.add_argument("--warmup-frames", type=int, default=DEFAULT_WARMUP_FRAMES)
    parser.add_argument("--profile-frames", type=int, default=DEFAULT_PROFILE_FRAMES)
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument("--min-fps", type=float, help="Fail if measured emulation FPS is lower.")
    parser.add_argument(
        "--audio-output",
        action="store_true",
        help="Enable APU sample generation during measured frames.",
    )
    parser.add_argument(
        "--cprofile",
        type=Path,
        help="Write cProfile stats for the measured Crystal overworld frames.",
    )
    parser.add_argument(
        "--cprofile-text",
        type=Path,
        help="Write a human-readable cProfile summary.",
    )
    parser.add_argument(
        "--trace-pc-frames",
        type=int,
        default=0,
        help="Run this many measured wall frames with trace enabled and print hot PC counts.",
    )
    parser.add_argument("--trace-pc-top", type=int, default=20)
    return parser.parse_args()


def configure_profiles(emulator: Emulator, enabled: bool) -> None:
    emulator.cpu.profile_enabled = enabled
    emulator.bus.profile_enabled = enabled
    emulator.bus.ppu.profile_enabled = enabled
    emulator.bus.apu.profile_enabled = enabled


def build_emulator(rom: Path, save_file: Path) -> tuple[Emulator, float]:
    rtc_now = load_oracle_rtc_now(save_file)
    cartridge = Cartridge(rom.read_bytes(), rom, rtc_time_provider=lambda: rtc_now)
    emulator = Emulator(cartridge, serial_sink=lambda _char: None, mode="cgb")
    emulator.load_save_file(save_file)
    return emulator, rtc_now


def apply_scripted_buttons(
    emulator: Emulator,
    button_script: ButtonScript,
    wall_frame: int,
) -> None:
    emulator.set_buttons(button_script.buttons_for_frame(wall_frame, set()))


def run_wall_frame(
    emulator: Emulator,
    *,
    wall_frame: int,
    button_script: ButtonScript,
    trace: bool = False,
    trace_sink=None,
) -> None:
    apply_scripted_buttons(emulator, button_script, wall_frame)
    target_cycles = (wall_frame + 1) * DOTS_PER_FRAME
    emulator.cpu.run(
        trace=trace,
        trace_sink=trace_sink,
        stop_condition=lambda target_cycles=target_cycles: emulator.cpu.cycles >= target_cycles,
    )


def run_wall_frames(
    emulator: Emulator,
    *,
    start_wall_frame: int,
    frames: int,
    button_script: ButtonScript,
) -> int:
    wall_frame = start_wall_frame
    for _ in range(frames):
        run_wall_frame(emulator, wall_frame=wall_frame, button_script=button_script)
        wall_frame += 1
    return wall_frame


def consume_profiles(emulator: Emulator, totals: ProfileTotals) -> None:
    cpu_profile = emulator.cpu.consume_profile()
    bus = emulator.bus
    bus_profile = bus.consume_profile()
    ppu_profile = bus.ppu.consume_profile()
    apu_profile = bus.apu.consume_profile()

    totals.cpu_interrupts += cpu_profile.interrupt_entries
    totals.cpu_halt_cycles += cpu_profile.halt_idle_cycles
    totals.cpu_halt_batches += cpu_profile.halt_idle_batches
    totals.bus_slow_cycles += bus_profile.slow_system_counter_cycles
    totals.bus_dma_cycles += bus_profile.oam_dma_cycles
    totals.bus_dma_starts += bus_profile.oam_dma_starts
    totals.bus_timer_overflows += bus_profile.timer_overflows
    totals.vram_dma_gdma_blocks += bus.vram_dma_gdma_blocks
    totals.vram_dma_hdma_blocks += bus.vram_dma_hdma_blocks
    totals.vram_dma_bytes += bus.vram_dma_bytes
    totals.apu_seconds += apu_profile.tick_seconds
    totals.apu_samples += apu_profile.generated_samples
    totals.apu_dropped_samples += apu_profile.dropped_samples
    totals.apu_reg_writes += apu_profile.register_writes
    totals.apu_triggers += apu_profile.channel_triggers
    totals.apu_disables += apu_profile.channel_disables
    totals.ppu_mode3_lines += ppu_profile.mode3_lines
    totals.ppu_rendered_lines += ppu_profile.rendered_lines
    totals.ppu_segments += ppu_profile.render_segments
    totals.ppu_sprite_lines += ppu_profile.sprite_lines
    totals.ppu_selected_sprites += ppu_profile.selected_sprites
    totals.ppu_max_sprites = max(totals.ppu_max_sprites, ppu_profile.max_sprites_per_line)
    totals.ppu_obj_dots += ppu_profile.obj_penalty_dots
    totals.ppu_window_dots += ppu_profile.window_penalty_dots
    totals.ppu_bg_fast_pixels += ppu_profile.bg_fast_pixels
    totals.ppu_bg_slow_pixels += ppu_profile.bg_slow_pixels
    totals.ppu_window_fast_pixels += ppu_profile.window_fast_pixels
    totals.ppu_window_slow_pixels += ppu_profile.window_slow_pixels
    totals.ppu_sprite_pixels += ppu_profile.sprite_pixels
    bus.vram_dma_gdma_blocks = 0
    bus.vram_dma_hdma_blocks = 0
    bus.vram_dma_bytes = 0


def metric_range(values: list[float], scale: float = 1.0) -> str:
    if not values:
        return "0.00-0.00"
    return f"{min(values) * scale:.2f}-{max(values) * scale:.2f}"


def int_range(values: list[int]) -> str:
    if not values:
        return "0-0"
    return f"{min(values)}-{max(values)}"


def summarize_window(
    *,
    index: int,
    window_size: int,
    totals: ProfileTotals,
    start: int,
    end: int,
) -> dict[str, Any]:
    frame_times = totals.frame_times[start:end]
    audio_times = totals.audio_times[start:end]
    frame_instructions = totals.frame_instructions[start:end]
    frame_cycles = totals.frame_cycles[start:end]
    elapsed = sum(frame_times)
    active_elapsed = elapsed + sum(audio_times)
    frames = len(frame_times)
    return {
        "index": index,
        "frames": frames,
        "window_size": window_size,
        "run_ms": elapsed / frames * 1000 if frames else 0.0,
        "draw_ms": 0.0,
        "audio_ms": sum(audio_times) / frames * 1000 if frames else 0.0,
        "active_ms": active_elapsed / frames * 1000 if frames else 0.0,
        "run_fps": frames / elapsed if elapsed > 0 else 0.0,
        "active_fps": frames / active_elapsed if active_elapsed > 0 else 0.0,
        "frame_ms_range": metric_range(frame_times, 1000),
        "cpu_instr": sum(frame_instructions),
        "cpu_frame_instr": int_range(frame_instructions),
        "cpu_cycles": sum(frame_cycles),
    }


def print_window_profile(scenario: str, window: dict[str, Any]) -> None:
    print(
        "pokemon-crystal-overworld-window",
        f"scenario={scenario}",
        f"index={window['index']}",
        f"frames={window['frames']}",
        f"window_size={window['window_size']}",
        f"run_ms={window['run_ms']:.2f}",
        f"draw_ms={window['draw_ms']:.2f}",
        f"audio_ms={window['audio_ms']:.2f}",
        f"active_ms={window['active_ms']:.2f}",
        f"run_fps={window['run_fps']:.2f}",
        f"active_fps={window['active_fps']:.2f}",
        f"frame_ms_range={window['frame_ms_range']}",
        f"cpu_instr={window['cpu_instr']}",
        f"cpu_frame_instr={window['cpu_frame_instr']}",
        f"cpu_cycles={window['cpu_cycles']}",
        flush=True,
    )


def rtc_halted(save_file: Path) -> bool:
    rtc_path = Path(f"{save_file}.rtc")
    try:
        state = json.loads(rtc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(state.get("halt"))


def main() -> int:
    args = parse_args()
    if args.warmup_frames < 0:
        raise SystemExit("--warmup-frames must be non-negative")
    if args.profile_frames < 1:
        raise SystemExit("--profile-frames must be positive")
    if args.window_size < 1:
        raise SystemExit("--window-size must be positive")
    if args.trace_pc_frames < 0:
        raise SystemExit("--trace-pc-frames must be non-negative")
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    if not args.save_file.exists():
        raise SystemExit(f"save file not found: {args.save_file}")

    button_script = parse_button_script(args.button_script)
    emulator, rtc_now = build_emulator(args.rom, args.save_file)
    wall_frame = 0
    wall_frame = run_wall_frames(
        emulator,
        start_wall_frame=wall_frame,
        frames=args.warmup_frames,
        button_script=button_script,
    )
    emulator.bus.apu.set_output_enabled(args.audio_output)

    if args.trace_pc_frames:
        trace_counts: Counter[int] = Counter()

        def count_pc(line: str) -> None:
            try:
                trace_counts[int(line[:4], 16)] += 1
            except ValueError:
                return

        for _ in range(args.trace_pc_frames):
            run_wall_frame(
                emulator,
                wall_frame=wall_frame,
                button_script=button_script,
                trace=True,
                trace_sink=count_pc,
            )
            wall_frame += 1
        hot_pcs = " ".join(
            f"{pc:04X}:{count}" for pc, count in trace_counts.most_common(args.trace_pc_top)
        )
        print(
            "pokemon-crystal-overworld-trace-pcs",
            f"frames={args.trace_pc_frames}",
            f"top={hot_pcs}",
            flush=True,
        )

    configure_profiles(emulator, True)
    emulator.cpu.consume_profile()
    emulator.bus.consume_profile()
    emulator.bus.ppu.consume_profile()
    emulator.bus.apu.consume_profile()
    emulator.bus.vram_dma_gdma_blocks = 0
    emulator.bus.vram_dma_hdma_blocks = 0
    emulator.bus.vram_dma_bytes = 0

    measured_start_wall_frame = wall_frame
    measured_start_ppu_frame = emulator.bus.ppu.frame_count
    measured_start_instructions = emulator.cpu.instructions
    measured_start_cycles = emulator.cpu.cycles
    totals = ProfileTotals(frame_times=[], audio_times=[], frame_instructions=[], frame_cycles=[])
    windows: list[dict[str, Any]] = []
    scenario = "overworld-audio" if args.audio_output else "overworld"

    profiler = cProfile.Profile() if args.cprofile is not None else None
    if profiler is not None:
        profiler.enable()
    try:
        for index in range(args.profile_frames):
            before_instructions = emulator.cpu.instructions
            before_cycles = emulator.cpu.cycles
            started = time.perf_counter()
            run_wall_frame(emulator, wall_frame=wall_frame, button_script=button_script)
            run_elapsed = time.perf_counter() - started
            audio_started = time.perf_counter()
            if args.audio_output:
                emulator.drain_audio_samples()
            audio_elapsed = time.perf_counter() - audio_started
            totals.frame_times.append(run_elapsed)
            totals.audio_times.append(audio_elapsed)
            totals.frame_instructions.append(emulator.cpu.instructions - before_instructions)
            totals.frame_cycles.append(emulator.cpu.cycles - before_cycles)
            consume_profiles(emulator, totals)
            wall_frame += 1
            if (index + 1) % args.window_size == 0:
                start = index + 1 - args.window_size
                window = summarize_window(
                    index=len(windows),
                    window_size=args.window_size,
                    totals=totals,
                    start=start,
                    end=index + 1,
                )
                windows.append(window)
                print_window_profile(scenario, window)
    finally:
        if profiler is not None:
            profiler.disable()
            args.cprofile.parent.mkdir(parents=True, exist_ok=True)
            profiler.dump_stats(args.cprofile)
            if args.cprofile_text is not None:
                args.cprofile_text.parent.mkdir(parents=True, exist_ok=True)
                with args.cprofile_text.open("w", encoding="utf-8") as output:
                    stats = pstats.Stats(profiler, stream=output)
                    stats.strip_dirs().sort_stats("cumtime").print_stats(60)

    frames = wall_frame - measured_start_wall_frame
    ppu_frames = emulator.bus.ppu.frame_count - measured_start_ppu_frame
    elapsed = sum(totals.frame_times)
    audio_elapsed = sum(totals.audio_times)
    active_elapsed = elapsed + audio_elapsed
    fps = frames / elapsed if elapsed > 0 else 0.0
    active_fps = frames / active_elapsed if active_elapsed > 0 else 0.0
    total_instructions = emulator.cpu.instructions - measured_start_instructions
    total_cycles = emulator.cpu.cycles - measured_start_cycles
    worst_window_fps = min((float(window["run_fps"]) for window in windows), default=fps)
    worst_window_active_fps = min(
        (float(window["active_fps"]) for window in windows),
        default=active_fps,
    )
    print(
        "pokemon-crystal-overworld-profile",
        f"scenario={scenario}",
        f"save_file={args.save_file}",
        f"rtc_file={args.save_file}.rtc",
        f"rtc_now={rtc_now:.6f}",
        f"rtc_halted={int(rtc_halted(args.save_file))}",
        f"warmup_frames={args.warmup_frames}",
        f"frames={frames}",
        f"ppu_frames={ppu_frames}",
        f"window_size={args.window_size}",
        f"windows={len(windows)}",
        f"run_ms={elapsed / frames * 1000:.2f}",
        "draw_ms=0.00",
        f"audio_ms={audio_elapsed / frames * 1000:.2f}",
        f"active_ms={active_elapsed / frames * 1000:.2f}",
        f"run_fps={fps:.2f}",
        f"active_fps={active_fps:.2f}",
        f"worst_window_fps={worst_window_fps:.2f}",
        f"worst_window_active_fps={worst_window_active_fps:.2f}",
        f"frame_ms_range={metric_range(totals.frame_times, 1000)}",
        f"cpu_instr={total_instructions}",
        f"cpu_frame_instr={int_range(totals.frame_instructions)}",
        f"cpu_cycles={total_cycles}",
        f"cpu_frame_cycles={int_range(totals.frame_cycles)}",
        f"cpu_interrupts={totals.cpu_interrupts}",
        f"cpu_halt_cycles={totals.cpu_halt_cycles}",
        f"cpu_halt_batches={totals.cpu_halt_batches}",
        f"bus_slow_cycles={totals.bus_slow_cycles}",
        f"bus_dma_cycles={totals.bus_dma_cycles}",
        f"bus_dma_starts={totals.bus_dma_starts}",
        f"bus_timer_overflows={totals.bus_timer_overflows}",
        f"vram_dma_gdma_blocks={totals.vram_dma_gdma_blocks}",
        f"vram_dma_hdma_blocks={totals.vram_dma_hdma_blocks}",
        f"vram_dma_bytes={totals.vram_dma_bytes}",
        f"audio_output={int(args.audio_output)}",
        f"apu_ms={totals.apu_seconds * 1000:.2f}",
        f"apu_samples={totals.apu_samples}",
        f"apu_dropped_samples={totals.apu_dropped_samples}",
        f"apu_reg_writes={totals.apu_reg_writes}",
        f"apu_triggers={totals.apu_triggers}",
        f"apu_disables={totals.apu_disables}",
        f"ppu_lines={totals.ppu_rendered_lines}/{totals.ppu_mode3_lines}",
        f"ppu_segments={totals.ppu_segments}",
        f"ppu_sprite_lines={totals.ppu_sprite_lines}",
        f"ppu_sprites={totals.ppu_selected_sprites}",
        f"ppu_max_sprites={totals.ppu_max_sprites}",
        f"ppu_obj_dots={totals.ppu_obj_dots}",
        f"ppu_win_dots={totals.ppu_window_dots}",
        f"ppu_bg_px={totals.ppu_bg_fast_pixels}/{totals.ppu_bg_slow_pixels}",
        f"ppu_win_px={totals.ppu_window_fast_pixels}/{totals.ppu_window_slow_pixels}",
        f"ppu_sprite_px={totals.ppu_sprite_pixels}",
        flush=True,
    )

    if args.min_fps is not None and fps < args.min_fps:
        print(f"FAIL Pokemon Crystal overworld profile FPS {fps:.2f} < {args.min_fps:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
