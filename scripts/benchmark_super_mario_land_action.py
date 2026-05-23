from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from button_script import ButtonScript, parse_button_script  # noqa: E402
from emulator import Emulator  # noqa: E402


DEFAULT_ROM = ROOT / "roms" / "SML.gb"
DEFAULT_BUTTON_SCRIPT = (
    "60:start:12,"
    "100:right+b:900,"
    "230:right+b+a:18,"
    "355:right+b+a:18,"
    "480:right+b+a:18,"
    "605:right+b+a:18"
)
DEFAULT_WARMUP_FRAMES = 120
DEFAULT_PROFILE_FRAMES = 600


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile the early Super Mario Land 1-1 action scene reproducibly."
    )
    parser.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    parser.add_argument(
        "--button-script",
        default=DEFAULT_BUTTON_SCRIPT,
        help="Frame-based script that starts the game and drives early 1-1 action.",
    )
    parser.add_argument("--warmup-frames", type=int, default=DEFAULT_WARMUP_FRAMES)
    parser.add_argument("--profile-frames", type=int, default=DEFAULT_PROFILE_FRAMES)
    parser.add_argument("--min-fps", type=float, help="Fail if measured emulation FPS is lower.")
    parser.add_argument(
        "--audio-output",
        action="store_true",
        help="Enable APU sample generation during the measured frames.",
    )
    parser.add_argument(
        "--cprofile",
        type=Path,
        help="Write cProfile stats for the measured action-scene frames.",
    )
    parser.add_argument(
        "--cprofile-text",
        type=Path,
        help="Write a human-readable cProfile summary.",
    )
    return parser.parse_args()


def configure_profiles(emulator: Emulator, enabled: bool) -> None:
    emulator.cpu.profile_enabled = enabled
    emulator.bus.profile_enabled = enabled
    emulator.bus.ppu.profile_enabled = enabled
    emulator.bus.apu.profile_enabled = enabled


def apply_scripted_buttons(
    emulator: Emulator,
    button_script: ButtonScript,
    start_frame: int,
) -> None:
    relative_frame = emulator.bus.ppu.frame_count - start_frame
    emulator.set_buttons(button_script.buttons_for_frame(relative_frame))


def run_frames(
    emulator: Emulator,
    *,
    frames: int,
    button_script: ButtonScript,
    start_frame: int,
) -> None:
    target = emulator.bus.ppu.frame_count + frames
    while emulator.bus.ppu.frame_count < target:
        apply_scripted_buttons(emulator, button_script, start_frame)
        emulator.run(max_frames=1)


def main() -> int:
    args = parse_args()
    if args.warmup_frames < 0:
        raise SystemExit("--warmup-frames must be non-negative")
    if args.profile_frames < 1:
        raise SystemExit("--profile-frames must be positive")

    button_script = parse_button_script(args.button_script)
    emulator = Emulator.from_rom_file(args.rom)
    start_frame = emulator.bus.ppu.frame_count

    run_frames(
        emulator,
        frames=args.warmup_frames,
        button_script=button_script,
        start_frame=start_frame,
    )
    emulator.bus.apu.set_output_enabled(args.audio_output)

    configure_profiles(emulator, True)
    emulator.cpu.consume_profile()
    emulator.bus.consume_profile()
    emulator.bus.ppu.consume_profile()
    emulator.bus.apu.consume_profile()

    measured_start_frame = emulator.bus.ppu.frame_count
    measured_start_instructions = emulator.cpu.instructions
    measured_start_cycles = emulator.cpu.cycles
    frame_times: list[float] = []
    cpu_min_instructions: int | None = None
    cpu_max_instructions = 0
    cpu_halt_cycles = 0
    cpu_halt_batches = 0
    bus_slow_cycles = 0
    bus_dma_cycles = 0
    bus_dma_starts = 0
    bus_timer_overflows = 0
    apu_seconds = 0.0
    apu_samples = 0
    apu_dropped_samples = 0
    apu_reg_writes = 0
    apu_triggers = 0
    apu_disables = 0
    ppu_mode3_lines = 0
    ppu_rendered_lines = 0
    ppu_segments = 0
    ppu_sprite_lines = 0
    ppu_selected_sprites = 0
    ppu_max_sprites = 0
    ppu_obj_dots = 0
    ppu_window_dots = 0
    ppu_bg_fast_pixels = 0
    ppu_bg_slow_pixels = 0
    ppu_window_fast_pixels = 0
    ppu_window_slow_pixels = 0
    ppu_sprite_pixels = 0

    profiler = cProfile.Profile() if args.cprofile is not None else None
    if profiler is not None:
        profiler.enable()
    try:
        for _ in range(args.profile_frames):
            apply_scripted_buttons(emulator, button_script, start_frame)
            before_instructions = emulator.cpu.instructions
            started = time.perf_counter()
            emulator.run(max_frames=1)
            if args.audio_output:
                emulator.drain_audio_samples()
            frame_times.append(time.perf_counter() - started)
            frame_instructions = emulator.cpu.instructions - before_instructions
            cpu_min_instructions = (
                frame_instructions
                if cpu_min_instructions is None
                else min(cpu_min_instructions, frame_instructions)
            )
            cpu_max_instructions = max(cpu_max_instructions, frame_instructions)

            cpu_profile = emulator.cpu.consume_profile()
            bus_profile = emulator.bus.consume_profile()
            ppu_profile = emulator.bus.ppu.consume_profile()
            apu_profile = emulator.bus.apu.consume_profile()
            cpu_halt_cycles += cpu_profile.halt_idle_cycles
            cpu_halt_batches += cpu_profile.halt_idle_batches
            bus_slow_cycles += bus_profile.slow_system_counter_cycles
            bus_dma_cycles += bus_profile.oam_dma_cycles
            bus_dma_starts += bus_profile.oam_dma_starts
            bus_timer_overflows += bus_profile.timer_overflows
            apu_seconds += apu_profile.tick_seconds
            apu_samples += apu_profile.generated_samples
            apu_dropped_samples += apu_profile.dropped_samples
            apu_reg_writes += apu_profile.register_writes
            apu_triggers += apu_profile.channel_triggers
            apu_disables += apu_profile.channel_disables
            ppu_mode3_lines += ppu_profile.mode3_lines
            ppu_rendered_lines += ppu_profile.rendered_lines
            ppu_segments += ppu_profile.render_segments
            ppu_sprite_lines += ppu_profile.sprite_lines
            ppu_selected_sprites += ppu_profile.selected_sprites
            ppu_max_sprites = max(ppu_max_sprites, ppu_profile.max_sprites_per_line)
            ppu_obj_dots += ppu_profile.obj_penalty_dots
            ppu_window_dots += ppu_profile.window_penalty_dots
            ppu_bg_fast_pixels += ppu_profile.bg_fast_pixels
            ppu_bg_slow_pixels += ppu_profile.bg_slow_pixels
            ppu_window_fast_pixels += ppu_profile.window_fast_pixels
            ppu_window_slow_pixels += ppu_profile.window_slow_pixels
            ppu_sprite_pixels += ppu_profile.sprite_pixels
    finally:
        if profiler is not None:
            profiler.disable()
            args.cprofile.parent.mkdir(parents=True, exist_ok=True)
            profiler.dump_stats(args.cprofile)
            if args.cprofile_text is not None:
                args.cprofile_text.parent.mkdir(parents=True, exist_ok=True)
                with args.cprofile_text.open("w", encoding="utf-8") as output:
                    stats = pstats.Stats(profiler, stream=output)
                    stats.strip_dirs().sort_stats("cumtime").print_stats(50)

    frames = emulator.bus.ppu.frame_count - measured_start_frame
    elapsed = sum(frame_times)
    fps = frames / elapsed if elapsed > 0 else 0.0
    total_instructions = emulator.cpu.instructions - measured_start_instructions
    total_cycles = emulator.cpu.cycles - measured_start_cycles
    min_frame_ms = min(frame_times) * 1000
    max_frame_ms = max(frame_times) * 1000
    print(
        "super-mario-land-action-profile",
        f"warmup_frames={args.warmup_frames}",
        f"frames={frames}",
        f"run_ms={elapsed / frames * 1000:.2f}",
        f"run_fps={fps:.2f}",
        f"frame_ms_range={min_frame_ms:.2f}-{max_frame_ms:.2f}",
        f"cpu_instr={total_instructions}",
        f"cpu_frame_instr={(cpu_min_instructions or 0)}-{cpu_max_instructions}",
        f"cpu_cycles={total_cycles}",
        f"cpu_halt_cycles={cpu_halt_cycles}",
        f"cpu_halt_batches={cpu_halt_batches}",
        f"bus_slow_cycles={bus_slow_cycles}",
        f"bus_dma_cycles={bus_dma_cycles}",
        f"bus_dma_starts={bus_dma_starts}",
        f"bus_timer_overflows={bus_timer_overflows}",
        f"audio_output={int(args.audio_output)}",
        f"apu_ms={apu_seconds * 1000:.2f}",
        f"apu_samples={apu_samples}",
        f"apu_dropped_samples={apu_dropped_samples}",
        f"apu_reg_writes={apu_reg_writes}",
        f"apu_triggers={apu_triggers}",
        f"apu_disables={apu_disables}",
        f"ppu_lines={ppu_rendered_lines}/{ppu_mode3_lines}",
        f"ppu_segments={ppu_segments}",
        f"ppu_sprite_lines={ppu_sprite_lines}",
        f"ppu_sprites={ppu_selected_sprites}",
        f"ppu_max_sprites={ppu_max_sprites}",
        f"ppu_obj_dots={ppu_obj_dots}",
        f"ppu_win_dots={ppu_window_dots}",
        f"ppu_bg_px={ppu_bg_fast_pixels}/{ppu_bg_slow_pixels}",
        f"ppu_win_px={ppu_window_fast_pixels}/{ppu_window_slow_pixels}",
        f"ppu_sprite_px={ppu_sprite_pixels}",
        flush=True,
    )

    if args.min_fps is not None and fps < args.min_fps:
        print(f"FAIL Super Mario Land action profile FPS {fps:.2f} < {args.min_fps:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
