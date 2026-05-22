from __future__ import annotations

import argparse
from collections import Counter
import cProfile
import pstats
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from button_script import ButtonScript, parse_button_script  # noqa: E402
from emulator import Emulator  # noqa: E402


DEFAULT_ROM = ROOT / "roms" / "PRed.gb"
DEFAULT_BUTTON_SCRIPT = "4200:start:20,4550:start:20"
DEFAULT_WARMUP_FRAMES = 4680
DEFAULT_PROFILE_FRAMES = 240


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile the first Pokemon Red text/menu scene reproducibly."
    )
    parser.add_argument("--rom", type=Path, default=DEFAULT_ROM)
    parser.add_argument(
        "--button-script",
        default=DEFAULT_BUTTON_SCRIPT,
        help="Frame-based script that reaches the text scene.",
    )
    parser.add_argument("--warmup-frames", type=int, default=DEFAULT_WARMUP_FRAMES)
    parser.add_argument("--profile-frames", type=int, default=DEFAULT_PROFILE_FRAMES)
    parser.add_argument("--min-fps", type=float, help="Fail if measured emulation FPS is lower.")
    parser.add_argument(
        "--cprofile",
        type=Path,
        help="Write cProfile stats for the measured text-scene frames.",
    )
    parser.add_argument(
        "--audio-output",
        action="store_true",
        help="Enable APU sample generation during the measured frames.",
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
        help="Run this many measured frames with trace enabled and print hot PC counts.",
    )
    parser.add_argument("--trace-pc-top", type=int, default=20)
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
    if args.trace_pc_frames < 0:
        raise SystemExit("--trace-pc-frames must be non-negative")
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

    if args.trace_pc_frames:
        trace_counts: Counter[int] = Counter()

        def count_pc(line: str) -> None:
            try:
                trace_counts[int(line[:4], 16)] += 1
            except ValueError:
                return

        for _ in range(args.trace_pc_frames):
            apply_scripted_buttons(emulator, button_script, start_frame)
            emulator.run(max_frames=1, trace=True, trace_sink=count_pc)
        hot_pcs = " ".join(
            f"{pc:04X}:{count}" for pc, count in trace_counts.most_common(args.trace_pc_top)
        )
        print(
            "pokemon-red-text-trace-pcs",
            f"frames={args.trace_pc_frames}",
            f"top={hot_pcs}",
            flush=True,
        )

    configure_profiles(emulator, True)
    measured_start_frame = emulator.bus.ppu.frame_count
    measured_start_instructions = emulator.cpu.instructions
    measured_start_cycles = emulator.cpu.cycles
    frame_times: list[float] = []
    cpu_min_instructions: int | None = None
    cpu_max_instructions = 0
    cpu_halt_cycles = 0
    cpu_halt_batches = 0
    ppu_window_fast_pixels = 0
    ppu_window_slow_pixels = 0
    ppu_sprite_pixels = 0
    ppu_segments = 0

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
            ppu_profile = emulator.bus.ppu.consume_profile()
            cpu_halt_cycles += cpu_profile.halt_idle_cycles
            cpu_halt_batches += cpu_profile.halt_idle_batches
            ppu_window_fast_pixels += ppu_profile.window_fast_pixels
            ppu_window_slow_pixels += ppu_profile.window_slow_pixels
            ppu_sprite_pixels += ppu_profile.sprite_pixels
            ppu_segments += ppu_profile.render_segments
    finally:
        if profiler is not None:
            profiler.disable()
            args.cprofile.parent.mkdir(parents=True, exist_ok=True)
            profiler.dump_stats(args.cprofile)
            if args.cprofile_text is not None:
                args.cprofile_text.parent.mkdir(parents=True, exist_ok=True)
                with args.cprofile_text.open("w", encoding="utf-8") as output:
                    stats = pstats.Stats(profiler, stream=output)
                    stats.strip_dirs().sort_stats("cumtime").print_stats(40)

    frames = emulator.bus.ppu.frame_count - measured_start_frame
    elapsed = sum(frame_times)
    fps = frames / elapsed if elapsed > 0 else 0.0
    total_instructions = emulator.cpu.instructions - measured_start_instructions
    total_cycles = emulator.cpu.cycles - measured_start_cycles
    min_frame_ms = min(frame_times) * 1000
    max_frame_ms = max(frame_times) * 1000
    print(
        "pokemon-red-text-profile",
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
        f"audio_output={int(args.audio_output)}",
        f"ppu_segments={ppu_segments}",
        f"ppu_win_px={ppu_window_fast_pixels}/{ppu_window_slow_pixels}",
        f"ppu_sprite_px={ppu_sprite_pixels}",
        flush=True,
    )

    if args.min_fps is not None and fps < args.min_fps:
        print(f"FAIL Pokemon Red text profile FPS {fps:.2f} < {args.min_fps:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
