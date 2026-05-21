from __future__ import annotations

import argparse
from pathlib import Path

from apu import DEFAULT_SAMPLE_RATE
from audio import WavAudioWriter
from debug import TraceLogger
from display import DisplayConfig, run_tk_display
from emulator import Emulator
from joypad import Joypad


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Milestone 1 DMG Game Boy CPU/ROM harness")
    parser.add_argument("rom", type=Path, help="Path to a .gb ROM")
    parser.add_argument(
        "--max-instructions",
        type=int,
        default=1_000_000,
        help="Stop after this many CPU instructions; use 0 for no limit",
    )
    parser.add_argument("--trace", action="store_true", help="Print disassembly/register trace")
    parser.add_argument("--trace-file", type=Path, help="Write trace output to a file")
    parser.add_argument("--boot-rom", type=Path, help="Optional DMG boot ROM to map at startup")
    parser.add_argument("--dump-frame", type=Path, help="Write the current PPU framebuffer as an ASCII PPM image")
    parser.add_argument("--dump-frame-bmp", type=Path, help="Write the current PPU framebuffer as a BMP image")
    parser.add_argument("--dump-audio", type=Path, help="Write generated APU samples as a stereo 16-bit WAV file")
    parser.add_argument(
        "--audio-sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Sample rate for --dump-audio and --window --audio",
    )
    parser.add_argument("--audio", action="store_true", help="Enable live audio playback in --window mode")
    parser.add_argument(
        "--audio-buffer-ms",
        type=int,
        default=DisplayConfig.audio_buffer_ms,
        help="Target live-audio buffer latency in milliseconds for --window --audio",
    )
    parser.add_argument(
        "--audio-chunk-ms",
        type=int,
        default=DisplayConfig.audio_chunk_ms,
        help="Live-audio submission chunk size in milliseconds for --window --audio",
    )
    parser.add_argument(
        "--capture-live-audio",
        type=Path,
        help="Write generated APU samples from live --window --audio playback to a WAV file",
    )
    parser.add_argument("--window", action="store_true", help="Run with a Tkinter display window and keyboard input")
    parser.add_argument("--scale", type=int, default=3, help="Window scale factor for --window")
    parser.add_argument("--fps", type=float, default=DisplayConfig.fps, help="Target display refresh rate for --window")
    parser.add_argument(
        "--frame-instruction-limit",
        type=int,
        default=200_000,
        help="Per-frame CPU instruction safety limit for --window",
    )
    parser.add_argument(
        "--profile-window",
        action="store_true",
        help="Print rolling timing for emulation and Tk framebuffer upload in --window mode",
    )
    parser.add_argument(
        "--profile-window-interval",
        type=int,
        default=DisplayConfig.profile_interval,
        help="Window frame count per --profile-window timing report",
    )
    parser.add_argument("--save-file", type=Path, help="Load cartridge RAM from this file and save it on exit")
    parser.add_argument(
        "--buttons",
        default="",
        help="Comma-separated held buttons for headless runs: a,b,select,start,right,left,up,down",
    )
    parser.add_argument("--frames", type=int, help="Stop after this many completed PPU frames")
    parser.add_argument("--step", action="store_true", help="Prompt before each instruction")
    parser.add_argument(
        "--stop-on-serial-result",
        action="store_true",
        help="Stop when serial output contains a Blargg-style Passed or Failed result",
    )
    parser.add_argument("--start-pc", type=lambda value: int(value, 0))
    parser.add_argument("--cold-boot-registers", action="store_true", help="Start registers at zero")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    boot_rom = args.boot_rom.read_bytes() if args.boot_rom is not None else None
    start_pc = args.start_pc if args.start_pc is not None else (0x0000 if boot_rom is not None else 0x0100)
    emulator = Emulator.from_rom_file(
        args.rom,
        start_pc=start_pc,
        post_boot=(not args.cold_boot_registers) and boot_rom is None,
        boot_rom=boot_rom,
    )
    if args.audio_sample_rate <= 0:
        raise SystemExit("--audio-sample-rate must be positive")
    if args.audio_buffer_ms <= 0:
        raise SystemExit("--audio-buffer-ms must be positive")
    if args.audio_chunk_ms <= 0:
        raise SystemExit("--audio-chunk-ms must be positive")
    if args.audio and not args.window:
        raise SystemExit("--audio requires --window")
    if args.capture_live_audio is not None and not args.window:
        raise SystemExit("--capture-live-audio requires --window")
    if args.max_instructions < 0:
        raise SystemExit("--max-instructions must be non-negative")
    if args.frames is not None and args.frames < 0:
        raise SystemExit("--frames must be non-negative")
    emulator.bus.apu.set_sample_rate(args.audio_sample_rate)
    if args.save_file is not None:
        emulator.load_save_file(args.save_file)

    print(emulator.cartridge.header.summary())
    print(emulator.cartridge.mapper_status)
    if not emulator.cartridge.is_supported_mapper:
        print("Warning: cartridge mapper is not implemented; execution may not be meaningful.")
    print()

    try:
        initial_buttons = Joypad.normalize_buttons(args.buttons)
        emulator.set_buttons(initial_buttons)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    max_instructions = None if args.max_instructions == 0 else args.max_instructions

    if args.window:
        if args.step or args.stop_on_serial_result or args.dump_audio:
            raise SystemExit("--window cannot be combined with step, audio dump, or serial-result stop modes")
        trace_enabled = args.trace or args.trace_file is not None
        with TraceLogger(args.trace_file) as logger:
            try:
                run_tk_display(
                    emulator,
                    config=DisplayConfig(
                        scale=args.scale,
                        fps=args.fps,
                        max_instructions_per_frame=args.frame_instruction_limit,
                        profile_window=args.profile_window,
                        profile_interval=args.profile_window_interval,
                        audio_enabled=args.audio,
                        audio_sample_rate=args.audio_sample_rate,
                        audio_buffer_ms=args.audio_buffer_ms,
                        audio_chunk_ms=args.audio_chunk_ms,
                        audio_capture_path=args.capture_live_audio,
                    ),
                    initial_buttons=initial_buttons,
                    max_frames=args.frames,
                    trace=trace_enabled,
                    trace_sink=logger.write if trace_enabled else None,
                )
            except (RuntimeError, ValueError) as exc:
                raise SystemExit(str(exc)) from exc
    else:
        trace_enabled = args.trace or args.trace_file is not None
        with TraceLogger(args.trace_file) as logger:
            audio_writer = (
                WavAudioWriter(args.dump_audio, sample_rate=args.audio_sample_rate)
                if args.dump_audio is not None
                else None
            )
            try:
                audio_sink = audio_writer.write if audio_writer is not None else None
                emulator.run(
                    max_instructions=max_instructions,
                    max_frames=args.frames,
                    stop_on_serial_result=args.stop_on_serial_result,
                    trace=trace_enabled,
                    trace_sink=logger.write if trace_enabled else None,
                    step_mode=args.step,
                    audio_sink=audio_sink,
                )
            finally:
                if audio_writer is not None:
                    audio_writer.close()

    print()
    print(
        f"Stopped after {emulator.cpu.instructions} instructions, "
        f"{emulator.cpu.cycles} cycles, {emulator.bus.ppu.frame_count} frames"
    )
    print(emulator.cpu.format_registers())
    if emulator.bus.serial_text:
        print(f"Serial output captured: {emulator.bus.serial_text!r}")
    if args.dump_frame is not None:
        emulator.bus.ppu.write_frame_ppm(args.dump_frame)
        print(f"Frame dumped to {args.dump_frame}")
    if args.dump_frame_bmp is not None:
        emulator.bus.ppu.write_frame_bmp(args.dump_frame_bmp)
        print(f"BMP frame dumped to {args.dump_frame_bmp}")
    if args.dump_audio is not None:
        print(f"Audio dumped to {args.dump_audio}")
    if args.save_file is not None and emulator.cartridge.has_persistent_data:
        emulator.save_save_file(args.save_file)
        print(f"Save data written to {args.save_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
